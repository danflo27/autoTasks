#!/usr/bin/env python3
"""Interactive configurator for one Ethereum function -> Discord monitor.

The generated OpenZeppelin Monitor files are intentionally ignored by git. RPC
and Discord URLs are written only to migration/.env with mode 0600.
"""

import argparse
import base64
import copy
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
MONITOR_PATH = BASE_DIR / "config" / "monitors" / "quickstart_function.json"
TRIGGER_PATH = BASE_DIR / "config" / "triggers" / "quickstart_function.json"
SCRIPTS_DIR = BASE_DIR / "config" / "triggers" / "scripts"
TRIGGER_ID = "quickstart_function_alert"
RPC_ENV = "RPC_ETHEREUM_MAINNET"
WEBHOOK_ENV = "CUSTOM_DISCORD_WEBHOOK_URL"
SOURCIFY_ABI_URL = "https://sourcify.dev/server/v2/contract/1/{address}?fields=abi"
RPC_HEALTH_CHECKS = (
    ("RPC_ETHEREUM_MAINNET", 1, True),
    ("RPC_SEPOLIA", 11155111, False),
    ("RPC_POLYGON", 137, False),
    ("RPC_OPTIMISM", 10, False),
    ("RPC_GNOSIS", 100, False),
    ("RPC_CHIADO", 10200, False),
)
DEFAULT_MESSAGE = (
    "**${monitor.name}**\n"
    "> Network: `${network.slug}`\n"
    "> Function: `${functions.0.signature}`\n"
    "> Observed: `${observed.utc}`\n"
    "> [View transaction](https://etherscan.io/tx/${transaction.hash})"
)
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
NAME_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")
ARRAY_SUFFIX_RE = re.compile(r"(?:\[[0-9]*\])*$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ENV_PLACEHOLDER_RE = re.compile(r"YOUR_[A-Z0-9_]+|<[A-Za-z0-9_-]+>")


class QuickstartError(Exception):
    """Actionable configuration error safe to show without leaking secrets."""


def validate_address(value):
    value = value.strip()
    if not ADDRESS_RE.fullmatch(value):
        raise QuickstartError("contract address must be 0x followed by exactly 40 hex characters")
    return value


def validate_rpc_url(value):
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise QuickstartError("RPC URL is invalid") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise QuickstartError("RPC URL must be an http:// or https:// URL")
    if parsed.fragment or any(ord(char) < 32 for char in value):
        raise QuickstartError("RPC URL contains unsupported characters")
    return value


def validate_webhook_url(value):
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise QuickstartError("Discord webhook URL is invalid") from None
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/[0-9]+/[^/]+", path)
    ):
        raise QuickstartError(
            "Discord webhook must look like https://discord.com/api/webhooks/<id>/<token>"
        )
    return value


def _split_top_level(value):
    if not value:
        return []
    parts = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise QuickstartError("function signature has unbalanced parentheses")
        elif char == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    if depth != 0:
        raise QuickstartError("function signature has unbalanced parentheses")
    parts.append(value[start:])
    if any(not part for part in parts):
        raise QuickstartError("function signature contains an empty parameter type")
    return parts


def _canonical_elementary_type(value):
    if value == "uint":
        return "uint256"
    if value == "int":
        return "int256"
    if value == "byte":
        return "bytes1"
    if value in ("address", "bool", "string", "bytes", "function"):
        return value
    match = re.fullmatch(r"(u?int)([0-9]+)", value)
    if match:
        bits = int(match.group(2))
        if 8 <= bits <= 256 and bits % 8 == 0:
            return "{}{}".format(match.group(1), bits)
        raise QuickstartError("integer widths must be a multiple of 8 from 8 through 256")
    match = re.fullmatch(r"bytes([0-9]+)", value)
    if match:
        size = int(match.group(1))
        if 1 <= size <= 32:
            return "bytes{}".format(size)
        raise QuickstartError("fixed bytes widths must be from bytes1 through bytes32")
    if value.startswith("fixed") or value.startswith("ufixed"):
        raise QuickstartError("fixed-point ABI types are not supported by this quickstart")
    raise QuickstartError(
        "unsupported ABI type {!r}; use canonical Solidity ABI types such as address or uint256".format(
            value
        )
    )


