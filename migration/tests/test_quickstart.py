import base64
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MIGRATION_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = MIGRATION_DIR / "config" / "triggers" / "scripts"
sys.path.insert(0, str(MIGRATION_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import generic_alert  # noqa: E402
import quickstart  # noqa: E402
import tellor_lib  # noqa: E402


class SignatureTests(unittest.TestCase):
    def test_canonicalizes_common_types(self):
        signature, entry = quickstart.parse_function_signature("transfer(address, uint0256)")
        self.assertEqual(signature, "transfer(address,uint256)")
        self.assertEqual([item["name"] for item in entry["inputs"]], ["arg0", "arg1"])
        self.assertEqual([item["type"] for item in entry["inputs"]], ["address", "uint256"])

    def test_zero_argument_function(self):
        signature, entry = quickstart.parse_function_signature("deposit()")
        self.assertEqual(signature, "deposit()")
        self.assertEqual(entry["inputs"], [])

    def test_tuple_and_array_signature(self):
        signature, entry = quickstart.parse_function_signature("submit((address,uint256)[],bytes32)")
        self.assertEqual(signature, "submit((address,uint256)[],bytes32)")
        self.assertEqual(entry["inputs"][0]["type"], "tuple[]")
        self.assertEqual(entry["inputs"][0]["components"][1]["type"], "uint256")

    def test_canonicalizes_leading_zero_bytes_and_array_lengths(self):
        signature, entry = quickstart.parse_function_signature("f(bytes01,uint256[01])")
        self.assertEqual(signature, "f(bytes1,uint256[1])")
        self.assertEqual(entry["inputs"][1]["type"], "uint256[1]")

    def test_rejects_array_length_above_u64(self):
        with self.assertRaisesRegex(quickstart.QuickstartError, r"2\^64"):
            quickstart.parse_function_signature("f(uint256[18446744073709551616])")

    def test_rejects_empty_tuple_and_non_callable_names(self):
        with self.assertRaisesRegex(quickstart.QuickstartError, "empty tuple"):
            quickstart.parse_function_signature("f(())")
        for name in ("constructor", "fallback", "receive"):
            with self.subTest(name=name), self.assertRaisesRegex(
                quickstart.QuickstartError, "not a selector-bearing"
            ):
                quickstart.parse_function_signature(name + "()")

    def test_accepts_solidity_dollar_identifier(self):
        signature, entry = quickstart.parse_function_signature("$mint(uint256)")
        self.assertEqual(signature, "$mint(uint256)")
        self.assertEqual(entry["name"], "$mint")

    def test_rejects_invalid_integer_width(self):
        with self.assertRaisesRegex(quickstart.QuickstartError, "integer widths"):
            quickstart.parse_function_signature("bad(uint7)")

    def test_bare_name_resolves_unique_verified_abi(self):
        abi = [
            {
                "type": "function",
                "name": "transfer",
                "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "to", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "outputs": [],
            }
        ]
        signature, entry, source = quickstart.resolve_function("transfer", abi)
        self.assertEqual(signature, "transfer(address,uint256)")
        self.assertEqual([item["name"] for item in entry["inputs"]], ["to", "amount"])
        self.assertEqual(source, "sourcify")

    def test_bare_overload_requires_signature(self):
        abi = [
            {"type": "function", "name": "safeTransferFrom", "inputs": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "id", "type": "uint256"}], "outputs": []},
            {"type": "function", "name": "safeTransferFrom", "inputs": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "id", "type": "uint256"}, {"name": "data", "type": "bytes"}],
             "outputs": []},
        ]
        with self.assertRaisesRegex(quickstart.QuickstartError, "overloaded"):
            quickstart.resolve_function("safeTransferFrom", abi)

    def test_verified_unnamed_inputs_get_stable_placeholders(self):
        abi = [{
            "type": "function",
            "name": "set",
            "inputs": [{"name": "", "type": "uint256"}],
            "outputs": [],
        }]
        signature, entry, _source = quickstart.resolve_function("set", abi)
        self.assertEqual(signature, "set(uint256)")
        self.assertEqual(entry["inputs"][0]["name"], "arg0")

    def test_verified_input_fallback_names_do_not_collide(self):
        abi = [{
            "type": "function",
            "name": "set",
            "inputs": [
                {"name": "arg1", "type": "uint256"},
                {"name": "", "type": "uint256"},
            ],
            "outputs": [],
        }]
        _signature, entry, _source = quickstart.resolve_function("set", abi)
        names = [item["name"] for item in entry["inputs"]]
        self.assertEqual(len(names), len(set(names)))

    def test_verified_abi_mismatch_requires_explicit_override(self):
        abi = [{"type": "function", "name": "deposit", "inputs": [], "outputs": []}]
        with self.assertRaisesRegex(quickstart.QuickstartError, "does not contain"):
            quickstart.resolve_function("withdraw(uint256)", abi)
        signature, _entry, source = quickstart.resolve_function(
            "withdraw(uint256)", abi, allow_abi_mismatch=True
        )
        self.assertEqual(signature, "withdraw(uint256)")
        self.assertEqual(source, "synthetic")


