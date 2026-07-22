"""One normalized alert handler per Tellor monitor."""

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import median

from tellor_lib import (
    ADDRESS_REPORTS, EVM_CALL_RPCS, EVMCallNotVerified,
    MIN_TRUSTED_PRICE_SOURCES, TELLORFLEX_PRICE_TOLERANCE,
    TRUSTED_PRICE_ASSETS, abi_decode_exact,
    decode_evm_response, decode_query_data, describe_bytes,
    fetch_trusted_prices, hex_bytes, historical_evm_call_reference, now_utc,
    oracle_value, parse_structured_arg, query_id_label, send_alert,
    signature_count, spot_value, unix_utc, usd, wei,
)

STAKING_EVENTS = {
    "NewStaker": "Stake Deposited!",
    "StakeWithdrawRequested": "Stake withdraw requested!",
    "StakeWithdrawn": "Stake withdrawn!",
}

BRIDGE_HEADLINES = {
    "addStakingRewards": "AddStakingRewards called !",
    "claimExtraWithdraw": "ClaimExtraWithdrawalFee called !",
    "pauseBridge": "PauseBridge called !",
    "unpauseBridge": "UnpauseBridge called !",
}

OUTCOME_NORMAL = "✅ Looks normal"
OUTCOME_RECEIVED = "👀 Report received"
OUTCOME_NOT_VERIFIED = "⚠️ NOT VERIFIED"
OUTCOME_DISPUTE = "🚨 POTENTIAL DISPUTE"
REPORT_OUTCOMES = {
    OUTCOME_NORMAL,
    OUTCOME_RECEIVED,
    OUTCOME_NOT_VERIFIED,
    OUTCOME_DISPUTE,
}
DISPUTABLE_ALERT_OUTCOMES = frozenset({OUTCOME_NOT_VERIFIED, OUTCOME_DISPUTE})

DISPUTABLE_MONITOR = "TellorFlex Disputable Value"
NEW_REPORT_SIGNATURE = "NewReport(bytes32,uint256,bytes,uint256,bytes,address)"
AMPL_QUERY_ID = "0x0d12ad49193163bbbeff4e6db8294ced23ff8605359fd666799d4e25a3aa0e3a"
USPCE_QUERY_ID = "0x612ec1d9cee860bb87deb6370ed0ae43345c9302c085c1dfc4c207cbec2970d7"


class MalformedReport(ValueError):
    """A known report or the NewReport envelope is structurally invalid."""


@dataclass(frozen=True)
class ReportOutcome:
    label: str
    details: tuple


def _outcome(label, *details):
    if label not in REPORT_OUTCOMES:
        raise ValueError(f"unknown report outcome: {label}")
    return ReportOutcome(label, tuple(details))


def _code(value):
    """Keep dynamic values inside one Discord inline-code span."""
    return (
        str(value)
        .replace("`", "ˋ")
        .replace("\r", r"\r")
        .replace("\n", r"\n")
    )


def _field(label, value):
    return f"{label}: `{_code(value)}`"


def _notify(m, headline, *details):
    lines = [f"**{headline}**", f"> Network: `{m.network}`"]
    for detail in details:
        lines.extend(f"> {line}" for line in str(detail).splitlines())
    lines.extend((
        f"> Observed: `{now_utc()}`",
        f"> [View transaction]({m.tx_link})",
    ))
    send_alert(m, "\n".join(lines))


def handle_staking(m):
    name = m.signature.split("(")[0]
    event = STAKING_EVENTS[name]
    args = m.arg_map()
    address = args["_staker"]
    if name == "StakeWithdrawn":
        amount = f"check withdraw request for {address}"
    else:
        amount = wei(args["_amount"])
    _notify(m, event, _field("Address", address), _field("Amount", amount))


