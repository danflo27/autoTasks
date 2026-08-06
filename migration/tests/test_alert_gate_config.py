import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ALERT_GATE = ROOT / "migration" / "alert_gate"
sys.path.insert(0, str(ALERT_GATE))

from tellor_alert_gate.config import ConfigurationError, Settings  # noqa: E402
from tellor_alert_gate.constants import EVM_SENSOR_SLUGS  # noqa: E402
from tellor_alert_gate.policy import (  # noqa: E402
    validate_databridge_sensor,
    validate_monitor_policy,
)


class AlertGateConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.production = ROOT / "migration" / "production"

    def test_catalog_and_eight_sensor_files_are_exact(self):
        result = validate_monitor_policy(
            self.production / "policy" / "monitors.json",
            self.production / "config" / "monitors",
        )
        self.assertEqual(result, {"logical_monitors": 11, "evm_sensors": 8})
        names = {
            json.loads(path.read_text())["name"]
            for path in (self.production / "config" / "monitors").glob("*.json")
        }
        self.assertEqual(names, EVM_SENSOR_SLUGS)

    def test_databridge_sensor_equals_the_pre_enrolled_set(self):
        configured = validate_databridge_sensor(
            self.production / "policy" / "enrolled_databridges.json",
            self.production / "config" / "monitors",
        )
        self.assertEqual(
            configured,
            {"0xffa3393be1e4b442fff6cd0df0794b0031e9cf65"},
        )

    def test_sensors_have_only_success_gate_and_spool_trigger(self):
        for path in (self.production / "config" / "monitors").glob("*.json"):
            sensor = json.loads(path.read_text())
            self.assertEqual(
                sensor["match_conditions"]["transactions"],
                [{"status": "Success", "expression": None}],
            )
            self.assertEqual(sensor["trigger_conditions"], [])
            self.assertEqual(sensor["triggers"], ["tellor_alert"])

    def test_spool_script_performs_one_local_append(self):
        script = self.production / "config" / "triggers" / "scripts" / "alert.py"
        source = script.read_text()
        for forbidden in ("requests", "urllib", "discord", "webhook"):
            self.assertNotIn(forbidden, source.lower())
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory) / "matches.jsonl"
            payload = {
                "monitor_match": {
                    "EVM": {
                        "monitor": {"name": "issuance-integrity"},
                        "transaction": {"hash": "0x" + "12" * 32},
                    }
                }
            }
            environment = dict(os.environ)
            environment["ALERT_GATE_SPOOL_PATH"] = str(spool)
            completed = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = spool.read_text().splitlines()
            self.assertEqual(len(rows), 1)
            envelope = json.loads(rows[0])
            self.assertEqual(envelope["schema_version"], 1)
            self.assertEqual(envelope["payload"], payload)

    def test_container_dependencies_pin_keccak_backend(self):
        requirements = (ALERT_GATE / "requirements.txt").read_text().splitlines()
        self.assertIn("pycryptodome==3.23.0", requirements)

    def test_runtime_requires_distinct_providers_and_both_ledger_seeds(self):
        base = {
            "RPC_ETHEREUM_MAINNET": "https://primary.invalid",
            "RPC_ETHEREUM_MAINNET_SECONDARY": "https://secondary.invalid",
            "TELLOR_LAYER_URL": "https://layer.invalid",
            "LAYER_REPLAY_START_HEIGHT": "10",
            "LAYER_MINTER_SEED_FILE": "/run/secrets/seed.json",
            "BRIDGE_LEDGER_SEED_FILE": "/run/secrets/bridge-seed.json",
            "TELLOR_ALERT_DELIVERY_MODE": "log-only",
        }
        settings = Settings.from_env(base, require_runtime=True)
        self.assertEqual(settings.layer_start_height, 10)
        duplicate = dict(base)
        duplicate["RPC_ETHEREUM_MAINNET_SECONDARY"] = duplicate[
            "RPC_ETHEREUM_MAINNET"
        ]
        with self.assertRaises(ConfigurationError):
            Settings.from_env(duplicate, require_runtime=True)
        no_seed = dict(base)
        no_seed.pop("LAYER_MINTER_SEED_FILE")
        with self.assertRaises(ConfigurationError):
            Settings.from_env(no_seed, require_runtime=True)
        no_bridge_seed = dict(base)
        no_bridge_seed.pop("BRIDGE_LEDGER_SEED_FILE")
        with self.assertRaises(ConfigurationError):
            Settings.from_env(no_bridge_seed, require_runtime=True)

    def test_live_mode_requires_exact_thirty_second_provider_delay(self):
        environment = {
            "RPC_ETHEREUM_MAINNET": "https://primary.invalid",
            "RPC_ETHEREUM_MAINNET_SECONDARY": "https://secondary.invalid",
            "TELLOR_LAYER_URL": "https://layer.invalid",
            "LAYER_REPLAY_START_HEIGHT": "1",
            "BRIDGE_LEDGER_SEED_FILE": "/run/secrets/bridge-seed.json",
            "TELLOR_ALERT_DELIVERY_MODE": "live",
            "DISCORD_WEBHOOKS_FILE": "/run/secrets/routes.json",
            "ALERT_GATE_SECOND_READ_DELAY": "0",
        }
        with self.assertRaises(ConfigurationError):
            Settings.from_env(environment, require_runtime=True)

    def test_active_tree_excludes_superseded_monitor_stacks(self):
        superseded = (
            "EVMCall",
            "addressUpdates",
            "bridges",
            "datafeed",
            "disputes",
            "dvm",
            "priceMonitor",
            "staking",
            "tips",
            "tokenBridge",
            "migration/.env.example",
            "migration/AWS_DOCKER_OPERATIONS.md",
            "migration/DEPLOYMENT_MANIFEST.schema.json",
            "migration/MIGRATION.md",
            "migration/TELLOR_MONITORING_PROGRESS.md",
            "migration/TELLOR_OPS_EC2_HANDOFF.md",
            "migration/WORKLOAD_CONTRACT.md",
            "migration/config",
            "migration/docker-compose.yaml",
            "migration/oci",
            "migration/ops",
            "migration/quickstart.py",
            "migration/report_freshness.py",
        )
        for relative_path in superseded:
            self.assertFalse(
                (ROOT / relative_path).exists(),
                f"superseded artifact remains active: {relative_path}",
            )


if __name__ == "__main__":
    unittest.main()