class ValidationAndGenerationTests(unittest.TestCase):
    def setUp(self):
        self.signature, self.entry = quickstart.parse_function_signature("transfer(address,uint256)")

    def test_rejects_invalid_address_and_webhook(self):
        with self.assertRaises(quickstart.QuickstartError):
            quickstart.validate_address("0x1234")
        with self.assertRaises(quickstart.QuickstartError):
            quickstart.validate_webhook_url("https://example.com/api/webhooks/1/token")

    def test_message_placeholders(self):
        message = "${functions.0.signature} to ${functions.0.args.arg0}: ${transaction.hash}"
        self.assertEqual(quickstart.validate_message_template(message, self.entry), message)
        self.assertEqual(
            quickstart.validate_message_template(quickstart.DEFAULT_MESSAGE, self.entry),
            quickstart.DEFAULT_MESSAGE,
        )
        with self.assertRaisesRegex(quickstart.QuickstartError, "unknown message placeholder"):
            quickstart.validate_message_template("${functions.0.args.nope}", self.entry)
        with self.assertRaisesRegex(quickstart.QuickstartError, "2,000"):
            quickstart.validate_message_template("x" * 2001, self.entry)

    def test_generated_json_contains_no_webhook_secret(self):
        monitor = quickstart.build_monitor_config(
            "Transfer monitor",
            "0x" + "12" * 20,
            self.signature,
            self.entry,
        )
        trigger = quickstart.build_trigger_config("Transfer seen: ${transaction.hash}")
        serialized = json.dumps({"monitor": monitor, "trigger": trigger})
        self.assertIn(quickstart.WEBHOOK_ENV, serialized)
        self.assertNotIn("discord.com/api/webhooks", serialized)
        self.assertFalse(monitor["paused"])
        self.assertEqual(monitor["networks"], ["ethereum_mainnet"])
        self.assertEqual(monitor["match_conditions"]["transactions"], [])

    def test_env_update_preserves_values_and_sets_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("KEEP_ME=value\nRPC_ETHEREUM_MAINNET=old\n")
            os.chmod(str(path), 0o644)
            quickstart.update_env_file(
                {
                    quickstart.RPC_ENV: "https://rpc.example/v1/key",
                    quickstart.WEBHOOK_ENV: "https://discord.com/api/webhooks/123/token",
                },
                path,
            )
            values = quickstart.read_env_values(path)
            self.assertEqual(values["KEEP_ME"], "value")
            self.assertEqual(values[quickstart.RPC_ENV], "https://rpc.example/v1/key")
            self.assertEqual(values[quickstart.WEBHOOK_ENV], "https://discord.com/api/webhooks/123/token")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_env_update_removes_duplicate_destination_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "CUSTOM_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/1/old-a'\n"
                "KEEP=value\n"
                "CUSTOM_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/2/old-b'\n"
            )
            new_value = "https://discord.com/api/webhooks/3/new"
            quickstart.update_env_file({quickstart.WEBHOOK_ENV: new_value}, path)
            self.assertEqual(quickstart.read_env_values(path)[quickstart.WEBHOOK_ENV], new_value)
            self.assertEqual(path.read_text().count(quickstart.WEBHOOK_ENV + "="), 1)

    def test_first_env_update_seeds_complete_example(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            example = root / ".env.example"
            example.write_text("RPC_SEPOLIA=https://sepolia.example\nUNRELATED=kept\n")
            quickstart.update_env_file(
                {quickstart.RPC_ENV: "https://mainnet.example"},
                path,
                template_path=example,
            )
            values = quickstart.read_env_values(path)
            self.assertEqual(values["RPC_SEPOLIA"], "https://sepolia.example")
            self.assertEqual(values["UNRELATED"], "kept")
            self.assertEqual(values[quickstart.RPC_ENV], "https://mainnet.example")

    def test_check_rejects_logged_error_even_on_zero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = root / "monitor.json"
            trigger = root / "trigger.json"
            monitor.touch()
            trigger.touch()
            with mock.patch.object(quickstart, "MONITOR_PATH", monitor), mock.patch.object(
                quickstart, "TRIGGER_PATH", trigger
            ), mock.patch.object(
                quickstart,
                "run_compose",
                return_value=(0, "\x1b[31mERROR\x1b[0m Error occurred, failed to resolve RPC_SEPOLIA"),
            ):
                with self.assertRaisesRegex(quickstart.QuickstartError, "despite exit code 0"):
                    quickstart.check_config()

    def test_offline_configure_cannot_start(self):
        args = quickstart.make_parser().parse_args(["configure", "--offline", "--start"])
        with self.assertRaisesRegex(quickstart.QuickstartError, "cannot be combined"):
            quickstart.configure(args)

    def test_replay_runs_mainnet_contract_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor_path = Path(directory) / "monitor.json"
            monitor_path.write_text(json.dumps({"addresses": [{"address": "0x" + "12" * 20}]}))
            args = SimpleNamespace(block=123, send=False, yes=False)
            with mock.patch.object(quickstart, "MONITOR_PATH", monitor_path), mock.patch.object(
                quickstart,
                "read_env_values",
                return_value={quickstart.RPC_ENV: "https://rpc.example"},
            ), mock.patch.object(quickstart, "validate_mainnet_contract") as preflight, mock.patch.object(
                quickstart, "run_compose", return_value=(0, "")
            ):
                quickstart.replay(args)
            preflight.assert_called_once()

    def test_rpc_health_skips_optional_placeholders_and_checks_mainnet(self):
        values = {
            quickstart.RPC_ENV: "https://mainnet.example",
            "RPC_SEPOLIA": "https://sepolia.example/v3/YOUR_NEW_INFURA_KEY",
        }
        with mock.patch.object(quickstart, "read_env_values", return_value=values), mock.patch.object(
            quickstart, "_rpc_call", return_value="0x1"
        ) as rpc, mock.patch("builtins.print") as output:
            quickstart.check_rpc_health()
        rpc.assert_called_once_with("https://mainnet.example", "eth_chainId", [])
        printed = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("RPC_ETHEREUM_MAINNET ok chain_id=1", printed)
        self.assertIn("RPC_SEPOLIA optional missing", printed)
        self.assertIn("RPC health checks passed", printed)

    def test_rpc_health_aggregates_wrong_chain_failure(self):
        with mock.patch.object(
            quickstart,
            "read_env_values",
            return_value={quickstart.RPC_ENV: "https://mainnet.example"},
        ), mock.patch.object(quickstart, "_rpc_call", return_value="0x5"), mock.patch(
            "builtins.print"
        ):
            with self.assertRaisesRegex(quickstart.QuickstartError, quickstart.RPC_ENV):
                quickstart.check_rpc_health()

    def test_rpc_call_converts_url_credentials_to_basic_auth(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'
        with mock.patch.object(quickstart.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(
                quickstart._rpc_call("https://user:p%40ss@rpc.example/rpc", "eth_chainId", []),
                "0x1",
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://rpc.example/rpc")
        self.assertEqual(
            request.get_header("Authorization"),
            "Basic " + base64.b64encode(b"user:p@ss").decode(),
        )


def _payload():
    return {
        "monitor_match": {
            "EVM": {
                "monitor": {"name": "Transfer monitor"},
                "network_slug": "ethereum_mainnet",
                "transaction": {
                    "hash": "0xabc",
                    "from": "0xfrom",
                    "to": "0xto",
                    "value": "0",
                },
                "receipt": {"status": "0x1"},
                "matched_on_args": {
                    "functions": [
                        {
                            "signature": "transfer(address,uint256)",
                            "args": [
                                {"name": "arg0", "value": "0xdestination"},
                                {"name": "arg1", "value": "42"},
                            ],
                        }
                    ],
                    "events": None,
                },
            }
        }
    }


class RenderingAndDeliveryTests(unittest.TestCase):
    def test_abi_decoder_rejects_truncated_or_noncanonical_values(self):
        malformed = [
            (["address"], b""),
            (["address"], b"\x01" + bytes(31)),
            (["bytes", "uint256"], b""),
            (["bytes"], (1).to_bytes(32, "big") + bytes(32)),
            (["bytes"], (32).to_bytes(32, "big") + (1).to_bytes(32, "big")),
        ]
        for types, data in malformed:
            with self.subTest(types=types, data=data.hex()):
                with self.assertRaises(ValueError):
                    tellor_lib.abi_decode(types, data)

    def test_unix_timestamp_formatter_labels_out_of_range_values(self):
        self.assertEqual(
            tellor_lib.unix_utc(2**256 - 1),
            "invalid timestamp (out of range)",
        )
        self.assertEqual(tellor_lib.unix_utc("not-a-number"), "invalid timestamp")

    def test_http_json_converts_url_credentials_to_basic_auth(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"result":"0x1"}'
        with mock.patch.object(tellor_lib.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(
                tellor_lib.http_json("https://user:secret@rpc.example/rpc", body={"id": 1}),
                {"result": "0x1"},
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://rpc.example/rpc")
        self.assertEqual(
            request.get_header("Authorization"),
            "Basic " + base64.b64encode(b"user:secret").decode(),
        )

    def test_generic_template_rendering(self):
        match = tellor_lib.Match(_payload())
        rendered = generic_alert.render_message(
            "${monitor.name}: ${functions.0.signature} ${function.args.arg1} ${transaction.hash}",
            match,
        )
        self.assertEqual(rendered, "Transfer monitor: transfer(address,uint256) 42 0xabc")

    def test_default_message_uses_normalized_format(self):
        match = tellor_lib.Match(_payload())
        with mock.patch.object(generic_alert, "now_utc", return_value="2026-07-14 03:00:00 UTC"):
            rendered = generic_alert.render_message(quickstart.DEFAULT_MESSAGE, match)
        self.assertEqual(
            rendered,
            "**Transfer monitor**\n"
            "> Network: `ethereum_mainnet`\n"
            "> Function: `transfer(address,uint256)`\n"
            "> Observed: `2026-07-14 03:00:00 UTC`\n"
            "> [View transaction](https://etherscan.io/tx/0xabc)",
        )

    def test_dynamic_arguments_are_markdown_escaped(self):
        payload = _payload()
        payload["monitor_match"]["EVM"]["matched_on_args"]["functions"][0]["args"][1][
            "value"
        ] = "[click](https://evil.example)\n@everyone `break`"
        rendered = generic_alert.render_message("${function.args.arg1}", tellor_lib.Match(payload))
        self.assertNotIn("[click](https://evil.example)", rendered)
        self.assertNotIn("\n", rendered)
        self.assertIn(r"\[click\]", rendered)
        self.assertNotIn("`break`", rendered)
        self.assertIn("ˋbreakˋ", rendered)
        summary = generic_alert.render_message("${functions}", tellor_lib.Match(payload))
        self.assertNotIn("[click](https://evil.example)", summary)
        self.assertIn(r"\[click\]", summary)
        self.assertNotIn("`break`", summary)
        self.assertIn("ˋbreakˋ", summary)

    def test_failed_function_receipt_is_suppressed(self):
        payload = _payload()
        payload["monitor_match"]["EVM"]["receipt"]["status"] = "0x0"
        self.assertFalse(tellor_lib.function_match_succeeded(tellor_lib.Match(payload)))

    def test_missing_receipt_fetches_only_the_matched_transaction(self):
        payload = _payload()
        payload["monitor_match"]["EVM"]["receipt"] = None
        match = tellor_lib.Match(payload)
        with mock.patch.dict(os.environ, {"RPC_ETHEREUM_MAINNET": "https://rpc.invalid"}), mock.patch.object(
            tellor_lib, "http_json", return_value={"result": {"status": "0x1"}}
        ) as rpc:
            self.assertTrue(tellor_lib.function_match_succeeded(match))
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(rpc.call_args.kwargs["body"]["method"], "eth_getTransactionReceipt")
        self.assertEqual(rpc.call_args.kwargs["body"]["params"], ["0xabc"])

    def test_missing_receipt_rpc_failure_is_sanitized(self):
        payload = _payload()
        payload["monitor_match"]["EVM"]["receipt"] = None
        with mock.patch.dict(os.environ, {"RPC_ETHEREUM_MAINNET": "https://secret.invalid/key"}), mock.patch.object(
            tellor_lib, "http_json", side_effect=OSError("https://secret.invalid/key")
        ):
            with self.assertRaisesRegex(RuntimeError, "RPC request failed") as raised:
                tellor_lib.function_match_succeeded(tellor_lib.Match(payload))
        self.assertNotIn("secret.invalid", str(raised.exception))

    def test_confirmed_webhook_payload_disables_mentions(self):
        with mock.patch.object(tellor_lib, "http_json", return_value={"id": "1"}) as post:
            tellor_lib.post_discord_webhook(
                "https://discord.com/api/webhooks/123/token",
                "hello @everyone",
            )
        url = post.call_args.args[0]
        body = post.call_args.kwargs["body"]
        self.assertIn("wait=true", url)
        self.assertEqual(body["content"], "hello @everyone")
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_rate_limit_retry_uses_full_retry_after(self):
        error = urllib.error.HTTPError(
            "https://redacted.invalid",
            429,
            "rate limited",
            {"Retry-After": "12.75"},
            io.BytesIO(b"{}"),
        )
        sleeps = []
        with mock.patch.object(tellor_lib, "http_json", side_effect=[error, {}]):
            tellor_lib.post_discord_webhook(
                "https://redacted.invalid/hook",
                "hello",
                sleep=sleeps.append,
            )
        self.assertEqual(sleeps, [12.75])

    def test_dynamic_message_is_capped_at_discord_limit(self):
        content = tellor_lib._discord_content("x" * 2500)
        self.assertEqual(len(content), tellor_lib.DISCORD_CONTENT_LIMIT)
        self.assertIn("truncated", content)

    def test_webhook_runtime_error_becomes_clean_cli_error(self):
        with mock.patch.object(
            tellor_lib, "post_discord_webhook", side_effect=RuntimeError("Discord webhook delivery failed")
        ):
            with self.assertRaisesRegex(quickstart.QuickstartError, "delivery failed"):
                quickstart.test_webhook("https://discord.com/api/webhooks/123/token")

    def test_docker_output_redacts_all_secret_like_env_values(self):
        with mock.patch.object(
            quickstart,
            "read_env_values",
            return_value={
                "RPC_SEPOLIA": "https://rpc.example/private",
                "CMC_PRO_API_KEY": "top-secret-key",
                "LOG_LEVEL": "info",
            },
        ):
            output = quickstart._redact_output(
                "rpc=https://rpc.example/private key=top-secret-key level=info"
            )
        self.assertNotIn("private", output)
        self.assertNotIn("top-secret-key", output)
        self.assertIn("level=info", output)


if __name__ == "__main__":
    unittest.main()
