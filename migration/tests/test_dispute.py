"""Deterministic tests for unified Tellor NewReport classification."""

import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MONITOR_DIR = ROOT / "config" / "monitors"
SCRIPT_DIR = ROOT / "config" / "triggers" / "scripts"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPT_DIR))

import handlers  # noqa: E402
import discord_routes  # noqa: E402
import tellor_lib  # noqa: E402


BTC_ID = "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac"
UNKNOWN_ID = "0x" + "11" * 32
REPORTER = "0x50a86759d495ecfa7c301071d6b0bdd4bd664ab0"
TARGET = "0x5589e306b1920f009979a50b88cae32aecd471e4"
BLOCK_HASH = "0x" + "22" * 32


def abi_encode(types, values):
    heads, tails = [], []
    head_size = 32 * len(types)
    for value_type, value in zip(types, values):
        if value_type == "uint256":
            heads.append(int(value).to_bytes(32, "big"))
        elif value_type == "address":
            heads.append(bytes(12) + bytes.fromhex(value.removeprefix("0x")))
        elif value_type in {"bytes", "string", "address[]"}:
            if value_type == "address[]":
                body = len(value).to_bytes(32, "big") + b"".join(
                    bytes(12) + bytes.fromhex(item.removeprefix("0x"))
                    for item in value
                )
            else:
                raw = value.encode() if value_type == "string" else value
                body = len(raw).to_bytes(32, "big") + raw + bytes(-len(raw) % 32)
            heads.append((head_size + sum(map(len, tails))).to_bytes(32, "big"))
            tails.append(body)
        else:
            raise ValueError(value_type)
    return b"".join(heads + tails)


def hx(raw):
    return "0x" + raw.hex()


def query_data(qtype, params):
    return hx(abi_encode(["string", "bytes"], [qtype, params]))


def spot_query(asset="btc", currency="usd"):
    return query_data("SpotPrice", abi_encode(["string", "string"], [asset, currency]))


def uint_value(raw_integer):
    return "0x" + int(raw_integer).to_bytes(32, "big").hex()


def report_args(query_id, value, qdata, **overrides):
    args = {
        "_queryId": query_id,
        "_time": 1_783_982_111,
        "_value": value,
        "_nonce": 7,
        "_queryData": qdata,
        "_reporter": REPORTER,
    }
    args.update(overrides)
    return args


def classify(args, **kwargs):
    return handlers.classify_report(args, **kwargs)[0]


class FakeMatch:
    monitor_name = handlers.DISPUTABLE_MONITOR
    network = "ethereum_mainnet"
    tx_hash = "0x" + "ab" * 32
    tx_link = "https://etherscan.io/tx/" + tx_hash
    signature = handlers.NEW_REPORT_SIGNATURE

    def __init__(self, args, signature=None):
        self.args = args
        if signature is not None:
            self.signature = signature

    def arg_map(self):
        return dict(self.args)


