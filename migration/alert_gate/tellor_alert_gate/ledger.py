"""M4 bridge ledger, M6 DataBank, and M7 dispute reconciliation."""

import json
import re
import time

from eth_abi import decode, encode
from eth_utils import keccak

from .constants import (
    BRIDGES,
    GOVERNANCE,
    M6_SPOT_QUERY_IDS,
    TELLOR_DATA_BANK,
    TELLOR_FLEX,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V1,
    TOKEN_BRIDGE_V2,
    ZERO_ADDRESS,
)
from .ethereum import (
    EVENT_BY_ADDRESS_TOPIC,
    call_data,
    decode_call,
    function_selector,
    hx,
    normalize_address,
    unhex,
)
from .layer import row_attributes
from .models import ChainPoint, Finding, Unresolved
from .values import MalformedKnownReport, validate_fixed_value


LOYA_RE = re.compile(r"(?P<amount>0|[1-9][0-9]*)loya\Z")
LOWER_HEX_RE = re.compile(r"[0-9a-f]+\Z")
BECH32_RE = re.compile(r"tellor1[023456789acdefghjklmnpqrstuvwxyz]{20,80}\Z")
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"


def evaluate_evm_disputes(events, chain_point):
    disputes = [event for event in events if event.address == GOVERNANCE and event.name == "NewDispute"]
    removals = [event for event in events if event.address == TELLOR_FLEX and event.name == "ValueRemoved"]
    findings = []
    rooted = set()
    for event in disputes:
        query_id = str(event.args["_queryId"]).lower()
        timestamp = int(event.args["_timestamp"])
        identity = (query_id, timestamp)
        if identity in rooted:
            continue
        rooted.add(identity)
        observed = {
            "dispute_id": int(event.args["_disputeId"]),
            "query_id": query_id,
            "report_timestamp": timestamp,
            "reporter": event.args["_reporter"],
            "duplicate_effects": [
                removal.signature
                for removal in removals
                if str(removal.args["_queryId"]).lower() == query_id
                and int(removal.args["_timestamp"]) == timestamp
            ],
        }
        findings.append(
            _evm_dispute_finding(event, chain_point, query_id, timestamp, observed)
        )
    for event in removals:
        query_id = str(event.args["_queryId"]).lower()
        timestamp = int(event.args["_timestamp"])
        if (query_id, timestamp) in rooted:
            continue
        rooted.add((query_id, timestamp))
        observed = {
            "query_id": query_id,
            "report_timestamp": timestamp,
            "fallback": "ValueRemoved without decodable NewDispute",
        }
        findings.append(
            _evm_dispute_finding(event, chain_point, query_id, timestamp, observed)
        )
    return findings


def _evm_dispute_finding(event, chain_point, query_id, timestamp, observed):
    return Finding(
        slug="governance-dispute",
        predicate="a new Ethereum dispute opened for a Tellor report",
        expected="no newly challenged or removed report",
        observed=observed,
        incident_key="governance-dispute:ethereum:{}:{}".format(query_id, timestamp),
        delivery_key="1:{}:M7:{}".format(
            event.transaction_hash, event.log_index
        ),
        chain_point=chain_point,
        signal=event.signature,
        contract=event.address,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        evidence={"query_id": query_id, "report_timestamp": timestamp},
    )


def evaluate_layer_dispute(row, block_row):
    attributes = row_attributes(row)
    required = {
        "disputer",
        "reporter",
        "dispute_category",
        "total_fee",
        "fee_paid",
        "pay_from_bond",
        "dispute_id",
        "value",
        "query_type",
        "query_id",
        "report_block_number",
        "microreport_timestamp",
    }
    if set(attributes) != required:
        raise Unresolved("Layer new_dispute attributes are not exact")
    query_id = _layer_hex32(attributes["query_id"])
    timestamp = _decimal(attributes["microreport_timestamp"], "microreport timestamp")
    for field in (
        "total_fee",
        "fee_paid",
        "dispute_id",
        "report_block_number",
    ):
        _decimal(attributes[field], field)
    if attributes["pay_from_bond"] not in {"true", "false"}:
        raise Unresolved("Layer dispute pay_from_bond is not canonical boolean")
    point = _layer_point(block_row)
    return Finding(
        slug="governance-dispute",
        predicate="a new Tellor Layer dispute opened for a microreport",
        expected="no newly challenged report",
        observed=attributes,
        incident_key="governance-dispute:tellor-1:{}:{}".format(
            query_id, timestamp
        ),
        delivery_key="tellor-1:{}:M7:{}".format(row["height"], row["event_key"]),
        chain_point=point,
        signal="new_dispute",
        contract="layer.dispute",
        evidence={"query_id": query_id, "report_timestamp": timestamp},
    )


