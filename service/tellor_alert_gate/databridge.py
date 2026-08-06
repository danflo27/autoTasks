"""M3 validator checkpoint reconciliation and DataBridge enrollment."""

import json
from pathlib import Path
import re
import time

from eth_abi import encode
from eth_utils import keccak

from .constants import TELLOR_DATA_BRIDGE, VALIDATOR_DOMAIN
from .ethereum import call_data, decode_call, hx, normalize_address, unhex
from .models import ChainPoint, Finding, Unresolved


class EnrollmentError(ValueError):
    pass


class EnrollmentRegistry:
    def __init__(self, contracts):
        self.contracts = {item["address"].lower(): item for item in contracts}

    @classmethod
    def load(cls, path):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            contracts = payload["contracts"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise EnrollmentError("DataBridge enrollment file is invalid") from error
        if payload.get("schema_version") != 1 or not isinstance(contracts, list):
            raise EnrollmentError("DataBridge enrollment schema is invalid")
        required = {
            "address",
            "runtime_bytecode_keccak256",
            "source_revision",
            "abi_revision",
            "validator_set_hash_domain_separator",
            "domain_rule",
            "stale_rule",
        }
        seen = set()
        for item in contracts:
            if not isinstance(item, dict) or set(item) != required:
                raise EnrollmentError("DataBridge enrollment fields are not exact")
            address = normalize_address(item["address"])
            if address in seen:
                raise EnrollmentError("DataBridge enrollment addresses must be unique")
            seen.add(address)
            if item["domain_rule"] not in {
                "compile-time-constant",
                "constructor-pinned-constant",
            }:
                raise EnrollmentError("DataBridge domain rule is unsupported")
            _bytes32(item["runtime_bytecode_keccak256"])
            _bytes32(item["validator_set_hash_domain_separator"])
            if not re.fullmatch(r"[0-9a-f]{40}", str(item["source_revision"])):
                raise EnrollmentError("DataBridge source revision must be a full commit")
            if not str(item["abi_revision"]).strip():
                raise EnrollmentError("DataBridge enrollment provenance is incomplete")
            if (
                item["stale_rule"]
                != "block_timestamp-validator_timestamp_ms/1000>unbonding_period"
            ):
                raise EnrollmentError("DataBridge stale rule is unsupported")
        return cls(contracts)

    def record(self, address):
        return self.contracts.get(normalize_address(address))

    def verify_live(self, address, ethereum, layer, block_number, sleep=time.sleep, delay=30):
        record = self.record(address)
        if record is None:
            return False, {"reason": "address is not pre-enrolled"}
        code = unhex(ethereum.code(address, block_number))
        code_hash = hx(keccak(code))
        if code_hash != record["runtime_bytecode_keccak256"].lower():
            return False, {"reason": "runtime bytecode hash mismatch", "code_hash": code_hash}
        state = read_databridge_state(ethereum, address, block_number)
        if (
            not state["initialized"]
            or state["validator_timestamp"] <= 0
            or state["power_threshold"] <= 0
            or int(state["checkpoint"], 16) == 0
        ):
            return False, {"reason": "DataBridge is not initialized with nonzero state"}
        first = read_layer_validator_params(layer, state["validator_timestamp"])
        if delay:
            sleep(delay)
        second = read_layer_validator_params(layer, state["validator_timestamp"])
        if first != second:
            return False, {"reason": "Tellor Layer checkpoint reads disagree"}
        domain = unhex(record["validator_set_hash_domain_separator"])
        computed = compute_checkpoint(
            domain,
            first["power_threshold"],
            first["timestamp"],
            first["validator_set_hash"],
        )
        ok = (
            first["timestamp"] == state["validator_timestamp"]
            and first["power_threshold"] == state["power_threshold"]
            and first["checkpoint"] == state["checkpoint"]
            and computed == state["checkpoint"]
        )
        return ok, {"ethereum": state, "layer": first, "computed_checkpoint": computed}


def read_databridge_state(client, address, block):
    return {
        "checkpoint": hx(
            decode_call(
                ["bytes32"], client.eth_call(address, call_data("lastValidatorSetCheckpoint()"), block)
            )[0]
        ),
        "power_threshold": int(
            decode_call(
                ["uint256"], client.eth_call(address, call_data("powerThreshold()"), block)
            )[0]
        ),
        "validator_timestamp": int(
            decode_call(
                ["uint256"], client.eth_call(address, call_data("validatorTimestamp()"), block)
            )[0]
        ),
        "unbonding_period": int(
            decode_call(
                ["uint256"], client.eth_call(address, call_data("unbondingPeriod()"), block)
            )[0]
        ),
        "initialized": bool(
            decode_call(
                ["bool"], client.eth_call(address, call_data("initialized()"), block)
            )[0]
        ),
    }


def read_layer_validator_params(client, timestamp):
    payload = client.validator_params(timestamp)
    try:
        result = {
            "checkpoint": _hex32(payload["checkpoint"]),
            "validator_set_hash": _hex32(payload["valset_hash"]),
            "timestamp": int(payload["timestamp"]),
            "power_threshold": int(payload["power_threshold"]),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise Unresolved("Tellor Layer checkpoint response is incomplete") from error
    if result["timestamp"] != int(timestamp):
        raise Unresolved("Tellor Layer checkpoint timestamp does not match request")
    return result


def compute_checkpoint(domain, power_threshold, validator_timestamp, validator_set_hash):
    raw_hash = (
        unhex(validator_set_hash)
        if isinstance(validator_set_hash, str)
        else bytes(validator_set_hash)
    )
    return hx(
        keccak(
            encode(
                ["bytes32", "uint256", "uint256", "bytes32"],
                [bytes(domain), int(power_threshold), int(validator_timestamp), raw_hash],
            )
        )
    )


def evaluate_validator_event(
    event,
    chain_point,
    ethereum,
    layer,
    second_delay=30,
    sleep=time.sleep,
    domain=VALIDATOR_DOMAIN,
):
    args = event.args
    threshold = int(args["_powerThreshold"])
    timestamp = int(args["_validatorTimestamp"])
    third = _hex32(args["_validatorSetHash"])
    state = read_databridge_state(ethereum, event.address, chain_point.number)
    previous = read_databridge_state(
        ethereum, event.address, chain_point.number - 1
    )
    first = read_layer_validator_params(layer, timestamp)
    if event.name == "GuardianResetValidatorSet":
        computed = compute_checkpoint(
            domain,
            first["power_threshold"],
            first["timestamp"],
            first["validator_set_hash"],
        )
        return Finding(
            slug="databridge-integrity",
            predicate="guardian validator-set reset executed",
            expected="ordinary signed validator-set update path",
            observed={
                "event_checkpoint": third,
                "post_state": state,
                "layer": first,
                "computed_checkpoint": computed,
            },
            incident_key="databridge-guardian-reset:{}".format(
                event.transaction_hash
            ),
            delivery_key="1:{}:M3:{}".format(
                event.transaction_hash, event.log_index
            ),
            chain_point=chain_point,
            signal=event.signature,
            contract=event.address,
            transaction_hash=event.transaction_hash,
            log_index=event.log_index,
            evidence={"validator_timestamp": timestamp, "checkpoint": third},
        )

    computed = compute_checkpoint(domain, threshold, timestamp, third)
    observed = {
        "event": {
            "power_threshold": threshold,
            "validator_timestamp": timestamp,
            "validator_set_hash": third,
        },
        "post_state": state,
        "previous_validator_timestamp": previous["validator_timestamp"],
        "layer": first,
        "computed_checkpoint": computed,
    }
    matches = _validator_update_matches(
        threshold, timestamp, third, computed, state, previous, first
    )
    if matches:
        return None
    if second_delay:
        sleep(second_delay)
    second = read_layer_validator_params(layer, timestamp)
    if _validator_update_matches(
        threshold, timestamp, third, computed, state, previous, second
    ):
        return None
    observed["layer_second_read"] = second
    return Finding(
        slug="databridge-integrity",
        predicate="finalized EVM validator update does not equal Tellor Layer and contract state",
        expected=(
            "strictly increasing timestamp and exact timestamp, threshold, validator-set "
            "hash, and checkpoint equality"
        ),
        observed=observed,
        incident_key="databridge-validator:{}".format(timestamp),
        delivery_key="1:{}:M3:{}".format(
            event.transaction_hash, event.log_index
        ),
        chain_point=chain_point,
        signal=event.signature,
        contract=event.address,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        evidence={"validator_timestamp": timestamp, "checkpoint": computed},
    )


def evaluate_stale_databridge(address, chain_point, ethereum):
    state = read_databridge_state(ethereum, address, chain_point.number)
    if not state["initialized"] or state["validator_timestamp"] == 0:
        return None
    age = chain_point.timestamp - state["validator_timestamp"] // 1000
    if age <= state["unbonding_period"]:
        return None
    timestamp = state["validator_timestamp"]
    return Finding(
        slug="databridge-integrity",
        predicate="DataBridge entered its source-proven unusable stale state",
        expected={"validator_age_seconds": "<={}".format(state["unbonding_period"])},
        observed={"validator_age_seconds": age, **state},
        incident_key="databridge-stale:{}".format(timestamp),
        delivery_key="1:{}:M3:stale".format(chain_point.block_hash),
        chain_point=chain_point,
        signal="finalized-state-evaluation",
        contract=address,
        evidence={"validator_timestamp": timestamp, "checkpoint": state["checkpoint"]},
    )


def _validator_update_matches(threshold, timestamp, valset_hash, computed, state, previous, layer):
    return (
        timestamp > previous["validator_timestamp"]
        and state["validator_timestamp"] == timestamp
        and state["power_threshold"] == threshold
        and state["checkpoint"] == computed
        and layer["timestamp"] == timestamp
        and layer["power_threshold"] == threshold
        and layer["validator_set_hash"] == valset_hash
        and layer["checkpoint"] == computed
    )


def _bytes32(value):
    raw = unhex(str(value).lower())
    if len(raw) != 32:
        raise ValueError("value is not bytes32")
    return raw


def _hex32(value):
    text = str(value).lower()
    if not text.startswith("0x"):
        text = "0x" + text
    _bytes32(text)
    return text
