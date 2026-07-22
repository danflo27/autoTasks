"""Shared library for the Tellor alert script trigger (see alert.py).

Loaded via sys.path by alert.py — this file is NOT registered as a trigger.
Python 3.12 stdlib only: the official Monitor image has no pip packages
(and its bundled node/jq are broken, which is why these scripts are Python).
"""

import ast
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from discord_routes import (
    DELIVERY_MODE_ENV,
    PLATFORM_DELIVERY_MODE_ENV,
    deliver_alert,
)

ALERT_LOG = "logs/alerts.log"  # /app/logs inside the container, mounted rw
HANDLER_DELIVERY_LOG = "logs/handler_delivery.log"
DISCORD_CONTENT_LIMIT = 2000

EXPLORER_TX = {
    "ethereum_mainnet": "https://etherscan.io/tx/",
    "sepolia": "https://sepolia.etherscan.io/tx/",
    "polygon": "https://polygonscan.com/tx/",
    "optimism": "https://optimistic.etherscan.io/tx/",
    "gnosis": "https://gnosisscan.io/tx/",
    "chiado": "https://gnosis-chiado.blockscout.com/tx/",
}

# chainId -> env var holding the RPC URL used to verify EVMCall reports
EVM_CALL_RPCS = {
    1: "RPC_ETHEREUM_MAINNET",
    10: "RPC_OPTIMISM",
    100: "RPC_GNOSIS",
    137: "RPC_POLYGON",
    10200: "RPC_CHIADO",
    11155111: "RPC_SEPOLIA",
}

# Exact upstream behavior reproduced by historical_evm_call_reference().
TELLIOT_EVM_CALL_TAG = "v0.4.20"
TELLIOT_EVM_CALL_COMMIT = "fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2"

MONITOR_RPCS = {
    "ethereum_mainnet": "RPC_ETHEREUM_MAINNET",
    "sepolia": "RPC_SEPOLIA",
}

# addressUpdates: queryId -> (display label, query type, response ABI type)
ADDRESS_REPORTS = {
    "0x3ab34a189e35885414ac4e83c5a7faa9d8f03a4d530728ef516d203d91d6309c": (
        "Autopay addresses", "AutopayAddresses", "address[]"
    ),
    "0xcf0c5863be1cf3b948a9ff43290f931399765d051a60c3b23a4e098148b1f707": (
        "Tellor oracle address", "TellorOracleAddress", "address"
    ),
}

# TellorFlex SpotPrice trusted references. Each entry is:
#   label (required) plus any of: cg, cmc, coinbase, defillama, paprika,
#   frankfurter, open_er_api, fxratesapi, fx, fixed.
# Market-priced feeds require two usable sources before they are verified.
# GYD is intentionally a one-reference peg: the three public market APIs below
# do not currently publish a GYD price, so treating their absence as a market
# price would hide a depeg rather than verify one.
MIN_TRUSTED_PRICE_SOURCES = 2

