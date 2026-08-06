"""M9-M11 scheduled absence checks with two-provider agreement."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hashlib
import time

from eth_abi import decode, encode

from .constants import (
    AMPL_USD_QUERY_ID,
    ETH_USD_QUERY_ID,
    TELLOR_FLEX,
    USPCE_QUERY_ID,
)
from .ethereum import call_data, hx, unhex
from .models import ChainPoint, Finding, Unresolved


CONFIRMATIONS = 12
MAX_HEAD_AGE_SECONDS = 600
MAX_FUTURE_SECONDS = 60
ETH_MAX_AGE_SECONDS = 14_400


@dataclass(frozen=True)
class DataBeforeObservation:
    block_number: int
    block_hash: str
    block_timestamp: int
    found: bool
    value: bytes
    report_timestamp: int
    primary_name: str
    secondary_name: str
    primary_result_sha256: str
    secondary_result_sha256: str

    def evidence(self):
        return {
            "confirmed_block_number": self.block_number,
            "confirmed_block_hash": self.block_hash,
            "confirmed_block_timestamp": self.block_timestamp,
            "found": self.found,
            "report_timestamp": self.report_timestamp if self.found else None,
            "providers": [self.primary_name, self.secondary_name],
            "raw_call_result_sha256": [
                self.primary_result_sha256,
                self.secondary_result_sha256,
            ],
        }


@dataclass(frozen=True)
class ScheduledOutcome:
    slug: str
    period: str
    failed: bool
    observation: DataBeforeObservation
    finding: Finding | None
    query_id: str
    window_start: int | None = None
    window_end: int | None = None

    def local_evidence(self):
        value = self.observation.evidence()
        value.update(
            {
                "period": self.period,
                "query_id": self.query_id,
                "window_start": self.window_start,
                "window_end": self.window_end,
                "failed": self.failed,
            }
        )
        return value


def evaluate_eth_usd(
    primary,
    secondary,
    *,
    second_delay=30,
    sleep=time.sleep,
    now=None,
):
    moment = int(time.time() if now is None else now)
    primary_head = _healthy_head(primary, moment)
    if primary_head["number"] < CONFIRMATIONS:
        raise Unresolved("Ethereum head is below the confirmation depth")
    block_number = primary_head["number"] - CONFIRMATIONS
    first = _read_data_before(
        primary,
        block_number,
        ETH_USD_QUERY_ID,
        None,
        moment,
    )
    query_before = first["block_timestamp"] + 1
    first = _read_data_before(
        primary,
        block_number,
        ETH_USD_QUERY_ID,
        query_before,
        moment,
    )
    if second_delay:
        sleep(second_delay)
    secondary_head = _healthy_head(secondary, moment)
    if secondary_head["number"] - block_number < CONFIRMATIONS:
        raise Unresolved("secondary provider has not confirmed the selected block")
    second = _read_data_before(
        secondary,
        block_number,
        ETH_USD_QUERY_ID,
        query_before,
        moment,
    )
    observation = _agree(first, second, primary, secondary)
    if observation.found and observation.report_timestamp > observation.block_timestamp:
        raise Unresolved("getDataBefore returned a future report")
    age = (
        observation.block_timestamp - observation.report_timestamp
        if observation.found
        else None
    )
    failed = not observation.found or age > ETH_MAX_AGE_SECONDS
    finding = None
    if failed:
        evidence = observation.evidence()
        evidence.update(
            {
                "query_id": ETH_USD_QUERY_ID,
                "computed_age_seconds": age if age is not None else "unbounded",
                "threshold": ">14400",
            }
        )
        point = _ethereum_point(observation)
        finding = Finding(
            slug="tellorflex-eth-usd-freshness",
            predicate="latest confirmed undisputed ETH/USD report age is greater than 14,400 seconds",
            expected={"found": True, "age_seconds": "<=14400"},
            observed={
                "found": observation.found,
                "last_report_timestamp": (
                    observation.report_timestamp if observation.found else None
                ),
                "age_seconds": age if age is not None else "unbounded",
            },
            incident_key="tellorflex-freshness:eth-usd:{}".format(
                observation.block_hash
            ),
            delivery_key="1:M9:{}".format(observation.block_hash),
            chain_point=point,
            signal="scheduled getDataBefore(bytes32,uint256)",
            contract=TELLOR_FLEX,
            evidence=evidence,
        )
    return ScheduledOutcome(
        slug="tellorflex-eth-usd-freshness",
        period=observation.block_hash,
        failed=failed,
        observation=observation,
        finding=finding,
        query_id=ETH_USD_QUERY_ID,
    )


def evaluate_ampl_day(
    primary,
    secondary,
    *,
    day=None,
    second_delay=30,
    sleep=time.sleep,
    now=None,
):
    moment = int(time.time() if now is None else now)
    target_day = day or latest_eligible_ampl_day(moment)
    start = _utc_timestamp(target_day, datetime_time(0, 0, 0))
    cutoff = _utc_timestamp(target_day, datetime_time(0, 30, 0))
    return _evaluate_window(
        primary,
        secondary,
        slug="tellorflex-ampl-usd-deadline",
        monitor_id="M10",
        query_id=AMPL_USD_QUERY_ID,
        period=target_day.isoformat(),
        start=start,
        cutoff=cutoff,
        second_delay=second_delay,
        sleep=sleep,
        now=moment,
    )


def evaluate_uspce_month(
    primary,
    secondary,
    *,
    year_month=None,
    second_delay=30,
    sleep=time.sleep,
    now=None,
):
    moment = int(time.time() if now is None else now)
    year, month = year_month or latest_eligible_uspce_month(moment)
    final_day = calendar.monthrange(year, month)[1]
    start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
    cutoff = int(datetime(year, month, final_day, tzinfo=timezone.utc).timestamp())
    return _evaluate_window(
        primary,
        secondary,
        slug="tellorflex-uspce-deadline",
        monitor_id="M11",
        query_id=USPCE_QUERY_ID,
        period="{:04d}-{:02d}".format(year, month),
        start=start,
        cutoff=cutoff,
        second_delay=second_delay,
        sleep=sleep,
        now=moment,
    )


def latest_eligible_ampl_day(now):
    moment = datetime.fromtimestamp(int(now), timezone.utc)
    cutoff = datetime.combine(moment.date(), datetime_time(0, 30), timezone.utc)
    return moment.date() if moment >= cutoff else moment.date() - timedelta(days=1)


def latest_eligible_uspce_month(now):
    moment = datetime.fromtimestamp(int(now), timezone.utc)
    final_day = calendar.monthrange(moment.year, moment.month)[1]
    cutoff = datetime(moment.year, moment.month, final_day, tzinfo=timezone.utc)
    if moment >= cutoff:
        return moment.year, moment.month
    if moment.month == 1:
        return moment.year - 1, 12
    return moment.year, moment.month - 1


def _evaluate_window(
    primary,
    secondary,
    *,
    slug,
    monitor_id,
    query_id,
    period,
    start,
    cutoff,
    second_delay,
    sleep,
    now,
):
    primary_head = _healthy_head(primary, now)
    confirmed = primary_head["number"] - CONFIRMATIONS
    if confirmed < 0:
        raise Unresolved("Ethereum head is below the confirmation depth")
    confirmed_block = primary.block(confirmed)
    if _block_timestamp(confirmed_block) < cutoff:
        return None
    block_number = _first_block_at_or_after(primary, cutoff, confirmed)
    first = _read_data_before(
        primary, block_number, query_id, cutoff + 1, now
    )
    if second_delay:
        sleep(second_delay)
    secondary_head = _healthy_head(secondary, now)
    if secondary_head["number"] - block_number < CONFIRMATIONS:
        raise Unresolved("secondary provider has not confirmed the cutoff block")
    second = _read_data_before(
        secondary, block_number, query_id, cutoff + 1, now
    )
    observation = _agree(first, second, primary, secondary)
    if observation.block_timestamp < cutoff:
        raise Unresolved("selected cutoff block precedes the deadline")
    found_in_window = (
        observation.found
        and start <= observation.report_timestamp <= cutoff
    )
    failed = not found_in_window
    finding = None
    if failed:
        evidence = observation.evidence()
        evidence.update(
            {
                "query_id": query_id,
                "period": period,
                "window_start": start,
                "window_end": cutoff,
            }
        )
        label = "AMPL/USD daily" if monitor_id == "M10" else "USPCE monthly"
        finding = Finding(
            slug=slug,
            predicate="no confirmed undisputed {} report exists in the required window".format(
                label
            ),
            expected={"report_timestamp": [{"inclusive_min": start}, {"inclusive_max": cutoff}]},
            observed={
                "found": observation.found,
                "report_timestamp": (
                    observation.report_timestamp if observation.found else None
                ),
                "period": period,
            },
            incident_key=(
                "tellorflex-deadline:ampl-usd:{}".format(period)
                if monitor_id == "M10"
                else "tellorflex-deadline:uspce:{}".format(period)
            ),
            delivery_key="1:{}:{}".format(monitor_id, period),
            chain_point=_ethereum_point(observation),
            signal="scheduled getDataBefore(bytes32,uint256)",
            contract=TELLOR_FLEX,
            evidence=evidence,
        )
    return ScheduledOutcome(
        slug=slug,
        period=period,
        failed=failed,
        observation=observation,
        finding=finding,
        query_id=query_id,
        window_start=start,
        window_end=cutoff,
    )


def _healthy_head(client, now):
    if client.chain_id() != 1:
        raise Unresolved("Ethereum provider is not on chain ID 1")
    number = client.head_number()
    block = client.block(number)
    if _block_number(block) != number:
        raise Unresolved("Ethereum provider head identity differs")
    timestamp = _block_timestamp(block)
    if timestamp > int(now) + MAX_FUTURE_SECONDS:
        raise Unresolved("Ethereum provider head is in the future")
    if timestamp < int(now) - MAX_HEAD_AGE_SECONDS:
        raise Unresolved("Ethereum provider head is stale")
    return {"number": number, "timestamp": timestamp, "hash": _block_hash(block)}


def _first_block_at_or_after(client, cutoff, high):
    low = 0
    while low < high:
        middle = (low + high) // 2
        if _block_timestamp(client.block(middle)) < cutoff:
            low = middle + 1
        else:
            high = middle
    selected = client.block(low)
    if _block_timestamp(selected) < cutoff:
        raise Unresolved("no confirmed cutoff block exists")
    if low > 0 and _block_timestamp(client.block(low - 1)) >= cutoff:
        raise Unresolved("cutoff block binary search did not find the earliest block")
    return low


def _read_data_before(client, block_number, query_id, before_timestamp, now):
    block = client.block(block_number)
    if _block_number(block) != int(block_number):
        raise Unresolved("Ethereum block number differs from request")
    block_hash = _block_hash(block)
    by_hash = client.block_by_hash(block_hash)
    if (
        _block_number(by_hash) != int(block_number)
        or _block_hash(by_hash) != block_hash
        or _block_timestamp(by_hash) != _block_timestamp(block)
    ):
        raise Unresolved("Ethereum block number/hash reads disagree")
    result = None
    decoded = None
    if before_timestamp is not None:
        result = client.eth_call(
            TELLOR_FLEX,
            call_data(
                "getDataBefore(bytes32,uint256)",
                ["bytes32", "uint256"],
                [unhex(query_id), int(before_timestamp)],
            ),
            block_number,
        )
        decoded = _decode_data_before(result, int(before_timestamp))
    return {
        "block_number": int(block_number),
        "block_hash": block_hash,
        "block_timestamp": _block_timestamp(block),
        "raw_result": result,
        "decoded": decoded,
    }


def _decode_data_before(value, before_timestamp):
    raw = unhex(value)
    try:
        decoded = decode(["bool", "bytes", "uint256"], raw, strict=True)
        if encode(["bool", "bytes", "uint256"], decoded) != raw:
            raise ValueError("non-canonical return")
    except Exception as error:
        raise Unresolved("getDataBefore result is not canonical ABI") from error
    found, report_value, report_timestamp = decoded
    report_timestamp = int(report_timestamp)
    if not found and (report_value or report_timestamp != 0):
        raise Unresolved("getDataBefore absent result has nonzero fields")
    if found and (report_timestamp <= 0 or report_timestamp >= before_timestamp):
        raise Unresolved("getDataBefore report timestamp violates strict-before semantics")
    return bool(found), bytes(report_value), report_timestamp


def _agree(first, second, primary, secondary):
    block_fields = ("block_number", "block_hash", "block_timestamp")
    if any(first[field] != second[field] for field in block_fields):
        raise Unresolved("Ethereum providers disagree on the selected block")
    if first["decoded"] != second["decoded"]:
        raise Unresolved("Ethereum providers disagree on getDataBefore")
    if first["decoded"] is None or first["raw_result"] is None:
        raise Unresolved("scheduled call result is missing")
    found, value, report_timestamp = first["decoded"]
    return DataBeforeObservation(
        block_number=first["block_number"],
        block_hash=first["block_hash"],
        block_timestamp=first["block_timestamp"],
        found=found,
        value=value,
        report_timestamp=report_timestamp,
        primary_name=primary.name,
        secondary_name=secondary.name,
        primary_result_sha256=_result_hash(first["raw_result"]),
        secondary_result_sha256=_result_hash(second["raw_result"]),
    )


def _result_hash(value):
    return "0x" + hashlib.sha256(unhex(value)).hexdigest()


def _block_number(block):
    try:
        return int(block["number"], 16)
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("Ethereum block number is invalid") from error


def _block_timestamp(block):
    try:
        return int(block["timestamp"], 16)
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("Ethereum block timestamp is invalid") from error


def _block_hash(block):
    value = str(block.get("hash", "")).lower()
    try:
        if len(value) != 66:
            raise ValueError
        int(value[2:], 16)
    except ValueError as error:
        raise Unresolved("Ethereum block hash is invalid") from error
    return value


def _ethereum_point(observation):
    return ChainPoint(
        "ethereum",
        observation.block_number,
        observation.block_hash,
        observation.block_timestamp,
    )


def _utc_timestamp(day, clock):
    return int(datetime.combine(day, clock, timezone.utc).timestamp())
