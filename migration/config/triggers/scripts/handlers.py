"""One handler per monitor. Each receives a tellor_lib.Match and sends (or
deliberately skips) a normalized alert while preserving the legacy monitor's
filtering and value semantics.

A handler that returns without calling send_alert drops the match silently —
that is how the legacy in-handler filtering (queryId allowlists, deviation
threshold, EVMCall-only) is preserved.
"""

import os
from statistics import median

from tellor_lib import (
    ADDRESS_REPORTS, DVM_FEEDS, DVM_TOLERANCE, EVM_CALL_RPCS,
    TRUSTED_PRICE_ASSETS, abi_decode, coincap_price, coingecko_price,
    coinmarketcap_price, decode_evm_response, decode_query_data, describe_bytes,
    eth_call, fx_usd_rate, hex_bytes, now_utc, oracle_value,
    parse_structured_arg, query_id_label, send_alert, signature_count,
    spot_value, unix_utc, usd, wei,
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
    args = m.arg_map()
    if m.signature.startswith("updateStakeAmount"):
        _notify(m, "Stake amount updated (updateStakeAmount was called)")
        return

    report = ADDRESS_REPORTS.get(args.get("_queryId", "").lower())
    if report is None:
        return  # submitValue for a queryId we don't care about
    label, expected_query_type, response_type = report
    query_type, query_params = decode_query_data(args["_queryData"])
    if query_type != expected_query_type:
        raise ValueError(
            f"{label} queryId carried unexpected query type {query_type!r}"
        )
    phantom = abi_decode(["bytes"], query_params)[0]
    if phantom:
        raise ValueError(f"{label} query carried a non-empty phantom parameter")
    raw_value = hex_bytes(args["_value"])
    try:
        value = abi_decode([response_type], raw_value)[0]
        if response_type == "address[]":
            shown = value[:12]
            details = [f"Addresses ({len(value)}):"]
            details.extend(f"- `{_code(address)}`" for address in shown)
            if len(value) > len(shown):
                details.append(f"- … {len(value) - len(shown)} more (see transaction)")
        else:
            details = [_field("Address", value)]
    except ValueError:
        details = [_field("Value", describe_bytes(raw_value, "malformed ABI address data"))]
    _notify(m, f"{label} reported", _field("Query", query_type), *details)


def handle_tellorflex_data_report(m):
    args = m.arg_map()
    value_hex = args["_value"]
    qtype, params = decode_query_data(args["_queryData"])
    padded = len(value_hex.removeprefix("0x")) == 64
    trusted = "n/a"

    if qtype == "SpotPrice":
        asset, currency = abi_decode(["string", "string"], params)
        feed = f"{asset} / {currency}".upper()
        value = usd(spot_value(value_hex))

        price_asset = TRUSTED_PRICE_ASSETS.get(args["_queryId"].lower())
        if price_asset is not None:
            _, cg_id, cmc_symbol, coincap_id = price_asset

            def try_fetch(fn, *fn_args):
                try:
                    return fn(*fn_args)
                except Exception:
                    return None

            prices = [
                try_fetch(coingecko_price, cg_id),
                try_fetch(coinmarketcap_price, cmc_symbol),
            ]
            if coincap_id:
                prices.append(try_fetch(coincap_price, coincap_id))
            available = [price for price in prices if price is not None]
            if available:
                trusted = usd(median(available))
    elif qtype == "EVMCall":
        chain_id, contract, calldata = abi_decode(["uint256", "address", "bytes"], params)
        selector = "0x" + calldata[:4].hex() if len(calldata) >= 4 else "unavailable"
        try:
            return_data, source_timestamp = decode_evm_response(value_hex)
            source_time = unix_utc(source_timestamp)
            result = describe_bytes(return_data, "untyped return data")
        except ValueError:
            source_time = "unavailable"
            result = describe_bytes(hex_bytes(value_hex), "malformed EVMCall response")
        feed = (
            f"EVMCall (chain {chain_id}, target {contract}, selector {selector})"
        )
        value = f"{result}; source block time {source_time}"
    elif qtype == "TellorRNG":
        requested_after = abi_decode(["uint256"], params)[0]
        random_value = hex_bytes(value_hex)
        if len(random_value) == 32:
            value = "0x" + random_value.hex()
        else:
            value = describe_bytes(random_value, "malformed TellorRNG value")
        feed = f"TellorRNG (requested after {unix_utc(requested_after)})"
    else:
        feed = qtype
        value = describe_bytes(hex_bytes(value_hex), "value data with an unavailable schema")

    _notify(
        m,
        "TellorFlex Data Report",
        _field("Feed", feed),
        _field("Value", value),
        _field("Trusted", trusted),
        _field("Padded", padded),
    )


def handle_dvm(m):
    args = m.arg_map()
    feed = DVM_FEEDS.get(args["_queryId"])
    if feed is None:
        return
    label, source, source_id = feed
    reported = spot_value(args["_value"])
    try:
        reference = coingecko_price(source_id) if source == "cg" else fx_usd_rate(source_id)
        if reference <= 0:
            raise ValueError(f"non-positive reference price: {reference}")
    except Exception as e:
        _notify(
            m,
            f"DVM NOT VERIFIED for {label}",
            _field("Reported", reported),
            _field("Reference source", f"{source}:{source_id}"),
            _field("Reason", e),
        )
        return
    diff = abs((reported - reference) / reference)
    if diff < DVM_TOLERANCE:
        return
    _notify(
        m,
        f"Potential Dispute for {label} !",
        _field("Reported", reported),
        _field("Expected", reference),
        _field("Difference", f"{diff * 100:.2f}%"),
    )


def handle_evm_call(m):
    args = m.arg_map()
    qtype, params = decode_query_data(args["_queryData"])
    if qtype != "EVMCall":
        return
    chain_id, contract, calldata = abi_decode(["uint256", "address", "bytes"], params)
    selector = "0x" + calldata[:4].hex() if len(calldata) >= 4 else "unavailable"
    try:
        submitted, source_timestamp = decode_evm_response(args["_value"])
        submitted_hex = "0x" + submitted.hex()
        submitted_text = describe_bytes(submitted, "untyped return data")
        source_time = unix_utc(source_timestamp)
    except Exception:
        submitted = None
        submitted_hex = None
        submitted_text = "unparseable EVMCall response"
        source_time = "unavailable"

    def alert(headline, expected, reason=None):
        details = [
            _field("Chain", chain_id),
            _field("Target", contract),
            _field("Call selector", selector),
            _field("Source block time", source_time),
            _field("Expected", expected),
            _field("Submitted", submitted_text),
        ]
        if reason:
            details.append(_field("Reason", reason))
        _notify(m, headline, *details)

    rpc_var = EVM_CALL_RPCS.get(chain_id)
    if rpc_var is None or not os.environ.get(rpc_var):
        alert(
            "EVMCall NOT VERIFIED",
            "unavailable",
            f"no RPC configured for chain ID {chain_id}",
        )
        return
    try:
        result = eth_call(os.environ[rpc_var], contract, "0x" + calldata.hex())
    except Exception as e:
        # Legacy code alerted "bad EVMCall" on RPC failure; distinguish it instead.
        alert("EVMCall NOT VERIFIED", "unavailable", f"RPC error: {e}")
        return
    if submitted is None or result.lower() != submitted_hex.lower():
        alert(
            "Potentially bad EVMCall",
            describe_bytes(hex_bytes(result), "untyped return data"),
        )


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
    args = m.arg_map()
    amount = int(args["value"]) / 1e6  # USDC has 6 decimals
    _notify(
        m,
        "Large USDC Transfer",
        _field("From", args["from"]),
        _field("To", args["to"]),
        _field("Amount", f"{amount:,.2f} USDC"),
    )


HANDLERS = {
    "Tellor Staking": handle_staking,
    "Tellor Token Bridge": handle_token_bridge,
    "Tellor Deposit To Layer": handle_deposit_to_layer,
    "Tellor Withdraw From Layer": handle_withdraw_from_layer,
    "Tellor Address Updates": handle_address_updates,
    "TellorFlex Data Report": handle_tellorflex_data_report,
    "Tellor DVM Price Deviation": handle_dvm,
    "Tellor EVMCall Validation": handle_evm_call,
    "Smoke Test USDC Transfer": handle_smoke,
    "Update Oracle Data Calls": handle_update_oracle_data,
    "Update Validator Set Calls": handle_update_validator_set,
    "Guardian Reset Validator Set Calls": handle_guardian_reset_validator_set,
}