TRUSTED_PRICE_ASSETS = {
    "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac": {
        "label": "BTC / USD",
        "cg": "bitcoin",
        "cmc": "BTC",
        "coinbase": "BTC-USD",
        "defillama": "coingecko:bitcoin",
        "paprika": "btc-bitcoin",
    },
    "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992": {
        "label": "ETH / USD",
        "cg": "ethereum",
        "cmc": "ETH",
        "coinbase": "ETH-USD",
        "defillama": "coingecko:ethereum",
        "paprika": "eth-ethereum",
    },
    "0x5c13cd9c97dbb98f2429c101a2a8150e6c7a0ddaff6124ee176a3a411067ded0": {
        "label": "TRB / USD",
        "cg": "tellor",
        "cmc": "TRB",
        "coinbase": "TRB-USD",
        "defillama": "coingecko:tellor",
        "paprika": "trb-tellor",
    },
    # MATIC rebranded to POL; CoinGecko matic-network id no longer returns usd.
    "0x40aa71e5205fdc7bdb7d65f7ae41daca3820c5d3a8f62357a99eda3aa27244a3": {
        "label": "MATIC / USD",
        "cg": "polygon-ecosystem-token",
        "cmc": "POL",
        "coinbase": "POL-USD",
        "defillama": "coingecko:polygon-ecosystem-token",
        "paprika": "pol-polygon-ecosystem-token",
    },
    "0x68584962e7ca6a57d672cdbfaa37c55431a84c5bb8c40d5d204a23f304f83b2e": {
        "label": "GYD / USD",
        "fixed": 1.0,
        "min_sources": 1,
    },
    "0x19585d912afb72378e3986a7a53f1eae1fbae792cd17e1d0df063681326823ae": {
        "label": "LTC / USD",
        "cg": "litecoin",
        "cmc": "LTC",
        "coinbase": "LTC-USD",
        "defillama": "coingecko:litecoin",
        "paprika": "ltc-litecoin",
    },
    "0xafc6a3f6c18df31f1078cf038745b48e55623330715d90efe3dc7935efd44938": {
        "label": "OP / USD",
        "cg": "optimism",
        "cmc": "OP",
        "coinbase": "OP-USD",
        "defillama": "coingecko:optimism",
        "paprika": "op-optimism",
    },
    "0xefa84ae5ea9eb0545e159f78f0a44911ac5a81ecb6ff0c4e32107bcfc66c4baa": {
        "label": "BCH / USD",
        "cg": "bitcoin-cash",
        "cmc": "BCH",
        "coinbase": "BCH-USD",
        "defillama": "coingecko:bitcoin-cash",
        "paprika": "bch-bitcoin-cash",
    },
    "0xb211d6f1abbd5bb431618547402a92250b765151acbe749e7f9c26dc19e5dd9a": {
        "label": "SOL / USD",
        "cg": "solana",
        "cmc": "SOL",
        "coinbase": "SOL-USD",
        "defillama": "coingecko:solana",
        "paprika": "sol-solana",
    },
    "0x8810ffb0cfcb6131da29ed4b229f252d6bac6fc98fc4a61ffbde5b48131e0228": {
        "label": "DOT / USD",
        "cg": "polkadot",
        "cmc": "DOT",
        "coinbase": "DOT-USD",
        "defillama": "coingecko:polkadot",
        "paprika": "dot-polkadot",
    },
    "0x537422e5383888586f8f9bca62c5bfd8eb0f8c1bcd335b1a691e6b550c92dcce": {
        "label": "FIL / USD",
        "cg": "filecoin",
        "cmc": "FIL",
        "coinbase": "FIL-USD",
        "defillama": "coingecko:filecoin",
        "paprika": "fil-filecoin",
    },
    "0x7f3fc5bbf0bcc372beece1d2711095b6c884c69e21dad1180f2160adfcd8b044": {
        "label": "BRL / USD",
        "frankfurter": "BRL",
        "open_er_api": "BRL",
        "fxratesapi": "BRL",
        "fx": "BRL",  # Optional keyed fourth source.
    },
    "0x2c81613b335c890096fd1c9a89766a2d71da2c9636505a9cb3b3dc7877cdad4b": {
        "label": "CNY / USD",
        "frankfurter": "CNY",
        "open_er_api": "CNY",
        "fxratesapi": "CNY",
        "fx": "CNY",  # Optional keyed fourth source.
    },
    "0x907154958baee4fb0ce2bbe50728141ac76eb2dc1731b3d40f0890746dd07e62": {
        "label": "STETH / USD",
        "cg": "staked-ether",
        "cmc": "STETH",
        "coinbase": "STETH-USD",
        "defillama": "coingecko:staked-ether",
        "paprika": "steth-lido-staked-ether",
    },
    "0x1962cde2f19178fe2bb2229e78a6d386e6406979edc7b9a1966d89d83b3ebf2e": {
        "label": "WSTETH / USD",
        "cg": "wrapped-steth",
        "cmc": "WSTETH",
        "defillama": "coingecko:wrapped-steth",
        "paprika": "wsteth-wrapped-liquid-staked-ether-20",
    },
    "0xfd47fa335a8c4886222ebae89a8de8d4a0187eb06c4429d3c0a7932332d2430d": {
        "label": "SWETH / USD",
        "cg": "sweth",
        "cmc": "SWETH",
        "defillama": "coingecko:sweth",
        "paprika": "sweth-swell-ethereum",
    },
    "0xbb5e0a51ab0e06354439f377e326ca71ec8149249d163f75f543fcdc25818e76": {
        "label": "CBETH / USD",
        "cg": "coinbase-wrapped-staked-eth",
        "cmc": "CBETH",
        "coinbase": "CBETH-USD",
        "defillama": "coingecko:coinbase-wrapped-staked-eth",
        "paprika": "cbeth-coinbase-wrapped-staked-eth",
    },
}

# Alert louder (still always notify) when |reported - trusted| / trusted exceeds this.
TELLORFLEX_PRICE_TOLERANCE = 0.20

