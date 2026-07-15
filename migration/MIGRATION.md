# Defender Autotasks → OpenZeppelin Monitor Migration

Self-hosted replacement candidate for the sunset Defender Monitor. Runs the **official
`openzeppelin/openzeppelin-monitor:v1.5.0` image** via Docker Compose; the
matching source checkout lives at `../../openzeppelin-monitor/` (same tag) for
reference. Static migration configs, scripts, tests, and this runbook are
version-controlled here; `.env` and generated quickstart configs are intentionally
local and gitignored.

**Status: production monitor service activated on 2026-07-13.** Eleven
production monitors are configured and each watches only `ethereum_mainnet`.
The former datafeed and price monitors are consolidated as `TellorFlex Data
Report`. The USDC smoke monitor is retained for safe replay but paused, and the
generated quickstart monitor was removed as a duplicate of `Update Oracle Data
Calls`. The activation checklist below remains the source of truth for replay
evidence, contract upgrades, new networks, and credential rotation.

> **Production warning:** OpenZeppelin's official image page labels Monitor
> **alpha** and says production use is at the operator's risk. This Compose file
> content-pins the reviewed v1.5.0 image digest, but the service still needs
> operational monitoring, backups, and a staged rollout.

## Five-minute custom-function quickstart

This is the usable path for a new Ethereum mainnet contract. It does not require
editing Python or JSON.

Prerequisites: Python 3.9+, Docker Compose v2 with the daemon running, an
Ethereum mainnet HTTP RPC URL, and a Discord incoming webhook for a test or
alert channel. Check the local tools first:

```sh
python3 --version
docker compose version
docker info >/dev/null
```

```sh
cd migration
python3 quickstart.py configure
```

The configurator prompts for:

1. A monitor name and Ethereum mainnet contract address.
2. A function name or canonical signature. A bare name such as `deposit` works
   only when Sourcify returns a verified ABI with exactly one overload. If no ABI
   is available, use an exact, case-sensitive signature such as `deposit()` or
   `transfer(address,uint256)`. If a fetched proxy ABI does not contain an
   implementation function, the CLI rejects it by default; manually verify the
   implementation signature and rerun with `--allow-unknown-signature`, then
   prove it against a known historical block before live start.
3. A Discord Markdown message template.
4. The mainnet RPC URL and Discord webhook in hidden prompts.

It checks chain ID 1 and deployed bytecode, seeds the complete `.env.example` on
first use, stores URLs only in gitignored `.env` (mode `0600`), and creates two
gitignored `quickstart_function.json` files. It then offers a confirmed test
message, hardened config validation, and live start. Re-running `configure`
replaces only those generated files; choose start, or run `python3 quickstart.py
start`, to force-recreate the container and load the new cached config.

`--offline` is generation/log-only mode. It never starts live monitoring;
`start` always performs the mainnet checks again.

Available message placeholders:

| Placeholder | Value |
|---|---|
| `${monitor.name}` | Configured monitor name |
| `${network.slug}` | `ethereum_mainnet` |
| `${transaction.hash}` | Matching transaction hash |
| `${transaction.from}` / `${transaction.to}` | Top-level transaction addresses |
| `${transaction.value}` | Raw transaction value in wei |
| `${observed.utc}` | UTC time when the trigger formats the alert |
| `${functions.0.signature}` | Resolved canonical signature |
| `${functions}` | Signature plus all decoded arguments |
| `${functions.0.args.<name>}` | Decoded argument in Monitor's raw/base-unit rendering; the CLI prints names |

Common operations:

```sh
python3 quickstart.py status
python3 quickstart.py logs
python3 quickstart.py test-webhook
python3 quickstart.py check
python3 quickstart.py check-rpcs
python3 quickstart.py start
python3 quickstart.py stop
```

Safe historical proof (Discord disabled; matches go to `logs/alerts.log`). This
2023 block requires an archive/historical-capable RPC; a recent-block-only live
RPC can legitimately reject it.

