"""Focused security and delivery tests for per-producer Discord routes."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MIGRATION_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = MIGRATION_DIR.parent
SCRIPT_DIR = MIGRATION_DIR / "config" / "triggers" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import discord_routes  # noqa: E402


VALID_URL_A = "https://discord.com/api/webhooks/123/token-A"
VALID_URL_B = "https://discord.com/api/v10/webhooks/456/token_B.part"


class SecureRouteFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.secret_dir = self.root / "secrets"
        self.secret_dir.mkdir(mode=0o700)
        os.chmod(self.secret_dir, 0o700)
        self.route_file = self.secret_dir / "discord_webhooks.json"

    def write_routes(self, routes, *, raw=False):
        content = routes if raw else json.dumps(routes)
        self.route_file.write_text(content, encoding="utf-8")
        os.chmod(self.route_file, 0o600)
        return self.route_file

    def atomic_routes(self, routes):
        replacement = self.secret_dir / "discord_webhooks.new"
        replacement.write_text(json.dumps(routes), encoding="utf-8")
        os.chmod(replacement, 0o600)
        os.replace(replacement, self.route_file)


class RegistryAndIgnoreTests(unittest.TestCase):
    def test_example_has_exact_15_key_registry_with_empty_values(self):
        example_path = MIGRATION_DIR / "config" / "discord_webhooks.example.json"
        example = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(tuple(example), discord_routes.ROUTE_NAMES)
        self.assertEqual(len(example), 15)
        self.assertTrue(all(value == "" for value in example.values()))
        self.assertEqual(
            discord_routes.OPTIONAL_ROUTE_NAMES,
            {"Smoke Test USDC Transfer"},
        )

    def test_secret_directory_and_env_backups_are_ignored(self):
        for relative in (
            "migration/secrets/discord_webhooks.json",
            "migration/.env.bak",
            "migration/.env.production",
            "migration/.env~",
        ):
            with self.subTest(relative=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "-q", "--no-index", relative],
                    cwd=REPO_DIR,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
        example = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "migration/.env.example"],
            cwd=REPO_DIR,
            check=False,
        )
        self.assertEqual(example.returncode, 1)

    def test_compose_mounts_route_directory_read_only_and_recovers_until_stopped(self):
        compose = (MIGRATION_DIR / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("./secrets:/app/secrets:ro", compose)
        self.assertIn("DISCORD_WEBHOOKS_FILE: /app/secrets/discord_webhooks.json", compose)
        self.assertIn("TELLOR_ALERT_DELIVERY_MODE:", compose)
        monitor_section = compose.split("  prometheus:", 1)[0]
        self.assertIn("restart: unless-stopped", monitor_section)
        self.assertNotIn("restart: on-failure:5", monitor_section)


class SecureLoadingTests(SecureRouteFixture):
    def test_repeated_urls_are_allowed(self):
        routes = {
            discord_routes.ROUTE_NAMES[0]: VALID_URL_A,
            discord_routes.ROUTE_NAMES[1]: VALID_URL_A,
        }
        self.write_routes(routes)
        self.assertEqual(discord_routes.load_discord_routes(self.route_file), routes)

    def test_duplicate_names_are_rejected_without_secret_echo(self):
        name = discord_routes.ROUTE_NAMES[0]
        self.write_routes(
            '{{"{0}":"{1}","{0}":"{2}"}}'.format(
                name, VALID_URL_A, VALID_URL_B
            ),
            raw=True,
        )
        with self.assertRaises(discord_routes.RouteConfigurationError) as raised:
            discord_routes.load_discord_routes(self.route_file)
        self.assertIn("duplicate", str(raised.exception))
        self.assertNotIn("token", str(raised.exception))

    def test_invalid_shapes_and_urls_are_rejected(self):
        cases = (
            ("[]", "object"),
            ("{not-json", "JSON"),
            (json.dumps({discord_routes.ROUTE_NAMES[0]: "http://example.invalid/hook"}), "https"),
            (json.dumps({discord_routes.ROUTE_NAMES[0]: 42}), "webhook URL"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.write_routes(raw, raw=True)
                with self.assertRaisesRegex(
                    discord_routes.RouteConfigurationError, expected
                ):
                    discord_routes.load_discord_routes(self.route_file)

    def test_file_and_directory_modes_are_exact(self):
        self.write_routes({discord_routes.ROUTE_NAMES[0]: VALID_URL_A})
        os.chmod(self.route_file, 0o640)
        with self.assertRaisesRegex(
            discord_routes.RouteConfigurationError, "0600"
        ):
            discord_routes.load_discord_routes(self.route_file)

        os.chmod(self.route_file, 0o600)
        os.chmod(self.secret_dir, 0o750)
        with self.assertRaisesRegex(
            discord_routes.RouteConfigurationError, "0700"
        ):
            discord_routes.load_discord_routes(self.route_file)

    def test_file_symlink_and_non_regular_file_are_rejected(self):
        target = self.secret_dir / "target.json"
        target.write_text(
            json.dumps({discord_routes.ROUTE_NAMES[0]: VALID_URL_A}),
            encoding="utf-8",
        )
        os.chmod(target, 0o600)
        self.route_file.symlink_to(target.name)
        with self.assertRaisesRegex(
            discord_routes.RouteConfigurationError, "unsafe"
        ):
            discord_routes.load_discord_routes(self.route_file)

        self.route_file.unlink()
        self.route_file.mkdir(mode=0o600)
        with self.assertRaises(discord_routes.RouteConfigurationError):
            discord_routes.load_discord_routes(self.route_file)

    def test_symlink_directory_is_rejected(self):
        real = self.root / "real-secrets"
        real.mkdir(mode=0o700)
        os.chmod(real, 0o700)
        route = real / "discord_webhooks.json"
        route.write_text(
            json.dumps({discord_routes.ROUTE_NAMES[0]: VALID_URL_A}),
            encoding="utf-8",
        )
        os.chmod(route, 0o600)
        linked = self.root / "linked-secrets"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(discord_routes.RouteConfigurationError):
            discord_routes.load_discord_routes(linked / route.name)

    def test_preflight_allows_missing_paused_smoke_and_warns_on_extras(self):
        routes = {name: VALID_URL_A for name in discord_routes.REQUIRED_ROUTE_NAMES}
        routes["Unknown producer"] = VALID_URL_B
        self.write_routes(routes)
        statuses, unknown_count, valid = discord_routes.preflight_route_statuses(
            self.route_file
        )
        self.assertTrue(valid)
        self.assertEqual(unknown_count, 1)
        self.assertEqual(statuses["Smoke Test USDC Transfer"], "optional-missing")

    def test_preflight_fails_when_required_route_is_missing(self):
        routes = {name: VALID_URL_A for name in discord_routes.REQUIRED_ROUTE_NAMES[1:]}
        self.write_routes(routes)
        statuses, unknown_count, valid = discord_routes.preflight_route_statuses(
            self.route_file
        )
        self.assertFalse(valid)
        self.assertEqual(unknown_count, 0)
        self.assertEqual(statuses[discord_routes.REQUIRED_ROUTE_NAMES[0]], "missing")


class DeliveryTests(SecureRouteFixture):
    def test_modes_are_explicit_and_unknown_producers_fail_closed(self):
        with mock.patch.object(discord_routes, "post_discord_webhook") as post:
            for mode in (None, "", "LOG-ONLY", "dry-run"):
                with self.subTest(mode=mode), self.assertRaises(
                    discord_routes.RouteConfigurationError
                ):
                    discord_routes.deliver_alert(
                        discord_routes.ROUTE_NAMES[0], "content", mode=mode
                    )
            with self.assertRaises(discord_routes.RouteConfigurationError):
                discord_routes.deliver_alert("Unknown producer", "content", mode="live")
        post.assert_not_called()

    def test_log_only_writes_locally_without_route_file_or_http(self):
        log_path = self.root / "logs" / "alerts.log"
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            discord_routes, "post_discord_webhook"
        ) as post:
            delivered = discord_routes.deliver_alert(
                discord_routes.ROUTE_NAMES[0],
                "hello",
                mode="log-only",
                log_path=log_path,
                context={"network": "ethereum_mainnet", "tx": "0xabc"},
            )
        self.assertFalse(delivered)
        post.assert_not_called()
        record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["monitor"], discord_routes.ROUTE_NAMES[0])
        self.assertEqual(record["mode"], "log-only")
        self.assertEqual(record["network"], "ethereum_mainnet")

    def test_missing_route_fails_without_legacy_env_fallback(self):
        self.write_routes({discord_routes.ROUTE_NAMES[1]: VALID_URL_B})
        environment = {
            discord_routes.ROUTE_PATH_ENV: str(self.route_file),
            "DISCORD_WEBHOOK_URL": VALID_URL_A,
        }
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
            discord_routes, "post_discord_webhook"
        ) as post:
            with self.assertRaises(discord_routes.RouteConfigurationError) as raised:
                discord_routes.deliver_alert(
                    discord_routes.ROUTE_NAMES[0], "hello", mode="live"
                )
        post.assert_not_called()
        self.assertNotIn("token", str(raised.exception))
        self.assertNotIn("discord.com", str(raised.exception))

    def test_live_delivery_reloads_after_atomic_route_replacement(self):
        name = discord_routes.ROUTE_NAMES[0]
        self.write_routes({name: VALID_URL_A})
        with mock.patch.dict(
            os.environ, {discord_routes.ROUTE_PATH_ENV: str(self.route_file)}, clear=True
        ), mock.patch.object(discord_routes, "post_discord_webhook") as post:
            self.assertTrue(discord_routes.deliver_alert(name, "one", mode="live"))
            self.atomic_routes({name: VALID_URL_B})
            self.assertTrue(discord_routes.deliver_alert(name, "two", mode="live"))
        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [VALID_URL_A, VALID_URL_B],
        )

    def test_confirmed_post_disables_mentions_and_never_leaks_failure_url(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id":"1"}'
        with mock.patch.object(
            discord_routes.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            discord_routes.post_discord_webhook(VALID_URL_A, "hello @everyone")
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(body["content"], "hello @everyone")
        self.assertEqual(
            request.full_url,
            VALID_URL_A + "?wait=true",
        )

        with mock.patch.object(
            discord_routes.urllib.request,
            "urlopen",
            side_effect=OSError(VALID_URL_A),
        ):
            with self.assertRaises(discord_routes.RouteDeliveryError) as raised:
                discord_routes.post_discord_webhook(
                    VALID_URL_A, "hello", attempts=1
                )
        self.assertNotIn("token-A", str(raised.exception))
        self.assertNotIn("discord.com", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