def evaluate_bridge_transaction(events, chain_point, ethereum, store, transaction=None):
    findings = []
    bridge_events = [event for event in events if event.address in BRIDGES]
    transfers = [
        event for event in events if event.address == TELLOR_MASTER and event.name == "Transfer"
    ]
    tx_hash = events[0].transaction_hash if events else ""

    for event in bridge_events:
        if event.name == "Withdraw":
            finding = _evaluate_evm_withdraw(
                event,
                transfers,
                chain_point,
                ethereum,
                store,
                transaction,
            )
            if finding:
                findings.append(finding)
        elif event.name == "ExtraWithdrawClaimed":
            finding = _evaluate_extra_claim(event, transfers, chain_point, ethereum)
            if finding:
                findings.append(finding)

    for transfer in transfers:
        source = normalize_address(transfer.args["_from"])
        if source not in BRIDGES or _outflow_has_exact_cause(transfer, bridge_events):
            continue
        findings.append(
            Finding(
                slug="bridge-ledger-integrity",
                predicate="TRB transfer out of a bridge has no same-transaction bridge cause",
                expected="Withdraw, exact pending claim, pause tribute burn, or exact pause refund",
                observed=transfer.args,
                incident_key="bridge-ledger:outflow:{}:{}".format(
                    tx_hash, transfer.log_index
                ),
                delivery_key="1:{}:M4:{}".format(tx_hash, transfer.log_index),
                chain_point=chain_point,
                signal=transfer.signature,
                contract=TELLOR_MASTER,
                transaction_hash=tx_hash,
                log_index=transfer.log_index,
            )
        )
    return findings


def evaluate_bridge_aggregate(row, block_row):
    """Create M4 evidence for a provably malformed bridge aggregate."""

    attributes = row_attributes(row)
    try:
        aggregate = parse_bridge_aggregate(row)
    except (Unresolved, ValueError) as error:
        if (
            attributes.get("micro_report_type")
            not in {"TRBBridge", "TRBBridgeV2"}
            and not isinstance(error, ValueError)
        ):
            raise
        point = _layer_point(block_row)
        return Finding(
            slug="bridge-ledger-integrity",
            predicate="committed bridge aggregate has a non-canonical identity or value",
            expected="canonical TRBBridge/TRBBridgeV2 query ID, query data, and report value",
            observed=str(error),
            incident_key="bridge-ledger:aggregate:{}".format(row["event_key"]),
            delivery_key="tellor-1:{}:M4:{}".format(
                row["height"], row["event_key"]
            ),
            chain_point=point,
            signal="aggregate_report",
            contract="layer.oracle",
            evidence={"event_key": row["event_key"]},
        )
    return None


def evaluate_layer_bridge_event(row, block_row, store, layer_client):
    if row["event_type"] == "deposit_claimed":
        return _evaluate_layer_deposit_claim(row, block_row, store, layer_client)
    if row["event_type"] == "tokens_withdrawn":
        return _evaluate_layer_withdraw(row, block_row, store)
    return None


def parse_bridge_aggregate(row):
    attributes = row_attributes(row)
    required = {
        "query_id",
        "query_data",
        "value",
        "aggregate_power",
        "micro_report_height",
        "timestamp",
        "micro_report_type",
    }
    if not required.issubset(attributes):
        raise Unresolved("Layer aggregate_report attributes are incomplete")
    query_data = _layer_hex(attributes["query_data"], "query_data")
    try:
        query_type, inner = _decode_exact(["string", "bytes"], query_data)
    except ValueError as error:
        raise Unresolved("Layer aggregate query envelope is not canonical") from error
    if query_type not in {"TRBBridge", "TRBBridgeV2"}:
        return None
    query_id = _layer_hex32(attributes["query_id"])
    if "0x" + keccak(query_data).hex() != query_id:
        raise ValueError("Layer aggregate query ID does not equal query data")
    if attributes["micro_report_type"] != query_type:
        raise ValueError("Layer aggregate query type attribute differs from query data")
    to_layer, identity = _decode_exact(["bool", "uint256"], inner)
    value = _layer_hex(attributes["value"], "aggregate value")
    evm_address, layer_address, amount, tip = _decode_exact(
        ["address", "string", "uint256", "uint256"], value
    )
    return {
        "generation": "v2" if query_type == "TRBBridgeV2" else "v1",
        "query_type": query_type,
        "query_id": query_id,
        "to_layer": bool(to_layer),
        "id": int(identity),
        "timestamp": _decimal(attributes["timestamp"], "aggregate timestamp"),
        "power": _decimal(attributes["aggregate_power"], "aggregate power"),
        "micro_report_height": _decimal(
            attributes["micro_report_height"], "micro report height"
        ),
        "evm_address": normalize_address(evm_address),
        "layer_address": layer_address,
        "amount": int(amount),
        "tip": int(tip),
        "value_hex": hx(value),
        "height": int(row["height"]),
        "event_key": row["event_key"],
    }


