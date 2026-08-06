"""Command-line entry points for validation and the alert-gate service."""

import argparse
import json
import logging
import signal
import sys

from .bridge_seed import read_bridge_ledger_seed
from .config import Settings
from .constants import (
    EVM_CALL_CHAIN_ENVS,
    GOVERNANCE,
    TELLOR_DATA_BANK,
    TELLOR_FLEX,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V1,
    TOKEN_BRIDGE_V2,
)
from .databridge import EnrollmentRegistry
from .delivery import DeliveryClient
from .ethereum import call_data, decode_call, normalize_address
from .freshness import evaluate_eth_usd
from .issuance import read_layer_mint_seed
from .layer import rfc3339_milliseconds
from .policy import validate_databridge_sensor, validate_monitor_policy
from .rpc import JsonRpcClient, LayerClient
from .service import AlertGate
from .state import StateStore


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tellor-alert-gate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-config")
    subparsers.add_parser("check-inputs")
    subparsers.add_parser("check-live")
    subparsers.add_parser("healthcheck")
    subparsers.add_parser("once")
    subparsers.add_parser("run")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.command == "check-config":
        settings = Settings.from_env(require_runtime=False)
        result = validate_monitor_policy(
            settings.monitor_manifest_file, settings.monitor_config_dir
        )
        validate_databridge_sensor(
            settings.enrolled_databridges_file, settings.monitor_config_dir
        )
        DeliveryClient(
            settings.delivery_mode,
            settings.routes_file,
            settings.alerts_log_path,
        ).validate_routes()
        print(json.dumps({"status": "valid", **result}, sort_keys=True))
        return 0
    if args.command == "healthcheck":
        # Deliberately does not call Settings.from_env(require_runtime=True):
        # a healthcheck must keep working (and keep telling the truth about
        # the running process) even if it is invoked before every runtime
        # secret is mounted, and it never touches the network -- it only
        # reads the poll loop's own liveness metadata out of the local
        # SQLite state file. See StateStore.poll_liveness for what this
        # actually proves and cannot catch.
        settings = Settings.from_env(require_runtime=False)
        state = StateStore(settings.state_path)
        try:
            healthy, age_seconds = state.poll_liveness()
        finally:
            state.close()
        print(
            json.dumps(
                {
                    "status": "healthy" if healthy else "unhealthy",
                    "last_scheduled_pass_age_seconds": age_seconds,
                },
                sort_keys=True,
            )
        )
        return 0 if healthy else 1
    if args.command in {"check-inputs", "check-live"}:
        settings = Settings.from_env(require_runtime=True)
        if args.command == "check-live" and settings.delivery_mode != "live":
            raise ValueError("check-live requires live delivery mode")
        result = validate_monitor_policy(
            settings.monitor_manifest_file, settings.monitor_config_dir
        )
        configured_databridges = validate_databridge_sensor(
            settings.enrolled_databridges_file, settings.monitor_config_dir
        )
        DeliveryClient(
            settings.delivery_mode,
            settings.routes_file,
            settings.alerts_log_path,
        ).validate_routes()
        bridge_seed = read_bridge_ledger_seed(settings.bridge_ledger_seed_file)
        if bridge_seed.layer_checkpoint_height + 1 != settings.layer_start_height:
            raise ValueError(
                "M4 Layer checkpoint must immediately precede replay start"
            )
        cursor = read_layer_mint_seed(
            settings.layer_minter_seed_file, settings.layer_start_height
        )
        if (
            cursor.previous_height != bridge_seed.layer_checkpoint_height
            or cursor.previous_block_hash != bridge_seed.layer_checkpoint_hash
            or cursor.previous_block_time_ms != bridge_seed.layer_checkpoint_time_ms
        ):
            raise ValueError("M4 and M8 Layer checkpoints do not identify one block")
        primary = JsonRpcClient(
            settings.ethereum_primary_name, settings.ethereum_primary_url
        )
        secondary = JsonRpcClient(
            settings.ethereum_secondary_name, settings.ethereum_secondary_url
        )
        if primary.chain_id() != 1 or secondary.chain_id() != 1:
            raise ValueError("both configured Ethereum providers must be mainnet")
        for provider in (primary, secondary):
            seed_block = provider.block(bridge_seed.ethereum_checkpoint_number)
            if (
                int(seed_block["number"], 16)
                != bridge_seed.ethereum_checkpoint_number
                or str(seed_block["hash"]).lower()
                != bridge_seed.ethereum_checkpoint_hash
            ):
                raise ValueError(
                    "M4 Ethereum checkpoint does not match both providers"
                )
        primary_head = primary.head_number()
        historical_number = max(0, primary_head - 12)
        historical = primary.block(historical_number, full_transactions=True)
        if int(historical["number"], 16) != historical_number:
            raise ValueError("historical Ethereum block identity differs")
        if primary.block("finalized").get("hash") is None:
            raise ValueError("primary provider does not support the finalized tag")
        transactions = historical.get("transactions") or []
        if not transactions:
            raise ValueError("historical capability probe block has no transaction")
        probe_hash = (
            transactions[0]["hash"]
            if isinstance(transactions[0], dict)
            else transactions[0]
        )
        primary.receipt(probe_hash)
        primary.trace(probe_hash)
        primary.logs(
            address=TELLOR_MASTER,
            from_block=historical_number,
            to_block=historical_number,
        )
        core_contracts = {
            TELLOR_MASTER,
            TELLOR_FLEX,
            GOVERNANCE,
            TOKEN_BRIDGE_V1,
            TOKEN_BRIDGE_V2,
            TELLOR_DATA_BANK,
        }
        for contract in core_contracts:
            if primary.code(contract, historical_number) in {"0x", "0x0"}:
                raise ValueError("core contract has no historical code")
        decode_call(
            ["uint256"],
            primary.eth_call(
                TELLOR_MASTER, call_data("totalSupply()"), historical_number
            ),
        )
        active_databridges = set()
        for contract in (TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2, TELLOR_DATA_BANK):
            address = normalize_address(
                decode_call(
                    ["address"],
                    primary.eth_call(
                        contract, call_data("dataBridge()"), historical_number
                    ),
                )[0]
            )
            active_databridges.add(address)
        if not active_databridges.issubset(configured_databridges):
            raise ValueError("an active DataBridge is absent from M3 enrollment")
        layer = LayerClient("tellor-layer", settings.layer_url)
        layer_status = layer.status()
        network = layer_status["result"]["node_info"]["network"]
        if network != "tellor-1":
            raise ValueError("configured Layer endpoint is not tellor-1")
        latest_layer_height = int(
            layer_status["result"]["sync_info"]["latest_block_height"]
        )
        layer.block(latest_layer_height)
        layer.block_results(latest_layer_height)
        if bridge_seed.layer_checkpoint_height:
            for _ in range(2):
                seed_layer_block = layer.block(bridge_seed.layer_checkpoint_height)
                try:
                    seed_result = seed_layer_block["result"]
                    seed_header = seed_result["block"]["header"]
                    seed_hash = str(seed_result["block_id"]["hash"]).upper()
                    seed_height = int(seed_header["height"])
                    seed_time = rfc3339_milliseconds(seed_header["time"])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError("M4 Layer checkpoint read is incomplete") from error
                if (
                    seed_height != bridge_seed.layer_checkpoint_height
                    or seed_hash != bridge_seed.layer_checkpoint_hash
                    or seed_time != bridge_seed.layer_checkpoint_time_ms
                ):
                    raise ValueError("M4 Layer checkpoint does not match live RPC")
        enrollment = EnrollmentRegistry.load(settings.enrolled_databridges_file)
        for address in sorted(active_databridges):
            verified, evidence = enrollment.verify_live(
                address,
                primary,
                layer,
                historical_number,
                delay=settings.second_read_delay_seconds,
            )
            if not verified:
                raise ValueError(
                    "active DataBridge enrollment did not verify: {}".format(
                        evidence
                    )
                )
        missing_sources = set(EVM_CALL_CHAIN_ENVS) - set(settings.evm_call_urls)
        if missing_sources:
            raise ValueError(
                "missing EVMCall archive providers: {}".format(
                    sorted(missing_sources)
                )
            )
        source_chain_ids = {}
        for chain_id, url in settings.evm_call_urls.items():
            source = JsonRpcClient("evm-call-{}".format(chain_id), url)
            observed_chain_id = source.chain_id()
            if observed_chain_id != chain_id:
                raise ValueError("EVMCall provider chain ID differs")
            source.block("finalized")
            source_chain_ids[chain_id] = observed_chain_id
        freshness_probe = evaluate_eth_usd(
            primary,
            secondary,
            second_delay=settings.second_read_delay_seconds,
        )
        if args.command == "check-live":
            state = StateStore(settings.state_path)
            try:
                state.assert_live_ready(
                    seed_digest=bridge_seed.digest,
                    ethereum_confirmed_block=historical_number,
                    latest_layer_height=latest_layer_height,
                )
            finally:
                state.close()
        print(
            json.dumps(
                {
                    "status": (
                        "live-ready"
                        if args.command == "check-live"
                        else "live-inputs-readable"
                    ),
                    **result,
                    "ethereum_historical_block": historical["hash"],
                    "layer_network": network,
                    "layer_seed_previous_height": cursor.previous_height,
                    "bridge_seed_sha256": bridge_seed.digest,
                    "active_databridges": sorted(active_databridges),
                    "evm_call_chain_ids": source_chain_ids,
                    "eth_usd_probe_failed_predicate": freshness_probe.failed,
                },
                sort_keys=True,
            )
        )
        return 0

    settings = Settings.from_env(require_runtime=True)
    validate_monitor_policy(
        settings.monitor_manifest_file, settings.monitor_config_dir
    )
    validate_databridge_sensor(
        settings.enrolled_databridges_file, settings.monitor_config_dir
    )
    gate = AlertGate(settings)
    try:
        if args.command == "once":
            gate.run_once(run_scheduled=True)
        else:
            signal.signal(signal.SIGTERM, lambda *_: gate.stop())
            signal.signal(signal.SIGINT, lambda *_: gate.stop())
            gate.run()
    finally:
        gate.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