# dvm: queryId -> (label, source, source id). source: "cg" = CoinGecko, "fx" = exchangerate-api
DVM_FEEDS = {
    "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac": ("BTC / USD", "cg", "bitcoin"),
    "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992": ("ETH / USD", "cg", "ethereum"),
    "0x5c13cd9c97dbb98f2429c101a2a8150e6c7a0ddaff6124ee176a3a411067ded0": ("TRB / USD", "cg", "tellor"),
    "0x19585d912afb72378e3986a7a53f1eae1fbae792cd17e1d0df063681326823ae": ("LTC / USD", "cg", "litecoin"),
    "0xafc6a3f6c18df31f1078cf038745b48e55623330715d90efe3dc7935efd44938": ("OP / USD", "cg", "optimism"),
    "0xefa84ae5ea9eb0545e159f78f0a44911ac5a81ecb6ff0c4e32107bcfc66c4baa": ("BCH / USD", "cg", "bitcoin-cash"),
    "0x40aa71e5205fdc7bdb7d65f7ae41daca3820c5d3a8f62357a99eda3aa27244a3": ("MATIC / USD", "cg", "polygon-ecosystem-token"),
    "0xb211d6f1abbd5bb431618547402a92250b765151acbe749e7f9c26dc19e5dd9a": ("SOL / USD", "cg", "solana"),
    "0x8810ffb0cfcb6131da29ed4b229f252d6bac6fc98fc4a61ffbde5b48131e0228": ("DOT / USD", "cg", "polkadot"),
    "0x537422e5383888586f8f9bca62c5bfd8eb0f8c1bcd335b1a691e6b550c92dcce": ("FIL / USD", "cg", "filecoin"),
    "0x7f3fc5bbf0bcc372beece1d2711095b6c884c69e21dad1180f2160adfcd8b044": ("BRL / USD", "fx", "BRL"),
    "0x2c81613b335c890096fd1c9a89766a2d71da2c9636505a9cb3b3dc7877cdad4b": ("CNY / USD", "fx", "CNY"),
    "0x907154958baee4fb0ce2bbe50728141ac76eb2dc1731b3d40f0890746dd07e62": ("STETH / USD", "cg", "staked-ether"),
    "0x1962cde2f19178fe2bb2229e78a6d386e6406979edc7b9a1966d89d83b3ebf2e": ("WSTETH / USD", "cg", "wrapped-steth"),
    "0xfd47fa335a8c4886222ebae89a8de8d4a0187eb06c4429d3c0a7932332d2430d": ("SWETH / USD", "cg", "sweth"),
    "0xbb5e0a51ab0e06354439f377e326ca71ec8149249d163f75f543fcdc25818e76": ("CBETH / USD", "cg", "coinbase-wrapped-staked-eth"),
}

DVM_TOLERANCE = 0.10


class Match:
    """Wraps the monitor_match JSON the Monitor pipes to trigger scripts."""

    def __init__(self, payload):
        evm = payload["monitor_match"].get("EVM")
        if evm is None:
            raise ValueError("only EVM matches are supported")
        self._evm = evm
        self.trigger_args = payload.get("args") or []
        self.monitor_name = evm["monitor"]["name"]
        self.network = evm.get("network_slug", "")
        self.transaction = evm.get("transaction") or {}
        self.receipt = evm.get("receipt")
        self.tx_hash = self.transaction.get("hash", "")

    def _matched(self, kind):
        entries = (self._evm.get("matched_on_args") or {}).get(kind) or []
        return entries[0] if entries else None

    @property
    def signature(self):
        for kind in ("functions", "events"):
            entry = self._matched(kind)
            if entry:
                return entry["signature"]
        return ""

    def arg_map(self):
        """Decoded args of the first matched function/event: name -> value string."""
        for kind in ("functions", "events"):
            entry = self._matched(kind)
            if entry and entry.get("args"):
                return {a["name"]: a["value"] for a in entry["args"]}
        return {}

    @property
    def tx_link(self):
        return EXPLORER_TX.get(self.network, "").rstrip("/") + "/" + self.tx_hash


