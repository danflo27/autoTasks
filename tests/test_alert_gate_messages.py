from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from tellor_alert_gate.models import ChainPoint, Finding  # noqa: E402


BLOCK_HASH = "0x" + "ab" * 32
CONTRACT = "0x" + "12" * 20
TRANSACTION_HASH = "0x" + "34" * 32
FIRST_SEEN = "2026-01-02T03:04:05.000000Z"

ROOT_ACTION_LINE = (
    "- tellormaster-control: Stop bridge and issuance automation, verify the "
    "controlling action, and inspect the target bytecode before another transaction."
)
BRIDGE_ACTION_LINE = (
    "- bridge-control: Stop the affected bridge automation and compare the action "
    "with the reviewed release manifest."
)
DATA_BRIDGE_ACTION_LINE = (
    "- databridge-integrity: Stop bridge relaying, preserve calldata and signatures, "
    "and compare the Tellor Layer validator set."
)
BRIDGE_LEDGER_ACTION_LINE = (
    "- bridge-ledger-integrity: Stop the affected relayer or claim path and preserve "
    "both chain records."
)
FLEX_VALUE_ACTION_LINE = (
    "- tellorflex-value-integrity: Preserve the query, value, and reference evidence "
    "and prepare a dispute review."
)
DATABANK_ACTION_LINE = (
    "- databank-value-integrity: Stop consumers from accepting the affected DataBank "
    "value and preserve the attestation and reference evidence."
)
GOVERNANCE_ACTION_LINE = (
    "- governance-dispute: Identify consumers of the query and timestamp, preserve "
    "the disputed value, and review the dispute evidence and voting deadline."
)
ISSUANCE_ACTION_LINE = (
    "- issuance-integrity: Stop bridge and issuance automation, preserve the "
    "transaction or block evidence, and reconcile the implementation version."
)

BOUNDARY_RELATED_SLUGS = (
    "bridge-control",
    "databridge-integrity",
    "bridge-ledger-integrity",
    "tellorflex-value-integrity",
    "databank-value-integrity",
    "governance-dispute",
    "issuance-integrity",
)