def _parse_abi_type(value, name):
    value = re.sub(r"\s+", "", value)
    if not value:
        raise QuickstartError("function signature contains an empty parameter type")

    suffix_match = ARRAY_SUFFIX_RE.search(value)
    suffix = suffix_match.group(0)
    base = value[: len(value) - len(suffix)] if suffix else value
    normalized_dimensions = []
    for length in re.findall(r"\[([0-9]*)\]", suffix):
        if length and int(length) <= 0:
            raise QuickstartError("fixed array lengths must be greater than zero")
        if length and int(length) > 2**64 - 1:
            raise QuickstartError("fixed array lengths cannot exceed 2^64 - 1")
        normalized_dimensions.append("[]" if not length else "[{}]".format(int(length)))
    canonical_suffix = "".join(normalized_dimensions)

    if base.startswith("("):
        if not base.endswith(")"):
            raise QuickstartError("tuple type has unbalanced parentheses")
        component_text = base[1:-1]
        if not component_text:
            raise QuickstartError("empty tuple ABI types are not valid Solidity function parameters")
        components = []
        canonical_components = []
        for index, part in enumerate(_split_top_level(component_text)):
            canonical, component = _parse_abi_type(part, "component{}".format(index))
            canonical_components.append(canonical)
            components.append(component)
        canonical = "({}){}".format(",".join(canonical_components), canonical_suffix)
        return canonical, {
            "name": name,
            "type": "tuple" + canonical_suffix,
            "components": components,
        }

    canonical_base = _canonical_elementary_type(base)
    return canonical_base + canonical_suffix, {
        "name": name,
        "type": canonical_base + canonical_suffix,
    }


