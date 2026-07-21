#!/usr/bin/env python3
"""Confirmed Tellor report-freshness checks for the tellor-ops host.

The contract-read behavior is pinned to tellorFlex commit
e2946ecc12b22e72e63bec6f298d31ff22967d5c. In that source,
``getDataBefore(bytes32,uint256)`` is strictly before its timestamp argument
and ``getIndexForDataBefore`` walks backward over disputed reports. Therefore
inclusive policy deadlines are queried as ``deadline + 1``.

This module intentionally uses only the Python standard library. The host
service and the OpenZeppelin Monitor image do not share a package environment.
"""

import argparse
import calendar
import copy
import json
import logging
import os
import re
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


UTC = timezone.utc

TELLORFLEX_ADDRESS = "0x8cFc184c877154a8F9ffE0fe75649dbe5e2DBEbf"
GET_DATA_BEFORE_SIGNATURE = "getDataBefore(bytes32,uint256)"
GET_DATA_BEFORE_SELECTOR = "a792765f"
TELLORFLEX_SOURCE_COMMIT = "e2946ecc12b22e72e63bec6f298d31ff22967d5c"

ETH_QUERY_ID = "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992"
AMPL_QUERY_ID = "0x0d12ad49193163bbbeff4e6db8294ced23ff8605359fd666799d4e25a3aa0e3a"
USPCE_QUERY_ID = "0x612ec1d9cee860bb87deb6370ed0ae43345c9302c085c1dfc4c207cbec2970d7"

ETH_ROUTE = "Tellor ETH/USD Freshness"
AMPL_ROUTE = "Tellor AMPL/USD Freshness"
USPCE_ROUTE = "Tellor USPCE Freshness"
HEALTH_ROUTE = "Tellor Freshness Checker Health"

RPC_ENV_NAMES = (
    "RPC_ETHEREUM_MAINNET",
    "RPC_ETHEREUM_MAINNET_FALLBACK_BLOCKPI",
    "RPC_ETHEREUM_MAINNET_FALLBACK_DRPC",
)

STATE_PATH = Path("/var/lib/tellor/report-freshness/state.json")
HEARTBEAT_PATH = Path("/var/lib/tellor/report-freshness/heartbeat")
ALERT_LOG_PATH = Path("/var/lib/tellor/report-freshness/alerts.log")

CONFIRMATION_BLOCKS = 12
ETH_MAX_AGE_SECONDS = 14_400
MAX_CONFIRMED_HEAD_AGE_SECONDS = 900
MAX_FUTURE_BLOCK_SKEW_SECONDS = 60
RPC_FAILURE_ALERT_THRESHOLD = 3
STATE_VERSION = 1

QUERY_ID_RE = re.compile(r"0x[0-9a-fA-F]{64}\Z")
HEX_DATA_RE = re.compile(r"0x(?:[0-9a-fA-F]{2})*\Z")
QUANTITY_RE = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MONTH_RE = re.compile(r"\d{4}-\d{2}\Z")


class FreshnessError(RuntimeError):
    """Base error for deterministic checker failures."""


class RpcError(FreshnessError):
    """A provider did not return a complete, valid Ethereum response."""


class AllProvidersFailed(FreshnessError):
    """Every configured provider failed the complete snapshot operation."""


class StateError(FreshnessError):
    """The durable state cannot be handled safely."""


class DeliveryTransitionError(FreshnessError):
    """One or more alert state transitions remain pending for retry."""


@dataclass(frozen=True)
class Block:
    number: int
    timestamp: int
    block_hash: str


@dataclass(frozen=True)
class Report:
    found: bool
    timestamp: int


@dataclass(frozen=True)
class PeriodObservation:
    period: str
    window_start: int
    cutoff: int
    cutoff_block: Block
    report: Report

    @property
    def has_report_in_window(self) -> bool:
        return self.report.found and self.window_start <= self.report.timestamp <= self.cutoff


@dataclass(frozen=True)
class Snapshot:
    provider_name: str
    head_number: int
    confirmed_block: Block
    eth_report: Report
    ampl: Optional[PeriodObservation]
    uspce: Optional[PeriodObservation]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_timestamp(timestamp: int) -> str:
    return _utc_iso(datetime.fromtimestamp(timestamp, UTC))