def evaluate_databank_event(
    event,
    chain_point,
    ethereum,
    layer,
    second_delay=30,
    sleep=time.sleep,
):
    outer_query = str(event.args["queryId"]).lower()
    try:
        attest_query, report, attestation_timestamp = event.args["attestData"]
        value, report_timestamp, power, _previous, _next, _consensus = report
        attest_query = str(attest_query).lower()
        value = str(value).lower()
        report_timestamp = int(report_timestamp)
        power = int(power)
        attestation_timestamp = int(attestation_timestamp)
    except (TypeError, ValueError) as error:
        raise Unresolved("OracleUpdated tuple did not decode") from error
    mismatches = []
    if outer_query != attest_query:
        mismatches.append("indexed query ID differs from attestation query ID")

    spec = EVENT_BY_ADDRESS_TOPIC[
        (
            TELLOR_DATA_BANK,
            "0x" + keccak(
                text="OracleUpdated(bytes32,(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256))"
            ).hex(),
        )
    ]
    block_logs = ethereum.logs(
        address=TELLOR_DATA_BANK,
        from_block=chain_point.number,
        to_block=chain_point.number,
        topics=[spec.topic],
    )
    decoded = sorted(
        (spec.decode(log) for log in block_logs),
        key=lambda candidate: candidate.log_index,
    )
    same_query = [
        candidate
        for candidate in decoded
        if str(candidate.args["queryId"]).lower() == outer_query
    ]
    c0 = int(
        decode_call(
            ["uint256"],
            ethereum.eth_call(
                TELLOR_DATA_BANK,
                call_data(
                    "getAggregateValueCount(bytes32)", ["bytes32"], [unhex(outer_query)]
                ),
                chain_point.number - 1,
            ),
        )[0]
    )
    c1 = int(
        decode_call(
            ["uint256"],
            ethereum.eth_call(
                TELLOR_DATA_BANK,
                call_data(
                    "getAggregateValueCount(bytes32)", ["bytes32"], [unhex(outer_query)]
                ),
                chain_point.number,
            ),
        )[0]
    )
    if c1 - c0 != len(same_query):
        mismatches.append("append-count delta differs from ordered successful events")
    position = next(
        (index for index, candidate in enumerate(same_query) if candidate.log_index == event.log_index),
        None,
    )
    if position is None:
        raise Unresolved("OracleUpdated event was absent from finalized block log read")
    stored = decode_call(
        ["bytes", "uint256", "uint256", "uint256", "uint256"],
        ethereum.eth_call(
            TELLOR_DATA_BANK,
            call_data(
                "getAggregateByIndex(bytes32,uint256)",
                ["bytes32", "uint256"],
                [unhex(outer_query), c0 + position],
            ),
            chain_point.number,
        ),
    )
    expected_stored = (
        unhex(value),
        power,
        report_timestamp,
        attestation_timestamp,
        chain_point.timestamp,
    )
    if tuple(stored) != expected_stored:
        mismatches.append("stored five-field aggregate differs from event and block")

    layer_first = _parse_historical_report(
        layer.historical_report(outer_query, report_timestamp)
    )
    if not _layer_report_matches(layer_first, outer_query, value, power):
        if second_delay:
            sleep(second_delay)
        layer_second = _parse_historical_report(
            layer.historical_report(outer_query, report_timestamp)
        )
        if not _layer_report_matches(layer_second, outer_query, value, power):
            mismatches.append("Tellor Layer historical aggregate differs from event")
    try:
        if outer_query in M6_SPOT_QUERY_IDS:
            if len(unhex(value)) != 32:
                raise MalformedKnownReport("fixed SpotPrice response is not 32 bytes")
        else:
            validate_fixed_value(outer_query, value)
    except (MalformedKnownReport, ValueError):
        mismatches.append("fixed response ABI does not round-trip")

    if not mismatches:
        return None
    return Finding(
        slug="databank-value-integrity",
        predicate="DataBank accepted value failed event, storage, Layer, or decoder equality",
        expected={
            "query_id": outer_query,
            "value": value,
            "power": power,
            "report_timestamp": report_timestamp,
            "stored": [hx(stored[0]), *stored[1:]],
        },
        observed={"mismatches": mismatches, "layer": layer_first},
        incident_key="databank-value:{}:{}".format(outer_query, report_timestamp),
        delivery_key="1:{}:M6:{}".format(
            event.transaction_hash, event.log_index
        ),
        chain_point=chain_point,
        signal=event.signature,
        contract=TELLOR_DATA_BANK,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        evidence={
            "query_id": outer_query,
            "report_timestamp": report_timestamp,
            "value": value,
        },
    )


