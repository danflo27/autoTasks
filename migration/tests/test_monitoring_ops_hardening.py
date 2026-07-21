import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


MIGRATION_DIR = Path(__file__).resolve().parents[1]
OPS_DIR = MIGRATION_DIR / "ops"
sys.path.insert(0, str(OPS_DIR))

import monitor_watchdog as watchdog  # noqa: E402


class MonitoringOpsHardeningTests(unittest.TestCase):
    def test_checkpoint_regression_remains_unhealthy_and_preserves_high_water_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "checkpoint"
            checkpoint_path.write_text("99", encoding="ascii")
            now_timestamp = int(
                datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc).timestamp()
            )
            os.utime(checkpoint_path, (now_timestamp, now_timestamp))
            inspector = watchdog.MonitorInspector(checkpoint_path=checkpoint_path)
            prior_state = watchdog._default_state()
            prior_state["last_checkpoint"] = 100
            prior_state["checkpoint_seen_at"] = now_timestamp - 10

            checkpoint, seen_at, reasons = inspector._inspect_checkpoint(
                prior_state, now_timestamp
            )

            self.assertEqual(checkpoint, 100)
            self.assertEqual(seen_at, now_timestamp - 10)
            self.assertEqual(
                reasons, ["Ethereum checkpoint regressed from 100 to 99"]
            )

    def test_installer_gates_env_file_type_mode_and_owner(self):
        installer = (OPS_DIR / "install-monitoring.sh").read_text(encoding="utf-8")

        self.assertIn('[[ ! -f "${ENV_FILE}" || -L "${ENV_FILE}" ]]', installer)
        self.assertIn(".env must have mode 0600", installer)
        self.assertIn(".env must be owned by root:root", installer)
        self.assertIn("stat -c '%a' \"${ENV_FILE}\"", installer)
        self.assertIn("stat -c '%U:%G' \"${ENV_FILE}\"", installer)


if __name__ == "__main__":
    unittest.main()