class ConfigTests(unittest.TestCase):
    def test_exact_unified_event_and_no_submit_value_overlap(self):
        expected_inputs = [
            ("_queryId", "bytes32", True),
            ("_time", "uint256", True),
            ("_value", "bytes", False),
            ("_nonce", "uint256", False),
            ("_queryData", "bytes", False),
            ("_reporter", "address", True),
        ]
        unified = json.loads((MONITOR_DIR / "tellorflex_disputable_value.json").read_text())
        event = unified["addresses"][0]["contract_spec"]
        self.assertEqual(unified["name"], handlers.DISPUTABLE_MONITOR)
        self.assertEqual(unified["networks"], ["ethereum_mainnet"])
        self.assertFalse(unified["paused"])
        self.assertEqual(len(event), 1)
        self.assertEqual(event[0]["name"], "NewReport")
        self.assertEqual(
            [(item["name"], item["type"], item["indexed"]) for item in event[0]["inputs"]],
            expected_inputs,
        )
        self.assertEqual(
            unified["match_conditions"]["events"],
            [{"signature": handlers.NEW_REPORT_SIGNATURE, "expression": None}],
        )

        submit_matches = []
        new_report_matches = []
        for path in MONITOR_DIR.glob("*.json"):
            config = json.loads(path.read_text())
            functions = config.get("match_conditions", {}).get("functions", [])
            events = config.get("match_conditions", {}).get("events", [])
            submit_matches.extend(
                (path.name, item["signature"])
                for item in functions if item["signature"].startswith("submitValue(")
            )
            new_report_matches.extend(
                (path.name, item["signature"])
                for item in events if item["signature"] == handlers.NEW_REPORT_SIGNATURE
            )
        self.assertEqual(submit_matches, [])
        self.assertEqual(
            new_report_matches,
            [("tellorflex_disputable_value.json", handlers.NEW_REPORT_SIGNATURE)],
        )
        self.assertEqual(
            json.loads((MONITOR_DIR / "address_updates.json").read_text())
            ["match_conditions"]["functions"],
            [{"signature": "updateStakeAmount()", "expression": None}],
        )
        for retired in ("dvm.json", "evm_call.json", "tellorflex_data_report.json"):
            self.assertFalse((MONITOR_DIR / retired).exists())
        self.assertIn(handlers.DISPUTABLE_MONITOR, handlers.HANDLERS)
        self.assertNotIn("TellorFlex Data Report", handlers.HANDLERS)
        self.assertNotIn("Tellor DVM Price Deviation", handlers.HANDLERS)
        self.assertNotIn("Tellor EVMCall Validation", handlers.HANDLERS)

    def test_catalogs_have_exact_target_sets(self):
        self.assertEqual(len(tellor_lib.TRUSTED_PRICE_ASSETS), 17)
        self.assertEqual(
            set(tellor_lib.EVM_CALL_RPCS),
            {1, 10, 100, 137, 10200, 11155111},
        )
        labels = {item["label"] for item in tellor_lib.TRUSTED_PRICE_ASSETS.values()}
        self.assertIn("BRL / USD", labels)
        self.assertIn("CNY / USD", labels)
        self.assertIn("GYD / USD", labels)
        for asset in tellor_lib.TRUSTED_PRICE_ASSETS.values():
            if "fixed" in asset:
                continue
            if "frankfurter" in asset:
                self.assertTrue(
                    {"frankfurter", "open_er_api", "fxratesapi"}.issubset(asset),
                    asset["label"],
                )
            else:
                self.assertTrue(
                    {"cg", "defillama", "paprika"}.issubset(asset), asset["label"]
                )
        gyd = next(
            asset for asset in tellor_lib.TRUSTED_PRICE_ASSETS.values()
            if asset["label"] == "GYD / USD"
        )
        self.assertEqual(gyd["min_sources"], 1)
        self.assertEqual(tellor_lib.TELLIOT_EVM_CALL_TAG, "v0.4.20")
        self.assertEqual(
            tellor_lib.TELLIOT_EVM_CALL_COMMIT,
            "fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2",
        )


