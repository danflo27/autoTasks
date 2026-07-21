import calendar
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


MIGRATION_DIR = Path(__file__).resolve().parents[1]
OPS_DIR = MIGRATION_DIR / "ops"
sys.path.insert(0, str(MIGRATION_DIR))
sys.path.insert(0, str(OPS_DIR))

import monitor_watchdog as watchdog  # noqa: E402
import report_freshness as freshness  # noqa: E402


UTC = timezone.utc
WALL_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def abi_get_data_before(found, value, timestamp):
    value = bytes(value)
    padded = value + b"\0" * ((32 - len(value) % 32) % 32)
    words = (
        int(bool(found)).to_bytes(32, "big")
        + (96).to_bytes(32, "big")
        + int(timestamp).to_bytes(32, "big")
        + len(value).to_bytes(32, "big")
        + padded
    )
    return "0x" + words.hex()


class FakeProvider:
    def __init__(self, now=WALL_NOW, name="primary", report_factory=None):
        self.name = name
        self.head = 100_012
        self.confirmed_height = self.head - freshness.CONFIRMATION_BLOCKS
        self.confirmed_timestamp = int(now.timestamp()) - 144
        self.genesis_timestamp = self.confirmed_timestamp - self.confirmed_height * 12
        self.report_factory = report_factory or self._normal_report
        self.query_calls = []
        self.block_calls = []
        self.chain = 1
        self.failure = None

    @staticmethod
    def _normal_report(query_id, before_timestamp, _block_number):
        if query_id == freshness.ETH_QUERY_ID:
            return freshness.Report(True, before_timestamp - 1)
        return freshness.Report(True, before_timestamp - 1)

    def chain_id(self):
        if self.failure:
            raise freshness.RpcError(self.failure)
        return self.chain

    def block_number(self):
        if self.failure:
            raise freshness.RpcError(self.failure)
        return self.head

    def block(self, number):
        self.block_calls.append(number)
        return freshness.Block(
            number=number,
            timestamp=self.genesis_timestamp + number * 12,
            block_hash="0x{:064x}".format(number + 1),
        )

    def get_data_before(self, query_id, before_timestamp, block_number):
        self.query_calls.append((query_id, before_timestamp, block_number))
        result = self.report_factory(query_id, before_timestamp, block_number)
        if result.found:
            assert result.timestamp < before_timestamp
        return result


class DeliveryRecorder:
    def __init__(self):
        self.calls = []
        self.failures = {}

    def fail(self, route, count=1):
        self.failures[route] = self.failures.get(route, 0) + count

    def __call__(self, route, content, **kwargs):
        self.calls.append((route, content, kwargs))
        remaining = self.failures.get(route, 0)
        if remaining:
            self.failures[route] = remaining - 1
            raise RuntimeError("simulated delivery failure")
        return kwargs.get("mode") == "live"

    def routes(self):
        return [call[0] for call in self.calls]


class CheckerHarness:
    def __init__(self, provider, now=WALL_NOW, deliver=None):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = freshness.StateStore(root / "state.json", now=lambda: now)
        self.deliver = deliver or DeliveryRecorder()
        self.checker = freshness.FreshnessChecker(
            providers=[provider],
            store=self.store,
            deliver=self.deliver,
            delivery_mode="log-only",
            heartbeat_path=root / "heartbeat",
            alert_log_path=root / "alerts.log",
            now=lambda: now,
        )

    def close(self):
        self.temporary.cleanup()