def _evaluate_layer_deposit_claim(row, block_row, store, layer_client):
    attributes = row_attributes(row)
    try:
        deposit_id = _decimal(attributes["deposit_id"], "deposit ID")
        recipient = attributes["recipient"]
        amount_loya = _loya(attributes["amount"])
    except (KeyError, TypeError) as error:
        raise Unresolved("deposit_claimed attributes are incomplete") from error
    aggregate_timestamp = _claim_timestamp(
        row, block_row, store, layer_client, deposit_id
    )
    aggregates = _bridge_aggregates(
        store,
        to_layer=True,
        identity=deposit_id,
        timestamp=aggregate_timestamp,
    )
    if len(aggregates) == 2:
        # The pinned Layer resolver prefers V2 and then falls back to V1 for
        # the same exact (deposit ID, timestamp).
        v2 = [item for item in aggregates if item["generation"] == "v2"]
        if len(v2) == 1:
            aggregates = v2
    if len(aggregates) != 1:
        raise Unresolved("deposit claim generation or aggregate is ambiguous")
    aggregate = aggregates[0]
    address = TOKEN_BRIDGE_V2 if aggregate["generation"] == "v2" else TOKEN_BRIDGE_V1
    deposits = [
        _evm_row(row_value)
        for row_value in store.evm_events("Deposit", address)
        if int(json.loads(row_value["args_json"])["_depositId"]) == deposit_id
    ]
    if len(deposits) == 0:
        raise Unresolved("finalized Ethereum deposit counterpart is not available yet")
    mismatches = []
    if len(deposits) != 1:
        mismatches.append("duplicate finalized deposit ID")
        deposit = deposits[0]
    else:
        deposit = deposits[0]
    expected_query_id = bridge_query_id(aggregate["query_type"], True, deposit_id)
    if aggregate["query_id"] != expected_query_id:
        mismatches.append("query ID or bridge generation differs")
    evm_args = deposit["args"]
    if normalize_address(evm_args["_sender"]) != aggregate["evm_address"]:
        mismatches.append("deposit sender differs")
    report_recipient = str(evm_args["_recipient"])
    if aggregate["layer_address"] != report_recipient:
        mismatches.append("aggregate recipient differs from Ethereum deposit")
    if BECH32_RE.fullmatch(report_recipient):
        if str(recipient) != report_recipient:
            mismatches.append("deposit recipient differs")
    else:
        team_payload = layer_client.team_address(int(row["height"]))
        team_address = str(team_payload.get("team_address", ""))
        if not BECH32_RE.fullmatch(team_address):
            raise Unresolved("historical Layer team address is invalid")
        if str(recipient) != team_address:
            mismatches.append("invalid report recipient did not use the historical team address")
    if int(evm_args["_amount"]) != aggregate["amount"]:
        mismatches.append("deposit amount differs")
    if int(evm_args["_tip"]) != aggregate["tip"]:
        mismatches.append("deposit tip differs")
    if aggregate["amount"] % 10**12 or aggregate["amount"] // 10**12 != amount_loya:
        mismatches.append("claimed loya amount differs from exact wei conversion")
    claimed = layer_client.deposit_claimed(deposit_id)
    if not _claimed_true(claimed):
        mismatches.append("historical deposit-claimed state is not true")
    if not mismatches:
        return None
    point = _layer_point(block_row)
    return Finding(
        slug="bridge-ledger-integrity",
        predicate="Tellor Layer deposit claim does not reconcile with its Ethereum deposit and aggregate",
        expected={"deposit_id": deposit_id, "aggregate": aggregate, "claimed": True},
        observed={"claim": attributes, "ethereum": evm_args, "mismatches": mismatches},
        incident_key="bridge-ledger:deposit:{}:{}".format(
            aggregate["generation"], deposit_id
        ),
        delivery_key="tellor-1:{}:M4:{}".format(row["height"], row["event_key"]),
        chain_point=point,
        signal="deposit_claimed",
        contract="layer.bridge",
        evidence={"deposit_id": deposit_id, "query_id": aggregate["query_id"]},
    )