class SpotAndOutcomeTests(unittest.TestCase):
    def test_raw_exact_twenty_percent_is_normal_and_one_wei_more_disputes(self):
        exact = report_args(BTC_ID, uint_value(120 * 10**18), spot_query())
        above = report_args(BTC_ID, uint_value(120 * 10**18 + 1), spot_query())
        self.assertEqual(
            classify(exact, price_fetcher=lambda _: [Decimal("100")] * 2).label,
            handlers.OUTCOME_NORMAL,
        )
        self.assertEqual(
            classify(above, price_fetcher=lambda _: [Decimal("100")] * 2).label,
            handlers.OUTCOME_DISPUTE,
        )

    def test_no_source_and_unknown_type_have_distinct_outcomes(self):
        no_source = report_args(BTC_ID, uint_value(100 * 10**18), spot_query())
        unknown = report_args(
            UNKNOWN_ID,
            "0xdeadbeef",
            query_data("FutureQuery", abi_encode(["bytes"], [b"parameters"])),
        )
        self.assertEqual(
            classify(no_source, price_fetcher=lambda _: []).label,
            handlers.OUTCOME_NOT_VERIFIED,
        )
        self.assertEqual(classify(unknown).label, handlers.OUTCOME_RECEIVED)

        empty_unknown = report_args(
            UNKNOWN_ID,
            "0x",
            query_data("FutureQuery", b""),
        )
        self.assertEqual(classify(empty_unknown).label, handlers.OUTCOME_RECEIVED)

    def test_market_spot_price_requires_two_usable_sources(self):
        args = report_args(BTC_ID, uint_value(100 * 10**18), spot_query())
        outcome = classify(args, price_fetcher=lambda _: [Decimal("100")])
        self.assertEqual(outcome.label, handlers.OUTCOME_NOT_VERIFIED)
        self.assertIn(("Trusted sources", 1), outcome.details)
        self.assertIn(
            ("Reason", "need 2 usable trusted price sources"), outcome.details
        )


class TrustedPriceSourceTests(unittest.TestCase):
    def test_new_source_response_shapes_are_decoded(self):
        def payload(url, **_kwargs):
            if "coinpaprika" in url:
                return {"quotes": {"USD": {"price": "100.25"}}}
            if "frankfurter" in url:
                return {"rates": {"USD": "0.2"}}
            if "open.er-api" in url:
                return {"rates": {"USD": "0.21"}}
            if "fxratesapi" in url:
                return {"rates": {"USD": "0.22"}}
            self.fail(url)

        with mock.patch.object(tellor_lib, "http_json", side_effect=payload):
            self.assertEqual(tellor_lib.coinpaprika_price("btc-bitcoin"), 100.25)
            self.assertEqual(tellor_lib.frankfurter_usd_rate("BRL"), 0.2)
            self.assertEqual(tellor_lib.open_er_api_usd_rate("BRL"), 0.21)
            self.assertEqual(tellor_lib.fxratesapi_usd_rate("BRL"), 0.22)

    def test_collector_uses_new_public_fallbacks_and_skips_one_failure(self):
        asset = {
            "cg": "bitcoin",
            "paprika": "btc-bitcoin",
            "frankfurter": "BRL",
        }
        with mock.patch.object(tellor_lib, "coingecko_price", return_value=100), mock.patch.object(
            tellor_lib, "coinpaprika_price", side_effect=RuntimeError("unavailable")
        ), mock.patch.object(tellor_lib, "frankfurter_usd_rate", return_value=0.2):
            self.assertEqual(tellor_lib.fetch_trusted_prices(asset), [100.0, 0.2])

    def test_handler_emits_each_label_once_and_never_calls_http(self):
        cases = [
            (
                report_args(BTC_ID, uint_value(100 * 10**18), spot_query()),
                [Decimal("100")] * 2,
                handlers.OUTCOME_NORMAL,
            ),
            (
                report_args(BTC_ID, uint_value(121 * 10**18), spot_query()),
                [Decimal("100")] * 2,
                handlers.OUTCOME_DISPUTE,
            ),
            (
                report_args(BTC_ID, uint_value(100 * 10**18), spot_query()),
                [],
                handlers.OUTCOME_NOT_VERIFIED,
            ),
            (
                report_args(UNKNOWN_ID, "0x01", query_data("FutureQuery", b"")),
                [],
                handlers.OUTCOME_RECEIVED,
            ),
        ]
        for args, prices, expected in cases:
            expects_alert = expected in handlers.DISPUTABLE_ALERT_OUTCOMES
            with self.subTest(expected=expected), mock.patch.object(
                handlers, "fetch_trusted_prices", return_value=prices
            ), mock.patch.object(handlers, "send_alert") as send, mock.patch.object(
                tellor_lib, "http_json", side_effect=AssertionError("HTTP must not run")
            ):
                handlers.handle_disputable_value(FakeMatch(args))
                if expects_alert:
                    send.assert_called_once()
                    content = send.call_args.args[1]
                    self.assertEqual(content.splitlines()[0], f"**{expected}**")
                    self.assertEqual(
                        sum(f"**{label}**" in content for label in handlers.REPORT_OUTCOMES),
                        1,
                    )
                else:
                    send.assert_not_called()

    def test_malformed_known_report_is_contained_as_one_dispute(self):
        malformed = report_args(BTC_ID, "0x", spot_query())
        with mock.patch.object(handlers, "send_alert") as send:
            handlers.handle_disputable_value(FakeMatch(malformed))
        send.assert_called_once()
        self.assertEqual(
            send.call_args.args[1].splitlines()[0],
            f"**{handlers.OUTCOME_DISPUTE}**",
        )


