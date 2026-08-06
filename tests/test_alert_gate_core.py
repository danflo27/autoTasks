from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

from eth_abi import encode
from eth_utils import keccak


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from tellor_alert_gate.constants import (  # noqa: E402
    ETH_USD_QUERY_ID,
    M6_SPOT_QUERY_IDS,
    TELLOR_DATA_BANK,
    TELLOR_DATA_BRIDGE,
    TELLOR_FLEX,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V2,
    VALIDATOR_DOMAIN,
    ZERO_ADDRESS,
)
from tellor_alert_gate.control import (  # noqa: E402
    evaluate_bridge_controls,
    evaluate_master_controls,
)
from tellor_alert_gate.bridge_seed import (  # noqa: E402
    EthereumDepositBackfiller,
    read_bridge_ledger_seed,
)
from tellor_alert_gate.databridge import (  # noqa: E402
    EnrollmentError,
    EnrollmentRegistry,
    compute_checkpoint,
    evaluate_stale_databridge,
    evaluate_validator_event,
)
from tellor_alert_gate.delivery import (  # noqa: E402
    STALE_SENDING_RESERVATION_SECONDS,
    DeliveryClient,
    publish,
)
from tellor_alert_gate.ethereum import (  # noqa: E402
    EVENT_SPECS,
    call_data,
    decode_receipt_events,
    function_selector,
    hx,
    unhex,
)
from tellor_alert_gate.issuance import (  # noqa: E402
    DAILY_RELEASE_WEI,
    LayerMintCursor,
    evaluate_ethereum_issuance,
    evaluate_layer_issuance,
)
from tellor_alert_gate.ledger import (  # noqa: E402
    _withdraw_attestation,
    bridge_query_id,
    evaluate_bridge_aggregate,
    evaluate_databank_event,
    evaluate_evm_disputes,
    evaluate_layer_dispute,
)
from tellor_alert_gate.layer import successful_event_rows  # noqa: E402
from tellor_alert_gate.models import (  # noqa: E402
    ChainPoint,
    DecodedEvent,
    Finding,
    Unresolved,
    utc_now,
    utc_text,
)
from tellor_alert_gate.service import AlertGate  # noqa: E402
from tellor_alert_gate.state import StateStore  # noqa: E402
from tellor_alert_gate.values import evaluate_new_report  # noqa: E402


TX_HASH = "0x" + "ab" * 32
BLOCK_HASH = "0x" + "cd" * 32
ORACLE = "0x" + "11" * 20
OWNER = "0x" + "22" * 20
CALLER = "0x" + "33" * 20
OLD_TELLOR = "0x" + "44" * 20


class MappingEthereum:
    def __init__(self, trace):
        self.trace_value = trace
        self.answers = {}

    def add(self, to, data, block, types, values):
        self.answers[(to.lower(), data.lower(), block)] = hx(encode(types, values))

    def eth_call(self, to, data, block="latest", extra=None):
        return self.answers[(to.lower(), data.lower(), block)]

    def trace(self, _transaction_hash):
        return self.trace_value


def transfer(to, amount, index, source=ZERO_ADDRESS):
    return DecodedEvent(
        address=TELLOR_MASTER,
        name="Transfer",
        signature="Transfer(address,address,uint256)",
        args={"_from": source, "_to": to, "_value": amount},
        transaction_hash=TX_HASH,
        log_index=index,
        block_number=10,
        block_hash=BLOCK_HASH,
    )


def add_uint_var(client, key, block, value):
    client.add(
        TELLOR_MASTER,
        call_data("getUintVar(bytes32)", ["bytes32"], [keccak(text=key)]),
        block,
        ["uint256"],
        [value],
    )


def add_address_var(client, key, block, value):
    client.add(
        TELLOR_MASTER,
        call_data("getAddressVars(bytes32)", ["bytes32"], [keccak(text=key)]),
        block,
        ["address"],
        [value],
    )


def add_supply(client, before, delta):
    client.add(TELLOR_MASTER, call_data("totalSupply()"), 9, ["uint256"], [before])
    client.add(
        TELLOR_MASTER,
        call_data("totalSupply()"),
        10,
        ["uint256"],
        [before + delta],
    )


