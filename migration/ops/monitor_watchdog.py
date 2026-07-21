#!/usr/bin/env python3
"""Desired-state-aware OpenZeppelin Monitor watchdog.

The watchdog deliberately performs only read-only Docker/systemd inspection.
Its service runs as root because Docker socket access is root-equivalent; the
installer never adds a monitoring user to the Docker group.
"""

import argparse
import copy
import json
import logging
import os
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


MIGRATION_DIR = Path("/opt/tellor/autoTasks/migration")
COMPOSE_FILE = MIGRATION_DIR / "docker-compose.yaml"
CHECKPOINT_PATH = MIGRATION_DIR / "data" / "ethereum_mainnet_last_block.txt"
STATE_PATH = Path("/var/lib/tellor/monitor-watchdog/state.json")
ALERT_LOG_PATH = Path("/var/lib/tellor/monitor-watchdog/alerts.log")
MONITOR_UNIT = "openzeppelin-monitor.service"
WATCHDOG_ROUTE = "Tellor Monitor Watchdog"
STATE_VERSION = 1
DEFAULT_MAX_CHECKPOINT_AGE_SECONDS = 600
UTC = timezone.utc


SCRIPT_PARENT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PARENT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PARENT))

from report_freshness import _atomic_write_bytes, routed_deliver  # noqa: E402


class WatchdogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    summary: str
    checkpoint: Optional[int]
    checkpoint_seen_at: Optional[int]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_command(arguments: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WatchdogError("inspection command could not run") from error
    return CommandResult(completed.returncode, completed.stdout.strip())


def _default_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "incident_open": False,
        "last_checkpoint": None,
        "checkpoint_seen_at": None,
    }


def _validated_state(value: Any) -> Dict[str, Any]:
    keys = {"version", "incident_open", "last_checkpoint", "checkpoint_seen_at"}
    if not isinstance(value, dict) or set(value) != keys or value.get("version") != STATE_VERSION:
        raise ValueError("invalid watchdog state object")
    if not isinstance(value["incident_open"], bool):
        raise ValueError("invalid watchdog incident state")
    checkpoint = value["last_checkpoint"]
    seen_at = value["checkpoint_seen_at"]
    if checkpoint is not None and (isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 0):
        raise ValueError("invalid watchdog checkpoint")
    if seen_at is not None and (isinstance(seen_at, bool) or not isinstance(seen_at, int) or seen_at < 0):
        raise ValueError("invalid watchdog checkpoint timestamp")
    if (checkpoint is None) != (seen_at is None):
        raise ValueError("incomplete watchdog checkpoint state")
    return copy.deepcopy(value)


