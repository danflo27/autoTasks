"""M1 TellorMaster controls and M2 bridge-control predicates."""

import json

from eth_utils import keccak

from .constants import (
    EIP_IMPLEMENTATION_SLOT,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V1,
    TOKEN_BRIDGE_V2,
)
from .ethereum import call_data, decode_call, hx, normalize_address, unhex
from .models import Finding, Unresolved
from .rpc import RpcMethodError


ADDRESS_KEYS = {
    "changeTellorContract(address)": "_TELLOR_CONTRACT",
    "changeDeity(address)": "_DEITY",
    "changeOwner(address)": "_OWNER",
    "NewTellorAddress(address)": "_TELLOR_CONTRACT",
    "NewProposedOracleAddress(address,uint256)": "_PROPOSED_ORACLE",
    "NewOracleAddress(address,uint256)": "_ORACLE_CONTRACT",
}


def address_var(client, contract, key, block):
    result = client.eth_call(
        contract,
        call_data("getAddressVars(bytes32)", ["bytes32"], [keccak(text=key)]),
        block,
    )
    return normalize_address(decode_call(["address"], result)[0])


def uint_var(client, contract, key, block):
    result = client.eth_call(
        contract,
        call_data("getUintVar(bytes32)", ["bytes32"], [keccak(text=key)]),
        block,
    )
    return int(decode_call(["uint256"], result)[0])


def evaluate_master_controls(events, functions, transaction, chain_point, ethereum, approvals):
    sender = normalize_address(transaction["from"])
    candidates = []
    for signature, args in functions:
        if signature in ADDRESS_KEYS:
            candidates.append((signature, args, None))
    for event in events:
        if event.address == TELLOR_MASTER and event.signature in ADDRESS_KEYS:
            candidates.append((event.signature, event.args, event))
    findings = []
    seen = set()
    for signal, args, event in candidates:
        target = _control_target(signal, args)
        identity = (signal, target)
        if identity in seen:
            continue
        seen.add(identity)
        key = ADDRESS_KEYS[signal]
        post_state = {
            "getAddressVars({})".format(key): address_var(
                ethereum, TELLOR_MASTER, key, chain_point.number
            )
        }
        proposal_timestamp = None
        if signal in {
            "NewProposedOracleAddress(address,uint256)",
            "NewOracleAddress(address,uint256)",
        }:
            proposal_timestamp = uint_var(
                ethereum,
                TELLOR_MASTER,
                "_TIME_PROPOSED_UPDATED",
                chain_point.number,
            )
            post_state["getUintVar(_TIME_PROPOSED_UPDATED)"] = proposal_timestamp
        observed = {"sender": sender, "target": target, "post_state": post_state}
        implementation_valid = True
        if signal in {"changeTellorContract(address)", "NewTellorAddress(address)"}:
            storage = ethereum.storage(
                TELLOR_MASTER, EIP_IMPLEMENTATION_SLOT, chain_point.number
            )
            implementation = normalize_address("0x" + storage[-40:])
            code = ethereum.code(target, chain_point.number)
            verify_value = None
            if code not in {"0x", "0x0"}:
                try:
                    verify_value = int(
                        decode_call(
                            ["uint256"],
                            ethereum.eth_call(
                                target, call_data("verify()"), chain_point.number
                            ),
                        )[0]
                    )
                except RpcMethodError:
                    verify_value = "execution reverted"
            post_state["eip_implementation_slot"] = implementation
            implementation_valid = (
                code not in {"0x", "0x0"}
                and isinstance(verify_value, int)
                and verify_value > 9000
                and implementation == target
                and post_state["getAddressVars({})".format(key)] == target
            )
            observed.update(
                {
                    "target_has_code": code not in {"0x", "0x0"},
                    "target_verify": verify_value,
                    "implementation_slot_matches_target": implementation == target,
                }
            )
        approved = approvals.exact_match(
            contract=TELLOR_MASTER,
            signal=signal,
            sender=sender,
            args=_approval_args(signal, args),
            post_state=post_state,
            at_time=chain_point.timestamp,
        )
        observed["approved_change_id"] = (
            approved["change_id"] if approved is not None else None
        )
        if approved is not None and implementation_valid:
            continue
        if signal in {
            "NewProposedOracleAddress(address,uint256)",
            "NewOracleAddress(address,uint256)",
        }:
            incident = "master-control:oracle:{}:{}".format(
                target, proposal_timestamp
            )
        else:
            incident = "master-control:{}:{}:{}".format(
                signal.split("(")[0], target, transaction["hash"].lower()
            )
        log_index = event.log_index if event else None
        suffix = ":{}".format(log_index) if log_index is not None else ""
        findings.append(
            Finding(
                slug="tellormaster-control",
                predicate=(
                    "control change lacks exact approval or valid implementation post-state"
                ),
                expected=(
                    "exact sender, arguments, post-state, time window, release manifest, "
                    "and implementation code/verify/slot checks"
                ),
                observed=observed,
                incident_key=incident,
                delivery_key="1:{}:M1{}".format(transaction["hash"].lower(), suffix),
                chain_point=chain_point,
                signal=signal,
                contract=TELLOR_MASTER,
                transaction_hash=transaction["hash"].lower(),
                log_index=log_index,
                evidence={"target": target, "sender": sender},
            )
        )
    return findings


