"""Fail-closed validation for the production monitor catalog and sensors."""

import json
from pathlib import Path

from .constants import EVM_SENSOR_SLUGS, MONITORS


class PolicyError(ValueError):
    pass


def validate_monitor_policy(manifest_file, monitor_config_dir):
    manifest = _json(manifest_file, "monitor manifest")
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "monitors"}:
        raise PolicyError("monitor manifest fields are not exact")
    if manifest["schema_version"] != 1 or not isinstance(manifest["monitors"], list):
        raise PolicyError("monitor manifest schema is invalid")
    expected = [
        {
            "id": monitor_id,
            "slug": slug,
            "severity": severity,
        }
        for slug, (monitor_id, severity) in MONITORS.items()
    ]
    actual = []
    for item in manifest["monitors"]:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "slug",
            "severity",
            "kind",
        }:
            raise PolicyError("monitor catalog entry fields are not exact")
        actual.append({key: item[key] for key in ("id", "slug", "severity")})
    if actual != expected:
        raise PolicyError("monitor catalog does not equal M1-M11")

    directory = Path(monitor_config_dir)
    files = sorted(directory.glob("*.json"))
    if len(files) != 8:
        raise PolicyError("production requires exactly eight EVM sensor files")
    names = set()
    for path in files:
        sensor = _json(path, "EVM sensor")
        name = sensor.get("name")
        names.add(name)
        if name not in EVM_SENSOR_SLUGS:
            raise PolicyError("unexpected EVM sensor name")
        if sensor.get("paused") is not False:
            raise PolicyError("production EVM sensor is paused")
        if sensor.get("networks") != ["ethereum_mainnet"]:
            raise PolicyError("production EVM sensor network differs")
        conditions = sensor.get("match_conditions")
        if not isinstance(conditions, dict):
            raise PolicyError("EVM sensor match conditions are missing")
        if conditions.get("transactions") != [
            {"status": "Success", "expression": None}
        ]:
            raise PolicyError("EVM sensor lacks the exact Success transaction gate")
        if sensor.get("trigger_conditions") != []:
            raise PolicyError("trigger_conditions are forbidden")
        if sensor.get("triggers") != ["tellor_alert"]:
            raise PolicyError("EVM sensor must use only tellor_alert")
        if not conditions.get("functions") and not conditions.get("events"):
            raise PolicyError("EVM sensor has no function or event prefilter")
    if names != EVM_SENSOR_SLUGS:
        raise PolicyError("EVM sensor catalog does not equal M1-M8")
    return {"logical_monitors": 11, "evm_sensors": 8}


def validate_databridge_sensor(enrolled_file, monitor_config_dir):
    enrollment = _json(enrolled_file, "DataBridge enrollment")
    try:
        enrolled = {
            str(item["address"]).lower() for item in enrollment["contracts"]
        }
    except (KeyError, TypeError) as error:
        raise PolicyError("DataBridge enrollment addresses are invalid") from error
    sensors = []
    for path in Path(monitor_config_dir).glob("*.json"):
        value = _json(path, "EVM sensor")
        if value.get("name") == "databridge-integrity":
            sensors.append(value)
    if len(sensors) != 1:
        raise PolicyError("exactly one M3 sensor is required")
    configured = {
        str(item.get("address", "")).lower()
        for item in sensors[0].get("addresses", [])
    }
    if not enrolled or configured != enrolled:
        raise PolicyError(
            "M3 sensor addresses must equal the pre-enrolled DataBridge set"
        )
    return configured


def _json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyError("{} cannot be read".format(label)) from error