def _evaluate_layer_withdraw(row, block_row, store):
    attributes = row_attributes(row)
    try:
        withdraw_id = _decimal(attributes["withdraw_id"], "withdraw ID")
        sender = attributes["sender"]
        recipient = normalize_address("0x" + attributes["recipient_evm_address"].removeprefix("0x"))
        amount = _loya(attributes["amount"])
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("tokens_withdrawn attributes are incomplete") from error
    aggregates = [
        aggregate
        for aggregate in _bridge_aggregates(
            store, to_layer=False, identity=withdraw_id, height=int(row["height"])
        )
        if aggregate["generation"] == "v2"
    ]
    mismatches = []
    if len(aggregates) != 1:
        mismatches.append("same-block V2 withdrawal aggregate count is not one")
        aggregate = aggregates[0] if aggregates else None
    else:
        aggregate = aggregates[0]
    if aggregate:
        if aggregate["layer_address"] != sender:
            mismatches.append("Layer withdrawal sender differs")
        if aggregate["evm_address"] != recipient:
            mismatches.append("EVM withdrawal recipient differs")
        if aggregate["amount"] != amount or aggregate["tip"] != 0:
            mismatches.append("withdrawal amount or tip differs")
        if aggregate["query_id"] != bridge_query_id("TRBBridgeV2", False, withdraw_id):
            mismatches.append("withdrawal query ID differs")
    if not mismatches:
        return None
    point = _layer_point(block_row)
    return Finding(
        slug="bridge-ledger-integrity",
        predicate="Tellor Layer withdrawal lacks one exact same-block V2 aggregate",
        expected={
            "withdraw_id": withdraw_id,
            "sender": sender,
            "recipient": recipient,
            "amount_loya": amount,
            "tip": 0,
        },
        observed={"aggregate": aggregate, "mismatches": mismatches},
        incident_key="bridge-ledger:withdraw:v2:{}".format(withdraw_id),
        delivery_key="tellor-1:{}:M4:{}".format(row["height"], row["event_key"]),
        chain_point=point,
        signal="tokens_withdrawn",
        contract="layer.bridge",
        evidence={"withdraw_id": withdraw_id},
    )


def _evaluate_evm_withdraw(
    event, transfers, chain_point, ethereum, store, transaction
):
    generation = "v2" if event.address == TOKEN_BRIDGE_V2 else "v1"
    identity = int(event.args["_depositId"])
    attestation = _withdraw_attestation(
        transaction, event.address, identity, ethereum
    )
    mismatches = []
    aggregates = [
        aggregate
        for aggregate in _bridge_aggregates(
            store,
            to_layer=False,
            identity=identity,
            timestamp=attestation["report_timestamp"],
        )
        if aggregate["generation"] == generation
    ]
    aggregate = aggregates[0] if len(aggregates) == 1 else None
    if not aggregates:
        raise Unresolved("historical Layer withdrawal aggregate is not available yet")
    if len(aggregates) != 1:
        mismatches.append("historical Layer withdrawal aggregate count is not one")
    aggregate_for_join = aggregates[0]
    candidates = []
    for row in store.layer_events(
        "tokens_withdrawn", height=aggregate_for_join["height"]
    ):
        attrs = row_attributes(row)
        try:
            candidate_id = _decimal(attrs.get("withdraw_id", -1), "withdraw ID")
        except Unresolved:
            continue
        if candidate_id == identity:
            candidates.append({"height": int(row["height"]), **attrs})
    if not candidates:
        raise Unresolved("Tellor Layer withdrawal counterpart is not available yet")
    if len(candidates) != 1:
        mismatches.append("same-block finalized Layer withdrawal count is not one")
    layer = candidates[0]
    amount_wei = _loya(layer["amount"]) * 10**12
    recipient = normalize_address(event.args["_recipient"])
    paid = sum(
        int(transfer.args["_value"])
        for transfer in transfers
        if normalize_address(transfer.args["_from"]) == event.address
        and normalize_address(transfer.args["_to"]) == recipient
    )
    parent_claim = _tokens_to_claim(
        ethereum, event.address, recipient, chain_point.number - 1
    )
    current_claim = _tokens_to_claim(
        ethereum, event.address, recipient, chain_point.number
    )
    pending_increase = current_claim - parent_claim
    expected_query_id = bridge_query_id(
        "TRBBridgeV2" if generation == "v2" else "TRBBridge",
        False,
        identity,
    )
    if attestation["identity"] != identity:
        mismatches.append("withdraw calldata identity differs from event")
    if attestation["query_id"] != expected_query_id:
        mismatches.append("withdraw calldata query ID or generation differs")
    if aggregate:
        if attestation["query_id"] != aggregate["query_id"]:
            mismatches.append("withdraw calldata query ID differs from aggregate")
        if attestation["report_value"] != aggregate["value_hex"]:
            mismatches.append("withdraw calldata report value differs from aggregate")
        if attestation["report_power"] != aggregate["power"]:
            mismatches.append("withdraw calldata aggregate power differs")
    if str(event.args["_sender"]) != layer["sender"]:
        mismatches.append("withdrawal sender differs")
    if recipient != normalize_address(
        "0x" + layer["recipient_evm_address"].removeprefix("0x")
    ):
        mismatches.append("withdrawal recipient differs")
    if int(event.args["_amount"]) != paid:
        mismatches.append("Withdraw event amount differs from the same-transaction transfer")
    if paid + pending_increase != amount_wei or pending_increase < 0:
        mismatches.append("paid transfer plus pending-claim increase differs from full amount")
    details = None
    if generation == "v2" and pending_increase > 0:
        decoded = decode_call(
            ["uint256", "address", "uint256", "uint256", "uint256"],
            ethereum.eth_call(
                TOKEN_BRIDGE_V2,
                call_data("withdrawDetails(uint256)", ["uint256"], [identity]),
                chain_point.number,
            ),
        )
        details = {
            "withdraw_id": int(decoded[0]),
            "recipient": normalize_address(decoded[1]),
            "amount": int(decoded[2]),
            "pending_amount": int(decoded[3]),
            "last_verified_time": int(decoded[4]),
        }
        if (
            details["withdraw_id"] != identity
            or details["recipient"] != recipient
            or details["amount"] != amount_wei
            or details["pending_amount"] != pending_increase
        ):
            mismatches.append("V2 withdrawDetails differs from the partial withdrawal")
    if not mismatches:
        return None
    return _m4_evm_finding(
        event,
        chain_point,
        "Ethereum withdrawal does not match the exact historical Layer withdrawal",
        {
            "layer": layer,
            "full_amount_wei": amount_wei,
            "paid_plus_pending": amount_wei,
        },
        {
            "event": event.args,
            "paid": paid,
            "tokens_to_claim_before": parent_claim,
            "tokens_to_claim_after": current_claim,
            "withdraw_details": details,
            "attestation": attestation,
            "aggregate": aggregate,
            "mismatches": mismatches,
        },
        "withdraw",
        generation,
        identity,
    )