```sh
# Configure WETH 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
# with deposit(), then:
python3 quickstart.py replay 17344627

# Explicitly allow real Discord posts only with a disposable/test webhook:
python3 quickstart.py replay 17344627 --send
```

Expected log evidence is transaction
`0xb72a2d8f2e804a2cac676d8248b053171ccfabca2594f5887c18108247ba5970`
and the exact configured message. If the RPC reports an archive/history error,
use a dedicated archive endpoint; do not weaken the chain/address checks.

After live start, success means all of the following:

- the CLI prints `Monitor container is running with the generated configuration`;
- `python3 quickstart.py status` shows the service running;
- `data/ethereum_mainnet_last_block.txt` advances after a polling interval; and
- Monitor logs contain no `ERROR` or `Script execution failed`.

Important boundaries:

- Function mode matches **top-level transactions** whose `to` is the watched
  address. A script-side `eth_getTransactionReceipt` check suppresses failed
  calls using one RPC request only after the selector matches. It does not trace
  internal contract-to-contract calls or observe off-chain/view `eth_call`
  reads. Prefer an event monitor when an action can occur through other contracts.
- A canonical signature contains parameter types but not names. When no verified
  ABI is available, generated names are `arg0`, `arg1`, and so on.
- `constructor`, `receive`, and `fallback` are not selector-bearing calls and
  this function quickstart rejects them; use an event or transaction monitor.
- Integer values are raw decimal base units, not token-formatted amounts. Arrays,
  tuples, bytes, and strings use Monitor's decoded string rendering. Dynamic
  argument text is Markdown-escaped and Discord mentions are disabled.
- Mainnet currently waits 12 confirmations and polls once per minute, so a live
  alert normally arrives roughly 2.4–3.4 minutes after inclusion, and longer
  during RPC or recovery delay.
- Expanded messages are capped at Discord's 2,000-character content limit; full
  content is still recorded in `logs/alerts.log`.
- Discord is best-effort: delivery is confirmed with `wait=true` and retried
  three times, but there is no durable outbox. After exhaustion, the full alert
  remains in `logs/alerts.log`; operators must watch Monitor logs for script
  failures and resend operationally if needed.
- `replay --send` executes the real trigger and therefore requires an explicit
  confirmation. Plain `replay` forcibly disables Discord.

## Decisions and updates (2026-07-02; updated 2026-07-10)

| Decision | Choice |
|---|---|
| Notification channel | Tellor handlers use `DISCORD_WEBHOOK_URL`; the generated quickstart monitor is isolated on `CUSTOM_DISCORD_WEBHOOK_URL`. Every script alert is also appended to `logs/alerts.log` (JSON lines). |
| Monitors in scope | All. disputes/bridges/tips have no autotask logic in this repo — need the old Defender sentinel config to port (see below) |
| Script language | **Python 3.12 stdlib** — the official image's node and jq are broken (glibc mismatch), python3 works; stdlib-only means no build/bundle step |
| RPC for Polygon/Optimism/Sepolia | Infura with a NEW key (old project IDs burned) |
| Ethereum mainnet RPC | Use a dedicated archive-capable node for production. `ethereum-rpc.publicnode.com` now rejects historical archive requests without a personal token; `eth-mainnet.public.blastapi.io` was used only for the 2026-07-10 prototype replay. |
| Mumbai | Dropped |
| AWS shape | Single EC2 + Docker Compose (Phase 3) |

## Version and provenance note

OpenZeppelin's public Monitor docs currently point production users to the
v1.3.x docs as the latest stable documentation, while this migration uses the
`openzeppelin/openzeppelin-monitor:v1.5.0` image content-pinned to
`sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635`.
The sibling
`../../openzeppelin-monitor/` checkout is clean at tag `v1.5.0`; the
"Verified v1.5.0 behavior" section below is based on that local source plus
live testing. OpenZeppelin's official Docker Hub page still labels the software
alpha. Re-run all tests, hardened `check`, replay, and live readback before
changing the image or digest.

## Architecture

One script trigger (`tellor_alert`) serves every monitor. It receives the match
JSON on stdin and dispatches **by monitor name** to a formatter that preserves
the legacy filtering/value semantics inside one normalized Discord envelope and
delivers it (Discord + alerts.log):

