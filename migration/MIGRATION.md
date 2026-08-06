The file below documents the older candidate implementation. It remains in the repository as migration evidence. It is not the final Tellor monitor runtime and it is not deployment guidance. For the final local M1-M11 alert-only design and validation-first deployment procedure, read `migration/production/README.md`.

# Defender Autotasks → OpenZeppelin Monitor Migration

This directory contains the locally implemented Tellor replacement for legacy
OpenZeppelin Defender autotasks. It runs the content-pinned
`openzeppelin/openzeppelin-monitor:v1.5.0` image and uses Python standard-library
scripts for classification, routing, freshness checks, and watchdog alerts.

## Status and authority boundary

| Item | Current repository state |
|---|---|
| Monitor configuration | Nine enabled Ethereum-mainnet monitors; one paused smoke monitor |
| Report monitoring | One indexed `NewReport` monitor and one four-outcome classifier |
| Discord delivery | Per-producer secure route file; explicit `live` or `log-only` |
| Freshness/watchdog | Implemented locally with systemd unit templates; not proof of host installation |
| Production | **Stopped boundary** — the latest dated handoff says Monitor was disabled/inactive |

Local tests and configuration files do not prove that EC2 contains these files,
that a systemd timer is enabled, or that a destination received an alert.
Deployment remains forbidden until an authorized operator completes the gates
in [`AWS_DOCKER_OPERATIONS.md`](AWS_DOCKER_OPERATIONS.md).

OpenZeppelin labels Monitor alpha software. The image is content-pinned, but it
still requires staged activation, checkpoint monitoring, backups, and a tested
rollback.

## Runtime layout

```text
migration/
├── config/
│   ├── discord_webhooks.example.json   exact 15-name route template
│   ├── monitors/                       ten production monitor JSON files
│   ├── networks/                       Ethereum mainnet plus inactive Sepolia config
│   └── triggers/scripts/               dispatch, classifier, route, and helpers
├── data/                               persistent Monitor checkpoints (gitignored)
├── logs/                               Monitor alert log (gitignored)
├── ops/                                additive installer, watchdog, systemd units
├── report_freshness.py                 confirmed-report freshness checker
├── docker-compose.yaml                 content-pinned Monitor service
├── quickstart.py                       validation/operator/custom-monitor CLI
└── tests/                              deterministic local suites and fixtures
```

The Monitor watches only `ethereum_mainnet`, polls once per minute, and waits
12 confirmations. With a 12-second block time, a normal alert is expected
roughly 2.4–3.4 minutes after inclusion, plus RPC or recovery delay.

## Current Monitor catalog

These are configuration states in the local worktree, not runtime states:

| Monitor name | State | Match and retained alert |
|---|---|---|
| `TellorFlex Disputable Value` | Enabled | Exact indexed `NewReport(bytes32,uint256,bytes,uint256,bytes,address)` event; one canonical report outcome |
| `Tellor Address Updates` | Enabled | `updateStakeAmount()` call |
| `Tellor Staking` | Enabled | `NewStaker`, `StakeWithdrawRequested`, `StakeWithdrawn` events |
| `Tellor Token Bridge` | Enabled | staking-reward, extra-withdraw claim, pause, and unpause calls |
| `Tellor Deposit To Layer` | Enabled | `depositToLayer` call |
| `Tellor Withdraw From Layer` | Enabled | `Withdraw` event |
| `Update Oracle Data Calls` | Enabled | `updateOracleData` call |
| `Update Validator Set Calls` | Enabled | `updateValidatorSet` call |
| `Guardian Reset Validator Set Calls` | Enabled | `GuardianResetValidatorSet` event |
| `Smoke Test USDC Transfer` | **Paused** | USDC transfer above the configured threshold; replay/pipeline proof only |

The old `TellorFlex Data Report`, `Tellor DVM Price Deviation`, and `Tellor
EVMCall Validation` configurations are retired. `submitValue` is no longer part
of `Tellor Address Updates`; the `NewReport` event is the single report path.

### Four report outcomes

Every `NewReport` match produces exactly one label and one delivery attempt:

| Label | Meaning |
|---|---|
| `✅ Looks normal` | A known report is structurally valid and the implemented evidence check passes. SpotPrice is normal at exactly 20% difference; the alarm threshold is strictly greater than 20%. |
| `👀 Report received` | A valid unknown query type was observed. The alert makes no correctness claim. |
| `⚠️ NOT VERIFIED` | Evidence is unavailable or ambiguous: no usable trusted price, missing archive RPC/block, timestamp ambiguity, reorganization ambiguity, unsupported chain, or similar uncertainty. |
| `🚨 POTENTIAL DISPUTE` | Trusted SpotPrice difference is strictly greater than 20%, historical EVMCall bytes mismatch, or a known report/envelope is malformed. |

