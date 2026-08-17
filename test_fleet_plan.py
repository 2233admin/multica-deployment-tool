import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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


class FleetPlanBehaviorTests(unittest.TestCase):
    @staticmethod
    def tools_available(_name: str) -> str:
        return "/test/tool"

    def test_valid_contract_produces_read_only_plan_and_ordered_phases(self):
        contract = fleet_plan.validate_contract(valid_contract())
        plan = fleet_plan.build_plan(contract, which=self.tools_available)

        self.assertTrue(plan["read_only"])
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(
            [phase["name"] for phase in plan["apply_phases"]],
            ["multica", "agx", "connector", "preflight"],
        )
        self.assertEqual(plan["nodes"][0]["name"], "deploy-01")
        self.assertEqual(plan["nodes"][0]["node_identity"], "agx-node-01")
        self.assertEqual(plan["disposable_task"]["project"], "project-a")
        self.assertEqual(
            [tool["name"] for tool in plan["required_tools"]], ["ssh", "scp"]
        )
        self.assertEqual(
            plan["multica"]["backend_image"], contract["multica"]["backend_image"]
        )
        self.assertEqual(plan["multica"]["web_image"], contract["multica"]["web_image"])

    def test_external_cli_seam_returns_human_plan_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(valid_contract()), encoding="utf-8")
            output = StringIO()
            with (
                patch("fleet_plan.shutil.which", side_effect=self.tools_available),
                patch.object(multica_deploy, "deploy") as deploy,
                patch.object(multica_deploy, "remote") as remote,
                redirect_stdout(output),
            ):
                result = multica_deploy.main(
                    ["fleet", "plan", "--contract", str(path)]
                )

        self.assertEqual(result, 0)
        self.assertIn("Fleet plan: planned", output.getvalue())
        self.assertIn("apply_phases:", output.getvalue())
        deploy.assert_not_called()
        remote.assert_not_called()

    def test_json_output_is_stable_and_contains_no_mutation_claim(self):
        contract = fleet_plan.validate_contract(valid_contract())
        plan = fleet_plan.build_plan(contract, which=self.tools_available)

        first = fleet_plan.render(plan, fleet_plan.OUTPUT_JSON)
        second = fleet_plan.render(plan, fleet_plan.OUTPUT_JSON)

        self.assertEqual(first, second)
        payload = json.loads(first)
        self.assertEqual(payload["status"], "planned")
        self.assertTrue(payload["read_only"])
        self.assertNotIn("apply_result", payload)

    def test_secret_shaped_fields_are_rejected_before_unknown_field_handling(self):
        contract = valid_contract()
        contract["multica"]["api_token"] = "do-not-accept"

        with self.assertRaises(fleet_plan.FleetPlanError) as raised:
            fleet_plan.validate_contract(contract)

        self.assertEqual(raised.exception.code, "secret_field")
        self.assertEqual(raised.exception.path, "$.multica.api_token")
        self.assertNotIn("do-not-accept", str(raised.exception))

    def test_mutable_image_and_source_references_are_rejected(self):
        image_contract = valid_contract()
        image_contract["multica"]["backend_image"] = "ghcr.io/example/multica-backend:latest"
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(image_contract)

        source_contract = valid_contract()
        source_contract["multica"].pop("backend_image")
        source_contract["multica"].pop("web_image")
        source_contract["multica"]["source_revision"] = "main"
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(source_contract)

    def test_full_source_revision_is_accepted_and_requires_docker(self):
        contract = valid_contract()
        contract["multica"].pop("backend_image")
        contract["multica"].pop("web_image")
        contract["multica"]["source_revision"] = "b" * 40

        normalized = fleet_plan.validate_contract(contract)
        plan = fleet_plan.build_plan(normalized, which=self.tools_available)

        self.assertEqual(normalized["multica"]["source_revision"], "b" * 40)
        self.assertEqual(
            [tool["name"] for tool in plan["required_tools"]],
            ["ssh", "scp", "docker", "git"],
        )

    def test_source_revision_64_hex_is_accepted(self):
        contract = valid_contract()
        contract["multica"].pop("backend_image")
        contract["multica"].pop("web_image")
        contract["multica"]["source_revision"] = "C" * 64

        normalized = fleet_plan.validate_contract(contract)
        self.assertEqual(normalized["multica"]["source_revision"], "c" * 64)

    def test_legacy_single_image_and_unsupported_platform_are_rejected(self):
        contract = valid_contract()
        contract["multica"]["image"] = contract["multica"].pop("backend_image")
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(contract)

    def test_linux_v1_rejects_windows_drive_and_unc_installation_roots(self):
        for installation_root in (r"C:\opt\agx", r"\\server\share\agx", "//server/share/agx"):
            with self.subTest(installation_root=installation_root):
                contract = valid_contract()
                contract["agx"]["installation_root"] = installation_root

                with self.assertRaises(fleet_plan.FleetPlanError):
                    fleet_plan.validate_contract(contract)

        contract = valid_contract()
        contract["nodes"][0]["platform"] = "windows"
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(contract)

    def test_node_identity_is_required_and_normalized_separately_from_name(self):
        contract = valid_contract()
        contract["nodes"][0]["node_identity"] = "agx-node-01"

        normalized = fleet_plan.validate_contract(contract)

        self.assertEqual(normalized["nodes"][0]["name"], "deploy-01")
        self.assertEqual(normalized["nodes"][0]["node_identity"], "agx-node-01")

        missing = valid_contract()
        del missing["nodes"][0]["node_identity"]
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(missing)

    def test_unknown_contract_version_is_rejected(self):
        contract = valid_contract()
        contract["contract_version"] = 2

        with self.assertRaises(fleet_plan.FleetPlanError) as raised:
            fleet_plan.validate_contract(contract)

        self.assertEqual(raised.exception.code, "unsupported_contract_version")

    def test_malformed_url_is_a_contract_error_not_a_traceback(self):
        contract = valid_contract()
        contract["multica"]["server_url"] = "http://[not-an-ipv6-host"

        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(contract)

    def test_one_node_and_one_project_are_frozen_for_v1(self):
        contract = valid_contract()
        contract["nodes"].append(copy.deepcopy(contract["nodes"][0]))
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(contract)

        contract = valid_contract()
        contract["projects"].append(copy.deepcopy(contract["projects"][0]))
        with self.assertRaises(fleet_plan.FleetPlanError):
            fleet_plan.validate_contract(contract)

    def test_invalid_cli_plan_does_not_invoke_deployment_actions(self):
        contract = valid_contract()
        contract["multica"]["password"] = "must-not-be-used"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(contract), encoding="utf-8")
            output = StringIO()
            with (
                patch.object(multica_deploy, "deploy") as deploy,
                patch.object(multica_deploy, "upgrade") as upgrade,
                patch.object(multica_deploy, "remote") as remote,
                redirect_stdout(output),
            ):
                result = multica_deploy.main(
                    ["fleet", "plan", "--contract", str(path), "--json"]
                )

        self.assertNotEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid")
        deploy.assert_not_called()
        upgrade.assert_not_called()
        remote.assert_not_called()

    def test_missing_local_tool_is_reported_as_nonzero_json_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(valid_contract()), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = fleet_plan.run_plan(
                    path,
                    output_format=fleet_plan.OUTPUT_JSON,
                    which=lambda _name: None,
                )

        self.assertNotEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid")
        self.assertEqual(
            json.loads(output.getvalue())["error"]["code"], "missing_local_tool"
        )


if __name__ == "__main__":
    unittest.main()