def handle_token_bridge(m):
    name = m.signature.split("(")[0]
    headline = BRIDGE_HEADLINES[name]
    args = m.arg_map()
    if name == "addStakingRewards":
        address, amount = "check tx", f"{wei(args['_amount'])} trb"
    elif name == "claimExtraWithdraw":
        address, amount = args["_recipient"], "check tx"
    else:
        address, amount = "n/a", "n/a"
    _notify(m, headline, _field("Address", address), _field("Amount", amount))


def handle_deposit_to_layer(m):
    args = m.arg_map()
    _notify(
        m,
        "DepositToLayer called !",
        _field("Layer recipient", args["_layerRecipient"]),
        _field("Amount", f"{wei(args['_amount'])} trb"),
        _field("Tip", f"{wei(args['_tip'])} trb"),
    )


def handle_withdraw_from_layer(m):
    args = m.arg_map()  # Withdraw event: _depositId, _sender, _recipient, _amount
    _notify(
        m,
        "WithdrawFromLayer called !",
        _field("Deposit ID", args["_depositId"]),
        _field("Amount", f"{wei(args['_amount'])} trb"),
        _field("Sender", args["_sender"]),
        _field("Recipient", args["_recipient"]),
    )


def handle_address_updates(m):
    if m.signature != "updateStakeAmount()":
        raise ValueError("Tellor Address Updates received an unexpected match")
    _notify(m, "Stake amount updated (updateStakeAmount was called)")