class FindingMessageTests(unittest.TestCase):
    @staticmethod
    def _finding(
        slug="tellormaster-control",
        predicate="owner action does not match the reviewed control",
        suffix="root",
    ):
        return Finding(
            slug=slug,
            predicate=predicate,
            expected="reviewed control owner",
            observed="unexpected control owner",
            incident_key="{}:deterministic-incident".format(suffix),
            delivery_key="{}:deterministic-delivery".format(suffix),
            chain_point=ChainPoint("ethereum", 987654, BLOCK_HASH, 1_700_000_000),
            signal="deterministic-control-signal",
            contract=CONTRACT,
            transaction_hash=TRANSACTION_HASH,
            log_index=7,
            evidence={
                "source": "local-fixture",
                "reference": "deterministic-evidence",
            },
            first_seen=FIRST_SEEN,
        )

    @staticmethod
    def _related_summary(finding):
        return {
            "monitor_id": finding.monitor_id,
            "slug": finding.slug,
            "severity": finding.severity,
            "predicate": finding.predicate,
        }

    def test_normal_message_uses_slug_header_and_required_evidence_labels(self):
        finding = self._finding()

        message = finding.message()
        lines = message.splitlines()

        self.assertEqual(lines[0], "**tellormaster-control**")
        self.assertNotIn("[P0]", lines[0])
        self.assertNotIn("M1", lines[0])
        self.assertNotIn("First seen:", message)

        for marker in (
            "Failed predicate:",
            "Chain:",
            "Finalized block:",
            "Contract:",
            "Signal:",
            "Transaction:",
            "Expected:",
            "Observed:",
            "Incident:",
            "Evidence:",
            "First action:",
        ):
            self.assertIn(marker, message)

    def test_normal_message_includes_complete_related_slug_and_action_text(self):
        finding = self._finding()
        related = self._finding(
            slug="bridge-control",
            predicate="bridge control predicate remains deterministic and unresolved",
            suffix="normal-related",
        )
        finding.related_findings = [self._related_summary(related)]

        lines = finding.message().splitlines()
        correlated_start = lines.index("Correlated failed predicates:")
        self.assertEqual(
            lines[correlated_start : correlated_start + 3],
            [
                "Correlated failed predicates:",
                "- bridge-control: bridge control predicate remains deterministic and unresolved",
                "  First action: Stop the affected bridge automation and compare the action with the reviewed release manifest.",
            ],
        )

    def test_compact_message_uses_slug_labels_for_related_and_first_actions(self):
        long_predicate = "control predicate detail remains deterministic and unresolved " * 80
        self.assertGreater(len(long_predicate), 2_000)
        finding = self._finding(predicate=long_predicate)
        related = self._finding(
            slug="bridge-control",
            predicate=(
                "bridge control predicate remains deterministic and unresolved " * 3
            ),
            suffix="related",
        )
        finding.related_findings = [self._related_summary(related)]

        message = finding.message()

        self.assertLessEqual(len(message), 2_000)
        self.assertEqual(message.splitlines()[0], "**tellormaster-control**")
        self.assertNotIn("[P0]", message)
        self.assertNotIn("M1", message)
        self.assertIn("Full evidence: retained in the local incident ledger", message)

        lines = message.splitlines()
        correlated_start = lines.index("Correlated failed predicates:") + 1
        first_actions_start = lines.index("First actions:")
        correlated_lines = lines[correlated_start:first_actions_start]
        self.assertEqual(len(correlated_lines), 1)
        self.assertEqual(
            correlated_lines[0],
            "- bridge-control: bridge control predicate remains deterministic and "
            "unresolved bridge control predicate remains dete…",
        )
        self.assertNotIn("P0", correlated_lines[0])
        self.assertNotIn("M2", correlated_lines[0])
        self.assertEqual(
            lines[1],
            "Failed predicate: control predicate detail remains deterministic and "
            "unresolved control predicate detail remains deterministic and unresolved "
            "control predicate detail remains deterministic and unre…",
        )

        evidence_marker = "Full evidence: retained in the local incident ledger"
        evidence_start = lines.index(evidence_marker)
        first_action_lines = lines[first_actions_start + 1 : evidence_start]
        self.assertEqual(len(first_action_lines), 2)
        self.assertEqual(first_action_lines, [ROOT_ACTION_LINE, BRIDGE_ACTION_LINE])

    def test_compact_message_keeps_complete_tail_at_boundary(self):
        finding = self._finding(predicate="root predicate detail " * 80)
        finding.related_findings = [
            self._related_summary(
                self._finding(
                    slug=slug,
                    predicate="related predicate detail remains deterministic " * 30,
                    suffix="boundary-{}".format(index),
                )
            )
            for index, slug in enumerate(BOUNDARY_RELATED_SLUGS)
        ]

        expected_action_lines = [
            ROOT_ACTION_LINE,
            BRIDGE_ACTION_LINE,
            DATA_BRIDGE_ACTION_LINE,
            BRIDGE_LEDGER_ACTION_LINE,
            FLEX_VALUE_ACTION_LINE,
            DATABANK_ACTION_LINE,
            GOVERNANCE_ACTION_LINE,
            ISSUANCE_ACTION_LINE,
        ]
        expected_evidence_line = (
            "Full evidence: retained in the local incident ledger"
        )
        unbounded_compact_lines = [
            "**tellormaster-control**",
            "Failed predicate: root predicate detail root predicate detail root predicate detail root predicate detail root predicate detail root predicate detail root predicate detail root predicate detail roo…",
            "Chain: ethereum",
            "Finalized block: 987654 ({})".format(BLOCK_HASH),
            "Transaction: {}".format(TRANSACTION_HASH),
            "Incident: root:deterministic-incident",
            "Correlated failed predicates:",
        ]
        unbounded_compact_lines.extend(
            "- {}: {}".format(
                slug,
                "related predicate detail remains deterministic related predicate detail remains deterministic relat…",
            )
            for slug in BOUNDARY_RELATED_SLUGS
        )
        unbounded_compact_lines.extend(
            ["First actions:"] + expected_action_lines + [expected_evidence_line]
        )
        self.assertGreater(len("\n".join(unbounded_compact_lines)), 2_000)

        message = finding.message()
        lines = message.splitlines()
        self.assertLessEqual(len(message), 2_000)
        self.assertEqual(lines[-1], expected_evidence_line)
        first_actions_start = lines.index("First actions:")
        evidence_start = lines.index(expected_evidence_line)
        self.assertEqual(
            lines[first_actions_start + 1 : evidence_start], expected_action_lines
        )

    def test_finding_identity_remains_internal_and_serialized(self):
        finding = self._finding()

        self.assertEqual(finding.monitor_id, "M1")
        self.assertEqual(finding.severity, "P0")
        self.assertEqual(finding.as_dict()["monitor_id"], "M1")
        self.assertEqual(finding.as_dict()["severity"], "P0")


if __name__ == "__main__":
    unittest.main()