class AlertGateCoreTests(unittest.TestCase):
    def test_two_independent_disputes_in_one_transaction_are_both_retained(self):
        events = []
        for index, byte in enumerate(("01", "02"), 1):
            events.append(
                DecodedEvent(
                    address="0xb30b1b98d8276b80bc4f5af9f9170ef3220ec27d",
                    name="NewDispute",
                    signature="NewDispute(uint256,bytes32,uint256,address)",
                    args={
                        "_disputeId": index,
                        "_queryId": "0x" + byte * 32,
                        "_timestamp": 100 + index,
                        "_reporter": CALLER,
                    },
                    transaction_hash=TX_HASH,
                    log_index=index,
                    block_number=10,
                    block_hash=BLOCK_HASH,
                )
            )
        findings = evaluate_evm_disputes(
            events, ChainPoint("ethereum", 10, BLOCK_HASH, 1_000)
        )
        self.assertEqual(len(findings), 2)
        self.assertNotEqual(findings[0].incident_key, findings[1].incident_key)

    def test_oracle_proposal_and_acceptance_share_stored_proposal_identity(self):
        class NoApprovals:
            def exact_match(self, **_kwargs):
                return None

        client = MappingEthereum({})
        add_address_var(client, "_PROPOSED_ORACLE", 10, ORACLE)
        add_address_var(client, "_ORACLE_CONTRACT", 10, ORACLE)
        add_uint_var(client, "_TIME_PROPOSED_UPDATED", 10, 900)
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1_000)
        transaction = {"from": CALLER, "hash": TX_HASH}

        def oracle_event(name, signature, timestamp):
            argument = (
                "_newProposedOracle"
                if name == "NewProposedOracleAddress"
                else "_newOracle"
            )
            return DecodedEvent(
                address=TELLOR_MASTER,
                name=name,
                signature=signature,
                args={argument: ORACLE, "_timestamp": timestamp},
                transaction_hash=TX_HASH,
                log_index=1,
                block_number=10,
                block_hash=BLOCK_HASH,
            )

        proposal = evaluate_master_controls(
            [
                oracle_event(
                    "NewProposedOracleAddress",
                    "NewProposedOracleAddress(address,uint256)",
                    900,
                )
            ],
            [],
            transaction,
            point,
            client,
            NoApprovals(),
        )[0]
        acceptance = evaluate_master_controls(
            [
                oracle_event(
                    "NewOracleAddress",
                    "NewOracleAddress(address,uint256)",
                    1_000,
                )
            ],
            [],
            transaction,
            point,
            client,
            NoApprovals(),
        )[0]
        self.assertEqual(proposal.incident_key, acceptance.incident_key)
        self.assertTrue(proposal.incident_key.endswith(":900"))

    def test_committed_declared_bridge_aggregate_with_bad_abi_is_p0(self):
        row = {
            "height": 25,
            "event_key": "tellor-1:25:finalize_block_events:0:aggregate_report",
            "attributes_json": json.dumps(
                {
                    "query_id": "00" * 32,
                    "query_data": "not-hex",
                    "value": "00",
                    "aggregate_power": "1",
                    "micro_report_height": "24",
                    "timestamp": "1000",
                    "micro_report_type": "TRBBridgeV2",
                }
            ),
        }
        block = {
            "height": 25,
            "block_hash": "AA" * 32,
            "block_time_ms": 1_000,
        }
        finding = evaluate_bridge_aggregate(row, block)
        self.assertEqual(finding.severity, "P0")

    def test_bridge_seed_import_binds_replay_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "bridge-seed.json"
            seed_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ethereum_chain_id": 1,
                        "layer_chain_id": "tellor-1",
                        "source": "reviewed fixture",
                        "verified_at": "2026-08-06T12:00:00Z",
                        "ethereum_checkpoint": {
                            "block_number": 10,
                            "block_hash": BLOCK_HASH,
                        },
                        "layer_checkpoint": {
                            "height": 20,
                            "block_hash": "AA" * 32,
                            "block_time_ms": 2_000,
                        },
                        "pending_ethereum_deposits": [
                            {
                                "generation": "v2",
                                "block_number": 9,
                                "block_hash": "0x" + "bb" * 32,
                                "transaction_hash": TX_HASH,
                                "log_index": 3,
                                "args": {
                                    "_depositId": 7,
                                    "_sender": CALLER,
                                    "_recipient": "tellor1fixture",
                                    "_amount": 10**18,
                                    "_tip": 0,
                                },
                            }
                        ],
                        "pending_layer_events": [],
                    }
                )
            )
            seed_path.chmod(0o600)
            seed = read_bridge_ledger_seed(seed_path)
            store = StateStore(Path(directory) / "state.sqlite3")
            store.import_bridge_seed(seed)
            self.assertEqual(len(store.evm_events("Deposit", TOKEN_BRIDGE_V2)), 1)
            self.assertEqual(store.layer_block(20)["block_hash"], "AA" * 32)
            store.set_metadata(
                {
                    "layer_evaluated_height": 20,
                    "layer_next_height": 21,
                    "layer_mint_cursor": json.dumps(
                        {
                            "initialized": True,
                            "previous_height": 20,
                            "previous_block_hash": "AA" * 32,
                            "previous_block_time_ms": 2_000,
                            "previous_inflation_time_ms": 2_000,
                        }
                    ),
                }
            )
            store.assert_live_ready(
                seed_digest=seed.digest,
                ethereum_confirmed_block=10,
                latest_layer_height=21,
            )

            replay_hash = "0x" + "ee" * 32
            replay_transaction = "0x" + "99" * 32

            class BackfillClient:
                def chain_id(self):
                    return 1

                def head_number(self):
                    return 24

                def logs(self, *, address, from_block, to_block, topics=None):
                    self.assert_range = (from_block, to_block)
                    if address != TOKEN_BRIDGE_V2:
                        return []
                    return [
                        {
                            "address": TOKEN_BRIDGE_V2,
                            "topics": topics,
                            "data": hx(
                                encode(
                                    [
                                        "uint256",
                                        "address",
                                        "string",
                                        "uint256",
                                        "uint256",
                                    ],
                                    [8, CALLER, "tellor1backfill", 2 * 10**18, 0],
                                )
                            ),
                            "transactionHash": replay_transaction,
                            "logIndex": "0x4",
                            "blockNumber": "0xb",
                            "blockHash": replay_hash,
                        }
                    ]

                def block(self, number):
                    return {
                        "number": hex(number),
                        "hash": replay_hash,
                        "timestamp": "0x1",
                    }

            backfiller = EthereumDepositBackfiller(
                BackfillClient(), store, seed
            )
            self.assertEqual(backfiller.ingest_available(), 12)
            self.assertEqual(len(store.evm_events("Deposit", TOKEN_BRIDGE_V2)), 2)
            store.close()

    def test_pre_enrolled_databridge_uses_the_pinned_event_abi(self):
        replacement = "0x" + "66" * 20
        signature = "ValidatorSetUpdated(uint256,uint256,bytes32)"
        receipt = {
            "logs": [
                {
                    "address": replacement,
                    "topics": [hx(keccak(text=signature))],
                    "data": hx(
                        encode(
                            ["uint256", "uint256", "bytes32"],
                            [100, 200, b"\x77" * 32],
                        )
                    ),
                    "transactionHash": TX_HASH,
                    "logIndex": "0x1",
                    "blockNumber": "0xa",
                    "blockHash": BLOCK_HASH,
                }
            ]
        }
        events = decode_receipt_events(
            receipt, databridge_addresses={replacement}
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].address, replacement)
        self.assertEqual(events[0].name, "ValidatorSetUpdated")
        self.assertEqual(events[0].args["_validatorTimestamp"], 200)

    def test_failed_layer_transaction_events_are_persisted_but_not_evaluated(self):
        block = {
            "raw_results_json": json.dumps(
                {
                    "result": {
                        "txs_results": [
                            {"code": 0},
                            {"code": 5},
                        ]
                    }
                }
            )
        }
        rows = [
            {"source": "txs_results:0", "event_type": "new_dispute"},
            {"source": "txs_results:1", "event_type": "new_dispute"},
            {"source": "finalize_block_events", "event_type": "mint_coins"},
        ]
        self.assertEqual(
            successful_event_rows(block, rows),
            [rows[0], rows[2]],
        )

    def test_withdraw_calldata_is_decoded_canonically(self):
        identity = 17
        query_id = bytes.fromhex(
            bridge_query_id("TRBBridgeV2", False, identity)[2:]
        )
        report_value = encode(
            ["address", "string", "uint256", "uint256"],
            [CALLER, "tellor1exampleaddress", 10**18, 0],
        )
        types = [
            "(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256)",
            "(address,uint256)[]",
            "(uint8,bytes32,bytes32)[]",
            "uint256",
        ]
        values = [
            (query_id, (report_value, 100, 123, 90, 110, 95), 101),
            [(CALLER, 1)],
            [(27, b"\x01" * 32, b"\x02" * 32)],
            identity,
        ]
        signature = (
            "withdrawFromLayer("
            "(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256),"
            "(address,uint256)[],(uint8,bytes32,bytes32)[],uint256)"
        )
        transaction = {
            "hash": TX_HASH,
            "to": TOKEN_BRIDGE_V2,
            "input": hx(function_selector(signature) + encode(types, values)),
        }

        decoded = _withdraw_attestation(
            transaction,
            TOKEN_BRIDGE_V2,
            identity,
            MappingEthereum({}),
        )
        self.assertEqual(decoded["identity"], identity)
        self.assertEqual(decoded["query_id"], hx(query_id))
        self.assertEqual(decoded["report_value"], hx(report_value))
        self.assertEqual(decoded["report_power"], 123)

        transaction["input"] = "0xzz"
        with self.assertRaises(Unresolved):
            _withdraw_attestation(
                transaction,
                TOKEN_BRIDGE_V2,
                identity,
                MappingEthereum({}),
            )

    def test_log_only_delivery_is_durable_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            log = Path(directory) / "alerts.jsonl"
            client = DeliveryClient("log-only", None, log)
            finding = Finding(
                slug="issuance-integrity",
                predicate="test predicate",
                expected=1,
                observed=2,
                incident_key="issuance:test",
                delivery_key="1:test:M8",
                chain_point=ChainPoint("ethereum", 10, BLOCK_HASH, 1000),
                signal="test",
            )
            self.assertTrue(publish(store, client, finding))
            self.assertFalse(publish(store, client, finding))
            self.assertEqual(len(log.read_text().splitlines()), 1)
            self.assertEqual(store.incident("issuance:test")["status"], "open")
            store.close()

    def test_live_server_error_preserves_delivery_reservation(self):
        class ServerErrorSession:
            def __init__(self):
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return type("Response", (), {"status_code": 500})()

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            log = Path(directory) / "alerts.jsonl"
            session = ServerErrorSession()
            client = DeliveryClient("live", None, log, session=session)
            client.validate_routes = lambda: {
                "issuance-integrity": "https://discord.com/api/webhooks/1/token"
            }
            finding = Finding(
                slug="issuance-integrity",
                predicate="test predicate",
                expected=1,
                observed=2,
                incident_key="issuance:test",
                delivery_key="1:test:M8",
                chain_point=ChainPoint("ethereum", 10, BLOCK_HASH, 1000),
                signal="test",
            )

            with self.assertRaisesRegex(RuntimeError, "HTTP 500") as error:
                publish(store, client, finding)

            self.assertTrue(error.exception.ambiguous)
            self.assertEqual(session.calls, 1)
            self.assertEqual(store.delivery(finding.delivery_key)["status"], "uncertain")
            self.assertFalse(publish(store, client, finding))
            self.assertEqual(session.calls, 1)
            self.assertEqual(store.incident(finding.incident_key)["status"], "open")
            self.assertEqual(len(log.read_text().splitlines()), 1)
            store.close()

    def test_fresh_sending_reservation_is_not_retried(self):
        # A `sending` row that was reserved moments ago could still be a
        # live in-flight HTTP call. publish() must not attempt a second
        # POST for it -- that would risk a duplicate Discord message for an
        # alert that is genuinely still being delivered.
        class CountingSession:
            def __init__(self):
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return type("Response", (), {"status_code": 200})()

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            log = Path(directory) / "alerts.jsonl"
            session = CountingSession()
            client = DeliveryClient("live", None, log, session=session)
            client.validate_routes = lambda: {
                "issuance-integrity": "https://discord.com/api/webhooks/1/token"
            }
            finding = Finding(
                slug="issuance-integrity",
                predicate="test predicate",
                expected=1,
                observed=2,
                incident_key="issuance:test",
                delivery_key="1:test:M8",
                chain_point=ChainPoint("ethereum", 10, BLOCK_HASH, 1000),
                signal="test",
            )

            self.assertTrue(store.reserve_delivery(finding))
            self.assertEqual(store.delivery(finding.delivery_key)["status"], "sending")

            self.assertFalse(publish(store, client, finding))

            self.assertEqual(session.calls, 0)
            self.assertEqual(store.delivery(finding.delivery_key)["status"], "sending")
            self.assertEqual(store.incident(finding.incident_key)["status"], "open")
            store.close()

    def test_stale_sending_reservation_is_retried_and_recovers(self):
        # A `sending` row left behind by a process that died between
        # reserve_delivery() and the HTTP call completing must eventually be
        # retried -- otherwise a P0 alert is lost forever with no recovery
        # path. Once the reservation is older than
        # STALE_SENDING_RESERVATION_SECONDS (far longer than any live
        # attempt can legitimately take), publish() must treat it as
        # crashed and actually deliver it.
        class CountingSession:
            def __init__(self):
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return type("Response", (), {"status_code": 200})()

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            log = Path(directory) / "alerts.jsonl"
            session = CountingSession()
            client = DeliveryClient("live", None, log, session=session)
            client.validate_routes = lambda: {
                "issuance-integrity": "https://discord.com/api/webhooks/1/token"
            }
            finding = Finding(
                slug="issuance-integrity",
                predicate="test predicate",
                expected=1,
                observed=2,
                incident_key="issuance:test",
                delivery_key="1:test:M8",
                chain_point=ChainPoint("ethereum", 10, BLOCK_HASH, 1000),
                signal="test",
            )

            self.assertTrue(store.reserve_delivery(finding))
            # Simulate a process that reserved the delivery and then died
            # before ever contacting Discord: back-date the reservation
            # well past the staleness threshold.
            stale_timestamp = utc_text(
                utc_now() - timedelta(seconds=STALE_SENDING_RESERVATION_SECONDS + 60)
            )
            store.connection.execute(
                "UPDATE deliveries SET reserved_at=? WHERE delivery_key=?",
                (stale_timestamp, finding.delivery_key),
            )
            store.connection.commit()

            self.assertTrue(publish(store, client, finding))

            self.assertEqual(session.calls, 1)
            delivery = store.delivery(finding.delivery_key)
            self.assertEqual(delivery["status"], "sent")
            self.assertIsNotNone(delivery["delivered_at"])
            self.assertEqual(store.incident(finding.incident_key)["status"], "open")
            self.assertEqual(len(log.read_text().splitlines()), 1)
            store.close()

    def test_valid_internal_mint_to_oracle_is_silent_and_closes_failure(self):
        timestamp = 1_000
        prior = 900
        released = DAILY_RELEASE_WEI * (timestamp - prior) // 86_400
        rewards = released * 2 // 100
        trace = {
            "to": "0x" + "55" * 20,
            "input": "0x12345678",
            "calls": [
                {
                    "from": "0x" + "55" * 20,
                    "to": TELLOR_MASTER,
                    "input": hx(function_selector("mintToOracle()")),
                    "calls": [
                        {
                            "from": TELLOR_MASTER,
                            "to": ORACLE,
                            "input": call_data(
                                "addStakingRewards(uint256)",
                                ["uint256"],
                                [rewards],
                            ),
                        }
                    ],
                }
            ],
        }
        client = MappingEthereum(trace)
        add_uint_var(client, "_LAST_RELEASE_TIME_DAO", 9, prior)
        add_uint_var(client, "_LAST_RELEASE_TIME_DAO", 10, timestamp)
        add_address_var(client, "_ORACLE_CONTRACT", 9, ORACLE)
        add_supply(client, 10**24, released)
        events = [
            transfer(ORACLE, released - rewards, 1),
            transfer(TELLOR_MASTER, rewards, 2),
            transfer(ORACLE, rewards, 3, source=TELLOR_MASTER),
        ]
        finding, proved = evaluate_ethereum_issuance(
            events,
            {"hash": TX_HASH},
            ChainPoint("ethereum", 10, BLOCK_HASH, timestamp),
            client,
        )
        self.assertIsNone(finding)
        self.assertTrue(proved)

    def test_valid_mint_to_team_and_migration_are_silent(self):
        timestamp = 1_000
        prior = 900
        released = DAILY_RELEASE_WEI * (timestamp - prior) // 86_400
        team_client = MappingEthereum(
            {"from": CALLER, "to": TELLOR_MASTER, "input": hx(function_selector("mintToTeam()"))}
        )
        add_uint_var(team_client, "_LAST_RELEASE_TIME_TEAM", 9, prior)
        add_uint_var(team_client, "_LAST_RELEASE_TIME_TEAM", 10, timestamp)
        add_address_var(team_client, "_OWNER", 9, OWNER)
        add_supply(team_client, 10**24, released)
        finding, proved = evaluate_ethereum_issuance(
            [transfer(OWNER, released, 1)],
            {"hash": TX_HASH},
            ChainPoint("ethereum", 10, BLOCK_HASH, timestamp),
            team_client,
        )
        self.assertIsNone(finding)
        self.assertFalse(proved)

        balance = 123456
        migration_client = MappingEthereum(
            {"from": CALLER, "to": TELLOR_MASTER, "input": hx(function_selector("migrate()"))}
        )
        add_address_var(migration_client, "_OLD_TELLOR", 9, OLD_TELLOR)
        migration_client.add(
            TELLOR_MASTER,
            call_data("isMigrated(address)", ["address"], [CALLER]),
            9,
            ["bool"],
            [False],
        )
        migration_client.add(
            TELLOR_MASTER,
            call_data("isMigrated(address)", ["address"], [CALLER]),
            10,
            ["bool"],
            [True],
        )
        migration_client.add(
            OLD_TELLOR,
            call_data("balanceOf(address)", ["address"], [CALLER]),
            9,
            ["uint256"],
            [balance],
        )
        add_supply(migration_client, 10**24, balance)
        finding, _ = evaluate_ethereum_issuance(
            [transfer(CALLER, balance, 1)],
            {"hash": TX_HASH},
            ChainPoint("ethereum", 10, BLOCK_HASH, timestamp),
            migration_client,
        )
        self.assertIsNone(finding)

    def test_wrong_ethereum_mint_amount_is_p0(self):
        timestamp = 1_000
        prior = 900
        released = DAILY_RELEASE_WEI * (timestamp - prior) // 86_400
        client = MappingEthereum(
            {"from": CALLER, "to": TELLOR_MASTER, "input": hx(function_selector("mintToTeam()"))}
        )
        add_uint_var(client, "_LAST_RELEASE_TIME_TEAM", 9, prior)
        add_uint_var(client, "_LAST_RELEASE_TIME_TEAM", 10, timestamp)
        add_address_var(client, "_OWNER", 9, OWNER)
        add_supply(client, 10**24, released + 1)
        finding, _ = evaluate_ethereum_issuance(
            [transfer(OWNER, released + 1, 1)],
            {"hash": TX_HASH},
            ChainPoint("ethereum", 10, BLOCK_HASH, timestamp),
            client,
        )
        self.assertEqual(finding.severity, "P0")

    def test_layer_initialization_zero_mint_and_positive_pair(self):
        cursor = LayerMintCursor.initial()
        init_row = {
            "event_type": "minter_initialized",
            "attributes_json": "{}",
            "source": "txs_results:0",
        }
        finding, cursor = evaluate_layer_issuance(
            [init_row],
            {"height": 1, "block_hash": "A" * 64, "block_time_ms": 1000},
            cursor,
        )
        self.assertIsNone(finding)
        finding, cursor = evaluate_layer_issuance(
            [],
            {"height": 2, "block_hash": "B" * 64, "block_time_ms": 2000},
            cursor,
        )
        self.assertIsNone(finding)
        rows = [
            {
                "event_type": "mint_coins",
                "attributes_json": json.dumps(
                    {"amount": "3306loya", "destination": "mint", "mode": "BeginBlock"}
                ),
                "source": "finalize_block_events",
            },
            {
                "event_type": "inflationary_rewards_distributed",
                "attributes_json": json.dumps(
                    {"total_amount": "3306loya", "mode": "BeginBlock"}
                ),
                "source": "finalize_block_events",
            },
        ]
        finding, cursor = evaluate_layer_issuance(
            rows,
            {"height": 3, "block_hash": "C" * 64, "block_time_ms": 3944},
            cursor,
        )
        self.assertIsNone(finding)
        finding, _ = evaluate_layer_issuance(
            [],
            {"height": 4, "block_hash": "D" * 64, "block_time_ms": 5944},
            cursor,
        )
        self.assertEqual(finding.severity, "P0")

    def test_m5_structural_spot_price_is_silent_and_known_malformed_is_p1(self):
        query_data = encode(
            ["string", "bytes"],
            ["SpotPrice", encode(["string", "string"], ["eth", "usd"])],
        )
        query_id = "0x" + keccak(query_data).hex()
        event = DecodedEvent(
            address=TELLOR_FLEX,
            name="NewReport",
            signature="NewReport(bytes32,uint256,bytes,uint256,bytes,address)",
            args={
                "_queryId": query_id,
                "_time": 100,
                "_value": hx(encode(["uint256"], [123])),
                "_nonce": 1,
                "_queryData": hx(query_data),
                "_reporter": CALLER,
            },
            transaction_hash=TX_HASH,
            log_index=1,
            block_number=10,
            block_hash=BLOCK_HASH,
        )
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1000)
        self.assertIsNone(evaluate_new_report(event, point, {}))
        event.args["_value"] = "0xzz"
        self.assertEqual(evaluate_new_report(event, point, {}).severity, "P1")
        event.args["_queryId"] = (
            "0x0d12ad49193163bbbeff4e6db8294ced23ff8605359fd666799d4e25a3aa0e3a"
        )
        event.args["_queryData"] = "0x01"
        self.assertEqual(evaluate_new_report(event, point, {}).severity, "P1")