These labels are operator evidence, not protocol adjudication. Trusted-price
medians can be time-misaligned, and a potential-dispute alert still requires
human review.

EVMCall validation covers chain IDs `1`, `10`, `100`, `137`, `10200`, and
`11155111`. It resolves the response timestamp to a historical target-chain
block, verifies the block is unambiguous, and replays at that block. Ambiguity
is `NOT VERIFIED`, never a false mismatch.

## Per-producer Discord routes

Production delivery does not use a shared `DISCORD_WEBHOOK_URL`. The live file
is `secrets/discord_webhooks.json`, selected through
`DISCORD_WEBHOOKS_FILE`. It is one JSON object from exact, case-sensitive names
to Discord webhook URLs.

| Route name | Producer | Requirement |
|---|---|---|
| `TellorFlex Disputable Value` | Monitor | Required |
| `Tellor Address Updates` | Monitor | Required |
| `Tellor Staking` | Monitor | Required |
| `Tellor Token Bridge` | Monitor | Required |
| `Tellor Deposit To Layer` | Monitor | Required |
| `Tellor Withdraw From Layer` | Monitor | Required |
| `Update Oracle Data Calls` | Monitor | Required |
| `Update Validator Set Calls` | Monitor | Required |
| `Guardian Reset Validator Set Calls` | Monitor | Required |
| `Smoke Test USDC Transfer` | Paused Monitor | Optional while paused; required if enabled |
| `Tellor ETH/USD Freshness` | Freshness checker | Required |
| `Tellor AMPL/USD Freshness` | Freshness checker | Required |
| `Tellor USPCE Freshness` | Freshness checker | Required |
| `Tellor Freshness Checker Health` | Freshness checker | Required |
| `Tellor Monitor Watchdog` | Watchdog | Required |

Repeated URL values are allowed. Duplicate JSON keys are rejected. Unknown
extra names produce a redacted warning. Each live delivery reopens the file, so
an atomic replacement is visible through the read-only **directory** mount
without restarting the container.

Security requirements:

- `secrets/` is a real, non-symlink directory with mode `0700`.
- `discord_webhooks.json` is a regular, non-symlink file with mode `0600`.
- URLs must be valid HTTPS `discord.com` webhook URLs.
- Missing mode, unsafe file metadata, invalid JSON/URL, or a missing enabled
  route fails closed. The alert remains locally logged; there is no environment
  or shared-webhook fallback.
- Errors and preflight output contain route names/statuses, never URLs or URL
  fragments.

### Create and validate a local route file

Do not put real URLs in shell arguments, chat, tickets, screenshots, or Git.
Use an approved secret editor or secret-manager workflow.

```sh
cd migration
install -d -m 0700 secrets
test ! -e secrets/discord_webhooks.json
install -m 0600 config/discord_webhooks.example.json \
  secrets/discord_webhooks.json
# Fill values using a secret-safe editor, then reassert modes:
chmod 0700 secrets
chmod 0600 secrets/discord_webhooks.json
python3 quickstart.py check-discord-routes
```

The preflight validates the file, exact configured monitor names, enabled/pause
states, and route coverage. It prints only names and statuses. A nonzero exit is
a deployment blocker.

For rotation, prepare a secure `0600` sibling file, preflight it by setting
`DISCORD_WEBHOOKS_FILE` to that path, and rename it over the live file on the
same filesystem. Do not edit the live file in place.

### Delivery modes

`TELLOR_ALERT_DELIVERY_MODE` accepts only:

- `live`: local log plus confirmed Discord POST (`wait=true`) with bounded
  retries; any final failure is raised.
- `log-only`: local log and zero Discord HTTP calls.

Replay must explicitly use `log-only`. `docker-compose.yaml` sets the
long-running Monitor service to `live`; the quickstart replay command overrides
it to `log-only` unless `--send` is explicitly confirmed. The generated custom
monitor remains isolated on `CUSTOM_DISCORD_WEBHOOK_URL`.

There is no durable outbox. After Discord retry exhaustion, the local record and
service error are the recovery evidence. Operators decide whether and how to
resend.

## Confirmed-report freshness

