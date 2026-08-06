"""M5 structural report checks and exact historical EVMCall replay."""

from eth_abi import decode, encode
from eth_utils import keccak

from .constants import FIXED_QUERY_TYPES, M6_SPOT_QUERY_IDS, TELLOR_FLEX
from .ethereum import hx, normalize_address, unhex
from .models import Finding, Unresolved


class MalformedKnownReport(ValueError):
    pass


def _decode_exact(types, raw, label):
    try:
        values = decode(list(types), raw, strict=True)
        if encode(list(types), list(values)) != raw:
            raise ValueError("non-canonical ABI")
        return values
    except Exception as error:
        raise MalformedKnownReport("{} is not canonical ABI".format(label)) from error


def validate_fixed_value(query_id, value):
    """Validate a known M5/M6 response. Return False for an unknown ID."""
    entry = FIXED_QUERY_TYPES.get(query_id.lower())
    response_type = entry[1] if entry else None
    if response_type is None:
        return False
    raw = unhex(value) if isinstance(value, str) else bytes(value)
    _decode_exact([response_type], raw, "fixed query response")
    return True


def evaluate_new_report(event, chain_point, source_clients):
    args = event.args
    query_id = str(args.get("_queryId", "")).lower()
    value_hex = args.get("_value")
    query_data_hex = args.get("_queryData")
    report_timestamp = _positive_int(args.get("_time"), "report timestamp")
    known_by_id = query_id in FIXED_QUERY_TYPES or query_id in M6_SPOT_QUERY_IDS
    try:
        query_data = unhex(query_data_hex)
        query_type, inner_data = _decode_exact(
            ["string", "bytes"], query_data, "queryData envelope"
        )
    except (ValueError, MalformedKnownReport):
        if not known_by_id:
            return None
        return _malformed(
            event,
            chain_point,
            query_id,
            report_timestamp,
            "known queryData envelope is malformed",
        )

    known_type = query_type in {
        "SpotPrice",
        "EVMCall",
        "TellorRNG",
        "AutopayAddresses",
        "TellorOracleAddress",
        "AmpleforthCustomSpotPrice",
        "AmpleforthUSPCE",
    }
    if not known_type:
        return None
    if "0x" + keccak(query_data).hex() != query_id:
        return _malformed(
            event,
            chain_point,
            query_id,
            report_timestamp,
            "query ID does not equal keccak256(queryData)",
        )

    try:
        try:
            value = unhex(value_hex)
        except (TypeError, ValueError) as error:
            raise MalformedKnownReport("known response is not strict hex") from error
        if query_type == "SpotPrice":
            asset, currency = _decode_exact(
                ["string", "string"], inner_data, "SpotPrice inner data"
            )
            if not asset or not currency:
                raise MalformedKnownReport("SpotPrice pair is empty")
            _decode_exact(["uint256"], value, "SpotPrice response")
            return None
        if query_type == "TellorRNG":
            _decode_exact(["uint256"], inner_data, "TellorRNG inner data")
            if len(value) != 32:
                raise MalformedKnownReport("TellorRNG response is not bytes32")
            return None
        if query_type in {
            "AutopayAddresses",
            "TellorOracleAddress",
            "AmpleforthCustomSpotPrice",
            "AmpleforthUSPCE",
        }:
            expected = FIXED_QUERY_TYPES.get(query_id)
            if expected is None or expected[0] != query_type:
                raise MalformedKnownReport("fixed query ID/type mismatch")
            phantom = _decode_exact(["bytes"], inner_data, "fixed inner data")[0]
            if phantom:
                raise MalformedKnownReport("fixed inner bytes are not empty")
            _decode_exact([expected[1]], value, "fixed response")
            return None
        return _evaluate_evm_call(
            event,
            chain_point,
            query_id,
            report_timestamp,
            inner_data,
            value,
            source_clients,
        )
    except MalformedKnownReport as error:
        return _malformed(
            event, chain_point, query_id, report_timestamp, str(error)
        )