class _FakeStore:
    """Minimal stand-in for StateStore's run_once() surface."""

    def __init__(self, calls):
        self.calls = calls
        self.meta = {}

    def ingest_spool(self, spool_path):
        self.calls.append("ingest_spool")

    def set_meta(self, key, value):
        self.calls.append("set_meta:{}".format(key))
        self.meta[key] = value


class _FakeGate:
    """A bare object exposing exactly the attributes AlertGate.run_once()
    touches, so run_once() can be exercised without constructing a real
    AlertGate (which requires live RPC endpoints, seed files, etc.)."""

    def __init__(self, *, matches_raise=False, scheduled_raises=False):
        self.calls = []
        self.store = _FakeStore(self.calls)
        self.settings = type("Settings", (), {"spool_path": "unused"})()
        self._matches_raise = matches_raise
        self._scheduled_raises = scheduled_raises

        gate = self

        class _DepositBackfiller:
            def ingest_available(self):
                gate.calls.append("deposit_backfill")

        class _LayerIngestor:
            def ingest_available(self, limit=100):
                gate.calls.append("layer_ingest")

        self.deposit_backfiller = _DepositBackfiller()
        self.layer_ingestor = _LayerIngestor()

    def process_matches(self):
        self.calls.append("process_matches")
        if self._matches_raise:
            raise RuntimeError("simulated unrelated RPC failure in process_matches")

    def process_layer_blocks(self, limit=100):
        self.calls.append("process_layer_blocks")

    def process_scheduled(self):
        self.calls.append("process_scheduled")
        if self._scheduled_raises:
            raise RuntimeError("simulated failure in process_scheduled")

    def process_stale_databridges(self):
        self.calls.append("process_stale_databridges")


