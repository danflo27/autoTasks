# Defender Autotasks → OpenZeppelin Monitor Migration

Self-hosted replacement for the sunset Defender Monitor. Monitor source is pinned at
**v1.5.0** in the sibling checkout `../../openzeppelin-monitor/`; all our configs,
scripts, and this runbook live here under `migration/` and are version-controlled
with the legacy autotasks they replace.

## Decisions (2026-07-02)

| Decision | Choice |
|---|---|
| Notification channel | **Discord webhook** (`DISCORD_WEBHOOK_URL`) |
| Monitors in scope | **All**, including the Unknown-status ones (Deposit/WithdrawFromLayer, disputes, bridges, tips) |
| RPC for Polygon/Optimism/Sepolia | **Infura with a NEW key** (old project IDs are burned) |
| Ethereum mainnet RPC | Coworker's ETH node (URL pending) |
| Mumbai | **Dropped** (network deprecated, no Amoy replacement) |
| AWS shape | **Single EC2 + Docker Compose** (Phase 3) |
| Monitor version | **v1.5.0** (latest release, 2026-04-23) |

## Verified v1.5.0 behavior (read from source, not docs)

These correct two errors in the original planning assumptions:

1. **`trigger_conditions` filter scripts signal via stdout, NOT exit code.**
   The *last line of stdout* must be `true` or `false`:
   - `true` → match is **filtered out** (no notification) — note the inversion vs. Defender's "return matches to alert"
   - `false` → match **proceeds** (notification fires)
   - Non-zero exit, unparseable output, empty output, or **timeout** → treated as a
     script error and the match **proceeds** (fails open — alert fires).
     Source: `src/bootstrap/mod.rs` `execute_trigger_condition` (only `Ok(true)`
     filters; every `Err` falls through to keep) + `src/services/trigger/script/executor.rs`
     `process_script_output`.
   - Filter scripts **cannot** modify the match or add template variables.

2. **Custom script triggers (`trigger_type: "script"`) are fire-and-forget.**
   Exit code 0 = success, non-zero = error logged; stdout is ignored
   (`process_script_output` short-circuits when `from_custom_notification`).
   They receive `{"monitor_match": {"EVM": {...}}, "args": [...]}` on stdin and must
   **build and send the notification themselves** (POST to the Discord webhook).
   This is the enrichment path for datafeed / priceMonitor / dvm / EVMCall.

3. **JS scripts run as `node -e "<file contents>"`** — the file is read at startup
   and inlined; **no npm install happens**. `require()` resolves from the process
   CWD (`/app` in the container), so npm deps must either be baked into the image
   or (preferred) the script must be **bundled to a single self-contained file with
   esbuild**. The production image (Alpine) ships `bash`, `python3`, `node`, `jq`.
   Script changes require a Monitor restart (contents cached at startup).