class ContractAndRpcTests(unittest.TestCase):
    def test_pinned_selector_and_source(self):
        self.assertEqual(freshness.GET_DATA_BEFORE_SELECTOR, "a792765f")
        self.assertEqual(
            freshness.TELLORFLEX_SOURCE_COMMIT,
            "e2946ecc12b22e72e63bec6f298d31ff22967d5c",
        )

    def test_decode_get_data_before_strict_boundary(self):
        encoded = abi_get_data_before(True, b"value", 100)
        report = freshness.decode_get_data_before(encoded, 101)
        self.assertEqual(report, freshness.Report(True, 100))
        with self.assertRaisesRegex(freshness.RpcError, "strict-before"):
            freshness.decode_get_data_before(encoded, 100)

    def test_decode_get_data_before_rejects_noncanonical_values(self):
        malformed_bool = bytearray.fromhex(abi_get_data_before(True, b"x", 1)[2:])
        malformed_bool[31] = 2
        with self.assertRaisesRegex(freshness.RpcError, "bool"):
            freshness.decode_get_data_before("0x" + malformed_bool.hex(), 2)
        with self.assertRaisesRegex(freshness.RpcError, "inconsistent"):
            freshness.decode_get_data_before(abi_get_data_before(False, b"x", 0), 2)

    def test_http_client_builds_cutoff_plus_one_calldata_and_block_tag(self):
        provider = freshness.HttpRpcProvider("test", "https://rpc.example")
        observed = []

        def fake_call(method, params):
            observed.append((method, params))
            return abi_get_data_before(True, b"x", 1_000)

        provider._call = fake_call
        report = provider.get_data_before(freshness.ETH_QUERY_ID, 1_001, 99)
        self.assertEqual(report.timestamp, 1_000)
        method, params = observed[0]
        self.assertEqual(method, "eth_call")
        self.assertEqual(params[1], "0x63")
        expected = (
            "0x"
            + freshness.GET_DATA_BEFORE_SELECTOR
            + freshness.ETH_QUERY_ID[2:]
            + "{:064x}".format(1_001)
        )
        self.assertEqual(params[0]["data"], expected)

    def test_resolves_first_block_at_or_after_cutoff(self):
        provider = FakeProvider()
        cutoff = provider.block(30_000).timestamp + 1
        found = freshness.first_block_at_or_after(provider, cutoff, provider.confirmed_height)
        self.assertEqual(found.number, 30_001)
        self.assertLess(provider.block(found.number - 1).timestamp, cutoff)
        self.assertGreaterEqual(found.timestamp, cutoff)

    def test_snapshot_uses_head_minus_twelve_and_confirmed_cutoff_blocks(self):
        provider = FakeProvider()
        state = freshness._default_state()
        snapshot = freshness.SnapshotCollector(provider, state, WALL_NOW).collect()
        self.assertEqual(snapshot.confirmed_block.number, provider.head - 12)
        eth_call = next(call for call in provider.query_calls if call[0] == freshness.ETH_QUERY_ID)
        self.assertEqual(eth_call[1], snapshot.confirmed_block.timestamp + 1)
        self.assertEqual(eth_call[2], snapshot.confirmed_block.number)
        for observation in (snapshot.ampl, snapshot.uspce):
            self.assertIsNotNone(observation)
            self.assertEqual(
                observation.report.timestamp,
                observation.cutoff,
            )
            self.assertGreaterEqual(observation.cutoff_block.timestamp, observation.cutoff)
            self.assertGreaterEqual(
                provider.head - observation.cutoff_block.number,
                freshness.CONFIRMATION_BLOCKS,
            )

    def test_period_reads_use_confirmed_block_for_current_dispute_state(self):
        provider = FakeProvider()

        def reports(query_id, before_timestamp, block_number):
            if query_id == freshness.ETH_QUERY_ID:
                return freshness.Report(True, before_timestamp - 1)
            # Model a deadline report that existed at the cutoff block but is
            # disputed by the time the current confirmed block is evaluated.
            if block_number < provider.confirmed_height:
                return freshness.Report(True, before_timestamp - 1)
            return freshness.Report(False, 0)

        provider.report_factory = reports
        snapshot = freshness.SnapshotCollector(
            provider, freshness._default_state(), WALL_NOW
        ).collect()

        self.assertFalse(snapshot.ampl.report.found)
        self.assertFalse(snapshot.uspce.report.found)
        period_calls = [
            call for call in provider.query_calls
            if call[0] in (freshness.AMPL_QUERY_ID, freshness.USPCE_QUERY_ID)
        ]
        self.assertEqual(
            [call[2] for call in period_calls],
            [snapshot.confirmed_block.number, snapshot.confirmed_block.number],
        )

    def test_fallback_restarts_complete_check_on_wrong_chain(self):
        wrong = FakeProvider(name="wrong")
        wrong.chain = 10
        good = FakeProvider(name="fallback")
        snapshot, attempted = freshness.collect_with_fallback(
            [wrong, good], freshness._default_state(), WALL_NOW
        )
        self.assertEqual(attempted, 2)
        self.assertEqual(snapshot.provider_name, "fallback")
        self.assertEqual(wrong.query_calls, [])

    def test_stale_confirmed_head_falls_back(self):
        stale = FakeProvider(name="stale")
        stale.confirmed_timestamp -= freshness.MAX_CONFIRMED_HEAD_AGE_SECONDS + 1
        stale.genesis_timestamp -= freshness.MAX_CONFIRMED_HEAD_AGE_SECONDS + 1
        good = FakeProvider(name="fallback")
        snapshot, attempted = freshness.collect_with_fallback(
            [stale, good], freshness._default_state(), WALL_NOW
        )
        self.assertEqual((snapshot.provider_name, attempted), ("fallback", 2))

    def test_invalid_configured_url_remains_a_fallback_failure(self):
        providers = freshness.configured_providers({
            "RPC_ETHEREUM_MAINNET": "not a URL",
            "RPC_ETHEREUM_MAINNET_FALLBACK_BLOCKPI": "https://rpc.example",
        })
        self.assertEqual([provider.name for provider in providers], [
            "RPC_ETHEREUM_MAINNET",
            "RPC_ETHEREUM_MAINNET_FALLBACK_BLOCKPI",
        ])
        with self.assertRaises(freshness.RpcError):
            providers[0].chain_id()

    def test_empty_provider_configuration_enters_failure_state_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deliver = DeliveryRecorder()
            store = freshness.StateStore(root / "state.json", now=lambda: WALL_NOW)
            checker = freshness.FreshnessChecker(
                providers=freshness.configured_providers({}),
                store=store,
                deliver=deliver,
                delivery_mode="log-only",
                heartbeat_path=root / "heartbeat",
                alert_log_path=root / "alerts.log",
                now=lambda: WALL_NOW,
            )

            for expected_failures in (1, 2, 3):
                with self.assertRaises(freshness.AllProvidersFailed):
                    checker.run()
                self.assertEqual(
                    store.load()["rpc"]["consecutive_failures"], expected_failures
                )

            self.assertEqual(deliver.routes(), [freshness.HEALTH_ROUTE])
            self.assertIn("Providers attempted: `0`", deliver.calls[0][1])
            heartbeat = json.loads((root / "heartbeat").read_text())
            self.assertEqual(heartbeat["status"], "rpc-failure")


