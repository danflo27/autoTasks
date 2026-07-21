import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "config" / "triggers" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tellor_lib  # noqa: E402


class FakeMatch:
    monitor_name = "Tellor Staking"
    network = "ethereum_mainnet"
    tx_hash = "0xabc"


class DeliveryAdapterTests(unittest.TestCase):
    def test_production_log_only_is_explicit_and_ignores_legacy_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "alerts.log"
            with mock.patch.object(tellor_lib, "ALERT_LOG", str(log_path)), mock.patch.dict(
                os.environ,
                {
                    "TELLOR_ALERT_DELIVERY_MODE": "log-only",
                    "TELLOR_DELIVERY_MODE": "live",
                    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/retired",
                },
                clear=False,
            ), mock.patch.object(tellor_lib, "post_discord_webhook") as post:
                delivered = tellor_lib.send_alert(FakeMatch(), "one local record")

            self.assertFalse(delivered)
            post.assert_not_called()
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["monitor"], "Tellor Staking")
            self.assertEqual(records[0]["mode"], "log-only")
            self.assertNotIn("retired", log_path.read_text())

    def test_production_live_uses_route_file_not_shared_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            route_directory = Path(directory) / "secrets"
            route_directory.mkdir(mode=0o700)
            os.chmod(route_directory, 0o700)
            route_file = route_directory / "discord_webhooks.json"
            route_file.write_text(json.dumps({
                "Tellor Staking": "https://discord.com/api/webhooks/123/routed",
            }))
            os.chmod(route_file, 0o600)
            self.assertEqual(stat.S_IMODE(route_directory.stat().st_mode), 0o700)
            log_path = Path(directory) / "alerts.log"
            lifecycle_path = Path(directory) / "handler_delivery.log"
            with mock.patch.object(
                tellor_lib, "ALERT_LOG", str(log_path)
            ), mock.patch.object(
                tellor_lib, "HANDLER_DELIVERY_LOG", str(lifecycle_path)
            ), mock.patch.dict(
                os.environ,
                {
                    "TELLOR_ALERT_DELIVERY_MODE": "live",
                    "DISCORD_WEBHOOKS_FILE": str(route_file),
                    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/456/retired",
                },
                clear=False,
            ), mock.patch("discord_routes.post_discord_webhook") as post:
                delivered = tellor_lib.send_alert(FakeMatch(), "routed record")

            self.assertTrue(delivered)
            post.assert_called_once_with(
                "https://discord.com/api/webhooks/123/routed", "routed record"
            )

    def test_platform_mode_and_single_materialized_webhook_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "alerts.log"
            lifecycle_path = Path(directory) / "handler_delivery.log"
            with mock.patch.object(
                tellor_lib, "ALERT_LOG", str(log_path)
            ), mock.patch.object(
                tellor_lib, "HANDLER_DELIVERY_LOG", str(lifecycle_path)
            ), mock.patch.dict(
                os.environ,
                {
                    "TELLOR_DELIVERY_MODE": "live",
                    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/platform",
                },
                clear=True,
            ), mock.patch("discord_routes.post_discord_webhook") as post:
                delivered = tellor_lib.send_alert(FakeMatch(), "platform record")

            self.assertTrue(delivered)
            post.assert_called_once_with(
                "https://discord.com/api/webhooks/123/platform", "platform record"
            )
            events = [
                json.loads(line)["event"]
                for line in lifecycle_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                events,
                [
                    "handler_delivery_eligible",
                    "handler_delivery_attempted",
                    "handler_delivery_succeeded",
                ],
            )

    def test_legacy_live_mode_never_falls_back_to_shared_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            alert_path = Path(directory) / "alerts.log"
            lifecycle_path = Path(directory) / "handler_delivery.log"
            with mock.patch.object(
                tellor_lib, "ALERT_LOG", str(alert_path)
            ), mock.patch.object(
                tellor_lib, "HANDLER_DELIVERY_LOG", str(lifecycle_path)
            ), mock.patch.dict(
                os.environ,
                {
                    "TELLOR_ALERT_DELIVERY_MODE": "live",
                    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/stale",
                },
                clear=True,
            ), mock.patch("discord_routes.post_discord_webhook") as post:
                with self.assertRaisesRegex(RuntimeError, "no configured Discord route"):
                    tellor_lib.send_alert(FakeMatch(), "must fail closed")

            post.assert_not_called()
            events = [
                json.loads(line)["event"]
                for line in lifecycle_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                events,
                ["handler_delivery_eligible", "handler_delivery_failed"],
            )

    def test_missing_delivery_mode_fails_instead_of_implicit_log_only(self):
        environment = dict(os.environ)
        environment.pop("TELLOR_ALERT_DELIVERY_MODE", None)
        environment.pop("TELLOR_DELIVERY_MODE", None)
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
            tellor_lib, "post_discord_webhook"
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "explicitly set"):
                tellor_lib.send_alert(FakeMatch(), "must fail")
        post.assert_not_called()

    def test_quickstart_custom_webhook_remains_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "alerts.log"
            with mock.patch.object(tellor_lib, "ALERT_LOG", str(log_path)), mock.patch.dict(
                os.environ,
                {
                    "TELLOR_ALERT_DELIVERY_MODE": "live",
                    "CUSTOM_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/custom",
                    "DISCORD_WEBHOOKS_FILE": "",
                },
                clear=False,
            ), mock.patch.object(tellor_lib, "post_discord_webhook") as post:
                delivered = tellor_lib.send_alert(
                    FakeMatch(),
                    "custom record",
                    webhook_env="CUSTOM_DISCORD_WEBHOOK_URL",
                )

            self.assertTrue(delivered)
            post.assert_called_once_with(
                "https://discord.com/api/webhooks/123/custom", "custom record"
            )
            self.assertEqual(json.loads(log_path.read_text())["mode"], "live")

    def test_other_environment_webhooks_are_rejected(self):
        with mock.patch.dict(
            os.environ, {"TELLOR_ALERT_DELIVERY_MODE": "live"}, clear=False
        ):
            with self.assertRaisesRegex(RuntimeError, "only the generated quickstart"):
                tellor_lib.send_alert(
                    FakeMatch(), "no fallback", webhook_env="DISCORD_WEBHOOK_URL"
                )


if __name__ == "__main__":
    unittest.main()
