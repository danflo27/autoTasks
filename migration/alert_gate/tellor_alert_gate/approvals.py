"""Exact, time-bounded approved-change records for M1 and M2."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MANDATORY_FIELDS = frozenset(
    {
        "change_id",
        "chain_id",
        "contract",
        "signal",
        "sender_policy",
        "expected_sender",
        "expected_args",
        "expected_post_state",
        "valid_from",
        "valid_until",
        "release_manifest_sha256",
    }
)


class ApprovalError(ValueError):
    pass


class ApprovalRegistry:
    def __init__(self, records, manifests_dir):
        self.records = records
        self.manifests_dir = Path(manifests_dir)

    @classmethod
    def load(cls, path, manifests_dir):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApprovalError("approved-change file is invalid") from error
        if not isinstance(value, list):
            raise ApprovalError("approved-change file must be a JSON array")
        seen = set()
        records = []
        for item in value:
            if not isinstance(item, dict) or set(item) != MANDATORY_FIELDS:
                raise ApprovalError("approved-change record fields are not exact")
            if item["change_id"] in seen:
                raise ApprovalError("approved-change IDs must be unique")
            seen.add(item["change_id"])
            _validate_record(item)
            records.append(item)
        return cls(records, manifests_dir)

    def exact_match(self, *, contract, signal, sender, args, post_state, at_time):
        moment = _moment(at_time)
        for record in self.records:
            if record["chain_id"] != 1:
                continue
            if record["contract"].lower() != contract.lower():
                continue
            if record["signal"] != signal:
                continue
            if record["sender_policy"] == "exact":
                if not sender or record["expected_sender"].lower() != sender.lower():
                    continue
            elif record["sender_policy"] == "permissionless-by-pinned-code":
                if signal not in {
                    "NewProposedOracleAddress(address,uint256)",
                    "NewOracleAddress(address,uint256)",
                }:
                    continue
            else:
                continue
            if _normalized(record["expected_args"]) != _normalized(args):
                continue
            if _normalized(record["expected_post_state"]) != _normalized(post_state):
                continue
            if not (_moment(record["valid_from"]) <= moment <= _moment(record["valid_until"])):
                continue
            if not self._manifest_matches(record["release_manifest_sha256"]):
                continue
            return record
        return None

    def _manifest_matches(self, digest):
        path = self.manifests_dir / (digest + ".json")
        try:
            content = path.read_bytes()
        except OSError:
            return False
        return hashlib.sha256(content).hexdigest() == digest


def _validate_record(record):
    if record["chain_id"] != 1:
        raise ApprovalError("approved change must target chain ID 1")
    if record["sender_policy"] not in {"exact", "permissionless-by-pinned-code"}:
        raise ApprovalError("approved-change sender policy is invalid")
    if record["sender_policy"] == "exact" and not record["expected_sender"]:
        raise ApprovalError("exact sender policy requires expected_sender")
    if (
        record["sender_policy"] == "permissionless-by-pinned-code"
        and record["expected_sender"] is not None
    ):
        raise ApprovalError("permissionless sender policy requires a null sender")
    if not isinstance(record["expected_args"], dict) or not record["expected_args"]:
        raise ApprovalError("approved-change expected arguments cannot be empty")
    if not isinstance(record["expected_post_state"], dict) or not record[
        "expected_post_state"
    ]:
        raise ApprovalError("approved-change post-state cannot be empty")
    if not SHA256_RE.fullmatch(str(record["release_manifest_sha256"])):
        raise ApprovalError("approved-change manifest digest is invalid")
    start = _moment(record["valid_from"])
    end = _moment(record["valid_until"])
    if end <= start:
        raise ApprovalError("approved-change time window is invalid")


def _moment(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise ApprovalError("approved-change time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApprovalError("approved-change time is invalid") from error
    if parsed.tzinfo is None:
        raise ApprovalError("approved-change time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _normalized(value):
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, str) and value.startswith("0x"):
        return value.lower()
    return value