class StructuralTests(unittest.TestCase):
    AMPL_QUERY_DATA = (
        "0x0000000000000000000000000000000000000000000000000000000000000040"
        "0000000000000000000000000000000000000000000000000000000000000080"
        "0000000000000000000000000000000000000000000000000000000000000019"
        "416d706c65666f727468437573746f6d53706f74507269636500000000000000"
        "0000000000000000000000000000000000000000000000000000000000000040"
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000000"
    )
    USPCE_QUERY_DATA = (
        "0x0000000000000000000000000000000000000000000000000000000000000040"
        "0000000000000000000000000000000000000000000000000000000000000080"
        "000000000000000000000000000000000000000000000000000000000000000f"
        "416d706c65666f72746855535043450000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000040"
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000000"
    )

    def test_pinned_ampl_and_uspce_structures(self):
        for query_id, qdata in (
            (handlers.AMPL_QUERY_ID, self.AMPL_QUERY_DATA),
            (handlers.USPCE_QUERY_ID, self.USPCE_QUERY_DATA),
        ):
            with self.subTest(query_id=query_id):
                outcome = classify(report_args(query_id, uint_value(10**18), qdata))
                self.assertEqual(outcome.label, handlers.OUTCOME_NORMAL)

    def test_address_and_rng_structures(self):
        phantom = abi_encode(["bytes"], [b""])
        autopay_id = next(
            key for key, value in tellor_lib.ADDRESS_REPORTS.items()
            if value[1] == "AutopayAddresses"
        )
        autopay = report_args(
            autopay_id,
            hx(abi_encode(["address[]"], [[TARGET, REPORTER]])),
            query_data("AutopayAddresses", phantom),
        )
        oracle_id = next(
            key for key, value in tellor_lib.ADDRESS_REPORTS.items()
            if value[1] == "TellorOracleAddress"
        )
        oracle_address = report_args(
            oracle_id,
            hx(abi_encode(["address"], [TARGET])),
            query_data("TellorOracleAddress", phantom),
        )
        rng = report_args(
            UNKNOWN_ID,
            "0x" + "a5" * 32,
            query_data("TellorRNG", abi_encode(["uint256"], [1_700_000_000])),
        )
        self.assertEqual(classify(autopay).label, handlers.OUTCOME_NORMAL)
        self.assertEqual(classify(oracle_address).label, handlers.OUTCOME_NORMAL)
        self.assertEqual(classify(rng).label, handlers.OUTCOME_NORMAL)

    def test_malformed_phantom_rng_and_noncanonical_abi_are_rejected(self):
        bad_phantom = report_args(
            handlers.AMPL_QUERY_ID,
            uint_value(10**18),
            query_data("AmpleforthCustomSpotPrice", abi_encode(["bytes"], [b"x"])),
        )
        bad_rng = report_args(
            UNKNOWN_ID,
            "0xdeadbeef",
            query_data("TellorRNG", abi_encode(["uint256"], [1_700_000_000])),
        )
        trailing = report_args(
            UNKNOWN_ID,
            "0x01",
            query_data("FutureQuery", b"") + "00" * 32,
        )
        for args in (bad_phantom, bad_rng, trailing):
            with self.subTest(args=args):
                with self.assertRaises(handlers.MalformedReport):
                    handlers.classify_report(args)