class WatchdogStateStore:
    def __init__(self, path: Path, now: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.now = now

    def load(self) -> Dict[str, Any]:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return _default_state()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise WatchdogError("watchdog state must be a regular non-symlink file")
        try:
            return _validated_state(json.loads(self.path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            stamp = self.now().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            preserved = self.path.with_name(
                "{}.corrupt.{}.{}".format(self.path.name, stamp, os.getpid())
            )
            try:
                os.replace(str(self.path), str(preserved))
            except OSError as preserve_error:
                raise WatchdogError("invalid watchdog state could not be preserved") from preserve_error
            logging.error("preserved invalid watchdog state as %s", preserved.name)
            return _default_state()

    def save(self, value: Mapping[str, Any]) -> None:
        state = _validated_state(dict(value))
        content = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            _atomic_write_bytes(self.path, content)
        except OSError as error:
            raise WatchdogError("could not atomically replace watchdog state") from error


class MonitorInspector:
    def __init__(
        self,
        command: Callable[[Sequence[str]], CommandResult] = run_command,
        checkpoint_path: Path = CHECKPOINT_PATH,
        migration_dir: Path = MIGRATION_DIR,
        compose_file: Path = COMPOSE_FILE,
        max_checkpoint_age: int = DEFAULT_MAX_CHECKPOINT_AGE_SECONDS,
    ):
        self.command = command
        self.checkpoint_path = Path(checkpoint_path)
        self.migration_dir = Path(migration_dir)
        self.compose_file = Path(compose_file)
        self.max_checkpoint_age = max_checkpoint_age

    def desired_running(self) -> bool:
        result = self.command(("/usr/bin/systemctl", "is-enabled", MONITOR_UNIT))
        state = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if result.returncode == 0 and state in ("enabled", "enabled-runtime", "linked", "linked-runtime"):
            return True
        if state in ("disabled", "masked", "masked-runtime", "not-found"):
            return False
        raise WatchdogError("could not determine the Monitor desired state")

    def inspect(self, prior_state: Mapping[str, Any], now_timestamp: int) -> HealthResult:
        reasons = []
        compose = self.command((
            "/usr/bin/docker",
            "compose",
            "--project-directory",
            str(self.migration_dir),
            "-f",
            str(self.compose_file),
            "ps",
            "-q",
            "monitor",
        ))
        container_id = compose.stdout.strip().splitlines()[0] if compose.stdout.strip() else ""
        if compose.returncode != 0:
            reasons.append("Docker Compose status failed")
        elif not container_id:
            reasons.append("Monitor container is absent")
        else:
            inspected = self.command((
                "/usr/bin/docker", "inspect", "--format", "{{json .State}}", container_id
            ))
            if inspected.returncode != 0:
                reasons.append("Monitor container inspection failed")
            else:
                try:
                    container_state = json.loads(inspected.stdout)
                except json.JSONDecodeError:
                    reasons.append("Monitor container state is invalid")
                else:
                    if not isinstance(container_state, dict) or container_state.get("Status") != "running":
                        reasons.append("Monitor container is not running")
                    health = container_state.get("Health")
                    if isinstance(health, dict) and health.get("Status") != "healthy":
                        reasons.append("Monitor container healthcheck is not healthy")

        checkpoint, seen_at, checkpoint_reasons = self._inspect_checkpoint(prior_state, now_timestamp)
        reasons.extend(checkpoint_reasons)
        summary = "; ".join(reasons) if reasons else "container and checkpoint checks passed"
        return HealthResult(not reasons, summary, checkpoint, seen_at)

    def _inspect_checkpoint(
        self, prior_state: Mapping[str, Any], now_timestamp: int
    ) -> tuple:
        reasons = []
        try:
            metadata = self.checkpoint_path.lstat()
        except FileNotFoundError:
            return None, None, ["Ethereum checkpoint is missing"]
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None, None, ["Ethereum checkpoint is not a regular file"]
        try:
            text = self.checkpoint_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None, None, ["Ethereum checkpoint is unreadable"]
        if not text.isdigit():
            return None, None, ["Ethereum checkpoint is invalid"]
        checkpoint = int(text)
        if now_timestamp - int(metadata.st_mtime) > self.max_checkpoint_age:
            reasons.append("Ethereum checkpoint file is stale")

        prior_checkpoint = prior_state["last_checkpoint"]
        if prior_checkpoint is not None and checkpoint < prior_checkpoint:
            reasons.append(
                f"Ethereum checkpoint regressed from {prior_checkpoint} to {checkpoint}"
            )
            # Keep the high-water mark so a restored or corrupt lower checkpoint
            # remains unhealthy until the monitor genuinely catches up.
            return prior_checkpoint, prior_state["checkpoint_seen_at"], reasons
        if prior_checkpoint == checkpoint:
            seen_at = prior_state["checkpoint_seen_at"]
            if seen_at is None:
                seen_at = now_timestamp
            elif now_timestamp - seen_at > self.max_checkpoint_age:
                reasons.append("Ethereum checkpoint has not advanced")
        else:
            seen_at = now_timestamp
        return checkpoint, seen_at, reasons


class MonitorWatchdog:
    def __init__(
        self,
        inspector: MonitorInspector,
        store: WatchdogStateStore,
        deliver: Callable[..., Any],
        delivery_mode: str,
        alert_log_path: Path = ALERT_LOG_PATH,
        now: Callable[[], datetime] = utc_now,
        hostname: Callable[[], str] = socket.gethostname,
    ):
        if delivery_mode not in ("live", "log-only"):
            raise ValueError("delivery mode must be live or log-only")
        self.inspector = inspector
        self.store = store
        self.deliver = deliver
        self.delivery_mode = delivery_mode
        self.alert_log_path = Path(alert_log_path)
        self.now = now
        self.hostname = hostname

    def _send(self, content: str, status: str) -> None:
        self.deliver(
            WATCHDOG_ROUTE,
            content,
            mode=self.delivery_mode,
            log_path=self.alert_log_path,
            context={"source": "monitor-watchdog", "status": status},
        )

    def run(self) -> bool:
        instant = self.now().astimezone(UTC)
        state = self.store.load()
        if not self.inspector.desired_running():
            desired_off = _default_state()
            if state != desired_off:
                self.store.save(desired_off)
            logging.info("Monitor is intentionally disabled; watchdog is quiet")
            return True

        result = self.inspector.inspect(state, int(instant.timestamp()))
        candidate = copy.deepcopy(state)
        candidate["last_checkpoint"] = result.checkpoint
        candidate["checkpoint_seen_at"] = result.checkpoint_seen_at
        if not result.healthy and not state["incident_open"]:
            content = "\n".join((
                "🚨 **OpenZeppelin Monitor watchdog warning**",
                "> Host: `{}`".format(self.hostname()),
                "> Check: `{}`".format(result.summary),
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(content, "alarm")
            candidate["incident_open"] = True
            self.store.save(candidate)
        elif result.healthy and state["incident_open"]:
            content = "\n".join((
                "✅ **OpenZeppelin Monitor watchdog recovered**",
                "> Host: `{}`".format(self.hostname()),
                "> Check: `{}`".format(result.summary),
                "> Observed: `{}`".format(_utc_iso(instant)),
            ))
            self._send(content, "recovery")
            candidate["incident_open"] = False
            self.store.save(candidate)
        elif candidate != state:
            self.store.save(candidate)
        return result.healthy


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch OpenZeppelin Monitor health")
    parser.add_argument("--state-path", type=Path, default=STATE_PATH)
    parser.add_argument("--checkpoint-path", type=Path, default=CHECKPOINT_PATH)
    parser.add_argument("--alert-log-path", type=Path, default=ALERT_LOG_PATH)
    parser.add_argument(
        "--delivery-mode",
        choices=("live", "log-only"),
        default=os.environ.get("TELLOR_ALERT_DELIVERY_MODE"),
        help="required explicitly or through TELLOR_ALERT_DELIVERY_MODE",
    )
    parser.add_argument(
        "--max-checkpoint-age",
        type=int,
        default=DEFAULT_MAX_CHECKPOINT_AGE_SECONDS,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.delivery_mode is None:
        parser.error("--delivery-mode or TELLOR_ALERT_DELIVERY_MODE is required")
    if args.max_checkpoint_age <= 0:
        parser.error("--max-checkpoint-age must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        watchdog = MonitorWatchdog(
            inspector=MonitorInspector(
                checkpoint_path=args.checkpoint_path,
                max_checkpoint_age=args.max_checkpoint_age,
            ),
            store=WatchdogStateStore(args.state_path),
            deliver=routed_deliver,
            delivery_mode=args.delivery_mode,
            alert_log_path=args.alert_log_path,
        )
        return 0 if watchdog.run() else 1
    except WatchdogError as error:
        logging.error("watchdog failed: %s", error)
        return 1
    except Exception as error:
        logging.error("watchdog failed with %s", error.__class__.__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
