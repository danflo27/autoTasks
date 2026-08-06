"""Small strict clients for Ethereum JSON-RPC and Tellor Layer HTTP reads."""

import itertools
import json
from typing import Any, Dict, Optional

import requests

from .models import Unresolved


class RpcError(Unresolved):
    pass


class RpcMethodError(RpcError):
    def __init__(self, provider, method, payload):
        super().__init__("{} returned a JSON-RPC error for {}".format(provider, method))
        self.method = method
        self.payload = payload


class JsonRpcClient:
    _ids = itertools.count(1)

    def __init__(self, name, url, timeout=20, session=None):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.session = session or requests.Session()

    def call(self, method, params=None):
        if not self.url:
            raise RpcError("{} is not configured".format(self.name))
        request_id = next(self._ids)
        try:
            response = self.session.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or [],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RpcError("{} request failed".format(self.name)) from error
        if not isinstance(payload, dict) or payload.get("id") != request_id:
            raise RpcError("{} returned an invalid JSON-RPC envelope".format(self.name))
        if payload.get("error") is not None:
            raise RpcMethodError(self.name, method, payload["error"])
        if "result" not in payload:
            raise RpcError("{} returned no JSON-RPC result".format(self.name))
        return payload["result"]

    def chain_id(self):
        return _hex_int(self.call("eth_chainId"), "chain ID")

    def head_number(self):
        return _hex_int(self.call("eth_blockNumber"), "head number")

    def block(self, number_or_tag, full_transactions=False):
        tag = number_or_tag if isinstance(number_or_tag, str) else hex(number_or_tag)
        value = self.call("eth_getBlockByNumber", [tag, full_transactions])
        if not isinstance(value, dict):
            raise RpcError("{} returned no requested block".format(self.name))
        required = ("number", "hash", "timestamp")
        if any(value.get(field) is None for field in required):
            raise RpcError("{} returned an incomplete block".format(self.name))
        return value

    def block_by_hash(self, block_hash, full_transactions=False):
        value = self.call("eth_getBlockByHash", [block_hash, full_transactions])
        if not isinstance(value, dict) or value.get("hash", "").lower() != block_hash.lower():
            raise RpcError("{} returned no requested block hash".format(self.name))
        return value

    def receipt(self, transaction_hash):
        value = self.call("eth_getTransactionReceipt", [transaction_hash])
        if not isinstance(value, dict):
            raise RpcError("{} returned no transaction receipt".format(self.name))
        return value

    def transaction(self, transaction_hash):
        value = self.call("eth_getTransactionByHash", [transaction_hash])
        if not isinstance(value, dict):
            raise RpcError("{} returned no transaction".format(self.name))
        return value

    def eth_call(self, to, data, block="latest", extra=None):
        call = {"to": to, "data": data}
        if extra:
            call.update(extra)
        tag = block if isinstance(block, str) else hex(block)
        value = self.call("eth_call", [call, tag])
        if not isinstance(value, str) or not value.startswith("0x"):
            raise RpcError("{} returned an invalid eth_call result".format(self.name))
        return value

    def code(self, address, block="latest"):
        tag = block if isinstance(block, str) else hex(block)
        value = self.call("eth_getCode", [address, tag])
        if not isinstance(value, str) or not value.startswith("0x"):
            raise RpcError("{} returned invalid bytecode".format(self.name))
        return value

    def storage(self, address, slot, block="latest"):
        tag = block if isinstance(block, str) else hex(block)
        value = self.call("eth_getStorageAt", [address, slot, tag])
        if not isinstance(value, str) or len(value) != 66:
            raise RpcError("{} returned invalid storage".format(self.name))
        return value

    def trace(self, transaction_hash):
        value = self.call(
            "debug_traceTransaction", [transaction_hash, {"tracer": "callTracer"}]
        )
        if not isinstance(value, dict):
            raise RpcError("{} returned an invalid call trace".format(self.name))
        return value

    def logs(self, *, address, from_block, to_block, topics=None):
        value = self.call(
            "eth_getLogs",
            [
                {
                    "address": address,
                    "fromBlock": hex(int(from_block)),
                    "toBlock": hex(int(to_block)),
                    "topics": topics or [],
                }
            ],
        )
        if not isinstance(value, list):
            raise RpcError("{} returned invalid logs".format(self.name))
        return value


class LayerClient:
    def __init__(self, name, base_url, timeout=20, session=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def get(self, path):
        if not self.base_url:
            raise RpcError("{} is not configured".format(self.name))
        try:
            response = self.session.get(
                self.base_url + "/" + path.lstrip("/"), timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RpcError("{} request failed".format(self.name)) from error
        if not isinstance(payload, dict):
            raise RpcError("{} returned invalid JSON".format(self.name))
        return payload

    def get_at_height(self, path, height):
        if not self.base_url:
            raise RpcError("{} is not configured".format(self.name))
        try:
            response = self.session.get(
                self.base_url + "/" + path.lstrip("/"),
                headers={"x-cosmos-block-height": str(int(height))},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RpcError("{} historical request failed".format(self.name)) from error
        response_height = response.headers.get("x-cosmos-block-height") or response.headers.get(
            "grpc-metadata-x-cosmos-block-height"
        )
        try:
            height_matches = int(response_height) == int(height)
        except (TypeError, ValueError):
            height_matches = False
        if not height_matches:
            raise RpcError("{} did not prove the requested historical height".format(self.name))
        if not isinstance(payload, dict):
            raise RpcError("{} returned invalid historical JSON".format(self.name))
        return payload

    def status(self):
        return self.get("rpc/status")

    def block(self, height):
        return self.get("rpc/block?height={}".format(int(height)))

    def block_results(self, height):
        return self.get("rpc/block_results?height={}".format(int(height)))

    def validator_params(self, timestamp):
        return self.get(
            "layer/bridge/get_validator_checkpoint_params/{}".format(int(timestamp))
        )

    def historical_report(self, query_id, timestamp):
        return self.get(
            "tellor-io/layer/oracle/retrieve_data/{}/{}".format(
                query_id.removeprefix("0x"), int(timestamp)
            )
        )

    def deposit_claimed(self, deposit_id):
        return self.get("layer/bridge/get_deposit_claimed/{}".format(int(deposit_id)))

    def transaction(self, transaction_hash):
        return self.get("cosmos/tx/v1beta1/txs/{}".format(str(transaction_hash).upper()))

    def team_address(self, height):
        return self.get_at_height(
            "tellor-io/layer/dispute/team-address", int(height)
        )


def _hex_int(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RpcError("invalid {}".format(label))
    try:
        return int(value, 16)
    except ValueError as error:
        raise RpcError("invalid {}".format(label)) from error


def canonical_hash(value):
    import hashlib

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + hashlib.sha256(encoded).hexdigest()
