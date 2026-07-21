"""Handler tests for the tellor_alert script trigger.

Runs alert.py exactly the way the Monitor does — `python3 -c <file content>`
with the match payload on stdin and cwd at the config root — then asserts on
what lands in logs/alerts.log. No third-party deps, works in the official
Monitor image:

    cd migration
    docker run --rm -v .:/work -w /work --entrypoint python3 \
        openzeppelin/openzeppelin-monitor:v1.5.0 tests/run_tests.py

Some cases try public price APIs. Their failure paths are deliberate and the
suite remains valid without network access.
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_PY = os.path.join(ROOT, "config", "triggers", "scripts", "alert.py")
ALERT_LOG = os.path.join(ROOT, "logs", "alerts.log")
MONITOR_DIR = os.path.join(ROOT, "config", "monitors")
SCRIPT_DIR = os.path.join(ROOT, "config", "triggers", "scripts")
sys.path.insert(0, SCRIPT_DIR)

import handlers as alert_handlers  # noqa: E402

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
        elif t in ("string", "bytes", "address[]"):
            if t == "address[]":
                raw = len(v).to_bytes(32, "big") + b"".join(
                    bytes(12) + bytes.fromhex(address.removeprefix("0x"))
                    for address in v
                )
            else:
                raw = v.encode() if t == "string" else v
            padded = raw + bytes(-len(raw) % 32)
            offset = tail_base + sum(len(x) for x in tails)
            heads.append(offset.to_bytes(32, "big"))
            tails.append(
                padded if t == "address[]" else len(raw).to_bytes(32, "big") + padded
            )
        else:
            raise ValueError(t)
    return b"".join(heads) + b"".join(tails)


def hx(b):
    return "0x" + b.hex()


def abi_shape(entries):
    return [
        (entry.get("name"), entry["type"], abi_shape(entry.get("components", [])))
        for entry in entries
    ]


def canonical_abi_type(entry):
    entry_type = entry["type"]
    if not entry_type.startswith("tuple"):
        return entry_type
    suffix = entry_type[len("tuple"):]
    components = ",".join(canonical_abi_type(item) for item in entry["components"])
    return f"({components}){suffix}"


def abi_entry_signature(entry):
    inputs = ",".join(canonical_abi_type(item) for item in entry.get("inputs", []))
    return f"{entry['name']}({inputs})"


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
                "receipt": {"status": "0x1"},
                "matched_on_args": {kind: [entry]},
            }
        },
        "args": [],
    }


def new_report(query_id, value_hex, query_data_hex, report_time=1_700_000_000):
    return payload(
        "TellorFlex Disputable Value",
        "NewReport(bytes32,uint256,bytes,uint256,bytes,address)",
        [
            ("_queryId", query_id, "bytes32"),
            ("_time", report_time, "uint256"),
            ("_value", value_hex, "bytes"),
            ("_nonce", "1", "uint256"),
            ("_queryData", query_data_hex, "bytes"),
            ("_reporter", ADDR, "address"),
        ],
        "events",
    )


# ── runner ────────────────────────────────────────────────────────────────────

def run_alert(case_payload, env_overrides=None):
    """Invoke alert.py Monitor-style; return (exit_code, new alert contents)."""
    before = line_count()
    env = {**os.environ, **(env_overrides or {})}
    env["TELLOR_ALERT_DELIVERY_MODE"] = "log-only"
    for key, value in list(env.items()):
        if value is None:
            env.pop(key)
    env.pop("DISCORD_WEBHOOK_URL", None)  # prove the retired shared fallback is irrelevant
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


def check(name, case_payload, expect_alert, contains=(), excludes=(), env=None):
    global PASS
    proc, alerts = run_alert(case_payload, env)
    problems = []
    if proc.returncode != 0:
        problems.append(f"exit={proc.returncode} stderr={proc.stderr.strip()}")
    if expect_alert and len(alerts) != 1:
        problems.append(f"expected exactly one alert, got {len(alerts)}")
    if not expect_alert and alerts:
        problems.append(f"expected no alert, got: {alerts}")
    for alert in alerts:
        content = alert["content"]
        lines = content.splitlines()
        if not lines or not re.fullmatch(r"\*\*[^*\n]+\*\*", lines[0]):
            problems.append(f"alert title is not normalized: {content!r}")
        if len(lines) < 4 or lines[1] != "> Network: `ethereum_mainnet`":
            problems.append(f"alert network line is not normalized: {content!r}")
        if len(lines) < 2 or not re.fullmatch(r"> Observed: `[^`]+ UTC`", lines[-2]):
            problems.append(f"alert timestamp is not normalized: {content!r}")
        if len(lines) < 1 or lines[-1] != (
            "> [View transaction](https://etherscan.io/tx/" + alert["tx"] + ")"
        ):
            problems.append(f"alert transaction link is not normalized: {content!r}")
        if any(not line.startswith("> ") for line in lines[1:]):
            problems.append(f"alert detail line is not normalized: {content!r}")
        if lines[0] in {
            "**✅ Looks normal**",
            "**👀 Report received**",
            "**⚠️ NOT VERIFIED**",
            "**🚨 POTENTIAL DISPUTE**",
        }:
            actual_fields = tuple(
                line.removeprefix("> ").split(":", 1)[0]
                for line in lines[1:-1]
            )
            required = (
                "Network", "Query type", "Query ID", "Report time",
                "Reporter", "Nonce", "Observed",
            )
            if any(field not in actual_fields for field in required):
                problems.append(
                    f"NewReport fields were {actual_fields}, expected to include {required}"
                )
    for needle in contains:
        if not any(needle in a["content"] for a in alerts):
            problems.append(f"missing {needle!r} in {[a['content'] for a in alerts]}")
    for needle in excludes:
        if any(needle in a["content"] for a in alerts):
            problems.append(f"unexpected {needle!r} in {[a['content'] for a in alerts]}")
    if problems:
        FAIL.append(f"{name}: " + "; ".join(problems))
        print(f"FAIL {name}: " + "; ".join(problems))
    else:
        PASS += 1
        print(f"ok   {name}")


AUTOPAY_ID = "0x3ab34a189e35885414ac4e83c5a7faa9d8f03a4d530728ef516d203d91d6309c"
ORACLE_ADDRESS_ID = "0xcf0c5863be1cf3b948a9ff43290f931399765d051a60c3b23a4e098148b1f707"
BTC_ID = "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac"
ETH_ID = "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992"
GYD_ID = "0x68584962e7ca6a57d672cdbfaa37c55431a84c5bb8c40d5d204a23f304f83b2e"
CNY_ID = "0x2c81613b335c890096fd1c9a89766a2d71da2c9636505a9cb3b3dc7877cdad4b"
UNKNOWN_ID = "0x" + "11" * 32
ADDR = "0x5589e306b1920f009979a50b88cae32aecd471e4"
OTHER_ADDR = "0x52410a1b9170e7fdbdc9fd9141f88737fa960c32"
ZERO_WORD = "0x" + "00" * 32


def main():
    # staking
    check("staking NewStaker",
          payload("Tellor Staking", "NewStaker(address,uint256)",
                  [("_staker", ADDR, "address"), ("_amount", int(100e18), "uint256")], "events"),
          True, ["Stake Deposited!", "Amount: `100.0`", ADDR])
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
    failed_call = payload("Tellor Token Bridge", "pauseBridge()", [])
    failed_call["monitor_match"]["EVM"]["receipt"]["status"] = "0x0"
    check("failed function call is dropped after receipt check",
          failed_call, False)

    # deposit / withdraw
    check("depositToLayer",
          payload("Tellor Deposit To Layer", "depositToLayer(uint256,uint256,string)",
                  [("_amount", int(5e18), "uint256"), ("_tip", int(1e17), "uint256"),
                   ("_layerRecipient", "tellor1`abc", "string")]),
          True, ["DepositToLayer called !", "5.0 trb", "0.1 trb", "tellor1ˋabc"],
          excludes=["tellor1`abc"])
    check("withdrawFromLayer",
          payload("Tellor Withdraw From Layer", "Withdraw(uint256,string,address,uint256)",
                  [("_depositId", 7, "uint256"), ("_sender", "tellor1xyz", "string"),
                   ("_recipient", ADDR, "address"), ("_amount", int(3e18), "uint256")], "events"),
          True, ["WithdrawFromLayer called !", "Deposit ID: `7`", "3.0 trb"])

    # Address Updates now owns only the non-report function.
    check("addressUpdates updateStakeAmount",
          payload("Tellor Address Updates", "updateStakeAmount()", []),
          True, ["updateStakeAmount was called"])

    # The unified NewReport entrypoint always emits one of four canonical
    # outcomes. These subprocess checks avoid network/RPC dependencies; the
    # classifier unit suite covers price and historical-call branches.
    rng_qd = hx(abi_encode(["string", "bytes"], [
        "TellorRNG", abi_encode(["uint256"], [1_700_000_000]),
    ]))
    rng_value = "0x" + "a5" * 32
    check("NewReport canonical TellorRNG is normal",
          new_report(UNKNOWN_ID, rng_value, rng_qd),
          True, ["✅ Looks normal", "Query type: `TellorRNG`",
                 "Validation: `canonical structure`", rng_value])
    unknown_qd = hx(abi_encode(["string", "bytes"], ["UnknownType", b""]))
    check("NewReport unknown valid query is neutral",
          new_report(UNKNOWN_ID, "0xdeadbeef", unknown_qd),
          True, ["👀 Report received", "Query type: `UnknownType`",
                 "unknown query type; no correctness claim"])
    check("NewReport malformed known report is contained",
          new_report(ETH_ID, uint_value_hex(1), "0xdeadbeef"),
          True, ["🚨 POTENTIAL DISPUTE", "malformed known report data",
                 "queryData has malformed ABI data"])

    # Unsupported chains are explicitly unverified without making an RPC call.
    no_rpc_qd = hx(abi_encode(["string", "bytes"], ["EVMCall", abi_encode(
        ["uint256", "address", "bytes"], [999999, ADDR, bytes.fromhex("18160ddd")])]))
    evmcall_value = hx(abi_encode(
        ["bytes", "uint256"], [int(123).to_bytes(32, "big"), 1_700_000_000]
    ))
    check("NewReport unsupported EVMCall chain is not verified",
          new_report(UNKNOWN_ID, evmcall_value, no_rpc_qd),
          True, ["⚠️ NOT VERIFIED", "Chain: `999999`", ADDR,
                 "Reason: `unsupported chain ID`"])

    phantom_param = abi_encode(["bytes"], [b""])
    autopay_query_data = hx(abi_encode(
        ["string", "bytes"], ["AutopayAddresses", phantom_param]
    ))
    autopay_value = hx(abi_encode(["address[]"], [[ADDR, OTHER_ADDR]]))
    oracle_address_value = hx(abi_encode(["address"], [OTHER_ADDR]))

    # smoke handler
    check("smoke USDC transfer",
          payload("Smoke Test USDC Transfer", "Transfer(address,address,uint256)",
                  [("from", ADDR, "address"), ("to", ADDR, "address"),
                   ("value", 2_500_000_000_000, "uint256")], "events"),
          True, ["Large USDC Transfer", "2,500,000.00 USDC"])

    # Tellor Layer relayer / validator-set monitors. Monitor formats nested
    # tuples as Python-literal-like strings; these fixtures mirror that output.
    report_ms = 1_700_000_000_000
    previous_ms = report_ms - 60_000
    # Match OpenZeppelin Monitor's format_token_value output exactly: quoted
    # strings inside tuples/arrays, with no whitespace between items.
    def oracle_attestation(query_id, value):
        return (
            f'(\"{query_id}\",(\"{value}\",{report_ms},48453,'
            f'{previous_ms},0,{report_ms}),{report_ms})'
        )

    attest_data = oracle_attestation(ETH_ID, uint_value_hex(3500.5))
    validators = f'[(\"{ADDR}\",60),(\"{OTHER_ADDR}\",40)]'
    signatures = (
        f'[(27,\"0x{"11" * 32}\",\"0x{"22" * 32}\"),'
        f'(0,\"{ZERO_WORD}\",\"{ZERO_WORD}\")]'
    )
    check("updateOracleData summarized",
          payload(
              "Update Oracle Data Calls",
              "updateOracleData((bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256),(address,uint256)[],(uint8,bytes32,bytes32)[])",
              [("_attestData", attest_data, "tuple"),
               ("_currentValidatorSet", validators, "tuple[]"),
               ("_sigs", signatures, "tuple[]")],
          ),
          True, ["Oracle data updated", "ETH / USD", "$3,500.50", "Consensus",
                 "2023-11-14 22:13:20 UTC", "Validators: `2`", "Signatures: `1 of 2`"],
          excludes=[uint_value_hex(3500.5), "0x" + "11" * 32])

    check("updateOracleData decodes relayed Autopay addresses",
          payload(
              "Update Oracle Data Calls",
              "updateOracleData((bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256),(address,uint256)[],(uint8,bytes32,bytes32)[])",
              [("_attestData", oracle_attestation(AUTOPAY_ID, autopay_value), "tuple"),
               ("_currentValidatorSet", validators, "tuple[]"),
               ("_sigs", signatures, "tuple[]")],
          ),
          True, ["Autopay addresses", "2 addresses", ADDR, OTHER_ADDR],
          excludes=[autopay_value, "0x" + "11" * 32])

    check("updateOracleData decodes relayed oracle address",
          payload(
              "Update Oracle Data Calls",
              "updateOracleData((bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256),(address,uint256)[],(uint8,bytes32,bytes32)[])",
              [("_attestData", oracle_attestation(
                   ORACLE_ADDRESS_ID, oracle_address_value), "tuple"),
               ("_currentValidatorSet", validators, "tuple[]"),
               ("_sigs", signatures, "tuple[]")],
          ),
          True, ["Tellor oracle address", OTHER_ADDR],
          excludes=[oracle_address_value, "0x" + "11" * 32])

    validator_hash = "0x" + "33" * 32
    check("updateValidatorSet summarized",
          payload(
              "Update Validator Set Calls",
              "updateValidatorSet(bytes32,uint64,uint256,(address,uint256)[],(uint8,bytes32,bytes32)[])",
              [("_newValidatorSetHash", validator_hash, "bytes32"),
               ("_newPowerThreshold", 80, "uint64"),
               ("_newValidatorTimestamp", report_ms, "uint256"),
               ("_currentValidatorSet", validators, "tuple[]"),
               ("_sigs", signatures, "tuple[]")],
          ),
          True, ["Validator set updated", "New power threshold: `80`", validator_hash,
                 "2023-11-14 22:13:20 UTC", "Current validators: `2`",
                 "Signatures: `1 of 2`"],
          excludes=["0x" + "11" * 32])

    guardian_hash = "0x" + "44" * 32
    check("guardian validator reset summarized",
          payload(
              "Guardian Reset Validator Set Calls",
              "GuardianResetValidatorSet(uint256,uint256,bytes32)",
              [("_powerThreshold", 80, "uint256"),
               ("_validatorTimestamp", report_ms, "uint256"),
               ("_validatorSetHash", guardian_hash, "bytes32")],
              "events",
          ),
          True, ["Guardian reset validator set", "Power threshold: `80`", guardian_hash,
                 "2023-11-14 22:13:20 UTC"])

    validator_shape = [
        ("addr", "address", []),
        ("power", "uint256", []),
    ]
    signature_shape = [
        ("v", "uint8", []),
        ("r", "bytes32", []),
        ("s", "bytes32", []),
    ]
    typed_monitors = {
        "update_oracle_data.json": {
            "signature": "updateOracleData((bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256),(address,uint256)[],(uint8,bytes32,bytes32)[])",
            "kind": "function",
            "shape": [
                ("_attestData", "tuple", [
                    ("queryId", "bytes32", []),
                    ("report", "tuple", [
                        ("value", "bytes", []),
                        ("timestamp", "uint256", []),
                        ("aggregatePower", "uint256", []),
                        ("previousTimestamp", "uint256", []),
                        ("nextTimestamp", "uint256", []),
                        ("lastConsensusTimestamp", "uint256", []),
                    ]),
                    ("attestationTimestamp", "uint256", []),
                ]),
                ("_currentValidatorSet", "tuple[]", validator_shape),
                ("_sigs", "tuple[]", signature_shape),
            ],
        },
        "update_validator_set.json": {
            "signature": "updateValidatorSet(bytes32,uint64,uint256,(address,uint256)[],(uint8,bytes32,bytes32)[])",
            "kind": "function",
            "shape": [
                ("_newValidatorSetHash", "bytes32", []),
                ("_newPowerThreshold", "uint64", []),
                ("_newValidatorTimestamp", "uint256", []),
                ("_currentValidatorSet", "tuple[]", validator_shape),
                ("_sigs", "tuple[]", signature_shape),
            ],
        },
        "guardian_reset_validator_set.json": {
            "signature": "GuardianResetValidatorSet(uint256,uint256,bytes32)",
            "kind": "event",
            "shape": [
                ("_powerThreshold", "uint256", []),
                ("_validatorTimestamp", "uint256", []),
                ("_validatorSetHash", "bytes32", []),
            ],
        },
    }
    for filename, expected in typed_monitors.items():
        with open(os.path.join(MONITOR_DIR, filename)) as f:
            monitor_config = json.load(f)
        signature = expected["signature"]
        match_kind = expected["kind"]
        match_key = f"{match_kind}s"
        matches = monitor_config["match_conditions"][match_key]
        contract_spec = monitor_config["addresses"][0].get("contract_spec") or []
        abi_entries = [entry for entry in contract_spec if entry.get("type") == match_kind]
        exact_abi = (
            len(abi_entries) == 1
            and abi_entry_signature(abi_entries[0]) == signature
            and abi_shape(abi_entries[0].get("inputs", [])) == expected["shape"]
        )
        if (monitor_config.get("triggers") == ["tellor_alert"]
                and [match.get("signature") for match in matches] == [signature]
                and exact_abi):
            globals()["PASS"] += 1
            print(f"ok   {filename} uses a decoded {match_kind} and typed handler")
        else:
            FAIL.append(f"{filename} should use a decoded {match_kind} and tellor_alert")
            print(f"FAIL {filename} should use a decoded {match_kind} and tellor_alert")

    combined_path = os.path.join(
        MONITOR_DIR, "tellorflex_disputable_value.json"
    )
    with open(combined_path) as f:
        combined_monitor = json.load(f)
    address_updates_path = os.path.join(MONITOR_DIR, "address_updates.json")
    with open(address_updates_path) as f:
        address_updates_monitor = json.load(f)
    old_monitor_paths = tuple(
        os.path.join(MONITOR_DIR, filename)
        for filename in ("dvm.json", "evm_call.json", "tellorflex_data_report.json")
    )
    report_owners = []
    for filename in sorted(os.listdir(MONITOR_DIR)):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(MONITOR_DIR, filename)) as f:
            monitor_config = json.load(f)
        matches = monitor_config.get("match_conditions") or {}
        signatures = [
            match.get("signature")
            for kind in ("functions", "events")
            for match in (matches.get(kind) or [])
        ]
        if any(signature == alert_handlers.NEW_REPORT_SIGNATURE
               or signature.startswith("submitValue(") for signature in signatures):
            report_owners.append(filename)
    report_event = combined_monitor["addresses"][0]["contract_spec"][0]
    indexed = [entry.get("indexed") for entry in report_event["inputs"]]
    address_signatures = [
        match.get("signature")
        for kind in ("functions", "events")
        for match in address_updates_monitor["match_conditions"].get(kind, [])
    ]
    if (
        combined_monitor.get("name") == "TellorFlex Disputable Value"
        and combined_monitor["match_conditions"]["events"] == [{
            "signature": alert_handlers.NEW_REPORT_SIGNATURE,
            "expression": None,
        }]
        and abi_entry_signature(report_event) == alert_handlers.NEW_REPORT_SIGNATURE
        and indexed == [True, True, False, False, False, True]
        and report_owners == ["tellorflex_disputable_value.json"]
        and address_signatures == ["updateStakeAmount()"]
        and not any(os.path.exists(path) for path in old_monitor_paths)
        and "TellorFlex Disputable Value" in alert_handlers.HANDLERS
        and not {
            "TellorFlex Data Report", "Tellor DVM Price Deviation",
            "Tellor EVMCall Validation",
        }.intersection(alert_handlers.HANDLERS)
    ):
        globals()["PASS"] += 1
        print("ok   every disputable NewReport is owned by one indexed event monitor")
    else:
        FAIL.append("disputable NewReport coverage should have exactly one owner")
        print("FAIL disputable NewReport coverage should have exactly one owner")

    for filename in sorted(os.listdir(MONITOR_DIR)):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(MONITOR_DIR, filename)) as f:
            monitor_config = json.load(f)
        expected_triggers = (
            ["quickstart_function_alert"]
            if filename == "quickstart_function.json"
            else ["tellor_alert"]
        )
        expected_paused = filename == "smoke_usdc.json"
        pause_state_matches = (
            monitor_config.get("paused") is True
            if expected_paused
            else monitor_config.get("paused") is False
        )
        if (
            pause_state_matches
            and monitor_config.get("networks") == ["ethereum_mainnet"]
            and monitor_config.get("triggers") == expected_triggers
        ):
            globals()["PASS"] += 1
            state = "paused" if expected_paused else "active"
            print(
                f"ok   {filename} is {state} on ethereum_mainnet only "
                "with its normalized trigger"
            )
        else:
            expected_state = "paused" if expected_paused else "active"
            FAIL.append(
                f"{filename} should be {expected_state} on ethereum_mainnet only "
                f"with {expected_triggers}"
            )
            print(
                f"FAIL {filename} should be {expected_state} on ethereum_mainnet only "
                f"with {expected_triggers}"
            )

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