class CalendarTests(unittest.TestCase):
    def test_ampl_window_is_inclusive_at_both_ends(self):
        start = int(datetime(2026, 7, 21, tzinfo=UTC).timestamp())
        cutoff = start + 30 * 60
        block = freshness.Block(1, cutoff, "0x" + "01" * 32)
        for timestamp in (start, cutoff):
            observation = freshness.PeriodObservation(
                "2026-07-21", start, cutoff, block, freshness.Report(True, timestamp)
            )
            self.assertTrue(observation.has_report_in_window)
        for timestamp in (start - 1, cutoff + 1):
            observation = freshness.PeriodObservation(
                "2026-07-21", start, cutoff, block, freshness.Report(True, timestamp)
            )
            self.assertFalse(observation.has_report_in_window)

    def test_ampl_latest_period_across_utc_deadline(self):
        before = datetime(2026, 7, 21, 0, 29, 59, tzinfo=UTC)
        at = datetime(2026, 7, 21, 0, 30, 0, tzinfo=UTC)
        self.assertEqual(
            freshness.latest_ampl_period(int(before.timestamp())), date(2026, 7, 20)
        )
        self.assertEqual(
            freshness.latest_ampl_period(int(at.timestamp())), date(2026, 7, 21)
        )

    def test_uspce_deadlines_cover_every_month_length_and_leap_year(self):
        expected = {
            (year, month): calendar.monthrange(year, month)[1]
            for year in (2024, 2025)
            for month in range(1, 13)
        }
        for (year, month), final_day in expected.items():
            with self.subTest(year=year, month=month):
                start, cutoff = freshness.uspce_period_bounds(year, month)
                self.assertEqual(start.day, 1)
                self.assertEqual(cutoff.day, final_day)
                self.assertEqual(cutoff.hour, 0)
                self.assertEqual(final_day, calendar.monthrange(year, month)[1])

    def test_uspce_period_rolls_over_december_and_at_final_day(self):
        before = datetime(2026, 1, 30, 23, 59, 59, tzinfo=UTC)
        at = datetime(2026, 1, 31, 0, 0, 0, tzinfo=UTC)
        self.assertEqual(freshness.latest_uspce_period(int(before.timestamp())), (2025, 12))
        self.assertEqual(freshness.latest_uspce_period(int(at.timestamp())), (2026, 1))