# ── formatting ────────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def unix_utc(value, milliseconds=False):
    """Unix timestamp -> readable UTC, retaining millisecond precision."""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return "invalid timestamp"
    if raw == 0:
        return "none"
    divisor = 1000 if milliseconds else 1
    try:
        timestamp = datetime.fromtimestamp(raw / divisor, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "invalid timestamp (out of range)"
    if milliseconds and raw % 1000:
        return timestamp.strftime("%Y-%m-%d %H:%M:%S.") + f"{raw % 1000:03d} UTC"
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def wei(v):
    """uint256 arg value (decimal string) -> whole-token float, like legacy /1e18."""
    return int(v) / 1e18


def spot_value(value_hex):
    """Reported bytes value -> rounded number, like legacy Math.round(x*100)/100."""
    return round(int(value_hex, 16) / 1e18, 2)


def usd(v):
    return "${:,.2f}".format(v)


def parse_structured_arg(value):
    """Parse Monitor's deterministic tuple/array display without executing code."""
    if isinstance(value, (list, tuple)):
        return value
    try:
        return ast.literal_eval(str(value))
    except (SyntaxError, ValueError) as error:
        raise ValueError("monitor returned malformed structured ABI data") from error


def compact_hex(raw):
    encoded = raw.hex()
    if len(raw) <= 12:
        return "0x" + encoded
    return "0x{}…{}".format(encoded[:16], encoded[-8:])


def describe_bytes(raw, label="untyped data"):
    if not raw:
        return "empty {}".format(label)
    return "{} bytes of {} ({})".format(len(raw), label, compact_hex(raw))


def signature_count(signatures):
    """Count non-empty (v, r, s) signatures without displaying signature data."""
    present = 0
    for signature in signatures:
        if len(signature) != 3:
            raise ValueError("monitor returned malformed signature data")
        v, r, s = signature
        if int(v) or int(str(r), 16) or int(str(s), 16):
            present += 1
    return present


def query_id_label(query_id):
    key = str(query_id).lower()
    if key in TRUSTED_PRICE_ASSETS:
        return TRUSTED_PRICE_ASSETS[key]["label"]
    if key in DVM_FEEDS:
        return DVM_FEEDS[key][0]
    if key in ADDRESS_REPORTS:
        return ADDRESS_REPORTS[key][0]
    return "Unknown query"


def oracle_value(query_id, value_hex):
    """Decode only query IDs whose response schema is known locally."""
    key = str(query_id).lower()
    if key in TRUSTED_PRICE_ASSETS or key in DVM_FEEDS:
        return usd(spot_value(value_hex))
    if key in ADDRESS_REPORTS:
        response_type = ADDRESS_REPORTS[key][2]
        raw = hex_bytes(value_hex)
        try:
            decoded = abi_decode([response_type], raw)[0]
        except ValueError:
            return describe_bytes(raw, "malformed ABI address data")
        if response_type == "address[]":
            shown = decoded[:12]
            suffix = "" if len(shown) == len(decoded) else ", …"
            return "{} addresses: {}{}".format(
                len(decoded), ", ".join(shown) or "none", suffix
            )
        return decoded
    return describe_bytes(hex_bytes(value_hex), "value data with an unavailable schema")


# ── minimal ABI decoding (only the types Tellor queryData uses) ──────────────

def abi_decode(types, data):
    """Decode ABI-encoded `data` (bytes) as a tuple of `types`.

    Supports uint256, address, address[], bytes32, bytes, and string — enough
    for the Tellor queryData and value shapes used by these monitors. Rejects
    truncated or non-canonical offsets instead of fabricating empty values.
    """
    head_size = 32 * len(types)
    if len(data) < head_size or len(data) % 32:
        raise ValueError("ABI data is truncated or not word-aligned")

    def dynamic_bounds(word, item_size):
        offset = int.from_bytes(word, "big")
        if offset < head_size or offset % 32 or offset + 32 > len(data):
            raise ValueError("ABI dynamic offset is invalid")
        length = int.from_bytes(data[offset:offset + 32], "big")
        start = offset + 32
        if length > (len(data) - start) // item_size:
            raise ValueError("ABI dynamic value extends past input data")
        return length, start

    out = []
    for i, t in enumerate(types):
        word = data[32 * i:32 * i + 32]
        if t == "uint256":
            out.append(int.from_bytes(word, "big"))
        elif t == "address":
            if any(word[:12]):
                raise ValueError("ABI address has nonzero high-order padding")
            out.append("0x" + word[-20:].hex())
        elif t == "bytes32":
            out.append("0x" + word.hex())
        elif t == "address[]":
            length, start = dynamic_bounds(word, 32)
            addresses = []
            for j in range(length):
                address_word = data[start + 32 * j:start + 32 * (j + 1)]
                if any(address_word[:12]):
                    raise ValueError("ABI address array has invalid padding")
                addresses.append("0x" + address_word[-20:].hex())
            out.append(addresses)
        elif t in ("bytes", "string"):
            length, start = dynamic_bounds(word, 1)
            raw = data[start:start + length]
            out.append(raw.decode() if t == "string" else raw)
        else:
            raise ValueError(f"unsupported ABI type: {t}")
    return out


def abi_decode_exact(types, data):
    """Decode canonical ABI data and reject aliases, padding, or trailing words.

    The Monitor receives attacker-controlled dynamic bytes.  The permissive
    decoder above remains available for legacy formatters; report validation
    uses this stricter boundary so malformed known values cannot look normal.
    """
    head_size = 32 * len(types)
    if len(data) < head_size or len(data) % 32:
        raise ValueError("ABI data is truncated or not word-aligned")

    dynamic_types = {"address[]", "bytes", "string"}
    expected_tail = head_size
    has_dynamic = False
    for index, value_type in enumerate(types):
        if value_type not in dynamic_types:
            continue
        has_dynamic = True
        word = data[index * 32:(index + 1) * 32]
        offset = int.from_bytes(word, "big")
        if offset != expected_tail or offset + 32 > len(data):
            raise ValueError("ABI dynamic values are not canonically ordered")
        length = int.from_bytes(data[offset:offset + 32], "big")
        if value_type == "address[]":
            payload_end = offset + 32 + length * 32
        else:
            payload_end = offset + 32 + length
        padded_end = (payload_end + 31) // 32 * 32
        if payload_end > len(data) or padded_end > len(data):
            raise ValueError("ABI dynamic value extends past input data")
        if any(data[payload_end:padded_end]):
            raise ValueError("ABI dynamic value has nonzero padding")
        expected_tail = padded_end

    expected_size = expected_tail if has_dynamic else head_size
    if len(data) != expected_size:
        raise ValueError("ABI data contains trailing words")
    return abi_decode(types, data)


def hex_bytes(hex_str):
    return bytes.fromhex(hex_str.removeprefix("0x"))


def decode_query_data(query_data_hex):
    """Tellor queryData -> (query type string, encoded parameters bytes)."""
    qtype, params = abi_decode_exact(["string", "bytes"], hex_bytes(query_data_hex))
    return qtype, params


def decode_evm_response(value_hex):
    """EVMCall response -> (untyped eth_call return bytes, source timestamp)."""
    return abi_decode_exact(["bytes", "uint256"], hex_bytes(value_hex))


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_json(url, headers=None, body=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "tellor-monitor/1.0",
        **(headers or {}),
    }
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None:
        hostname = parts.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname
        if parts.port is not None:
            netloc += f":{parts.port}"
        url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        request_headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
    req = urllib.request.Request(url, data=data, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _receipt_succeeded(receipt):
    status = receipt.get("status") if isinstance(receipt, dict) else None
    if isinstance(status, bool):
        return status
    if isinstance(status, int):
        return status == 1
    if isinstance(status, str):
        try:
            return int(status, 16 if status.lower().startswith("0x") else 10) == 1
        except ValueError:
            pass
    raise RuntimeError("transaction receipt returned an invalid status")


def function_match_succeeded(match):
    """Verify a matched top-level function call with one receipt request.

    Monitor v1.5 may assume success without fetching a receipt when a block has
    unrelated logs. Checking only after the selector matches avoids both false
    alerts and a receipt request for every transaction in every block.
    """
    if match._matched("functions") is None:
        return True
    if match.receipt is not None:
        return _receipt_succeeded(match.receipt)
    rpc_env = MONITOR_RPCS.get(match.network)
    rpc_url = os.environ.get(rpc_env or "")
    if not rpc_env or not rpc_url:
        raise RuntimeError("cannot verify function status: no RPC for {}".format(match.network))
    try:
        payload = http_json(
            rpc_url,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionReceipt",
                "params": [match.tx_hash],
            },
        )
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            "cannot verify function status: RPC returned HTTP {}".format(error.code)
        ) from None
    except Exception:
        raise RuntimeError("cannot verify function status: RPC request failed") from None
    if not isinstance(payload, dict) or payload.get("error") or payload.get("result") is None:
        raise RuntimeError("cannot verify function status: receipt unavailable")
    return _receipt_succeeded(payload["result"])


