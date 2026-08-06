"""Runtime configuration with explicit deployment-gate validation."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, Optional

from .constants import EVM_CALL_CHAIN_ENVS


class ConfigurationError(ValueError):
    pass


def _required(env, name, required):
    value = env.get(name, "").strip()
    if required and not value:
        raise ConfigurationError("{} is required".format(name))
    return value


@dataclass(frozen=True)
class Settings:
    spool_path: Path
    state_path: Path
    alerts_log_path: Path
    delivery_mode: str
    routes_file: Optional[Path]
    approved_changes_file: Path
    release_manifests_dir: Path
    enrolled_databridges_file: Path
    monitor_manifest_file: Path
    monitor_config_dir: Path
    ethereum_primary_url: str
    ethereum_secondary_url: str
    ethereum_primary_name: str = "ethereum-primary"
    ethereum_secondary_name: str = "ethereum-secondary"
    layer_url: str = ""
    layer_start_height: Optional[int] = None
    layer_minter_seed_file: Optional[Path] = None
    bridge_ledger_seed_file: Optional[Path] = None
    second_read_delay_seconds: float = 30.0
    poll_interval_seconds: float = 5.0
    evm_call_urls: Dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env=None, require_runtime=True):
        values = dict(os.environ if env is None else env)
        delivery_mode = values.get("TELLOR_ALERT_DELIVERY_MODE", "log-only").strip()
        if delivery_mode not in {"live", "log-only"}:
            raise ConfigurationError(
                "TELLOR_ALERT_DELIVERY_MODE must be live or log-only"
            )
        routes = values.get("DISCORD_WEBHOOKS_FILE", "").strip()
        if delivery_mode == "live" and not routes:
            raise ConfigurationError("DISCORD_WEBHOOKS_FILE is required in live mode")
        start = values.get("LAYER_REPLAY_START_HEIGHT", "").strip()
        if require_runtime and not start:
            raise ConfigurationError(
                "LAYER_REPLAY_START_HEIGHT is required for complete M4/M7/M8 state"
            )
        try:
            start_height = int(start) if start else None
            second_delay = float(values.get("ALERT_GATE_SECOND_READ_DELAY", "30"))
            poll_interval = float(values.get("ALERT_GATE_POLL_INTERVAL", "5"))
        except ValueError as error:
            raise ConfigurationError("numeric alert-gate setting is invalid") from error
        if start_height is not None and start_height < 1:
            raise ConfigurationError("LAYER_REPLAY_START_HEIGHT must be positive")
        if second_delay < 0 or poll_interval <= 0:
            raise ConfigurationError("alert-gate intervals are invalid")
        if delivery_mode == "live" and second_delay != 30:
            raise ConfigurationError(
                "live delivery requires ALERT_GATE_SECOND_READ_DELAY=30"
            )
        primary = _required(values, "RPC_ETHEREUM_MAINNET", require_runtime)
        secondary = _required(
            values, "RPC_ETHEREUM_MAINNET_SECONDARY", require_runtime
        )
        if primary and secondary and primary == secondary:
            raise ConfigurationError("the two Ethereum providers must be distinct")
        layer_url = _required(values, "TELLOR_LAYER_URL", require_runtime)
        primary_name = values.get(
            "RPC_ETHEREUM_MAINNET_NAME", "ethereum-primary"
        ).strip()
        secondary_name = values.get(
            "RPC_ETHEREUM_MAINNET_SECONDARY_NAME", "ethereum-secondary"
        ).strip()
        if require_runtime and (
            not primary_name
            or not secondary_name
            or primary_name == secondary_name
        ):
            raise ConfigurationError(
                "the two Ethereum provider identities must be distinct"
            )
        seed = values.get("LAYER_MINTER_SEED_FILE", "").strip()
        if require_runtime and start_height != 1 and not seed:
            raise ConfigurationError(
                "LAYER_MINTER_SEED_FILE is required unless replay starts at height 1"
            )
        bridge_seed = values.get("BRIDGE_LEDGER_SEED_FILE", "").strip()
        if require_runtime and not bridge_seed:
            raise ConfigurationError(
                "BRIDGE_LEDGER_SEED_FILE is required for complete M4 state"
            )
        evm_urls = {
            chain_id: values.get(variable, "").strip()
            for chain_id, variable in EVM_CALL_CHAIN_ENVS.items()
            if values.get(variable, "").strip()
        }
        return cls(
            spool_path=Path(
                values.get(
                    "ALERT_GATE_SPOOL_PATH",
                    "/var/spool/tellor-alert-gate/monitor-matches.jsonl",
                )
            ),
            state_path=Path(
                values.get(
                    "ALERT_GATE_STATE_PATH",
                    "/var/lib/tellor-alert-gate/state.sqlite3",
                )
            ),
            alerts_log_path=Path(
                values.get(
                    "ALERT_GATE_ALERTS_LOG",
                    "/var/lib/tellor-alert-gate/alerts.jsonl",
                )
            ),
            delivery_mode=delivery_mode,
            routes_file=Path(routes) if routes else None,
            approved_changes_file=Path(
                values.get(
                    "APPROVED_CHANGES_FILE",
                    "/etc/tellor-alert-gate/approved_changes.json",
                )
            ),
            release_manifests_dir=Path(
                values.get(
                    "RELEASE_MANIFESTS_DIR",
                    "/etc/tellor-alert-gate/release-manifests",
                )
            ),
            enrolled_databridges_file=Path(
                values.get(
                    "ENROLLED_DATABRIDGES_FILE",
                    "/etc/tellor-alert-gate/enrolled_databridges.json",
                )
            ),
            monitor_manifest_file=Path(
                values.get(
                    "MONITOR_MANIFEST_FILE",
                    "/etc/tellor-alert-gate/monitors.json",
                )
            ),
            monitor_config_dir=Path(
                values.get(
                    "MONITOR_CONFIG_DIR",
                    "/etc/tellor-alert-gate/monitor-config/monitors",
                )
            ),
            ethereum_primary_url=primary,
            ethereum_secondary_url=secondary,
            ethereum_primary_name=primary_name,
            ethereum_secondary_name=secondary_name,
            layer_url=layer_url.rstrip("/"),
            layer_start_height=start_height,
            layer_minter_seed_file=Path(seed) if seed else None,
            bridge_ledger_seed_file=(Path(bridge_seed) if bridge_seed else None),
            second_read_delay_seconds=second_delay,
            poll_interval_seconds=poll_interval,
            evm_call_urls=evm_urls,
        )