class EVMCallTests(unittest.TestCase):
    def evm_args(self, chain_id=1, calldata=b"\x18\x16\x0d\xdd", submitted=None):
        submitted = bytes.fromhex("00" * 31 + "7b") if submitted is None else submitted
        qdata = query_data(
            "EVMCall",
            abi_encode(["uint256", "address", "bytes"], [chain_id, TARGET, calldata]),
        )
        value = hx(abi_encode(["bytes", "uint256"], [submitted, 110]))
        return report_args(UNKNOWN_ID, value, qdata)

    def test_match_mismatch_missing_rpc_and_ambiguity(self):
        block = {"number": 2, "timestamp": 110, "hash": BLOCK_HASH}
        expected = bytes.fromhex("00" * 31 + "7b")
        match = classify(
            self.evm_args(submitted=expected),
            evm_reference=lambda *_: (expected, block),
            environ={"RPC_ETHEREUM_MAINNET": "https://rpc.invalid"},
        )
        mismatch = classify(
            self.evm_args(submitted=bytes(32)),
            evm_reference=lambda *_: (expected, block),
            environ={"RPC_ETHEREUM_MAINNET": "https://rpc.invalid"},
        )
        missing = classify(self.evm_args(), environ={})

        def ambiguous(*_):
            raise tellor_lib.EVMCallNotVerified("no exact block")

        ambiguity = classify(
            self.evm_args(),
            evm_reference=ambiguous,
            environ={"RPC_ETHEREUM_MAINNET": "https://rpc.invalid"},
        )
        self.assertEqual(match.label, handlers.OUTCOME_NORMAL)
        self.assertEqual(mismatch.label, handlers.OUTCOME_DISPUTE)
        self.assertEqual(missing.label, handlers.OUTCOME_NOT_VERIFIED)
        self.assertEqual(ambiguity.label, handlers.OUTCOME_NOT_VERIFIED)

    def test_empty_calldata_is_not_verified_but_one_to_three_bytes_are_zero(self):
        block = {"number": 2, "timestamp": 110, "hash": BLOCK_HASH}
        with mock.patch.object(
            tellor_lib,
            "exact_block_at_timestamp",
            side_effect=AssertionError("empty calldata must not query the RPC"),
        ):
            with self.assertRaisesRegex(
                tellor_lib.EVMCallNotVerified,
                "empty calldata is invalid in the pinned reporter",
            ):
                tellor_lib.historical_evm_call_reference("rpc", TARGET, b"", 110)
            outcome = classify(
                self.evm_args(calldata=b"", submitted=bytes(32)),
                environ={"RPC_ETHEREUM_MAINNET": "https://rpc.invalid"},
            )
        self.assertEqual(outcome.label, handlers.OUTCOME_NOT_VERIFIED)
        self.assertIn(
            ("Reason", "empty calldata is invalid in the pinned reporter"),
            outcome.details,
        )

        stable = mock.patch.object(tellor_lib, "exact_block_at_timestamp", return_value=block)
        by_number = mock.patch.object(tellor_lib, "rpc_block", return_value=block)
        by_hash = mock.patch.object(tellor_lib, "rpc_block_by_hash", return_value=block)
        no_call = mock.patch.object(
            tellor_lib, "eth_call", side_effect=AssertionError("short calldata must not execute")
        )
        no_code = mock.patch.object(
            tellor_lib, "rpc_result", side_effect=AssertionError("short calldata must not read code")
        )
        with stable, by_number, by_hash, no_call, no_code:
            for size in (1, 2, 3):
                with self.subTest(size=size):
                    expected, _ = tellor_lib.historical_evm_call_reference(
                        "rpc", TARGET, b"\x01" * size, 110
                    )
                    self.assertEqual(expected, bytes(32))

    def test_exact_block_search_validates_timestamp_neighbors_and_hash(self):
        timestamps = [90, 100, 110, 120, 130]

        def block(number):
            return {
                "number": hex(number),
                "timestamp": hex(timestamps[number]),
                "hash": "0x" + f"{number + 1:02x}" * 32,
            }

        def http_json(_url, body=None, **_kwargs):
            method, params = body["method"], body["params"]
            if method == "eth_getBlockByNumber":
                number = 4 if params[0] == "latest" else int(params[0], 16)
                return {"result": block(number)}
            if method == "eth_getBlockByHash":
                number = int(params[0][2:4], 16) - 1
                return {"result": block(number)}
            raise AssertionError(method)

        with mock.patch.object(tellor_lib, "http_json", side_effect=http_json):
            found = tellor_lib.exact_block_at_timestamp("https://rpc.invalid", 110)
            self.assertEqual(found["number"], 2)
            with self.assertRaisesRegex(tellor_lib.EVMCallNotVerified, "no block"):
                tellor_lib.exact_block_at_timestamp("https://rpc.invalid", 115)

        duplicate = [90, 100, 110, 110, 130]
        timestamps[:] = duplicate
        with mock.patch.object(tellor_lib, "http_json", side_effect=http_json):
            with self.assertRaisesRegex(tellor_lib.EVMCallNotVerified, "multiple"):
                tellor_lib.exact_block_at_timestamp("https://rpc.invalid", 110)

    def test_historical_reference_zero_and_reorg_branches(self):
        block = {"number": 2, "timestamp": 110, "hash": BLOCK_HASH}
        stable = mock.patch.object(tellor_lib, "exact_block_at_timestamp", return_value=block)
        by_number = mock.patch.object(tellor_lib, "rpc_block", return_value=block)
        by_hash = mock.patch.object(tellor_lib, "rpc_block_by_hash", return_value=block)
        with stable, by_number, by_hash:
            expected, _ = tellor_lib.historical_evm_call_reference(
                "rpc", TARGET, b"\x01\x02\x03", 110
            )
            self.assertEqual(expected, bytes(32))

            with mock.patch.object(tellor_lib, "eth_call", return_value="0x"), mock.patch.object(
                tellor_lib, "rpc_result", return_value="0x"
            ):
                expected, _ = tellor_lib.historical_evm_call_reference(
                    "rpc", TARGET, b"\x18\x16\x0d\xdd", 110
                )
                self.assertEqual(expected, bytes(32))

            with mock.patch.object(
                tellor_lib, "eth_call",
                side_effect=tellor_lib.EVMCallExecutionError("reverted"),
            ), mock.patch.object(tellor_lib, "rpc_result", return_value="0x60016002"):
                expected, _ = tellor_lib.historical_evm_call_reference(
                    "rpc", TARGET, b"\x18\x16\x0d\xdd", 110
                )
                self.assertEqual(expected, bytes(32))

            with mock.patch.object(tellor_lib, "eth_call", return_value="0x"), mock.patch.object(
                tellor_lib, "rpc_result", return_value="0x601818160ddd60"
            ):
                with self.assertRaisesRegex(tellor_lib.EVMCallNotVerified, "empty call"):
                    tellor_lib.historical_evm_call_reference(
                        "rpc", TARGET, b"\x18\x16\x0d\xdd", 110
                    )

        changed = {**block, "hash": "0x" + "33" * 32}
        with mock.patch.object(
            tellor_lib, "exact_block_at_timestamp", return_value=block
        ), mock.patch.object(
            tellor_lib, "eth_call", return_value="0x01"
        ), mock.patch.object(
            tellor_lib, "rpc_block", return_value=changed
        ):
            with self.assertRaisesRegex(tellor_lib.EVMCallNotVerified, "identity changed"):
                tellor_lib.historical_evm_call_reference(
                    "rpc", TARGET, b"\x18\x16\x0d\xdd", 110
                )


