"""Fail-closed route loading and final alert delivery."""

import json
import os
from pathlib import Path
import re
import stat

import requests

from .constants import MONITORS
from .models import Finding, utc_text


DISCORD_URL = re.compile(
    r"https://(?:canary\.|ptb\.)?discord\.com/api(?:/v\d+)?/webhooks/[^/\s]+/[^/\s]+\Z"
)


# How long a `sending` delivery reservation may be held before we treat it as
# abandoned by a dead process rather than a live in-flight attempt.
#
# A single deliver() call makes at most 3 HTTP POST attempts (retrying only
# on HTTP 429), each with a `requests` timeout of (connect=5s, read=15s), so
# a worst-case in-flight attempt that is still legitimately running takes at
# most roughly 3 * (5 + 15) = 60 seconds between `reserve_delivery()` and the
# HTTP call resolving. STALE_SENDING_RESERVATION_SECONDS is set to 10x that
# bound (600s / 10 minutes) so a live attempt is never mistaken for a
# crashed one, while a genuinely abandoned P0 alert is still recovered
# automatically well within an operator's incident-response window instead
# of being lost forever.
STALE_SENDING_RESERVATION_SECONDS = 600


class DeliveryError(RuntimeError):
    def __init__(self, message, ambiguous=False):
        super().__init__(message)
        self.ambiguous = ambiguous


class DeliveryClient:
    def __init__(self, mode, routes_file, alerts_log, session=None):
        if mode not in {"live", "log-only"}:
            raise ValueError("invalid delivery mode")
        self.mode = mode
        self.routes_file = Path(routes_file) if routes_file else None
        self.alerts_log = Path(alerts_log)
        self.session = session or requests.Session()

    def validate_routes(self):
        if self.mode == "log-only":
            return {}
        if self.routes_file is None:
            raise DeliveryError("route file is not configured")
        try:
            metadata = self.routes_file.lstat()
        except OSError as error:
            raise DeliveryError("route file is unavailable") from error
        if not stat.S_ISREG(metadata.st_mode) or self.routes_file.is_symlink():
            raise DeliveryError("route file must be a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DeliveryError("route file permissions are broader than 0600")
        try:
            pairs = json.loads(
                self.routes_file.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_pairs,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise DeliveryError("route file is invalid") from error
        expected = set(MONITORS)
        if not isinstance(pairs, dict) or set(pairs) != expected:
            raise DeliveryError("route file must contain the exact monitor catalog")
        for value in pairs.values():
            if not isinstance(value, str) or not DISCORD_URL.fullmatch(value):
                raise DeliveryError("route file contains an invalid Discord URL")
        return pairs

    def deliver(self, finding):
        message = finding.message()
        self._append_local(finding, message)
        if self.mode == "log-only":
            return
        routes = self.validate_routes()
        url = routes[finding.slug]
        payload = {"content": message, "allowed_mentions": {"parse": []}}
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.post(
                    url + "?wait=true", json=payload, timeout=(5, 15)
                )
            except requests.RequestException as error:
                raise DeliveryError("Discord delivery result is ambiguous", True) from error
            if 200 <= response.status_code < 300:
                return
            last_error = "Discord returned HTTP {}".format(response.status_code)
            if response.status_code >= 500:
                # Discord can accept a webhook before returning a server error.
                # Preserve the reservation instead of risking a second opening alert.
                raise DeliveryError(last_error, ambiguous=True)
            if response.status_code < 500 and response.status_code != 429:
                break
        raise DeliveryError(last_error or "Discord delivery failed")

    def _append_local(self, finding, message):
        self.alerts_log.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps(
            {
                "recorded_at": utc_text(),
                "finding": finding.as_dict(),
                "content": message,
                "delivery_mode": self.mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor = os.open(
            self.alerts_log,
            os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_WRONLY,
            0o600,
        )
        try:
            payload = (record + "\n").encode()
            if os.write(descriptor, payload) != len(payload):
                raise DeliveryError("short write to local alert ledger")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def publish(store, client, finding):
    existing = store.delivery(finding.delivery_key)
    if existing is not None:
        if existing["status"] == "sending" and store.reclaim_stale_sending(
            finding.delivery_key, STALE_SENDING_RESERVATION_SECONDS
        ):
            # `sending` (unlike `uncertain`) means the HTTP call never got a
            # chance to complete or fail -- the process almost certainly
            # died between reserve_delivery() and contacting Discord, so
            # Discord was very likely never reached. reclaim_stale_sending
            # only returns True when this reservation has sat in `sending`
            # far longer than any live attempt could take (see
            # STALE_SENDING_RESERVATION_SECONDS), so this is a crash-recovery
            # retry, not a duplicate-risking retry of a real in-flight call.
            return _attempt_delivery(store, client, finding)
        if (
            existing["status"] in {"sending", "uncertain"}
            and store.incident(finding.incident_key) is None
        ):
            # A process or network boundary made the remote result ambiguous
            # (or the `sending` reservation is still within its live
            # in-flight window). Preserve deduplication instead of risking a
            # duplicate opening.
            store.open_incident(finding)
        return False
    if store.incident(finding.incident_key) is not None:
        store.add_evidence(
            finding.delivery_key,
            finding.slug,
            finding.as_dict(),
            incident_key=finding.incident_key,
        )
        return False
    if not store.reserve_delivery(finding):
        return False
    return _attempt_delivery(store, client, finding)


def _attempt_delivery(store, client, finding):
    try:
        client.deliver(finding)
    except DeliveryError as error:
        store.delivery_failed(finding.delivery_key, error, error.ambiguous)
        raise
    store.delivery_succeeded(finding)
    return True


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate route name")
        result[key] = value
    return result