4. **Templates support only built-in variables** — `${monitor.name}`,
   `${transaction.hash|from|to|value}`, `${events.N.signature}`,
   `${events.N.args.<name>}`, `${functions.N.*}`. No custom/computed variables, no
   explorer-link helper (bake explorer URL prefix into each trigger's message body).

5. **`match_conditions` expressions** support `==`/`!=` on hex values (case-sensitive
   on hex chars), numeric comparisons, `starts_with`/`ends_with`/`contains`, and
   `AND`/`OR` — so the addressUpdates queryId filter can be a pure expression, e.g.
   `queryId == '0x3ab3...'`, no script needed.

6. **Secrets**: any config value can be `{"type": "environment", "value": "VAR"}`
   (case-insensitive type tag). Resolved from the container env → compose loads `.env`.

7. **State**: last processed block per network in `data/<slug>_last_block.txt`
   (always written); optional missed-block recovery via `recovery_config` in the
   network JSON (off for now, revisit in Phase 3).

## Architecture: two tiers (confirmed)

| Tier | Monitors | Mechanism |
|---|---|---|
| Simple | staking, tokenBridge, DepositToLayer, WithdrawFromLayer, addressUpdates, disputes, bridges, tips | `match_conditions` (+ expressions) → built-in **discord** trigger with `${...}` template |
| Enriching | datafeed, priceMonitor, dvm, EVMCall | `match_conditions` → **script trigger** (bundled Node.js) that decodes/fetches/eth_calls, formats the legacy template text, and POSTs to Discord itself |

Forced behavior changes vs. Defender (call out to confirm):

- **Filter fails open**: on Defender, a crashed autotask meant *no* alert; here a
  crashed/timed-out filter script means the alert *fires unfiltered*. Bias is now
  toward false positives instead of silent misses (arguably better for alerting).
- **Per-event headlines** (tokenBridge): one Discord trigger's template is static per
  trigger, so either one trigger per event signature or move formatting to a script.
- **Explorer links**: rebuilt as hardcoded per-network URL prefix + `${transaction.hash}`.

## Layout

```
migration/
├── MIGRATION.md            ← this file
├── docker-compose.yaml     ← builds ../../openzeppelin-monitor (v1.5.0), mounts ./config
├── .env.example            ← all env vars (copy to .env, fill in)
├── config/
│   ├── networks/           ← ethereum_mainnet.json, sepolia.json (more pending addresses)
│   ├── monitors/           ← one JSON per ported monitor
│   ├── triggers/           ← discord + script trigger definitions
│   │   └── scripts/        ← bundled Node.js enrichment scripts
│   └── filters/            ← trigger_conditions filter scripts (avoid if expressions suffice)
├── data/                   ← block checkpoints (gitignored)
└── logs/                   ← (gitignored)
```

## Port order & status

| # | Monitor | Approach | Status |
|---|---|---|---|
| 1 | staking | match_conditions + discord template | ☐ blocked on address+ABI |
| 2 | tokenBridge | match_conditions + per-event discord triggers | ☐ blocked on address+ABI |
| 3 | Deposit/WithdrawFromLayer | match_conditions + discord template | ☐ blocked on address+ABI |
| 4 | addressUpdates | queryId expression + discord template | ☐ blocked on address+ABI |
| 5 | datafeed | script trigger (ABI decode, no APIs) | ☐ |
| 6 | priceMonitor | script trigger (CG/CMC/CoinCap) | ☐ needs new CMC key |
| 7 | dvm | script trigger (10% deviation gate) | ☐ needs new exchangerate key |
| 8 | EVMCall | script trigger (multi-chain eth_call) | ☐ |
| 9 | disputes / bridges / tips | match_conditions + discord template | ☐ need event definitions |

## Needed from Dan (blocking Phase 2)

- [ ] Contract addresses + ABIs per network (Google Sheet export)
- [ ] Which networks each monitor watches (mainnet? Sepolia? others?)
- [ ] Coworker's ETH node URL → `RPC_ETHEREUM_MAINNET`
- [ ] New Infura key → `RPC_SEPOLIA` / `RPC_POLYGON` / `RPC_OPTIMISM`
- [ ] Discord webhook URL for the alerts channel → `DISCORD_WEBHOOK_URL`
- [ ] Rotated CoinMarketCap + ExchangeRate-API keys (old ones are burned in git history)
- [ ] What disputes/bridges/tips sentinels actually matched on (Defender UI config —
      the repo only has generic templates for these)

## Runbook

All commands from `autoTasks/migration/`.

```sh
cp .env.example .env        # once; fill in real values
docker compose build        # build Monitor v1.5.0 image (Rust compile, slow first time)
docker compose up -d        # start
docker compose logs -f monitor
docker compose down
docker compose --profile metrics up -d   # + Prometheus :9090, Grafana :3000
```

Validation / testing:

```sh
# validate all JSON configs without starting (image entrypoint IS the binary —
# pass flags only). "No active monitors found" = configs valid but none defined yet.
docker compose run --rm monitor --check

# RPC health check. NOTE: upstream's scripts/validate_network_config.sh curls the
# raw `.url.value`, which for our environment-type configs is the VAR NAME — useless.
# Check the resolved URLs directly instead:
set -a; source .env; set +a
for u in "$RPC_ETHEREUM_MAINNET" "$RPC_SEPOLIA" "$RPC_POLYGON" "$RPC_OPTIMISM" "$RPC_GNOSIS" "$RPC_CHIADO"; do
  echo "$u -> $(curl -s -m 10 "$u" -X POST -H 'Content-Type: application/json' \
    --data '{"method":"net_version","params":[],"id":1,"jsonrpc":"2.0"}' | jq -r .result)"
done

# replay a historical block through one monitor (per-monitor test; record each below)
docker compose run --rm monitor \
  --monitor-path /app/config/monitors/<name>.json --network <slug> --block <N>
```

Config changes: edit JSON/scripts here, then `docker compose restart monitor`
(scripts are cached at startup — a restart is required, not just a new block).

### Per-monitor test log

(filled in as each monitor is ported — command + block number + expected output)
