"""Durable alert-gate orchestration for the eleven production monitors."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import threading
import time

from .approvals import ApprovalRegistry
from .bridge_seed import EthereumDepositBackfiller, read_bridge_ledger_seed
from .config import Settings
from .constants import (
    EVM_SENSOR_SLUGS,
    MONITORS,
    TELLOR_DATA_BANK,
    TELLOR_FLEX,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V1,
    TOKEN_BRIDGE_V2,
)
from .control import evaluate_bridge_controls, evaluate_master_controls
from .databridge import (
    EnrollmentRegistry,
    evaluate_stale_databridge,
    evaluate_validator_event,
)
from .delivery import DeliveryClient, DeliveryError, publish
from .ethereum import (
    call_data,
    decode_call,
    decode_master_control_function,
    decode_receipt_events,
    monitor_evm,
    normalize_address,
    receipt_chain_point,
)
from .freshness import evaluate_ampl_day, evaluate_eth_usd, evaluate_uspce_month
from .issuance import (
    LayerMintCursor,
    evaluate_ethereum_issuance,
    evaluate_layer_issuance,
    read_layer_mint_seed,
)
from .layer import LayerIngestor, successful_event_rows
from .ledger import (
    evaluate_bridge_aggregate,
    evaluate_bridge_transaction,
    evaluate_databank_event,
    evaluate_evm_disputes,
    evaluate_layer_bridge_event,
    evaluate_layer_dispute,
)
from .models import ChainPoint, Finding, Unresolved, utc_text
from .rpc import JsonRpcClient, LayerClient
from .state import StateStore
from .values import evaluate_new_report


LOGGER = logging.getLogger("tellor_alert_gate")


def _run_stage(name, fn, *args, **kwargs):
    """Run one run_once() stage in isolation.

    A stage failure here (an unrelated RPC hiccup, a transient network
    error, anything) must not prevent later stages -- in particular the
    M9-M11 absence checks in process_scheduled() and the M3 check in
    process_stale_databridges() -- from running. Those checks exist
    specifically to detect "nothing happened when something should have",
    so silently skipping them because an earlier, unrelated stage raised
    would make "no alert" indistinguishable from "everything is fine",
    which is exactly the failure mode they exist to catch.

    Returns True if the stage completed without raising, False if it
    failed (in which case the failure has already been logged with the
    stage name for identification).
    """
    try:
        fn(*args, **kwargs)
        return True
    except Exception:
        LOGGER.exception(
            "alert-gate stage %r failed; continuing with remaining stages", name
        )
        return False


class AlertGate:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = StateStore(settings.state_path)
        self.ethereum = JsonRpcClient(
            settings.ethereum_primary_name, settings.ethereum_primary_url
        )
        self.ethereum_secondary = JsonRpcClient(
            settings.ethereum_secondary_name, settings.ethereum_secondary_url
        )
        self.layer = LayerClient("tellor-layer", settings.layer_url)
        self.approvals = ApprovalRegistry.load(
            settings.approved_changes_file, settings.release_manifests_dir
        )
        self.enrollments = EnrollmentRegistry.load(
            settings.enrolled_databridges_file
        )
        self.bridge_seed = read_bridge_ledger_seed(
            settings.bridge_ledger_seed_file
        )
        if (
            self.bridge_seed.layer_checkpoint_height + 1
            != settings.layer_start_height
        ):
            raise ValueError(
                "M4 Layer checkpoint must immediately precede replay start"
            )
        self.store.import_bridge_seed(self.bridge_seed)
        self.delivery = DeliveryClient(
            settings.delivery_mode,
            settings.routes_file,
            settings.alerts_log_path,
        )
        self.delivery.validate_routes()
        self.source_clients = {
            1: self.ethereum,
            **{
                chain_id: JsonRpcClient("evm-call-{}".format(chain_id), url)
                for chain_id, url in settings.evm_call_urls.items()
                if chain_id != 1
            },
        }
        self.layer_ingestor = LayerIngestor(
            self.layer, self.store, settings.layer_start_height
        )
        self.deposit_backfiller = EthereumDepositBackfiller(
            self.ethereum, self.store, self.bridge_seed
        )
        self.layer_cursor = self._load_layer_cursor()
        if settings.delivery_mode == "live":
            confirmed = max(
                self.bridge_seed.ethereum_checkpoint_number,
                self.ethereum.head_number() - 13,
            )
            self.store.assert_live_ready(
                seed_digest=self.bridge_seed.digest,
                ethereum_confirmed_block=confirmed,
                latest_layer_height=self.layer_ingestor.latest_height(),
            )
        self.stop_event = threading.Event()

    def close(self):
        self.store.close()

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once(run_scheduled=self._scheduled_due())
            except Exception:
                LOGGER.exception("alert-gate cycle failed; no notification was sent for the error")
            self.stop_event.wait(self.settings.poll_interval_seconds)

    def run_once(self, *, run_scheduled=True):
        # Each stage is isolated: a failure here is logged and swallowed so
        # it cannot prevent later stages -- especially the scheduled
        # absence checks below -- from running this cycle. See _run_stage.
        _run_stage("ingest_spool", self.store.ingest_spool, self.settings.spool_path)
        _run_stage("deposit_backfill", self.deposit_backfiller.ingest_available)
        _run_stage("process_matches", self.process_matches)
        _run_stage("layer_ingest", self.layer_ingestor.ingest_available, limit=100)
        _run_stage("process_layer_blocks", self.process_layer_blocks, limit=100)
        if run_scheduled:
            scheduled_ok = _run_stage("process_scheduled", self.process_scheduled)
            stale_ok = _run_stage(
                "process_stale_databridges", self.process_stale_databridges
            )
            # Only record the scheduled pass as complete if both scheduled
            # stages actually ran successfully. If either failed,
            # last_scheduled_minute stays unset/stale, so _scheduled_due()
            # keeps requesting a scheduled pass on every subsequent poll
            # (rather than waiting a full interval) until the M9-M11 and M3
            # checks actually get to run. process_scheduled() is idempotent
            # per period, so re-running it after a stale_ok failure is safe.
            if scheduled_ok and stale_ok:
                self.store.set_meta("last_scheduled_minute", int(time.time()) // 60)

    def process_matches(self, limit=25):
        for row in self.store.pending_matches(limit=limit):
            try:
                envelope = json.loads(row["raw_json"])
                self._evaluate_match(envelope)
            except Unresolved as error:
                self.store.retry_match(row["record_hash"], error)
                continue
            except DeliveryError as error:
                self.store.retry_match(row["record_hash"], error)
                continue
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                # A malformed local spool record is not chain evidence. Retain it in
                # the queue row and complete it without any external notification.
                self.store.add_evidence(
                    "invalid-spool:{}".format(row["record_hash"]),
                    "tellormaster-control",
                    {"record_hash": row["record_hash"], "reason": str(error)[:500]},
                )
            except Exception as error:
                LOGGER.exception("transaction evaluation failed")
                self.store.retry_match(row["record_hash"], error)
                continue
            self.store.complete_match(row["record_hash"])

    def _evaluate_match(self, envelope):
        if envelope.get("schema_version") != 1:
            raise ValueError("unsupported spool schema")
        monitor, transaction_hash, _evm = monitor_evm(envelope)
        if monitor not in EVM_SENSOR_SLUGS:
            raise ValueError("spooled monitor is not in the EVM sensor catalog")
        transaction = self.ethereum.transaction(transaction_hash)
        receipt = self.ethereum.receipt(transaction_hash)
        if str(transaction.get("hash", "")).lower() != transaction_hash:
            raise Unresolved("transaction hash readback differs")
        if str(receipt.get("transactionHash", "")).lower() != transaction_hash:
            raise Unresolved("receipt transaction hash differs")
        point = receipt_chain_point(self.ethereum, receipt, confirmations=12)
        if (
            str(transaction.get("blockHash", "")).lower() != point.block_hash
            or int(transaction.get("blockNumber", "0x0"), 16) != point.number
        ):
            raise Unresolved("transaction and receipt block identities differ")
        events = decode_receipt_events(
            receipt, databridge_addresses=self.enrollments.contracts
        )
        self.store.record_evm_events(events)
        functions = decode_master_control_function(transaction)
        findings = []

        if functions or any(
            event.address == TELLOR_MASTER
            and event.name
            in {"NewTellorAddress", "NewProposedOracleAddress", "NewOracleAddress"}
            for event in events
        ):
            findings.extend(
                evaluate_master_controls(
                    events,
                    functions,
                    transaction,
                    point,
                    self.ethereum,
                    self.approvals,
                )
            )

        findings.extend(
            evaluate_bridge_controls(
                events,
                transaction,
                point,
                self.ethereum,
                self.layer,
                self.approvals,
                self.enrollments,
                self.store,
                second_delay=self.settings.second_read_delay_seconds,
                sleep=time.sleep,
            )
        )
        for event in events:
            if event.name in {"ValidatorSetUpdated", "GuardianResetValidatorSet"}:
                finding = evaluate_validator_event(
                    event,
                    point,
                    self.ethereum,
                    self.layer,
                    second_delay=self.settings.second_read_delay_seconds,
                    sleep=time.sleep,
                    domain=bytes.fromhex(
                        self.enrollments.record(event.address)[
                            "validator_set_hash_domain_separator"
                        ][2:]
                    ),
                )
                if finding:
                    findings.append(finding)

        findings.extend(
            evaluate_bridge_transaction(
                events, point, self.ethereum, self.store, transaction
            )
        )
        for event in events:
            if event.address == TELLOR_FLEX and event.name == "NewReport":
                finding = evaluate_new_report(event, point, self.source_clients)
                if finding:
                    findings.append(finding)
            elif event.address == TELLOR_DATA_BANK and event.name == "OracleUpdated":
                finding = evaluate_databank_event(
                    event,
                    point,
                    self.ethereum,
                    self.layer,
                    second_delay=self.settings.second_read_delay_seconds,
                    sleep=time.sleep,
                )
                if finding:
                    findings.append(finding)
        findings.extend(evaluate_evm_disputes(events, point))
        issuance, successful_oracle = evaluate_ethereum_issuance(
            events, transaction, point, self.ethereum
        )
        if issuance:
            findings.append(issuance)
        if successful_oracle:
            for row in self.store.open_incidents_with_prefix(
                "v2-mint-to-oracle-failed:"
            ):
                self.store.close_incident(row["incident_key"])

        findings = self._suppress_existing_causes(findings)
        if findings:
            self._publish_correlated(findings)

    def process_layer_blocks(self, limit=100):
        evaluated = int(
            self.store.get_meta(
                "layer_evaluated_height", self.settings.layer_start_height - 1
            )
        )
        for block in self.store.layer_blocks_after(evaluated, limit=limit):
            height = int(block["height"])
            if height != self.layer_cursor.previous_height + 1:
                raise Unresolved("Layer evaluator cursor is not consecutive")
            if self.layer_cursor.previous_height:
                raw_block = json.loads(block["raw_block_json"])
                last_hash = (
                    raw_block["result"]["block"]["header"].get("last_block_id") or {}
                ).get("hash", "").upper()
                if last_hash != self.layer_cursor.previous_block_hash:
                    raise Unresolved("Layer minter seed does not connect to replay")
            rows = successful_event_rows(
                block, self.store.layer_events(height=height)
            )
            findings = []
            issuance, next_cursor = evaluate_layer_issuance(
                rows, block, self.layer_cursor
            )
            if issuance:
                findings.append(issuance)
            for row in rows:
                if row["event_type"] == "aggregate_report":
                    finding = evaluate_bridge_aggregate(row, block)
                    if finding:
                        findings.append(finding)
                elif row["event_type"] in {"deposit_claimed", "tokens_withdrawn"}:
                    finding = evaluate_layer_bridge_event(
                        row, block, self.store, self.layer
                    )
                    if finding:
                        findings.append(finding)
                elif row["event_type"] == "new_dispute":
                    findings.append(evaluate_layer_dispute(row, block))
            findings = self._suppress_existing_causes(findings)
            for finding in findings:
                publish(self.store, self.delivery, finding)
            self.layer_cursor = next_cursor
            self.store.set_metadata(
                {
                    "layer_mint_cursor": json.dumps(
                        next_cursor.as_json(), sort_keys=True, separators=(",", ":")
                    ),
                    "layer_evaluated_height": height,
                }
            )
            evaluated = height

    def process_scheduled(self):
        now = int(time.time())
        jobs = [
            ("tellorflex-eth-usd-freshness", evaluate_eth_usd),
            ("tellorflex-ampl-usd-deadline", evaluate_ampl_day),
            ("tellorflex-uspce-deadline", evaluate_uspce_month),
        ]
        outcomes = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for slug, evaluator in jobs:
                primary = JsonRpcClient(
                    self.settings.ethereum_primary_name,
                    self.settings.ethereum_primary_url,
                )
                secondary = JsonRpcClient(
                    self.settings.ethereum_secondary_name,
                    self.settings.ethereum_secondary_url,
                )
                future = executor.submit(
                    evaluator,
                    primary,
                    secondary,
                    second_delay=self.settings.second_read_delay_seconds,
                    sleep=time.sleep,
                    now=now,
                )
                futures[future] = slug
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    outcome = future.result()
                except Unresolved as error:
                    LOGGER.warning("%s remains unresolved: %s", slug, error)
                    continue
                if outcome is not None:
                    outcomes[slug] = outcome

        openings = []
        completed_periods = {}
        for slug, _ in jobs:
            outcome = outcomes.get(slug)
            if outcome is None:
                continue
            period_key = "scheduled_period:{}".format(slug)
            if self.store.get_meta(period_key) == outcome.period:
                continue
            if not outcome.failed:
                for incident in self.store.open_incidents_for_slug(slug):
                    self.store.close_incident(incident["incident_key"])
                self.store.set_meta(period_key, outcome.period)
                continue
            dispute = self._absorbing_dispute(outcome)
            if dispute is not None:
                self.store.add_evidence(
                    "scheduled:{}:{}".format(slug, outcome.period),
                    slug,
                    outcome.local_evidence(),
                    incident_key=dispute["incident_key"],
                )
                self.store.set_meta(period_key, outcome.period)
                continue
            existing = self.store.open_incident_for_slug(slug)
            if existing is not None:
                self.store.add_evidence(
                    "scheduled:{}:{}".format(slug, outcome.period),
                    slug,
                    outcome.local_evidence(),
                    incident_key=existing["incident_key"],
                )
                self.store.set_meta(period_key, outcome.period)
                continue
            openings.append(outcome.finding)
            completed_periods[slug] = (period_key, outcome.period)

        if openings:
            root = openings[0]
            independent = openings[1:]
            root.related_findings = [_related_summary(item) for item in independent]
            sent = publish(self.store, self.delivery, root)
            delivery = self.store.delivery(root.delivery_key)
            durable = sent or (
                delivery is not None
                and delivery["status"] in {"sent", "uncertain", "sending"}
            )
            if durable:
                if self.store.incident(root.incident_key) is None:
                    self.store.open_incident(root)
                for finding in independent:
                    self.store.open_incident(finding)
                self.store.set_metadata(
                    {
                        key: period
                        for key, period in completed_periods.values()
                    }
                )

    def process_stale_databridges(self):
        if self.ethereum.chain_id() != 1:
            raise Unresolved("M3 provider is not Ethereum mainnet")
        head = self.ethereum.head_number()
        if head < 12:
            raise Unresolved("M3 provider head is below confirmation depth")
        block = self.ethereum.block(head - 12)
        point = ChainPoint(
            "ethereum",
            int(block["number"], 16),
            str(block["hash"]).lower(),
            int(block["timestamp"], 16),
        )
        active = set()
        for contract in (TOKEN_BRIDGE_V1, TOKEN_BRIDGE_V2, TELLOR_DATA_BANK):
            address = normalize_address(
                decode_call(
                    ["address"],
                    self.ethereum.eth_call(
                        contract, call_data("dataBridge()"), point.number
                    ),
                )[0]
            )
            active.add(address)
        if not active.issubset(self.enrollments.contracts):
            raise Unresolved("an active DataBridge is not pre-enrolled")
        for address in sorted(self.enrollments.contracts):
            if address not in active:
                self._close_stale_databridge(address)
                continue
            finding = evaluate_stale_databridge(address, point, self.ethereum)
            if finding:
                publish(self.store, self.delivery, finding)
            else:
                self._close_stale_databridge(address)

    def _close_stale_databridge(self, address):
        for row in self.store.open_incidents_with_prefix("databridge-stale:"):
            try:
                context = json.loads(row["context_json"])
                contract = normalize_address(context.get("contract"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if contract == address:
                self.store.close_incident(row["incident_key"])

    def _load_layer_cursor(self):
        encoded = self.store.get_meta("layer_mint_cursor")
        if encoded is not None:
            cursor = LayerMintCursor.from_json(json.loads(encoded))
        else:
            cursor = read_layer_mint_seed(
                self.settings.layer_minter_seed_file,
                self.settings.layer_start_height,
            )
        evaluated = int(
            self.store.get_meta(
                "layer_evaluated_height", self.settings.layer_start_height - 1
            )
        )
        if cursor.previous_height != evaluated:
            raise ValueError("Layer minter cursor and evaluated height differ")
        if encoded is None and (
            cursor.previous_height != self.bridge_seed.layer_checkpoint_height
            or cursor.previous_block_hash
            != self.bridge_seed.layer_checkpoint_hash
            or cursor.previous_block_time_ms
            != self.bridge_seed.layer_checkpoint_time_ms
        ):
            raise ValueError("M4 and M8 Layer checkpoints do not identify one block")
        return cursor

    def _suppress_existing_causes(self, findings):
        kept = []
        current_disputes = [
            finding for finding in findings if finding.slug == "governance-dispute"
        ]
        for finding in findings:
            if finding.slug == "bridge-control" and finding.incident_key.startswith(
                "v2-mint-to-oracle-failed:"
            ):
                existing = self.store.open_incidents_with_prefix(
                    "v2-mint-to-oracle-failed:"
                )
                if existing:
                    self.store.add_evidence(
                        finding.delivery_key,
                        finding.slug,
                        finding.as_dict(),
                        incident_key=existing[0]["incident_key"],
                    )
                    continue
            if finding.slug in {
                "tellorflex-value-integrity",
                "databank-value-integrity",
            }:
                evidence = finding.evidence or {}
                query_id = evidence.get("query_id")
                timestamp = evidence.get("report_timestamp")
                current_dispute = next(
                    (
                        item
                        for item in current_disputes
                        if _same_query_time(item, query_id, timestamp)
                    ),
                    None,
                )
                dispute = current_dispute or (
                    self.store.open_dispute(query_id, timestamp)
                    if query_id is not None
                    else None
                )
                if current_dispute is not None:
                    kept.append(finding)
                    continue
                if dispute is not None:
                    incident_key = dispute["incident_key"]
                    self.store.add_evidence(
                        finding.delivery_key,
                        finding.slug,
                        finding.as_dict(),
                        incident_key=incident_key,
                    )
                    continue
                if finding.slug == "databank-value-integrity":
                    copied = self.store.matching_open_incident(
                        "tellorflex-value-integrity",
                        query_id,
                        timestamp,
                        evidence.get("value"),
                    )
                    if copied is not None:
                        self.store.add_evidence(
                            finding.delivery_key,
                            finding.slug,
                            finding.as_dict(),
                            incident_key=copied["incident_key"],
                        )
                        continue
            kept.append(finding)
        return kept

    def _publish_correlated(self, findings):
        unique = []
        seen = set()
        for finding in findings:
            if finding.incident_key not in seen:
                unique.append(finding)
                seen.add(finding.incident_key)
        order = {
            "tellormaster-control": 0,
            "bridge-control": 1,
            "databridge-integrity": 2,
            "governance-dispute": 3,
            "tellorflex-value-integrity": 4,
            "databank-value-integrity": 5,
            "bridge-ledger-integrity": 6,
            "issuance-integrity": 7,
        }
        unique.sort(key=lambda item: (order.get(item.slug, 99), item.log_index or -1))
        root = unique[0]
        absorbed = []
        independent = []
        for finding in unique[1:]:
            if root.slug in {"tellormaster-control", "bridge-control"}:
                absorbed.append(finding)
            elif root.slug == "databridge-integrity" and finding.slug in {
                "bridge-ledger-integrity",
                "databank-value-integrity",
            }:
                absorbed.append(finding)
            elif root.slug == "governance-dispute" and finding.slug in {
                "tellorflex-value-integrity",
                "databank-value-integrity",
            } and _same_query_time(
                root,
                finding.evidence.get("query_id"),
                finding.evidence.get("report_timestamp"),
            ):
                absorbed.append(finding)
            else:
                independent.append(finding)
        root.related_findings = [
            _related_summary(item) for item in absorbed + independent
        ]
        if absorbed:
            root.evidence = {
                **(root.evidence or {}),
                "absorbed_findings": [item.as_dict() for item in absorbed],
            }
        sent = publish(self.store, self.delivery, root)
        delivery = self.store.delivery(root.delivery_key)
        durable = sent or (
            delivery is not None
            and delivery["status"] in {"sent", "uncertain", "sending"}
        )
        if not durable:
            return
        if self.store.incident(root.incident_key) is None:
            self.store.open_incident(root)
        for item in absorbed:
            self.store.add_evidence(
                item.delivery_key,
                item.slug,
                item.as_dict(),
                incident_key=root.incident_key,
            )
        for item in independent:
            self.store.open_incident(item)

    def _absorbing_dispute(self, outcome):
        for row in self.store.open_incidents_for_slug("governance-dispute"):
            try:
                context = json.loads(row["context_json"])
                evidence = context.get("evidence") or {}
                query_id = str(evidence.get("query_id", "")).lower()
                timestamp = int(evidence["report_timestamp"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if query_id != outcome.query_id.lower():
                continue
            if outcome.slug == "tellorflex-eth-usd-freshness":
                if (
                    timestamp <= outcome.observation.block_timestamp
                    and outcome.observation.block_timestamp - timestamp <= 14_400
                ):
                    return row
            elif outcome.window_start <= timestamp <= outcome.window_end:
                return row
        return None

    def _scheduled_due(self):
        current = int(time.time()) // 60
        return self.store.get_meta("last_scheduled_minute") != str(current)


def _same_query_time(finding, query_id, timestamp):
    evidence = finding.evidence or {}
    try:
        return (
            str(evidence.get("query_id", "")).lower() == str(query_id).lower()
            and int(evidence.get("report_timestamp", -1)) == int(timestamp)
        )
    except (TypeError, ValueError):
        return False


def _related_summary(finding):
    return {
        "monitor_id": finding.monitor_id,
        "slug": finding.slug,
        "severity": finding.severity,
        "predicate": finding.predicate,
        "incident_key": finding.incident_key,
        "expected": finding.expected,
        "observed": finding.observed,
    }
