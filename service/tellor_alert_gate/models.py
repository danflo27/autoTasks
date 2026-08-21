"""Shared typed records for evidence, unresolved checks, and findings."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional

from .constants import FIRST_ACTIONS, MONITORS


def utc_now():
    return datetime.now(timezone.utc)


def utc_text(value=None):
    moment = value or utc_now()
    # Always emit microseconds. isoformat() drops them when they happen to be
    # exactly zero, which makes the text sort wrong: "...:00Z" would compare
    # greater than "...:00.000001Z" because "." < "Z". state.py compares and
    # orders these timestamps as text (reserved_at, next_attempt_at,
    # inserted_at, opened_at), so a fixed width keeps that ordering sound.
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class Unresolved(RuntimeError):
    """Evidence is incomplete or ambiguous and must remain silent."""


@dataclass(frozen=True)
class ChainPoint:
    chain: str
    number: int
    block_hash: str
    timestamp: int


@dataclass(frozen=True)
class DecodedEvent:
    address: str
    name: str
    signature: str
    args: Dict[str, Any]
    transaction_hash: str
    log_index: int
    block_number: int
    block_hash: str


@dataclass
class Finding:
    slug: str
    predicate: str
    expected: Any
    observed: Any
    incident_key: str
    delivery_key: str
    chain_point: ChainPoint
    signal: str
    contract: str = "n/a"
    transaction_hash: Optional[str] = None
    log_index: Optional[int] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    first_seen: str = field(default_factory=utc_text)
    severity_override: Optional[str] = None
    related_findings: list = field(default_factory=list)

    @property
    def monitor_id(self):
        return MONITORS[self.slug][0]

    @property
    def severity(self):
        return self.severity_override or MONITORS[self.slug][1]

    def message(self):
        point_label = "block" if self.chain_point.chain == "ethereum" else "height"
        lines = [
            "**{}**".format(self.slug),
            "Failed predicate: {}".format(self.predicate),
            "Chain: {}".format(self.chain_point.chain),
            "Finalized {}: {} ({})".format(
                point_label, self.chain_point.number, self.chain_point.block_hash
            ),
            "Contract: {}".format(self.contract),
            "Signal: {}".format(self.signal),
        ]
        if self.transaction_hash:
            lines.append("Transaction: {}".format(self.transaction_hash))
        lines.extend(
            [
                "Expected: {}".format(_compact(self.expected)),
                "Observed: {}".format(_compact(self.observed)),
                "Incident: {}".format(self.incident_key),
            ]
        )
        if self.chain_point.chain == "ethereum" and self.transaction_hash:
            lines.append(
                "Evidence: https://etherscan.io/tx/{}".format(self.transaction_hash)
            )
        elif self.evidence:
            lines.append("Evidence: {}".format(_compact(self.evidence)))
        if self.related_findings:
            lines.append("Correlated failed predicates:")
            for related in self.related_findings:
                lines.append(
                    "- {}: {}".format(
                        related["slug"],
                        related["predicate"],
                    )
                )
                lines.append(
                    "  First action: {}".format(FIRST_ACTIONS[related["slug"]])
                )
        lines.append("First action: {}".format(FIRST_ACTIONS[self.slug]))
        message = "\n".join(lines)
        if len(message) > 2000:
            message = self._compact_message()
        return message

    def _compact_message(self):
        point_label = "block" if self.chain_point.chain == "ethereum" else "height"
        prefix_lines = [
            "**{}**".format(self.slug),
            "Failed predicate: {}".format(_clip(self.predicate, 180)),
            "Chain: {}".format(self.chain_point.chain),
            "Finalized {}: {} ({})".format(
                point_label,
                self.chain_point.number,
                self.chain_point.block_hash,
            ),
        ]
        if self.transaction_hash:
            prefix_lines.append("Transaction: {}".format(self.transaction_hash))
        prefix_lines.append("Incident: {}".format(self.incident_key))
        if self.related_findings:
            prefix_lines.append("Correlated failed predicates:")
            for related in self.related_findings:
                prefix_lines.append(
                    "- {}: {}".format(
                        related["slug"],
                        _clip(related["predicate"], 100),
                    )
                )
        tail_lines = ["First actions:"]
        tail_lines.append(
            "- {}: {}".format(self.slug, _clip(FIRST_ACTIONS[self.slug], 160))
        )
        seen = {self.slug}
        for related in self.related_findings:
            if related["slug"] in seen:
                continue
            seen.add(related["slug"])
            tail_lines.append(
                "- {}: {}".format(
                    related["slug"],
                    _clip(FIRST_ACTIONS[related["slug"]], 160),
                )
            )
        tail_lines.append("Full evidence: retained in the local incident ledger")

        tail_length = len("\n".join(tail_lines))
        prefix_budget = max(0, 2000 - tail_length - 1)
        selected_prefix = []
        selected_length = 0
        for line in prefix_lines:
            line_length = len(line) + (1 if selected_prefix else 0)
            if selected_length + line_length > prefix_budget:
                break
            selected_prefix.append(line)
            selected_length += line_length

        return "\n".join(selected_prefix + tail_lines)

    def as_dict(self):
        return {
            "monitor_id": self.monitor_id,
            "slug": self.slug,
            "severity": self.severity,
            "predicate": self.predicate,
            "expected": self.expected,
            "observed": self.observed,
            "incident_key": self.incident_key,
            "delivery_key": self.delivery_key,
            "chain_point": self.chain_point.__dict__,
            "signal": self.signal,
            "contract": self.contract,
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "evidence": self.evidence,
            "first_seen": self.first_seen,
            "related_findings": self.related_findings,
        }


def _compact(value):
    if isinstance(value, str):
        return value.replace("\r", "\\r").replace("\n", "\\n")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _clip(value, limit):
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