def parse_function_signature(value):
    value = value.strip()
    match = re.fullmatch(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\((.*)\)", value)
    if not match:
        if NAME_RE.fullmatch(value):
            raise QuickstartError(
                "a bare function name needs a verified ABI; otherwise enter a canonical signature "
                "such as {}()".format(value)
            )
        raise QuickstartError("function must be a name or canonical signature such as transfer(address,uint256)")

    function_name, params_text = match.groups()
    if function_name in ("constructor", "fallback", "receive"):
        raise QuickstartError(
            "{} is not a selector-bearing callable function; use an event or transaction monitor".format(
                function_name
            )
        )
    inputs = []
    canonical_types = []
    compact_params = re.sub(r"\s+", "", params_text)
    for index, part in enumerate(_split_top_level(compact_params)):
        canonical, abi_input = _parse_abi_type(part, "arg{}".format(index))
        canonical_types.append(canonical)
        inputs.append(abi_input)
    signature = "{}({})".format(function_name, ",".join(canonical_types))
    entry = {
        "type": "function",
        "name": function_name,
        "stateMutability": "nonpayable",
        "inputs": inputs,
        "outputs": [],
    }
    return signature, entry


def _canonical_type_from_abi(param):
    abi_type = param.get("type", "")
    if abi_type.startswith("tuple"):
        suffix = abi_type[len("tuple") :]
        if not ARRAY_SUFFIX_RE.fullmatch(suffix):
            raise QuickstartError("verified ABI contains an invalid tuple type")
        components = param.get("components")
        if not isinstance(components, list) or not components:
            raise QuickstartError("verified ABI tuple is missing components")
        raw = "({}){}".format(",".join(_canonical_type_from_abi(item) for item in components), suffix)
        return _parse_abi_type(raw, "")[0]
    return _parse_abi_type(abi_type, param.get("name", ""))[0]


def function_signature_from_abi(entry):
    name = entry.get("name", "")
    if not NAME_RE.fullmatch(name) or entry.get("type") != "function":
        raise QuickstartError("verified ABI contains an invalid function entry")
    inputs = entry.get("inputs")
    if not isinstance(inputs, list):
        raise QuickstartError("verified ABI function is missing its inputs array")
    return "{}({})".format(name, ",".join(_canonical_type_from_abi(item) for item in inputs))


def _normalize_input_names(entry):
    seen = set()
    for index, item in enumerate(entry.get("inputs") or []):
        name = item.get("name", "")
        if not NAME_RE.fullmatch(name) or name in seen:
            name = "arg{}".format(index)
            while name in seen:
                name += "_"
            item["name"] = name
        seen.add(name)
    return entry


def resolve_function(identifier, abi=None, allow_abi_mismatch=False):
    identifier = identifier.strip()
    functions = [item for item in (abi or []) if isinstance(item, dict) and item.get("type") == "function"]

    if "(" not in identifier:
        if not NAME_RE.fullmatch(identifier):
            raise QuickstartError("function name is invalid")
        matches = [item for item in functions if item.get("name") == identifier]
        if not matches:
            raise QuickstartError(
                "Sourcify did not provide a unique {!r} ABI; enter the full canonical signature".format(
                    identifier
                )
            )
        if len(matches) > 1:
            choices = sorted(function_signature_from_abi(item) for item in matches)
            raise QuickstartError(
                "function {!r} is overloaded; choose one of: {}".format(identifier, ", ".join(choices))
            )
        entry = _normalize_input_names(copy.deepcopy(matches[0]))
        return function_signature_from_abi(entry), entry, "sourcify"

    signature, synthetic_entry = parse_function_signature(identifier)
    matches = []
    for item in functions:
        if item.get("name") != synthetic_entry["name"]:
            continue
        try:
            if function_signature_from_abi(item) == signature:
                matches.append(item)
        except QuickstartError:
            continue
    if len(matches) == 1:
        return signature, _normalize_input_names(copy.deepcopy(matches[0])), "sourcify"
    if abi is not None and not allow_abi_mismatch:
        raise QuickstartError(
            "verified ABI does not contain {}; correct the signature or use "
            "--allow-unknown-signature for a proxy/implementation function after verifying it manually".format(
                signature
            )
        )
    return signature, synthetic_entry, "synthetic"


def fetch_sourcify_abi(address, timeout=10):
    url = SOURCIFY_ABI_URL.format(address=quote(address, safe=""))
    request = urllib.request.Request(url, headers={"User-Agent": "tellor-monitor-quickstart/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise QuickstartError("Sourcify ABI lookup failed with HTTP {}".format(error.code)) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    abi = payload.get("abi") if isinstance(payload, dict) else None
    return abi if isinstance(abi, list) else None


def _rpc_call(rpc_url, method, params, timeout=10):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "tellor-monitor-quickstart/1.0"}
    parsed = urlsplit(rpc_url)
    if parsed.username is not None:
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = "[{}]".format(hostname)
        netloc = hostname
        if parsed.port is not None:
            netloc += ":{}".format(parsed.port)
        rpc_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        credentials = "{}:{}".format(unquote(parsed.username), unquote(parsed.password or ""))
        headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
    request = urllib.request.Request(
        rpc_url,
        data=body,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise QuickstartError("Ethereum RPC rejected a request with HTTP {}".format(error.code)) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise QuickstartError("Ethereum RPC request failed; check the URL and network access") from None
    if not isinstance(payload, dict) or payload.get("error"):
        raise QuickstartError("Ethereum RPC returned an error for {}".format(method))
    if "result" not in payload:
        raise QuickstartError("Ethereum RPC returned no result for {}".format(method))
    return payload["result"]


def validate_mainnet_contract(rpc_url, address):
    chain_id = _rpc_call(rpc_url, "eth_chainId", [])
    try:
        parsed_chain_id = int(chain_id, 16)
    except (TypeError, ValueError):
        raise QuickstartError("Ethereum RPC returned an invalid chain ID") from None
    if parsed_chain_id != 1:
        raise QuickstartError("RPC is chain ID {}, not Ethereum mainnet (chain ID 1)".format(parsed_chain_id))
    code = _rpc_call(rpc_url, "eth_getCode", [address, "latest"])
    if not isinstance(code, str) or code.lower() in ("0x", "0x0", "0x00"):
        raise QuickstartError("no deployed contract bytecode was found at that address on mainnet")


def check_rpc_health():
    """Validate required and configured optional RPCs without printing URLs."""
    values = read_env_values()
    failures = []
    for env_name, expected_chain_id, required in RPC_HEALTH_CHECKS:
        rpc_url = values.get(env_name, "").strip()
        if not rpc_url or ENV_PLACEHOLDER_RE.search(rpc_url):
            if required:
                print("{} required missing".format(env_name))
                failures.append(env_name)
            else:
                print("{} optional missing".format(env_name))
            continue
        try:
            chain_id = _rpc_call(validate_rpc_url(rpc_url), "eth_chainId", [])
            if isinstance(chain_id, bool):
                raise ValueError
            actual_chain_id = chain_id if isinstance(chain_id, int) else int(chain_id, 16)
        except (QuickstartError, TypeError, ValueError) as error:
            reason = str(error) if isinstance(error, QuickstartError) else "invalid chain ID"
            print("{} failed: {}".format(env_name, reason))
            failures.append(env_name)
            continue
        if actual_chain_id != expected_chain_id:
            print(
                "{} wrong chain_id={} expected={}".format(
                    env_name, actual_chain_id, expected_chain_id
                )
            )
            failures.append(env_name)
        else:
            print("{} ok chain_id={}".format(env_name, actual_chain_id))
    if failures:
        raise QuickstartError("RPC health checks failed for {}".format(", ".join(failures)))
    print("RPC health checks passed.")


def validate_message_template(message, abi_entry):
    if not message or not message.strip():
        raise QuickstartError("Discord message cannot be empty")
    if len(message) > 2000:
        raise QuickstartError("Discord message template cannot exceed 2,000 characters")
    input_names = {
        item.get("name") or "arg{}".format(index)
        for index, item in enumerate(abi_entry.get("inputs") or [])
    }
    fixed = {
        "monitor.name",
        "network.slug",
        "transaction.hash",
        "transaction.from",
        "transaction.to",
        "transaction.value",
        "observed.utc",
        "functions",
        "functions.0.signature",
        "function.signature",
    }
    unknown = []
    for placeholder in PLACEHOLDER_RE.findall(message):
        if placeholder in fixed:
            continue
        for prefix in ("functions.0.args.", "function.args."):
            if placeholder.startswith(prefix) and placeholder[len(prefix) :] in input_names:
                break
        else:
            unknown.append(placeholder)
    if unknown:
        raise QuickstartError("unknown message placeholder(s): {}".format(", ".join(sorted(set(unknown)))))
    return message


def build_monitor_config(name, address, signature, abi_entry):
    if not name.strip():
        raise QuickstartError("monitor name cannot be empty")
    return {
        "name": name.strip(),
        "paused": False,
        "networks": ["ethereum_mainnet"],
        "addresses": [{"address": address, "contract_spec": [abi_entry]}],
        "match_conditions": {
            "functions": [{"signature": signature, "expression": None}],
            "events": [],
            # generic_alert.py performs one receipt check after the function
            # selector matches. Leaving this empty avoids v1.5 fetching a
            # receipt for every transaction in zero-log blocks.
            "transactions": [],
        },
        "trigger_conditions": [],
        "triggers": [TRIGGER_ID],
    }


def build_trigger_config(message):
    return {
        TRIGGER_ID: {
            "name": "Quickstart Function Discord Alert",
            "trigger_type": "script",
            "config": {
                "language": "Python",
                "script_path": "./config/triggers/scripts/generic_alert.py",
                "arguments": [message, WEBHOOK_ENV],
                "timeout_ms": 180000,
            },
        }
    }


def _decode_env_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def read_env_values(path=ENV_PATH):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = _decode_env_value(value)
    return values


def _dotenv_quote(value):
    if any(char in value for char in ("'", "\n", "\r", "\x00")):
        raise QuickstartError("URLs cannot contain quotes or control characters")
    return "'{}'".format(value)


def _atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False, encoding="utf-8")
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temp_path), mode)
        os.replace(str(temp_path), str(path))
    finally:
        if temp_path.exists():
            temp_path.unlink()


