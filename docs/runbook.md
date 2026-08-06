# Per-monitor response runbook

This is the operator's response reference for each of the eleven Tellor alert-gate monitors, M1 through M11. For architecture, see [`../README.md`](../README.md); for deployment procedure, see [`operations.md`](operations.md); for target addresses and event signatures at a glance, see [`monitor-inventory.csv`](monitor-inventory.csv).

M1-M8 are OpenZeppelin Monitor EVM sensors defined by JSON files in [`../config/monitors/`](../config/monitors/): the Monitor only detects the raw on-chain event or function call and hands it to the alert gate, which applies the actual pass/fail logic described below. M9-M11 have **no Monitor JSON at all** — they detect the *absence* of an expected report, and an absence cannot be proven by watching for events. Instead, the alert gate runs them on a schedule inside `service/tellor_alert_gate/freshness.py`, reading `getDataBefore` from two independent Ethereum providers that must agree before a finding is raised.

Severities and first-response actions below are read directly from `MONITORS` and `FIRST_ACTIONS` in [`../service/tellor_alert_gate/constants.py`](../service/tellor_alert_gate/constants.py).

## M1 — tellormaster-control (P0)

**Target:** TellorMaster, `0x88dF592F8eb5D7Bd38bFeF7dEb0fBc02cf3778a0`.

**Fires on:** a successful `changeTellorContract(address)`, `changeDeity(address)`, or `changeOwner(address)` call, or a `NewTellorAddress`, `NewProposedOracleAddress`, or `NewOracleAddress` event (`config/monitors/m1_tellormaster_control.json`).

**Alerts when:** the change does not exactly match a valid, reviewed record in `policy/approved_changes.json` and its required post-state.

**First action:** stop bridge and issuance automation, verify the controlling action, and inspect the target bytecode before allowing another transaction.

## M2 — bridge-control (P0)

**Targets:** TokenBridge V1 (`0x5589e306b1920F009979a50B88caE32aecD471E4`) and TokenBridge V2 (`0x6ec401744008f4B018Ed9A36f76e6629799Ee50E`).

**Fires on:** `BridgeStateUpdated`, `PauseProposed`, `PauseApproved`, `DataBridgeUpdated`, `RoleUpdateProposed`, `RoleUpdateAccepted`, or `MintToOracleFailed` (`config/monitors/m2_bridge_control.json`).

**Alerts when:** a bridge pauses; a role or DataBridge change is unapproved or unenrolled; a DataBridge post-state differs from what is expected; or a new `MintToOracleFailed` failure period begins.

**First action:** stop the affected bridge automation and compare the action against the reviewed release manifest.

## M3 — databridge-integrity (P0)

**Target:** TellorDataBridge, `0xFfa3393BE1E4b442fff6cD0df0794B0031e9CF65`.

**Fires on:** `ValidatorSetUpdated` or `GuardianResetValidatorSet` (`config/monitors/m3_databridge_integrity.json`). This monitor also has a scheduled component: the alert gate periodically compares the Ethereum-side validator checkpoint against the Tellor Layer validator-checkpoint endpoints, and computes the DataBridge's staleness age against the live on-chain `unbondingPeriod()` value (`service/tellor_alert_gate/databridge.py`) rather than a fixed constant.

**Alerts when:** every guardian reset fires; the Ethereum and Tellor Layer validator sets persistently disagree; the validator timestamp fails to increase; or the validator-set age exceeds `unbondingPeriod()`, the exact boundary at which the bridge becomes unusable.

**First action:** stop bridge relaying, preserve calldata and signatures, and compare against the Tellor Layer validator set.

## M4 — bridge-ledger-integrity (P0)

**Targets:** TokenBridge V1, TokenBridge V2, and TellorMaster (for bridge-address token transfers).

**Fires on:** `Deposit`, `Withdraw`, both `ExtraWithdrawClaimed` signatures, `TokensToClaimUpdated`, `ExtraWithdrawReverified`, and `Transfer` events where the bridge is sender or recipient (`config/monitors/m4_bridge_ledger_integrity.json`). The alert gate reconciles these against Tellor Layer `deposit_claimed`, `tokens_withdrawn`, and `aggregate_report` events, starting from the reviewed checkpoint in `secrets/bridge_ledger_seed.json`.

**Alerts when:** a finalized deposit, withdrawal, claim, or bridge token outflow cannot be reconciled exactly across Ethereum and Tellor Layer. A delay or an absence alone does not, by itself, cause an alert.

**First action:** stop the affected relayer or claim path and preserve both chain records.

## M5 — tellorflex-value-integrity (P1)

**Target:** TellorFlex, `0x8cFc184c877154a8F9ffE0fe75649dbe5e2DBEbf`.

**Fires on:** `NewReport(bytes32,uint256,bytes,uint256,bytes,address)` (`config/monitors/m5_tellorflex_value_integrity.json`). For EVMCall-type queries, the alert gate performs an exact finalized historical replay: it requires a canonical ABI input, a configured source-chain client, exactly one matching source block at the reported timestamp, code present at that block, non-empty replay bytes, and a second confirming read that the block and code are stable (see [`../MONITORING_SOURCE_MAP.md`](../MONITORING_SOURCE_MAP.md) for the full condition list).

