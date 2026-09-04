import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import multica_deploy


class DeploymentToolTests(unittest.TestCase):
    def test_desktop_sync_defaults_and_opt_out(self):
        parser = multica_deploy.build_parser()
        base = ["upgrade", "--nas-host", "nas", "--nas-ip", "203.0.113.20"]
        self.assertTrue(parser.parse_args(base).desktop_sync)
        self.assertFalse(parser.parse_args(base + ["--no-desktop-sync"]).desktop_sync)

    def test_desktop_version_follows_formal_image_tag(self):
        args = argparse.Namespace(desktop_version="", image_tag="v0.4.28")
        self.assertEqual(multica_deploy.resolve_desktop_version(args), "v0.4.28")
        args.image_tag = "dev-20260818"
        self.assertEqual(multica_deploy.resolve_desktop_version(args), "latest")

    def test_desktop_profile_preserves_credentials_and_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile" / "config.json"
            profile.parent.mkdir(parents=True)
            profile.write_text(
                json.dumps(
                    {
                        "server_url": "https://desktop-api.multica.ai",
                        "app_url": "https://desktop-api.multica.ai",
                        "token": "local-token",
                        "workspace_id": "workspace-1",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                multica_deploy,
                "desktop_paths",
                return_value=(root / "Multica.exe", root / "multica.exe", profile),
            ), patch.object(multica_deploy.Path, "home", return_value=root):
                result = multica_deploy.preserve_desktop_profile(
                    "http://203.0.113.20:3010", "desktop-api.multica.ai"
                )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["server_url"], "http://203.0.113.20:3010")
            self.assertEqual(payload["app_url"], "http://203.0.113.20:3010")
            self.assertEqual(payload["token"], "local-token")
            self.assertEqual(payload["workspace_id"], "workspace-1")

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

    def test_netbird_mode_is_available_and_validated(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "deploy",
                "--nas-host",
                "nas",
                "--nas-ip",
                "100.80.110.105",
                "--netbird",
            ]
        )
        multica_deploy.validate_config(args)
        self.assertTrue(args.netbird)

    def test_netbird_verification_checks_connection_and_ip(self):
        args = argparse.Namespace(
            netbird=True,
            nas_ip="100.80.110.105",
            app_port=3010,
            docker_path="docker",
            no_sudo=False,
        )
        with patch.object(multica_deploy, "remote") as remote:
            multica_deploy.verify_netbird_endpoint(args)
        command = remote.call_args.args[1]
        self.assertIn("Management: Connected", command)
        self.assertIn("Signal: Connected", command)
        self.assertIn("NetBird IP: 100.80.110.105/", command)

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

    def test_compose_has_inert_default_for_gitea_backend_host_override(self):
        compose = (multica_deploy.PACKAGE_ROOT / "docker-compose.selfhost.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '${GITEA_BACKEND_HOST_OVERRIDE:-unused.invalid:127.0.0.1}', compose
        )

    def test_env_template_documents_gitea_backend_host_override(self):
        template = (multica_deploy.PACKAGE_ROOT / ".env.template").read_text(
            encoding="utf-8"
        )
        self.assertIn("GITEA_BACKEND_HOST_OVERRIDE=", template)

    def test_gitea_backend_host_override_accepts_host_ip_pairs(self):
        multica_deploy.validate_gitea_backend_host_override("git.example.com:192.168.1.10")

    def test_gitea_backend_host_override_rejects_malformed_values(self):
        for bad in ("git.example.com", "git.example.com:", ":192.168.1.10", "git.example.com:not-an-ip"):
            with self.assertRaises(multica_deploy.ConfigError):
                multica_deploy.validate_gitea_backend_host_override(bad)

    def test_gitea_backchannel_not_configured_when_gitea_unset(self):
        args = argparse.Namespace(nas_target="/volume1/docker/multica")
        with patch.object(multica_deploy, "remote_capture", return_value="") as capture:
            status, detail = multica_deploy.gitea_backchannel_status(args)
        self.assertEqual((status, detail), ("not_configured", ""))
        capture.assert_called_once()

    def test_gitea_backchannel_reachable_uses_resolve_override(self):
        args = argparse.Namespace(
            nas_target="/volume1/docker/multica", docker_path="docker", no_sudo=False
        )
        env_output = (
            "GITEA_ISSUER_URL=http://git.xart.top:8418\n"
            "GITEA_CLIENT_ID=abc123\n"
            "GITEA_BACKEND_HOST_OVERRIDE=git.xart.top:10.253.0.1\n"
        )
        with patch.object(
            multica_deploy, "remote_capture", side_effect=[env_output, "200"]
        ) as capture:
            status, detail = multica_deploy.gitea_backchannel_status(args)
        self.assertEqual((status, detail), ("reachable", "200"))
        probe_command = capture.call_args_list[1].args[1]
        self.assertIn(f"--network {multica_deploy.COMPOSE_NETWORK_NAME}", probe_command)
        self.assertIn(multica_deploy.GITEA_BACKCHANNEL_PROBE_IMAGE, probe_command)
        self.assertIn("--resolve", probe_command)
        self.assertIn("git.xart.top:8418:10.253.0.1", probe_command)

    def test_gitea_backchannel_skips_resolve_without_override(self):
        args = argparse.Namespace(
            nas_target="/volume1/docker/multica", docker_path="docker", no_sudo=False
        )
        env_output = "GITEA_ISSUER_URL=https://gitea.internal\nGITEA_CLIENT_ID=abc123\n"
        with patch.object(
            multica_deploy, "remote_capture", side_effect=[env_output, "200"]
        ) as capture:
            status, detail = multica_deploy.gitea_backchannel_status(args)
        self.assertEqual(status, "reachable")
        probe_command = capture.call_args_list[1].args[1]
        self.assertNotIn("--resolve", probe_command)
        self.assertIn("https://gitea.internal:443/", probe_command)

    def test_gitea_backchannel_unreachable_when_probe_fails(self):
        args = argparse.Namespace(
            nas_target="/volume1/docker/multica", docker_path="docker", no_sudo=False
        )
        env_output = "GITEA_ISSUER_URL=http://git.xart.top:8418\nGITEA_CLIENT_ID=abc123\n"
        with patch.object(
            multica_deploy,
            "remote_capture",
            side_effect=[env_output, RuntimeError("NAS 命令失败（退出码 28）。")],
        ):
            status, detail = multica_deploy.gitea_backchannel_status(args)
        self.assertEqual(status, "unreachable")
        self.assertIn("28", detail)

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

    def test_deploy_options_expose_yes_flag(self):
        parser = multica_deploy.build_parser()
        for command in ("deploy", "upgrade", "build"):
            args = parser.parse_args(
                [command, "--nas-host", "nas", "--nas-ip", "203.0.113.20"]
                + (["--source-dir", ".."] if command == "build" else [])
            )
            self.assertFalse(args.yes)

    def test_caddyfile_drift_blocks_overwrite_without_yes(self):
        args = argparse.Namespace(nas_target="/volume1/docker/multica", yes=False)
        hand_edited = "http://100.80.110.105:3010 {\n  bind 100.80.110.105 192.168.50.130\n}\n"
        rendered = "http://100.80.110.105:3010 {\n  bind 100.80.110.105\n}\n"
        with patch.object(multica_deploy, "remote_capture", return_value=hand_edited):
            with self.assertRaises(RuntimeError) as ctx:
                multica_deploy.check_caddyfile_drift(args, rendered)
        self.assertIn("192.168.50.130", str(ctx.exception))
        self.assertIn("--yes", str(ctx.exception))

    def test_caddyfile_drift_check_skipped_with_yes(self):
        args = argparse.Namespace(nas_target="/volume1/docker/multica", yes=True)
        with patch.object(multica_deploy, "remote_capture") as capture:
            multica_deploy.check_caddyfile_drift(args, "anything")
        capture.assert_not_called()

    def test_caddyfile_drift_ignored_when_target_has_no_existing_file(self):
        args = argparse.Namespace(nas_target="/volume1/docker/multica", yes=False)
        with patch.object(multica_deploy, "remote_capture", return_value=""):
            multica_deploy.check_caddyfile_drift(args, "http://203.0.113.20:3010 {\n}\n")

    def test_caddyfile_drift_ignored_when_content_matches(self):
        args = argparse.Namespace(nas_target="/volume1/docker/multica", yes=False)
        rendered = "http://203.0.113.20:3010 {\n  bind 203.0.113.20\n}\n"
        with patch.object(multica_deploy, "remote_capture", return_value=rendered):
            multica_deploy.check_caddyfile_drift(args, rendered)


if __name__ == "__main__":
    unittest.main()
