import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import fleet_apply
import fleet_plan
import multica_deploy


def valid_contract() -> dict:
    return {
        "contract_version": 1,
        "multica": {
            "server_url": "http://100.80.110.105:3010",
            "profile": "production",
            "workspace_id": "workspace-id",
            "backend_image": "ghcr.io/2233admin/multica-backend@sha256:" + "a" * 64,
            "web_image": "ghcr.io/2233admin/multica-web@sha256:" + "b" * 64,
        },
        "agx": {"version": "0.1.0", "installation_root": "/opt/agx"},
        "nodes": [
            {
                "name": "deploy-01",
                "node_identity": "agx-node-01",
                "platform": "linux",
                "labels": ["docker", "staging"],
            }
        ],
        "projects": [
            {
                "name": "project-a",
                "repository": "2233admin/project-a",
                "ref": "main",
                "environment": "staging",
            }
        ],
    }


class FakeAdapters:
    def __init__(self):
        self.calls = []
        self.reconcile_calls = 0
        self.fail_phase = None

    def _call(self, phase, *args):
        self.calls.append(phase)
        if phase == self.fail_phase:
            raise RuntimeError("temporary adapter failure")
        return {"phase": phase, "api_token": "must-not-leak"}

    def adapters(self):
        return fleet_apply.FleetApplyAdapters(
            multica=lambda contract: self._call("multica", contract),
            agx=lambda contract, node: self._call("agx", contract, node),
            connector=lambda contract, node: self._call("connector", contract, node),
            preflight=lambda contract, node: self._call("preflight", contract, node),
            reconcile=lambda contract, node: self._reconcile(contract, node),
        )

    def _reconcile(self, contract, node):
        self.reconcile_calls += 1
        return {"ready": True, "node": node["name"]}


