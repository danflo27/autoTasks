"""Handler tests for the tellor_alert script trigger.

Runs alert.py exactly the way the Monitor does — `python3 -c <file content>`
with the match payload on stdin and cwd at the config root — then asserts on
what lands in logs/alerts.log. No third-party deps, works in the official
Monitor image:

    cd migration
    docker run --rm -v .:/work -w /work --entrypoint python3 \
        openzeppelin/openzeppelin-monitor:v1.5.0 tests/run_tests.py

Some cases hit live public APIs (CoinGecko) — network required.
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_PY = os.path.join(ROOT, "config", "triggers", "scripts", "alert.py")
ALERT_LOG = os.path.join(ROOT, "logs", "alerts.log")

PASS, FAIL = 0, []


# ── minimal ABI encoder (mirror of tellor_lib.abi_decode, for test inputs) ───

def abi_encode(types, values):
    heads, tails = [], []
    tail_base = 32 * len(types)
    for t, v in zip(types, values):
        if t == "uint256":
            heads.append(v.to_bytes(32, "big"))
        elif t == "address":
            heads.append(bytes(12) + bytes.fromhex(v.removeprefix("0x")))
        elif t in ("string", "bytes"):
            raw = v.encode() if t == "string" else v
            padded = raw + bytes(-len(raw) % 32)
            offset = tail_base + sum(len(x) for x in tails)
            heads.append(offset.to_bytes(32, "big"))
            tails.append(len(raw).to_bytes(32, "big") + padded)
        else:
            raise ValueError(t)
    return b"".join(heads) + b"".join(tails)


def hx(b):
    return "0x" + b.hex()


def spot_query_data(asset, currency):
    return hx(abi_encode(["string", "bytes"], ["SpotPrice", abi_encode(["string", "string"], [asset, currency])]))


def uint_value_hex(v):
    return hx(int(v * 1e18).to_bytes(32, "big"))


# ── payload construction ──────────────────────────────────────────────────────

def payload(monitor, signature, args, kind="functions"):
    entry = {
        "signature": signature,
        "args": [{"name": n, "value": str(v), "indexed": False, "kind": k} for n, v, k in args],
        "hex_signature": None,
    }
    return {
        "monitor_match": {
            "EVM": {
                "monitor": {"name": monitor},
                "network_slug": "ethereum_mainnet",
                "transaction": {"hash": "0x" + "ab" * 32},
                "matched_on_args": {kind: [entry]},
            }
        },
        "args": [],
    }


def submit_value(monitor, query_id, value_hex, query_data_hex):
    return payload(monitor, "submitValue(bytes32,bytes,uint256,bytes)", [
        ("_queryId", query_id, "bytes32"),
        ("_value", value_hex, "bytes"),
        ("_nonce", "1", "uint256"),
        ("_queryData", query_data_hex, "bytes"),
    ])


# ── runner ────────────────────────────────────────────────────────────────────

def run_alert(case_payload, env_overrides=None):
    """Invoke alert.py Monitor-style; return (exit_code, new alert contents)."""
    before = line_count()
    env = {**os.environ, **(env_overrides or {})}
    env.pop("DISCORD_WEBHOOK_URL", None)  # log-only during tests
    with open(ALERT_PY) as f:
        content = f.read()
    proc = subprocess.run(
        [sys.executable, "-c", content],
        input=json.dumps(case_payload), capture_output=True, text=True,
        cwd=ROOT, env=env, timeout=60,
    )
    new = []
    if os.path.exists(ALERT_LOG):
        with open(ALERT_LOG) as f:
            new = [json.loads(line) for line in f.readlines()[before:]]
    return proc, new


def line_count():
    if not os.path.exists(ALERT_LOG):
        return 0
    with open(ALERT_LOG) as f:
        return len(f.readlines())


def check(name, case_payload, expect_alert, contains=(), env=None):
    global PASS
    proc, alerts = run_alert(case_payload, env)
    problems = []
    if proc.returncode != 0:
        problems.append(f"exit={proc.returncode} stderr={proc.stderr.strip()}")
    if expect_alert and not alerts:
        problems.append("expected an alert, got none")
    if not expect_alert and alerts:
        problems.append(f"expected no alert, got: {alerts}")
    for needle in contains:
        if not any(needle in a["content"] for a in alerts):
            problems.append(f"missing {needle!r} in {[a['content'] for a in alerts]}")
    if problems:
        FAIL.append(f"{name}: " + "; ".join(problems))
        print(f"FAIL {name}: " + "; ".join(problems))
    else:
        PASS += 1
        print(f"ok   {name}")


AUTOPAY_ID = "0x3ab34a189e35885414ac4e83c5a7faa9d8f03a4d530728ef516d203d91d6309c"
BTC_ID = "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac"
ETH_ID = "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992"
UNKNOWN_ID = "0x" + "11" * 32
ADDR = "0x5589e306b1920f009979a50b88cae32aecd471e4"


def main():
    # staking
    check("staking NewStaker",
          payload("Tellor Staking", "NewStaker(address,uint256)",
                  [("_staker", ADDR, "address"), ("_amount", int(100e18), "uint256")], "events"),
          True, ["Stake Deposited!", "Amount: 100.0", ADDR])
    check("staking StakeWithdrawn",
          payload("Tellor Staking", "StakeWithdrawn(address)",
                  [("_staker", ADDR, "address")], "events"),
          True, ["Stake withdrawn!", f"check withdraw request for {ADDR}"])

    # token bridge
    check("tokenBridge addStakingRewards",
          payload("Tellor Token Bridge", "addStakingRewards(uint256)",
                  [("_amount", int(250e18), "uint256")]),
          True, ["AddStakingRewards called !", "250.0 trb", "check tx"])
    check("tokenBridge pauseBridge",
          payload("Tellor Token Bridge", "pauseBridge()", []),
          True, ["PauseBridge called !", "n/a"])

    # deposit / withdraw
    check("depositToLayer",
          payload("Tellor Deposit To Layer", "depositToLayer(uint256,uint256,string)",
                  [("_amount", int(5e18), "uint256"), ("_tip", int(1e17), "uint256"),
                   ("_layerRecipient", "tellor1abc", "string")]),
          True, ["DepositToLayer called !", "5.0 trb", "0.1 trb", "tellor1abc"])
    check("withdrawFromLayer",
          payload("Tellor Withdraw From Layer", "Withdraw(uint256,string,address,uint256)",
                  [("_depositId", 7, "uint256"), ("_sender", "tellor1xyz", "string"),
                   ("_recipient", ADDR, "address"), ("_amount", int(3e18), "uint256")], "events"),
          True, ["WithdrawFromLayer called !", "Deposit ID: `7`", "3.0 trb"])

    # addressUpdates
    check("addressUpdates autopay report",
          submit_value("Tellor Address Updates", AUTOPAY_ID, uint_value_hex(1), spot_query_data("a", "b")),
          True, ["autopay address report", "ethereum_mainnet"])
    check("addressUpdates other queryId dropped",
          submit_value("Tellor Address Updates", UNKNOWN_ID, uint_value_hex(1), spot_query_data("a", "b")),
          False)
    check("addressUpdates updateStakeAmount",
          payload("Tellor Address Updates", "updateStakeAmount()", []),
          True, ["updateStakeAmount was called"])

    # datafeed
    check("datafeed SpotPrice",
          submit_value("Tellor Datafeed", ETH_ID, uint_value_hex(3500.5), spot_query_data("eth", "usd")),
          True, ["ETH / USD", "$3,500.50", "Padded: True"])
    evmcall_qd = hx(abi_encode(["string", "bytes"], ["EVMCall", abi_encode(
        ["uint256", "address", "bytes"], [10200, ADDR, bytes.fromhex("18160ddd")])]))
    check("datafeed EVMCall shows chainId",
          submit_value("Tellor Datafeed", UNKNOWN_ID, uint_value_hex(1), evmcall_qd),
          True, ["EVMCall,  10200"])

    # priceMonitor (live CoinGecko; CMC/CoinCap fail without keys -> n/a)
    check("priceMonitor BTC",
          submit_value("Tellor Price Monitor", BTC_ID, uint_value_hex(50000), spot_query_data("btc", "usd")),
          True, ["BTC / USD, 50000.0"])
    check("priceMonitor unknown id dropped",
          submit_value("Tellor Price Monitor", UNKNOWN_ID, uint_value_hex(1), spot_query_data("x", "usd")),
          False)

    # dvm (live CoinGecko reference)
    check("dvm huge deviation alerts",
          submit_value("Tellor DVM Price Deviation", BTC_ID, uint_value_hex(1), spot_query_data("btc", "usd")),
          True, ["Potential Dispute for BTC / USD"])
    check("dvm unknown id dropped",
          submit_value("Tellor DVM Price Deviation", UNKNOWN_ID, uint_value_hex(1), spot_query_data("x", "usd")),
          False)

    # EVMCall validation
    evmcall_value = hx(abi_encode(["bytes", "uint256"], [int(123).to_bytes(32, "big"), 1700000000]))
    no_rpc_qd = hx(abi_encode(["string", "bytes"], ["EVMCall", abi_encode(
        ["uint256", "address", "bytes"], [999999, ADDR, bytes.fromhex("18160ddd")])]))
    check("evmCall unsupported chain -> NOT VERIFIED",
          submit_value("Tellor EVMCall Validation", UNKNOWN_ID, evmcall_value, no_rpc_qd),
          True, ["NOT VERIFIED", "999999"])
    check("evmCall non-EVMCall dropped",
          submit_value("Tellor EVMCall Validation", ETH_ID, uint_value_hex(1), spot_query_data("eth", "usd")),
          False)

    # smoke handler
    check("smoke USDC transfer",
          payload("Smoke Test USDC Transfer", "Transfer(address,address,uint256)",
                  [("from", ADDR, "address"), ("to", ADDR, "address"),
                   ("value", 2_500_000_000_000, "uint256")], "events"),
          True, ["Large USDC Transfer", "2,500,000.00 USDC"])

    # dispatch guard
    proc, _ = run_alert(payload("No Such Monitor", "x()", []))
    if proc.returncode != 0 and "no handler" in proc.stderr:
        globals()["PASS"] += 1
        print("ok   unknown monitor exits non-zero")
    else:
        FAIL.append("unknown monitor should exit non-zero with clear error")

    print(f"\n{PASS} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
