"""Verified M4 checkpoint import and finalized Ethereum deposit backfill."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import stat

from eth_utils import keccak

from .constants import TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2
from .ethereum import EVENT_BY_ADDRESS_TOPIC, hx, normalize_address
from .models import DecodedEvent, Unresolved


EVM_HASH_RE = re.compile(r"0x[0-9a-f]{64}\Z")
LAYER_HASH_RE = re.compile(r"[0-9A-F]{64}\Z")
LAYER_SOURCE_RE = re.compile(r"txs_results:(0|[1-9][0-9]*)\Z")
PENDING_LAYER_TYPES = frozenset({"aggregate_report", "tokens_withdrawn"})
DEPOSIT_SIGNATURE = "Deposit(uint256,address,string,uint256,uint256)"


@dataclass(frozen=True)
class BridgeLedgerSeed:
    digest: str
    ethereum_checkpoint_number: int
    ethereum_checkpoint_hash: str
    layer_checkpoint_height: int
    layer_checkpoint_hash: str
    layer_checkpoint_time_ms: int
    evm_events: tuple
    layer_blocks: tuple
    layer_events: tuple


def read_bridge_ledger_seed(path):
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("M4 bridge-ledger seed cannot be read") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError("M4 bridge-ledger seed must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("M4 bridge-ledger seed permissions are broader than 0600")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("M4 bridge-ledger seed is invalid JSON") from error
    required = {
        "schema_version",
        "ethereum_chain_id",
        "layer_chain_id",
        "source",
        "verified_at",
        "ethereum_checkpoint",
        "layer_checkpoint",
        "pending_ethereum_deposits",
        "pending_layer_events",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("M4 bridge-ledger seed fields are not exact")
    if (
        payload["schema_version"] != 1
        or payload["ethereum_chain_id"] != 1
        or payload["layer_chain_id"] != "tellor-1"
    ):
        raise ValueError("M4 bridge-ledger seed chain identity is invalid")
    if not isinstance(payload["source"], str) or not payload["source"].strip():
        raise ValueError("M4 bridge-ledger seed source is required")
    _verified_time(payload["verified_at"])

    ethereum_checkpoint = _exact_object(
        payload["ethereum_checkpoint"], {"block_number", "block_hash"}, "Ethereum checkpoint"
    )
    ethereum_number = _positive_int(
        ethereum_checkpoint["block_number"], "Ethereum checkpoint block"
    )
    ethereum_hash = _evm_hash(
        ethereum_checkpoint["block_hash"], "Ethereum checkpoint hash"
    )
    layer_checkpoint = _exact_object(
        payload["layer_checkpoint"],
        {"height", "block_hash", "block_time_ms"},
        "Layer checkpoint",
    )
    layer_height = _nonnegative_int(
        layer_checkpoint["height"], "Layer checkpoint height"
    )
    if layer_height == 0:
        if layer_checkpoint["block_hash"] != "" or layer_checkpoint["block_time_ms"] != 0:
            raise ValueError("genesis M4 checkpoint must use an empty hash and zero time")
        layer_hash = ""
        layer_time = 0
    else:
        layer_hash = _layer_hash(
            layer_checkpoint["block_hash"], "Layer checkpoint hash"
        )
        layer_time = _positive_int(
            layer_checkpoint["block_time_ms"], "Layer checkpoint time"
        )

    raw_evm_events = payload["pending_ethereum_deposits"]
    raw_layer_events = payload["pending_layer_events"]
    if not isinstance(raw_evm_events, list) or not isinstance(raw_layer_events, list):
        raise ValueError("M4 pending event lists are invalid")
    evm_events = tuple(
        _evm_deposit(item, ethereum_number) for item in raw_evm_events
    )
    if len({(item.transaction_hash, item.log_index) for item in evm_events}) != len(
        evm_events
    ):
        raise ValueError("M4 seed has duplicate Ethereum event identities")

    layer_events = []
    blocks = {
        layer_height: {
            "height": layer_height,
            "block_hash": layer_hash,
            "block_time_ms": layer_time,
        }
    }
    for item in raw_layer_events:
        event, block = _layer_event(item, layer_height)
        existing = blocks.get(block["height"])
        if existing is not None and existing != block:
            raise ValueError("M4 seed has conflicting Layer block identities")
        blocks[block["height"]] = block
        layer_events.append(event)
    if len({item["event_key"] for item in layer_events}) != len(layer_events):
        raise ValueError("M4 seed has duplicate Layer event identities")

    return BridgeLedgerSeed(
        digest=hashlib.sha256(raw).hexdigest(),
        ethereum_checkpoint_number=ethereum_number,
        ethereum_checkpoint_hash=ethereum_hash,
        layer_checkpoint_height=layer_height,
        layer_checkpoint_hash=layer_hash,
        layer_checkpoint_time_ms=layer_time,
        evm_events=evm_events,
        layer_blocks=tuple(blocks[key] for key in sorted(blocks)),
        layer_events=tuple(layer_events),
    )


class EthereumDepositBackfiller:
    """Backfill Deposit logs after the reviewed M4 checkpoint."""

    def __init__(self, ethereum, store, seed, confirmations=12, range_size=2_000):
        self.ethereum = ethereum
        self.store = store
        self.seed = seed
        self.confirmations = int(confirmations)
        self.range_size = int(range_size)

    def ingest_available(self, limit_ranges=5):
        if self.ethereum.chain_id() != 1:
            raise Unresolved("M4 backfill provider is not Ethereum mainnet")
        target = self.ethereum.head_number() - self.confirmations
        if target < self.seed.ethereum_checkpoint_number:
            raise Unresolved("M4 Ethereum head precedes the reviewed checkpoint")
        cursor = int(
            self.store.get_meta(
                "evm_deposit_replayed_through",
                self.seed.ethereum_checkpoint_number,
            )
        )
        if cursor < self.seed.ethereum_checkpoint_number or cursor > target:
            raise Unresolved("M4 Ethereum replay cursor is outside the finalized range")
        ranges = 0
        while cursor < target and ranges < int(limit_ranges):
            end = min(cursor + self.range_size, target)
            events = []
            for address in (TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2):
                spec = _deposit_spec(address)
                logs = self.ethereum.logs(
                    address=address,
                    from_block=cursor + 1,
                    to_block=end,
                    topics=[spec.topic],
                )
                events.extend(spec.decode(log) for log in logs)
            for event in events:
                block = self.ethereum.block(event.block_number)
                if (
                    int(block["number"], 16) != event.block_number
                    or str(block["hash"]).lower() != event.block_hash
                ):
                    raise Unresolved("M4 backfill log block identity changed")
            self.store.record_evm_events(events)
            self.store.set_meta("evm_deposit_replayed_through", end)
            cursor = end
            ranges += 1
        return cursor


def _deposit_spec(address):
    topic = hx(keccak(text=DEPOSIT_SIGNATURE))
    return EVENT_BY_ADDRESS_TOPIC[(address, topic)]


def _evm_deposit(item, checkpoint):
    required = {
        "generation",
        "block_number",
        "block_hash",
        "transaction_hash",
        "log_index",
        "args",
    }
    value = _exact_object(item, required, "pending Ethereum deposit")
    generation = value["generation"]
    if generation not in {"v1", "v2"}:
        raise ValueError("M4 seed deposit generation is invalid")
    block_number = _positive_int(value["block_number"], "deposit block number")
    if block_number > checkpoint:
        raise ValueError("M4 seed deposit is after its Ethereum checkpoint")
    args = _exact_object(
        value["args"],
        {"_depositId", "_sender", "_recipient", "_amount", "_tip"},
        "pending Ethereum deposit arguments",
    )
    decoded_args = {
        "_depositId": _nonnegative_int(args["_depositId"], "deposit ID"),
        "_sender": normalize_address(args["_sender"]),
        "_recipient": str(args["_recipient"]),
        "_amount": _nonnegative_int(args["_amount"], "deposit amount"),
        "_tip": _nonnegative_int(args["_tip"], "deposit tip"),
    }
    return DecodedEvent(
        address=TOKEN_BRIDGE_V1 if generation == "v1" else TOKEN_BRIDGE_V2,
        name="Deposit",
        signature=DEPOSIT_SIGNATURE,
        args=decoded_args,
        transaction_hash=_evm_hash(value["transaction_hash"], "deposit transaction hash"),
        log_index=_nonnegative_int(value["log_index"], "deposit log index"),
        block_number=block_number,
        block_hash=_evm_hash(value["block_hash"], "deposit block hash"),
    )


def _layer_event(item, checkpoint):
    required = {
        "height",
        "block_hash",
        "block_time_ms",
        "source",
        "event_ordinal",
        "event_type",
        "attributes",
        "transaction_hash",
    }
    value = _exact_object(item, required, "pending Layer event")
    height = _positive_int(value["height"], "pending Layer event height")
    if height > checkpoint:
        raise ValueError("pending Layer event is after its checkpoint")
    block_hash = _layer_hash(value["block_hash"], "pending Layer event block hash")
    block_time = _positive_int(value["block_time_ms"], "pending Layer event time")
    source = str(value["source"])
    if source != "finalize_block_events" and not LAYER_SOURCE_RE.fullmatch(source):
        raise ValueError("pending Layer event source is invalid")
    ordinal = _nonnegative_int(value["event_ordinal"], "pending Layer event ordinal")
    event_type = str(value["event_type"])
    if event_type not in PENDING_LAYER_TYPES:
        raise ValueError("pending Layer event type is outside the M4 seed")
    attributes = value["attributes"]
    if not isinstance(attributes, dict):
        raise ValueError("pending Layer event attributes are invalid")
    transaction_hash = value["transaction_hash"]
    if source.startswith("txs_results:"):
        transaction_hash = _layer_hash(transaction_hash, "pending Layer transaction hash")
    elif transaction_hash is not None:
        raise ValueError("FinalizeBlock seed event cannot claim a transaction hash")
    event_key = "tellor-1:{}:{}:{}:{}".format(
        height, source, ordinal, event_type
    )
    return (
        {
            "event_key": event_key,
            "height": height,
            "block_hash": block_hash,
            "source": source,
            "event_ordinal": ordinal,
            "event_type": event_type,
            "attributes": attributes,
            "transaction_hash": transaction_hash,
        },
        {
            "height": height,
            "block_hash": block_hash,
            "block_time_ms": block_time,
        },
    )


def _verified_time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("M4 seed verified_at must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("M4 seed verified_at is invalid") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("M4 seed verified_at must be UTC")


def _exact_object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("{} fields are not exact".format(label))
    return value


def _evm_hash(value, label):
    text = str(value).lower()
    if not EVM_HASH_RE.fullmatch(text):
        raise ValueError("{} is invalid".format(label))
    return text


def _layer_hash(value, label):
    text = str(value).upper()
    if not LAYER_HASH_RE.fullmatch(text):
        raise ValueError("{} is invalid".format(label))
    return text


def _positive_int(value, label):
    result = _nonnegative_int(value, label)
    if result == 0:
        raise ValueError("{} must be positive".format(label))
    return result


def _nonnegative_int(value, label):
    if isinstance(value, bool):
        raise ValueError("{} is invalid".format(label))
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("{} is invalid".format(label)) from error
    if result < 0 or str(value) != str(result):
        raise ValueError("{} must be canonical nonnegative decimal".format(label))
    return result
