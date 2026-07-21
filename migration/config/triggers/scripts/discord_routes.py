"""Secure, per-producer Discord routing shared by Monitor and host jobs.

This module intentionally uses only the Python standard library.  Monitor
triggers import it from the read-only config mount, while host-side freshness
and watchdog jobs can add this directory to ``sys.path`` and use the same
delivery primitive.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MONITOR_ROUTE_NAMES = (
    "TellorFlex Disputable Value",
    "Tellor Address Updates",
    "Tellor Staking",
    "Tellor Token Bridge",
    "Tellor Deposit To Layer",
    "Tellor Withdraw From Layer",
    "Update Oracle Data Calls",
    "Update Validator Set Calls",
    "Guardian Reset Validator Set Calls",
    "Smoke Test USDC Transfer",
)

HOST_ROUTE_NAMES = (
    "Tellor ETH/USD Freshness",
    "Tellor AMPL/USD Freshness",
    "Tellor USPCE Freshness",
    "Tellor Freshness Checker Health",
    "Tellor Monitor Watchdog",
)

ROUTE_NAMES = MONITOR_ROUTE_NAMES + HOST_ROUTE_NAMES
OPTIONAL_ROUTE_NAMES = frozenset({"Smoke Test USDC Transfer"})
REQUIRED_ROUTE_NAMES = tuple(
    name for name in ROUTE_NAMES if name not in OPTIONAL_ROUTE_NAMES
)

ROUTE_PATH_ENV = "DISCORD_WEBHOOKS_FILE"
DELIVERY_MODE_ENV = "TELLOR_ALERT_DELIVERY_MODE"
DELIVERY_MODES = frozenset({"live", "log-only"})
DISCORD_CONTENT_LIMIT = 2000
MAX_ROUTE_FILE_BYTES = 128 * 1024

_WEBHOOK_PATH_RE = re.compile(
    r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9._-]+"
)


class DiscordRouteError(RuntimeError):
    """Base class for errors safe to surface without secret redaction."""


class RouteConfigurationError(DiscordRouteError):
    """The delivery mode, route file, or requested route is invalid."""


class RouteDeliveryError(DiscordRouteError):
    """Local logging or confirmed Discord delivery failed."""


def validate_webhook_url(value):
    """Validate and normalize a Discord webhook without echoing it on error."""
    if not isinstance(value, str):
        raise RouteConfigurationError("Discord route must be a webhook URL")
    value = value.strip()
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise RouteConfigurationError("Discord route must be a webhook URL")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        raise RouteConfigurationError("Discord route is invalid") from None
    path = parsed.path.rstrip("/")
    try:
        port = parsed.port
    except ValueError:
        raise RouteConfigurationError("Discord route is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _WEBHOOK_PATH_RE.fullmatch(path)
    ):
        raise RouteConfigurationError(
            "Discord route must be an https://discord.com webhook URL"
        )
    return urllib.parse.urlunsplit(("https", "discord.com", path, "", ""))


def _mode_bits(value):
    return stat.S_IMODE(value.st_mode)


def _secure_read(path):
    """Read one complete route-file generation using directory-relative fds."""
    route_path = Path(path)
    if not route_path.name or route_path.name in (".", ".."):
        raise RouteConfigurationError("Discord route file path is invalid")

    directory = route_path.parent
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_only = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_only is None:
        raise RouteConfigurationError(
            "secure Discord route file access is unsupported on this platform"
        )

    directory_flags = os.O_RDONLY | nofollow | directory_only
    file_flags = os.O_RDONLY | nofollow
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC

    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(os.fspath(directory), directory_flags)
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode) or _mode_bits(directory_stat) != 0o700:
            raise RouteConfigurationError(
                "Discord route directory must be a non-symlink directory with mode 0700"
            )

        file_fd = os.open(route_path.name, file_flags, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or _mode_bits(file_stat) != 0o600:
            raise RouteConfigurationError(
                "Discord route file must be a regular non-symlink file with mode 0600"
            )

        with os.fdopen(file_fd, "rb", closefd=True) as stream:
            file_fd = None
            raw = stream.read(MAX_ROUTE_FILE_BYTES + 1)
    except RouteConfigurationError:
        raise
    except (OSError, ValueError):
        raise RouteConfigurationError(
            "Discord route file is missing, inaccessible, or unsafe"
        ) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)

    if len(raw) > MAX_ROUTE_FILE_BYTES:
        raise RouteConfigurationError("Discord route file is too large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RouteConfigurationError("Discord route file is not valid UTF-8") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RouteConfigurationError(
                "Discord route file contains a duplicate monitor name"
            )
        result[key] = value
    return result


def _parse_route_file(path):
    try:
        routes = json.loads(_secure_read(path), object_pairs_hook=_unique_object)
    except RouteConfigurationError:
        raise
    except json.JSONDecodeError:
        raise RouteConfigurationError("Discord route file is not valid JSON") from None
    if not isinstance(routes, dict):
        raise RouteConfigurationError(
            "Discord route file must contain one monitor-name-to-URL object"
        )
    return routes


def _route_path(path=None):
    if path is not None:
        return Path(path)
    configured = os.environ.get(ROUTE_PATH_ENV, "").strip()
    if not configured:
        raise RouteConfigurationError(
            "DISCORD_WEBHOOKS_FILE is required for live delivery"
        )
    return Path(configured)


def load_discord_routes(path=None):
    """Load and validate routes from a freshly opened secure file."""
    routes = _parse_route_file(_route_path(path))
    validated = {}
    for name, webhook in routes.items():
        if not isinstance(name, str) or not name or any(ord(char) < 32 for char in name):
            raise RouteConfigurationError("Discord route file contains an invalid monitor name")
        validated[name] = validate_webhook_url(webhook)
    return validated


def preflight_route_statuses(path=None, required_names=None):
    """Return redaction-safe route statuses, unknown counts, and validity."""
    required = set(REQUIRED_ROUTE_NAMES if required_names is None else required_names)
    if not required.issubset(ROUTE_NAMES):
        raise RouteConfigurationError("route preflight received an unknown required name")
    try:
        routes = load_discord_routes(path)
    except RouteConfigurationError:
        return (
            {name: "file-invalid" for name in ROUTE_NAMES},
            0,
            False,
        )

    statuses = {}
    valid = True
    for name in ROUTE_NAMES:
        if name in routes:
            statuses[name] = "ok"
        elif name in required:
            statuses[name] = "missing"
            valid = False
        else:
            statuses[name] = "optional-missing"
    unknown_count = len(set(routes) - set(ROUTE_NAMES))
    return statuses, unknown_count, valid


def _discord_wait_url(webhook):
    parts = urllib.parse.urlsplit(webhook)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "wait=true", "")
    )


def _retry_after(error):
    value = error.headers.get("Retry-After")
    if value is None:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            value = payload.get("retry_after")
        except Exception:
            value = None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 1.0


def _discord_content(content):
    if len(content) <= DISCORD_CONTENT_LIMIT:
        return content
    suffix = "\n… (truncated; full content is in the local alert log)"
    return content[: DISCORD_CONTENT_LIMIT - len(suffix)] + suffix


def post_discord_webhook(webhook, content, attempts=3, sleep=time.sleep):
    """POST once with bounded retry and Discord persistence confirmation."""
    webhook = validate_webhook_url(webhook)
    payload = json.dumps(
        {
            "content": _discord_content(content),
            "allowed_mentions": {"parse": []},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _discord_wait_url(webhook),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "tellor-monitor/1.0",
        },
    )
    last_reason = "network error"
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                response.read()
            return
        except urllib.error.HTTPError as error:
            last_reason = "HTTP {}".format(error.code)
            if error.code == 429 and attempt < attempts:
                sleep(_retry_after(error))
                continue
            if 500 <= error.code < 600 and attempt < attempts:
                sleep(float(attempt))
                continue
            raise RouteDeliveryError(
                "Discord webhook delivery failed ({})".format(last_reason)
            ) from None
        except Exception:
            if attempt == attempts:
                raise RouteDeliveryError(
                    "Discord webhook delivery failed after {} attempts ({})".format(
                        attempts, last_reason
                    )
                ) from None
            sleep(float(attempt) / 2)


def _append_alert_log(log_path, monitor_name, content, mode, context):
    record = {
        "ts": _datetime.datetime.now(_datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
        "monitor": monitor_name,
        "mode": mode,
        "content": content,
    }
    if context is not None:
        try:
            extra = dict(context)
        except (TypeError, ValueError):
            raise RouteDeliveryError("local alert context is invalid") from None
        if set(extra).intersection(record):
            raise RouteDeliveryError("local alert context contains a reserved field")
        record.update(extra)
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        raise RouteDeliveryError("local alert logging failed") from None


def deliver_alert(monitor_name, content, *, mode, log_path=None, context=None):
    """Log and deliver one alert without requiring an OpenZeppelin Match.

    ``mode`` is deliberately an argument rather than an implicit default.
    Live calls reopen and revalidate ``DISCORD_WEBHOOKS_FILE`` every time so an
    atomic replacement of the mounted route file takes effect immediately.
    """
    if monitor_name not in ROUTE_NAMES:
        raise RouteConfigurationError("alert producer name is not registered")
    if mode not in DELIVERY_MODES:
        raise RouteConfigurationError("delivery mode must be 'live' or 'log-only'")
    if not isinstance(content, str) or not content:
        raise RouteConfigurationError("alert content must be a non-empty string")

    if log_path is not None:
        _append_alert_log(log_path, monitor_name, content, mode, context)

    if mode == "log-only":
        return False

    routes = load_discord_routes()
    webhook = routes.get(monitor_name)
    if webhook is None:
        raise RouteConfigurationError(
            "enabled alert producer has no configured Discord route"
        )
    post_discord_webhook(webhook, content)
    return True


__all__ = (
    "DELIVERY_MODE_ENV",
    "DELIVERY_MODES",
    "DiscordRouteError",
    "HOST_ROUTE_NAMES",
    "MONITOR_ROUTE_NAMES",
    "OPTIONAL_ROUTE_NAMES",
    "REQUIRED_ROUTE_NAMES",
    "ROUTE_NAMES",
    "ROUTE_PATH_ENV",
    "RouteConfigurationError",
    "RouteDeliveryError",
    "deliver_alert",
    "load_discord_routes",
    "post_discord_webhook",
    "preflight_route_statuses",
    "validate_webhook_url",
)