**Alerts when:** a known report is structurally non-canonical, or an exact finalized historical EVMCall replay proves the successful non-empty return bytes differ from what was reported. If the check cannot prove those conditions, the match stays unresolved rather than alerting — it also does not fall back to the Telliot zero-result convention for a target without code or an empty replay.

**First action:** preserve the query, value, and reference evidence and prepare a dispute review.

## M6 — databank-value-integrity (P0)

**Target:** TellorDataBank, `0x5526e7e7CF982f5D3045DAf18b7e813E4b7a8FE5`.

**Fires on:** `OracleUpdated(bytes32,(bytes32,(bytes,uint256,uint256,uint256,uint256,uint256),uint256))` (`config/monitors/m6_databank_value_integrity.json`). The alert gate cross-checks the accepted update against the indexed query ID, the `getAggregateValueCount`/`getAggregateByIndex` reads, the stored record, and the fixed-query decoder, and separately reads the corresponding Tellor Layer `retrieve_data` value.

**Alerts when:** the accepted update differs from any of the above. For the Layer-data comparison specifically, the gate reads Tellor Layer data a second time before alerting — a mismatch is only reported if the second read still disagrees.

**First action:** stop consumers from accepting the affected DataBank value and preserve the attestation and reference evidence.

## M7 — governance-dispute (P1)

**Targets:** Governance, `0xB30b1B98d8276b80bC4f5aF9f9170ef3220EC27D`, and TellorFlex (for the `ValueRemoved` fallback path).

**Fires on:** `NewDispute(uint256,bytes32,uint256,address)` or `ValueRemoved(bytes32,uint256)` (`config/monitors/m7_governance_dispute.json`). The alert gate also observes the Tellor Layer `new_dispute` event.

**Alerts when:** an Ethereum `NewDispute`, an isolated `ValueRemoved` fallback, or a Tellor Layer `new_dispute` opens for a given query ID and report timestamp. It alerts once per dispute.

**First action:** identify consumers of the query and timestamp, preserve the disputed value, and review the dispute evidence and voting deadline.

## M8 — issuance-integrity (P0)

**Target:** TellorMaster, `0x88dF592F8eb5D7Bd38bFeF7dEb0fBc02cf3778a0`.

**Fires on:** `Transfer(address,address,uint256)` where `_from` is the zero address, i.e. a mint (`config/monitors/m8_issuance_integrity.json`). The alert gate additionally traces the `mintToOracle`, `mintToTeam`, and `migrate` functions, and cross-checks Tellor Layer `mint_coins` and `inflationary_rewards_distributed` event pairs.

**Alerts when:** an Ethereum mint's path, recipient, amount, log, supply delta, or state transition is invalid, or when a Tellor Layer mint/reward event pair is invalid, missing, or unexpected — including a pair appearing before initialization, or appearing when the issuance formula expects no positive pair.

**First action:** stop bridge and issuance automation, preserve the transaction or block evidence, and reconcile the implementation version.

## M9 — tellorflex-eth-usd-freshness (P1)

**No Monitor JSON.** Scheduled inside `service/tellor_alert_gate/freshness.py`, function `evaluate_eth_usd`.

**Condition:** the gate takes the primary Ethereum provider's head, requires it to be on chain ID 1, not more than 60 seconds in the future, and not more than 600 seconds stale, then reads `getDataBefore(bytes32,uint256)` for the ETH/USD query ID at the block 12 confirmations behind that head. After a delay (30 seconds by default), it repeats the read on a second, independent provider and requires the two to agree on block identity and decoded result before proceeding.

**Alerts when:** no undisputed ETH/USD report is found, or the report's age (confirmed block timestamp minus report timestamp) exceeds `ETH_MAX_AGE_SECONDS = 14,400` seconds (4 hours). If the two providers disagree, or either provider's head is unhealthy, the check is left unresolved rather than raising an alert or a clean pass.

**First action:** inspect the ETH/USD reporting job and open disputes, then restore reporting.

## M10 — tellorflex-ampl-usd-deadline (P1)

**No Monitor JSON.** Scheduled inside `service/tellor_alert_gate/freshness.py`, function `evaluate_ampl_day`.

**Condition:** for the latest eligible day (today, once the clock has passed 00:30 UTC; otherwise yesterday), the gate defines a window from `00:00:00` UTC to `00:30:00` UTC on that day, finds the earliest 12-confirmation-deep block at or after `00:30:00` UTC, and — after the same two-provider agreement check used by M9 — looks for an undisputed AMPL/USD report timestamp inside that window.

**Alerts when:** no undisputed AMPL/USD report timestamp falls within the inclusive `00:00:00`-`00:30:00` UTC window for that day.

**First action:** inspect the AMPL reporting schedule and disputes, then restore the next window.

## M11 — tellorflex-uspce-deadline (P1)

**No Monitor JSON.** Scheduled inside `service/tellor_alert_gate/freshness.py`, function `evaluate_uspce_month`.

**Condition:** for the latest eligible month (the current month once the clock has reached `00:00:00` UTC on that month's final calendar day; otherwise the previous month), the gate defines a window from the first day of the month `00:00:00` UTC through `00:00:00` UTC on the final calendar day, inclusive, finds the earliest confirmed block at or after that cutoff, and — after the same two-provider agreement check — looks for an undisputed USPCE report timestamp inside that window.

**Alerts when:** no undisputed USPCE report timestamp falls within that inclusive monthly window.

**First action:** inspect the USPCE reporting schedule and disputes, then restore the next deadline.
