"""Shared library for the Tellor alert script trigger (see alert.py).

Loaded via sys.path by alert.py — this file is NOT registered as a trigger.
Python 3.12 stdlib only: the official Monitor image has no pip packages
(and its bundled node/jq are broken, which is why these scripts are Python).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ALERT_LOG = "logs/alerts.log"  # /app/logs inside the container, mounted rw

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
    100: "RPC_GNOSIS",
    137: "RPC_POLYGON",
    10: "RPC_OPTIMISM",
    11155111: "RPC_SEPOLIA",
    10200: "RPC_CHIADO",
}

# addressUpdates: queryId -> alert message
ADDRESS_REPORT_IDS = {
    "0x3ab34a189e35885414ac4e83c5a7faa9d8f03a4d530728ef516d203d91d6309c": "autopay address report",
    "0xcf0c5863be1cf3b948a9ff43290f931399765d051a60c3b23a4e098148b1f707": "oracle address report",
}

# priceMonitor: queryId -> (label, coingecko id, coinmarketcap symbol, coincap id | None)
PRICE_MONITOR_ASSETS = {
    "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac": ("BTC / USD", "bitcoin", "BTC", "bitcoin"),
    "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992": ("ETH / USD", "ethereum", "ETH", "ethereum"),
    "0x5c13cd9c97dbb98f2429c101a2a8150e6c7a0ddaff6124ee176a3a411067ded0": ("TRB / USD", "tellor", "TRB", "tellor"),
    "0x40aa71e5205fdc7bdb7d65f7ae41daca3820c5d3a8f62357a99eda3aa27244a3": ("MATIC / USD", "matic-network", "MATIC", None),
}

# dvm: queryId -> (label, source, source id). source: "cg" = CoinGecko, "fx" = exchangerate-api
DVM_FEEDS = {
    "0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac": ("BTC / USD", "cg", "bitcoin"),
    "0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992": ("ETH / USD", "cg", "ethereum"),
    "0x5c13cd9c97dbb98f2429c101a2a8150e6c7a0ddaff6124ee176a3a411067ded0": ("TRB / USD", "cg", "tellor"),
    "0x19585d912afb72378e3986a7a53f1eae1fbae792cd17e1d0df063681326823ae": ("LTC / USD", "cg", "litecoin"),
    "0xafc6a3f6c18df31f1078cf038745b48e55623330715d90efe3dc7935efd44938": ("OP / USD", "cg", "optimism"),
    "0xefa84ae5ea9eb0545e159f78f0a44911ac5a81ecb6ff0c4e32107bcfc66c4baa": ("BCH / USD", "cg", "bitcoin-cash"),
    "0x40aa71e5205fdc7bdb7d65f7ae41daca3820c5d3a8f62357a99eda3aa27244a3": ("MATIC / USD", "cg", "matic-network"),
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
        self.tx_hash = (evm.get("transaction") or {}).get("hash", "")

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


def wei(v):
    """uint256 arg value (decimal string) -> whole-token float, like legacy /1e18."""
    return int(v) / 1e18


def spot_value(value_hex):
    """Reported bytes value -> rounded number, like legacy Math.round(x*100)/100."""
    return round(int(value_hex, 16) / 1e18, 2)


def usd(v):
    return "${:,.2f}".format(v)


# ── minimal ABI decoding (only the types Tellor queryData uses) ──────────────

def abi_decode(types, data):
    """Decode ABI-encoded `data` (bytes) as a tuple of `types`.

    Supports uint256, address, bytes32, bytes, string — enough for Tellor
    queryData/value shapes: (string,bytes), (string,string),
    (uint256,address,bytes), (bytes,uint256).
    """
    out = []
    for i, t in enumerate(types):
        word = data[32 * i:32 * i + 32]
        if t == "uint256":
            out.append(int.from_bytes(word, "big"))
        elif t == "address":
            out.append("0x" + word[-20:].hex())
        elif t == "bytes32":
            out.append("0x" + word.hex())
        elif t in ("bytes", "string"):
            off = int.from_bytes(word, "big")
            length = int.from_bytes(data[off:off + 32], "big")
            raw = data[off + 32:off + 32 + length]
            out.append(raw.decode() if t == "string" else raw)
        else:
            raise ValueError(f"unsupported ABI type: {t}")
    return out


def hex_bytes(hex_str):
    return bytes.fromhex(hex_str.removeprefix("0x"))


def decode_query_data(query_data_hex):
    """Tellor queryData -> (query type string, encoded parameters bytes)."""
    qtype, params = abi_decode(["string", "bytes"], hex_bytes(query_data_hex))
    return qtype, params


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_json(url, headers=None, body=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "tellor-monitor/1.0",
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def eth_call(rpc_url, to, calldata_hex):
    """Raw JSON-RPC eth_call; returns the hex result string."""
    result = http_json(rpc_url, body={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": calldata_hex}, "latest"],
    })
    if "error" in result:
        raise RuntimeError(f"eth_call error: {result['error']}")
    return result["result"]


# ── price sources ─────────────────────────────────────────────────────────────

def coingecko_price(cg_id):
    data = http_json(f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd")
    return float(data[cg_id]["usd"])


def coinmarketcap_price(symbol):
    key = os.environ["CMC_PRO_API_KEY"]
    data = http_json(
        f"https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest?symbol={symbol}",
        headers={"X-CMC_PRO_API_KEY": key},
    )
    return float(data["data"][symbol][0]["quote"]["USD"]["price"])


def coincap_price(cc_id):
    data = http_json(f"https://api.coincap.io/v2/assets/{cc_id}")
    return float(data["data"]["priceUsd"])


def fx_usd_rate(currency):
    key = os.environ["EXCHANGERATE_API_KEY"]
    data = http_json(f"https://v6.exchangerate-api.com/v6/{key}/latest/{currency}")
    return float(data["conversion_rates"]["USD"])


# ── alert delivery ────────────────────────────────────────────────────────────

def send_alert(match, content):
    """Deliver an alert: append to logs/alerts.log, then POST to Discord.

    Without DISCORD_WEBHOOK_URL set the alert is log-only (used by the smoke
    test and safe for local runs). A Discord failure exits non-zero so the
    Monitor logs a trigger error.
    """
    record = json.dumps({
        "ts": now_utc(),
        "monitor": match.monitor_name,
        "network": match.network,
        "tx": match.tx_hash,
        "content": content,
    })
    os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
    with open(ALERT_LOG, "a") as f:
        f.write(record + "\n")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("DISCORD_WEBHOOK_URL not set; alert logged only", file=sys.stderr)
        return
    for attempt in (1, 2):
        try:
            http_json(webhook, body={"content": content})
            return
        except urllib.error.HTTPError as e:
            if e.code == 204:  # Discord returns 204 No Content on success
                return
            if attempt == 2:
                raise
        except Exception:
            if attempt == 2:
                raise
