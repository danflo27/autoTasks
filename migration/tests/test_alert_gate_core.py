import json
from pathlib import Path
import sys
import tempfile
import unittest

from eth_abi import encode
from eth_utils import keccak


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "alert_gate"))

from tellor_alert_gate.constants import (  # noqa: E402
    TELLOR_FLEX,
    TELLOR_MASTER,
    TOKEN_BRIDGE_V2,
    ZERO_ADDRESS,
)
from tellor_alert_gate.control import evaluate_master_controls  # noqa: E402
from tellor_alert_gate.bridge_seed import (  # noqa: E402
    EthereumDepositBackfiller,
    read_bridge_ledger_seed,
)
from tellor_alert_gate.delivery import DeliveryClient, publish  # noqa: E402
from tellor_alert_gate.ethereum import (  # noqa: E402
    call_data,
    decode_receipt_events,
    function_selector,
    hx,
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
    evaluate_evm_disputes,
)
from tellor_alert_gate.layer import successful_event_rows  # noqa: E402
from tellor_alert_gate.models import (  # noqa: E402
    ChainPoint,
    DecodedEvent,
    Finding,
    Unresolved,
)
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


if __name__ == "__main__":
    unittest.main()