`report_freshness.py` queries the TellorFlex contract through the configured
Ethereum-mainnet RPCs. A provider must return a complete, current, internally
consistent snapshot; otherwise the checker tries the next provider. It uses the
12-confirmed block and `getDataBefore(cutoff + 1)` so inclusive deadlines are
correct even though the contract lookup is strictly before its argument.

| Check | Policy | Alert behavior |
|---|---|---|
| ETH/USD | Confirmed report age strictly greater than 14,400 seconds | One alarm per incident and one recovery |
| AMPL/USD | At least one report from `00:00:00` through `00:30:00 UTC`, inclusive | One immutable missed-date alert |
| USPCE | At final-calendar-day `00:00:00 UTC`, require a report since month start | One immutable missed-month alert |
| RPC health | Three consecutive complete provider failures | One health warning; one recovery after a complete success; missing-report alarms suppressed during failure |

The minute timer runs `tellor-report-freshness.service` as the unprivileged
`tellor-monitoring` user. Important files are:

| Path | Purpose |
|---|---|
| `/var/lib/tellor/report-freshness/state.json` | Incident and immutable-period deduplication state |
| `/var/lib/tellor/report-freshness/heartbeat` | Atomic JSON heartbeat: timestamp, status, confirmed block |
| `/var/lib/tellor/report-freshness/alerts.log` | Local JSON-lines delivery evidence |

State is atomically replaced. Invalid prior state is preserved with a
`.corrupt.<timestamp>.<pid>` suffix, then the checker bootstraps only the latest
eligible period. State does not advance after failed delivery, so the
transition remains pending for a later timer run.

## Monitor watchdog

`monitor-watchdog.timer` runs every five minutes. Its service runs as root only
because Docker-socket access is root-equivalent; it does not grant Docker-group
membership to the freshness user.

The watchdog first reads the desired state of `openzeppelin-monitor.service`:

- If the unit is intentionally disabled, it clears stale incident state and is
  quiet.
- If enabled, it checks that the Compose container exists and is running,
  honors a Docker health status if one exists, and checks that
  `data/ethereum_mainnet_last_block.txt` is regular, readable, recent, and
  advancing. The default stale/no-advance threshold is 600 seconds.
- It sends one warning when an incident opens and one recovery when checks pass
  again.

Watchdog state and local alerts live under
`/var/lib/tellor/monitor-watchdog/`. Corrupt state is preserved before a clean
bootstrap. A delivery failure prevents the incident transition from being
committed, allowing the next run to retry.

## Ownership and operating responsibilities

| Owner | Responsibility |
|---|---|
| Code/config maintainer | Monitor JSON, classifier, route registry, tests, pinned versions, reviewed changes |
| Secret owner | Destination ownership, webhook rotation, authoritative secret store, route-file installation without disclosure |
| RPC owner | Archive retention, chain-ID correctness, quotas, fallback health, rotation of exposed credentials |
| Host operator | Backups, checksums, systemd/Compose activation, state permissions, checkpoint/heartbeat readback, rollback |
| Alert responder | Triage `POTENTIAL DISPUTE`, `NOT VERIFIED`, freshness, and watchdog incidents; record outcomes |
| Deployment approver | Target account/host, maintenance window, go/no-go, rollback owner, controlled-delivery authorization |

No single role should infer deployment authorization from a passing local test.

## Failure modes

| Failure | Observable behavior | Operator action |
|---|---|---|
| Route missing, malformed, symlinked, or wrong mode | Preflight/delivery fails without fallback; safe error only | Repair through an atomic secret-manager install; rerun preflight |
| Discord HTTP retries exhausted | Local alert remains; producer exits/fails transition | Check destination ownership/status; retry only under incident policy |
| Trusted SpotPrice unavailable | `NOT VERIFIED` | Restore sources; do not treat as a normal report |
| Historical EVMCall unavailable or ambiguous | `NOT VERIFIED` | Restore archive RPC and investigate block/timestamp ambiguity |
| Malformed known report | `POTENTIAL DISPUTE` alarm rather than a lost handler | Review raw transaction/report evidence |
| All freshness RPCs fail | Missing-report alarms suppressed; third failure opens health incident | Restore at least one complete provider; verify recovery |
| Freshness delivery fails | State transition remains pending; heartbeat says `delivery-failure` | Repair route/destination and allow retry |
| Corrupt freshness/watchdog state | Original preserved; latest period bootstrapped | Preserve artifact, diagnose, verify deduplication |
| Monitor disabled intentionally | Watchdog quiet | Record desired state; do not interpret as healthy runtime |
| Container down or checkpoint stale | One watchdog warning, then deduplicated until recovery | Inspect service/container/RPC/checkpoint before restart |