def _evaluate_extra_claim(event, transfers, chain_point, ethereum):
    recipient = normalize_address(event.args["_recipient"])
    amount = int(event.args["_amount"])
    paid = sum(
        int(transfer.args["_value"])
        for transfer in transfers
        if normalize_address(transfer.args["_from"]) == event.address
        and normalize_address(transfer.args["_to"]) == recipient
    )
    parent = _tokens_to_claim(ethereum, event.address, recipient, chain_point.number - 1)
    current = _tokens_to_claim(ethereum, event.address, recipient, chain_point.number)
    mismatches = []
    if paid != amount:
        mismatches.append("same-transaction bridge transfer differs from claim")
    if parent - current != amount:
        mismatches.append("tokensToClaim decrease differs from claim")
    details = None
    if event.address == TOKEN_BRIDGE_V2:
        identity = int(event.args["_withdrawId"])
        before_details = _withdraw_details(
            ethereum, identity, chain_point.number - 1
        )
        after_details = _withdraw_details(
            ethereum, identity, chain_point.number
        )
        details = {"before": before_details, "after": after_details}
        if (
            before_details["withdraw_id"] != identity
            or after_details["withdraw_id"] != identity
            or before_details["recipient"] != recipient
            or after_details["recipient"] != recipient
            or before_details["amount"] != after_details["amount"]
            or before_details["pending_amount"]
            - after_details["pending_amount"]
            != amount
        ):
            mismatches.append("V2 withdrawDetails pending decrease differs from claim")
    if not mismatches:
        return None
    if event.address == TOKEN_BRIDGE_V1:
        identity = "{}:{}".format(event.transaction_hash, event.log_index)
        generation = "v1"
    else:
        identity = int(event.args["_withdrawId"])
        generation = "v2"
    return _m4_evm_finding(
        event,
        chain_point,
        "pending withdrawal claim does not reconcile with state and transfer",
        {"amount": amount, "tokens_to_claim_decrease": amount, "transfer": amount},
        {
            "paid": paid,
            "tokens_to_claim_before": parent,
            "tokens_to_claim_after": current,
            "withdraw_details": details,
            "mismatches": mismatches,
        },
        "claim",
        generation,
        identity,
    )


def bridge_query_id(query_type, to_layer, identity):
    query_data = encode(
        ["string", "bytes"],
        [query_type, encode(["bool", "uint256"], [bool(to_layer), int(identity)])],
    )
    return "0x" + keccak(query_data).hex()


