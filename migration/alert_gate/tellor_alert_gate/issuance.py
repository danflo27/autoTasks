"""M8 exact Ethereum and Tellor Layer issuance checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import stat

from eth_utils import keccak

from .constants import TELLOR_MASTER, ZERO_ADDRESS
from .ethereum import call_data, decode_call, function_selector, hx, normalize_address
from .layer import row_attributes
from .models import ChainPoint, Finding, Unresolved


DAILY_RELEASE_WEI = 146_940_000_000_000_000_000
DAILY_LAYER_LOYA = 146_940_000
SECONDS_PER_DAY = 86_400
MILLISECONDS_PER_DAY = 86_400_000

MINT_SELECTORS = {
    hx(function_selector("mintToOracle()")): "mintToOracle()",
    hx(function_selector("mintToTeam()")): "mintToTeam()",
    hx(function_selector("migrate()")): "migrate()",
}
ADD_STAKING_REWARDS = hx(function_selector("addStakingRewards(uint256)"))
COIN_RE = re.compile(r"(?P<amount>0|[1-9][0-9]*)loya\Z")


@dataclass(frozen=True)
class LayerMintCursor:
    """The replayed minter state immediately before the next block."""

    initialized: bool
    previous_height: int
    previous_block_hash: str
    previous_block_time_ms: int
    previous_inflation_time_ms: int | None

    @classmethod
    def initial(cls):
        return cls(False, 0, "", 0, None)

    @classmethod
    def from_json(cls, value):
        try:
            if not isinstance(value["initialized"], bool):
                raise ValueError("initialized is not boolean")
            cursor = cls(
                initialized=value["initialized"],
                previous_height=int(value["previous_height"]),
                previous_block_hash=str(value["previous_block_hash"]).upper(),
                previous_block_time_ms=int(value["previous_block_time_ms"]),
                previous_inflation_time_ms=(
                    None
                    if value.get("previous_inflation_time_ms") is None
                    else int(value["previous_inflation_time_ms"])
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Layer minter cursor is incomplete") from error
        if cursor.previous_height < 0 or cursor.previous_block_time_ms < 0:
            raise ValueError("Layer minter cursor has negative values")
        if cursor.previous_height == 0:
            if cursor.previous_block_hash or cursor.previous_block_time_ms:
                raise ValueError("genesis cursor must not claim a previous block")
        elif not re.fullmatch(r"[0-9A-F]{64}", cursor.previous_block_hash):
            raise ValueError("Layer minter cursor block hash is invalid")
        if cursor.initialized and cursor.previous_inflation_time_ms is None:
            # This is valid only directly after the initialization transaction. The
            # next BeginBlock establishes PreviousBlockTime without minting.
            pass
        if not cursor.initialized and cursor.previous_inflation_time_ms is not None:
            raise ValueError("uninitialized minter cannot have inflation time")
        if (
            cursor.previous_inflation_time_ms is not None
            and cursor.previous_inflation_time_ms != cursor.previous_block_time_ms
        ):
            raise ValueError(
                "minter PreviousBlockTime must equal the previous committed block time"
            )
        return cursor

    def as_json(self):
        return asdict(self)


def read_layer_mint_seed(path, replay_start_height):
    """Read a verified checkpoint, or allow a complete replay from height one."""

    if int(replay_start_height) == 1:
        return LayerMintCursor.initial()
    if path is None:
        raise ValueError(
            "LAYER_MINTER_SEED_FILE is required unless replay starts at height 1"
        )
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("Layer minter seed cannot be read") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError("Layer minter seed must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Layer minter seed permissions are broader than 0600")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Layer minter seed cannot be read") from error
    if payload.get("chain_id") != "tellor-1":
        raise ValueError("Layer minter seed chain_id must be tellor-1")
    if set(payload) != {
        "schema_version",
        "chain_id",
        "source",
        "verified_at",
        "initialization_event",
        "last_matched_positive_pair",
        "cursor",
    } or payload.get("schema_version") != 1:
        raise ValueError("Layer minter seed fields are not exact")
    cursor = LayerMintCursor.from_json(payload.get("cursor") or {})
    if not cursor.initialized:
        raise ValueError("checkpoint seed must prove initialized minter state")
    if cursor.previous_height + 1 != int(replay_start_height):
        raise ValueError("Layer minter seed does not precede replay start height")
    _validate_seed_proof(payload, cursor)
    return cursor


def evaluate_ethereum_issuance(events, transaction, point, ethereum):
    """Return (finding, proved_mint_to_oracle) for a finalized transaction."""

    mint_events = [
        event
        for event in events
        if event.address == TELLOR_MASTER
        and event.name == "Transfer"
        and normalize_address(event.args["_from"]) == ZERO_ADDRESS
    ]
    if not mint_events:
        return None, False

    trace = ethereum.trace(transaction["hash"])
    calls = _flatten_trace(trace)
    paths = []
    for call in calls:
        if call.get("error") or not _same_address(call.get("to"), TELLOR_MASTER):
            continue
        data = str(call.get("input", "")).lower()
        path = MINT_SELECTORS.get(data[:10]) if len(data) >= 10 else None
        if path is not None:
            paths.append((path, call))

    observed_mints = sorted(
        (
            normalize_address(event.args["_to"]),
            int(event.args["_value"]),
            event.log_index,
        )
        for event in mint_events
    )
    expected = {}
    mismatches = []
    successful_oracle = False
    if len(paths) != 1:
        mismatches.append("successful trace contains zero or multiple recognized mint paths")
        path_name = "unrecognized"
    else:
        path_name, path_call = paths[0]
        parent = point.number - 1
        if parent < 0:
            raise Unresolved("issuance transaction has no parent block")
        if path_name == "mintToOracle()":
            expected, path_mismatches = _oracle_expectation(
                events, calls, point, ethereum, parent
            )
        elif path_name == "mintToTeam()":
            expected, path_mismatches = _team_expectation(point, ethereum, parent)
        else:
            expected, path_mismatches = _migration_expectation(
                path_call, point, ethereum, parent
            )
        mismatches.extend(path_mismatches)
        expected_mints = sorted(
            (recipient, amount) for recipient, amount in expected["mints"]
        )
        actual_mints = sorted((recipient, amount) for recipient, amount, _ in observed_mints)
        if actual_mints != expected_mints:
            mismatches.append("zero-address mint recipients, values, or count differ")

    parent_supply = _uint_call(ethereum, TELLOR_MASTER, "totalSupply()", (), (), point.number - 1)
    current_supply = _uint_call(ethereum, TELLOR_MASTER, "totalSupply()", (), (), point.number)
    log_total = sum(amount for _, amount, _ in observed_mints)
    if current_supply - parent_supply != log_total:
        mismatches.append("total-supply delta differs from zero-address mint logs")
    if not mismatches and path_name == "mintToOracle()":
        successful_oracle = True
    if not mismatches:
        return None, successful_oracle

    finding = Finding(
        slug="issuance-integrity",
        predicate="Ethereum TRB issuance violates the pinned mint path",
        expected={
            "path": path_name,
            **expected,
            "total_supply_delta": log_total,
        },
        observed={
            "recognized_paths": [name for name, _ in paths],
            "mints": observed_mints,
            "total_supply_before": parent_supply,
            "total_supply_after": current_supply,
            "mismatches": mismatches,
        },
        incident_key="issuance:ethereum:{}".format(transaction["hash"].lower()),
        delivery_key="1:{}:M8".format(transaction["hash"].lower()),
        chain_point=point,
        signal="Transfer(address,address,uint256) from zero address",
        contract=TELLOR_MASTER,
        transaction_hash=transaction["hash"].lower(),
        log_index=min(event.log_index for event in mint_events),
        evidence={"trace_path": path_name, "mint_log_count": len(mint_events)},
    )
    return finding, False


def evaluate_layer_issuance(event_rows, block_row, cursor):
    """Evaluate one committed block and return (finding, next_cursor)."""

    height = int(block_row["height"])
    block_hash = str(block_row["block_hash"]).upper()
    time_ms = int(block_row["block_time_ms"])
    if height != cursor.previous_height + 1:
        raise Unresolved("Layer issuance replay is not consecutive")
    if cursor.previous_height and time_ms < cursor.previous_block_time_ms:
        raise Unresolved("Tellor Layer block time moved backwards")
    if (
        cursor.previous_inflation_time_ms is not None
        and cursor.previous_inflation_time_ms != cursor.previous_block_time_ms
    ):
        raise Unresolved("replayed minter PreviousBlockTime is inconsistent")

    initialized_events = [row for row in event_rows if row["event_type"] == "minter_initialized"]
    mint_rows = [row for row in event_rows if row["event_type"] == "mint_coins"]
    reward_rows = [
        row
        for row in event_rows
        if row["event_type"] == "inflationary_rewards_distributed"
    ]
    if len(initialized_events) > 1 or (cursor.initialized and initialized_events):
        raise Unresolved("minter initialization event history is inconsistent")

    if not cursor.initialized:
        expected_amount = None
        next_initialized = bool(initialized_events)
        next_inflation_time = None
    elif cursor.previous_inflation_time_ms is None:
        expected_amount = None
        next_initialized = True
        next_inflation_time = time_ms
    else:
        delta = time_ms - cursor.previous_inflation_time_ms
        expected_amount = DAILY_LAYER_LOYA * delta // MILLISECONDS_PER_DAY
        next_initialized = True
        next_inflation_time = time_ms

    mismatches = []
    observed_mints = [_mint_attributes(row) for row in mint_rows]
    observed_rewards = [_reward_attributes(row) for row in reward_rows]
    if expected_amount is None or expected_amount == 0:
        if mint_rows or reward_rows:
            reason = (
                "ordinary inflation exists before/at initialization"
                if expected_amount is None
                else "mint events exist when the integer formula is zero"
            )
            mismatches.append(reason)
    else:
        if len(observed_mints) != 1 or len(observed_rewards) != 1:
            mismatches.append("positive inflation does not have exactly one event pair")
        if len(observed_mints) == 1:
            mint = observed_mints[0]
            if mint["amount"] != expected_amount or mint["destination"] != "mint":
                mismatches.append("mint_coins amount or destination differs")
            if mint_rows[0]["source"] != "finalize_block_events":
                mismatches.append("mint_coins is not a committed FinalizeBlock event")
        if len(observed_rewards) == 1:
            reward = observed_rewards[0]
            if reward["amount"] != expected_amount:
                mismatches.append("inflationary reward amount differs")
            if reward_rows[0]["source"] != "finalize_block_events":
                mismatches.append(
                    "inflationary_rewards_distributed is not a committed FinalizeBlock event"
                )

    next_cursor = LayerMintCursor(
        initialized=next_initialized,
        previous_height=height,
        previous_block_hash=block_hash,
        previous_block_time_ms=time_ms,
        previous_inflation_time_ms=next_inflation_time,
    )
    if not mismatches:
        return None, next_cursor
    point = ChainPoint("tellor-1", height, block_hash, time_ms // 1000)
    return (
        Finding(
            slug="issuance-integrity",
            predicate="Tellor Layer issuance differs from the exact BeginBlock formula",
            expected={
                "amount_loya": expected_amount,
                "event_pair": expected_amount is not None and expected_amount > 0,
                "destination": "mint" if expected_amount else None,
            },
            observed={
                "mint_coins": observed_mints,
                "inflationary_rewards_distributed": observed_rewards,
                "initialized_before_block": cursor.initialized,
                "mismatches": mismatches,
            },
            incident_key="issuance:tellor-1:{}".format(height),
            delivery_key="tellor-1:{}:M8".format(height),
            chain_point=point,
            signal="mint_coins + inflationary_rewards_distributed",
            contract="layer.mint",
            evidence={
                "current_time_ms": time_ms,
                "previous_time_ms": cursor.previous_inflation_time_ms,
            },
        ),
        next_cursor,
    )


def _oracle_expectation(events, calls, point, client, parent):
    prior_release = _uint_var(client, "_LAST_RELEASE_TIME_DAO", parent)
    released = DAILY_RELEASE_WEI * (point.timestamp - prior_release) // SECONDS_PER_DAY
    rewards = released * 2 // 100
    oracle = _address_var(client, "_ORACLE_CONTRACT", parent)
    current_release = _uint_var(client, "_LAST_RELEASE_TIME_DAO", point.number)
    expected = {
        "mints": [(oracle, released - rewards), (TELLOR_MASTER, rewards)],
        "prior_release_time": prior_release,
        "release_time_after": point.timestamp,
        "staking_rewards": rewards,
        "oracle": oracle,
    }
    mismatches = []
    if point.timestamp < prior_release:
        raise Unresolved("DAO release timestamp is after the issuance block")
    if current_release != point.timestamp:
        mismatches.append("DAO release timestamp did not advance to block time")
    expected_staking_input = call_data(
        "addStakingRewards(uint256)", ["uint256"], [rewards]
    ).lower()
    staking_calls = [
        call
        for call in calls
        if not call.get("error")
        and _same_address(call.get("to"), oracle)
        and str(call.get("input", "")).lower() == expected_staking_input
    ]
    if len(staking_calls) != 1:
        mismatches.append("trace lacks one exact addStakingRewards call")
    routed = sum(
        int(event.args["_value"])
        for event in events
        if event.address == TELLOR_MASTER
        and event.name == "Transfer"
        and normalize_address(event.args["_from"]) == TELLOR_MASTER
        and normalize_address(event.args["_to"]) == oracle
    )
    if routed != rewards:
        mismatches.append("staking-reward transfer routing differs")
    return expected, mismatches


def _team_expectation(point, client, parent):
    prior_release = _uint_var(client, "_LAST_RELEASE_TIME_TEAM", parent)
    if point.timestamp < prior_release:
        raise Unresolved("team release timestamp is after the issuance block")
    released = DAILY_RELEASE_WEI * (point.timestamp - prior_release) // SECONDS_PER_DAY
    owner = _address_var(client, "_OWNER", parent)
    current_release = _uint_var(client, "_LAST_RELEASE_TIME_TEAM", point.number)
    mismatches = []
    if current_release != point.timestamp:
        mismatches.append("team release timestamp did not advance to block time")
    return {
        "mints": [(owner, released)],
        "prior_release_time": prior_release,
        "release_time_after": point.timestamp,
        "owner": owner,
    }, mismatches


def _migration_expectation(call, point, client, parent):
    caller = normalize_address(call.get("from"))
    old_tellor = _address_var(client, "_OLD_TELLOR", parent)
    parent_migrated = _bool_call(
        client, TELLOR_MASTER, "isMigrated(address)", ["address"], [caller], parent
    )
    current_migrated = _bool_call(
        client,
        TELLOR_MASTER,
        "isMigrated(address)",
        ["address"],
        [caller],
        point.number,
    )
    old_balance = _uint_call(
        client, old_tellor, "balanceOf(address)", ["address"], [caller], parent
    )
    mismatches = []
    if parent_migrated:
        mismatches.append("caller was already migrated at the parent block")
    if not current_migrated:
        mismatches.append("post-state migrated flag is false")
    return {
        "mints": [(caller, old_balance)],
        "caller": caller,
        "old_tellor": old_tellor,
        "parent_migrated": False,
        "post_migrated": True,
    }, mismatches


def _flatten_trace(root):
    if not isinstance(root, dict):
        raise Unresolved("call trace root is invalid")
    output = []
    stack = [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            raise Unresolved("call trace node is invalid")
        output.append(node)
        children = node.get("calls") or []
        if not isinstance(children, list):
            raise Unresolved("call trace children are invalid")
        stack.extend(reversed(children))
    return output


def _same_address(value, expected):
    try:
        return normalize_address(value) == expected
    except (TypeError, ValueError):
        return False


def _uint_var(client, key, block):
    return _uint_call(
        client,
        TELLOR_MASTER,
        "getUintVar(bytes32)",
        ["bytes32"],
        [keccak(text=key)],
        block,
    )


def _address_var(client, key, block):
    value = decode_call(
        ["address"],
        client.eth_call(
            TELLOR_MASTER,
            call_data("getAddressVars(bytes32)", ["bytes32"], [keccak(text=key)]),
            block,
        ),
    )[0]
    return normalize_address(value)


def _uint_call(client, to, signature, types, values, block):
    return int(
        decode_call(
            ["uint256"], client.eth_call(to, call_data(signature, types, values), block)
        )[0]
    )


def _bool_call(client, to, signature, types, values, block):
    return bool(
        decode_call(
            ["bool"], client.eth_call(to, call_data(signature, types, values), block)
        )[0]
    )


def _mint_attributes(row):
    attrs = row_attributes(row)
    try:
        match = COIN_RE.fullmatch(str(attrs["amount"]))
        destination = str(attrs["destination"])
    except (KeyError, TypeError) as error:
        raise Unresolved("mint_coins attributes are incomplete") from error
    if match is None:
        return {"amount": None, "raw_amount": attrs.get("amount"), "destination": destination}
    return {"amount": int(match.group("amount")), "destination": destination}


def _reward_attributes(row):
    attrs = row_attributes(row)
    try:
        match = COIN_RE.fullmatch(str(attrs["total_amount"]))
    except (KeyError, TypeError) as error:
        raise Unresolved("inflationary reward attributes are incomplete") from error
    if match is None:
        return {"amount": None, "raw_amount": attrs.get("total_amount")}
    return {"amount": int(match.group("amount"))}


def _validate_seed_proof(payload, cursor):
    initialization = payload.get("initialization_event")
    pair = payload.get("last_matched_positive_pair")
    if not isinstance(payload.get("source"), str) or not payload["source"].strip():
        raise ValueError("Layer minter seed source is required")
    if not isinstance(payload.get("verified_at"), str) or not payload["verified_at"].endswith("Z"):
        raise ValueError("Layer minter seed verified_at must be canonical UTC")
    try:
        verified = datetime.fromisoformat(
            payload["verified_at"].replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ValueError("Layer minter seed verified_at is invalid") from error
    if verified.utcoffset() is None or verified.utcoffset().total_seconds() != 0:
        raise ValueError("Layer minter seed verified_at must be UTC")
    if not isinstance(initialization, dict) or set(initialization) != {
        "height",
        "block_hash",
        "event_key",
    }:
        raise ValueError("Layer minter initialization proof is incomplete")
    if not isinstance(pair, dict) or set(pair) != {
        "height",
        "block_hash",
        "amount_loya",
        "mint_amount_loya",
        "distribution_amount_loya",
    }:
        raise ValueError("Layer minter last-pair proof is incomplete")
    init_height = int(initialization["height"])
    pair_height = int(pair["height"])
    if not (0 < init_height <= pair_height <= cursor.previous_height):
        raise ValueError("Layer minter seed proof heights are inconsistent")
    for proof in (initialization, pair):
        block_hash = str(proof["block_hash"]).upper()
        if not re.fullmatch(r"[0-9A-F]{64}", block_hash):
            raise ValueError("Layer minter seed proof block hash is invalid")
    if not str(initialization["event_key"]).startswith(
        "tellor-1:{}:".format(init_height)
    ):
        raise ValueError("Layer minter initialization event identity is invalid")
    amounts = [
        int(pair["amount_loya"]),
        int(pair["mint_amount_loya"]),
        int(pair["distribution_amount_loya"]),
    ]
    if amounts[0] <= 0 or len(set(amounts)) != 1:
        raise ValueError("Layer minter last positive event pair does not match")