def evaluate_bridge_controls(
    events,
    transaction,
    chain_point,
    ethereum,
    layer,
    approvals,
    enrollments,
    store,
    second_delay=30,
    sleep=None,
):
    sender = normalize_address(transaction["from"])
    tx_hash = transaction["hash"].lower()
    bridge_events = [event for event in events if event.address in {TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2}]
    findings = []

    pause_events = [
        event
        for event in bridge_events
        if event.name == "PauseApproved"
        or (event.name == "BridgeStateUpdated" and int(event.args["_newState"]) == 1)
    ]
    if pause_events:
        root = min(pause_events, key=lambda event: event.log_index)
        proposal = next(
            (event for event in pause_events if event.name == "PauseApproved"), None
        )
        generation = "v1" if root.address == TOKEN_BRIDGE_V1 else "v2"
        proposal_id = proposal.args["_proposalId"] if proposal else tx_hash
        findings.append(
            Finding(
                slug="bridge-control",
                predicate="bridge entered the paused state",
                expected={"bridge_state": "UNPAUSED"},
                observed={
                    "bridge": generation,
                    "signals": [event.signature for event in pause_events],
                    "proposal_id": proposal_id,
                },
                incident_key="bridge-pause:{}:{}".format(generation, proposal_id),
                delivery_key="1:{}:M2".format(tx_hash),
                chain_point=chain_point,
                signal=" + ".join(event.signature for event in pause_events),
                contract=root.address,
                transaction_hash=tx_hash,
                log_index=root.log_index,
                evidence={"proposal_id": proposal_id},
            )
        )

    for event in bridge_events:
        if event.name == "DataBridgeUpdated":
            target = normalize_address(event.args["_dataBridge"])
            post = normalize_address(
                decode_call(
                    ["address"],
                    ethereum.eth_call(
                        TOKEN_BRIDGE_V2,
                        call_data("dataBridge()"),
                        chain_point.number,
                    ),
                )[0]
            )
            post_state = {"dataBridge()": post}
            approved = approvals.exact_match(
                contract=TOKEN_BRIDGE_V2,
                signal=event.signature,
                sender=sender,
                args={"_dataBridge": target},
                post_state=post_state,
                at_time=chain_point.timestamp,
            )
            enrolled = False
            enrollment_evidence = {"reason": "change is not approved"}
            if approved is not None:
                kwargs = {}
                if sleep is not None:
                    kwargs["sleep"] = sleep
                enrolled, enrollment_evidence = enrollments.verify_live(
                    target,
                    ethereum,
                    layer,
                    chain_point.number,
                    delay=second_delay,
                    **kwargs
                )
            if approved is not None and enrolled and post == target:
                continue
            findings.append(
                Finding(
                    slug="bridge-control",
                    predicate="DataBridge change is unapproved, unenrolled, or has wrong post-state",
                    expected="exact approved change to a pre-enrolled verified DataBridge",
                    observed={
                        "target": target,
                        "post_state": post_state,
                        "approved": approved is not None,
                        "enrollment": enrollment_evidence,
                    },
                    incident_key="v2-databridge:{}:{}".format(target, tx_hash),
                    delivery_key="1:{}:M2:{}".format(tx_hash, event.log_index),
                    chain_point=chain_point,
                    signal=event.signature,
                    contract=TOKEN_BRIDGE_V2,
                    transaction_hash=tx_hash,
                    log_index=event.log_index,
                    evidence={"target": target},
                )
            )
        elif event.name in {"RoleUpdateProposed", "RoleUpdateAccepted"}:
            role = str(event.args["_role"]).lower()
            new_address = normalize_address(event.args["_newAddress"])
            if event.name == "RoleUpdateProposed":
                post = decode_call(
                    ["address", "uint256", "uint256"],
                    ethereum.eth_call(
                        TOKEN_BRIDGE_V2,
                        call_data("roleUpdateProposals(bytes32)", ["bytes32"], [unhex(role)]),
                        chain_point.number,
                    ),
                )
                delay = int(post[1])
                args = {
                    "_role": role,
                    "_newAddress": new_address,
                    "_newUpdateDelay": int(event.args["_newUpdateDelay"]),
                }
                post_state = {
                    "roleUpdateProposals(bytes32)": {
                        "new_address": normalize_address(post[0]),
                        "new_update_delay": delay,
                        "proposal_time": int(post[2]),
                    }
                }
            else:
                post = decode_call(
                    ["address", "uint256"],
                    ethereum.eth_call(
                        TOKEN_BRIDGE_V2,
                        call_data("roles(bytes32)", ["bytes32"], [unhex(role)]),
                        chain_point.number,
                    ),
                )
                delay = int(post[1])
                args = {
                    "_role": role,
                    "_newAddress": new_address,
                    "_newUpdateDelay": delay,
                }
                post_state = {
                    "roles(bytes32)": {
                        "role_address": normalize_address(post[0]),
                        "role_update_delay": delay,
                    }
                }
                existing_role = _open_role_incident(store, role, new_address)
                if existing_role is not None:
                    store.add_evidence(
                        "1:{}:M2:{}".format(tx_hash, event.log_index),
                        "bridge-control",
                        {"signal": event.signature, "args": args},
                        incident_key=existing_role["incident_key"],
                    )
                    continue
            approved = approvals.exact_match(
                contract=TOKEN_BRIDGE_V2,
                signal=event.signature,
                sender=sender,
                args=args,
                post_state=post_state,
                at_time=chain_point.timestamp,
            )
            if approved is not None:
                continue
            findings.append(
                Finding(
                    slug="bridge-control",
                    predicate="V2 role change has no exact active approved-change record",
                    expected="exact sender, role, address, delay, post-state, window, and manifest",
                    observed={"sender": sender, "args": args, "post_state": post_state},
                    incident_key="v2-role:{}:{}:{}:{}".format(
                        role, new_address, delay, tx_hash
                    ),
                    delivery_key="1:{}:M2:{}".format(tx_hash, event.log_index),
                    chain_point=chain_point,
                    signal=event.signature,
                    contract=TOKEN_BRIDGE_V2,
                    transaction_hash=tx_hash,
                    log_index=event.log_index,
                    evidence={"role": role, "new_address": new_address},
                )
            )
        elif event.name == "MintToOracleFailed":
            findings.append(
                Finding(
                    slug="bridge-control",
                    severity_override="P1",
                    predicate="TokenBridgeV2 failed to mint released TRB to the oracle",
                    expected="successful mintToOracle execution and exact issuance routing",
                    observed="MintToOracleFailed event",
                    incident_key="v2-mint-to-oracle-failed:{}".format(tx_hash),
                    delivery_key="1:{}:M2:{}".format(tx_hash, event.log_index),
                    chain_point=chain_point,
                    signal=event.signature,
                    contract=TOKEN_BRIDGE_V2,
                    transaction_hash=tx_hash,
                    log_index=event.log_index,
                )
            )
    return findings


def _control_target(signal, args):
    names = {
        "changeTellorContract(address)": "_tContract",
        "changeDeity(address)": "_newDeity",
        "changeOwner(address)": "_newOwner",
        "NewTellorAddress(address)": "_newTellor",
        "NewProposedOracleAddress(address,uint256)": "_newProposedOracle",
        "NewOracleAddress(address,uint256)": "_newOracle",
    }
    try:
        return normalize_address(args[names[signal]])
    except (KeyError, ValueError) as error:
        raise Unresolved("control target did not decode") from error


def _approval_args(signal, args):
    result = {}
    for key, value in args.items():
        if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
            result[key] = value.lower()
        elif key == "_timestamp":
            result[key] = int(value)
        else:
            result[key] = value
    return result


def _open_role_incident(store, role, new_address):
    for row in store.open_incidents_for_slug("bridge-control"):
        try:
            context = json.loads(row["context_json"])
            evidence = context.get("evidence") or {}
            if (
                evidence.get("role", "").lower() == role.lower()
                and evidence.get("new_address", "").lower()
                == new_address.lower()
            ):
                return row
        except (ValueError, AttributeError):
            continue
    return None
