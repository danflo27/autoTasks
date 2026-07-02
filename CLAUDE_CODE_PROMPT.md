# Claude Code Prompt: Defender Autotasks → Self-Hosted OpenZeppelin Monitor

Use this document as the initial prompt when opening this repo in **Claude Code Desktop**.

---

## Background

I previously used **OpenZeppelin Defender** (hosted) to monitor Tellor/oracle-related on-chain activity. OpenZeppelin is sunsetting hosted Defender Monitor support and has open-sourced the replacement: **OpenZeppelin Monitor** (a Rust service, configured via JSON + optional filter scripts).

This repo (`autoTasks`) contains my legacy **Defender Autotask** JavaScript handlers and notification templates. On Defender, I pasted this code into their UI; they ran the infra and invoked my handlers when sentinels matched.

I now want to **self-host OpenZeppelin Monitor** and recreate the same monitoring behavior — locally in Docker first, then on **AWS**.

**Official references (read these first):**

- Docs: https://docs.openzeppelin.com/monitor (note: docs default to unreleased `main` — switch to the docs for the release tag you install)
- Repo: https://github.com/OpenZeppelin/openzeppelin-monitor
- Target the **latest stable release tag** (v1.5.0 as of April 2026); avoid unreleased `main`.

---

## Key facts about OpenZeppelin Monitor (verified 2026-07 — re-verify against the pinned release)

- **Rust binary**, configured with JSON files:
  - `config/networks/` — RPC URLs, chain ID, cron schedule, confirmation blocks
  - `config/monitors/` — watched addresses, `match_conditions` (events/functions/expressions), `trigger_conditions`, trigger refs
  - `config/triggers/` — notification channels: Slack, Discord, Email, Telegram, Webhook, **or custom scripts**
  - `config/filters/` — custom filter scripts
