import argparse
import inspect
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import multica_deploy


class DeploymentToolTests(unittest.TestCase):
    def test_desktop_sync_defaults_and_opt_out(self):
        parser = multica_deploy.build_parser()
        base = ["upgrade", "--nas-host", "nas", "--nas-ip", "203.0.113.20"]
        args = parser.parse_args(base)
        self.assertTrue(args.desktop_sync)
        self.assertEqual(args.image_tag, "latest")
        self.assertEqual(args.desktop_version, "")
        self.assertFalse(parser.parse_args(base + ["--no-desktop-sync"]).desktop_sync)

    def test_desktop_version_follows_runtime_before_image_tag(self):
        args = argparse.Namespace(desktop_version="", image_tag="v0.4.28")
        self.assertEqual(
            multica_deploy.resolve_desktop_version(args, "v0.4.30"), "v0.4.30"
        )
        self.assertEqual(multica_deploy.resolve_desktop_version(args), "v0.4.28")
        args.image_tag = "dev-20260818"
        self.assertEqual(multica_deploy.resolve_desktop_version(args), "latest")

    def test_runtime_version_requires_matching_backend_and_frontend_labels(self):
        args = argparse.Namespace(
            nas_target="/volume1/docker/multica",
            docker_path="docker",
            no_sudo=False,
        )
        with patch.object(multica_deploy, "compose", return_value="compose"), patch.object(
            multica_deploy, "privileged", return_value="docker"
        ), patch.object(
            multica_deploy,
            "remote_capture",
            return_value="backend=v0.4.30\nfrontend=v0.4.30\n",
        ):
            self.assertEqual(multica_deploy.detect_runtime_version(args), "v0.4.30")

    def test_runtime_version_rejects_mismatched_labels(self):
        args = argparse.Namespace(
            nas_target="/volume1/docker/multica",
            docker_path="docker",
            no_sudo=False,
        )
        with patch.object(multica_deploy, "compose", return_value="compose"), patch.object(
            multica_deploy, "privileged", return_value="docker"
        ), patch.object(
            multica_deploy,
            "remote_capture",
            return_value="backend=v0.4.30\nfrontend=v0.4.29\n",
        ):
            self.assertEqual(multica_deploy.detect_runtime_version(args), "")

    def test_desktop_cli_capability_probe_accepts_daemon_help(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "multica.exe"
            cli.write_bytes(b"placeholder")
            completed = subprocess.CompletedProcess(
                [str(cli)], 0, stdout="multica daemon <command>", stderr=""
            )
            with patch.object(multica_deploy.subprocess, "run", return_value=completed):
                self.assertEqual(
                    multica_deploy._desktop_cli_capability(cli), ""
                )

    def test_desktop_cli_capability_probe_rejects_missing_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "multica.exe"
            cli.write_bytes(b"placeholder")
            completed = subprocess.CompletedProcess(
                [str(cli)], 2, stdout="unknown command", stderr=""
            )
            with patch.object(multica_deploy.subprocess, "run", return_value=completed):
                self.assertTrue(
                    multica_deploy._desktop_cli_capability(cli)
                )

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

    def test_parser_exposes_fast_hot_update(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "build",
                "--nas-host",
                "nas",
                "--nas-ip",
                "192.0.2.10",
                "--source-dir",
                "..",
                "--hot-update",
            ]
        )
        self.assertTrue(args.hot_update)

    def test_hot_update_replaces_only_application_services(self):
        args = argparse.Namespace()
        addresses = multica_deploy.TargetAddresses(
            bind_address="192.0.2.10",
            browser_origin="http://192.0.2.10:4310",
            service_origin="http://192.0.2.10:4310",
            oauth_origin="http://192.0.2.10:4310",
        )
        with patch.object(multica_deploy, "remote") as remote:
            multica_deploy.hot_update_services(args, "compose", addresses)
        commands = [call.args[1] for call in remote.call_args_list]
        self.assertIn("compose up -d --no-deps --force-recreate backend", commands[0])
        self.assertIn("/readyz", commands[1])
        self.assertIn("compose up -d --no-deps --force-recreate frontend", commands[2])
        self.assertIn("--status running --services frontend", commands[3])
        self.assertTrue(all("postgres" not in command and "caddy" not in command for command in commands))

    def test_parser_exposes_optional_github_device_flow_settings(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "deploy",
                "--nas-host",
                "nas",
                "--nas-ip",
                "192.0.2.10",
                "--app-port",
                "4310",
                "--github-device-flow",
                "--github-device-flow-client-id",
                "Iv1.test_client_id",
            ]
        )
        self.assertTrue(args.github_device_flow_enabled)
        self.assertEqual(args.github_device_flow_client_id, "Iv1.test_client_id")
        device_args = parser.parse_args(["github-device-flow"])
        self.assertEqual(device_args.command, "github-device-flow")

    def test_device_flow_requires_client_id_only_when_enabled(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "deploy",
                "--nas-host",
                "nas",
                "--nas-ip",
                "192.0.2.10",
                "--app-port",
                "4310",
                "--github-device-flow",
            ]
        )
        with self.assertRaisesRegex(multica_deploy.ConfigError, "client ID"):
            multica_deploy.validate_config(args)
        args.github_device_flow_enabled = False
        multica_deploy.validate_config(args)

    def test_dev_image_tag_is_valid_for_source_builds(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "build",
                "--nas-host",
                "nas",
                "--nas-ip",
                "203.0.113.20",
                "--app-port",
                "4310",
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
                "--app-port",
                "4310",
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

    def test_watchdog_assets_and_config_are_ready_for_synology(self):
        for name in multica_deploy.WATCHDOG_FILES:
            self.assertTrue((multica_deploy.PACKAGE_ROOT / name).is_file())
        args = argparse.Namespace(
            nas_target="/volume1/docker/multica",
            nas_ip="100.80.110.105",
            app_port=3010,
            docker_path="/var/packages/ContainerManager/target/usr/bin/docker",
        )
        rendered = multica_deploy.render_watchdog_config(args)
        try:
            config = rendered.read_text(encoding="utf-8")
        finally:
            rendered.unlink(missing_ok=True)
        self.assertIn("MULTICA_TARGET='/volume1/docker/multica'", config)
        self.assertIn("MULTICA_NAS_IP='100.80.110.105'", config)
        self.assertIn("MULTICA_DOCKER_PATH='/var/packages/ContainerManager/target/usr/bin/docker'", config)
        self.assertNotIn("__NAS_", config)

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

    def test_self_hosted_caddy_uses_local_entry_and_backend_api(self):
        caddyfile = (multica_deploy.PACKAGE_ROOT / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("@local_root path /", caddyfile)
        self.assertIn("redir @local_root /login 302", caddyfile)
        self.assertIn("@multica_api path /api/*", caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:3011", caddyfile)

    def test_github_app_credentials_are_passed_to_backend(self):
        compose = (multica_deploy.PACKAGE_ROOT / "docker-compose.selfhost.yml").read_text(
            encoding="utf-8"
        )
        template = (multica_deploy.PACKAGE_ROOT / ".env.template").read_text(
            encoding="utf-8"
        )
        for variable in (
            "GITHUB_APP_SLUG",
            "GITHUB_WEBHOOK_SECRET",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
        ):
            self.assertIn(f"{variable}:", compose)
            self.assertIn(f"{variable}=", template)

    def test_public_url_is_validated(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "deploy",
                "--nas-host",
                "nas",
                "--nas-ip",
                "203.0.113.20",
                "--app-port",
                "4310",
                "--public-url",
                "https://multica.example.com",
            ]
        )
        multica_deploy.validate_config(args)
        self.assertEqual(args.public_url, "https://multica.example.com")
        args.public_url = "https://multica.example.com/path"
        with self.assertRaises(multica_deploy.ConfigError):
            multica_deploy.validate_config(args)
        args.public_url = "http://multica.example.com"
        with self.assertRaises(multica_deploy.ConfigError):
            multica_deploy.validate_config(args)

    def test_initialize_env_renders_callback_defaults(self):
        args = argparse.Namespace(
            nas_target="/opt/multica",
            image_tag="v0.4.28",
            backend_image="",
            web_image="",
            backend_port=3011,
            frontend_port=3012,
            app_port=3010,
            nas_ip="192.0.2.10",
            public_url="https://multica.example.com",
            owner="multica",
            group="multica",
            no_sudo=True,
        )
        with patch.object(multica_deploy, "remote") as remote:
            multica_deploy.initialize_remote_env(args)
        script = remote.call_args.args[1]
        self.assertIn("GITEA_REDIRECT_URI http://192.0.2.10:3010/auth/callback", script)
        self.assertIn("upsert MULTICA_PUBLIC_URL https://multica.example.com", script)

    def test_initialize_env_renders_device_flow_without_public_url(self):
        args = argparse.Namespace(
            nas_target="/opt/multica",
            image_tag="v0.4.28",
            backend_image="",
            web_image="",
            backend_port=3011,
            frontend_port=3012,
            app_port=3010,
            nas_ip="192.0.2.10",
            public_url="",
            browser_url="",
            service_url="",
            oauth_origin="",
            plane_url="",
            github_device_flow_enabled=True,
            github_device_flow_client_id="Iv1.test_client_id",
            owner="multica",
            group="multica",
            no_sudo=True,
        )
        with patch.object(multica_deploy, "remote") as remote:
            multica_deploy.initialize_remote_env(args)
        script = remote.call_args.args[1]
        self.assertIn("upsert GITHUB_DEVICE_FLOW_ENABLED true", script)
        self.assertIn("upsert GITHUB_APP_CLIENT_ID Iv1.test_client_id", script)
        self.assertNotIn("MULTICA_PUBLIC_URL https", script)

    def test_target_addresses_keep_roles_separate(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "deploy",
                "--nas-host",
                "ssh-target",
                "--nas-ip",
                "192.0.2.10",
                "--app-port",
                "4310",
                "--browser-url",
                "https://multica.example.test",
                "--service-url",
                "http://192.0.2.10:4310",
                "--oauth-origin",
                "https://login.example.test/multica",
                "--plane-url",
                "http://plane.example.test:9090",
            ]
        )
        with self.assertRaises(multica_deploy.ConfigError):
            multica_deploy.validate_config(args)
        args.oauth_origin = "https://login.example.test"
        multica_deploy.validate_config(args)
        addresses = multica_deploy.resolve_target_addresses(args)
        self.assertEqual(addresses.bind_address, "192.0.2.10")
        self.assertEqual(addresses.browser_origin, "https://multica.example.test")
        self.assertEqual(addresses.service_origin, "http://192.0.2.10:4310")
        self.assertEqual(addresses.oauth_callback_url, "https://login.example.test/auth/callback")
        self.assertEqual(addresses.plane_url, "http://plane.example.test:9090")

    def test_caddy_renders_browser_host_and_target_bind_separately(self):
        args = argparse.Namespace(
            nas_ip="192.0.2.10",
            browser_url="http://multica.example.test:4310",
            service_url="http://192.0.2.10:4310",
            oauth_origin="",
            plane_url="",
            app_port=4310,
        )
        rendered = multica_deploy.render_caddy(args)
        try:
            content = rendered.read_text(encoding="utf-8")
        finally:
            rendered.unlink(missing_ok=True)
        self.assertIn("http://multica.example.test:4310 {", content)
        self.assertIn("bind 192.0.2.10", content)
        self.assertNotIn("__BROWSER_", content)
        self.assertNotIn("__BIND_", content)

    def test_live_origin_must_match_contract_origin(self):
        contract = {"multica": {"server_url": "http://192.0.2.10:4310"}}
        matching = argparse.Namespace(
            nas_ip="192.0.2.10",
            service_url="",
            browser_url="",
            oauth_origin="",
            plane_url="",
            app_port=4310,
        )
        multica_deploy._assert_live_origin_matches_contract(contract, matching)

        mismatched = argparse.Namespace(
            **{**vars(matching), "service_url": "http://192.0.2.11:4310"}
        )
        with self.assertRaisesRegex(multica_deploy.ConfigError, "不一致"):
            multica_deploy._assert_live_origin_matches_contract(contract, mismatched)

    def test_saved_config_serializes_address_roles_ports_and_plane(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deploy.json"
            args = argparse.Namespace(
                config_file=str(path),
                nas_host="ssh-target",
                ssh_port=2222,
                nas_ip="192.0.2.10",
                browser_url="https://multica.example.test",
                service_url="http://192.0.2.10:4310",
                oauth_origin="https://multica.example.test",
                plane_url="http://plane.example.test:9090",
                app_port=4310,
                backend_port=4311,
                frontend_port=4312,
                network_subnet="10.254.0.0/24",
                nas_target="/opt/multica",
                source_dir="",
                docker_path="docker",
                no_sudo=False,
                netbird=False,
                owner="multica",
                group="multica",
                backend_image="",
                web_image="",
                public_url="",
            )
            multica_deploy.save_config(args)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["browser_url"], "https://multica.example.test")
            self.assertEqual(payload["service_url"], "http://192.0.2.10:4310")
            self.assertEqual(payload["app_port"], 4310)
            self.assertEqual(payload["plane_url"], "http://plane.example.test:9090")
            self.assertNotIn("GITEA_CLIENT_SECRET", payload)

    def test_saved_config_serializes_device_flow_metadata_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deploy.json"
            args = argparse.Namespace(
                config_file=str(path),
                nas_host="nas",
                ssh_port=22,
                nas_ip="192.0.2.10",
                nas_target="/opt/multica",
                docker_path="docker",
                no_sudo=False,
                owner="multica",
                group="multica",
                github_device_flow_enabled=True,
                github_device_flow_client_id="Iv1.test_client_id",
                JWT_SECRET="must-not-persist",
                GITHUB_WEBHOOK_SECRET="must-not-persist",
            )
            multica_deploy.save_config(args)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["github_device_flow_enabled"])
            self.assertEqual(payload["github_device_flow_client_id"], "Iv1.test_client_id")
            self.assertNotIn("JWT_SECRET", payload)
            self.assertNotIn("GITHUB_WEBHOOK_SECRET", payload)

    def test_device_flow_configuration_updates_only_non_secret_values(self):
        args = argparse.Namespace(
            github_device_flow_enabled=False,
            github_device_flow_client_id="",
        )
        with patch.object(multica_deploy, "prompt_default", return_value="y"), \
            patch.object(
                multica_deploy, "prompt_required", return_value="Iv1.test_client_id"
            ), \
            patch.object(multica_deploy, "update_backend_env") as update, \
            patch.object(multica_deploy, "save_config") as save:
            multica_deploy.configure_github_device_flow(args)
        self.assertEqual(
            update.call_args.args[1],
            {
                "GITHUB_DEVICE_FLOW_ENABLED": "true",
                "GITHUB_APP_CLIENT_ID": "Iv1.test_client_id",
            },
        )
        save.assert_called_once_with(args)

    def test_doctor_reports_device_flow_and_webhook_states_without_values(self):
        parser = multica_deploy.build_parser()
        args = parser.parse_args(
            [
                "doctor",
                "--nas-host",
                "nas",
                "--nas-ip",
                "192.0.2.10",
                "--app-port",
                "4310",
            ]
        )
        remote_output = (
            "github_device_flow=enabled\n"
            "github_device_flow_app_client_id=configured\n"
            "github_device_flow_token_encryption_key=configured\n"
            "github_app=disabled\n"
            "github_webhook=not-configured (LAN/NetBird is okay)\n"
        )
        captured = io.StringIO()
        with patch.object(multica_deploy, "check_binary"), \
            patch.object(multica_deploy, "check_package"), \
            patch.object(multica_deploy, "auto_detect_remote_docker"), \
            patch.object(multica_deploy, "verify_netbird_endpoint"), \
            patch.object(multica_deploy, "remote_capture", return_value=remote_output) as remote, \
            patch.object(
                multica_deploy,
                "open_service_url",
                side_effect=multica_deploy.urllib.error.URLError("offline"),
            ), \
            redirect_stdout(captured):
            multica_deploy.doctor(args)
        output = captured.getvalue()
        self.assertIn("github_device_flow=enabled", output)
        self.assertIn("github_device_flow_app_client_id=configured", output)
        self.assertIn("github_device_flow_token_encryption_key=configured", output)
        self.assertIn("github_webhook=not-configured (LAN/NetBird is okay)", output)
        self.assertNotIn("test-secret", output)
        command = remote.call_args.args[1]
        self.assertIn("github_device_flow_app_client_id=configured", command)
        self.assertIn("github_device_flow_token_encryption_key=configured", command)
        self.assertNotIn("test-secret", command)

    def test_product_positioning_and_runtime_templates_have_no_reference_addresses(self):
        readme = (multica_deploy.PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        install = (multica_deploy.PACKAGE_ROOT / "install.py").read_text(encoding="utf-8")
        caddy = (multica_deploy.PACKAGE_ROOT / "Caddyfile").read_text(encoding="utf-8")
        env_template = (multica_deploy.PACKAGE_ROOT / ".env.template").read_text(encoding="utf-8")
        self.assertIn("Multica 本地版一键部署包", readme)
        self.assertIn("Multica 本地版一键部署包", install)
        contents = (
            Path(multica_deploy.__file__).read_text(encoding="utf-8"),
            caddy,
            env_template,
        )
        for content in contents:
            self.assertNotIn("192.168.50.130", content)
            self.assertNotIn("100.80.110.105", content)
        self.assertNotIn("192.168.50.130", readme)
        self.assertNotIn("100.80.110.105", readme)

    def test_github_private_key_is_written_as_multiline_env_value(self):
        source = inspect.getsource(multica_deploy.update_backend_env)
        self.assertIn("GITHUB_APP_PRIVATE_KEY='", source)
        self.assertIn("tr -d", source)
        self.assertIn("^GITHUB_TOKEN_ENCRYPTION_KEY=.", source)

    def test_templates_include_device_flow_and_keep_app_webhook_optional(self):
        compose = (multica_deploy.PACKAGE_ROOT / "docker-compose.selfhost.yml").read_text(
            encoding="utf-8"
        )
        template = (multica_deploy.PACKAGE_ROOT / ".env.template").read_text(
            encoding="utf-8"
        )
        for variable in (
            "GITHUB_DEVICE_FLOW_ENABLED",
            "GITHUB_APP_CLIENT_ID",
            "GITHUB_TOKEN_ENCRYPTION_KEY",
        ):
            self.assertIn(variable, compose)
            self.assertIn(f"{variable}=", template)
        self.assertIn("GITHUB_DEVICE_FLOW_ENABLED=false", template)
        self.assertNotIn("GITHUB_APP_CLIENT_ID=Iv1.", template)
        self.assertIn("LAN/NetBird", template)

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
                "MULTICA_IMAGE_TAG=v0.4.28\n"
                "MULTICA_BACKEND_IMAGE=\n"
                "MULTICA_WEB_IMAGE=\n"
                "JWT_SECRET=must-not-be-read\n"
            ),
        ):
            state = multica_deploy.read_release_state(args)
        self.assertEqual(state["MULTICA_IMAGE_TAG"], "v0.4.28")
        self.assertNotIn("JWT_SECRET", state)

    def test_default_release_mode_follows_running_runtime(self):
        self.assertEqual(multica_deploy.DEFAULTS["image_tag"], "latest")
        self.assertIn(
            "MULTICA_IMAGE_TAG=latest",
            (multica_deploy.PACKAGE_ROOT / ".env.template").read_text(encoding="utf-8"),
        )

    def test_live_verify_markers_decode_only_their_json_section(self):
        output = "\n".join(
            [
                "noise from the shell",
                "MULTICA_VERIFY_AUTH_BEGIN",
                '{"authenticated":true}',
                "MULTICA_VERIFY_AUTH_END",
                "MULTICA_VERIFY_RUNTIME_BEGIN",
                '{"status":"running"}',
                "MULTICA_VERIFY_RUNTIME_END",
            ]
        )
        self.assertEqual(
            multica_deploy._json_between_markers(
                output, "MULTICA_VERIFY_RUNTIME_BEGIN", "MULTICA_VERIFY_RUNTIME_END"
            ),
            {"status": "running"},
        )
        with self.assertRaises(RuntimeError):
            multica_deploy._json_between_markers(
                output, "MULTICA_VERIFY_WORKSPACE_BEGIN", "MULTICA_VERIFY_WORKSPACE_END"
            )

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