```
config/
├── networks/            ethereum_mainnet.json, sepolia.json
├── monitors/            one JSON per legacy sentinel (name = dispatch key!)
└── triggers/
    ├── tellor_script.json   the single "tellor_alert" script trigger
    └── scripts/
        ├── alert.py         Tellor entry point (stdin JSON -> dispatch by name)
        ├── generic_alert.py quickstart template renderer + delivery
        ├── handlers.py      one formatter per monitor + HANDLERS map
        └── tellor_lib.py    match parsing, ABI decode, price APIs, Discord, maps
quickstart.py            interactive custom-function configurator and operator CLI
tests/run_tests.py       50 handler/config checks, run inside the Monitor image
tests/test_quickstart.py stdlib unit tests for parsing, generation, and delivery
```

Legacy in-handler filtering (queryId allowlists, 10% deviation gate,
EVMCall-only) lives in the handlers — a handler that returns without sending
drops the match, exactly like an autotask returning no matches. Renaming a
monitor in its JSON breaks dispatch: update `HANDLERS` in handlers.py too.

| Legacy autotask | Monitor | Match | Handler behavior |
|---|---|---|---|
| staking | Tellor Staking | events NewStaker / StakeWithdrawRequested / StakeWithdrawn | format, ÷1e18 |
| tokenBridge | Tellor Token Bridge | fns addStakingRewards / claimExtraWithdraw / pauseBridge / unpauseBridge | per-fn headline |
| DepositToLayer | Tellor Deposit To Layer | fn depositToLayer(uint256,uint256,string) | amounts ÷1e18 |
| WithdrawFromLayer | Tellor Withdraw From Layer | **event** Withdraw(uint256,string,address,uint256) — legacy args match the event, not the struct-heavy fn | deposit details |
| addressUpdates | Tellor Address Updates | fns submitValue + updateStakeAmount() | alert only for the 2 address-report queryIds / updateStakeAmount |
| datafeed + priceMonitor | TellorFlex Data Report | fn submitValue | one `Network`/`Feed`/`Value`/`Trusted`/`Padded`/`Observed` schema for SpotPrice/EVMCall/RNG/unknown reports; tracked SpotPrice feeds use the median of available CG/CMC/CoinCap prices |
| dvm | Tellor DVM Price Deviation | fn submitValue | 16 feeds; alert only ≥10% off reference |
| EVMCall | Tellor EVMCall Validation | fn submitValue | decode, eth_call target chain, alert on mismatch; RPC failure now alerts "NOT VERIFIED" instead of a false "bad EVMCall" |
| — | Smoke Test USDC Transfer | event Transfer > 1M USDC | paused pipeline proof; temporarily unpause only for webhooks-off replay |

## Verified v1.5.0 behavior (from source + live testing)

1. **Script triggers are fire-and-forget**: exit 0 = ok, non-zero = error
   logged, stdout ignored. Scripts receive `{"monitor_match": {"EVM": {...}},
   "args": [...]}` on stdin; `network_slug` and decoded `matched_on_args` are
   included. Scripts must send the notification themselves.
2. **`trigger_conditions` filter scripts** (we don't use any): last stdout line
   `true` = drop match, `false` = keep. Errors/timeouts KEEP the match (fails
   open). They cannot enrich notifications.
3. **Scripts run as `python3 -c "<file content>"`** with cwd `/app`; file
   contents are cached at startup → **config/script changes need a restart**.
   Shared modules import via `sys.path.insert(0, "config/triggers/scripts")`.
4. **The official image's node and jq are broken** (`GLIBC_2.43 not found`,
   Wolfi base). python3 3.12 and bash work. If JS is ever needed, build the
   image locally from `../../openzeppelin-monitor/Dockerfile.production`
   (Alpine — the compose file has a commented build stanza).
5. **Replay mode (`--monitor-path --network --block`) EXECUTES triggers**, not
   just match printing — with either webhook variable set, replaying can post
   real Discord messages. The quickstart command disables its webhook by
   default; manual Tellor replay must explicitly override `DISCORD_WEBHOOK_URL=`.