class StateTests(unittest.TestCase):
    def test_state_round_trip_is_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = freshness.StateStore(path)
            expected = freshness._default_state()
            store.save(expected)
            self.assertEqual(store.load(), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_state_is_preserved_then_bootstrapped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = freshness.StateStore(path, now=lambda: WALL_NOW)
            state = store.load()
            self.assertEqual(state, freshness._default_state())
            self.assertTrue(store.bootstrapped)
            self.assertIsNotNone(store.preserved_corrupt_path)
            self.assertEqual(store.preserved_corrupt_path.read_text(), "{not json")
            self.assertFalse(path.exists())

    def test_symlink_state_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("keep")
            state = root / "state.json"
            state.symlink_to(target)
            with self.assertRaisesRegex(freshness.StateError, "non-symlink"):
                freshness.StateStore(state).load()
            self.assertEqual(target.read_text(), "keep")

    def test_failed_atomic_replace_keeps_prior_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = freshness.StateStore(path)
            initial = freshness._default_state()
            store.save(initial)
            changed = freshness._default_state()
            changed["eth"]["incident_open"] = True
            with mock.patch.object(freshness.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(freshness.StateError):
                    store.save(changed)
            self.assertEqual(store.load(), initial)


class FreshnessTransitionTests(unittest.TestCase):
    def _provider_with_eth_age(self, age):
        def reports(query_id, before_timestamp, _block_number):
            if query_id == freshness.ETH_QUERY_ID:
                return freshness.Report(True, before_timestamp - 1 - age)
            return freshness.Report(True, before_timestamp - 1)

        return FakeProvider(report_factory=reports)

    def test_eth_threshold_is_strictly_greater_than_14400(self):
        for age, should_alert in ((14_399, False), (14_400, False), (14_401, True)):
            with self.subTest(age=age):
                harness = CheckerHarness(self._provider_with_eth_age(age))
                try:
                    harness.checker.run()
                    self.assertEqual(
                        freshness.ETH_ROUTE in harness.deliver.routes(), should_alert
                    )
                finally:
                    harness.close()

    def test_eth_alarm_deduplicates_and_sends_one_recovery(self):
        current_age = [14_401]

        def reports(query_id, before_timestamp, _block_number):
            if query_id == freshness.ETH_QUERY_ID:
                return freshness.Report(True, before_timestamp - 1 - current_age[0])
            return freshness.Report(True, before_timestamp - 1)

        harness = CheckerHarness(FakeProvider(report_factory=reports))
        try:
            harness.checker.run()
            harness.checker.run()
            current_age[0] = 60
            harness.checker.run()
            eth_calls = [call for call in harness.deliver.calls if call[0] == freshness.ETH_ROUTE]
            self.assertEqual(len(eth_calls), 2)
            self.assertIn("stale", eth_calls[0][1])
            self.assertIn("recovered", eth_calls[1][1])
        finally:
            harness.close()

    def test_failed_eth_delivery_does_not_advance_and_retries(self):
        deliver = DeliveryRecorder()
        deliver.fail(freshness.ETH_ROUTE)
        harness = CheckerHarness(self._provider_with_eth_age(14_401), deliver=deliver)
        try:
            with self.assertRaises(freshness.DeliveryTransitionError):
                harness.checker.run()
            self.assertFalse(harness.store.load()["eth"]["incident_open"])
            harness.checker.run()
            self.assertTrue(harness.store.load()["eth"]["incident_open"])
            self.assertEqual(deliver.routes().count(freshness.ETH_ROUTE), 2)
        finally:
            harness.close()

    def test_failed_eth_recovery_delivery_keeps_incident_open(self):
        current_age = [14_401]

        def reports(query_id, before_timestamp, _block_number):
            if query_id == freshness.ETH_QUERY_ID:
                return freshness.Report(True, before_timestamp - 1 - current_age[0])
            return freshness.Report(True, before_timestamp - 1)

        deliver = DeliveryRecorder()
        harness = CheckerHarness(FakeProvider(report_factory=reports), deliver=deliver)
        try:
            harness.checker.run()
            current_age[0] = 60
            deliver.fail(freshness.ETH_ROUTE)
            with self.assertRaises(freshness.DeliveryTransitionError):
                harness.checker.run()
            self.assertTrue(harness.store.load()["eth"]["incident_open"])
            harness.checker.run()
            self.assertFalse(harness.store.load()["eth"]["incident_open"])
        finally:
            harness.close()

    def test_missed_calendar_delivery_is_immutable_and_retried(self):
        def reports(query_id, before_timestamp, _block_number):
            if query_id == freshness.AMPL_QUERY_ID:
                return freshness.Report(False, 0)
            return freshness.Report(True, before_timestamp - 1)

        deliver = DeliveryRecorder()
        deliver.fail(freshness.AMPL_ROUTE)
        harness = CheckerHarness(FakeProvider(report_factory=reports), deliver=deliver)
        try:
            with self.assertRaises(freshness.DeliveryTransitionError):
                harness.checker.run()
            self.assertIsNone(harness.store.load()["ampl"]["last_period"])
            harness.checker.run()
            delivered_period = harness.store.load()["ampl"]["last_period"]
            self.assertIsNotNone(delivered_period)
            harness.checker.run()
            self.assertEqual(deliver.routes().count(freshness.AMPL_ROUTE), 2)
        finally:
            harness.close()

    def test_bootstrap_queries_only_latest_daily_and_monthly_period(self):
        provider = FakeProvider()
        harness = CheckerHarness(provider)
        try:
            harness.checker.run()
            query_ids = [call[0] for call in provider.query_calls]
            self.assertEqual(query_ids.count(freshness.AMPL_QUERY_ID), 1)
            self.assertEqual(query_ids.count(freshness.USPCE_QUERY_ID), 1)
            state = harness.store.load()
            self.assertEqual(state["ampl"]["last_period"], "2026-07-21")
            self.assertEqual(state["uspce"]["last_period"], "2026-06")
        finally:
            harness.close()

    def test_three_complete_rpc_failures_alarm_once_then_recover(self):
        provider = FakeProvider()
        provider.failure = "unavailable"
        harness = CheckerHarness(provider)
        try:
            for expected_count in (1, 2, 3, 3):
                with self.assertRaises(freshness.AllProvidersFailed):
                    harness.checker.run()
                self.assertEqual(
                    harness.store.load()["rpc"]["consecutive_failures"], expected_count
                )
            self.assertEqual(harness.deliver.routes().count(freshness.HEALTH_ROUTE), 1)
            self.assertNotIn(freshness.ETH_ROUTE, harness.deliver.routes())
            self.assertNotIn(freshness.AMPL_ROUTE, harness.deliver.routes())
            provider.failure = None
            harness.checker.run()
            health_calls = [call for call in harness.deliver.calls if call[0] == freshness.HEALTH_ROUTE]
            self.assertEqual(len(health_calls), 2)
            self.assertIn("recovered", health_calls[1][1])
            self.assertEqual(
                harness.store.load()["rpc"],
                {"consecutive_failures": 0, "incident_open": False},
            )
        finally:
            harness.close()

    def test_failed_rpc_health_delivery_stays_pending(self):
        provider = FakeProvider()
        provider.failure = "unavailable"
        deliver = DeliveryRecorder()
        deliver.fail(freshness.HEALTH_ROUTE)
        harness = CheckerHarness(provider, deliver=deliver)
        try:
            for _ in range(2):
                with self.assertRaises(freshness.AllProvidersFailed):
                    harness.checker.run()
            with self.assertRaises(RuntimeError):
                harness.checker.run()
            self.assertEqual(
                harness.store.load()["rpc"],
                {"consecutive_failures": 2, "incident_open": False},
            )
            with self.assertRaises(freshness.AllProvidersFailed):
                harness.checker.run()
            self.assertTrue(harness.store.load()["rpc"]["incident_open"])
        finally:
            harness.close()

    def test_heartbeat_distinguishes_success_and_rpc_failure(self):
        provider = FakeProvider()
        harness = CheckerHarness(provider)
        try:
            harness.checker.run()
            payload = json.loads(harness.checker.heartbeat_path.read_text())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["confirmed_block"], provider.confirmed_height)
            provider.failure = "unavailable"
            with self.assertRaises(freshness.AllProvidersFailed):
                harness.checker.run()
            payload = json.loads(harness.checker.heartbeat_path.read_text())
            self.assertEqual(payload["status"], "rpc-failure")
            self.assertIsNone(payload["confirmed_block"])
        finally:
            harness.close()


class FakeWatchdogInspector:
    def __init__(self, desired, results=None):
        self.desired = desired
        self.results = list(results or [])
        self.inspect_calls = 0

    def desired_running(self):
        return self.desired

    def inspect(self, _state, _now):
        self.inspect_calls += 1
        return self.results.pop(0)


class WatchdogTests(unittest.TestCase):
    def _make(self, inspector, directory, deliver):
        return watchdog.MonitorWatchdog(
            inspector=inspector,
            store=watchdog.WatchdogStateStore(Path(directory) / "state.json", now=lambda: WALL_NOW),
            deliver=deliver,
            delivery_mode="log-only",
            alert_log_path=Path(directory) / "alerts.log",
            now=lambda: WALL_NOW,
            hostname=lambda: "tellor-ops",
        )

    def test_desired_off_is_quiet_and_clears_old_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            store = watchdog.WatchdogStateStore(Path(directory) / "state.json")
            state = watchdog._default_state()
            state["incident_open"] = True
            state["last_checkpoint"] = 10
            state["checkpoint_seen_at"] = 1
            store.save(state)
            deliver = DeliveryRecorder()
            monitor = watchdog.MonitorWatchdog(
                inspector=FakeWatchdogInspector(False),
                store=store,
                deliver=deliver,
                delivery_mode="log-only",
                alert_log_path=Path(directory) / "alerts.log",
                now=lambda: WALL_NOW,
            )
            self.assertTrue(monitor.run())
            self.assertEqual(deliver.calls, [])
            self.assertEqual(store.load(), watchdog._default_state())

    def test_watchdog_deduplicates_warning_and_sends_recovery(self):
        bad = watchdog.HealthResult(False, "checkpoint stale", 100, 1)
        good = watchdog.HealthResult(True, "checks passed", 101, 2)
        inspector = FakeWatchdogInspector(True, [bad, bad, good])
        deliver = DeliveryRecorder()
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._make(inspector, directory, deliver)
            self.assertFalse(monitor.run())
            self.assertFalse(monitor.run())
            self.assertTrue(monitor.run())
            calls = [call for call in deliver.calls if call[0] == watchdog.WATCHDOG_ROUTE]
            self.assertEqual(len(calls), 2)
            self.assertIn("warning", calls[0][1])
            self.assertIn("recovered", calls[1][1])

    def test_watchdog_failed_warning_delivery_does_not_open_incident(self):
        bad = watchdog.HealthResult(False, "checkpoint stale", 100, 1)
        inspector = FakeWatchdogInspector(True, [bad, bad])
        deliver = DeliveryRecorder()
        deliver.fail(watchdog.WATCHDOG_ROUTE)
        with tempfile.TemporaryDirectory() as directory:
            monitor = self._make(inspector, directory, deliver)
            with self.assertRaises(RuntimeError):
                monitor.run()
            state = watchdog.WatchdogStateStore(Path(directory) / "state.json").load()
            self.assertFalse(state["incident_open"])
            self.assertFalse(monitor.run())
            state = watchdog.WatchdogStateStore(Path(directory) / "state.json").load()
            self.assertTrue(state["incident_open"])

    def test_monitor_inspector_checks_container_health_and_checkpoint_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint"
            checkpoint.write_text("100")
            now_timestamp = int(WALL_NOW.timestamp())
            os.utime(checkpoint, (now_timestamp, now_timestamp))

            def command(arguments):
                if arguments[1:3] == ("compose", "--project-directory"):
                    return watchdog.CommandResult(0, "container-id")
                if arguments[1] == "inspect":
                    return watchdog.CommandResult(
                        0, json.dumps({"Status": "running", "Health": {"Status": "healthy"}})
                    )
                raise AssertionError(arguments)

            inspector = watchdog.MonitorInspector(
                command=command,
                checkpoint_path=checkpoint,
                migration_dir=Path(directory),
                compose_file=Path(directory) / "compose.yaml",
                max_checkpoint_age=600,
            )
            state = watchdog._default_state()
            first = inspector.inspect(state, now_timestamp)
            self.assertTrue(first.healthy)
            state["last_checkpoint"] = 100
            state["checkpoint_seen_at"] = now_timestamp - 601
            second = inspector.inspect(state, now_timestamp)
            self.assertFalse(second.healthy)
            self.assertIn("not advanced", second.summary)

    def test_ops_units_are_gated_and_hardened(self):
        service = (OPS_DIR / "systemd" / "tellor-report-freshness.service").read_text()
        timer = (OPS_DIR / "systemd" / "tellor-report-freshness.timer").read_text()
        watchdog_service = (OPS_DIR / "systemd" / "monitor-watchdog.service").read_text()
        installer = (OPS_DIR / "install-monitoring.sh").read_text()
        self.assertIn("User=tellor-monitoring", service)
        self.assertIn("StateDirectory=tellor/report-freshness", service)
        self.assertIn("NoNewPrivileges=yes", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("OnCalendar=*-*-* *:*:00 UTC", timer)
        self.assertNotIn("User=tellor-monitoring", watchdog_service)
        self.assertIn("docker.service", watchdog_service)
        self.assertIn('if [[ "${1:-}" != "--enable" ]]', installer)
        self.assertIn("check-discord-routes", installer)


if __name__ == "__main__":
    unittest.main()