def update_env_file(updates, path=ENV_PATH, template_path=None):
    if template_path is None and path == ENV_PATH:
        template_path = ENV_EXAMPLE_PATH
    if path.exists():
        lines = path.read_text().splitlines()
    elif template_path is not None and template_path.exists():
        lines = template_path.read_text().splitlines()
    else:
        lines = []
    remaining = dict(updates)
    update_keys = set(updates)
    written_updates = set()
    output = []
    for line in lines:
        match = re.match(r"^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(2) in update_keys:
            key = match.group(2)
            if key not in written_updates:
                output.append("{}={}".format(key, _dotenv_quote(updates[key])))
                written_updates.add(key)
                remaining.pop(key, None)
        else:
            output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# Quickstart custom function monitor")
        for key, value in remaining.items():
            output.append("{}={}".format(key, _dotenv_quote(value)))
    _atomic_write(path, "\n".join(output).rstrip() + "\n", mode=0o600)
    os.chmod(str(path), 0o600)


def write_generated_configs(monitor_config, trigger_config):
    _atomic_write(MONITOR_PATH, json.dumps(monitor_config, indent=2) + "\n")
    _atomic_write(TRIGGER_PATH, json.dumps(trigger_config, indent=2) + "\n")


def _redact_output(value):
    secrets = read_env_values()
    for key, secret in secrets.items():
        if secret and any(
            marker in key.upper()
            for marker in ("RPC", "KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK")
        ):
            value = value.replace(secret, "<redacted:{}>".format(key))
    value = re.sub(
        r"https://discord\.com/api(?:/v[0-9]+)?/webhooks/[0-9]+/[^\s?'\"]+",
        "<redacted:discord-webhook>",
        value,
    )
    return value


def run_compose(arguments, check=True, extra_env=None, collect=False):
    command = ["docker", "compose"] + list(arguments)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            env=extra_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise QuickstartError("Docker CLI was not found; install Docker Desktop or Docker Engine") from None
    output = []
    try:
        for line in process.stdout:
            safe_line = _redact_output(line)
            print(safe_line, end="")
            if collect:
                output.append(safe_line)
        returncode = process.wait()
    except KeyboardInterrupt:
        process.send_signal(2)
        process.wait()
        raise
    if check and returncode:
        raise QuickstartError("Docker Compose command failed with exit code {}".format(returncode))
    return returncode, "".join(output)


def check_config():
    print("Validating all Monitor configuration...")
    returncode, output = run_compose(
        ["run", "--rm", "monitor", "--check"], check=False, collect=True
    )
    normalized = ANSI_ESCAPE_RE.sub("", output)
    reported_error = re.search(
        r"(?im)\bERROR\b|Error occurred|configuration (?:error|failed)|failed to (?:load|resolve|validate|initialize)",
        normalized,
    )
    if returncode or reported_error:
        raise QuickstartError(
            "Monitor configuration validation reported an error"
            + (" (exit {})".format(returncode) if returncode else " despite exit code 0")
        )
    print("Monitor configuration is valid.")


def verify_monitor_running(delay=3):
    time.sleep(delay)
    _returncode, output = run_compose(
        ["ps", "--status", "running", "--services", "monitor"],
        check=False,
        collect=True,
    )
    services = [line.strip() for line in ANSI_ESCAPE_RE.sub("", output).splitlines()]
    if "monitor" not in services:
        run_compose(["logs", "--tail", "80", "monitor"], check=False)
        raise QuickstartError("monitor container did not remain running after initialization")
    print("Monitor container is running with the generated configuration.")


def start_monitor():
    values = read_env_values()
    rpc_url = values.get(RPC_ENV, "")
    webhook = values.get(WEBHOOK_ENV, "")
    if not rpc_url or not webhook:
        raise QuickstartError("configured RPC and custom Discord webhook are required before start")
    validate_rpc_url(rpc_url)
    validate_webhook_url(webhook)
    try:
        monitor = json.loads(MONITOR_PATH.read_text())
        address = monitor["addresses"][0]["address"]
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise QuickstartError("generated monitor configuration is missing or invalid") from None
    print("Checking mainnet chain ID and contract bytecode...")
    validate_mainnet_contract(rpc_url, validate_address(address))
    check_config()
    print("Starting the monitor service...")
    run_compose(["up", "-d", "--force-recreate", "monitor"])
    verify_monitor_running()


def test_webhook(webhook):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from tellor_lib import post_discord_webhook
    finally:
        sys.path.pop(0)
    try:
        post_discord_webhook(
            webhook,
            "OpenZeppelin Monitor quickstart test: webhook delivery is working. Mentions are disabled.",
        )
    except Exception as error:
        raise QuickstartError(str(error)) from None
    print("Discord confirmed the test message.")


def _prompt(label, default=None, secret=False):
    suffix = " [{}]".format(default) if default else ""
    reader = getpass.getpass if secret else input
    value = reader("{}{}: ".format(label, suffix)).strip()
    return value or (default or "")


def _prompt_secret_or_existing(label, existing):
    if existing and _yes_no("Use the existing {} from .env?".format(label), True):
        return existing
    return _prompt(label, secret=True)


def _yes_no(question, default=True):
    prompt = " [Y/n] " if default else " [y/N] "
    answer = input(question + prompt).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _prompt_message(default):
    print("Discord message template (Markdown allowed). Finish with a line containing only a dot.")
    print("Press Enter then '.' to use this default:\n{}".format(default))
    lines = []
    while True:
        line = input()
        if line == ".":
            break
        lines.append(line)
    message = "\n".join(lines).strip()
    return message or default


def configure(args):
    interactive = sys.stdin.isatty()
    if args.offline and args.start:
        raise QuickstartError("--offline cannot be combined with --start; validate online before live start")
    if args.log_only and args.start:
        raise QuickstartError("--log-only cannot be combined with --start; a live start requires Discord")
    existing = read_env_values()
    name = args.name or (_prompt("Monitor name", "Custom Ethereum Function") if interactive else "")
    address = args.address or (_prompt("Ethereum mainnet contract address") if interactive else "")
    identifier = args.function or (_prompt("Function name or canonical signature") if interactive else "")
    if not name or not address or not identifier:
        raise QuickstartError("name, address, and function are required")
    address = validate_address(address)

    abi = None
    if not args.offline:
        print("Looking for a verified ABI in Sourcify v2...")
        abi = fetch_sourcify_abi(address)
    signature, abi_entry, abi_source = resolve_function(
        identifier, abi, allow_abi_mismatch=args.allow_unknown_signature
    )
    input_names = [item.get("name") or "arg{}".format(i) for i, item in enumerate(abi_entry.get("inputs") or [])]
    if input_names:
        print("Function arguments available in messages: {}".format(", ".join(input_names)))
    print("Resolved function: {} ({} ABI)".format(signature, abi_source))

    message = args.message
    if message is None:
        if not interactive:
            raise QuickstartError("message is required in non-interactive mode")
        message = _prompt_message(DEFAULT_MESSAGE)
    validate_message_template(message, abi_entry)

    rpc_url = os.environ.get("QUICKSTART_RPC_URL") or existing.get(RPC_ENV, "")
    webhook = os.environ.get("QUICKSTART_DISCORD_WEBHOOK_URL") or existing.get(WEBHOOK_ENV, "")
    if interactive:
        rpc_url = _prompt_secret_or_existing("Ethereum mainnet RPC URL", rpc_url)
        if not args.log_only:
            webhook = _prompt_secret_or_existing("Discord webhook URL", webhook)
    if not rpc_url:
        raise QuickstartError(
            "RPC URL is required; use the hidden prompt or set QUICKSTART_RPC_URL for automation"
        )
    rpc_url = validate_rpc_url(rpc_url)
    if args.log_only:
        webhook = ""
    elif not webhook:
        raise QuickstartError(
            "Discord webhook is required; use the hidden prompt or set QUICKSTART_DISCORD_WEBHOOK_URL"
        )
    else:
        webhook = validate_webhook_url(webhook)

    if not args.offline:
        print("Checking mainnet chain ID and contract bytecode...")
        validate_mainnet_contract(rpc_url, address)

    monitor = build_monitor_config(name, address, signature, abi_entry)
    trigger = build_trigger_config(message)
    if (MONITOR_PATH.exists() or TRIGGER_PATH.exists()) and not args.force:
        if not interactive or not _yes_no("Replace the existing quickstart configuration?", False):
            raise QuickstartError("quickstart configuration already exists; rerun with --force to replace it")

    update_env_file({RPC_ENV: rpc_url, WEBHOOK_ENV: webhook})
    write_generated_configs(monitor, trigger)
    print("Saved {}".format(MONITOR_PATH.relative_to(BASE_DIR)))
    print("Saved {}".format(TRIGGER_PATH.relative_to(BASE_DIR)))
    print("Saved secrets to .env (mode 0600); no secret was written to JSON or printed.")

    should_test = bool(args.test_webhook)
    if interactive and webhook and not args.test_webhook:
        should_test = _yes_no("Send one configuration-test message to Discord now?", True)
    if should_test:
        if not webhook:
            raise QuickstartError("cannot test a webhook in --log-only mode")
        test_webhook(webhook)

    should_check = not args.skip_check and (args.start or (interactive and _yes_no("Validate with the pinned Monitor image now?", True)))
    if should_check:
        check_config()
    should_start = args.start or (
        interactive
        and not args.offline
        and not args.log_only
        and _yes_no("Start monitoring Ethereum mainnet now?", True)
    )
    if should_start:
        if not webhook:
            raise QuickstartError("live start requires a Discord webhook; rerun configure without --log-only")
        if not should_check:
            check_config()
        print("Starting the monitor service...")
        run_compose(["up", "-d", "--force-recreate", "monitor"])
        verify_monitor_running()
    else:
        print("Configuration is ready. Start or reload it with: python3 quickstart.py start")
        print("An already-running container keeps its old cached config until that command runs.")
        if args.offline:
            print("Offline mode never starts live monitoring; 'start' will revalidate mainnet first.")


def replay(args):
    if args.block <= 0:
        raise QuickstartError("block must be a positive integer")
    if not MONITOR_PATH.exists():
        raise QuickstartError("run configure before replay")
    values = read_env_values()
    rpc_url = values.get(RPC_ENV, "")
    if not rpc_url:
        raise QuickstartError("Ethereum mainnet RPC URL is not configured")
    try:
        monitor = json.loads(MONITOR_PATH.read_text())
        address = monitor["addresses"][0]["address"]
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise QuickstartError("generated monitor configuration is missing or invalid") from None
    print("Checking mainnet chain ID and contract bytecode before replay...")
    validate_mainnet_contract(validate_rpc_url(rpc_url), validate_address(address))
    command = ["run", "--rm"]
    if args.send:
        webhook = read_env_values().get(WEBHOOK_ENV, "")
        if not webhook:
            raise QuickstartError("no custom Discord webhook is configured")
        validate_webhook_url(webhook)
        if not args.yes:
            confirmation = input("Replay will post real Discord messages. Type SEND to continue: ")
            if confirmation != "SEND":
                raise QuickstartError("replay cancelled")
    else:
        command += ["-e", "{}=".format(WEBHOOK_ENV)]
        print("Discord is disabled for this replay; matches will be written to logs/alerts.log.")
    command += [
        "monitor",
        "--monitor-path",
        "/app/config/monitors/quickstart_function.json",
        "--network",
        "ethereum_mainnet",
        "--block",
        str(args.block),
    ]
    run_compose(command)


def make_parser():
    parser = argparse.ArgumentParser(
        description="Configure and operate one Ethereum mainnet function -> Discord monitor."
    )
    subparsers = parser.add_subparsers(dest="command")

    configure_parser = subparsers.add_parser("configure", help="validate inputs and generate the monitor")
    configure_parser.add_argument("--name")
    configure_parser.add_argument("--address")
    configure_parser.add_argument("--function")
    configure_parser.add_argument("--message")
    configure_parser.add_argument("--offline", action="store_true", help="skip Sourcify and RPC checks")
    configure_parser.add_argument(
        "--allow-unknown-signature",
        action="store_true",
        help="allow a full proxy/implementation signature absent from a fetched ABI",
    )
    configure_parser.add_argument("--log-only", action="store_true", help="configure without a webhook")
    configure_parser.add_argument("--test-webhook", action="store_true")
    configure_parser.add_argument("--skip-check", action="store_true")
    configure_parser.add_argument("--start", action="store_true")
    configure_parser.add_argument("--force", action="store_true")

    subparsers.add_parser("check", help="run OpenZeppelin Monitor config validation")
    subparsers.add_parser("check-rpcs", help="validate configured RPC chain IDs without printing URLs")
    subparsers.add_parser("start", help="validate and start the monitor service")
    subparsers.add_parser("status", help="show the monitor service status")
    subparsers.add_parser("stop", help="stop the monitor service")
    subparsers.add_parser("logs", help="follow monitor logs")
    subparsers.add_parser("test-webhook", help="send one confirmed test message")

    replay_parser = subparsers.add_parser("replay", help="replay the generated monitor at one block")
    replay_parser.add_argument("block", type=int)
    replay_parser.add_argument("--send", action="store_true", help="allow real Discord delivery")
    replay_parser.add_argument("--yes", action="store_true", help="skip the SEND confirmation")
    return parser


def main(argv=None):
    parser = make_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["configure"]
    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "configure":
            configure(args)
        elif command == "check":
            check_config()
        elif command == "check-rpcs":
            check_rpc_health()
        elif command == "start":
            start_monitor()
        elif command == "status":
            run_compose(["ps", "monitor"])
        elif command == "stop":
            run_compose(["stop", "monitor"])
        elif command == "logs":
            run_compose(["logs", "-f", "monitor"])
        elif command == "test-webhook":
            webhook = read_env_values().get(WEBHOOK_ENV, "")
            if not webhook:
                raise QuickstartError("no custom Discord webhook is configured")
            test_webhook(validate_webhook_url(webhook))
        elif command == "replay":
            replay(args)
        else:
            parser.error("unknown command")
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except QuickstartError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
