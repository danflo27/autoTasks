# Tellor Monitoring Implementation Progress

> Authoritative implementation/progress handoff. This document records the locally implemented system and point-in-time evidence; it does not claim production deployment or current host state.

## Metadata

| Field | Value |
|---|---|
| Document | Tellor Monitoring Implementation Progress |
| Plan version | `0.2.2` |
| Overall plan status | `IN PROGRESS — local implementation complete; runtime gates remain` |
| Document status | `LOCALLY VERIFIED` |
| Repository branch | `monitor-migration` |
| Baseline commit | `ef4739cb69be39a13fbd059a0a72c7539bebb2bf` |
| Worktree | `DIRTY — pre-existing user changes preserved` |
| Production state | `POINT-IN-TIME — Monitor disabled/inactive` |
| Authority boundary | Local repository implementation and documentation authorized; no production, account, host, secret rotation, or destination mutation authorized |
| Baseline captured | `2026-07-21T06:13:12Z` |
| Last document verification | `2026-07-21T14:57:01Z` |

### Status rules

The only allowed statuses are:

- `NOT STARTED`
- `IN PROGRESS`
- `IMPLEMENTED`
- `LOCALLY VERIFIED`
- `DEPLOYED VERIFIED`
- `BLOCKED`
- `SUPERSEDED`

Every work-ledger row includes an ID, deliverable, status, acceptance criterion, evidence, dependency or blocker, next action, and last-updated UTC. Local tests never imply deployment. `DEPLOYED VERIFIED` requires direct runtime and delivery evidence from the intended host and destinations.

Version `0.2.2` implements and locally verifies the routing, report-classification, freshness, watchdog, test, and documentation work described below. Static Compose and systemd artifacts are implemented, but Docker-image, Linux systemd, live RPC, live destination, EC2 deployment, and rollback evidence remain outside the local evidence boundary. The immutable `0.1` baseline below is preserved verbatim.

## Immutable baseline