class RunOnceStageIsolationTests(unittest.TestCase):
    """Regression coverage for BUG 3: a raising early stage in run_once()
    must not prevent process_scheduled() (M9-M11) or
    process_stale_databridges() (M3) from running -- those are exactly the
    absence-detection checks that a silently-skipped scheduled pass would
    defeat."""

    def test_early_stage_failure_still_lets_scheduled_stages_run(self):
        gate = _FakeGate(matches_raise=True)

        with self.assertLogs("tellor_alert_gate", level="ERROR") as logs:
            AlertGate.run_once(gate, run_scheduled=True)

        self.assertIn("process_matches", gate.calls)
        self.assertIn("layer_ingest", gate.calls)
        self.assertIn("process_layer_blocks", gate.calls)
        self.assertIn("process_scheduled", gate.calls)
        self.assertIn("process_stale_databridges", gate.calls)
        self.assertIn("set_meta:last_scheduled_minute", gate.calls)
        self.assertTrue(
            any("process_matches" in message for message in logs.output),
            msg="expected the failing stage name in the log output: {}".format(
                logs.output
            ),
        )

    def test_failed_scheduled_stage_does_not_mark_pass_complete(self):
        # If process_scheduled() itself fails, last_scheduled_minute must
        # NOT be recorded -- otherwise the M9-M11/M3 checks would be
        # skipped until the next scheduling interval instead of being
        # retried on the very next poll.
        gate = _FakeGate(scheduled_raises=True)

        with self.assertLogs("tellor_alert_gate", level="ERROR"):
            AlertGate.run_once(gate, run_scheduled=True)

        self.assertIn("process_scheduled", gate.calls)
        self.assertIn("process_stale_databridges", gate.calls)
        self.assertNotIn("set_meta:last_scheduled_minute", gate.calls)