def _bridge_aggregates(store, *, to_layer, identity, height=None, timestamp=None):
    values = []
    for row in store.layer_events("aggregate_report", height):
        try:
            aggregate = parse_bridge_aggregate(row)
        except (Unresolved, ValueError):
            continue
        if (
            aggregate
            and aggregate["to_layer"] == to_layer
            and aggregate["id"] == identity
            and (timestamp is None or aggregate["timestamp"] == int(timestamp))
        ):
            values.append(aggregate)
    return values


def _claim_timestamp(row, block_row, store, layer_client, deposit_id):
    if row["source"].startswith("txs_results:") and row["transaction_hash"]:
        payload = layer_client.transaction(row["transaction_hash"])
        try:
            response = payload["tx_response"]
            if int(response["height"]) != int(row["height"]):
                raise ValueError("transaction height differs")
            if str(response["txhash"]).upper() != str(row["transaction_hash"]).upper():
                raise ValueError("transaction hash differs")
            if int(response.get("code", 0)) != 0:
                raise ValueError("transaction was not successful")
            messages = payload["tx"]["body"]["messages"]
        except (KeyError, TypeError, ValueError) as error:
            raise Unresolved("Layer claim transaction response is incomplete") from error
        timestamps = []
        for message in messages:
            if not str(message.get("@type", "")).endswith(
                ".MsgClaimDepositsRequest"
            ):
                continue
            ids = message.get("deposit_ids") or []
            values = message.get("timestamps") or []
            if len(ids) != len(values):
                raise Unresolved("Layer claim message arrays have different lengths")
            for identity, timestamp in zip(ids, values):
                if _decimal(identity, "deposit ID") == deposit_id:
                    timestamps.append(_decimal(timestamp, "aggregate timestamp"))
        if len(timestamps) != 1:
            raise Unresolved("Layer claim message does not identify one timestamp")
        return timestamps[0]

    eligible_before = int(block_row["block_time_ms"]) - 12 * 60 * 60 * 1000
    candidates = [
        aggregate
        for aggregate in _bridge_aggregates(
            store, to_layer=True, identity=deposit_id
        )
        if aggregate["timestamp"] <= eligible_before
    ]
    timestamps = sorted({item["timestamp"] for item in candidates})
    if len(timestamps) != 1:
        raise Unresolved(
            "EndBlock deposit claim does not resolve to one eligible aggregate timestamp"
        )
    return timestamps[0]


def _outflow_has_exact_cause(transfer, bridge_events):
    source = normalize_address(transfer.args["_from"])
    recipient = normalize_address(transfer.args["_to"])
    amount = int(transfer.args["_value"])
    for event in bridge_events:
        if event.address != source:
            continue
        if event.name in {"Withdraw", "ExtraWithdrawClaimed"}:
            if (
                normalize_address(event.args["_recipient"]) == recipient
                and int(event.args["_amount"]) == amount
            ):
                return True
        elif event.name == "PauseRefunded":
            if normalize_address(event.args["_proposer"]) == recipient and amount == 10_000 * 10**18:
                return True
        elif event.name == "PauseApproved":
            if recipient == DEAD_ADDRESS and amount == 10_000 * 10**18:
                return True
    return False


def _withdraw_attestation(transaction, bridge, identity, ethereum):
    if not isinstance(transaction, dict):
        raise Unresolved("withdraw transaction calldata is unavailable")
    signature = (
        "withdrawFromLayer("
        "(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256),"
        "(address,uint256)[],(uint8,bytes32,bytes32)[],uint256)"
    )
    selector = bytes(function_selector(signature))
    calls = []
    try:
        if normalize_address(transaction.get("to")) == bridge:
            calls.append({"input": transaction.get("input", "0x")})
    except (TypeError, ValueError):
        pass
    if not calls:
        try:
            transaction_hash = transaction["hash"]
        except KeyError as error:
            raise Unresolved("withdraw transaction hash is unavailable") from error
        trace = ethereum.trace(transaction_hash)
        stack = [trace]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                raise Unresolved("withdraw call trace node is invalid")
            try:
                target_matches = normalize_address(node.get("to")) == bridge
            except (TypeError, ValueError):
                target_matches = False
            raw_input = str(node.get("input", ""))
            if (
                not node.get("error")
                and target_matches
                and raw_input.startswith("0x")
            ):
                calls.append(node)
            children = node.get("calls") or []
            if not isinstance(children, list):
                raise Unresolved("withdraw call trace children are invalid")
            stack.extend(children)
    types = [
        "(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256)",
        "(address,uint256)[]",
        "(uint8,bytes32,bytes32)[]",
        "uint256",
    ]
    decoded = []
    for call in calls:
        try:
            raw = unhex(str(call.get("input", "")).lower())
        except (TypeError, ValueError) as error:
            raise Unresolved("withdraw calldata is not strict hex") from error
        if len(raw) < 4 or raw[:4] != selector:
            continue
        try:
            values = decode(types, raw[4:], strict=True)
            if encode(types, values) != raw[4:]:
                raise ValueError("non-canonical calldata")
            attest, _validators, _signatures, call_identity = values
            query_id, report, attestation_timestamp = attest
            report_value, report_timestamp, power, previous, following, consensus = report
            decoded.append(
                {
                    "identity": int(call_identity),
                    "query_id": hx(query_id),
                    "report_value": hx(report_value),
                    "report_timestamp": int(report_timestamp),
                    "report_power": int(power),
                    "previous_timestamp": int(previous),
                    "next_timestamp": int(following),
                    "last_consensus_timestamp": int(consensus),
                    "attestation_timestamp": int(attestation_timestamp),
                }
            )
        except Exception as error:
            raise Unresolved("withdraw calldata is not canonical ABI") from error
    matches = [item for item in decoded if item["identity"] == int(identity)]
    if len(matches) != 1:
        raise Unresolved("withdraw event does not map to one exact executed calldata")
    return matches[0]