6. Match combination: `transactions` conditions AND (events OR functions);
   within a category, conditions are OR'd. v1.5 can assume Success without a
   receipt, so function monitor JSON leaves transaction conditions empty and
   both script entry points check `eth_getTransactionReceipt` once after a
   selector match. Failed calls are dropped; events imply success.
7. State: `data/<network>_last_block.txt` checkpoint (always written);
   `recovery_config` in network JSON enables missed-block retry. Mainnet
   recovery is enabled in this migration; Sepolia remains unchanged until it is
   part of activation scope.

## Forced behavior changes vs Defender (all deliberate)

- **Timestamps** are alert-time UTC, not block time (block timestamp isn't in
  the match payload).
- **TellorFlex Data Report**: the overlapping datafeed and price alerts are one
  alert. Failed price APIs drop out of the trusted median; zero available
  sources produce `Trusted: n/a` without suppressing the submitted report.
- **dvm**: reference API/key failures alert as DVM NOT VERIFIED instead of
  crashing the script and losing the match.
- **EVMCall**: RPC failures alert distinctly as NOT VERIFIED (legacy false-"bad
  EVMCall"). Unsupported chainIds (e.g. dropped Mumbai) also alert NOT VERIFIED.
- **addressUpdates** `updateStakeAmount`: legacy printed `undefined` for data
  (args[0] of a no-arg function); now omitted.
- A crashed handler = alert lost with an error in Monitor logs (Defender was
  the same: crashed autotask = no alert). Watch for `Script execution failed`.

## Historical Tellor proof (2026-07-02)

Known basic test contract: **USDC mainnet Transfer events** (`Smoke Test USDC
Transfer` monitor, threshold 1M USDC).

1. **19/19 handler tests** passed in the original 2026-07-02 proof:
   `docker run --rm -v .:/work -w /work --entrypoint python3 openzeppelin/openzeppelin-monitor:v1.5.0 tests/run_tests.py`
2. **`--check`**: all 10 monitors + 2 networks + trigger validate.
3. **Replay** of mainnet block `25446155` (contains a 10,000,000 USDC
   transfer): 2 matches, expression filter applied, script trigger executed.
4. **Live run**: block watcher picked up new blocks and `logs/alerts.log`
   captured real transfers within seconds, e.g.
   `Amount: 10,000,000.00 USDC` tx `0x1283bf73c8a9...` (etherscan-linked,
   formatted by handle_smoke). The smoke monitor is now paused, so it no longer
   emits live USDC transfer alerts.

### Current verification (2026-07-14)

1. **39/39 quickstart tests** pass on host Python 3.9. Coverage includes canonical ABI
   parsing, overload/mismatch guards, fresh `.env` seeding and deduplication,
   secret redaction, zero-exit validation errors, mainnet/replay preflight,
   receipt status, Markdown safety, confirmed webhook payloads, content limits,
   normalized default messages, and rate-limit waiting.
2. **50/50 Tellor handler/config checks** pass on host and in the exact pinned
   Monitor image, including the receipt guard, the shared message-format
   contract, the 11-active-plus-paused-smoke policy, mainnet-only watcher scope,
   and deterministic trusted-price medians. Price-source failure paths remain
   offline-safe.
3. **`python3 quickstart.py check`** validates all generated and legacy configs
   with the content-pinned v1.5.0 image and rejects logged `ERROR` output even
   when upstream returns exit code 0.
4. **Historical function replay**: WETH `deposit()` at block `17344627` matched
   direct transaction
   `0xb72a2d8f2e804a2cac676d8248b053171ccfabca2594f5887c18108247ba5970`;
   the configured message was rendered into `logs/alerts.log` with Discord
   forcibly disabled.
5. **Live log-only start**: the content-pinned container survived the CLI's
   post-start readback and advanced
   `data/ethereum_mainnet_last_block.txt`; it was stopped after verification.
6. A real Discord success requires an operator-supplied webhook. The CLI's
   `test-webhook` command requests `wait=true` and reports success only after
   Discord confirms persistence; no webhook was supplied in this repository
   session, so no real channel was contacted.

## Activation checklist (what Dan provides, per monitor)

Candidate addresses below are from Tellor's own repos on this machine —
**verify before deploying or reloading a changed monitor**.

- [ ] Oracle monitors (staking, address_updates, tellorflex_data_report, dvm,
      evm_call): confirm oracle address per network. Candidate mainnet
      `0x8cFc184c877154a8F9ffE0fe75649dbe5e2DBEbf`, sepolia
      `0xB19584Be015c04cf6CFBF6370Fe94a58b7A38830` (telliot contract_directory).
- [ ] Bridge monitors (token_bridge, deposit_to_layer, withdraw_from_layer):
      confirm bridge address + V1 vs V2 (V2 renames claimExtraWithdraw →
      claimExtraWithdrawByWithdrawId and pauseBridge → proposePauseBridge/
      approvePause; ABIs in `../../layer/evm/artifacts/.../TokenBridgeV2.json`).
      Candidate mainnet V1 `0x5589e306b1920F009979a50B88caE32aecD471E4`
      (bridgewatch config).
- [ ] Confirm every monitor still watches exactly `ethereum_mainnet`; the test
      suite rejects any other watcher network.
- [ ] EVMCall target-chain RPC vars (`RPC_POLYGON`, `RPC_OPTIMISM`,
      `RPC_GNOSIS`, `RPC_CHIADO`, `RPC_SEPOLIA`) only verify submitted EVMCall
      values. They do not make Monitor watch those chains unless matching
      network JSONs are added and monitor `networks` arrays include those slugs.
- [ ] `.env`: coworker's ETH node URL, NEW Infura key, `DISCORD_WEBHOOK_URL`,
      rotated `CMC_PRO_API_KEY` + `EXCHANGERATE_API_KEY` (old keys in git
      history are burned).
- [ ] disputes / bridges / tips: repo only has generic templates — need the old
      Defender sentinel config (contract + events) to port. Alert text would be
      `**Defender Monitor <name> Triggered**` style via a small handler.
- [ ] Before deploying each changed monitor: replay a block with a known event
      and record it in the test log below.

## Runbook

All commands from `autoTasks/migration/`.

```sh
# Never overwrite an existing secret file.
test -e .env || cp .env.example .env
chmod 600 .env
git check-ignore -v .env    # must show the repository .gitignore rule

docker compose pull monitor
# Start only after at least one confirmed monitor is active and replay-proven.
docker compose up -d --force-recreate monitor
docker compose logs -f monitor
tail -f logs/alerts.log     # every alert, as JSON lines
docker compose up -d --force-recreate monitor  # reload cached config/scripts
docker compose down
```

Metrics are intentionally omitted from this short runbook: that profile needs
the sibling Monitor checkout and publishes Grafana/Prometheus ports. Follow the
host firewall and credential steps in `WHAT_I_NEED_TO_DO.md` before enabling it.

Validation / testing:

```sh
# Tellor handler tests (offline-safe; source failures exercise fallback alerts)
docker run --rm -v .:/work -w /work --entrypoint python3 \
    openzeppelin/openzeppelin-monitor@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635 \
    tests/run_tests.py

# v1.5 can log config errors while exiting 0. This wrapper scans its output;
# production monitors stay active on ethereum_mainnet and smoke stays paused.
python3 quickstart.py check

# Safe manual Tellor replay: force both webhooks empty. Production monitors are
# already active. For smoke only, temporarily set paused=false so v1.5 preloads
# its script, then restore paused=true immediately after replay.
docker compose run --rm \
  -e DISCORD_WEBHOOK_URL= -e CUSTOM_DISCORD_WEBHOOK_URL= monitor \
  --monitor-path /app/config/monitors/<name>.json --network <slug> --block <N>
```

For RPC checks, use
[`WHAT_I_NEED_TO_DO.md` Section 9](WHAT_I_NEED_TO_DO.md#9-check-rpc-health-without-printing-secrets),
which prints variable names and chain IDs but never token-bearing URLs. The full
verification sequence is: confirm facts → assert the 11-active-plus-paused-smoke
mainnet-only policy → hardened check → webhooks-off replay → force-recreate and
inspect the runtime. Do not trust raw `docker compose ... --check` exit status
by itself.

### Per-monitor test log

| Monitor | Command | Result |
|---|---|---|
| Smoke Test USDC Transfer | historical command from 2026-07-02 | ✅ 2 matches then; current config is paused, and replay requires temporary unpause with both webhooks forced off, followed by re-pause |
| Generated WETH deposit | `python3 quickstart.py replay 17344627` | ✅ direct `deposit()` matched and custom message logged; Discord forcibly disabled (2026-07-10) |
| Tellor monitors | same pattern, block TBD per monitor | pending address confirmation |

## Provenance

- **Source map**:
  - `quickstart.py`, `config/triggers/scripts/generic_alert.py`,
    `config/triggers/scripts/tellor_lib.py`, and `tests/`: implemented behavior
    and verification.
  - `.env.example`, `.gitignore`, `docker-compose.yaml`, and
    `config/networks/ethereum_mainnet.json`: secret seeding/isolation, generated
    file scope, content-pinned runtime, confirmation delay, polling, and recovery.
  - `../../openzeppelin-monitor/` at clean tag `v1.5.0`: EVM function matching,
    script arguments/payload, template variables, and config validation.
  - [OpenZeppelin Monitor v1.3 stable docs](https://docs.openzeppelin.com/monitor/1.3.x):
    monitor/trigger schemas, canonical signature syntax, and notification fields.
  - [Official OpenZeppelin Monitor image](https://hub.docker.com/r/openzeppelin/openzeppelin-monitor):
    alpha/production-risk warning and published container distribution.
  - [Solidity ABI specification](https://docs.soliditylang.org/en/latest/abi-spec.html):
    selectors, canonical types, overload behavior, and ABI decoding requirements.
  - [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/):
    `eth_chainId`, `eth_getCode`, and block/log query semantics.
  - [Sourcify API v2](https://docs.sourcify.dev/docs/api/): verified ABI lookup;
    the legacy v1 API was disabled on 2026-07-07.
  - [Discord Webhook Resource](https://docs.discord.com/developers/resources/webhook)
    and [Rate Limits](https://docs.discord.com/developers/topics/rate-limits):
    `wait=true`, 2,000-character content, allowed mentions, and `Retry-After`.
- **Changed claims**: this guide now distinguishes the Tellor production
  migration from a working arbitrary-function quickstart, records 11 production
  monitor configs on mainnet only and the USDC smoke monitor as paused,
  documents the consolidated TellorFlex report and trusted median, defines the
  normalized alert envelope, and states the top-level-call, confirmation,
  replay, and real-webhook boundaries explicitly.
- **Assumptions**: production supplies a reliable Ethereum mainnet RPC and a
  valid Discord incoming webhook; historical replay additionally requires
  archive access. The USDC smoke monitor is intentionally retained but paused.
  A bare function name is usable only when Sourcify returns one unambiguous ABI
  entry; proxy overrides are manually verified and replayed.
- **Validation**: 39/39 quickstart tests on host and 50/50 handler/config checks
  on host plus the exact pinned Monitor image; Python bytecode compile; JSON
  parse; the exact active-production/paused-smoke mainnet-only assertion;
  hardened Monitor `check`; recreated-container readback; live mainnet
  checkpoint advancement; and diff whitespace check.
- **Residual risks**: OpenZeppelin Monitor is alpha; no user/test Discord webhook
  was available for a real success post; the public replay RPC is not a
  production SLA; proxy overrides can be mistyped despite explicit opt-in;
  internal calls and view reads are out of scope; Discord has no durable outbox;
  accidentally unpausing the USDC smoke monitor can create high alert volume;
  a `Retry-After` longer than the trigger's 180-second timeout is logged as a
  failed trigger rather than queued.