## Custom function quickstart

This path generates one gitignored Ethereum-mainnet top-level function monitor.
It does not trace internal calls or observe view/off-chain `eth_call` reads.

Prerequisites: Python 3.9+, Docker Compose v2, a running Docker daemon for image
checks, an Ethereum-mainnet HTTP RPC, and—only for live custom delivery—a
Discord incoming webhook.

```sh
cd migration
python3 quickstart.py configure
```

The configurator accepts a contract address, function name or canonical
signature, and message template. A bare function name is allowed only when
Sourcify returns one unambiguous ABI entry. Proxy/implementation signatures
absent from the fetched ABI require explicit `--allow-unknown-signature` and a
known-block replay.

`--offline` skips Sourcify and RPC checks; it does not itself mean log-only.
Use `--log-only` to configure without the custom webhook. Neither option can be
combined with `--start`.

Useful placeholders:

| Placeholder | Value |
|---|---|
| `${monitor.name}` | Custom monitor name |
| `${network.slug}` | `ethereum_mainnet` |
| `${transaction.hash}` | Transaction hash |
| `${transaction.from}` / `${transaction.to}` | Top-level addresses |
| `${transaction.value}` | Raw transaction value in wei |
| `${observed.utc}` | Alert-format time |
| `${functions.0.signature}` | Canonical matched signature |
| `${functions}` | Signature and decoded arguments |
| `${functions.0.args.<name>}` | One decoded argument |

Common read-only/local operations:

```sh
python3 quickstart.py check
python3 quickstart.py check-rpcs
python3 quickstart.py check-discord-routes
python3 quickstart.py status
python3 quickstart.py logs
```

Safe historical replay explicitly disables Discord:

```sh
# Example after configuring WETH deposit():
python3 quickstart.py replay 17344627
```

`replay --send` is a real external write and requires explicit confirmation and
an authorized disposable/test destination. Do not use it as a local verifier.

## Safe local verification

Run from `migration/`:

```sh
python3 -m unittest tests/test_discord_routes.py
python3 -m unittest tests/test_delivery_integration.py
python3 -m unittest tests/test_dispute.py
python3 -m unittest tests/test_report_freshness.py
python3 -m unittest tests/test_monitoring_ops_hardening.py
python3 -m unittest tests/test_quickstart.py
python3 tests/run_tests.py
docker compose config --quiet
```

With a working Docker daemon:

```sh
python3 quickstart.py check
```

Do not run `check-discord-routes` against the example file: its intentionally
empty values must fail. Use a securely installed route file. Do not start a
container, enable a timer, or send a test webhook merely to make local checks
green.

## Deployment and rollback

Use [`AWS_DOCKER_OPERATIONS.md`](AWS_DOCKER_OPERATIONS.md). It contains the
authorization, secret-rotation, backup/diff/checksum, additive installation,
activation, readback, controlled-delivery, and rollback templates. Never rerun
the legacy `scripts/tellor-ops-setup.sh` wholesale.

## Provenance

- **Source map:** [`MONITORING_SOURCE_MAP.md`](MONITORING_SOURCE_MAP.md) pins
  OpenZeppelin Monitor v1.5.0, TellorFlex, Tellor data specifications, and
  Telliot v0.4.20. `TELLOR_MONITORING_PROGRESS.md` is the local policy/evidence
  ledger; neither file authorizes deployment.
- **Changed claims:** stale “activated,” 11-monitor, shared-webhook,
  function-based DVM/EVMCall, and `TellorFlex Data Report` wording was replaced
  with the stopped-production boundary, 9-enabled-plus-paused-smoke catalog,
  indexed `NewReport` classifier, secure routes, freshness, and watchdog
  behavior.
- **Assumptions:** the dated EC2 handoff has not been re-read from the host;
  production supplies correctly owned secrets and archive-capable RPCs only
  after authorization.
- **Validation:** for this documentation pass, local source/config readback,
  Markdown-link validation, whitespace validation, and a secret-pattern scan.
  The deterministic commands above remain operator gates; no host activation
  or destination delivery is claimed.
- **Residual risks:** Monitor is alpha; no durable Discord outbox exists;
  trusted sources and archive RPCs can fail; host permissions and current EC2
  state require deployment-time readback; classification remains evidence for
  human review rather than protocol truth.
