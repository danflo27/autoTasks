#!/usr/bin/env python3
"""Couple OpenZeppelin Monitor and watchdog desired state safely.

The command never prints environment values. Mutating operations validate the
secret-file metadata and existing redacted preflights, then restore the prior
systemd enablement/active state if a transition fails.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


MIGRATION_DIR = Path("/opt/tellor/autoTasks/migration")
COMPOSE_FILE = MIGRATION_DIR / "docker-compose.yaml"
ENV_FILE = MIGRATION_DIR / ".env"
ROUTE_FILE = MIGRATION_DIR / "secrets" / "discord_webhooks.json"
CHECKPOINT_FILE = MIGRATION_DIR / "data" / "ethereum_mainnet_last_block.txt"
MONITOR_UNIT = "openzeppelin-monitor.service"
WATCHDOG_UNIT = "monitor-watchdog.timer"
ENABLED_STATES = {"enabled"}
DISABLED_STATES = {"disabled"}
ACTIVE_STATES = {"active"}
INACTIVE_STATES = {"inactive"}


class HostOpsError(RuntimeError):
    """A lifecycle operation could not complete safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


class CommandRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        check: bool = True,
    ) -> CommandResult:
        command_env = None
        if env is not None:
            command_env = dict(os.environ)
            command_env.update(env)
        try:
            completed = subprocess.run(
                list(arguments),
                cwd=str(cwd) if cwd else None,
                env=command_env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise HostOpsError("required host command could not run") from error
        result = CommandResult(completed.returncode, completed.stdout.strip())
        if check and result.returncode != 0:
            raise HostOpsError("required host command failed")
        return result


@dataclass(frozen=True)
class UnitState:
    enabled_state: str
    active_state: str

    @property
    def enabled(self) -> bool:
        return self.enabled_state in ENABLED_STATES

    @property
    def active(self) -> bool:
        return self.active_state in ACTIVE_STATES


@dataclass(frozen=True)
class LifecycleSnapshot:
    monitor: UnitState
    watchdog: UnitState


class MonitorLifecycle:
    def __init__(
        self,
        runner: Optional[CommandRunner] = None,
        migration_dir: Path = MIGRATION_DIR,
        checkpoint_file: Path = CHECKPOINT_FILE,
    ):
        self.runner = runner or CommandRunner()
        self.migration_dir = Path(migration_dir)
        self.compose_file = self.migration_dir / "docker-compose.yaml"
        self.env_file = self.migration_dir / ".env"
        self.route_file = self.migration_dir / "secrets" / "discord_webhooks.json"
        self.checkpoint_file = Path(checkpoint_file)

    def _systemctl_state(self, operation: str, unit: str) -> str:
        result = self.runner.run(
            ("/usr/bin/systemctl", operation, unit), check=False
        )
        output = result.stdout.strip().splitlines()
        return output[0] if output else "unknown"

    def observe_unit(self, unit: str) -> UnitState:
        return UnitState(
            enabled_state=self._systemctl_state("is-enabled", unit),
            active_state=self._systemctl_state("is-active", unit),
        )

    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            monitor=self.observe_unit(MONITOR_UNIT),
            watchdog=self.observe_unit(WATCHDOG_UNIT),
        )

    @staticmethod
    def _require_supported_snapshot(snapshot: LifecycleSnapshot) -> None:
        allowed_enabled = ENABLED_STATES | DISABLED_STATES
        allowed_active = ACTIVE_STATES | INACTIVE_STATES
        for name, unit in (("Monitor", snapshot.monitor), ("watchdog", snapshot.watchdog)):
            if unit.enabled_state not in allowed_enabled:
                raise HostOpsError(
                    "{} enablement is {}; resolve it explicitly first".format(
                        name, unit.enabled_state
                    )
                )
            if unit.active_state not in allowed_active:
                raise HostOpsError(
                    "{} activity is {}; resolve it explicitly first".format(
                        name, unit.active_state
                    )
                )

    @staticmethod
    def _validate_private_file(path: Path, owner_uid: Optional[int] = None) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise HostOpsError("required private file is missing: {}".format(path)) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise HostOpsError("required private file is not a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise HostOpsError("required private file must have mode 0600")
        if owner_uid is not None and metadata.st_uid != owner_uid:
            raise HostOpsError("required private file has the wrong owner")

    def preflight_enable(self) -> None:
        self._validate_private_file(self.env_file, owner_uid=0)
        self._validate_private_file(self.route_file)
        try:
            route_directory = self.route_file.parent.lstat()
        except FileNotFoundError as error:
            raise HostOpsError("route directory is missing") from error
        if (
            stat.S_ISLNK(route_directory.st_mode)
            or not stat.S_ISDIR(route_directory.st_mode)
            or stat.S_IMODE(route_directory.st_mode) != 0o700
        ):
            raise HostOpsError("route directory must be a real mode-0700 directory")

        self.runner.run(
            (
                "/usr/bin/docker",
                "compose",
                "--project-directory",
                str(self.migration_dir),
                "-f",
                str(self.compose_file),
                "config",
                "--quiet",
            )
        )
        redacted_env = {"DISCORD_WEBHOOKS_FILE": str(self.route_file)}
        self.runner.run(
            ("/usr/bin/python3", "quickstart.py", "check-discord-routes"),
            cwd=self.migration_dir,
            env=redacted_env,
        )
        self.runner.run(
            ("/usr/bin/python3", "quickstart.py", "check-rpcs"),
            cwd=self.migration_dir,
        )

    def _container_state(self) -> Dict[str, Any]:
        compose = self.runner.run(
            (
                "/usr/bin/docker",
                "compose",
                "--project-directory",
                str(self.migration_dir),
                "-f",
                str(self.compose_file),
                "ps",
                "-q",
                "monitor",
            ),
            check=False,
        )
        container_id = compose.stdout.strip().splitlines()[0] if compose.stdout.strip() else ""
        if compose.returncode != 0:
            return {"id": None, "status": "inspection-failed", "health": None}
        if not container_id:
            return {"id": None, "status": "absent", "health": None}
        inspected = self.runner.run(
            (
                "/usr/bin/docker",
                "inspect",
                "--format",
                "{{json .State}}",
                container_id,
            ),
            check=False,
        )
        if inspected.returncode != 0:
            return {"id": container_id, "status": "inspection-failed", "health": None}
        try:
            state = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            return {"id": container_id, "status": "invalid", "health": None}
        health = state.get("Health") if isinstance(state, dict) else None
        return {
            "id": container_id,
            "status": state.get("Status") if isinstance(state, dict) else "invalid",
            "health": health.get("Status") if isinstance(health, dict) else None,
        }

    def _checkpoint_state(self) -> Dict[str, Any]:
        try:
            metadata = self.checkpoint_file.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return {"path": str(self.checkpoint_file), "block": None, "age_seconds": None}
            text = self.checkpoint_file.read_text(encoding="ascii").strip()
            block = int(text) if text.isdigit() else None
            age = max(0, int(time.time() - metadata.st_mtime))
            return {"path": str(self.checkpoint_file), "block": block, "age_seconds": age}
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return {"path": str(self.checkpoint_file), "block": None, "age_seconds": None}

    def status(self) -> Dict[str, Any]:
        snapshot = self.snapshot()
        container = self._container_state()
        enabled_pair = (snapshot.monitor.enabled, snapshot.watchdog.enabled)
        allowed = ENABLED_STATES | DISABLED_STATES
        if (
            snapshot.monitor.enabled_state not in allowed
            or snapshot.watchdog.enabled_state not in allowed
        ):
            desired_state = "unsupported"
        elif enabled_pair == (True, True):
            desired_state = "enabled"
        elif enabled_pair == (False, False):
            desired_state = "disabled"
        else:
            desired_state = "drift"

        if desired_state == "enabled":
            healthy = (
                snapshot.monitor.active
                and snapshot.watchdog.active
                and container["status"] == "running"
                and container["health"] in (None, "healthy")
            )
        elif desired_state == "disabled":
            healthy = (
                not snapshot.monitor.active
                and not snapshot.watchdog.active
                and container["status"] in ("absent", "created", "exited", "dead")
            )
        else:
            healthy = False

        return {
            "schema_version": 1,
            "desired_state": desired_state,
            "healthy": healthy,
            "monitor": asdict(snapshot.monitor),
            "watchdog": asdict(snapshot.watchdog),
            "container": container,
            "checkpoint": self._checkpoint_state(),
        }

    def _set_enabled(self, unit: str, enabled: bool) -> None:
        self.runner.run(
            ("/usr/bin/systemctl", "enable" if enabled else "disable", unit)
        )

    def _set_active(self, unit: str, active: bool) -> None:
        self.runner.run(
            ("/usr/bin/systemctl", "start" if active else "stop", unit)
        )

    def _restore(self, snapshot: LifecycleSnapshot) -> None:
        errors = []
        actions = (
            (self._set_enabled, MONITOR_UNIT, snapshot.monitor.enabled),
            (self._set_enabled, WATCHDOG_UNIT, snapshot.watchdog.enabled),
            (self._set_active, MONITOR_UNIT, snapshot.monitor.active),
            (self._set_active, WATCHDOG_UNIT, snapshot.watchdog.active),
        )
        for function, unit, value in actions:
            try:
                function(unit, value)
            except HostOpsError as error:
                errors.append(str(error))
        if errors:
            raise HostOpsError("transition failed and prior lifecycle state could not be restored")

    def enable(self) -> Dict[str, Any]:
        before = self.snapshot()
        self._require_supported_snapshot(before)
        self.preflight_enable()
        try:
            self._set_enabled(MONITOR_UNIT, True)
            self._set_enabled(WATCHDOG_UNIT, True)
            self._set_active(MONITOR_UNIT, True)
            container = self._container_state()
            if container["status"] != "running":
                raise HostOpsError("Monitor container did not reach running state")
            self._set_active(WATCHDOG_UNIT, True)
            after = self.status()
            if after["desired_state"] != "enabled" or not after["healthy"]:
                raise HostOpsError("enabled lifecycle readback did not converge")
            return after
        except HostOpsError as transition_error:
            try:
                self._restore(before)
            except HostOpsError as restore_error:
                raise restore_error from transition_error
            raise HostOpsError("enable failed; prior lifecycle state was restored") from transition_error

    def disable(self) -> Dict[str, Any]:
        before = self.snapshot()
        self._require_supported_snapshot(before)
        try:
            self._set_active(WATCHDOG_UNIT, False)
            self._set_enabled(WATCHDOG_UNIT, False)
            self._set_active(MONITOR_UNIT, False)
            self._set_enabled(MONITOR_UNIT, False)
            after = self.status()
            if after["desired_state"] != "disabled" or not after["healthy"]:
                raise HostOpsError("disabled lifecycle readback did not converge")
            return after
        except HostOpsError as transition_error:
            try:
                self._restore(before)
            except HostOpsError as restore_error:
                raise restore_error from transition_error
            raise HostOpsError("disable failed; prior lifecycle state was restored") from transition_error


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read or reconcile coupled Monitor/watchdog desired state"
    )
    parser.add_argument("action", choices=("status", "enable", "disable"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    if args.action != "status" and os.geteuid() != 0:
        print("monitor_state.py enable/disable must run as root", file=sys.stderr)
        return 1
    lifecycle = MonitorLifecycle()
    try:
        result = getattr(lifecycle, args.action)()
    except HostOpsError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["healthy"] else 3


if __name__ == "__main__":
    sys.exit(main())
