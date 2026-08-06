"""Append one raw OpenZeppelin MonitorMatch to the durable alert-gate spool.

OpenZeppelin Monitor executes this file with Python 3.12 in the Monitor image.
It intentionally has no network or notification code.
"""

import datetime
import json
import os
import sys


ALLOWED_MONITORS = frozenset(
    {
        "tellormaster-control",
        "bridge-control",
        "databridge-integrity",
        "bridge-ledger-integrity",
        "tellorflex-value-integrity",
        "databank-value-integrity",
        "governance-dispute",
        "issuance-integrity",
    }
)
SPOOL_PATH = os.environ.get(
    "ALERT_GATE_SPOOL_PATH", "/app/spool/monitor-matches.jsonl"
)


def main():
    payload = json.load(sys.stdin)
    match = payload.get("monitor_match", {}).get("EVM")
    if not isinstance(match, dict):
        raise ValueError("tellor_alert accepts only EVM MonitorMatch payloads")
    monitor_name = match.get("monitor", {}).get("name")
    if monitor_name not in ALLOWED_MONITORS:
        raise ValueError("monitor is not in the production alert-only catalog")

    envelope = {
        "schema_version": 1,
        "spooled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "payload": payload,
    }
    record = (json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(
        SPOOL_PATH,
        os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_WRONLY,
        0o600,
    )
    try:
        written = os.write(descriptor, record)
        if written != len(record):
            raise OSError("short append to alert-gate spool")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


try:
    main()
except Exception as error:
    print("tellor_alert spool append failed: {!r}".format(error), file=sys.stderr)
    sys.exit(1)