class HistoricalFixtureTests(unittest.TestCase):
    def test_rpc_verified_new_report_fixture_decodes_and_classifies_offline(self):
        fixture = json.loads(
            (FIXTURE_DIR / "new_report_eth_usd_block_25526730.json").read_text()
        )
        raw = bytes.fromhex(fixture["data"].removeprefix("0x"))
        value, nonce, qdata = tellor_lib.abi_decode_exact(
            ["bytes", "uint256", "bytes"], raw
        )
        decoded = fixture["decoded"]
        self.assertEqual(
            fixture["topics"][0],
            "0x48e9e2c732ba278de6ac88a3a57a5c5ba13d3d8370e709b3b98333a57876ca95",
        )
        self.assertEqual(hx(value), decoded["_value"])
        self.assertEqual(nonce, decoded["_nonce"])
        self.assertEqual(hx(qdata), decoded["_queryData"])
        self.assertEqual(fixture["topics"][1], decoded["_queryId"])
        self.assertEqual(int(fixture["topics"][2], 16), decoded["_time"])
        self.assertEqual("0x" + fixture["topics"][3][-40:], decoded["_reporter"])

        reported = Decimal(int.from_bytes(value, "big")) / Decimal(10**18)
        with mock.patch.object(
            tellor_lib, "http_json", side_effect=AssertionError("fixture replay must be offline")
        ):
            outcome = classify(
                decoded,
                price_fetcher=lambda _: [reported] * 2,
                environ={},
            )
        self.assertEqual(outcome.label, handlers.OUTCOME_NORMAL)

    def test_confirmed_normal_fixture_does_not_deliver(self):
        fixture = json.loads(
            (FIXTURE_DIR / "new_report_eth_usd_block_25526730.json").read_text()
        )
        decoded = fixture["decoded"]
        reported = Decimal(int(decoded["_value"], 16)) / Decimal(10**18)

        with mock.patch.object(
            handlers, "fetch_trusted_prices", return_value=[reported] * 2
        ), mock.patch.object(handlers, "send_alert") as send, mock.patch.object(
            tellor_lib, "http_json", side_effect=AssertionError("HTTP must not run")
        ):
            handlers.handle_disputable_value(FakeMatch(decoded))

        send.assert_not_called()

    def test_confirmed_dispute_fixture_replays_through_handler_in_explicit_log_only_mode(self):
        fixture = json.loads(
            (FIXTURE_DIR / "new_report_eth_usd_block_25526730.json").read_text()
        )
        decoded = fixture["decoded"]
        reported = Decimal(int(decoded["_value"], 16)) / Decimal(10**18)
        disputed = reported * Decimal("2")

        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "alerts.log"

            def log_only(match, content):
                return discord_routes.deliver_alert(
                    match.monitor_name,
                    content,
                    mode="log-only",
                    log_path=log_path,
                    context={
                        "network": match.network,
                        "tx": match.tx_hash,
                        "source_block": fixture["block_number"],
                    },
                )

            with mock.patch.object(
                handlers, "fetch_trusted_prices", return_value=[reported] * 2
            ), mock.patch.object(
                handlers, "send_alert", side_effect=log_only
            ) as send, mock.patch.object(
                tellor_lib, "http_json", side_effect=AssertionError("HTTP must not run")
            ), mock.patch.object(
                discord_routes.urllib.request,
                "urlopen",
                side_effect=AssertionError("HTTP must not run"),
            ):
                disputed_args = dict(decoded)
                disputed_args["_value"] = hex(int(disputed * Decimal(10**18)))
                handlers.handle_disputable_value(FakeMatch(disputed_args))

            send.assert_called_once()
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["mode"], "log-only")
            self.assertEqual(records[0]["source_block"], fixture["block_number"])
            self.assertEqual(
                records[0]["content"].splitlines()[0],
                f"**{handlers.OUTCOME_DISPUTE}**",
            )


if __name__ == "__main__":
    unittest.main()
