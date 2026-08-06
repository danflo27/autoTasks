"""Tests for the alert-gate process-liveness healthcheck.

`StateStore.poll_liveness` and the `healthcheck` CLI subcommand exist so
`docker-compose.production.yaml` can detect a wedged-but-alive alert-gate
process (e.g. deadlocked on the SQLite state file, or permanently failing
every RPC call) rather than only detecting a crashed one. See the docstring
on `StateStore.poll_liveness` in service/tellor_alert_gate/state.py for what
the signal actually proves and what it cannot catch.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from tellor_alert_gate.state import StateStore  # noqa: E402


class PollLivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def test_unhealthy_before_any_scheduled_pass_has_completed(self):
        healthy, age_seconds = self.store.poll_liveness()
        self.assertFalse(healthy)
        self.assertIsNone(age_seconds)

    def test_healthy_immediately_after_a_scheduled_pass(self):
        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.store.set_meta("last_scheduled_minute", int(now.timestamp()) // 60)
        healthy, age_seconds = self.store.poll_liveness(now=now)
        self.assertTrue(healthy)
        self.assertEqual(age_seconds, 0)

    def test_healthy_within_the_staleness_budget(self):
        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        recorded = now - timedelta(seconds=180)
        self.store.set_meta("last_scheduled_minute", int(recorded.timestamp()) // 60)
        healthy, age_seconds = self.store.poll_liveness(
            now=now, max_stale_seconds=300
        )
        self.assertTrue(healthy)
        self.assertEqual(age_seconds, 180)

    def test_unhealthy_once_the_poll_loop_stops_advancing_it(self):
        """A deadlocked or dead-RPC-wedged poll loop stops writing
        last_scheduled_minute entirely; poll_liveness must eventually flag
        that as unhealthy rather than staying green forever."""
        now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        recorded = now - timedelta(seconds=600)
        self.store.set_meta("last_scheduled_minute", int(recorded.timestamp()) // 60)
        healthy, age_seconds = self.store.poll_liveness(
            now=now, max_stale_seconds=300
        )
        self.assertFalse(healthy)
        self.assertEqual(age_seconds, 600)

    def test_malformed_metadata_is_treated_as_unhealthy_not_a_crash(self):
        self.store.set_meta("last_scheduled_minute", "not-a-number")
        healthy, age_seconds = self.store.poll_liveness()
        self.assertFalse(healthy)
        self.assertIsNone(age_seconds)


class HealthcheckCliTests(unittest.TestCase):
    def _run(self, state_path, extra_env=None):
        environment = {
            "ALERT_GATE_STATE_PATH": str(state_path),
            "PATH": "/usr/bin:/bin",
        }
        if extra_env:
            environment.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "tellor_alert_gate", "healthcheck"],
            cwd=str(ROOT / "service"),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_healthcheck_exits_nonzero_before_first_scheduled_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            result = self._run(state_path)
            self.assertEqual(result.returncode, 1, msg=result.stderr)
            self.assertIn('"status": "unhealthy"', result.stdout)

    def test_healthcheck_exits_zero_once_a_pass_has_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            store = StateStore(state_path)
            try:
                store.set_meta("last_scheduled_minute", int(__import__("time").time()) // 60)
            finally:
                store.close()
            result = self._run(state_path)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn('"status": "healthy"', result.stdout)


if __name__ == "__main__":
    unittest.main()