def _hex_of_size(value, size, label):
    try:
        raw = hex_bytes(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise MalformedReport(f"{label} is not valid hex") from error
    if len(raw) != size:
        raise MalformedReport(f"{label} is not {size} bytes")
    return raw


def _new_report_args(args):
    required = ("_queryId", "_time", "_value", "_nonce", "_queryData", "_reporter")
    missing = [name for name in required if name not in args]
    if missing:
        raise MalformedReport("NewReport is missing required arguments")
    query_id = str(args["_queryId"]).lower()
    _hex_of_size(query_id, 32, "query ID")
    _hex_of_size(args["_reporter"], 20, "reporter")
    try:
        report_time = int(args["_time"])
        nonce = int(args["_nonce"])
    except (TypeError, ValueError) as error:
        raise MalformedReport("report time or nonce is invalid") from error
    if report_time <= 0 or nonce < 0:
        raise MalformedReport("report time or nonce is out of range")
    try:
        value = hex_bytes(args["_value"])
    except (AttributeError, TypeError, ValueError) as error:
        raise MalformedReport("report value is not valid hex") from error
    return {
        **args,
        "_queryId": query_id,
        "_time": report_time,
        "_nonce": nonce,
        "_value_bytes": value,
    }


def _decode_exact(types, data, label):
    try:
        return abi_decode_exact(types, data)
    except (UnicodeError, ValueError) as error:
        raise MalformedReport(f"{label} has malformed ABI data") from error


def _decimal_value(raw, label):
    if len(raw) != 32:
        raise MalformedReport(f"{label} response is not one 32-byte value")
    return Decimal(int.from_bytes(raw, "big")) / Decimal(10**18)


def _decimal_text(value):
    shown = format(value, "f")
    return shown.rstrip("0").rstrip(".") if "." in shown else shown


def _classify_spot(args, params, price_fetcher):
    asset, currency = _decode_exact(["string", "string"], params, "SpotPrice query")
    if not asset or not currency:
        raise MalformedReport("SpotPrice asset or currency is empty")
    feed = f"{asset} / {currency}".upper()
    reported = _decimal_value(args["_value_bytes"], "SpotPrice")
    price_asset = TRUSTED_PRICE_ASSETS.get(args["_queryId"])
    if price_asset is None:
        return _outcome(
            OUTCOME_NOT_VERIFIED,
            ("Feed", feed),
            ("Reported", usd(reported)),
            ("Reason", "no trusted-source catalog entry for this query ID"),
        )

    expected_pair = tuple(part.lower() for part in price_asset["label"].split(" / "))
    if (asset, currency) != expected_pair:
        raise MalformedReport("SpotPrice query parameters do not match the known query ID")
    try:
        available = price_fetcher(price_asset)
    except Exception:
        return _outcome(
            OUTCOME_NOT_VERIFIED,
            ("Feed", feed),
            ("Reported", usd(reported)),
            ("Reason", "trusted-price source request failed"),
        )
    usable = []
    for price in available or ():
        try:
            candidate = Decimal(str(price))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if candidate.is_finite() and candidate > 0:
            usable.append(candidate)
    required_sources = price_asset.get("min_sources", MIN_TRUSTED_PRICE_SOURCES)
    if len(usable) < required_sources:
        return _outcome(
            OUTCOME_NOT_VERIFIED,
            ("Feed", feed),
            ("Reported", usd(reported)),
            ("Trusted sources", len(usable)),
            ("Reason", f"need {required_sources} usable trusted price sources"),
        )
    trusted = median(usable)
    difference = abs(reported - trusted) / trusted
    label = (
        OUTCOME_DISPUTE
        if difference > Decimal(str(TELLORFLEX_PRICE_TOLERANCE))
        else OUTCOME_NORMAL
    )
    return _outcome(
        label,
        ("Feed", feed),
        ("Reported", usd(reported)),
        ("Trusted median", usd(trusted)),
        ("Trusted sources", len(usable)),
        ("Difference", f"{difference * 100:.6f}%"),
    )


def _classify_evm_call(args, params, evm_reference, environ):
    chain_id, contract, calldata = _decode_exact(
        ["uint256", "address", "bytes"], params, "EVMCall query"
    )
    try:
        submitted, source_timestamp = decode_evm_response(args["_value"])
    except (AttributeError, TypeError, UnicodeError, ValueError) as error:
        raise MalformedReport("EVMCall response has malformed ABI data") from error
    selector = "0x" + calldata[:4].hex() if len(calldata) >= 4 else "short calldata"
    base = (
        ("Chain", chain_id),
        ("Target", contract),
        ("Call selector", selector),
        ("Source block time", unix_utc(source_timestamp)),
        ("Submitted", describe_bytes(submitted, "EVMCall return data")),
    )
    rpc_var = EVM_CALL_RPCS.get(chain_id)
    rpc_url = environ.get(rpc_var or "")
    if rpc_var is None:
        return _outcome(OUTCOME_NOT_VERIFIED, *base, ("Reason", "unsupported chain ID"))
    if not rpc_url:
        return _outcome(
            OUTCOME_NOT_VERIFIED, *base,
            ("Reason", f"no RPC configured for chain ID {chain_id}"),
        )
    try:
        expected, block = evm_reference(
            rpc_url, contract, calldata, source_timestamp
        )
    except EVMCallNotVerified as error:
        return _outcome(OUTCOME_NOT_VERIFIED, *base, ("Reason", str(error)))
    except Exception:
        return _outcome(
            OUTCOME_NOT_VERIFIED, *base,
            ("Reason", "historical EVMCall validation failed"),
        )
    checked = base + (
        ("Historical block", block["number"]),
        ("Historical block hash", block["hash"]),
        ("Expected", describe_bytes(expected, "historical EVMCall return data")),
    )
    return _outcome(
        OUTCOME_NORMAL if submitted == expected else OUTCOME_DISPUTE,
        *checked,
    )


def _classify_phantom(args, qtype, params):
    known = {
        "AutopayAddresses": (
            next(key for key, value in ADDRESS_REPORTS.items() if value[1] == "AutopayAddresses"),
            "address[]",
        ),
        "TellorOracleAddress": (
            next(key for key, value in ADDRESS_REPORTS.items() if value[1] == "TellorOracleAddress"),
            "address",
        ),
        "AmpleforthCustomSpotPrice": (AMPL_QUERY_ID, "ufixed256x18"),
        "AmpleforthUSPCE": (USPCE_QUERY_ID, "ufixed256x18"),
    }
    expected_query_id, response_type = known[qtype]
    if args["_queryId"] != expected_query_id:
        raise MalformedReport(f"{qtype} query ID does not match its canonical query")
    phantom = _decode_exact(["bytes"], params, f"{qtype} query")[0]
    if phantom:
        raise MalformedReport(f"{qtype} phantom parameter is not empty")
    if response_type == "ufixed256x18":
        value = _decimal_value(args["_value_bytes"], qtype)
        shown = _decimal_text(value)
    else:
        value = _decode_exact([response_type], args["_value_bytes"], f"{qtype} response")[0]
        shown = ", ".join(value) if response_type == "address[]" else value
    return _outcome(
        OUTCOME_NORMAL,
        ("Feed", qtype),
        ("Value", shown or "empty address list"),
        ("Validation", "canonical structure"),
    )


def classify_report(args, price_fetcher=None, evm_reference=None, environ=None):
    """Classify one decoded NewReport without performing alert delivery."""
    price_fetcher = price_fetcher or fetch_trusted_prices
    evm_reference = evm_reference or historical_evm_call_reference
    environ = os.environ if environ is None else environ
    args = _new_report_args(args)
    try:
        qtype, params = decode_query_data(args["_queryData"])
    except (AttributeError, TypeError, UnicodeError, ValueError) as error:
        raise MalformedReport("queryData has malformed ABI data") from error
    if not qtype:
        raise MalformedReport("query type is empty")

    known_query_ids = {
        **{key: value[1] for key, value in ADDRESS_REPORTS.items()},
        AMPL_QUERY_ID: "AmpleforthCustomSpotPrice",
        USPCE_QUERY_ID: "AmpleforthUSPCE",
    }
    if args["_queryId"] in TRUSTED_PRICE_ASSETS and qtype != "SpotPrice":
        raise MalformedReport("known SpotPrice query ID carried another query type")
    expected_qtype = known_query_ids.get(args["_queryId"])
    if expected_qtype is not None and qtype != expected_qtype:
        raise MalformedReport("known structural query ID carried another query type")

    if qtype == "SpotPrice":
        outcome = _classify_spot(args, params, price_fetcher)
    elif qtype == "EVMCall":
        outcome = _classify_evm_call(args, params, evm_reference, environ)
    elif qtype == "TellorRNG":
        requested_after = _decode_exact(["uint256"], params, "TellorRNG query")[0]
        if requested_after <= 0 or len(args["_value_bytes"]) != 32:
            raise MalformedReport("TellorRNG timestamp or bytes32 response is invalid")
        outcome = _outcome(
            OUTCOME_NORMAL,
            ("Feed", qtype),
            ("Requested after", unix_utc(requested_after)),
            ("Value", "0x" + args["_value_bytes"].hex()),
            ("Validation", "canonical structure"),
        )
    elif qtype in {
        "AutopayAddresses", "TellorOracleAddress",
        "AmpleforthCustomSpotPrice", "AmpleforthUSPCE",
    }:
        outcome = _classify_phantom(args, qtype, params)
    else:
        outcome = _outcome(
            OUTCOME_RECEIVED,
            ("Feed", qtype),
            ("Value", describe_bytes(args["_value_bytes"], "untyped report data")),
            ("Validation", "unknown query type; no correctness claim"),
        )
    return outcome, args, qtype


def handle_disputable_value(m):
    """Contain report-data errors and emit exactly one canonical outcome."""
    args = m.arg_map()
    qtype = "unavailable"
    validated = None
    try:
        if m.signature != NEW_REPORT_SIGNATURE:
            raise MalformedReport("unexpected event signature")
        outcome, validated, qtype = classify_report(args)
    except MalformedReport as error:
        outcome = _outcome(
            OUTCOME_DISPUTE,
            ("Reason", str(error)),
            ("Validation", "malformed known report data"),
        )
    except Exception:
        outcome = _outcome(
            OUTCOME_NOT_VERIFIED,
            ("Reason", "classifier failed unexpectedly"),
        )

    context = validated or args
    details = [("Query type", qtype)]
    for label, key in (
        ("Query ID", "_queryId"),
        ("Report time", "_time"),
        ("Reporter", "_reporter"),
        ("Nonce", "_nonce"),
    ):
        if key in context:
            value = unix_utc(context[key]) if key == "_time" else context[key]
            details.append((label, value))
    details.extend(outcome.details)
    if outcome.label not in DISPUTABLE_ALERT_OUTCOMES:
        return
    _notify(m, outcome.label, *(_field(label, value) for label, value in details))


def _proof_details(args):
    validators = parse_structured_arg(args["_currentValidatorSet"])
    signatures = parse_structured_arg(args["_sigs"])
    total_power = sum(int(validator[1]) for validator in validators)
    return validators, signatures, total_power, signature_count(signatures)


def handle_update_oracle_data(m):
    args = m.arg_map()
    attest_data = parse_structured_arg(args["_attestData"])
    if len(attest_data) != 3 or len(attest_data[1]) != 6:
        raise ValueError("monitor returned malformed oracle attestation data")
    query_id, report, attestation_timestamp = attest_data
    value_hex, report_timestamp, aggregate_power, previous_timestamp, next_timestamp, last_consensus = report
    validators, signatures, validator_power, present_signatures = _proof_details(args)
    mode = "Consensus" if int(report_timestamp) == int(last_consensus) else "Optimistic"
    _notify(
        m,
        "Oracle data updated",
        _field("Feed", query_id_label(query_id)),
        _field("Value", oracle_value(query_id, value_hex)),
        _field("Query ID", query_id),
        _field("Report mode", mode),
        _field("Report time", unix_utc(report_timestamp, milliseconds=True)),
        _field("Previous report", unix_utc(previous_timestamp, milliseconds=True)),
        _field("Next report", unix_utc(next_timestamp, milliseconds=True)),
        _field("Last consensus", unix_utc(last_consensus, milliseconds=True)),
        _field("Attested", unix_utc(attestation_timestamp, milliseconds=True)),
        _field("Aggregate power", f"{int(aggregate_power):,}"),
        f"Validators: `{len(validators)}` (total power `{validator_power:,}`)",
        _field("Signatures", f"{present_signatures} of {len(signatures)}"),
    )


def handle_update_validator_set(m):
    args = m.arg_map()
    validators, signatures, validator_power, present_signatures = _proof_details(args)
    _notify(
        m,
        "Validator set updated",
        _field("New power threshold", f"{int(args['_newPowerThreshold']):,}"),
        _field(
            "New validator timestamp",
            unix_utc(args["_newValidatorTimestamp"], milliseconds=True),
        ),
        _field("New validator-set hash", args["_newValidatorSetHash"]),
        f"Current validators: `{len(validators)}` (total power `{validator_power:,}`)",
        _field("Signatures", f"{present_signatures} of {len(signatures)}"),
        "New membership is represented by the hash; it is not included in this call.",
    )


def handle_guardian_reset_validator_set(m):
    args = m.arg_map()
    _notify(
        m,
        "Guardian reset validator set",
        _field("Power threshold", f"{int(args['_powerThreshold']):,}"),
        _field(
            "Validator timestamp",
            unix_utc(args["_validatorTimestamp"], milliseconds=True),
        ),
        _field("Validator-set hash", args["_validatorSetHash"]),
    )


def handle_smoke(m):
    """Large USDC transfers are never delivered to Discord."""
    return


HANDLERS = {
    "Tellor Staking": handle_staking,
    "Tellor Token Bridge": handle_token_bridge,
    "Tellor Deposit To Layer": handle_deposit_to_layer,
    "Tellor Withdraw From Layer": handle_withdraw_from_layer,
    "Tellor Address Updates": handle_address_updates,
    DISPUTABLE_MONITOR: handle_disputable_value,
    "Smoke Test USDC Transfer": handle_smoke,
    "Update Oracle Data Calls": handle_update_oracle_data,
    "Update Validator Set Calls": handle_update_validator_set,
    "Guardian Reset Validator Set Calls": handle_guardian_reset_validator_set,
}
