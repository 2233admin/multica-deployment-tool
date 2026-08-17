import json
import subprocess
import unittest

from agx_multica_connector import (
    AgxMulticaConnector,
    CommandResult,
    ConnectorResult,
    MulticaCliConfig,
    TaskReference,
)


SCHEMA = "multica.agx-connector/v1"
CLI_VERSION = "0.4.26"


def success_payload(**overrides):
    payload = {
        "schema": SCHEMA,
        "cli_version": CLI_VERSION,
        "task_id": "task-123",
        "deployment_id": "deployment-456",
        "node_identity": "node-01",
        "status": "completed",
        "health": "healthy",
        "rollback_available": True,
        "summary": {"result": "ready", "duration_ms": 42},
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    def __init__(self, *, stdout="", returncode=0, stderr="", error=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.error is not None:
            raise self.error
        return CommandResult(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class AgxMulticaConnectorTests(unittest.TestCase):
    def setUp(self):
        self.config = MulticaCliConfig(
            command=("multica", "connector", "dispatch"),
            version=CLI_VERSION,
            json_schema=SCHEMA,
            timeout_seconds=7.5,
        )
        self.task = TaskReference(
            task_id="task-123",
            repository="2233admin/project-a",
            ref="main",
            environment="staging",
            action="deploy",
            target_selector="node-01",
        )

    def test_compatibility_is_configured_metadata_not_a_live_verified_claim(self):
        result = AgxMulticaConnector(self.config, runner=FakeRunner()).compatibility()

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "compatible")
        self.assertEqual(result.payload["cli_version"], CLI_VERSION)
        self.assertEqual(result.payload["json_schema"], SCHEMA)
        self.assertNotIn("verified", json.dumps(result.payload))

    def test_success_uses_structured_cli_arguments_and_returns_redacted_result(self):
        runner = FakeRunner(stdout=json.dumps(success_payload()))
        result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.payload["deployment_id"], "deployment-456")
        self.assertEqual(result.payload["node_identity"], "node-01")
        self.assertEqual(result.payload["rollback_available"], True)
        argv, timeout = runner.calls[0]
        self.assertEqual(argv[:3], list(self.config.command))
        self.assertIn("--output", argv)
        self.assertIn("json", argv)
        self.assertIn("--schema", argv)
        self.assertIn(SCHEMA, argv)
        self.assertIn("--version", argv)
        self.assertIn(CLI_VERSION, argv)
        self.assertIn("--task-id", argv)
        self.assertIn("task-123", argv)
        self.assertIn("--repository", argv)
        self.assertIn("2233admin/project-a", argv)
        self.assertEqual(timeout, 7.5)

    def test_timeout_is_a_typed_result_without_command_output(self):
        runner = FakeRunner(
            error=subprocess.TimeoutExpired(
                cmd=["multica", "connector", "dispatch"], timeout=7.5
            )
        )

        result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "timeout")
        self.assertIsNone(result.payload)
        self.assertIsNone(result.exit_code)

    def test_nonzero_exit_is_captured_without_leaking_stderr(self):
        secret = "token=do-not-leak"
        runner = FakeRunner(returncode=17, stderr=f"auth failed {secret}")

        result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "nonzero_exit")
        self.assertEqual(result.exit_code, 17)
        self.assertNotIn(secret, result.message)
        self.assertNotIn(secret, repr(result))

    def test_human_output_and_invalid_json_are_rejected(self):
        runner = FakeRunner(stdout="Multica connected successfully")

        result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_json")
        self.assertIsNone(result.payload)

    def test_schema_and_version_mismatches_are_rejected(self):
        cases = (
            ("schema_mismatch", {"schema": "multica.agx-connector/v2"}),
            ("version_mismatch", {"cli_version": "0.4.25"}),
        )
        for code, override in cases:
            with self.subTest(code=code):
                runner = FakeRunner(stdout=json.dumps(success_payload(**override)))
                result = AgxMulticaConnector(self.config, runner=runner).execute(
                    self.task
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.code, code)
                self.assertIsNone(result.payload)

    def test_non_completed_status_is_a_failed_typed_result(self):
        for status in ("running", "failed", "stale"):
            with self.subTest(status=status):
                runner = FakeRunner(stdout=json.dumps(success_payload(status=status)))
                result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

                self.assertFalse(result.ok)
                self.assertEqual(result.code, "invalid_status")
                self.assertIsNone(result.payload)

    def test_non_healthy_health_is_a_failed_typed_result(self):
        for health in ("degraded", "failed", "stale"):
            with self.subTest(health=health):
                runner = FakeRunner(stdout=json.dumps(success_payload(health=health)))
                result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

                self.assertFalse(result.ok)
                self.assertEqual(result.code, "invalid_health")
                self.assertIsNone(result.payload)

    def test_missing_key_fields_are_rejected(self):
        payload = success_payload()
        del payload["deployment_id"]
        runner = FakeRunner(stdout=json.dumps(payload))

        result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "missing_field")
        self.assertIn("deployment_id", result.message)

    def test_sensitive_fields_and_token_values_are_redacted(self):
        secret = "super-secret-token"
        payload = success_payload(
            summary={
                "message": f"Bearer {secret}",
                "api_token": secret,
                "nested": {"password": "hidden-password"},
            }
        )
        runner = FakeRunner(stdout=json.dumps(payload))

        result = AgxMulticaConnector(self.config, runner=runner).execute(self.task)
        encoded = json.dumps(result.payload, sort_keys=True)

        self.assertTrue(result.ok)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("hidden-password", encoded)
        self.assertEqual(result.payload["summary"]["api_token"], "[REDACTED]")
        self.assertEqual(result.payload["summary"]["nested"]["password"], "[REDACTED]")

    def test_unknown_task_fields_are_rejected_without_echoing_values(self):
        secret = "node-token-should-not-appear"
        task = {
            "task_id": "task-123",
            "repository": "2233admin/project-a",
            "ref": "main",
            "environment": "staging",
            "action": "deploy",
            "target_selector": "node-01",
            "token": secret,
        }
        runner = FakeRunner(stdout=json.dumps(success_payload()))

        result = AgxMulticaConnector(self.config, runner=runner).execute(task)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_task")
        self.assertNotIn(secret, result.message)


if __name__ == "__main__":
    unittest.main()
