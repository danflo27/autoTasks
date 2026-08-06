import calendar
from datetime import date, datetime, timezone
from pathlib import Path
import sys
import unittest

from eth_abi import encode


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "alert_gate"))

from tellor_alert_gate.freshness import (  # noqa: E402
    evaluate_ampl_day,
    evaluate_eth_usd,
    evaluate_uspce_month,
    latest_eligible_uspce_month,
)
from tellor_alert_gate.models import Unresolved  # noqa: E402


class FakeProvider:
    def __init__(self, name, now, report_timestamp, *, spacing=12, head=200, disagree=False):
        self.name = name
        self.now = int(now)
        self.report_timestamp = report_timestamp
        self.spacing = spacing
        self.head = head
        self.genesis_time = self.now - self.head * self.spacing
        self.disagree = disagree

    def chain_id(self):
        return 1

    def head_number(self):
        return self.head

    def block(self, number_or_tag, full_transactions=False):
        number = self.head if number_or_tag == "latest" else int(number_or_tag)
        timestamp = self.genesis_time + number * self.spacing
        digest = number + (1 if self.disagree and number == self.head - 12 else 0)
        return {
            "number": hex(number),
            "timestamp": hex(timestamp),
            "hash": "0x" + format(digest, "064x"),
        }

    def block_by_hash(self, block_hash, full_transactions=False):
        number = int(block_hash, 16)
        if self.disagree and number == self.head - 11:
            number -= 1
        return self.block(number)

    def eth_call(self, to, data, block="latest", extra=None):
        if self.report_timestamp is None:
            values = [False, b"", 0]
        else:
            values = [True, encode(["uint256"], [123]), int(self.report_timestamp)]
        return "0x" + encode(["bool", "bytes", "uint256"], values).hex()


def schedule_provider(name, now, report_timestamp, disagree=False):
    # One-minute blocks over 35 days are enough for every daily/monthly fixture.
    return FakeProvider(
        name,
        now,
        report_timestamp,
        spacing=60,
        head=35 * 24 * 60,
        disagree=disagree,
    )


class AlertGateFreshnessTests(unittest.TestCase):
    def test_eth_age_boundaries(self):
        now = 2_000_000_000
        selected_time = now - 12 * 12
        for age, failed in ((14_399, False), (14_400, False), (14_401, True)):
            primary = FakeProvider("primary", now, selected_time - age)
            secondary = FakeProvider("secondary", now, selected_time - age)
            outcome = evaluate_eth_usd(
                primary, secondary, second_delay=0, now=now
            )
            self.assertEqual(outcome.failed, failed, age)
            if failed:
                self.assertEqual(outcome.finding.severity, "P1")

    def test_eth_missing_report_fails_and_provider_disagreement_is_unresolved(self):
        now = 2_000_000_000
        outcome = evaluate_eth_usd(
            FakeProvider("primary", now, None),
            FakeProvider("secondary", now, None),
            second_delay=0,
            now=now,
        )
        self.assertTrue(outcome.failed)
        with self.assertRaises(Unresolved):
            evaluate_eth_usd(
                FakeProvider("primary", now, now - 100),
                FakeProvider("secondary", now, now - 100, disagree=True),
                second_delay=0,
                now=now,
            )

    def test_ampl_window_is_inclusive(self):
        target = date(2026, 8, 6)
        start = int(datetime(2026, 8, 6, tzinfo=timezone.utc).timestamp())
        cutoff = start + 30 * 60
        now = cutoff + 3600
        for timestamp, failed in (
            (start - 1, True),
            (start, False),
            (cutoff, False),
        ):
            outcome = evaluate_ampl_day(
                schedule_provider("primary", now, timestamp),
                schedule_provider("secondary", now, timestamp),
                day=target,
                second_delay=0,
                now=now,
            )
            self.assertEqual(outcome.failed, failed, timestamp)

    def test_uspce_cutoff_uses_gregorian_final_day(self):
        for year, month, final_day in (
            (2025, 2, 28),
            (2024, 2, 29),
            (2026, 4, 30),
            (2026, 8, 31),
        ):
            self.assertEqual(calendar.monthrange(year, month)[1], final_day)
            start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
            cutoff = int(
                datetime(year, month, final_day, tzinfo=timezone.utc).timestamp()
            )
            now = cutoff + 3600
            outcome = evaluate_uspce_month(
                schedule_provider("primary", now, cutoff),
                schedule_provider("secondary", now, cutoff),
                year_month=(year, month),
                second_delay=0,
                now=now,
            )
            self.assertFalse(outcome.failed)
            self.assertEqual(outcome.window_start, start)
            self.assertEqual(outcome.window_end, cutoff)

    def test_latest_month_before_cutoff_is_previous_month(self):
        before = int(datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc).timestamp())
        at = int(datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(latest_eligible_uspce_month(before), (2026, 7))
        self.assertEqual(latest_eligible_uspce_month(at), (2026, 8))


if __name__ == "__main__":
    unittest.main()