def _m4_evm_finding(event, point, predicate, expected, observed, kind, generation, identity):
    return Finding(
        slug="bridge-ledger-integrity",
        predicate=predicate,
        expected=expected,
        observed=observed,
        incident_key="bridge-ledger:{}:{}:{}".format(kind, generation, identity),
        delivery_key="1:{}:M4:{}".format(event.transaction_hash, event.log_index),
        chain_point=point,
        signal=event.signature,
        contract=event.address,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        evidence={"bridge_identity": identity, "generation": generation},
    )


def _tokens_to_claim(client, bridge, recipient, block):
    return int(
        decode_call(
            ["uint256"],
            client.eth_call(
                bridge,
                call_data("tokensToClaim(address)", ["address"], [recipient]),
                block,
            ),
        )[0]
    )


def _withdraw_details(client, identity, block):
    decoded = decode_call(
        ["uint256", "address", "uint256", "uint256", "uint256"],
        client.eth_call(
            TOKEN_BRIDGE_V2,
            call_data("withdrawDetails(uint256)", ["uint256"], [int(identity)]),
            block,
        ),
    )
    return {
        "withdraw_id": int(decoded[0]),
        "recipient": normalize_address(decoded[1]),
        "amount": int(decoded[2]),
        "pending_amount": int(decoded[3]),
        "last_verified_time": int(decoded[4]),
    }


def _parse_historical_report(payload):
    try:
        aggregate = payload["aggregate"]
        return {
            "query_id": _layer_hex32(aggregate["query_id"]),
            "aggregate_value": _layer_hex(aggregate["aggregate_value"], "aggregate value").hex(),
            "aggregate_power": int(aggregate["aggregate_power"]),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("Tellor Layer historical aggregate is incomplete") from error


def _layer_report_matches(value, query_id, event_value, power):
    return (
        value["query_id"] == query_id
        and "0x" + value["aggregate_value"] == event_value
        and value["aggregate_power"] == power
    )


def _layer_point(block_row):
    return ChainPoint(
        "tellor-1",
        int(block_row["height"]),
        block_row["block_hash"],
        int(block_row["block_time_ms"]) // 1000,
    )


def _evm_row(row):
    return {"row": row, "args": json.loads(row["args_json"])}


def _decode_exact(types, raw):
    try:
        value = decode(types, raw, strict=True)
        if encode(types, value) != raw:
            raise ValueError("non-canonical ABI")
        return value
    except Exception as error:
        raise ValueError("non-canonical ABI") from error


def _layer_hex(value, label):
    text = str(value)
    if text.startswith("0x") or len(text) % 2 or not LOWER_HEX_RE.fullmatch(text):
        raise Unresolved("Layer {} is not strict lowercase hex".format(label))
    return bytes.fromhex(text)


def _layer_hex32(value):
    raw = _layer_hex(value, "query ID")
    if len(raw) != 32:
        raise Unresolved("Layer query ID is not bytes32")
    return "0x" + raw.hex()


def _decimal(value, label):
    text = str(value)
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise Unresolved("Layer {} is not canonical decimal".format(label))
    return int(text)


def _loya(value):
    match = LOYA_RE.fullmatch(str(value))
    if match is None:
        raise Unresolved("Layer coin is not canonical loya")
    return int(match.group("amount"))


def _claimed_true(payload):
    for key in ("claimed", "deposit_claimed", "is_claimed"):
        if payload.get(key) is True:
            return True
        if isinstance(payload.get(key), dict) and payload[key].get("claimed") is True:
            return True
    return False