class FleetApplyBehaviorTests(unittest.TestCase):
    @staticmethod
    def valid_agx_status(**overrides):
        payload = {
            "phase": "configured",
            "installation_id": "installation-01",
            "bundle_id": "bundle-01",
            "missing": [],
            "modified": [],
            "initialization": {"status": "initialized", "problems": []},
        }
        payload.update(overrides)
        return payload

    def test_reconcile_requires_structured_healthy_agx_status(self):
        args = type("Args", (), {"agx_bin": "agx"})()
        contract = fleet_plan.validate_contract(valid_contract())
        node = contract["nodes"][0]
        with (
            patch.object(
                multica_deploy,
                "node_remote_capture",
                side_effect=["AGX 0.1.0", json.dumps(self.valid_agx_status())],
            ) as capture,
            patch.object(multica_deploy, "apply_fleet_preflight", return_value={"operation": "preflight"}),
        ):
            result = multica_deploy.reconcile_fleet_apply(contract, node, args)

        self.assertTrue(result["ready"])
        self.assertEqual(result["agx_status"], "configured")
        self.assertIn("--output json", capture.call_args.args[1])

    def test_reconcile_rejects_human_malformed_failed_stale_and_wrong_status(self):
        contract = fleet_plan.validate_contract(valid_contract())
        node = contract["nodes"][0]
        args = type("Args", (), {"agx_bin": "agx"})()
        cases = (
            "AGX is ready",
            "not-json",
            self.valid_agx_status(phase="installing"),
            self.valid_agx_status(missing=["agent-control"]),
            self.valid_agx_status(modified=["bundle.json"]),
            self.valid_agx_status(initialization={"status": "initialized", "problems": ["repo"]}),
            self.valid_agx_status(installation_id=""),
            self.valid_agx_status(bundle_id=""),
            self.valid_agx_status(initialization={"status": "failed", "problems": []}),
        )
        for raw in cases:
            with self.subTest(raw=raw):
                output = raw if isinstance(raw, str) else json.dumps(raw)
                with (
                    patch.object(
                        multica_deploy,
                        "node_remote_capture",
                        side_effect=["AGX 0.1.0", output],
                    ),
                    patch.object(multica_deploy, "apply_fleet_preflight") as preflight,
                ):
                    with self.assertRaises(RuntimeError):
                        multica_deploy.reconcile_fleet_apply(contract, node, args)
                preflight.assert_not_called()

    def test_reconcile_rejects_empty_or_wrong_agx_version_from_separate_command(self):
        contract = fleet_plan.validate_contract(valid_contract())
        node = contract["nodes"][0]
        args = type("Args", (), {"agx_bin": "agx"})()
        for version in ("", "AGX 9.9.9"):
            with self.subTest(version=version):
                with (
                    patch.object(
                        multica_deploy,
                        "node_remote_capture",
                        side_effect=[version, json.dumps(self.valid_agx_status())],
                    ),
                    patch.object(multica_deploy, "apply_fleet_preflight") as preflight,
                ):
                    with self.assertRaises(RuntimeError):
                        multica_deploy.reconcile_fleet_apply(contract, node, args)
                preflight.assert_not_called()

    def test_apply_orders_boundaries_and_finishes_configured(self):
        fake = FakeAdapters()
        with tempfile.TemporaryDirectory() as directory:
            result = fleet_apply.apply_contract(
                valid_contract(),
                fake.adapters(),
                contract_path=Path(directory) / "contract.json",
                state_path=Path(directory) / "state.json",
            )
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "configured")
        self.assertEqual(fake.calls, ["multica", "agx", "connector", "preflight"])
        self.assertEqual(result["last_completed_phase"], "preflight")
        self.assertEqual(state["status"], "configured")
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertNotIn("must-not-leak", json.dumps(state))

    def test_repeated_apply_is_a_no_op(self):
        fake = FakeAdapters()
        with tempfile.TemporaryDirectory() as directory:
            kwargs = {
                "contract_path": Path(directory) / "contract.json",
                "state_path": Path(directory) / "state.json",
            }
            first = fleet_apply.apply_contract(valid_contract(), fake.adapters(), **kwargs)
            fake.calls.clear()
            second = fleet_apply.apply_contract(valid_contract(), fake.adapters(), **kwargs)

        self.assertFalse(first["no_op"])
        self.assertTrue(second["no_op"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.reconcile_calls, 1)
        self.assertEqual([phase["status"] for phase in second["phases"]], ["skipped"] * 4)

    def test_stale_state_reconciles_before_replaying_phases(self):
        fake = FakeAdapters()
        stale = {"value": False}

        def reconcile(contract, node):
            fake.reconcile_calls += 1
            return stale["value"]

        adapters = fleet_apply.FleetApplyAdapters(
            multica=lambda contract: fake._call("multica", contract),
            agx=lambda contract, node: fake._call("agx", contract, node),
            connector=lambda contract, node: fake._call("connector", contract, node),
            preflight=lambda contract, node: fake._call("preflight", contract, node),
            reconcile=reconcile,
        )
        with tempfile.TemporaryDirectory() as directory:
            kwargs = {
                "contract_path": Path(directory) / "contract.json",
                "state_path": Path(directory) / "state.json",
            }
            fleet_apply.apply_contract(valid_contract(), adapters, **kwargs)
            fake.calls.clear()
            replayed = fleet_apply.apply_contract(valid_contract(), adapters, **kwargs)

        self.assertFalse(replayed["no_op"])
        self.assertEqual(fake.calls, ["multica", "agx", "connector", "preflight"])

    def test_reconcile_failure_never_returns_no_op_or_mutates(self):
        fake = FakeAdapters()

        def reconcile(contract, node):
            fake.reconcile_calls += 1
            raise RuntimeError("stale readiness")

        adapters = fleet_apply.FleetApplyAdapters(
            multica=lambda contract: fake._call("multica", contract),
            agx=lambda contract, node: fake._call("agx", contract, node),
            connector=lambda contract, node: fake._call("connector", contract, node),
            preflight=lambda contract, node: fake._call("preflight", contract, node),
            reconcile=reconcile,
        )
        with tempfile.TemporaryDirectory() as directory:
            kwargs = {
                "contract_path": Path(directory) / "contract.json",
                "state_path": Path(directory) / "state.json",
            }
            fleet_apply.apply_contract(valid_contract(), fake.adapters(), **kwargs)
            fake.calls.clear()
            with self.assertRaises(fleet_apply.FleetApplyError) as raised:
                fleet_apply.apply_contract(valid_contract(), adapters, **kwargs)

        self.assertEqual(raised.exception.code, "reconcile_failed")
        self.assertEqual(fake.calls, [])

    def test_failure_records_last_completed_phase_and_retry_skips_it(self):
        fake = FakeAdapters()
        fake.fail_phase = "agx"
        with tempfile.TemporaryDirectory() as directory:
            kwargs = {
                "contract_path": Path(directory) / "contract.json",
                "state_path": Path(directory) / "state.json",
            }
            failed = fleet_apply.apply_contract(valid_contract(), fake.adapters(), **kwargs)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failed_phase"], "agx")
            self.assertEqual(failed["last_completed_phase"], "multica")

            fake.fail_phase = None
            fake.calls.clear()
            resumed = fleet_apply.apply_contract(valid_contract(), fake.adapters(), **kwargs)

        self.assertEqual(resumed["status"], "configured")
        self.assertEqual(fake.calls, ["agx", "connector", "preflight"])
        self.assertIn("--resume", resumed["retry_command"])

    def test_invalid_contract_is_rejected_before_any_adapter_call(self):
        fake = FakeAdapters()
        contract = copy.deepcopy(valid_contract())
        contract["nodes"][0]["private_key"] = "not-allowed"

        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_apply.apply_contract(contract, fake.adapters())

        self.assertEqual(fake.calls, [])

    def test_apply_parser_does_not_remove_existing_commands(self):
        parser = multica_deploy.build_parser()
        apply_args = parser.parse_args(
            ["fleet", "apply", "--contract", "contract.json", "--node-host", "node"]
        )
        self.assertEqual(apply_args.fleet_command, "apply")
        for command in ("deploy", "upgrade", "build", "client-bootstrap"):
            if command == "client-bootstrap":
                continue
            parsed = parser.parse_args([command]) if command == "build" else None
            if parsed is not None:
                self.assertEqual(parsed.command, command)

    def test_compose_supports_full_digest_refs_without_breaking_tag_inputs(self):
        compose = (multica_deploy.PACKAGE_ROOT / "docker-compose.selfhost.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("MULTICA_BACKEND_REF", compose)
        self.assertIn("MULTICA_WEB_REF", compose)
        self.assertIn("MULTICA_IMAGE_TAG", compose)

    def test_image_contract_keeps_backend_and_web_refs_separate(self):
        args = multica_deploy.build_parser().parse_args(
            [
                "fleet",
                "apply",
                "--contract",
                "contract.json",
                "--node-host",
                "node",
                "--agx-github-owner",
                "octo-lab",
                "--agx-provider",
                "codex",
            ]
        )
        with patch.object(multica_deploy, "deploy") as deploy:
            result = multica_deploy.apply_fleet_multica(
                fleet_plan.validate_contract(valid_contract()), args
            )

        self.assertEqual(result["backend_image"], valid_contract()["multica"]["backend_image"])
        self.assertEqual(result["web_image"], valid_contract()["multica"]["web_image"])
        self.assertEqual(args.backend_ref, valid_contract()["multica"]["backend_image"])
        self.assertEqual(args.web_ref, valid_contract()["multica"]["web_image"])
        deploy.assert_called_once_with(args)

    def test_source_revision_comparison_accepts_40_and_64_digit_forms(self):
        short = "a" * 40
        full = "a" * 64
        self.assertTrue(multica_deploy._source_revisions_match(short, full.upper()))
        self.assertTrue(multica_deploy._source_revisions_match(full, short))
        self.assertFalse(multica_deploy._source_revisions_match(short, "b" * 40))
        self.assertFalse(multica_deploy._source_revisions_match(short, "not-a-sha"))

    def test_missing_agx_init_args_is_rejected_before_fake_mutation(self):
        fake = FakeAdapters()
        parser = multica_deploy.build_parser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(valid_contract()), encoding="utf-8")
            args = parser.parse_args(
                [
                    "fleet",
                    "apply",
                    "--contract",
                    str(contract_path),
                    "--state-file",
                    str(root / "state.json"),
                    "--nas-host",
                    "nas",
                    "--nas-ip",
                    "100.80.110.105",
                    "--node-host",
                    "node",
                    "--json",
                ]
            )
            output = StringIO()
            with redirect_stdout(output):
                result = multica_deploy.run_fleet_apply(
                    args,
                    adapters=fake.adapters(),
                    which=lambda _name: "/test/tool",
                )

        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "apply_error")
        self.assertEqual(fake.calls, [])

    def test_top_level_apply_cli_can_use_fake_mutations(self):
        fake = FakeAdapters()
        parser = multica_deploy.build_parser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract_path = root / "contract.json"
            state_path = root / "state.json"
            contract_path.write_text(json.dumps(valid_contract()), encoding="utf-8")
            args = parser.parse_args(
                [
                    "fleet",
                    "apply",
                    "--contract",
                    str(contract_path),
                    "--state-file",
                    str(state_path),
                    "--nas-host",
                    "nas",
                    "--nas-ip",
                    "100.80.110.105",
                    "--node-host",
                    "node",
                    "--agx-github-owner",
                    "octo-lab",
                    "--agx-provider",
                    "codex",
                    "--json",
                ]
            )
            output = StringIO()
            with redirect_stdout(output):
                result = multica_deploy.run_fleet_apply(
                    args,
                    adapters=fake.adapters(),
                    which=lambda _name: "/test/tool",
                )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "configured")
        self.assertEqual(fake.calls, ["multica", "agx", "connector", "preflight"])


if __name__ == "__main__":
    unittest.main()
