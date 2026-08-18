import copy
import json
import unittest

from fleet_verify import FleetVerifier


def valid_contract() -> dict:
    return {
        "contract_version": 1,
        "multica": {"workspace_id": "workspace-id"},
        "agx": {"version": "0.1.0"},
        "nodes": [{"name": "deploy-01", "node_identity": "node-identity-01"}],
    }


def multica_evidence() -> dict:
    return {
        "health": {"status": "healthy", "mock": False},
        "readiness": {"status": "ready", "ready": True, "mock": False},
        "auth": {"status": "authenticated", "authenticated": True, "mock": False},
        "workspace": {
            "status": "available",
            "workspace_id": "workspace-id",
            "mock": False,
        },
        "runtime": {
            "status": "online",
            "runtime_id": "runtime-01",
            "online": True,
            "mock": False,
        },
    }


def agx_evidence() -> dict:
    return {
        "installation": {
            "status": "installed",
            "installation_id": "installation-01",
            "version": "0.1.0",
            "mock": False,
        },
        "version": {"version": "0.1.0", "mock": False},
        "bundle": {
            "status": "installed",
            "bundle_id": "bundle-01",
            "version": "0.1.0",
            "mock": False,
        },
        "node": {
            "status": "registered",
            "node_identity": "node-identity-01",
            "mock": False,
        },
        "lifecycle": {"status": "ready", "mock": False},
    }


def task_evidence() -> dict:
    common = {
        "task_id": "task-01",
        "deployment_id": "deployment-01",
        "node_identity": "node-identity-01",
        "status": "completed",
        "health": "healthy",
        "mock": False,
    }
    return {
        "multica": {"receipt": dict(common), "readback": dict(common)},
        "agx": {"receipt": dict(common), "readback": dict(common)},
    }


class FleetVerifyTests(unittest.TestCase):
    def make_verifier(self, *, multica=None, agx=None, task=None, calls=None):
        calls = calls if calls is not None else []
        multica = multica or multica_evidence()
        agx = agx or agx_evidence()
        task = task or task_evidence()

        def read_multica(_context):
            calls.append("multica")
            return copy.deepcopy(multica)

        def read_agx(_context):
            calls.append("agx")
            return copy.deepcopy(agx)

        def run_task(context):
            calls.append(("task", context["task"]["disposable"]))
            return copy.deepcopy(task)

        return FleetVerifier(
            multica_reader=read_multica,
            agx_reader=read_agx,
            task_runner=run_task,
        ), calls

    def test_matching_live_receipts_are_verified(self):
        verifier, calls = self.make_verifier()

        result = verifier.verify(valid_contract())

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["task_id"], "task-01")
        self.assertEqual(result["deployment_id"], "deployment-01")
        self.assertEqual(result["node_identity"], "node-identity-01")
        self.assertEqual(calls, ["multica", "agx", ("task", True)])

    def test_v_prefixed_agx_versions_match_contract(self):
        contract = valid_contract()
        contract["agx"]["version"] = "v0.1.0"
        evidence = agx_evidence()
        for section in ("installation", "version", "bundle"):
            evidence[section]["version"] = "v0.1.0"
        verifier, _calls = self.make_verifier(agx=evidence)

        result = verifier.verify(contract)

        self.assertEqual(result["status"], "verified")

    def test_missing_runtime_awaits_verification_without_running_task(self):
        multica = multica_evidence()
        del multica["runtime"]
        verifier, calls = self.make_verifier(multica=multica)

        result = verifier.verify(valid_contract())

        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["code"], "missing_evidence")
        self.assertEqual(calls, ["multica", "agx"])

    def test_missing_agx_receipt_awaits_verification(self):
        task = task_evidence()
        del task["agx"]["receipt"]
        verifier, _calls = self.make_verifier(task=task)

        result = verifier.verify(valid_contract())

        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["code"], "missing_task_evidence")

    def test_task_id_mismatch_is_blocked(self):
        task = task_evidence()
        task["agx"]["readback"]["task_id"] = "other-task"
        verifier, _calls = self.make_verifier(task=task)

        result = verifier.verify(valid_contract())

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "evidence_mismatch")

    def test_task_node_identity_must_match_verified_agx_node(self):
        task = task_evidence()
        for side in task.values():
            for record in side.values():
                record["node_identity"] = "another-node"
        verifier, _calls = self.make_verifier(task=task)

        result = verifier.verify(valid_contract())

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "evidence_mismatch")

    def test_deployment_id_must_be_nonempty_and_match_on_both_sides(self):
        for deployment_id in ("", "other-deployment"):
            with self.subTest(deployment_id=deployment_id):
                task = task_evidence()
                task["multica"]["receipt"]["deployment_id"] = deployment_id
                task["multica"]["readback"]["deployment_id"] = deployment_id
                verifier, _calls = self.make_verifier(task=task)

                result = verifier.verify(valid_contract())

                self.assertEqual(
                    result["status"],
                    "blocked" if deployment_id else "awaiting_verification",
                )

    def test_task_node_identity_must_match_contract_and_explicit_node(self):
        verifier, _calls = self.make_verifier()

        contract = valid_contract()
        contract["nodes"][0]["node_identity"] = "contract-node"
        result = verifier.verify(
            contract,
            node={"name": "deploy-01", "node_identity": "request-node"},
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "evidence_mismatch")

    def test_contract_node_identity_is_required_for_agx_and_task_binding(self):
        contract = valid_contract()
        del contract["nodes"][0]["node_identity"]
        verifier, calls = self.make_verifier()

        result = verifier.verify(contract)

        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["code"], "missing_evidence")
        self.assertEqual(calls, [])

    def test_another_node_with_self_consistent_agx_and_task_evidence_is_rejected(self):
        contract = valid_contract()
        other = "another-node"
        agx = agx_evidence()
        agx["node"]["node_identity"] = other
        task = task_evidence()
        for side in task.values():
            for record in side.values():
                record["node_identity"] = other
        verifier, _calls = self.make_verifier(agx=agx, task=task)

        result = verifier.verify(contract)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "evidence_mismatch")

    def test_mock_or_incomplete_evidence_cannot_verify(self):
        mocked = task_evidence()
        mocked["agx"]["receipt"]["mock"] = True
        verifier, _calls = self.make_verifier(task=mocked)
        self.assertEqual(verifier.verify(valid_contract())["status"], "blocked")
        self.assertEqual(verifier.verify(valid_contract())["code"], "mock_evidence")

        incomplete = agx_evidence()
        del incomplete["bundle"]
        verifier, _calls = self.make_verifier(agx=incomplete)
        result = verifier.verify(valid_contract())
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["code"], "missing_evidence")

    def test_repeated_verification_is_stable_and_redacted(self):
        secret = "do-not-write-this-token"
        task = task_evidence()
        task["multica"]["receipt"]["token"] = secret
        calls = []
        verifier, calls = self.make_verifier(task=task, calls=calls)

        first = verifier.verify(valid_contract())
        second = verifier.verify(valid_contract())

        self.assertEqual(first, second)
        self.assertEqual(calls.count(("task", True)), 2)
        rendered = json.dumps(first, sort_keys=True)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("token", rendered.lower())


if __name__ == "__main__":
    unittest.main()