def _parse_quantity(value: Any, label: str) -> int:
    if not isinstance(value, str) or not QUANTITY_RE.fullmatch(value):
        raise RpcError("{} is not a canonical JSON-RPC quantity".format(label))
    return int(value, 16)


def _validate_rpc_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as error:
        raise RpcError("RPC URL is invalid") from error
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.fragment:
        raise RpcError("RPC URL must be an HTTP(S) endpoint without a fragment")
    return value


def _default_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "eth": {"incident_open": False},
        "ampl": {"last_period": None},
        "uspce": {"last_period": None},
        "rpc": {"consecutive_failures": 0, "incident_open": False},
    }


def _validate_date(value: Optional[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("invalid date period")
    date.fromisoformat(value)


def _validate_month(value: Optional[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise ValueError("invalid month period")
    year, month = (int(item) for item in value.split("-"))
    if year < 1 or not 1 <= month <= 12:
        raise ValueError("invalid month period")


def _validated_state(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "eth", "ampl", "uspce", "rpc"}:
        raise ValueError("state object has unexpected keys")
    if value.get("version") != STATE_VERSION:
        raise ValueError("unsupported state version")

    eth = value.get("eth")
    ampl = value.get("ampl")
    uspce = value.get("uspce")
    rpc = value.get("rpc")
    if not isinstance(eth, dict) or set(eth) != {"incident_open"}:
        raise ValueError("invalid ETH state")
    if not isinstance(eth["incident_open"], bool):
        raise ValueError("invalid ETH incident state")
    if not isinstance(ampl, dict) or set(ampl) != {"last_period"}:
        raise ValueError("invalid AMPL state")
    if not isinstance(uspce, dict) or set(uspce) != {"last_period"}:
        raise ValueError("invalid USPCE state")
    _validate_date(ampl["last_period"])
    _validate_month(uspce["last_period"])
    if not isinstance(rpc, dict) or set(rpc) != {"consecutive_failures", "incident_open"}:
        raise ValueError("invalid RPC state")
    failures = rpc["consecutive_failures"]
    if isinstance(failures, bool) or not isinstance(failures, int) or not 0 <= failures <= RPC_FAILURE_ALERT_THRESHOLD:
        raise ValueError("invalid RPC failure count")
    if not isinstance(rpc["incident_open"], bool):
        raise ValueError("invalid RPC incident state")
    return copy.deepcopy(value)


def _atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = None
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.tmp.".format(path.name), dir=str(path.parent)
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


class StateStore:
    """Crash-safe JSON state with preservation of any corrupt prior file."""

    def __init__(self, path: Path, now: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.now = now
        self.bootstrapped = False
        self.preserved_corrupt_path = None  # type: Optional[Path]

    def load(self) -> Dict[str, Any]:
        self.bootstrapped = False
        self.preserved_corrupt_path = None
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            self.bootstrapped = True
            return _default_state()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StateError("state path must be a regular non-symlink file")
        try:
            raw = self.path.read_text(encoding="utf-8")
            return _validated_state(json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            stamp = self.now().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            preserved = self.path.with_name(
                "{}.corrupt.{}.{}".format(self.path.name, stamp, os.getpid())
            )
            try:
                os.replace(str(self.path), str(preserved))
            except OSError as preserve_error:
                raise StateError("corrupt state could not be preserved") from preserve_error
            self.bootstrapped = True
            self.preserved_corrupt_path = preserved
            logging.error("preserved invalid freshness state as %s", preserved.name)
            return _default_state()

    def save(self, state_value: Mapping[str, Any]) -> None:
        state = _validated_state(dict(state_value))
        content = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            _atomic_write_bytes(self.path, content)
        except OSError as error:
            raise StateError("could not atomically replace freshness state") from error


class HttpRpcProvider:
    """Small, secret-redacting Ethereum JSON-RPC client."""

    def __init__(self, name: str, url: str, timeout: float = 8.0):
        self.name = name
        self._url = _validate_rpc_url(url)
        self.timeout = timeout
        self._request_id = 0
        self._block_cache = {}  # type: Dict[int, Block]

    def _call(self, method: str, params: Sequence[Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": list(params)}
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "tellor-freshness/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except Exception as error:
            raise RpcError("{} request failed".format(self.name)) from error
        if len(raw) > 2 * 1024 * 1024:
            raise RpcError("{} returned an oversized response".format(self.name))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RpcError("{} returned invalid JSON".format(self.name)) from error
        if not isinstance(payload, dict) or payload.get("id") != request_id:
            raise RpcError("{} returned a mismatched JSON-RPC response".format(self.name))
        if payload.get("error") is not None or "result" not in payload:
            raise RpcError("{} returned a JSON-RPC error".format(self.name))
        return payload["result"]

    def chain_id(self) -> int:
        return _parse_quantity(self._call("eth_chainId", []), "chain ID")

    def block_number(self) -> int:
        return _parse_quantity(self._call("eth_blockNumber", []), "head block")

    def block(self, number: int) -> Block:
        if number in self._block_cache:
            return self._block_cache[number]
        payload = self._call("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(payload, dict):
            raise RpcError("{} did not return block {}".format(self.name, number))
        actual_number = _parse_quantity(payload.get("number"), "block number")
        timestamp = _parse_quantity(payload.get("timestamp"), "block timestamp")
        block_hash = payload.get("hash")
        if actual_number != number:
            raise RpcError("{} returned the wrong block".format(self.name))
        if not isinstance(block_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash):
            raise RpcError("{} returned an invalid block hash".format(self.name))
        result = Block(number=number, timestamp=timestamp, block_hash=block_hash.lower())
        self._block_cache[number] = result
        return result

    def get_data_before(self, query_id: str, before_timestamp: int, block_number: int) -> Report:
        if not QUERY_ID_RE.fullmatch(query_id):
            raise ValueError("query ID must be bytes32 hex")
        if before_timestamp < 0 or before_timestamp >= 2 ** 256:
            raise ValueError("query timestamp is outside uint256")
        calldata = "0x{}{}{:064x}".format(
            GET_DATA_BEFORE_SELECTOR, query_id.removeprefix("0x"), before_timestamp
        )
        result = self._call(
            "eth_call",
            [{"to": TELLORFLEX_ADDRESS, "data": calldata}, hex(block_number)],
        )
        return decode_get_data_before(result, before_timestamp)


class InvalidRpcProvider:
    """Keep an invalid configured endpoint in the health/fallback state machine."""

    def __init__(self, name: str):
        self.name = name

    def chain_id(self) -> int:
        raise RpcError("{} RPC URL is invalid".format(self.name))


def decode_get_data_before(result: Any, before_timestamp: int) -> Report:
    if not isinstance(result, str) or not HEX_DATA_RE.fullmatch(result):
        raise RpcError("getDataBefore returned invalid hex data")
    raw = bytes.fromhex(result[2:])
    if len(raw) < 128 or len(raw) % 32:
        raise RpcError("getDataBefore returned malformed ABI data")
    found_word = int.from_bytes(raw[0:32], "big")
    offset = int.from_bytes(raw[32:64], "big")
    report_timestamp = int.from_bytes(raw[64:96], "big")
    if found_word not in (0, 1):
        raise RpcError("getDataBefore returned a non-canonical bool")
    if offset != 96 or offset + 32 > len(raw):
        raise RpcError("getDataBefore returned an invalid bytes offset")
    value_length = int.from_bytes(raw[offset:offset + 32], "big")
    value_end = offset + 32 + value_length
    padded_end = ((value_end + 31) // 32) * 32
    if padded_end != len(raw) or any(raw[value_end:padded_end]):
        raise RpcError("getDataBefore returned invalid dynamic bytes")
    found = found_word == 1
    if not found and (report_timestamp != 0 or value_length != 0):
        raise RpcError("getDataBefore returned inconsistent missing data")
    if found and (report_timestamp == 0 or value_length == 0):
        raise RpcError("getDataBefore returned inconsistent present data")
    if found and report_timestamp >= before_timestamp:
        raise RpcError("getDataBefore violated strict-before semantics")
    return Report(found=found, timestamp=report_timestamp)


def configured_providers(environment: Mapping[str, str] = os.environ) -> List[Any]:
    providers = []
    for name in RPC_ENV_NAMES:
        value = environment.get(name, "").strip()
        if value:
            try:
                providers.append(HttpRpcProvider(name=name, url=value))
            except RpcError:
                providers.append(InvalidRpcProvider(name=name))
    # An empty registry is still a complete-check failure.  Let it enter the
    # same durable health state machine as configured providers that all fail.
    return providers


def first_block_at_or_after(provider: Any, cutoff: int, upper_height: int) -> Block:
    """Resolve the earliest block whose timestamp is at least ``cutoff``."""
    upper = provider.block(upper_height)
    if upper.timestamp < cutoff:
        raise RpcError("cutoff block is not yet confirmed")
    low = 0
    high = upper_height
    while low < high:
        middle = (low + high) // 2
        if provider.block(middle).timestamp >= cutoff:
            high = middle
        else:
            low = middle + 1
    found = provider.block(low)
    if found.timestamp < cutoff:
        raise RpcError("could not resolve cutoff block")
    if low > 0 and provider.block(low - 1).timestamp >= cutoff:
        raise RpcError("provider returned non-monotonic cutoff blocks")
    return found


def _at_utc(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime.combine(day, datetime_time(hour=hour, minute=minute, tzinfo=UTC))


def latest_ampl_period(confirmed_timestamp: int) -> date:
    instant = datetime.fromtimestamp(confirmed_timestamp, UTC)
    today = instant.date()
    if instant >= _at_utc(today, minute=30):
        return today
    return today - timedelta(days=1)


def _previous_month(year: int, month: int) -> Tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def uspce_period_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    final_day = calendar.monthrange(year, month)[1]
    return (
        datetime(year, month, 1, tzinfo=UTC),
        datetime(year, month, final_day, tzinfo=UTC),
    )


def latest_uspce_period(confirmed_timestamp: int) -> Tuple[int, int]:
    instant = datetime.fromtimestamp(confirmed_timestamp, UTC)
    year, month = instant.year, instant.month
    _, cutoff = uspce_period_bounds(year, month)
    if instant >= cutoff:
        return year, month
    return _previous_month(year, month)


def _period_is_new(last_period: Optional[str], period: str) -> bool:
    return last_period is None or period > last_period


class SnapshotCollector:
    def __init__(
        self,
        provider: Any,
        state: Mapping[str, Any],
        now: datetime,
        confirmations: int = CONFIRMATION_BLOCKS,
        max_head_age: int = MAX_CONFIRMED_HEAD_AGE_SECONDS,
    ):
        self.provider = provider
        self.state = state
        self.now = now.astimezone(UTC)
        self.confirmations = confirmations
        self.max_head_age = max_head_age

    def collect(self) -> Snapshot:
        if self.provider.chain_id() != 1:
            raise RpcError("{} is not Ethereum mainnet".format(self.provider.name))
        head_number = self.provider.block_number()
        if head_number < self.confirmations:
            raise RpcError("{} has no confirmed head".format(self.provider.name))
        confirmed_height = head_number - self.confirmations
        confirmed = self.provider.block(confirmed_height)
        wall_timestamp = int(self.now.timestamp())
        age = wall_timestamp - confirmed.timestamp
        if age > self.max_head_age:
            raise RpcError("{} confirmed head is stale".format(self.provider.name))
        if age < -MAX_FUTURE_BLOCK_SKEW_SECONDS:
            raise RpcError("{} confirmed head is in the future".format(self.provider.name))

        eth_report = self.provider.get_data_before(
            ETH_QUERY_ID, confirmed.timestamp + 1, confirmed.number
        )
        ampl = self._ampl_observation(confirmed, head_number)
        uspce = self._uspce_observation(confirmed, head_number)
        return Snapshot(
            provider_name=self.provider.name,
            head_number=head_number,
            confirmed_block=confirmed,
            eth_report=eth_report,
            ampl=ampl,
            uspce=uspce,
        )

    def _ampl_observation(self, confirmed: Block, head_number: int) -> Optional[PeriodObservation]:
        period_day = latest_ampl_period(confirmed.timestamp)
        period = period_day.isoformat()
        if not _period_is_new(self.state["ampl"]["last_period"], period):
            return None
        start = _at_utc(period_day)
        cutoff = _at_utc(period_day, minute=30)
        return self._period_observation(
            AMPL_QUERY_ID, period, start, cutoff, confirmed.number, head_number
        )

    def _uspce_observation(self, confirmed: Block, head_number: int) -> Optional[PeriodObservation]:
        year, month = latest_uspce_period(confirmed.timestamp)
        period = "{:04d}-{:02d}".format(year, month)
        if not _period_is_new(self.state["uspce"]["last_period"], period):
            return None
        start, cutoff = uspce_period_bounds(year, month)
        return self._period_observation(
            USPCE_QUERY_ID, period, start, cutoff, confirmed.number, head_number
        )

    def _period_observation(
        self,
        query_id: str,
        period: str,
        start: datetime,
        cutoff: datetime,
        confirmed_height: int,
        head_number: int,
    ) -> PeriodObservation:
        cutoff_timestamp = int(cutoff.timestamp())
        cutoff_block = first_block_at_or_after(
            self.provider, cutoff_timestamp, confirmed_height
        )
        if cutoff_block.number + self.confirmations > head_number:
            raise RpcError("cutoff block does not have the required confirmations")
        report = self.provider.get_data_before(
            query_id, cutoff_timestamp + 1, confirmed_height
        )
        return PeriodObservation(
            period=period,
            window_start=int(start.timestamp()),
            cutoff=cutoff_timestamp,
            cutoff_block=cutoff_block,
            report=report,
        )


def collect_with_fallback(
    providers: Iterable[Any],
    state: Mapping[str, Any],
    now: datetime,
    max_head_age: int = MAX_CONFIRMED_HEAD_AGE_SECONDS,
) -> Tuple[Snapshot, int]:
    attempted = 0
    for provider in providers:
        attempted += 1
        try:
            return SnapshotCollector(provider, state, now, max_head_age=max_head_age).collect(), attempted
        except RpcError as error:
            logging.warning("RPC provider %s failed a complete check: %s", provider.name, error)
        except Exception as error:
            logging.warning(
                "RPC provider %s failed a complete check with %s",
                getattr(provider, "name", "unnamed"),
                error.__class__.__name__,
            )
    if attempted == 0:
        raise AllProvidersFailed("no RPC providers are configured")
    raise AllProvidersFailed("all {} configured RPC providers failed".format(attempted))


class FreshnessChecker:
    def __init__(
        self,
        providers: Iterable[Any],
        store: StateStore,
        deliver: Callable[..., Any],
        delivery_mode: str,
        heartbeat_path: Path = HEARTBEAT_PATH,
        alert_log_path: Path = ALERT_LOG_PATH,
        now: Callable[[], datetime] = utc_now,
        max_head_age: int = MAX_CONFIRMED_HEAD_AGE_SECONDS,
    ):
        if delivery_mode not in ("live", "log-only"):
            raise ValueError("delivery mode must be live or log-only")
        self.providers = list(providers)
        self.store = store
        self.deliver = deliver
        self.delivery_mode = delivery_mode
        self.heartbeat_path = Path(heartbeat_path)
        self.alert_log_path = Path(alert_log_path)
        self.now = now
        self.max_head_age = max_head_age

    def run(self) -> Snapshot:
        instant = self.now().astimezone(UTC)
        state = self.store.load()
        try:
            snapshot, _attempted = collect_with_fallback(
                self.providers, state, instant, max_head_age=self.max_head_age
            )
        except AllProvidersFailed:
            status = "rpc-failure"
            try:
                self._record_rpc_failure(state, len(self.providers), instant)
            except Exception:
                status = "delivery-failure"
                self._heartbeat(instant, status, None)
                raise
            self._heartbeat(instant, status, None)
            raise

        errors = []  # type: List[Exception]
        self._attempt_transition(errors, self._record_rpc_success, state, snapshot, instant)
        self._attempt_transition(errors, self._evaluate_eth, state, snapshot, instant)
        if snapshot.ampl is not None:
            self._attempt_transition(errors, self._evaluate_ampl, state, snapshot.ampl, instant)
        if snapshot.uspce is not None:
            self._attempt_transition(errors, self._evaluate_uspce, state, snapshot.uspce, instant)
        self._heartbeat(instant, "delivery-failure" if errors else "ok", snapshot)
        if errors:
            raise DeliveryTransitionError(
                "{} alert transition(s) remain pending".format(len(errors))
            )
        return snapshot

    @staticmethod
    def _attempt_transition(errors: List[Exception], operation: Callable[..., None], *args: Any) -> None:
        try:
            operation(*args)
        except Exception as error:
            errors.append(error)
            logging.error("freshness transition remains pending: %s", error.__class__.__name__)

    def _commit(self, state: Dict[str, Any], mutation: Callable[[Dict[str, Any]], None]) -> None:
        candidate = copy.deepcopy(state)
        mutation(candidate)
        self.store.save(candidate)
        state.clear()
        state.update(candidate)

    def _send(self, route: str, content: str, status: str) -> None:
        self.deliver(
            route,
            content,
            mode=self.delivery_mode,
            log_path=self.alert_log_path,
            context={"source": "report-freshness", "status": status},
        )

    def _record_rpc_failure(self, state: Dict[str, Any], attempted: int, instant: datetime) -> None:
        failures = min(state["rpc"]["consecutive_failures"] + 1, RPC_FAILURE_ALERT_THRESHOLD)
        if failures >= RPC_FAILURE_ALERT_THRESHOLD and not state["rpc"]["incident_open"]:
            content = "\n".join((
                "⚠️ **Tellor freshness checker RPC unavailable**",
                "> Consecutive complete failures: `{}`".format(failures),
                "> Providers attempted: `{}`".format(attempted),
                "> Missing-report alarms are suppressed until a complete RPC check succeeds.",
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(HEALTH_ROUTE, content, "alarm")
            self._commit(
                state,
                lambda candidate: candidate["rpc"].update(
                    {"consecutive_failures": failures, "incident_open": True}
                ),
            )
            return
        self._commit(
            state,
            lambda candidate: candidate["rpc"].update({"consecutive_failures": failures}),
        )

    def _record_rpc_success(self, state: Dict[str, Any], snapshot: Snapshot, instant: datetime) -> None:
        rpc_state = state["rpc"]
        if rpc_state["incident_open"]:
            content = "\n".join((
                "✅ **Tellor freshness checker RPC recovered**",
                "> Confirmed block: `{}`".format(snapshot.confirmed_block.number),
                "> Provider: `{}`".format(snapshot.provider_name),
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(HEALTH_ROUTE, content, "recovery")
        if rpc_state["incident_open"] or rpc_state["consecutive_failures"]:
            self._commit(
                state,
                lambda candidate: candidate["rpc"].update(
                    {"consecutive_failures": 0, "incident_open": False}
                ),
            )

    def _evaluate_eth(self, state: Dict[str, Any], snapshot: Snapshot, instant: datetime) -> None:
        report = snapshot.eth_report
        age = snapshot.confirmed_block.timestamp - report.timestamp if report.found else None
        stale = age is None or age > ETH_MAX_AGE_SECONDS
        incident_open = state["eth"]["incident_open"]
        if stale and not incident_open:
            last_report = _format_timestamp(report.timestamp) if report.found else "none"
            age_text = str(age) if age is not None else "unavailable"
            content = "\n".join((
                "🚨 **Tellor ETH/USD report is stale**",
                "> Last undisputed report: `{}`".format(last_report),
                "> Confirmed report age: `{}` seconds".format(age_text),
                "> Threshold: strictly greater than `14400` seconds",
                "> Confirmed block: `{}`".format(snapshot.confirmed_block.number),
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(ETH_ROUTE, content, "alarm")
            self._commit(state, lambda candidate: candidate["eth"].update({"incident_open": True}))
        elif not stale and incident_open:
            content = "\n".join((
                "✅ **Tellor ETH/USD report freshness recovered**",
                "> Latest undisputed report: `{}`".format(_format_timestamp(report.timestamp)),
                "> Confirmed report age: `{}` seconds".format(age),
                "> Confirmed block: `{}`".format(snapshot.confirmed_block.number),
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(ETH_ROUTE, content, "recovery")
            self._commit(state, lambda candidate: candidate["eth"].update({"incident_open": False}))

    def _evaluate_ampl(
        self, state: Dict[str, Any], observation: PeriodObservation, instant: datetime
    ) -> None:
        if not observation.has_report_in_window:
            content = "\n".join((
                "🚨 **Tellor AMPL/USD daily report window missed**",
                "> Date: `{}`".format(observation.period),
                "> Required window: `00:00:00–00:30:00 UTC` inclusive",
                "> Cutoff block: `{}` (12-confirmed)".format(observation.cutoff_block.number),
                "> This missed-date result is immutable.",
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(AMPL_ROUTE, content, "alarm")
        self._commit(
            state,
            lambda candidate: candidate["ampl"].update({"last_period": observation.period}),
        )

    def _evaluate_uspce(
        self, state: Dict[str, Any], observation: PeriodObservation, instant: datetime
    ) -> None:
        if not observation.has_report_in_window:
            content = "\n".join((
                "🚨 **Tellor USPCE monthly report deadline missed**",
                "> Month: `{}`".format(observation.period),
                "> Required window: month start through final-day `00:00:00 UTC` inclusive",
                "> Cutoff block: `{}` (12-confirmed)".format(observation.cutoff_block.number),
                "> This missed-month result is immutable.",
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(USPCE_ROUTE, content, "alarm")
        self._commit(
            state,
            lambda candidate: candidate["uspce"].update({"last_period": observation.period}),
        )

    def _heartbeat(self, instant: datetime, status: str, snapshot: Optional[Snapshot]) -> None:
        payload = {
            "ts": _utc_iso(instant),
            "status": status,
            "confirmed_block": snapshot.confirmed_block.number if snapshot else None,
        }
        try:
            _atomic_write_bytes(
                self.heartbeat_path,
                (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            )
        except OSError as error:
            raise StateError("could not atomically replace freshness heartbeat") from error


def routed_deliver(
    monitor_name: str,
    content: str,
    *,
    mode: str,
    log_path: Optional[Path] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> Any:
    scripts_directory = Path(__file__).resolve().parent / "config" / "triggers" / "scripts"
    if str(scripts_directory) not in sys.path:
        sys.path.insert(0, str(scripts_directory))
    try:
        from discord_routes import deliver_alert
    except ImportError as error:
        raise DeliveryTransitionError("discord route module is unavailable") from error
    return deliver_alert(
        monitor_name,
        content,
        mode=mode,
        log_path=log_path,
        context=context,
    )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check confirmed Tellor report freshness")
    parser.add_argument("--state-path", type=Path, default=STATE_PATH)
    parser.add_argument("--heartbeat-path", type=Path, default=HEARTBEAT_PATH)
    parser.add_argument("--alert-log-path", type=Path, default=ALERT_LOG_PATH)
    parser.add_argument(
        "--delivery-mode",
        choices=("live", "log-only"),
        default=os.environ.get("TELLOR_ALERT_DELIVERY_MODE"),
        help="required explicitly or through TELLOR_ALERT_DELIVERY_MODE",
    )
    parser.add_argument(
        "--max-confirmed-head-age",
        type=int,
        default=MAX_CONFIRMED_HEAD_AGE_SECONDS,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.delivery_mode is None:
        parser.error("--delivery-mode or TELLOR_ALERT_DELIVERY_MODE is required")
    if args.max_confirmed_head_age <= 0:
        parser.error("--max-confirmed-head-age must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        checker = FreshnessChecker(
            providers=configured_providers(),
            store=StateStore(args.state_path),
            deliver=routed_deliver,
            delivery_mode=args.delivery_mode,
            heartbeat_path=args.heartbeat_path,
            alert_log_path=args.alert_log_path,
            max_head_age=args.max_confirmed_head_age,
        )
        snapshot = checker.run()
        logging.info(
            "freshness check complete at confirmed block %s via %s",
            snapshot.confirmed_block.number,
            snapshot.provider_name,
        )
        return 0
    except FreshnessError as error:
        logging.error("freshness check failed: %s", error)
        return 1
    except Exception as error:
        logging.error("freshness check failed with %s", error.__class__.__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