class CodeEthereum(MappingEthereum):
    """MappingEthereum plus eth_getCode, for EnrollmentRegistry.verify_live()."""

    def __init__(self, trace, code_by_address_block):
        super().__init__(trace)
        self._code = code_by_address_block

    def code(self, address, block="latest"):
        return self._code[(address.lower(), block)]


class LogsEthereum(MappingEthereum):
    """MappingEthereum plus eth_getLogs, for evaluate_databank_event()."""

    def __init__(self, trace, logs):
        super().__init__(trace)
        self._logs = logs

    def logs(self, *, address, from_block, to_block, topics=None):
        return self._logs


class NoApprovals:
    def exact_match(self, **_kwargs):
        return None


class ApprovedChange:
    """Stand-in approvals source that always returns an active approved change."""

    def exact_match(self, **_kwargs):
        return {"change_id": "release-1"}


class StubEnrollments:
    """Stand-in for EnrollmentRegistry when the test is not about enrollment
    itself (that is covered directly by the EnrollmentRegistry tests below)."""

    def __init__(self, result):
        self.result = result

    def verify_live(self, *args, **kwargs):
        return self.result


class ValidatorParamsLayer:
    """Fake Tellor Layer client that returns a fixed validator-checkpoint
    response for every read (used to model both agreeing and disagreeing
    consecutive reads)."""

    def __init__(self, payload):
        self.payload = payload

    def validator_params(self, _timestamp):
        return self.payload


