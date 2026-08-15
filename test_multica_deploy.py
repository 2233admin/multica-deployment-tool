import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import multica_deploy


class DeploymentToolTests(unittest.TestCase):
    def test_parser_exposes_maintenance_commands(self):
        parser = multica_deploy.build_parser()
        for command in ("upgrade", "build", "doctor", "rollback"):
            args = parser.parse_args(
                [command, "--nas-host", "nas", "--nas-ip", "203.0.113.20"]
            )
            self.assertEqual(args.command, command)

    def test_dev_image_tag_is_valid_for_source_builds(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "build",
                "--nas-host",
                "nas",
                "--nas-ip",
                "203.0.113.20",
                "--source-dir",
                "..",
                "--image-tag",
                "dev-20260816",
            ]
        )
        multica_deploy.validate_config(args)

    def test_platform_aliases_normalize_for_cross_arch_builds(self):
        self.assertEqual(multica_deploy.normalize_platform("x86_64"), "linux/amd64")
        self.assertEqual(multica_deploy.normalize_platform("aarch64"), "linux/arm64")
        self.assertEqual(multica_deploy.normalize_platform("armv7l"), "linux/arm/v7")
        with self.assertRaises(multica_deploy.ConfigError):
            multica_deploy.normalize_platform("linux/s390x")

    def test_source_checkout_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "Dockerfile",
                "Dockerfile.web",
                "docker-compose.selfhost.yml",
                "docker-compose.selfhost.build.yml",
            ):
                (root / name).write_text("placeholder\n", encoding="utf-8")
            multica_deploy.validate_source_checkout(root)

    def test_compose_has_no_weak_secret_fallbacks(self):
        compose = (multica_deploy.PACKAGE_ROOT / "docker-compose.selfhost.yml").read_text(
            encoding="utf-8"
        )
        weak_password = "${" + "POSTGRES_PASSWORD" + ":-multica}"
        weak_jwt = "${" + "JWT_SECRET" + ":-change-me-in-production}"
        self.assertNotIn(weak_password, compose)
        self.assertNotIn(weak_jwt, compose)
        self.assertIn("${POSTGRES_PASSWORD:?", compose)
        self.assertIn("${JWT_SECRET:?", compose)

    def test_synology_defaults_are_detected(self):
        args = argparse.Namespace(
            docker_path="docker",
            nas_target=multica_deploy.DEFAULTS["nas_target"],
            nas_host="nas",
            ssh_port=0,
        )
        with patch.object(
            multica_deploy,
            "probe_remote",
            return_value={
                "synology_docker": "/var/packages/ContainerManager/target/usr/bin/docker",
                "multica_target": "/volume1/docker/multica",
            },
        ):
            multica_deploy.auto_detect_remote_docker(args)
        self.assertEqual(
            args.docker_path,
            "/var/packages/ContainerManager/target/usr/bin/docker",
        )
        self.assertEqual(args.nas_target, "/volume1/docker/multica")

    def test_previous_release_state_is_secret_free(self):
        args = argparse.Namespace(nas_target="/opt/multica")
        with patch.object(
            multica_deploy,
            "remote_capture",
            return_value=(
                "MULTICA_IMAGE_TAG=v0.4.26\n"
                "MULTICA_BACKEND_IMAGE=\n"
                "MULTICA_WEB_IMAGE=\n"
                "JWT_SECRET=must-not-be-read\n"
            ),
        ):
            state = multica_deploy.read_release_state(args)
        self.assertEqual(state["MULTICA_IMAGE_TAG"], "v0.4.26")
        self.assertNotIn("JWT_SECRET", state)

    def test_saved_config_excludes_application_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deploy.json"
            args = argparse.Namespace(
                config_file=str(path),
                nas_host="nas",
                ssh_port=22,
                nas_ip="203.0.113.20",
                nas_target="/opt/multica",
                source_dir="../multica",
                docker_path="docker",
                no_sudo=False,
                owner="multica",
                group="multica",
                backend_image="",
                web_image="",
                JWT_SECRET="must-not-persist",
                GITEA_CLIENT_SECRET="must-not-persist",
            )
            multica_deploy.save_config(args)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("JWT_SECRET", payload)
            self.assertNotIn("GITEA_CLIENT_SECRET", payload)
            self.assertEqual(payload["nas_host"], "nas")


if __name__ == "__main__":
    unittest.main()