This section is the dated local and runtime baseline for plan version `0.1`. Do not revise it when conditions change. Append later runtime observations to [Runtime snapshots](#runtime-snapshots) and record changes in the [Decision and deviation ledger](#decision-and-deviation-ledger).

### Local repository snapshot — `2026-07-21T06:13:12Z`

The repository contains twelve OpenZeppelin Monitor JSON files. Every monitor declares only `ethereum_mainnet` and uses the shared `tellor_alert` trigger. Eleven are unpaused; `Smoke Test USDC Transfer` is paused.

| Current monitor | Contract | Current match |
|---|---|---|
| Tellor Address Updates | `0x8cFc…BEbf` | `submitValue`, `updateStakeAmount` |
| TellorFlex Data Report | `0x8cFc…BEbf` | `submitValue` |
| Tellor DVM Price Deviation | `0x8cFc…BEbf` | `submitValue` |
| Tellor EVMCall Validation | `0x8cFc…BEbf` | `submitValue` |
| Tellor Staking | `0x8cFc…BEbf` | `NewStaker`, `StakeWithdrawRequested`, `StakeWithdrawn` |
| Tellor Token Bridge | `0x5589…71E4` | `addStakingRewards`, `claimExtraWithdraw`, `pauseBridge`, `unpauseBridge` |
| Tellor Deposit To Layer | `0x5589…71E4` | `depositToLayer` |
| Tellor Withdraw From Layer | `0x5589…71E4` | `Withdraw` event |
| Update Oracle Data Calls | `0x5526…8FE5` | `updateOracleData` |
| Update Validator Set Calls | `0xFfa339…CF65` | `updateValidatorSet` |
| Guardian Reset Validator Set Calls | `0xFfa339…CF65` | `GuardianResetValidatorSet` event |
| Smoke Test USDC Transfer | USDC `0xA0b8…eB48` | Paused large-transfer event |

Current behavior and gaps:

- Four configurations overlap around `submitValue`: Tellor Address Updates, TellorFlex Data Report, Tellor DVM Price Deviation, and Tellor EVMCall Validation.
- Checked-in production handlers share `DISCORD_WEBHOOK_URL`. If it is unset, delivery returns successfully after writing only to the local alert log.
- Quickstart is isolated on `CUSTOM_DISCORD_WEBHOOK_URL` and must remain isolated.
- No per-monitor route file, route preflight, freshness checker, freshness state, or freshness timer exists.
- The current uncommitted code includes partial trusted-price and strict `>20%` work. It is baseline/dependency evidence, not completion of this plan.
- The current DVM alarms at `>=10%` deviation.
- The current EVMCall handler checks `latest`, omits chain ID `1`, and does not replay the call at the historical target-chain block corresponding to the response timestamp.
- A malformed known query or value can raise through the handler entry point, exit non-zero, and produce no alert.
- Compose uses content-pinned OpenZeppelin Monitor v1.5.0, `restart: on-failure:5`, and no container healthcheck.
- Ethereum mainnet polling runs minutely and waits 12 confirmations.

Verified local evidence at this snapshot:

| Command | Result | Boundary |
|---|---|---|
| `python3 migration/tests/run_tests.py` | `54 passed, 0 failed` | Local working tree only |
| `python3 -m unittest migration/tests/test_quickstart.py` | `39 passed` | Local working tree only |
| `python3 migration/quickstart.py check` | `BLOCKED` | Docker API unavailable at the local socket; no Monitor-image config check ran |

### EC2 handoff snapshot — `2026-07-21 04:52–04:59 UTC`

The following is a timestamped handoff observation, not live truth:

- OpenZeppelin Monitor was disabled and inactive.
- The container had exited `137` because it did not stop within Docker's grace period and was sent SIGKILL; `OOMKilled=false`.
- The watchdog timer remained enabled and repeatedly detected the intentionally stopped Monitor.
- The then-current Discord URL appeared in operator chat and requires rotation if still used.
- Host application trees were copied trees without Git metadata.
- `migration/scripts/tellor-ops-setup.sh` is non-idempotent and must not be rerun wholesale.
- README and MIGRATION “activated” wording is stale relative to this later snapshot.
- The locally linked `migration/AWS_DOCKER_OPERATIONS.md` is missing.

## Target interfaces and alert catalog

Everything in this section is the implemented local interface contract. Ledger status and verification evidence—not this prose alone—determine what has been locally or operationally verified.

### Per-monitor routing

The route object has exactly these 15 case-sensitive names:

1. `TellorFlex Disputable Value`
2. `Tellor Address Updates`
3. `Tellor Staking`
4. `Tellor Token Bridge`
5. `Tellor Deposit To Layer`
6. `Tellor Withdraw From Layer`
7. `Update Oracle Data Calls`
8. `Update Validator Set Calls`
9. `Guardian Reset Validator Set Calls`
10. `Smoke Test USDC Transfer`
11. `Tellor ETH/USD Freshness`
12. `Tellor AMPL/USD Freshness`
13. `Tellor USPCE Freshness`
14. `Tellor Freshness Checker Health`
15. `Tellor Monitor Watchdog`

Routing decisions:

- Each monitor has one URL. All outcomes and recoveries from that monitor share its URL. Repeated URLs across names are allowed.
- The live secret is `migration/secrets/discord_webhooks.json`.
- `migration/secrets/` must be mode `0700`; the file must be a regular non-symlink file with mode `0600`.
- Ignore the entire secrets directory and `.env` backup variants.
- Commit `migration/config/discord_webhooks.example.json` with all 15 exact keys and empty values.
- JSON is exactly one monitor-name-to-URL object. Duplicate keys are invalid.
- A missing or invalid route for an enabled monitor is logged locally and fails without fallback.
- The paused smoke route is optional. Unknown extra names warn.
- Routes are read on every delivery.
- Mount the secrets directory read-only, not only the file, so atomic file replacement is visible without restarting the container.
- Host freshness and watchdog processes read the same host file.
- The path interface is `DISCORD_WEBHOOKS_FILE`.
- Delivery mode is explicit: `live` or `log-only`. Replay must explicitly select `log-only`.
- Redacted preflight is `python3 migration/quickstart.py check-discord-routes`.
- Preflight prints only monitor name and status, never URLs or URL fragments.
- Retire production reliance on `DISCORD_WEBHOOK_URL` and watchdog-specific webhook variables after migration.
- Preserve quickstart's isolated `CUSTOM_DISCORD_WEBHOOK_URL` behavior.

### Unified report interface

Monitor the exact event:

```solidity
NewReport(bytes32 indexed _queryId,uint256 indexed _time,bytes _value,uint256 _nonce,bytes _queryData,address indexed _reporter)
```

One event produces one generated handler/delivery attempt. This is not an exactly-once guarantee.

Every report produces exactly one of these outcome labels:

- `✅ Looks normal`
- `👀 Report received`
- `⚠️ NOT VERIFIED`
- `🚨 POTENTIAL DISPUTE`

Classification and configuration contract:

- Remove the DVM and EVMCall monitor configurations and remove `submitValue` from Tellor Address Updates.
- SpotPrice uses the 17-feed union from the current trusted-price and DVM catalogs.
- SpotPrice comparison is strictly `abs(reported - trusted) / trusted > 0.20`; exactly 20% is normal.
- No usable trusted source yields `⚠️ NOT VERIFIED`.
- A trusted-source median is operator evidence, not protocol truth. Rapid markets and imperfect time alignment can cause false positives.
- EVMCall target chain IDs are `1`, `10`, `100`, `137`, `10200`, and `11155111`.
- Resolve the response timestamp to the target-chain block and replay at that historical block.
- A historical mismatch is potentially disputable. Missing archive RPC, no exact block, RPC ambiguity, or reorganization ambiguity is `⚠️ NOT VERIFIED`.
- Preserve pinned Telliot EVMCall behavior: empty calldata is not verifiable; non-empty 1–3-byte calldata, a no-code target, or a missing selector after a failed call yields the canonical 32-byte zero result.
- TellorRNG, address reports, AMPL, and USPCE receive structural validation.
- A valid unknown query type is neutral and must never be falsely verified.
- Malformed known query/value data produces an alarm rather than a lost handler.

### Freshness interface

Constants:

| Item | Value |
|---|---|
| TellorFlex contract | `0x8cFc184c877154a8F9ffE0fe75649dbe5e2DBEbf` |
| ETH/USD query ID | `0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992` |
| AMPL/USD query ID | `0x0d12ad49193163bbbeff4e6db8294ced23ff8605359fd666799d4e25a3aa0e3a` |
| USPCE query ID | `0x612ec1d9cee860bb87deb6370ed0ae43345c9302c085c1dfc4c207cbec2970d7` |
| State | `/var/lib/tellor/report-freshness/state.json` |
| Heartbeat | `/var/lib/tellor/report-freshness/heartbeat` |

Timing and state policy:

- `getDataBefore` is strictly before its timestamp argument and skips disputed reports; use `cutoff + 1` for inclusive deadlines.
- Evaluate only after the cutoff block is 12-confirmed. With minutely polling, expected notification latency is approximately 2.4–3.4 minutes, plus RPC/recovery delay.
- ETH/USD: alarm only when confirmed report age is strictly greater than 14,400 seconds; send one incident alarm and one recovery.
- AMPL/USD: operator policy requires one report from `00:00` through `00:30 UTC` inclusive; send one immutable missed-date alert.
- USPCE: operator policy evaluates at `00:00 UTC` at the start of the final calendar day and requires a report since month start; send one immutable missed-month alert.
- Three complete provider failures produce one checker-health alert and one recovery. RPC failure never becomes a false missing-report alarm.
- Replace state atomically; preserve corrupt state; on bootstrap consider only the latest period; do not advance state after failed delivery.

### Retained alert catalog

The completed system retains or introduces all of these alerts:

- Stake amount update.
- Staking deposit, withdrawal request, and withdrawal.
- Bridge staking rewards, extra-withdraw claim, pause, unpause, deposit, and withdrawal.
- Oracle data update.
- Validator-set update.
- Guardian validator-set reset.
- Paused smoke transfer.
- ETH/USD, AMPL/USD, and USPCE freshness alarms.
- ETH/USD freshness recovery.
- Freshness-checker health warning and recovery.
- Monitor watchdog warning and recovery.

## Complete work ledger

Evidence is scoped to the local worktree unless a row explicitly says otherwise. `LOCALLY VERIFIED` never implies deployment; `IMPLEMENTED` denotes an artifact whose runtime acceptance still requires the intended Linux/Docker environment.

| ID | Deliverable | Status | Acceptance criterion | Evidence | Dependency / blocker | Next action | Last-updated UTC |
|---|---|---|---|---|---|---|---|
| ROUTE-001 | Schema, example, ignore rules | `LOCALLY VERIFIED` | Exact 15-key example; secrets and `.env` backups ignored | Example file and ignore assertions; route suite `16/16` | Route names must remain synchronized | Reverify after producer-name changes | `2026-07-21T14:41:18Z` |
| ROUTE-002 | Secure loader and validation | `LOCALLY VERIFIED` | Strict JSON, duplicate-key rejection, modes, regular-file and symlink checks | `discord_routes.py`; route suite `16/16` | ROUTE-001 | Reverify after loader changes | `2026-07-21T14:41:18Z` |
| ROUTE-003 | Routed delivery and log-only mode | `LOCALLY VERIFIED` | Per-name route, read per delivery, explicit `live`/`log-only`, no fallback | Route suite `16/16`; delivery integration `5/5` | ROUTE-002 | Obtain authorized live proof under TEST-005 | `2026-07-21T14:41:18Z` |
| ROUTE-004 | Redacted preflight/name synchronization | `LOCALLY VERIFIED` | `check-discord-routes`; names/status only; exact enabled-name sync | Route suite `16/16`; quickstart suite `42/42` | ROUTE-001–003 | Run host preflight only after secret installation | `2026-07-21T14:41:18Z` |
| DISPUTE-001 | Indexed event monitor and duplicate removal | `LOCALLY VERIFIED` | Exact `NewReport` event; one unified monitor; old DVM/EVMCall and Address Updates overlap removed | Unified config and uniqueness assertions; dispute suite `15/15` | Route name exists | Reverify after config changes | `2026-07-21T14:41:18Z` |
| DISPUTE-002 | Single-outcome classifier/message contract | `LOCALLY VERIFIED` | Exactly one of four labels and one delivery attempt per event | Pure classifier tests and handler runner `33/33` | DISPUTE-001, ROUTE-003 | Reverify after outcome changes | `2026-07-21T14:41:18Z` |
| DISPUTE-003 | SpotPrice union and threshold | `LOCALLY VERIFIED` | 17-feed union; strict `>20%`; no source = NOT VERIFIED | Boundary/source tests in dispute suite `15/15` | External sources remain operational dependencies | Observe source quality after authorized activation | `2026-07-21T14:41:18Z` |
| DISPUTE-004 | Historical EVMCall validation | `LOCALLY VERIFIED` | Six chains; timestamp-to-block resolution; historical replay; ambiguity handling; zero-result preservation | Pinned Telliot semantics, including empty vs. 1–3-byte calldata, and deterministic EVMCall cases; dispute suite `15/15` | Live archive RPC coverage unverified | Run authorized archive-RPC replay under TEST-005 | `2026-07-21T14:54:03Z` |
| DISPUTE-005 | Structural validators and unknown fallback | `LOCALLY VERIFIED` | TellorRNG/address/AMPL/USPCE validation; unknown valid query neutral | Structural/unknown cases in handler and dispute suites | DISPUTE-002 | Reverify with query-spec changes | `2026-07-21T14:41:18Z` |
| DISPUTE-006 | Malformed-input containment | `LOCALLY VERIFIED` | Every malformed known input emits an alarm and does not escape the handler | Negative fixtures pass in handler/dispute suites | DISPUTE-002, ROUTE-003 | Reverify with decoder changes | `2026-07-21T14:41:18Z` |
| FRESH-001 | Confirmed RPC/query client | `LOCALLY VERIFIED` | Provider fallback, confirmed cutoff block, strict-before query, disputed-report skip | Deterministic provider/confirmed-block tests; freshness suite `33/33` | Live RPC unverified | Run live read-only RPC probe after authorization | `2026-07-21T14:41:18Z` |
| FRESH-002 | ETH rolling freshness | `LOCALLY VERIFIED` | Strictly `>14400s`; one alarm and one recovery | Threshold/dedup/recovery tests; freshness suite `33/33` | FRESH-001, FRESH-005 | Verify runtime heartbeat after deployment | `2026-07-21T14:41:18Z` |
| FRESH-003 | AMPL daily deadline | `LOCALLY VERIFIED` | Inclusive 00:00–00:30 UTC; immutable missed-date alert | Calendar, confirmation, and post-cutoff-dispute regressions; `33/33` | FRESH-001, FRESH-005 | Verify next authorized daily window | `2026-07-21T14:41:18Z` |
| FRESH-004 | USPCE monthly deadline | `LOCALLY VERIFIED` | Final-day 00:00 UTC check; report since month start; immutable missed-month alert | Month/leap/confirmation/dispute tests; `33/33` | FRESH-001, FRESH-005 | Verify next authorized monthly window | `2026-07-21T14:41:18Z` |
| FRESH-005 | State, deduplication, retry | `LOCALLY VERIFIED` | Atomic replacement, corrupt-state preservation, restart dedup, no advancement after failed delivery | State/corruption/retry tests; freshness suite `33/33` | ROUTE-003 | Verify filesystem ownership on target | `2026-07-21T14:41:18Z` |
| FRESH-006 | RPC fallback, health, recovery, bootstrap | `LOCALLY VERIFIED` | Three total failures = one health alarm; recovery; latest-period-only bootstrap; no false missing alarm | Provider and zero-provider regressions, heartbeat/state assertions; `33/33` | Live providers unverified | Run live read-only provider failover probe | `2026-07-21T14:41:18Z` |
| OPS-001 | Secret mount and restart policy | `IMPLEMENTED` | Secret directory mounted read-only; atomic replacement visible; reviewed restart policy | Compose renders cleanly; directory mount and `unless-stopped` present | Docker daemon unavailable for mount behavior | Exercise atomic replacement in Monitor container | `2026-07-21T14:41:18Z` |
| OPS-002 | Freshness service/timer/state directories | `IMPLEMENTED` | Hardened unit/timer; state/heartbeat paths and ownership; enablement gated | Units and additive installer; `bash -n` and static hardening tests pass | Linux systemd/target ownership not locally available | Run `systemd-analyze verify` and gated install on target | `2026-07-21T14:54:03Z` |
| OPS-003 | Watchdog coverage, deduplication, recovery | `LOCALLY VERIFIED` | Desired-state aware; checkpoint/health depth; one warning and recovery | Watchdog tests include dedup, recovery, failed delivery, stale and regressing checkpoints | Docker/systemd readback remains runtime work | Verify desired-state and checkpoint behavior on target | `2026-07-21T14:41:18Z` |
| TEST-001 | Route/security tests | `LOCALLY VERIFIED` | Schema, duplicates, URLs, modes, symlink, permissions, redaction, no fallback | `test_discord_routes.py` `16/16`; delivery integration `5/5` | None locally | Re-run after routing changes | `2026-07-21T14:41:18Z` |
| TEST-002 | Dispute/config tests | `LOCALLY VERIFIED` | Config uniqueness and every outcome including exact 20% and malformed inputs | `test_dispute.py` `15/15`; handler/config runner `33/33`; pinned empty-calldata regression included | None locally | Re-run after classifier/config changes | `2026-07-21T14:54:03Z` |
| TEST-003 | Freshness/calendar/state tests | `LOCALLY VERIFIED` | UTC rollover, leap years, month lengths, confirmations, provider/state/retry cases | `test_report_freshness.py` `33/33`; ops hardening `2/2` | None locally | Re-run after freshness/ops changes | `2026-07-21T14:41:18Z` |
| TEST-004 | Quickstart/hardened checks | `BLOCKED` | Existing 39 remain green; new preflight and compose checks pass | Current quickstart `42/42`; Compose config passes; image check cannot reach Docker socket | Working Docker daemon required | Run `python3 migration/quickstart.py check` with Docker available | `2026-07-21T14:41:18Z` |
| TEST-005 | Replay and delivery proof | `BLOCKED` | Confirmed-block NewReport replay; explicit log-only has zero HTTP; one controlled proof per distinct destination | Confirmed-event fixture and explicit zero-HTTP log-only tests pass | Docker Monitor image and authorized destinations unavailable | Run Monitor-image replay, then approved redacted destination proofs | `2026-07-21T14:41:18Z` |
| DOC-001 | Progress report | `LOCALLY VERIFIED` | Only this file added; full readback; names/IDs/ledger checked; secret scan clean; links validated | This document and verification record | None | Maintain append-only ledgers as work advances | `2026-07-21T06:13:12Z` |
| DOC-002 | Monitor/alert/operator documentation | `LOCALLY VERIFIED` | Current behavior, alert catalog, route operations, failure modes, ownership | README/MIGRATION updated; complete safe-test commands, local links, catalog, stale-claim, and secret scans pass | Runtime claims remain excluded | Maintain with interface changes | `2026-07-21T14:54:03Z` |
| DOC-003 | Provenance and deployment runbook | `LOCALLY VERIFIED` | Source/version map, reviewed deployment/rollback, evidence templates | Pinned source map and AWS runbook present; pre-existing, absent, and symlinked systemd units have integrity-checked backup/rollback paths; link/security gate checks pass | No runbook step executed on a host | Execute only after explicit authorization | `2026-07-21T14:57:01Z` |
| DEPLOY-001 | Rotate secrets and establish source of truth | `NOT STARTED` | Exposed webhook rotated; one authoritative secret path; owners/dates recorded | No secret values or remote stores accessed | Explicit deployment/secret authority absent | Operator authorizes owner/store and rotates affected URL | `2026-07-21T14:41:18Z` |
| DEPLOY-002 | Backup/diff/checksum host delta | `NOT STARTED` | Reviewed backups, secret-safe diff, checksums, rollback mapping | Runbook only; host not accessed | DEPLOY-001 and target authorization | Inventory and compare reviewed host files | `2026-07-21T14:41:18Z` |
| DEPLOY-003 | Install and validate route secret | `NOT STARTED` | Directory `0700`, regular file `0600`, exact enabled routes, redacted preflight | Example/preflight locally verified; no host secret installed | DEPLOY-001–002 and target authorization | Install atomically and read back metadata | `2026-07-21T14:41:18Z` |
| DEPLOY-004 | Activate Monitor and freshness timer | `NOT STARTED` | Reviewed deltas installed; daemon reload; Monitor/timer enabled and active | Production remains at point-in-time disabled handoff state | All local gates and explicit go/no-go | Activate through reviewed runbook | `2026-07-21T14:41:18Z` |
| DEPLOY-005 | Runtime and delivery evidence | `NOT STARTED` | Checkpoint, heartbeat, logs, watchdog, timer, and controlled destination proofs | No target runtime or destination evidence | DEPLOY-004 and destination authorization | Capture redacted runtime matrix | `2026-07-21T14:41:18Z` |
| DEPLOY-006 | Rollback verification | `NOT STARTED` | Stop/disable freshness, restore reviewed files, recreate Monitor, align watchdog, preserve logs/state, read back | Runbook contract only; no host rollback proof | Backups, non-compromised secrets, rollback owner | Execute a controlled authorized rollback test | `2026-07-21T14:41:18Z` |

## Verification matrix

Current evidence is from the local macOS worktree unless explicitly identified as a runtime check. The full command record is in [Validation evidence](#validation-evidence).

| Verification area | Required proof | Planned work | Current evidence |
|---|---|---|---|
| Existing handler/config suite | Existing suite remains green | TEST-002 | Immutable baseline `54/54`; reorganized current runner `33/33` |
| Existing quickstart suite | Existing suite remains green | TEST-004 | Immutable baseline `39/39`; expanded current suite `42/42` |
| New route/config suites | All new focused suites pass | TEST-001–004 | Full discovery `113/113`; focused route `16/16`, dispute `15/15`, freshness `33/33`, delivery `5/5`, ops `2/2` |
| Exact route synchronization | Exact 15 keys equal all enabled target producers; paused smoke optional | ROUTE-004, TEST-001 | Passed deterministic synchronization assertions |
| Duplicate handling | Duplicate JSON keys rejected; duplicate URL values accepted | ROUTE-002, TEST-001 | Passed duplicate-key/value cases |
| Secret file safety | Directory `0700`; file `0600`; regular file; symlink rejected | ROUTE-002, TEST-001, DEPLOY-003 | Loader and installer gates pass locally; target metadata not read |
| Read-only directory mount | Directory is mounted read-only and atomic replacement is visible | OPS-001, TEST-004 | Compose renders the read-only directory mount; container behavior blocked by Docker socket |
| Missing/invalid route | Local log plus failure; no env or other fallback | ROUTE-003, TEST-001 | Passed route and delivery-integration cases |
| Redaction | No URLs/fragments in preflight, logs, test output, or artifacts | ROUTE-004, TEST-001 | Preflight/output and repository secret-pattern scans pass |
| Log-only replay | Explicit mode and zero HTTP calls | ROUTE-003, TEST-001, TEST-005 | Passed explicit log-only/zero-HTTP tests |
| SpotPrice threshold | Exactly 20% normal; greater than 20% potential dispute | DISPUTE-003, TEST-002 | Boundary cases pass |
| EVMCall history | Match, mismatch, missing archive, no exact block, RPC/reorg ambiguity | DISPUTE-004, TEST-002 | Deterministic cases pass; live archive providers unverified |
| Report outcomes | All four labels; one outcome/delivery attempt per event | DISPUTE-002, TEST-002 | All four outcomes and one-delivery contract pass |
| Malformed known reports | Alarm emitted; handler does not lose the report | DISPUTE-006, TEST-002 | Negative fixtures pass |
| Calendar boundaries | UTC rollover, leap years, every month length, inclusive cutoffs | FRESH-003–004, TEST-003 | Calendar and post-cutoff-dispute regressions pass |
| Confirmation delay | Deadline evaluated only at 12-confirmed cutoff block | FRESH-001, TEST-003 | Confirmed-height gating passes |
| RPC health | Fallback, total failure, threshold, warning, recovery, no false absence | FRESH-006, TEST-003 | Deterministic provider and zero-provider transitions pass; live RPC unverified |
| State safety | Atomic write, corrupt preservation, bootstrap, restart dedup, delivery retry | FRESH-005–006, TEST-003 | State and retry cases pass |
| Confirmed NewReport replay | Known confirmed event in explicit log-only mode | TEST-005 | Confirmed event fixture passes direct handler replay; Monitor-image replay blocked |
| Runtime readback | systemd/container/timer/checkpoint/heartbeat/logs/watchdog | DEPLOY-004–005 | Not run; no host authorization |
| Destination proof | One controlled delivery per distinct destination, URL never recorded | DEPLOY-005 | Not run; no destination authorization or route secret |

## Deployment gates

Deployment is forbidden until all applicable local acceptance rows are evidenced and an operator explicitly authorizes deployment.

1. Obtain explicit deployment authorization with target host/account and rollback owner confirmed.
2. Rotate the exposed webhook if still in use and establish one secret source of truth.
3. Back up, diff, and checksum installed files without printing or storing secret values.
4. Copy only reviewed deltas. Never rerun `migration/scripts/tellor-ops-setup.sh` wholesale.
5. Install the route directory and file with the required modes and file type.
6. Run the redacted route preflight.
7. Run local, config, and explicit log-only replay verification.
8. Enable/reload the Monitor and freshness timer.
9. Verify checkpoints, heartbeat, logs, watchdog behavior, and controlled delivery.

## Rollback contract

Rollback must:

1. Stop and disable the freshness timer/service.
2. Restore only reviewed, checksummed backups and recreate the Monitor container so configuration/env changes take effect.
3. Preserve logs, freshness state, corrupt-state captures, checkpoints, and failed artifacts for diagnosis.
4. Never restore a compromised webhook or secret backup.
5. Align watchdog desired state with Monitor desired state so an intentional stop is not treated as an incident.
6. Record complete systemd, timer, container, checkpoint, heartbeat, log, and route-preflight readback.

## Runtime snapshots

Append new observations; do not edit or delete older rows.

| Snapshot UTC | Environment | Observation | Evidence boundary |
|---|---|---|---|
| `2026-07-21 04:52–04:59` | tellor-ops EC2 | Monitor disabled/inactive; container exited 137 after stop timeout; watchdog enabled/noisy; webhook exposed in chat | Point-in-time handoff; not re-read during DOC-001 |
| `2026-07-21T14:41:18Z` | Local macOS worktree | Version `0.2` implementation and deterministic verification completed; no EC2, live destination, or secret store accessed | Local evidence only; does not update the earlier production snapshot |
| `2026-07-21T14:54:03Z` | Local macOS worktree | Final `0.2.1` verification completed after provenance and rollback-review fixes | Supersedes only the earlier local verification checkpoint; production state remains unchanged and unknown beyond the handoff |
| `2026-07-21T14:57:01Z` | Local macOS worktree | Final `0.2.2` documentation verification completed after systemd-symlink integrity review | Supersedes earlier local verification checkpoints; production state remains unchanged and unknown beyond the handoff |

## Decision and deviation ledger

Append decisions, deviations, blockers, and superseded proposals. Never delete old entries.

| Recorded UTC | ID | Type | Decision / deviation | Rationale | Supersedes / follow-up |
|---|---|---|---|---|---|
| `2026-07-21T06:13:12Z` | DEC-001 | Decision | Plan version `0.1` is document-only; only DOC-001 may advance | Preserve the implementation/deployment authorization boundary | None |
| `2026-07-21T06:13:12Z` | DEC-002 | Decision | Per-monitor route names are exact and case-sensitive; URLs may repeat | Stable synchronization and least-surprise routing | Implement in ROUTE-001–004 |
| `2026-07-21T06:13:12Z` | DEC-003 | Operator policy | AMPL uses an inclusive 00:00–00:30 UTC daily window | User/operator-defined deadline | Validate in FRESH-003/TEST-003 |
| `2026-07-21T06:13:12Z` | DEC-004 | Operator policy | USPCE checks at 00:00 UTC on the final calendar day for a report since month start | User-selected monthly policy | Validate in FRESH-004/TEST-003 |
| `2026-07-21T06:13:12Z` | DEC-005 | Evidence boundary | Telliot zero-result behavior must be version-pinned during DISPUTE-004 | Upstream reference behavior can change; this report does not implement it | Record pinned commit and fixtures later |
| `2026-07-21T06:13:12Z` | DEV-001 | Blocker | Monitor image config check could not run because the local Docker socket is unavailable | Docker daemon/socket absent locally | Rerun under TEST-004 with a working daemon |
| `2026-07-21T14:41:18Z` | DEC-006 | Decision | User authorization advanced plan `0.2` through local implementation, testing, and documentation only | The request explicitly required actual changes but supplied no target host/account, secret authority, destination authority, or rollback owner | Supersedes DEC-001 only for local work; deployment boundary remains |
| `2026-07-21T14:41:18Z` | DEC-007 | Decision | Production delivery requires an explicit `live`/`log-only` mode and exact per-name route; retired shared webhook variables never serve as fallback | Fail-closed routing prevents cross-destination leakage and makes replay behavior explicit | Implemented by ROUTE-001–004 |
| `2026-07-21T14:41:18Z` | DEC-008 | Evidence boundary | Tellor dataSpecs, TellorFlex, and Telliot behavior are pinned in `MONITORING_SOURCE_MAP.md` | Reproducible query/event/zero-result semantics are required for dispute decisions | Replaces DEC-005 follow-up; update pins through the documented review rule |
| `2026-07-21T14:41:18Z` | DEC-009 | Decision | Calendar report lookup uses the current 12-confirmed evaluation block while the cutoff block gates eligibility | Disputes opened after the cutoff but before evaluation must remain visible | Covered by post-cutoff-dispute regression |
| `2026-07-21T14:41:18Z` | DEV-002 | Blocker | Docker-image validation and mount/replacement behavior remain unavailable locally | `/Users/df/.docker/run/docker.sock` is absent | Complete TEST-004 with a working Docker daemon |
| `2026-07-21T14:41:18Z` | DEV-003 | Blocker | Live systemd, RPC, EC2, destination, activation, and rollback evidence was not collected | Required authorization and target environment were not supplied | Complete TEST-005 and DEPLOY-001–006 through the reviewed runbook |
| `2026-07-21T14:54:03Z` | DEC-010 | Review resolution | Final review aligned empty EVMCall calldata with pinned Telliot, split Monitor/host Python provenance, and added exact backup/restore-or-remove handling for all four systemd units | These issues could otherwise create unsupported dispute conclusions or an incomplete rollback | Verified in plan version `0.2.1` |
| `2026-07-21T14:54:03Z` | DEV-004 | Evidence correction | The `14:41:18Z` local checkpoint preceded final provenance/runbook fixes and is not the final verification timestamp | Append-only history is preserved while current metadata points to the later clean checkpoint | Superseded for final local evidence by `14:54:03Z` |
| `2026-07-21T14:57:01Z` | DEC-011 | Review resolution | Systemd unit rollback records absent units, regular files, and symlinks; symlink targets are stored in checksummed regular records and type/target-checked before restore | A substituted backup object must not bypass integrity validation or be restored | Final read-only reviewer confirmed the targeted defect resolved |
| `2026-07-21T14:57:01Z` | DEV-005 | Evidence correction | The `14:54:03Z` checkpoint preceded the final symlink-integrity fix | Preserve the audit trail while identifying the actual clean checkpoint | Superseded for final local evidence by `14:57:01Z` |

## Change log

Append entries; record superseded material rather than removing it.

| Version | Changed UTC | Status | Change | Validation |
|---|---|---|---|---|
| `0.1` | `2026-07-21T06:13:12Z` | `LOCALLY VERIFIED` | Created authoritative baseline, target interfaces, full work ledger, verification matrix, deployment/rollback gates, ledgers, and Major-tier provenance | Full readback; required-name/ID/ledger checks; local link check; secret-pattern scan; Git status comparison |
| `0.2` | `2026-07-21T14:41:18Z` | `LOCALLY VERIFIED` | Implemented routing, unified report classification, freshness/state, watchdog, operations artifacts, deterministic tests, operator docs, pinned source map, and deployment/rollback runbook; preserved deployment as an explicit future gate | `113/113` discovery, focused suites, handler/config runner, Compose render, compilation, shell syntax, JSON parse, links, secret scan, and diff checks |
| `0.2.1` | `2026-07-21T14:54:03Z` | `LOCALLY VERIFIED` | Resolved final provenance and rollback findings; refreshed complete safe-verification commands and final evidence timestamp | `113/113` discovery; `33/33` handler/config; `42/42` quickstart; `71/71` focused; compilation, shell, JSON, Compose, links, secret, ledger/catalog, runbook, whitespace, and diff checks passed |
| `0.2.2` | `2026-07-21T14:57:01Z` | `LOCALLY VERIFIED` | Closed final systemd-unit rollback integrity gap for preserved symlinks and recorded the reviewer-confirmed clean checkpoint | Targeted reviewer resolved; links, secret/whitespace, ledger/catalog, runbook-integrity, and diff checks passed after the documentation-only fix |

## Provenance

### Source map

[`MONITORING_SOURCE_MAP.md`](MONITORING_SOURCE_MAP.md) is the maintained version map. It pins:

- [TellorFlex `e2946ecc12b22e72e63bec6f298d31ff22967d5c`](https://github.com/tellor-io/tellorFlex/tree/e2946ecc12b22e72e63bec6f298d31ff22967d5c) for the exact `NewReport` event and strict-before/dispute-skipping behavior.
- [Tellor dataSpecs `8c7225defb04bf5aaf18080a33bfaa3feba23765`](https://github.com/tellor-io/dataSpecs/tree/8c7225defb04bf5aaf18080a33bfaa3feba23765) for the query and response encodings used by structural and EVMCall validation.
- [Telliot feeds v0.4.20 / `fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2`](https://github.com/tellor-io/telliot-feeds/tree/fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2) for EVMCall reporter zero-result behavior.
- OpenZeppelin Monitor `v1.5.0` at the image digest declared in [`docker-compose.yaml`](docker-compose.yaml).

Primary local implementation evidence:

- [`config/discord_webhooks.example.json`](config/discord_webhooks.example.json), [`config/triggers/scripts/discord_routes.py`](config/triggers/scripts/discord_routes.py), [`config/triggers/scripts/tellor_lib.py`](config/triggers/scripts/tellor_lib.py), and [`quickstart.py`](quickstart.py) implement the route schema, secure loader, delivery adapter, explicit mode, and redacted preflight.
- [`config/monitors/tellorflex_disputable_value.json`](config/monitors/tellorflex_disputable_value.json), [`config/triggers/scripts/handlers.py`](config/triggers/scripts/handlers.py), and [`tests/fixtures/new_report_eth_usd_block_25526730.json`](tests/fixtures/new_report_eth_usd_block_25526730.json) implement and exercise the unified report path.
- [`report_freshness.py`](report_freshness.py), [`ops/monitor_watchdog.py`](ops/monitor_watchdog.py), and [`ops/systemd/`](ops/systemd/) implement confirmed freshness, durable state, health transitions, watchdog behavior, and gated services.
- [`MIGRATION.md`](MIGRATION.md), [`AWS_DOCKER_OPERATIONS.md`](AWS_DOCKER_OPERATIONS.md), and [`../README.md`](../README.md) document current local behavior, safe verification, authorization gates, deployment, readback, and rollback.
- The immutable baseline and [`TELLOR_OPS_EC2_HANDOFF.md`](TELLOR_OPS_EC2_HANDOFF.md) remain the only production-state evidence; no newer remote readback occurred.

### Changed claims

- Preserved the complete immutable `0.1` repository and EC2 baseline while advancing current metadata and append-only ledgers to `0.2`.
- Implemented and locally verified exact per-monitor routing without a shared production fallback.
- Replaced overlapping `submitValue` monitoring with one indexed `NewReport` classifier that emits one of four outcomes.
- Implemented confirmed ETH/AMPL/USPCE freshness, durable state/retry/health behavior, and a desired-state-aware watchdog with checkpoint regression detection.
- Added gated Compose/systemd/installer artifacts and current operator/deployment/rollback documentation without claiming host installation.
- Reconciled all 33 work-ledger rows against direct local evidence and explicit runtime blockers.

### Assumptions and operator policies

- This progress plan is the approved local policy source for the 15 route names, freshness thresholds/windows, target chain list, state paths, and delivery outcomes.
- Production URL values, route assignments, owners, and current secret-rotation state were intentionally not read.
- The EC2 handoff remains point-in-time evidence until an authorized live readback appends a newer snapshot.
- Trusted medians are operational evidence, not protocol truth; time alignment and source availability must be visible in outcomes.
- Historical EVMCall conclusions require archive-capable RPC data with unambiguous timestamp/block/hash resolution; ambiguous evidence remains `NOT VERIFIED`.
- Updating an upstream pin, query ID, contract, Monitor image, or RPC policy requires source-map review and the documented verification gates.

### Validation evidence

Final local verification session: `2026-07-21T14:57:01Z`, macOS worktree on branch `monitor-migration`. This clean checkpoint supersedes the earlier local checkpoints after the final provenance and systemd-rollback integrity fixes.

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s migration/tests -p 'test_*.py'` → `113/113` passed.
- `PYTHONDONTWRITEBYTECODE=1 python3 migration/tests/run_tests.py` → `33 passed, 0 failed`.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest migration/tests/test_quickstart.py` → `42/42` passed.
- Focused route, dispute, freshness, delivery-integration, and operations-hardening suites → `16/16`, `15/15`, `33/33`, `5/5`, and `2/2` passed.
- Python compilation with a sandbox-local bytecode cache and `bash -n migration/ops/install-monitoring.sh` → passed.
- All `migration/config/**/*.json` parsed; `docker compose -f migration/docker-compose.yaml config --quiet` → passed.
- Local Markdown links, exact ledger/route/catalog assertions, secret-pattern scan, and `git diff --check` → passed.
- `python3 migration/quickstart.py check` remains blocked because `/Users/df/.docker/run/docker.sock` is absent; it was not repeated unchanged after the blocker was classified.

### Residual risks

- No Docker-backed Monitor v1.5.0 configuration validation or container mount/atomic-replacement proof ran.
- No live archive RPC, Linux systemd, container, timer, heartbeat, checkpoint, EC2, Discord destination, activation, or rollback evidence exists.
- Current production/runtime state may differ from the 2026-07-21 handoff.
- The exposed webhook's rotation state and the authoritative secret store remain unknown.
- Historical EVMCall behavior can only be as reliable as target archive retention and timestamp/block/hash stability.
- Static macOS checks do not substitute for `systemd-analyze verify`, service-user ownership/readability, or target-host readback.
- OPS-001/OPS-002, TEST-004/TEST-005, and DEPLOY-001–006 remain open at the runtime/authorization boundary.