class HistoricalReportLayer:
    """Fake Tellor Layer client for evaluate_databank_event()'s
    historical_report() reconciliation read."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def historical_report(self, _query_id, _timestamp):
        self.calls += 1
        return self.payload


def add_databridge_state(
    client,
    address,
    block,
    *,
    checkpoint,
    power_threshold,
    validator_timestamp,
    unbonding_period=86_400,
    initialized=True,
):
    client.add(address, call_data("lastValidatorSetCheckpoint()"), block, ["bytes32"], [unhex(checkpoint)])
    client.add(address, call_data("powerThreshold()"), block, ["uint256"], [power_threshold])
    client.add(address, call_data("validatorTimestamp()"), block, ["uint256"], [validator_timestamp])
    client.add(address, call_data("unbondingPeriod()"), block, ["uint256"], [unbonding_period])
    client.add(address, call_data("initialized()"), block, ["bool"], [initialized])


ORACLE_UPDATED_SPEC = next(
    spec
    for spec in EVENT_SPECS
    if spec.name == "OracleUpdated" and spec.address == TELLOR_DATA_BANK.lower()
)


def oracle_updated_log(
    query_id_hex, attest_query_hex, report_tuple, attestation_timestamp, log_index, block_number, block_hash
):
    data = hx(
        encode(
            ["(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256)"],
            [(unhex(attest_query_hex), report_tuple, attestation_timestamp)],
        )
    )
    return {
        "address": TELLOR_DATA_BANK,
        "topics": [ORACLE_UPDATED_SPEC.topic, query_id_hex],
        "data": data,
        "transactionHash": TX_HASH,
        "logIndex": hex(log_index),
        "blockNumber": hex(block_number),
        "blockHash": block_hash,
    }


class M2BridgeControlTests(unittest.TestCase):
    """M2 bridge-control coverage for evaluate_bridge_controls(), never
    exercised by any test before this. Covers the pause trip-wire, the V2
    DataBridge-swap control (both an unapproved swap and an approved,
    enrolled, matching swap), the V2 role-update control (unapproved vs.
    approved), and the always-alert MintToOracleFailed signal."""

    def test_bridge_state_paused_is_always_flagged(self):
        event = DecodedEvent(
            address=TOKEN_BRIDGE_V2,
            name="BridgeStateUpdated",
            signature="BridgeStateUpdated(uint8)",
            args={"_newState": 1},
            transaction_hash=TX_HASH,
            log_index=1,
            block_number=10,
            block_hash=BLOCK_HASH,
        )
        transaction = {"from": CALLER, "hash": TX_HASH}
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1000)
        findings = evaluate_bridge_controls(
            [event], transaction, point, MappingEthereum({}), None, NoApprovals(), None, None
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "P0")
        self.assertEqual(findings[0].incident_key, "bridge-pause:v2:{}".format(TX_HASH))

    def test_unapproved_databridge_swap_is_flagged_and_approved_enrolled_swap_is_silent(self):
        target = "0x" + "66" * 20
        event = DecodedEvent(
            address=TOKEN_BRIDGE_V2,
            name="DataBridgeUpdated",
            signature="DataBridgeUpdated(address)",
            args={"_dataBridge": target},
            transaction_hash=TX_HASH,
            log_index=2,
            block_number=10,
            block_hash=BLOCK_HASH,
        )
        transaction = {"from": CALLER, "hash": TX_HASH}
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1000)

        unapproved_client = MappingEthereum({})
        unapproved_client.add(TOKEN_BRIDGE_V2, call_data("dataBridge()"), 10, ["address"], [target])
        findings = evaluate_bridge_controls(
            [event],
            transaction,
            point,
            unapproved_client,
            None,
            NoApprovals(),
            StubEnrollments((False, {"reason": "address is not pre-enrolled"})),
            None,
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0].incident_key, "v2-databridge:{}:{}".format(target, TX_HASH)
        )
        self.assertFalse(findings[0].observed["approved"])

        approved_client = MappingEthereum({})
        approved_client.add(TOKEN_BRIDGE_V2, call_data("dataBridge()"), 10, ["address"], [target])
        silent = evaluate_bridge_controls(
            [event],
            transaction,
            point,
            approved_client,
            None,
            ApprovedChange(),
            StubEnrollments((True, {"ethereum": {}, "layer": {}})),
            None,
        )
        self.assertEqual(silent, [])

    def test_unapproved_role_update_is_flagged_and_approved_role_update_is_silent(self):
        role = "0x" + "aa" * 32
        new_address = "0x" + "77" * 20
        event = DecodedEvent(
            address=TOKEN_BRIDGE_V2,
            name="RoleUpdateProposed",
            signature="RoleUpdateProposed(bytes32,address,uint256)",
            args={"_role": role, "_newAddress": new_address, "_newUpdateDelay": 3600},
            transaction_hash=TX_HASH,
            log_index=3,
            block_number=10,
            block_hash=BLOCK_HASH,
        )
        transaction = {"from": CALLER, "hash": TX_HASH}
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1000)

        unapproved_client = MappingEthereum({})
        unapproved_client.add(
            TOKEN_BRIDGE_V2,
            call_data("roleUpdateProposals(bytes32)", ["bytes32"], [unhex(role)]),
            10,
            ["address", "uint256", "uint256"],
            [new_address, 3600, 1000],
        )
        findings = evaluate_bridge_controls(
            [event], transaction, point, unapproved_client, None, NoApprovals(), None, None
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0].incident_key,
            "v2-role:{}:{}:{}:{}".format(role, new_address, 3600, TX_HASH),
        )

        approved_client = MappingEthereum({})
        approved_client.add(
            TOKEN_BRIDGE_V2,
            call_data("roleUpdateProposals(bytes32)", ["bytes32"], [unhex(role)]),
            10,
            ["address", "uint256", "uint256"],
            [new_address, 3600, 1000],
        )
        silent = evaluate_bridge_controls(
            [event], transaction, point, approved_client, None, ApprovedChange(), None, None
        )
        self.assertEqual(silent, [])

    def test_mint_to_oracle_failed_always_raises_a_p1_finding(self):
        event = DecodedEvent(
            address=TOKEN_BRIDGE_V2,
            name="MintToOracleFailed",
            signature="MintToOracleFailed()",
            args={},
            transaction_hash=TX_HASH,
            log_index=4,
            block_number=10,
            block_hash=BLOCK_HASH,
        )
        transaction = {"from": CALLER, "hash": TX_HASH}
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1000)
        findings = evaluate_bridge_controls(
            [event], transaction, point, MappingEthereum({}), None, NoApprovals(), None, None
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "P1")


class M3DatabridgeIntegrityTests(unittest.TestCase):
    """M3 databridge-integrity coverage: EnrollmentRegistry (schema loading
    and the live verify_live() check), evaluate_validator_event() (ordinary
    validator-set updates and the always-alert guardian reset), and
    evaluate_stale_databridge(), which compares validator-set age against
    the *live* on-chain unbondingPeriod() rather than a fixed constant."""

    def test_matching_validator_update_is_silent_and_disagreeing_update_is_p0(self):
        threshold = 5000
        timestamp = 1_700_000_000
        valset_hash = keccak(b"validator-set-fixture")
        computed = compute_checkpoint(VALIDATOR_DOMAIN, threshold, timestamp, valset_hash)

        event = DecodedEvent(
            address=TELLOR_DATA_BRIDGE,
            name="ValidatorSetUpdated",
            signature="ValidatorSetUpdated(uint256,uint256,bytes32)",
            args={
                "_powerThreshold": threshold,
                "_validatorTimestamp": timestamp,
                "_validatorSetHash": hx(valset_hash),
            },
            transaction_hash=TX_HASH,
            log_index=1,
            block_number=20,
            block_hash=BLOCK_HASH,
        )
        point = ChainPoint("ethereum", 20, BLOCK_HASH, timestamp + 100)

        matching_client = MappingEthereum({})
        add_databridge_state(
            matching_client, TELLOR_DATA_BRIDGE, 20,
            checkpoint=computed, power_threshold=threshold, validator_timestamp=timestamp,
        )
        add_databridge_state(
            matching_client, TELLOR_DATA_BRIDGE, 19,
            checkpoint=hx(b"\x00" * 32), power_threshold=threshold, validator_timestamp=timestamp - 1000,
        )
        matching_layer = ValidatorParamsLayer(
            {
                "checkpoint": computed,
                "valset_hash": hx(valset_hash),
                "timestamp": timestamp,
                "power_threshold": threshold,
            }
        )
        self.assertIsNone(
            evaluate_validator_event(
                event, point, matching_client, matching_layer, second_delay=0, sleep=lambda *_: None
            )
        )

        disagreeing_client = MappingEthereum({})
        add_databridge_state(
            disagreeing_client, TELLOR_DATA_BRIDGE, 20,
            checkpoint=computed, power_threshold=threshold, validator_timestamp=timestamp,
        )
        add_databridge_state(
            disagreeing_client, TELLOR_DATA_BRIDGE, 19,
            checkpoint=hx(b"\x00" * 32), power_threshold=threshold, validator_timestamp=timestamp - 1000,
        )
        # The Tellor Layer checkpoint disagrees with the finalized EVM state
        # on both the first read and the (retried) second read.
        disagreeing_layer = ValidatorParamsLayer(
            {
                "checkpoint": computed,
                "valset_hash": hx(valset_hash),
                "timestamp": timestamp,
                "power_threshold": threshold + 1,
            }
        )
        finding = evaluate_validator_event(
            event, point, disagreeing_client, disagreeing_layer, second_delay=0, sleep=lambda *_: None
        )
        self.assertEqual(finding.severity, "P0")
        self.assertEqual(finding.incident_key, "databridge-validator:{}".format(timestamp))

        guardian_event = DecodedEvent(
            address=TELLOR_DATA_BRIDGE,
            name="GuardianResetValidatorSet",
            signature="GuardianResetValidatorSet(uint256,uint256,bytes32)",
            args={
                "_powerThreshold": threshold,
                "_validatorTimestamp": timestamp,
                "_validatorSetHash": hx(valset_hash),
            },
            transaction_hash=TX_HASH,
            log_index=2,
            block_number=20,
            block_hash=BLOCK_HASH,
        )
        # A guardian reset alerts unconditionally, even when the resulting
        # state is perfectly self-consistent -- it is an out-of-band admin
        # path that must always be reviewed.
        guardian_finding = evaluate_validator_event(
            guardian_event, point, matching_client, matching_layer, second_delay=0, sleep=lambda *_: None
        )
        self.assertIsNotNone(guardian_finding)
        self.assertEqual(guardian_finding.incident_key, "databridge-guardian-reset:{}".format(TX_HASH))

    def test_stale_databridge_uses_the_live_unbonding_period_not_a_constant(self):
        validator_timestamp_ms = 1_700_000_000_000
        unbonding_period = 100_000
        client = MappingEthereum({})
        add_databridge_state(
            client, TELLOR_DATA_BRIDGE, 30,
            checkpoint=hx(b"\x11" * 32),
            power_threshold=1000,
            validator_timestamp=validator_timestamp_ms,
            unbonding_period=unbonding_period,
        )
        # Age exactly equal to the live-read unbonding period is still
        # tolerated ("<=", not "<").
        fresh_point = ChainPoint(
            "ethereum", 30, BLOCK_HASH, validator_timestamp_ms // 1000 + unbonding_period
        )
        self.assertIsNone(evaluate_stale_databridge(TELLOR_DATA_BRIDGE, fresh_point, client))

        stale_point = ChainPoint(
            "ethereum", 30, BLOCK_HASH, validator_timestamp_ms // 1000 + unbonding_period + 1
        )
        finding = evaluate_stale_databridge(TELLOR_DATA_BRIDGE, stale_point, client)
        self.assertEqual(finding.severity, "P0")
        self.assertEqual(finding.incident_key, "databridge-stale:{}".format(validator_timestamp_ms))

        uninitialized_client = MappingEthereum({})
        add_databridge_state(
            uninitialized_client, TELLOR_DATA_BRIDGE, 31,
            checkpoint=hx(b"\x00" * 32),
            power_threshold=1,
            validator_timestamp=0,
            unbonding_period=1,
            initialized=False,
        )
        self.assertIsNone(
            evaluate_stale_databridge(
                TELLOR_DATA_BRIDGE, ChainPoint("ethereum", 31, BLOCK_HASH, 10**9), uninitialized_client
            )
        )

    def test_enrollment_registry_load_validates_the_pinned_schema(self):
        contract = {
            "address": TELLOR_DATA_BRIDGE,
            "runtime_bytecode_keccak256": hx(keccak(b"bytecode-fixture")),
            "source_revision": "a" * 40,
            "abi_revision": "v1",
            "validator_set_hash_domain_separator": hx(VALIDATOR_DOMAIN),
            "domain_rule": "compile-time-constant",
            "stale_rule": "block_timestamp-validator_timestamp_ms/1000>unbonding_period",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "enrollment.json"
            path.write_text(json.dumps({"schema_version": 1, "contracts": [contract]}))
            registry = EnrollmentRegistry.load(path)
            self.assertIsNotNone(registry.record(TELLOR_DATA_BRIDGE))

            path.write_text(
                json.dumps(
                    {"schema_version": 1, "contracts": [{**contract, "domain_rule": "nonsense"}]}
                )
            )
            with self.assertRaises(EnrollmentError):
                EnrollmentRegistry.load(path)

    def test_enrollment_registry_verify_live_confirms_or_rejects_the_live_contract(self):
        code_bytes = b"real-bytecode-fixture"
        record = {
            "address": TELLOR_DATA_BRIDGE,
            "runtime_bytecode_keccak256": hx(keccak(code_bytes)),
            "validator_set_hash_domain_separator": hx(VALIDATOR_DOMAIN),
        }
        registry = EnrollmentRegistry([record])

        threshold = 5000
        timestamp = 1_700_000_000
        valset_hash = keccak(b"validator-set-fixture-2")
        computed = compute_checkpoint(VALIDATOR_DOMAIN, threshold, timestamp, valset_hash)
        layer = ValidatorParamsLayer(
            {
                "checkpoint": computed,
                "valset_hash": hx(valset_hash),
                "timestamp": timestamp,
                "power_threshold": threshold,
            }
        )

        matching_client = CodeEthereum({}, {(TELLOR_DATA_BRIDGE.lower(), 40): hx(code_bytes)})
        add_databridge_state(
            matching_client, TELLOR_DATA_BRIDGE, 40,
            checkpoint=computed, power_threshold=threshold, validator_timestamp=timestamp,
        )
        ok, evidence = registry.verify_live(
            TELLOR_DATA_BRIDGE, matching_client, layer, 40, sleep=lambda *_: None, delay=30
        )
        self.assertTrue(ok, evidence)

        empty_registry = EnrollmentRegistry([])
        not_enrolled, reason = empty_registry.verify_live(TELLOR_DATA_BRIDGE, None, None, 40)
        self.assertFalse(not_enrolled)
        self.assertEqual(reason["reason"], "address is not pre-enrolled")

        tampered_client = CodeEthereum(
            {}, {(TELLOR_DATA_BRIDGE.lower(), 40): hx(b"tampered-bytecode")}
        )
        add_databridge_state(
            tampered_client, TELLOR_DATA_BRIDGE, 40,
            checkpoint=computed, power_threshold=threshold, validator_timestamp=timestamp,
        )
        mismatched, reason = registry.verify_live(
            TELLOR_DATA_BRIDGE, tampered_client, layer, 40, sleep=lambda *_: None, delay=30
        )
        self.assertFalse(mismatched)
        self.assertEqual(reason["reason"], "runtime bytecode hash mismatch")

    def test_compute_checkpoint_matches_keccak_of_the_pinned_abi_encoding(self):
        power_threshold = 5000
        validator_timestamp = 1_700_000_000
        validator_set_hash = keccak(b"validator-set-fixture")
        expected = hx(
            keccak(
                encode(
                    ["bytes32", "uint256", "uint256", "bytes32"],
                    [VALIDATOR_DOMAIN, power_threshold, validator_timestamp, validator_set_hash],
                )
            )
        )
        self.assertEqual(
            compute_checkpoint(VALIDATOR_DOMAIN, power_threshold, validator_timestamp, validator_set_hash),
            expected,
        )
        # A hex-string validator-set hash (as decoded off an event) must be
        # accepted identically to the raw-bytes form (as read from the
        # Tellor Layer client).
        self.assertEqual(
            compute_checkpoint(
                VALIDATOR_DOMAIN, power_threshold, validator_timestamp, hx(validator_set_hash)
            ),
            expected,
        )


class M6DatabankValueIntegrityTests(unittest.TestCase):
    """M6 databank-value-integrity coverage for evaluate_databank_event(),
    never exercised by any test before this. Covers a fully reconciled
    OracleUpdated event (silent), a Tellor Layer disagreement (P0), and the
    two distinct Unresolved paths: an undecodable attestation tuple, and an
    event that the finalized eth_getLogs read cannot locate."""

    def _matching_fixture(self):
        outer_query = ETH_USD_QUERY_ID
        self.assertIn(outer_query, M6_SPOT_QUERY_IDS)
        value_hex = hx(encode(["uint256"], [12345]))
        report_timestamp = 1_700_000_000
        power = 100
        attestation_timestamp = 1_700_000_010
        report_tuple = (unhex(value_hex), report_timestamp, power, 1_699_999_000, 0, 1_700_000_005)
        log = oracle_updated_log(
            outer_query, outer_query, report_tuple, attestation_timestamp,
            log_index=3, block_number=50, block_hash=BLOCK_HASH,
        )
        event = ORACLE_UPDATED_SPEC.decode(log)
        point = ChainPoint("ethereum", 50, BLOCK_HASH, 5_000_000)

        def build_client():
            client = LogsEthereum({}, [log])
            client.add(
                TELLOR_DATA_BANK,
                call_data("getAggregateValueCount(bytes32)", ["bytes32"], [unhex(outer_query)]),
                49, ["uint256"], [0],
            )
            client.add(
                TELLOR_DATA_BANK,
                call_data("getAggregateValueCount(bytes32)", ["bytes32"], [unhex(outer_query)]),
                50, ["uint256"], [1],
            )
            client.add(
                TELLOR_DATA_BANK,
                call_data(
                    "getAggregateByIndex(bytes32,uint256)", ["bytes32", "uint256"], [unhex(outer_query), 0]
                ),
                50,
                ["bytes", "uint256", "uint256", "uint256", "uint256"],
                [unhex(value_hex), power, report_timestamp, attestation_timestamp, point.timestamp],
            )
            return client

        return outer_query, value_hex, power, event, point, build_client

    def test_reconciled_oracle_update_is_silent_and_layer_disagreement_is_p0(self):
        outer_query, value_hex, power, event, point, build_client = self._matching_fixture()

        agreeing_layer = HistoricalReportLayer(
            {
                "aggregate": {
                    "query_id": outer_query[2:],
                    "aggregate_value": value_hex[2:],
                    "aggregate_power": power,
                }
            }
        )
        finding = evaluate_databank_event(
            event, point, build_client(), agreeing_layer, second_delay=0, sleep=lambda *_: None
        )
        self.assertIsNone(finding)

        disagreeing_layer = HistoricalReportLayer(
            {
                "aggregate": {
                    "query_id": outer_query[2:],
                    "aggregate_value": value_hex[2:],
                    "aggregate_power": power + 1,
                }
            }
        )
        finding = evaluate_databank_event(
            event, point, build_client(), disagreeing_layer, second_delay=0, sleep=lambda *_: None
        )
        self.assertEqual(finding.severity, "P0")
        self.assertIn(
            "Tellor Layer historical aggregate differs from event", finding.observed["mismatches"]
        )
        # The mismatch must be confirmed on a retried second read before
        # alerting -- a single disagreeing read is not enough evidence.
        self.assertEqual(disagreeing_layer.calls, 2)

    def test_undecodable_attestation_tuple_is_unresolved_not_alerted(self):
        event = DecodedEvent(
            address=TELLOR_DATA_BANK,
            name="OracleUpdated",
            signature="OracleUpdated(bytes32,(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256))",
            args={"queryId": "0x" + "11" * 32, "attestData": ["0x" + "11" * 32]},
            transaction_hash=TX_HASH,
            log_index=1,
            block_number=10,
            block_hash=BLOCK_HASH,
        )
        point = ChainPoint("ethereum", 10, BLOCK_HASH, 1000)
        with self.assertRaises(Unresolved):
            evaluate_databank_event(event, point, None, None)

    def test_event_absent_from_finalized_log_read_is_unresolved_not_alerted(self):
        outer_query = ETH_USD_QUERY_ID
        value_hex = hx(encode(["uint256"], [1]))
        report_tuple = (unhex(value_hex), 1_700_000_000, 5, 0, 0, 0)
        log = oracle_updated_log(
            outer_query, outer_query, report_tuple, 1_700_000_001,
            log_index=9, block_number=60, block_hash=BLOCK_HASH,
        )
        event = ORACLE_UPDATED_SPEC.decode(log)
        point = ChainPoint("ethereum", 60, BLOCK_HASH, 5_000_000)
        # The finalized eth_getLogs read for this block comes back empty --
        # e.g. a reorg or a lagging node -- so the event this MonitorMatch
        # names cannot be located there. That must be treated as incomplete
        # evidence (retry), not as proof of tampering.
        client = LogsEthereum({}, [])
        client.add(
            TELLOR_DATA_BANK,
            call_data("getAggregateValueCount(bytes32)", ["bytes32"], [unhex(outer_query)]),
            59, ["uint256"], [0],
        )
        client.add(
            TELLOR_DATA_BANK,
            call_data("getAggregateValueCount(bytes32)", ["bytes32"], [unhex(outer_query)]),
            60, ["uint256"], [0],
        )
        with self.assertRaises(Unresolved):
            evaluate_databank_event(event, point, client, None)


class M7LayerDisputeTests(unittest.TestCase):
    """M7 governance-dispute coverage for the Tellor Layer side,
    evaluate_layer_dispute(), which had no test before this (only the
    Ethereum side, evaluate_evm_disputes(), was covered)."""

    def _attributes(self, **overrides):
        attributes = {
            "disputer": CALLER,
            "reporter": CALLER,
            "dispute_category": "Warning",
            "total_fee": "100",
            "fee_paid": "50",
            "pay_from_bond": "false",
            "dispute_id": "7",
            "value": "00" * 32,
            "query_type": "SpotPrice",
            "query_id": "01" * 32,
            "report_block_number": "12345",
            "microreport_timestamp": "1700000000",
        }
        attributes.update(overrides)
        return attributes

    def test_new_layer_dispute_is_p1_with_the_derived_incident_key(self):
        row = {
            "height": 55,
            "event_key": "tellor-1:55:txs_results:0:new_dispute",
            "attributes_json": json.dumps(self._attributes()),
        }
        block_row = {"height": 55, "block_hash": "AA" * 32, "block_time_ms": 5_000_000}
        finding = evaluate_layer_dispute(row, block_row)
        self.assertEqual(finding.severity, "P1")
        self.assertEqual(
            finding.incident_key,
            "governance-dispute:tellor-1:0x{}:{}".format("01" * 32, 1_700_000_000),
        )
        self.assertEqual(finding.chain_point.chain, "tellor-1")

    def test_incomplete_or_non_canonical_dispute_attributes_are_unresolved_not_alerted(self):
        block_row = {"height": 55, "block_hash": "AA" * 32, "block_time_ms": 5_000_000}

        missing_field = self._attributes()
        del missing_field["value"]
        row = {"height": 55, "event_key": "x1", "attributes_json": json.dumps(missing_field)}
        with self.assertRaises(Unresolved):
            evaluate_layer_dispute(row, block_row)

        non_boolean_bond = self._attributes(pay_from_bond="yes")
        row = {"height": 55, "event_key": "x2", "attributes_json": json.dumps(non_boolean_bond)}
        with self.assertRaises(Unresolved):
            evaluate_layer_dispute(row, block_row)


if __name__ == "__main__":
    unittest.main()
