import copy
import json
import unittest

from fleet_multi import FleetMultiError, build_multi_plan


def base_config() -> dict:
    return {
        "workspaces": [
            {"identity": "workspace-a", "name": "Workspace A", "profile": "production"}
        ],
        "environments": [
            {"identity": "staging", "name": "Staging", "workspace_id": "workspace-a"}
        ],
        "nodes": [
            {
                "identity": "node-01",
                "name": "worker-01",
                "labels": {"role": "worker", "zone": "east"},
            }
        ],
        "projects": [
            {
                "identity": "project-a",
                "name": "Project A",
                "environment": "staging",
                "selector": {"identity": "node-01"},
            }
        ],
    }


class FleetMultiTests(unittest.TestCase):
    def test_selector_uses_stable_identity_and_labels(self):
        config = base_config()
        config["nodes"].append(
            {
                "identity": "node-02",
                "name": "worker-02",
                "labels": {"role": "worker", "zone": "west"},
            }
        )
        config["projects"][0]["selector"] = {"labels": {"zone": "west"}}

        result = build_multi_plan(config)

        self.assertEqual([node["identity"] for node in result["nodes"]], ["node-02"])
        self.assertEqual(result["groups"][0]["environment"], "staging")

        config["projects"][0]["selector"] = {"name": "does-not-exist"}
        with self.assertRaisesRegex(FleetMultiError, "no node matches"):
            build_multi_plan(config)

        config["projects"][0]["selector"] = {"labels": {"role": "worker"}}
        with self.assertRaisesRegex(FleetMultiError, "matches multiple"):
            build_multi_plan(config)

    def test_duplicate_node_identity_is_rejected(self):
        config = base_config()
        config["nodes"].append(
            {"identity": "node-01", "name": "worker-copy", "labels": {"role": "worker"}}
        )

        with self.assertRaisesRegex(FleetMultiError, "duplicate node identity"):
            build_multi_plan(config)

    def test_cross_workspace_or_profile_mixing_is_rejected(self):
        config = base_config()
        config["workspaces"].append(
            {"identity": "workspace-b", "name": "Workspace B", "profile": "production"}
        )
        config["nodes"][0]["workspace_id"] = "workspace-b"

        with self.assertRaisesRegex(FleetMultiError, "cross-workspace"):
            build_multi_plan(config)

        config = base_config()
        config["workspaces"][0]["profile"] = "staging-profile"
        config["projects"][0]["profile"] = "production"
        with self.assertRaisesRegex(FleetMultiError, "cross-profile"):
            build_multi_plan(config)

    def test_status_aggregation_keeps_all_node_states_and_agx_links(self):
        config = base_config()
        config["nodes"] = []
        config["projects"] = []
        statuses = ["planned", "configured", "healthy", "busy", "failed", "unavailable"]
        for index, status in enumerate(statuses, start=1):
            identity = f"node-{index:02d}"
            config["nodes"].append(
                {
                    "identity": identity,
                    "name": f"worker-{index:02d}",
                    "labels": {"slot": str(index)},
                    "agx_receipt": {"receipt_id": f"receipt-{index}", "api_token": "secret"},
                    "agx_task_link": f"agx://task/{index}",
                }
            )
            config["projects"].append(
                {
                    "identity": f"project-{index:02d}",
                    "name": f"Project {index}",
                    "environment": "staging",
                    "selector": {"identity": identity},
                }
            )
            config.setdefault("statuses", {})[identity] = status

        result = build_multi_plan(config)

        self.assertEqual(result["status_counts"], {status: 1 for status in statuses})
        self.assertEqual(
            [node["status"] for node in result["nodes"]], statuses
        )
        self.assertTrue(all(node["status_flags"]["planned"] for node in result["nodes"]))
        self.assertEqual(result["nodes"][0]["agx_receipt"]["api_token"], "<redacted>")
        self.assertEqual(result["nodes"][0]["agx_task_link"], "agx://task/1")
        self.assertEqual(result["live_gate"], "not_run")
        self.assertFalse(result["execution_policy"]["executed"])

    def test_serial_is_default_and_parallel_is_only_declared(self):
        result = build_multi_plan(base_config())

        self.assertEqual(
            result["execution_policy"],
            {"mode": "serial", "max_concurrency": 1, "executed": False},
        )

        config = base_config()
        config["policy"] = {"mode": "parallel", "max_concurrency": 3}
        parallel = build_multi_plan(config)
        self.assertEqual(parallel["execution_policy"]["mode"], "parallel")
        self.assertEqual(parallel["execution_policy"]["max_concurrency"], 3)
        self.assertFalse(parallel["execution_policy"]["executed"])

    def test_sensitive_unknown_fields_are_rejected_and_output_is_redacted(self):
        config = base_config()
        config["api_token"] = "must-not-be-accepted"

        with self.assertRaises(FleetMultiError) as raised:
            build_multi_plan(config)
        self.assertEqual(raised.exception.code, "sensitive_field")
        self.assertNotIn("must-not-be-accepted", str(raised.exception))

        config = base_config()
        config["nodes"][0]["agx_receipt"] = {
            "receipt_id": "receipt-01",
            "nested": {"password": "do-not-leak"},
            "message": "token=do-not-leak",
        }
        rendered = json.dumps(build_multi_plan(config), sort_keys=True)
        self.assertNotIn("do-not-leak", rendered)
        self.assertIn("<redacted>", rendered)

    def test_multica_cannot_be_declared_as_fleet_authority(self):
        config = base_config()
        config["authority"] = "multica"

        with self.assertRaises(FleetMultiError) as raised:
            build_multi_plan(config)

        self.assertEqual(raised.exception.code, "authority_violation")


if __name__ == "__main__":
    unittest.main()
