# Defender Autotasks → OpenZeppelin Monitor Migration

Self-hosted replacement for the sunset Defender Monitor. Runs the **official
`openzeppelin/openzeppelin-monitor:v1.5.0` image** via Docker Compose; the
matching source checkout lives at `../../openzeppelin-monitor/` (same tag) for
reference. All configs, scripts, tests, and this runbook are version-controlled
here alongside the legacy autotasks they replace.

**Status: implementation complete and smoke-proven.** Every monitor is ported
and unit-tested; the pipeline is proven end-to-end against USDC on mainnet.
The Tellor monitors are `paused` pending Dan confirming addresses/keys —
see [Activation checklist](#activation-checklist).

## Decisions (2026-07-02)

| Decision | Choice |
|---|---|
| Notification channel | **Discord webhook** (`DISCORD_WEBHOOK_URL`); every alert also appended to `logs/alerts.log` (JSON lines) |
| Monitors in scope | All. disputes/bridges/tips have no autotask logic in this repo — need the old Defender sentinel config to port (see below) |
| Script language | **Python 3.12 stdlib** — the official image's node and jq are broken (glibc mismatch), python3 works; stdlib-only means no build/bundle step |
| RPC for Polygon/Optimism/Sepolia | Infura with a NEW key (old project IDs burned) |
| Ethereum mainnet RPC | Coworker's ETH node (URL pending); `ethereum-rpc.publicnode.com` as smoke-test stand-in (`eth.drpc.org` 403s `eth_getLogs`) |
| Mumbai | Dropped |
| AWS shape | Single EC2 + Docker Compose (Phase 3) |

## Architecture

One script trigger (`tellor_alert`) serves every monitor. It receives the match
JSON on stdin and dispatches **by monitor name** to a formatter that reproduces
the legacy template text and delivers it (Discord + alerts.log):

```
config/
├── networks/            ethereum_mainnet.json, sepolia.json
├── monitors/            one JSON per legacy sentinel (name = dispatch key!)
└── triggers/
    ├── tellor_script.json   the single "tellor_alert" script trigger
    └── scripts/
        ├── alert.py         entry point (stdin JSON -> dispatch by monitor name)
        ├── handlers.py      one formatter per monitor + HANDLERS map
        └── tellor_lib.py    match parsing, ABI decode, price APIs, Discord, maps
tests/run_tests.py       19 handler tests, run inside the Monitor image
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
| datafeed | Tellor Datafeed | fn submitValue | decode SpotPrice/EVMCall/RNG, USD format |
| priceMonitor | Tellor Price Monitor | fn submitValue | 4 assets; CG+CMC+CoinCap, average; a dead source becomes `n/a` instead of killing the alert (legacy skipped the whole event) |
| dvm | Tellor DVM Price Deviation | fn submitValue | 16 feeds; alert only ≥10% off reference |
| EVMCall | Tellor EVMCall Validation | fn submitValue | decode, eth_call target chain, alert on mismatch; RPC failure now alerts "NOT VERIFIED" instead of a false "bad EVMCall" |
| — | Smoke Test USDC Transfer | event Transfer > 1M USDC | pipeline proof; delete when no longer wanted |

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
   just match printing — with `DISCORD_WEBHOOK_URL` set, replaying posts real
   Discord messages. Unset it (or use a test channel) when replaying.
6. Match combination: `transactions` conditions AND (events OR functions);
   within a category, conditions are OR'd. Function-based monitors here gate on
   tx status Success so failed calls don't alert (events imply success).
7. State: `data/<network>_last_block.txt` checkpoint (always written);
   `recovery_config` in network JSON enables missed-block retry (Phase 3).

## Forced behavior changes vs Defender (all deliberate)

- **Timestamps** are alert-time UTC, not block time (block timestamp isn't in
  the match payload).
- **priceMonitor**: one flaky price API no longer suppresses the whole alert.
- **EVMCall**: RPC failures alert distinctly as NOT VERIFIED (legacy false-"bad
  EVMCall"). Unsupported chainIds (e.g. dropped Mumbai) also alert NOT VERIFIED.
- **addressUpdates** `updateStakeAmount`: legacy printed `undefined` for data
  (args[0] of a no-arg function); now omitted.
- A crashed handler = alert lost with an error in Monitor logs (Defender was
  the same: crashed autotask = no alert). Watch for `Script execution failed`.

## Proof of working pipeline (2026-07-02)

Known basic test contract: **USDC mainnet Transfer events** (`Smoke Test USDC
Transfer` monitor, threshold 1M USDC).

1. **19/19 handler tests** pass inside the official image (includes live
   CoinGecko-backed dvm/priceMonitor paths and all drop/filter paths):
   `docker run --rm -v .:/work -w /work --entrypoint python3 openzeppelin/openzeppelin-monitor:v1.5.0 tests/run_tests.py`
2. **`--check`**: all 10 monitors + 2 networks + trigger validate.
3. **Replay** of mainnet block `25446155` (contains a 10,000,000 USDC
   transfer): 2 matches, expression filter applied, script trigger executed.
4. **Live run**: block watcher picked up new blocks and `logs/alerts.log`
   captured real transfers within seconds, e.g.
   `Amount: 10,000,000.00 USDC` tx `0x1283bf73c8a9...` (etherscan-linked,
   formatted by handle_smoke).

## Activation checklist (what Dan provides, per monitor)

Candidate addresses below are from Tellor's own repos on this machine —
**verify before unpausing** (set `"paused": false`).

- [ ] Oracle monitors (staking, address_updates, datafeed, price_monitor, dvm,
      evm_call): confirm oracle address per network. Candidate mainnet
      `0x8cFc184c877154a8F9ffE0fe75649dbe5e2DBEbf`, sepolia
      `0xB19584Be015c04cf6CFBF6370Fe94a58b7A38830` (telliot contract_directory).
- [ ] Bridge monitors (token_bridge, deposit_to_layer, withdraw_from_layer):
      confirm bridge address + V1 vs V2 (V2 renames claimExtraWithdraw →
      claimExtraWithdrawByWithdrawId and pauseBridge → proposePauseBridge/
      approvePause; ABIs in `../../layer/evm/artifacts/.../TokenBridgeV2.json`).
      Candidate mainnet V1 `0x5589e306b1920F009979a50B88caE32aecD471E4`
      (bridgewatch config).
- [ ] Which network(s) each monitor watches (all default `ethereum_mainnet`;
      add network JSONs for others).
- [ ] `.env`: coworker's ETH node URL, NEW Infura key, `DISCORD_WEBHOOK_URL`,
      rotated `CMC_PRO_API_KEY` + `EXCHANGERATE_API_KEY` (old keys in git
      history are burned).
- [ ] disputes / bridges / tips: repo only has generic templates — need the old
      Defender sentinel config (contract + events) to port. Alert text would be
      `**Defender Monitor <name> Triggered**` style via a small handler.
- [ ] After unpausing each monitor: replay a block with a known event and
      record it in the test log below.

## Runbook

All commands from `autoTasks/migration/`.

```sh
cp .env.example .env        # once; fill in real values
docker compose up -d        # start (pulls official v1.5.0 image)
docker compose logs -f monitor
tail -f logs/alerts.log     # every alert, as JSON lines
docker compose restart monitor   # REQUIRED after any config/script change
docker compose down
docker compose --profile metrics up -d   # + Prometheus :9090, Grafana :3000
```

Validation / testing:

```sh
# handler unit tests (runs in the image; needs network for CoinGecko cases)
docker run --rm -v .:/work -w /work --entrypoint python3 \
    openzeppelin/openzeppelin-monitor:v1.5.0 tests/run_tests.py

# config validation (image entrypoint IS the binary — pass flags only)
docker compose run --rm monitor --check

# replay one monitor against a historical block
# ⚠️ executes triggers for real — posts to Discord if DISCORD_WEBHOOK_URL is set
docker compose run --rm monitor \
  --monitor-path /app/config/monitors/<name>.json --network <slug> --block <N>

# RPC health check (upstream validate_network_config.sh can't resolve
# environment-type URLs, so check the resolved values directly)
set -a; source .env; set +a
for u in "$RPC_ETHEREUM_MAINNET" "$RPC_SEPOLIA" "$RPC_POLYGON" "$RPC_OPTIMISM" "$RPC_GNOSIS" "$RPC_CHIADO"; do
  echo "$u -> $(curl -s -m 10 "$u" -X POST -H 'Content-Type: application/json' \
    --data '{"method":"net_version","params":[],"id":1,"jsonrpc":"2.0"}' | jq -r .result)"
done
```

### Per-monitor test log

| Monitor | Command | Result |
|---|---|---|
| Smoke Test USDC Transfer | `docker compose run --rm monitor --monitor-path /app/config/monitors/smoke_usdc.json --network ethereum_mainnet --block 25446155` | ✅ 2 matches (10M USDC transfer), alert formatted + logged (2026-07-02) |
| Tellor monitors | same pattern, block TBD per monitor | pending address confirmation |