def _evaluate_evm_call(
    event,
    chain_point,
    query_id,
    report_timestamp,
    inner_data,
    value,
    source_clients,
):
    chain_id, target, calldata = _decode_exact(
        ["uint256", "address", "bytes"], inner_data, "EVMCall inner data"
    )
    returned, source_timestamp = _decode_exact(
        ["bytes", "uint256"], value, "EVMCall response"
    )
    if len(calldata) < 4:
        raise Unresolved("EVMCall calldata is shorter than four bytes")
    client = source_clients.get(int(chain_id))
    if client is None:
        raise Unresolved("EVMCall source chain is not configured")
    source_block = historical_block_at_timestamp(client, int(source_timestamp))
    block_number = int(source_block["number"], 16)
    block_hash = source_block["hash"].lower()
    target = normalize_address(target)
    code_before = client.code(target, block_number)
    if code_before in {"0x", "0x0"}:
        raise Unresolved("EVMCall target had no code at the source block")
    try:
        reproduced_hex = client.eth_call(
            target,
            hx(calldata),
            block_number,
            extra={"gasPrice": 0},
        )
    except Unresolved:
        raise
    reproduced = unhex(reproduced_hex)
    if not reproduced:
        raise Unresolved("EVMCall replay returned empty bytes")
    reread = client.block_by_hash(block_hash)
    reread_number = client.block(block_number)
    if (
        int(reread["number"], 16) != block_number
        or int(reread["timestamp"], 16) != int(source_timestamp)
        or reread_number["hash"].lower() != block_hash
        or int(reread_number["timestamp"], 16) != int(source_timestamp)
        or client.code(target, block_number).lower() != code_before.lower()
    ):
        raise Unresolved("EVMCall source block or code changed during replay")
    if reproduced == returned:
        return None
    return Finding(
        slug="tellorflex-value-integrity",
        predicate="exact finalized EVMCall replay bytes differ from the report",
        expected={
            "chain_id": int(chain_id),
            "source_block": block_number,
            "return_data": hx(reproduced),
        },
        observed={"return_data": hx(returned)},
        incident_key="tellorflex-value:{}:{}".format(query_id, report_timestamp),
        delivery_key="1:{}:M5:{}".format(
            event.transaction_hash, event.log_index
        ),
        chain_point=chain_point,
        signal=event.signature,
        contract=TELLOR_FLEX,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        evidence={
            "query_id": query_id,
            "report_timestamp": report_timestamp,
            "value": hx(value),
            "source_block_hash": block_hash,
            "source_timestamp": int(source_timestamp),
        },
    )


def historical_block_at_timestamp(client, timestamp):
    finalized = client.block("finalized")
    high = int(finalized["number"], 16)
    if int(finalized["timestamp"], 16) < timestamp:
        raise Unresolved("EVMCall source timestamp is after finalized head")
    low = 0
    while low < high:
        middle = (low + high) // 2
        block = client.block(middle)
        if int(block["timestamp"], 16) < timestamp:
            low = middle + 1
        else:
            high = middle
    candidate = client.block(low)
    if int(candidate["timestamp"], 16) != timestamp:
        raise Unresolved("EVMCall source timestamp has no exact block")
    if low > 0 and int(client.block(low - 1)["timestamp"], 16) == timestamp:
        raise Unresolved("EVMCall source timestamp is ambiguous")
    finalized_number = int(finalized["number"], 16)
    if low < finalized_number and int(client.block(low + 1)["timestamp"], 16) == timestamp:
        raise Unresolved("EVMCall source timestamp is ambiguous")
    return candidate


def _malformed(event, chain_point, query_id, report_timestamp, reason):
    return Finding(
        slug="tellorflex-value-integrity",
        predicate="known Tellor report is structurally invalid",
        expected="canonical query envelope and exact response ABI round-trip",
        observed=reason,
        incident_key="tellorflex-value:{}:{}".format(query_id, report_timestamp),
        delivery_key="1:{}:M5:{}".format(
            event.transaction_hash, event.log_index
        ),
        chain_point=chain_point,
        signal=event.signature,
        contract=TELLOR_FLEX,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        evidence={
            "query_id": query_id,
            "report_timestamp": report_timestamp,
            "value": event.args.get("_value"),
        },
    )


def _positive_int(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise Unresolved("{} is invalid".format(label)) from error
    if number <= 0:
        raise Unresolved("{} is invalid".format(label))
    return number
