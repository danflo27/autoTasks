"""Ethereum ABI helpers and independent receipt-log decoding."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from eth_abi import decode, encode
from eth_utils import keccak

from .constants import (
    GOVERNANCE,
    TELLOR_DATA_BANK,
    TELLOR_DATA_BRIDGE,
    TELLOR_FLEX,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V1,
    TOKEN_BRIDGE_V2,
)
from .models import DecodedEvent, Unresolved


def hx(raw):
    return "0x" + bytes(raw).hex()


def unhex(value):
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise ValueError("invalid hex value")
    return bytes.fromhex(value[2:])


def normalize_address(value):
    if isinstance(value, bytes):
        value = hx(value[-20:])
    value = str(value).lower()
    if len(value) != 42 or not value.startswith("0x"):
        raise ValueError("invalid address")
    int(value[2:], 16)
    return value


def function_selector(signature):
    return keccak(text=signature)[:4]


def call_data(signature, types=(), values=()):
    return hx(function_selector(signature) + encode(list(types), list(values)))


def decode_call(types, value):
    try:
        return decode(list(types), unhex(value), strict=True)
    except Exception as error:
        raise Unresolved("eth_call result did not decode exactly") from error


def bytes32_text(value):
    if isinstance(value, bytes) and len(value) == 32:
        return hx(value)
    text = str(value).lower()
    if len(text) != 66:
        raise ValueError("not bytes32")
    int(text[2:], 16)
    return text


@dataclass(frozen=True)
class EventSpec:
    address: str
    name: str
    signature: str
    names: Tuple[str, ...]
    types: Tuple[str, ...]
    indexed: Tuple[bool, ...]

    @property
    def topic(self):
        return hx(keccak(text=self.signature))

    def decode(self, log):
        topics = log.get("topics") or []
        if not topics or str(topics[0]).lower() != self.topic:
            raise ValueError("event topic mismatch")
        indexed_types = [kind for kind, flag in zip(self.types, self.indexed) if flag]
        plain_types = [kind for kind, flag in zip(self.types, self.indexed) if not flag]
        if len(topics) != 1 + len(indexed_types):
            raise ValueError("event topic count mismatch")
        indexed_values = []
        for kind, topic in zip(indexed_types, topics[1:]):
            if kind in {"bytes", "string"} or kind.endswith("[]"):
                indexed_values.append(str(topic).lower())
            else:
                indexed_values.append(decode([kind], unhex(topic), strict=True)[0])
        plain_values = (
            list(decode(plain_types, unhex(log.get("data", "0x")), strict=True))
            if plain_types
            else []
        )
        indexed_iter = iter(indexed_values)
        plain_iter = iter(plain_values)
        raw_values = [
            next(indexed_iter) if flag else next(plain_iter) for flag in self.indexed
        ]
        args = {
            name: json_value(value) for name, value in zip(self.names, raw_values)
        }
        return DecodedEvent(
            address=normalize_address(log["address"]),
            name=self.name,
            signature=self.signature,
            args=args,
            transaction_hash=str(log["transactionHash"]).lower(),
            log_index=int(log["logIndex"], 16),
            block_number=int(log["blockNumber"], 16),
            block_hash=str(log["blockHash"]).lower(),
        )


def json_value(value):
    if isinstance(value, bytes):
        return hx(value)
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
        return value.lower()
    return value


def _event(address, name, types, names, indexed=None):
    signature = "{}({})".format(name, ",".join(types))
    return EventSpec(
        address.lower(),
        name,
        signature,
        tuple(names),
        tuple(types),
        tuple(indexed or [False] * len(types)),
    )


EVENT_SPECS = [
    _event(TELLOR_MASTER, "NewTellorAddress", ["address"], ["_newTellor"]),
    _event(
        TELLOR_MASTER,
        "NewProposedOracleAddress",
        ["address", "uint256"],
        ["_newProposedOracle", "_timestamp"],
    ),
    _event(
        TELLOR_MASTER,
        "NewOracleAddress",
        ["address", "uint256"],
        ["_newOracle", "_timestamp"],
    ),
    _event(
        TELLOR_MASTER,
        "Transfer",
        ["address", "address", "uint256"],
        ["_from", "_to", "_value"],
        [True, True, False],
    ),
    _event(
        TOKEN_BRIDGE_V1, "BridgeStateUpdated", ["uint8"], ["_newState"]
    ),
    _event(
        TOKEN_BRIDGE_V2, "BridgeStateUpdated", ["uint8"], ["_newState"]
    ),
    _event(
        TOKEN_BRIDGE_V2,
        "PauseProposed",
        ["uint256", "address", "uint256"],
        ["_proposalId", "_proposer", "_proposalTime"],
    ),
    _event(
        TOKEN_BRIDGE_V2,
        "PauseApproved",
        ["uint256", "address", "uint256"],
        ["_proposalId", "_proposer", "_proposalTime"],
    ),
    _event(
        TOKEN_BRIDGE_V2,
        "PauseRefunded",
        ["uint256", "address", "uint256"],
        ["_proposalId", "_proposer", "_proposalTime"],
    ),
    _event(TOKEN_BRIDGE_V2, "DataBridgeUpdated", ["address"], ["_dataBridge"]),
    _event(
        TOKEN_BRIDGE_V2,
        "RoleUpdateProposed",
        ["bytes32", "address", "uint256"],
        ["_role", "_newAddress", "_newUpdateDelay"],
    ),
    _event(
        TOKEN_BRIDGE_V2,
        "RoleUpdateAccepted",
        ["bytes32", "address"],
        ["_role", "_newAddress"],
    ),
    _event(TOKEN_BRIDGE_V2, "MintToOracleFailed", [], []),
    _event(
        TELLOR_DATA_BRIDGE,
        "ValidatorSetUpdated",
        ["uint256", "uint256", "bytes32"],
        ["_powerThreshold", "_validatorTimestamp", "_validatorSetHash"],
    ),
    _event(
        TELLOR_DATA_BRIDGE,
        "GuardianResetValidatorSet",
        ["uint256", "uint256", "bytes32"],
        ["_powerThreshold", "_validatorTimestamp", "_validatorSetHash"],
    ),
    _event(
        TELLOR_FLEX,
        "NewReport",
        ["bytes32", "uint256", "bytes", "uint256", "bytes", "address"],
        ["_queryId", "_time", "_value", "_nonce", "_queryData", "_reporter"],
    ),
    _event(
        TELLOR_DATA_BANK,
        "OracleUpdated",
        ["bytes32", "(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256)"],
        ["queryId", "attestData"],
        [True, False],
    ),
    _event(
        GOVERNANCE,
        "NewDispute",
        ["uint256", "bytes32", "uint256", "address"],
        ["_disputeId", "_queryId", "_timestamp", "_reporter"],
    ),
    _event(
        TELLOR_FLEX,
        "ValueRemoved",
        ["bytes32", "uint256"],
        ["_queryId", "_timestamp"],
    ),
]

for bridge in (TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2):
    EVENT_SPECS.extend(
        [
            _event(
                bridge,
                "Deposit",
                ["uint256", "address", "string", "uint256", "uint256"],
                ["_depositId", "_sender", "_recipient", "_amount", "_tip"],
            ),
            _event(
                bridge,
                "Withdraw",
                ["uint256", "string", "address", "uint256"],
                ["_depositId", "_sender", "_recipient", "_amount"],
            ),
        ]
    )
EVENT_SPECS.extend(
    [
        _event(
            TOKEN_BRIDGE_V1,
            "ExtraWithdrawClaimed",
            ["address", "uint256"],
            ["_recipient", "_amount"],
        ),
        _event(
            TOKEN_BRIDGE_V2,
            "ExtraWithdrawClaimed",
            ["uint256", "address", "uint256"],
            ["_withdrawId", "_recipient", "_amount"],
        ),
        _event(
            TOKEN_BRIDGE_V2,
            "TokensToClaimUpdated",
            ["address", "uint256"],
            ["_recipient", "_amount"],
        ),
        _event(
            TOKEN_BRIDGE_V2,
            "ExtraWithdrawReverified",
            ["uint256", "address", "uint256"],
            ["_withdrawId", "_recipient", "_amount"],
        ),
    ]
)

EVENT_BY_ADDRESS_TOPIC = {
    (spec.address, spec.topic): spec for spec in EVENT_SPECS
}
DATABRIDGE_EVENT_BY_TOPIC = {
    spec.topic: spec
    for spec in EVENT_SPECS
    if spec.address == TELLOR_DATA_BRIDGE
    and spec.name in {"ValidatorSetUpdated", "GuardianResetValidatorSet"}
}


def decode_receipt_events(receipt, databridge_addresses=()):
    enrolled_databridges = {
        normalize_address(address) for address in databridge_addresses
    }
    decoded = []
    for log in receipt.get("logs") or []:
        try:
            address = normalize_address(log["address"])
            topic = str(log["topics"][0]).lower()
            key = (address, topic)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        spec = EVENT_BY_ADDRESS_TOPIC.get(key)
        if spec is None and address in enrolled_databridges:
            spec = DATABRIDGE_EVENT_BY_TOPIC.get(topic)
        if spec is None:
            continue
        try:
            decoded.append(spec.decode(log))
        except Exception as error:
            raise Unresolved(
                "a relevant finalized receipt log did not decode exactly"
            ) from error
    return sorted(decoded, key=lambda event: event.log_index)


def monitor_evm(envelope):
    try:
        evm = envelope["payload"]["monitor_match"]["EVM"]
        monitor = evm["monitor"]["name"]
        transaction_hash = evm["transaction"]["hash"].lower()
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid spooled MonitorMatch") from error
    return monitor, transaction_hash, evm


def matched_functions(evm):
    entries = (evm.get("matched_on_args") or {}).get("functions") or []
    result = []
    for entry in entries:
        signature = entry.get("signature")
        args = {
            argument["name"]: argument.get("value")
            for argument in entry.get("args") or []
            if isinstance(argument, dict) and "name" in argument
        }
        if signature:
            result.append((signature, args))
    return result


MASTER_CONTROL_FUNCTIONS = {
    hx(function_selector("changeTellorContract(address)")): (
        "changeTellorContract(address)",
        "_tContract",
    ),
    hx(function_selector("changeDeity(address)")): (
        "changeDeity(address)",
        "_newDeity",
    ),
    hx(function_selector("changeOwner(address)")): (
        "changeOwner(address)",
        "_newOwner",
    ),
}


def decode_master_control_function(transaction):
    """Decode a direct TellorMaster control call without trusting MonitorMatch."""

    try:
        if normalize_address(transaction.get("to")) != TELLOR_MASTER:
            return []
    except (TypeError, ValueError):
        return []
    data = unhex(str(transaction.get("input", "")).lower())
    if len(data) < 4:
        return []
    entry = MASTER_CONTROL_FUNCTIONS.get(hx(data[:4]))
    if entry is None:
        return []
    signature, name = entry
    try:
        values = decode(["address"], data[4:], strict=True)
        if encode(["address"], values) != data[4:]:
            raise ValueError("non-canonical calldata")
    except Exception as error:
        raise Unresolved("TellorMaster control calldata is not canonical") from error
    return [(signature, {name: normalize_address(values[0])})]


def receipt_chain_point(client, receipt, confirmations=12):
    try:
        status = int(receipt["status"], 16)
        block_number = int(receipt["blockNumber"], 16)
        block_hash = receipt["blockHash"].lower()
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("transaction receipt is incomplete") from error
    if status != 1:
        raise Unresolved("transaction receipt is not successful")
    if client.head_number() - block_number < confirmations:
        raise Unresolved("transaction is not 12-confirmed")
    block = client.block(block_number)
    if block["hash"].lower() != block_hash:
        raise Unresolved("transaction block hash changed")
    from .models import ChainPoint

    return ChainPoint(
        "ethereum", block_number, block_hash, int(block["timestamp"], 16)
    )
