"""One handler per monitor. Each receives a tellor_lib.Match and sends (or
deliberately skips) the alert. Message text mirrors the legacy Defender
templates in this repo (../../<monitor>/..Template.md).

A handler that returns without calling send_alert drops the match silently —
that is how the legacy in-handler filtering (queryId allowlists, deviation
threshold, EVMCall-only) is preserved.
"""

import os

from tellor_lib import (
    ADDRESS_REPORT_IDS, DVM_FEEDS, DVM_TOLERANCE, EVM_CALL_RPCS,
    PRICE_MONITOR_ASSETS, abi_decode, coincap_price, coingecko_price,
    coinmarketcap_price, decode_query_data, eth_call, fx_usd_rate, hex_bytes,
    now_utc, send_alert, spot_value, usd, wei,
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


def handle_staking(m):
    name = m.signature.split("(")[0]
    event = STAKING_EVENTS[name]
    args = m.arg_map()
    address = args["_staker"]
    if name == "StakeWithdrawn":
        amount = f"check withdraw request for {address}"
    else:
        amount = wei(args["_amount"])
    send_alert(m, f"[{event}]({m.tx_link})\n`Address: {address}`\n`Amount: {amount}`")


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
    send_alert(m, f"[{headline}]({m.tx_link})\n\n`Address: {address}`\n`Amount: {amount}`")


def handle_deposit_to_layer(m):
    args = m.arg_map()
    send_alert(m, (
        f"[DepositToLayer called !]({m.tx_link})\n\n"
        f"Layer Recipient: `{args['_layerRecipient']}`\n"
        f"Amount: `{wei(args['_amount'])} trb`\n"
        f"Tip: `{wei(args['_tip'])} trb`"
    ))


def handle_withdraw_from_layer(m):
    args = m.arg_map()  # Withdraw event: _depositId, _sender, _recipient, _amount
    send_alert(m, (
        f"[WithdrawFromLayer called !]({m.tx_link})\n\n"
        f"Deposit ID: `{args['_depositId']}`\n"
        f"Amount: `{wei(args['_amount'])} trb`\n"
        f"Sender: `{args['_sender']}`\n"
        f"Recipient: `{args['_recipient']}`"
    ))


def handle_address_updates(m):
    args = m.arg_map()
    if m.signature.startswith("updateStakeAmount"):
        message, data = "updateStakeAmount", ""
    else:
        message = ADDRESS_REPORT_IDS.get(args.get("_queryId", ""))
        if message is None:
            return  # submitValue for a queryId we don't care about
        data = args["_queryData"]
    send_alert(m, f"{message} was called on {m.network}\n\n{m.tx_link}\n\n{data} at {now_utc()}")


def handle_datafeed(m):
    args = m.arg_map()
    value_hex = args["_value"]
    qtype, params = decode_query_data(args["_queryData"])
    padded = ""
    if "SpotPrice" in qtype:
        asset, currency = abi_decode(["string", "string"], params)
        label = f"{asset} / {currency}".upper()
        value = usd(spot_value(value_hex))
        padded = len(value_hex.removeprefix("0x")) == 64
    elif "EVMCall" in qtype:
        label = qtype
        value = abi_decode(["uint256", "address", "bytes"], params)[0]  # chainId
    else:  # RNG and anything else: raw label + numeric value
        label = qtype
        value = spot_value(value_hex)
    send_alert(m, f"> `{label},  {value}, Padded: {padded}`   [{m.network}]({m.tx_link})")


def handle_price_monitor(m):
    args = m.arg_map()
    asset = PRICE_MONITOR_ASSETS.get(args["_queryId"])
    if asset is None:
        return
    label, cg_id, cmc_symbol, coincap_id = asset
    reported = spot_value(args["_value"])

    def try_fetch(fn, *fn_args):
        try:
            return fn(*fn_args)
        except Exception:
            return None  # a dead source drops out of the average

    prices = {
        "cg": try_fetch(coingecko_price, cg_id),
        "cmc": try_fetch(coinmarketcap_price, cmc_symbol),
        "coincap": try_fetch(coincap_price, coincap_id) if coincap_id else None,
    }
    available = [p for p in prices.values() if p is not None]
    avg = sum(available) / len(available) if available else "n/a"
    fmt = lambda p: "n/a" if p is None else p
    send_alert(m, f"{label}, {reported}, {fmt(prices['cg'])}, {fmt(prices['cmc'])}, {fmt(prices['coincap'])}, {avg}")


def handle_dvm(m):
    args = m.arg_map()
    feed = DVM_FEEDS.get(args["_queryId"])
    if feed is None:
        return
    label, source, source_id = feed
    reference = coingecko_price(source_id) if source == "cg" else fx_usd_rate(source_id)
    reported = spot_value(args["_value"])
    diff = abs((reported - reference) / reference)
    if reference <= 0 or diff < DVM_TOLERANCE:
        return
    send_alert(m, (
        f"** Potential Dispute for {label} on {m.network} ! **\n\n"
        f"> Reported: `{reported}`\n"
        f"> Expected: `{reference}` is {diff * 100:.2f}% different\n\n"
        f"> Timestamp: [{now_utc()}]({m.tx_link})"
    ))


def handle_evm_call(m):
    args = m.arg_map()
    qtype, params = decode_query_data(args["_queryData"])
    if "EVMCall" not in qtype:
        return
    chain_id, contract, calldata = abi_decode(["uint256", "address", "bytes"], params)
    try:
        submitted = "0x" + abi_decode(["bytes", "uint256"], hex_bytes(args["_value"]))[0].hex()
    except Exception:
        submitted = None

    def alert(headline, result):
        send_alert(m, (
            f"** {headline} on {m.network} **\n"
            f"Expected: `{result}`\n"
            f"Value submitted: `{submitted}`\n"
            f"> {now_utc()}\n> [Block Explorer]({m.tx_link})"
        ))

    rpc_var = EVM_CALL_RPCS.get(chain_id)
    if rpc_var is None or not os.environ.get(rpc_var):
        alert(f"EVMCall NOT VERIFIED (no RPC for chainId {chain_id})", "unknown")
        return
    try:
        result = eth_call(os.environ[rpc_var], contract, "0x" + calldata.hex())
    except Exception as e:
        # Legacy code alerted "bad EVMCall" on RPC failure; distinguish it instead.
        alert(f"EVMCall NOT VERIFIED (RPC error on chainId {chain_id}: {e})", "unknown")
        return
    if submitted is None or result.lower() != submitted.lower():
        alert("Potentially bad EVMCall", result)


def handle_smoke(m):
    args = m.arg_map()
    amount = int(args["value"]) / 1e6  # USDC has 6 decimals
    send_alert(m, (
        f"[Large USDC Transfer]({m.tx_link})\n"
        f"`From: {args['from']}`\n`To: {args['to']}`\n`Amount: {amount:,.2f} USDC`"
    ))


HANDLERS = {
    "Tellor Staking": handle_staking,
    "Tellor Token Bridge": handle_token_bridge,
    "Tellor Deposit To Layer": handle_deposit_to_layer,
    "Tellor Withdraw From Layer": handle_withdraw_from_layer,
    "Tellor Address Updates": handle_address_updates,
    "Tellor Datafeed": handle_datafeed,
    "Tellor Price Monitor": handle_price_monitor,
    "Tellor DVM Price Deviation": handle_dvm,
    "Tellor EVMCall Validation": handle_evm_call,
    "Smoke Test USDC Transfer": handle_smoke,
}