class EVMCallNotVerified(RuntimeError):
    """Historical EVM evidence was unavailable or ambiguous."""


class EVMCallExecutionError(RuntimeError):
    """The node executed eth_call and returned a JSON-RPC call error."""


def rpc_result(rpc_url, method, params):
    """Return one JSON-RPC result while keeping URL-bearing errors redacted."""
    try:
        payload = http_json(rpc_url, body={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
    except Exception as error:
        raise EVMCallNotVerified(f"{method} transport failure") from error
    if not isinstance(payload, dict):
        raise EVMCallNotVerified(f"{method} returned a non-object response")
    if payload.get("error") is not None:
        if method == "eth_call":
            error = payload["error"]
            message = str(error.get("message", "") if isinstance(error, dict) else error).lower()
            if "revert" in message or "invalid opcode" in message:
                raise EVMCallExecutionError("eth_call returned an execution error")
            raise EVMCallNotVerified("eth_call returned an ambiguous RPC error")
        raise EVMCallNotVerified(f"{method} returned an RPC error")
    if "result" not in payload or payload["result"] is None:
        raise EVMCallNotVerified(f"{method} returned no result")
    return payload["result"]


def _rpc_quantity(value, field):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EVMCallNotVerified(f"block {field} is not a hex quantity")
    try:
        return int(value, 16)
    except ValueError as error:
        raise EVMCallNotVerified(f"block {field} is invalid") from error


def rpc_block(rpc_url, block_identifier):
    """Read and validate the block identity fields used for historical replay."""
    block = rpc_result(rpc_url, "eth_getBlockByNumber", [block_identifier, False])
    if not isinstance(block, dict):
        raise EVMCallNotVerified("eth_getBlockByNumber returned malformed block data")
    number = _rpc_quantity(block.get("number"), "number")
    timestamp = _rpc_quantity(block.get("timestamp"), "timestamp")
    block_hash = block.get("hash")
    try:
        hash_bytes = hex_bytes(block_hash)
    except (AttributeError, TypeError, ValueError) as error:
        raise EVMCallNotVerified("block hash is invalid") from error
    if len(hash_bytes) != 32:
        raise EVMCallNotVerified("block hash is not bytes32")
    return {"number": number, "timestamp": timestamp, "hash": block_hash.lower()}


def rpc_block_by_hash(rpc_url, block_hash):
    block = rpc_result(rpc_url, "eth_getBlockByHash", [block_hash, False])
    if not isinstance(block, dict):
        raise EVMCallNotVerified("eth_getBlockByHash returned malformed block data")
    number = _rpc_quantity(block.get("number"), "number")
    timestamp = _rpc_quantity(block.get("timestamp"), "timestamp")
    returned_hash = block.get("hash")
    try:
        hash_bytes = hex_bytes(returned_hash)
    except (AttributeError, TypeError, ValueError) as error:
        raise EVMCallNotVerified("block hash lookup returned an invalid hash") from error
    if len(hash_bytes) != 32:
        raise EVMCallNotVerified("block hash lookup did not return bytes32")
    return {"number": number, "timestamp": timestamp, "hash": returned_hash.lower()}


def exact_block_at_timestamp(rpc_url, target_timestamp):
    """Find the sole canonical block whose timestamp equals ``target_timestamp``."""
    try:
        target_timestamp = int(target_timestamp)
    except (TypeError, ValueError) as error:
        raise EVMCallNotVerified("source timestamp is invalid") from error
    if target_timestamp <= 0:
        raise EVMCallNotVerified("source timestamp is not positive")

    cache = {}

    def read(number):
        if number not in cache:
            cache[number] = rpc_block(rpc_url, hex(number))
            if cache[number]["number"] != number:
                raise EVMCallNotVerified("RPC returned the wrong block number")
        return cache[number]

    latest = rpc_block(rpc_url, "latest")
    latest_number = latest["number"]
    cache[latest_number] = latest
    if latest["timestamp"] < target_timestamp:
        raise EVMCallNotVerified("no exact block exists for a future timestamp")

    low, high = 0, latest_number
    while low < high:
        middle = (low + high) // 2
        if read(middle)["timestamp"] < target_timestamp:
            low = middle + 1
        else:
            high = middle
    block = read(low)
    if block["timestamp"] != target_timestamp:
        raise EVMCallNotVerified("no block has the exact source timestamp")
    if low > 0 and read(low - 1)["timestamp"] == target_timestamp:
        raise EVMCallNotVerified("multiple blocks have the source timestamp")
    if low < latest_number and read(low + 1)["timestamp"] == target_timestamp:
        raise EVMCallNotVerified("multiple blocks have the source timestamp")
    if rpc_block_by_hash(rpc_url, block["hash"]) != block:
        raise EVMCallNotVerified("block number and hash lookups disagree")
    return block


def eth_call(rpc_url, to, calldata_hex, block_identifier="latest"):
    """Raw JSON-RPC eth_call at an explicit block; returns its hex result."""
    result = rpc_result(rpc_url, "eth_call", [{
        "gasPrice": "0x0", "to": to, "data": calldata_hex,
    }, block_identifier])
    try:
        hex_bytes(result)
    except (AttributeError, TypeError, ValueError) as error:
        raise EVMCallNotVerified("eth_call returned invalid hex data") from error
    return result


def historical_evm_call_reference(rpc_url, to, calldata, source_timestamp):
    """Reproduce Telliot v0.4.20 EVMCall behavior at one exact block.

    The pinned reporter rejects empty calldata, but submits 32 zero bytes for
    one-to-three-byte calldata, an address without code, or a failed selector
    absent from bytecode. Other execution ambiguity is not evidence of a
    dispute.
    """
    if not calldata:
        raise EVMCallNotVerified("empty calldata is invalid in the pinned reporter")
    block = exact_block_at_timestamp(rpc_url, source_timestamp)
    block_tag = hex(block["number"])
    zero_result = bytes(32)

    if len(calldata) < 4:
        expected = zero_result
    else:
        call_error = None
        try:
            call_hex = eth_call(rpc_url, to, "0x" + calldata.hex(), block_tag)
            call_result = hex_bytes(call_hex)
        except EVMCallExecutionError as error:
            call_error = error
            call_result = None

        code_hex = None
        if call_error is not None or call_result == b"":
            code_hex = rpc_result(rpc_url, "eth_getCode", [to, block_tag])
            try:
                code = hex_bytes(code_hex)
            except (AttributeError, TypeError, ValueError) as error:
                raise EVMCallNotVerified("eth_getCode returned invalid hex data") from error
            if not code:
                expected = zero_result
            elif call_error is not None and calldata[:4] not in code:
                expected = zero_result
            elif call_error is not None:
                raise EVMCallNotVerified("call reverted with a selector present in code")
            else:
                raise EVMCallNotVerified("empty call result from an address with code")
        else:
            expected = call_result

    after = rpc_block(rpc_url, block_tag)
    if after != block or rpc_block_by_hash(rpc_url, block["hash"]) != block:
        raise EVMCallNotVerified("block identity changed during historical replay")
    return expected, block


# ── price sources ─────────────────────────────────────────────────────────────

def coingecko_price(cg_id):
    data = http_json(f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd")
    price = data[cg_id]["usd"]
    if price is None:
        raise ValueError(f"CoinGecko returned no usd price for {cg_id}")
    return float(price)


def coinmarketcap_price(symbol):
    key = os.environ.get("CMC_PRO_API_KEY")
    if not key:
        raise RuntimeError("CMC_PRO_API_KEY not set")
    data = http_json(
        f"https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest?symbol={symbol}",
        headers={"X-CMC_PRO_API_KEY": key},
    )
    return float(data["data"][symbol][0]["quote"]["USD"]["price"])


def coinbase_price(pair):
    """Coinbase spot price. `pair` is e.g. ETH-USD."""
    data = http_json(f"https://api.coinbase.com/v2/prices/{pair}/spot")
    return float(data["data"]["amount"])


def defillama_price(coin_id):
    """DefiLlama aggregated price. `coin_id` is e.g. coingecko:ethereum."""
    data = http_json(f"https://coins.llama.fi/prices/current/{coin_id}")
    return float(data["coins"][coin_id]["price"])


def coinpaprika_price(coin_id):
    """CoinPaprika public USD ticker; coin_id is e.g. ``eth-ethereum``."""
    data = http_json(f"https://api.coinpaprika.com/v1/tickers/{coin_id}")
    return float(data["quotes"]["USD"]["price"])


def coincap_price(cc_id):
    """Legacy CoinCap v2 helper. Host is gone; kept for tests / optional callers."""
    key = os.environ.get("COINCAP_API_KEY")
    if key:
        data = http_json(
            f"https://rest.coincap.io/v3/assets/{cc_id}",
            headers={"Authorization": f"Bearer {key}"},
        )
        return float(data["data"]["priceUsd"])
    data = http_json(f"https://api.coincap.io/v2/assets/{cc_id}")
    return float(data["data"]["priceUsd"])


def fx_usd_rate(currency):
    key = os.environ.get("EXCHANGERATE_API_KEY")
    if not key:
        raise RuntimeError("EXCHANGERATE_API_KEY not set")
    data = http_json(f"https://v6.exchangerate-api.com/v6/{key}/latest/{currency}")
    return float(data["conversion_rates"]["USD"])


def frankfurter_usd_rate(currency):
    """Frankfurter's public ECB-backed conversion for one unit of currency."""
    data = http_json(
        f"https://api.frankfurter.dev/v1/latest?base={currency}&symbols=USD"
    )
    return float(data["rates"]["USD"])


def open_er_api_usd_rate(currency):
    """ExchangeRate-API's public keyless conversion for one unit of currency."""
    data = http_json(f"https://open.er-api.com/v6/latest/{currency}")
    return float(data["rates"]["USD"])


def fxratesapi_usd_rate(currency):
    """FXRatesAPI's public keyless conversion for one unit of currency."""
    data = http_json(
        f"https://api.fxratesapi.com/latest?base={currency}&currencies=USD"
    )
    return float(data["rates"]["USD"])


def fetch_trusted_prices(asset):
    """Collect available reference prices for one TRUSTED_PRICE_ASSETS entry."""
    if "fixed" in asset:
        return [float(asset["fixed"])]

    prices = []
    for key, fetcher in (
        ("cg", coingecko_price),
        ("cmc", coinmarketcap_price),
        ("coinbase", coinbase_price),
        ("defillama", defillama_price),
        ("paprika", coinpaprika_price),
        ("frankfurter", frankfurter_usd_rate),
        ("open_er_api", open_er_api_usd_rate),
        ("fxratesapi", fxratesapi_usd_rate),
        ("fx", fx_usd_rate),
    ):
        source_id = asset.get(key)
        if not source_id:
            continue
        try:
            price = fetcher(source_id)
        except Exception:
            continue
        if price is None or price <= 0:
            continue
        prices.append(float(price))
    return prices


# ── alert delivery ────────────────────────────────────────────────────────────

def _discord_wait_url(webhook):
    """Set wait=true so Discord confirms that it persisted the message."""
    parts = urllib.parse.urlsplit(webhook)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def _retry_after(error):
    value = error.headers.get("Retry-After")
    if value is None:
        try:
            payload = json.loads(error.read().decode())
            value = payload.get("retry_after")
        except Exception:
            value = None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 1.0


def _discord_content(content):
    """Keep Discord content valid even if a dynamic argument expands the template."""
    if len(content) <= DISCORD_CONTENT_LIMIT:
        return content
    suffix = "\n… (truncated; full content is in logs/alerts.log)"
    return content[: DISCORD_CONTENT_LIMIT - len(suffix)] + suffix


def post_discord_webhook(webhook, content, attempts=3, sleep=time.sleep):
    """Post one confirmed Discord message without leaking the token-bearing URL."""
    payload = {
        "content": _discord_content(content),
        "allowed_mentions": {"parse": []},
    }
    url = _discord_wait_url(webhook)
    last_reason = "network error"
    for attempt in range(1, attempts + 1):
        try:
            http_json(url, body=payload)
            return
        except urllib.error.HTTPError as error:
            last_reason = "HTTP {}".format(error.code)
            if error.code == 429 and attempt < attempts:
                sleep(_retry_after(error))
                continue
            if 500 <= error.code < 600 and attempt < attempts:
                sleep(float(attempt))
                continue
            raise RuntimeError("Discord webhook delivery failed ({})".format(last_reason)) from None
        except Exception:
            if attempt == attempts:
                raise RuntimeError(
                    "Discord webhook delivery failed after {} attempts ({})".format(
                        attempts, last_reason
                    )
                ) from None
            sleep(float(attempt) / 2)


def _append_custom_alert_log(match, content, mode):
    """Keep the generated quickstart log format compatible and secret-free."""
    record = json.dumps({
        "ts": now_utc(),
        "monitor": match.monitor_name,
        "mode": mode,
        "network": match.network,
        "tx": match.tx_hash,
        "content": content,
    })
    os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
    with open(ALERT_LOG, "a") as stream:
        stream.write(record + "\n")


def send_alert(match, content, webhook_env=None):
    """Deliver one alert using an explicit live or log-only mode.

    Checked-in Tellor producers prefer exact monitor-name routes through the
    secure ``DISCORD_WEBHOOKS_FILE`` loader. The production platform may
    instead materialize its one fixed ``DISCORD_WEBHOOK_URL``. The generated
    quickstart remains isolated on ``CUSTOM_DISCORD_WEBHOOK_URL``.
    """
    legacy_mode = os.environ.get(DELIVERY_MODE_ENV)
    platform_mode = os.environ.get(PLATFORM_DELIVERY_MODE_ENV)
    mode = legacy_mode or platform_mode
    if mode not in ("live", "log-only"):
        raise RuntimeError(
            "{} or {} must be explicitly set to live or log-only".format(
                DELIVERY_MODE_ENV, PLATFORM_DELIVERY_MODE_ENV
            )
        )

    if webhook_env is not None:
        if webhook_env != "CUSTOM_DISCORD_WEBHOOK_URL":
            raise RuntimeError("only the generated quickstart may use an environment webhook")
        _append_custom_alert_log(match, content, mode)
        if mode == "log-only":
            return False
        webhook = os.environ.get(webhook_env)
        if not webhook:
            raise RuntimeError("CUSTOM_DISCORD_WEBHOOK_URL is required for live delivery")
        post_discord_webhook(webhook, content)
        return True

    return deliver_alert(
        match.monitor_name,
        content,
        mode=mode,
        log_path=ALERT_LOG,
        context={"network": match.network, "tx": match.tx_hash},
        lifecycle_log_path=HANDLER_DELIVERY_LOG,
        allow_platform_webhook=not bool(legacy_mode),
    )