- **Custom `trigger_conditions` scripts** (Bash/Python/**JavaScript**): receive `{"monitor_match": ..., "args": ...}` as **JSON on stdin**, and signal via the **last line of stdout** — `true` = filter the match OUT (drop), `false` = keep it (notify). *(Verified in v1.5.0 source — NOT exit-code based as previously assumed.)* They are **filters only; they cannot add fields to the notification payload**.
  - ⚠️ Any script **error — non-zero exit, bad/empty output, or timeout — keeps the match** (fails open: alert fires even though the script didn't finish) — matters for my API-calling filters.
  - Scripts are loaded at startup; changes require a restart.
- **Custom-script notification channel**: a trigger of type script that runs on match — this is the escape hatch for monitors that need to *enrich* the alert (fetch API prices, do an `eth_call`, format a message) and send it themselves.
- **Notification templates** use `${...}` variables, e.g. `${monitor.name}`, `${transaction.hash}`, `${events.0.args.value}`, `${functions.0.signature}`.
- **Secrets**: config values support `{"type": "Plain"|"Environment"|"HashicorpCloudVault", "value": ...}` — use `Environment` everywhere.
- **Testing**: `./openzeppelin-monitor --check` (validate config), `--monitor-path=... --network=... --block=N` (replay a specific historical block), `./scripts/validate_network_config.sh` (RPC health).
- **Docker**: `Dockerfile.production`, `docker-compose.yaml`; `docker compose --profile metrics up -d` adds Prometheus (:9090) + Grafana (:3000).
- Persists state between restarts; supports EVM, Stellar, Solana (I only need EVM).

### The critical architecture decision (resolve early, in Phase 1)

Defender autotasks did **filter + enrich + format** in one handler. Monitor splits this:

| Autotask did | Monitor equivalent |
|---|---|
| Filter matches (e.g. only these queryIds, only ≥10% deviation) | `match_conditions` expressions, or `trigger_conditions` script (exit code) |
| Enrich (fetch CoinGecko/CMC prices, `eth_call` cross-chain) and put results **in the message** | ❌ not possible via `trigger_conditions` — needs a **custom-script trigger** that builds and sends the notification itself (e.g. posts to Slack/Discord webhook directly) |
| Format the message | `${...}` template in the trigger config (simple cases only) |

So expect two tiers:

1. **Simple monitors** (staking, tokenBridge, Deposit/WithdrawFromLayer, addressUpdates): pure `match_conditions` + template triggers. Little or no script.
2. **Enriching monitors** (datafeed, priceMonitor, dvm, EVMCall): custom-script trigger (Node.js) that receives the match JSON, does the decoding/API calls/`eth_call`, formats the old template text, and posts to the webhook itself. Port the autotask bodies into these scripts.

Verify this split against the pinned release's docs/examples before building — if newer versions let scripts inject template variables, prefer that.

---

## Phase 0 Discovery (pre-filled from repo audit — verified against code)

> **Still needed from me:** exact contract addresses per network, which sentinels are active, notification channel choice, and production RPC URLs (Google Sheet: https://docs.google.com/spreadsheets/d/1pP_XQ0TuoIUMlY56tcqTNumuRI5_ozMUghQRob-4f04/edit?usp=sharing).

### Repo layout

```
autoTasks/
├── README.md
├── priceMonitor/          priceMonitorAutotask.js + priceMonitorTemplate.md
├── dvm/                   dvmAutotask.js + dvmTemplate.md
├── EVMCall/               EVMCallAutotask.js + EVMCallTemplate.md
├── datafeed/              datafeedAutotask.js + datafeedTemplate.md
├── staking/               stakingAutotask.js + stakingNotificationTemplate.md
├── tokenBridge/           tokenBridgeAutotask.js + tokenBridgeTemplate.md
│   ├── DepositToLayer/    depositToLayerAutotask.js + depositToLayerTemplate.md
│   └── WithdrawFromLayer/ withdrawFromLayerAutotask.js + withdrawFromLayerTemplate.md
├── addressUpdates/        addressUpdateAutotask.js + addressUpdateTemplate.md
├── disputes/              disputesTemplate.md          (template only — no autotask)
├── bridges/               bridgeTemplate.md            (template only — generic)
└── tips/                  tipsTemplate.md              (template only — generic)
```

### Legacy Defender handler contract

Every autotask follows this pattern:

```js
exports.handler = async function (payload) {
  const conditionRequest = payload.request.body;
  const events = conditionRequest.events;
  const matches = [];
  for (const evt of events) {
    // evt.matchReasons[0].signature, .args, evt.hash, evt.timestamp
    matches.push({ hash: evt.hash, metadata: { ... } });
  }
  return { matches };
};
```

**Tellor `submitValue` event arg layout** (used by priceMonitor, dvm, datafeed, EVMCall, addressUpdates):

| Index | Field     | Usage                                      |
|-------|-----------|--------------------------------------------|
| 0     | queryId   | bytes32 — identifies the data feed         |
| 1     | value     | hex — reported value                       |
| 3     | queryData | bytes — encoded query type + parameters    |

Query data is typically ABI-decoded as `(string type, bytes parameters)`.

---

### Monitor inventory

| # | Monitor | Autotask | Template | Status | Complexity | Filter logic | Notification |
|---|---------|----------|----------|--------|------------|--------------|--------------|
| 1 | **staking** | ✅ | ✅ | Active | Low | Match `NewStaker`, `StakeWithdrawRequested`, or `StakeWithdrawn` by signature; format amount ÷ 1e18 | `[event](tx link)` + address + amount |
| 2 | **tokenBridge** | ✅ | ✅ | Active | Low | Match `addStakingRewards`, `claimExtraWithdraw`, `pauseBridge`, `unpauseBridge` | headline + address + amount |
| 3 | **DepositToLayer** | ✅ | ✅ | Unknown | Low | All events on sentinel (assumes `depositToLayer` fn); args: amount, tip, layerRecipient | headline + recipient + amount + tip |
| 4 | **WithdrawFromLayer** | ✅ | ✅ | Unknown | Low | All events on sentinel; args: depositId, sender, recipient, amount | headline + deposit details |
| 5 | **addressUpdates** | ✅ | ✅ | Active | Low | `updateStakeAmount` OR `submitValue` with specific queryIds (autopay / oracle address reports) | message + network + tx link + data |
| 6 | **datafeed** | ✅ | ✅ | Active | Medium | All `submitValue` events; decode SpotPrice/EVMCall/RNG; format label + value | label, value, padded flag + network link |
| 7 | **priceMonitor** | ✅ | ✅ | Active | High | Known queryIds only; fetch CG + CMC + CoinCap; **always returns match** (informational CSV-style) | CSV: label, value, cg, cmc, coincap, avg |
| 8 | **dvm** | ✅ | ✅ | Active | High | 16 spot-price queryIds; fetch reference price (CG or exchangerate-api); **alert only if ≥10% deviation** | "Potential Dispute" + reported vs expected + % diff |
| 9 | **EVMCall** | ✅ | ✅ | Active | High | `queryData` contains `EVMCall`; decode chainId/address/calldata; `eth_call` on target chain; alert if result ≠ submitted value | bad EVMCall + expected vs submitted |
| 10 | **disputes** | ❌ | ✅ | Unknown | — | Generic Defender template (`matchReasonsFormatted`) — likely paired with a Defender-native monitor, not a custom autotask | generic monitor triggered message |
| 11 | **bridges / tips** | ❌ | ✅ | Unknown | — | Generic templates only | generic monitor triggered message |

**Notes:**

- `tokenBridgeAutotask.js` has `depositToLayer` / `withdrawFromLayer` handlers **commented out** — those live in separate autotasks instead.
- `dvmTemplate.md` content is dispute-oriented ("Potential Dispute") despite living under `dvm/` — functionally a **price deviation alert**, distinct from `priceMonitor` which logs all price submissions without a tolerance gate.
- `priceMonitor` and `dvm` overlap on queryIds (BTC, ETH, TRB, MATIC) but differ in APIs used and alert threshold.
- Minor code-quality issue to fix while porting: `headline` is an undeclared (implicit-global) variable in `tokenBridgeAutotask.js` and both Deposit/Withdraw autotasks; `signature` is unused in `tokenBridgeAutotask.js`.

---

### Per-monitor detail

#### 1. staking

- **Events:** `NewStaker`, `StakeWithdrawRequested`, `StakeWithdrawn` (matched via `signature.includes(...)`)
- **Processing:** Extract address + amount from args; divide amount by 1e18; withdrawn events use string message instead of numeric amount
- **Port as:** Event `match_conditions` + template trigger (no script)
- **Dependencies:** None

#### 2. tokenBridge

- **Events:** `addStakingRewards`, `claimExtraWithdraw`, `pauseBridge`, `unpauseBridge`
- **Processing:** Headline per event type; format TRB amounts
- **Port as:** Event `match_conditions` + template trigger (one monitor JSON can carry all four events; per-event headline may need one trigger per event or a small script)
- **Dependencies:** None

#### 3. DepositToLayer / WithdrawFromLayer

- **Functions/events:** Assumes sentinel already filters to the right call — handler processes every incoming event
- **DepositToLayer args:** `[amount, tip, layerRecipient]` — amounts ÷ 1e18
- **WithdrawFromLayer args:** `[depositId, sender, recipient, amount]`
- **Port as:** Function or event `match_conditions` + template trigger
- **Dependencies:** None

#### 4. addressUpdates

- **Events/functions:** `updateStakeAmount`, `submitValue`
- **Filtering:**
  - `updateStakeAmount` → always alert ("updateStakeAmount")
  - `submitValue` → only for queryIds:
    - `0x3ab34a189e35885414ac4e83c5a7faa9d8f03a4d530728ef516d203d91d6309c` → "autopay address report"
    - `0xcf0c5863be1cf3b948a9ff43290f931399765d051a60c3b23a4e098148b1f707` → "oracle address report"
- **Port as:** `match_conditions` expression on queryId if expressions support it; otherwise a tiny `trigger_conditions` script (exit 0 only for these queryIds)
- **Dependencies:** None

#### 5. datafeed

- **Events:** All `submitValue` (no filtering — every event becomes a match)
- **Processing:** Decode query type string; branch on SpotPrice / EVMCall / RNG; format USD for SpotPrice; for EVMCall replace value display with chainId
- **Port as:** Custom-script **trigger** (needs ABI decode + branching to build the message)
- **Dependencies:** `web3` (or `ethers`/`viem`) for ABI decode

#### 6. priceMonitor

- **Events:** `submitValue` for known queryIds only
- **QueryId → asset map:**

  | queryId | Label |
  |---------|-------|
  | `0xa6f013ee236804827b77696d350e9f0ac3e879328f2a3021d473a0b778ad78ac` | BTC/USD |
  | `0x83a7f3d48786ac2667503a61e8c415438ed2922eb86a2906e4ee66d9a2ce4992` | ETH/USD |
  | `0x5c13cd9c97dbb98f2429c101a2a8150e6c7a0ddaff6124ee176a3a411067ded0` | TRB/USD |
  | `0x40aa71e5205fdc7bdb7d65f7ae41daca3820c5d3a8f62357a99eda3aa27244a3` | MATIC/USD |

- **Processing:** Fetch prices from CoinGecko + CoinMarketCap + CoinCap (when available); compute average; **no deviation threshold** — always pushes match
- **Port as:** Custom-script **trigger** (API calls + message assembly)
- **Dependencies:** `axios`, `web3`; **secrets:** `CMC_PRO_API_KEY` (currently hardcoded)

#### 7. dvm (dispute / deviation detection)

- **Events:** `submitValue` for 16 spot-price queryIds
- **QueryIds:** BTC, ETH, TRB, LTC, OP, BCH, MATIC, SOL, DOT, FIL, BRL, CNY, STETH, WSTETH, SWETH, CBETH (full hex values in `dvm/dvmAutotask.js`)
- **Processing:** Fetch reference price (CoinGecko for crypto; exchangerate-api.com for BRL/CNY); alert only if `|value - ref| / ref >= 0.10` (10% tolerance)
- **Port as:** Custom-script **trigger** (API fetch + threshold gate + message). Note the timeout-passes-by-default caveat if any of this ends up in `trigger_conditions` instead.
- **Dependencies:** `axios`, `web3`; **secrets:** `EXCHANGERATE_API_KEY` (currently hardcoded)

#### 8. EVMCall validation

- **Events:** `submitValue` where queryData type is `EVMCall`
- **Processing:**
  1. Decode `(chainId, contractAddress, calldata)` from queryData
  2. Decode `(value, timestamp)` from reported value hex
  3. `eth_call` on target chain with contract + calldata
  4. Alert only if `result !== value` (mismatch)
- **Chains supported:**

  | chainId | Network | RPC (hardcoded — replace with env) |
  |---------|---------|--------------------------------------|
  | 100 | Gnosis | `https://rpc.gnosis.gateway.fm/` |
  | 137 | Polygon | Infura polygon-mainnet |
  | 10 | Optimism | Infura optimism-mainnet |
  | 11155111 | Sepolia | Infura sepolia |
  | 80001 | Mumbai | Infura polygon-mumbai (deprecated network — probably drop) |
  | 10200 | Chiado | `https://rpc.chiado.gnosis.gateway.fm/` |

- **Port as:** Custom-script **trigger** (multi-chain `eth_call` + compare + message)
- **Dependencies:** `web3`, `web3-eth-abi`; **secrets:** Infura project IDs (currently hardcoded)

---

### Networks referenced in code

| Network | chainId | Used by | In-repo RPC |
|---------|---------|---------|-------------|
| Gnosis | 100 | EVMCall | Public gateway |
| Polygon | 137 | EVMCall | Infura (hardcoded) |
| Optimism | 10 | EVMCall | Infura (hardcoded) |
| Sepolia | 11155111 | EVMCall | Infura (hardcoded) |
| Mumbai | 80001 | EVMCall | Infura (hardcoded; network deprecated) |
| Chiado | 10200 | EVMCall | Public gateway |

**Not in code but likely in the Google Sheet / Defender config:** Ethereum mainnet, Arbitrum, Tellor layer networks — confirm with me. **My coworker runs an ETH node** we can use as the Ethereum mainnet RPC (get me to provide the URL/access details).

---

### Secrets to externalize AND ROTATE

⚠️ These keys are hardcoded in this repo's git history — treat them as burned. Rotate/reissue each one; put the new values only in `.env`.

| Secret | Where hardcoded | Env var |
|--------|-----------------|---------|
| CoinMarketCap API key | `priceMonitor/priceMonitorAutotask.js:8` | `CMC_PRO_API_KEY` |
| ExchangeRate-API key | `dvm/dvmAutotask.js` (~lines 84, 88) | `EXCHANGERATE_API_KEY` |
| Infura project ID #1 (Sepolia/Mumbai) | `EVMCall/EVMCallAutotask.js` (~line 70) | per-chain RPC URL env vars |
| Infura project ID #2 (Optimism/Polygon) | `EVMCall/EVMCallAutotask.js` (~lines 73–75) | per-chain RPC URL env vars |
| Notification webhook / SMTP | (to be created) | `SLACK_WEBHOOK_URL`, etc. |
| Monitor RPC URLs (all chains) | network configs | `RPC_ETHEREUM_MAINNET`, `RPC_POLYGON`, … |

Use Monitor's `{"type": "Environment", "value": "VAR_NAME"}` secret form in all JSON configs.

---

### Recommended port order

1. **staking** — no external deps, config-only, good smoke test
2. **tokenBridge** — same pattern
3. **DepositToLayer / WithdrawFromLayer** — if still active
4. **addressUpdates** — queryId filter (expression or tiny script)
5. **datafeed** — first custom-script trigger (ABI decode only, no external APIs)
6. **priceMonitor** — external APIs
7. **dvm** — external APIs + threshold
8. **EVMCall** — multi-chain RPC + eth_call

---

### Open questions for me (ask before Phase 1)

- [ ] Notification channel: Slack / Discord / Email / Telegram / Webhook?
- [ ] Which monitors are still active in production? (DepositToLayer/WithdrawFromLayer, disputes, bridges, tips = Unknown)
- [ ] Primary chain(s) + Tellor oracle contract address(es)?
- [ ] RPC providers: coworker's ETH node for mainnet — what about Polygon/Optimism/Gnosis? Keep Infura (new key) or switch?
- [ ] Drop deprecated networks (Mumbai)? Replace with Amoy or nothing?
- [ ] AWS deploy shape: single EC2 + Docker Compose (simplest) vs ECS? (Recommend starting EC2 + Compose.)
- [ ] Contract addresses + ABIs from Google Sheet (export or paste)

---

## Goal

Help me build a **production-ready, self-hosted monitoring setup** using OpenZeppelin Monitor that preserves my current alert logic and notification content as closely as possible. I have not set any of this up before — explain infra steps as we go.

## Constraints & preferences

1. **Platform:** develop on macOS (darwin); run via **Docker Compose** locally, then deploy the same Compose setup to **AWS** (likely one EC2 instance).
2. **Secrets:** everything via `.env` / Monitor `Environment` secrets. Rotate the burned keys listed above.
3. **Scope:** recreate existing monitors first; don't redesign alert logic unless Monitor forces it. Call out any forced behavior changes explicitly.
4. **License:** Monitor is AGPL-3.0 — fine for internal use; note implications before we modify and expose it as a service.
5. **Layout:** Monitor install in sibling dir `../openzeppelin-monitor/` (pinned to a release tag); keep ported configs/scripts/docs in this repo under `migration/` so they're version-controlled together.
6. **No secrets in git.** Provide `.env.example` with placeholder names only.

## Phased work plan

### Phase 0 — Discovery ✅ (pre-filled above)

Review the inventory, ask me the open questions, then proceed.

### Phase 1 — Bootstrap Monitor

1. Clone OpenZeppelin Monitor at the latest release tag; get it running with Docker Compose.
2. Read the release's docs/examples and **confirm the filter-vs-enrich split** described above; decide the pattern for the enriching monitors before porting anything.
3. Create `.env.example` and document required env vars.
4. Create `config/networks/*.json` for every chain we keep (coworker's ETH node for mainnet).
5. Validate: `./openzeppelin-monitor --check` and `./scripts/validate_network_config.sh`.
6. Start `migration/MIGRATION.md`: Defender → Monitor mapping + ops runbook.

### Phase 2 — Port monitors (in recommended order)

For each monitor:

1. Create `config/monitors/<name>_monitor.json` (addresses + ABI from me).
2. Port filtering to `match_conditions` expressions where possible; `trigger_conditions` script only when expressions can't do it.
3. Port enrichment/formatting to either a `${...}` template trigger (simple) or a custom-script trigger (datafeed, priceMonitor, dvm, EVMCall) that reproduces the old template text and posts to the webhook.
4. Test by replaying a real historical block: `--monitor-path=... --network=... --block=<block with a known event>`.
5. Record the test command + expected output in `MIGRATION.md`; commit per monitor.

### Phase 3 — Production hardening + AWS

1. Persistent data dir (checkpoint/resume) mounted as a Docker volume.
2. Missed-block recovery, sensible `max_past_blocks` + `cron_schedule` per chain.
3. Restart policy in Compose; optional `--profile metrics` (Prometheus/Grafana).
4. AWS: provision (EC2 + Docker Compose to start), move `.env` securely (SSM Parameter Store or similar), set up log access.
5. Runbook in `MIGRATION.md`: start/stop, logs, config changes, adding a monitor, rotating a secret.

## Deliverables

- [ ] Working OpenZeppelin Monitor install (pinned release, Docker Compose)
- [ ] All network + monitor + trigger JSON configs
- [ ] Ported filter/notification scripts, no hardcoded secrets, burned keys rotated
- [ ] `.env.example`
- [ ] `migration/MIGRATION.md` with mapping, per-monitor test commands, and ops runbook
- [ ] AWS deployment steps documented (Phase 3)

## Implementation notes

1. **Tellor `submitValue`** — verify the exact event signature from the ABI I provide (args: queryId, value, nonce, queryData per the layout table above).
2. **Template variable migration:**

   | Defender | Monitor |
   |----------|---------|
   | `{{ metadata.label }}` | not available in templates — enriched fields must come from a custom-script trigger |
   | `{{ transaction.link }}` | build from `${transaction.hash}` + explorer base URL per network |
   | `{{ sentinel.network }}` | network slug (bake into per-network monitor/trigger config) |
   | `{{ sentinel.name }}` | `${monitor.name}` |
   | `{{ matchReasonsFormatted }}` | compose from `${events.0.*}` / `${functions.0.*}` |

3. **Script runtime** — Monitor executes JS scripts as external processes (stdin JSON → exit code for filters). They run under whatever `node` is in the container image, so npm deps like `axios`/`web3` require bundling or installing them in the image — check how the OZ examples handle deps; consider zero-dep `fetch` + `viem` bundled with esbuild if that's cleaner.
4. **EVMCall RPC reliability** — private RPC endpoints in production; all URLs from env; handle RPC failure without false "mismatch" alerts (old code's failure mode is worth checking).
5. **Don't guess Monitor behavior** — when unsure about script I/O, template variables, or config shape, read `examples/config/` and the docs for the pinned release first.

## How to work

- Confirm the open questions with me, then start Phase 1.
- Prefer incremental commits with clear messages (one monitor per commit in Phase 2).
- Call out breaking differences between Defender and Monitor explicitly — especially anywhere alert content or timing will differ from what I get today.
- I'm new to self-hosting infra: when we hit Docker/AWS steps, tell me what you're doing and why, not just the commands.
