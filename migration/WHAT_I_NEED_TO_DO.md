# What I Need To Do To Run The Monitor Safely

> This is the production/AWS activation runbook for the fixed Tellor monitors.
> To configure an arbitrary Ethereum contract function and Discord message, use
> the [five-minute custom-function quickstart](MIGRATION.md#five-minute-custom-function-quickstart)
> instead; it requires no Python or JSON edits and uses an isolated webhook.

This is the owner runbook for operating the activated OpenZeppelin Monitor on
an EC2 host for Ethereum mainnet Tellor monitoring. Eleven production monitors
are configured active; the USDC smoke monitor is retained but paused. Use the
gates below before deploying or reloading any config change.

The production target is:

- Official image: v1.5.0 content-pinned in `docker-compose.yaml`
- Runtime: Docker Compose on one Amazon Linux 2023 EC2 host
- Production network watched by the current configs: `ethereum_mainnet`
- Alert channel: Discord webhook plus local JSON-lines file at `logs/alerts.log`
- Secret boundary: `migration/.env`, which is ignored by git

Do not skip gates. Replay mode executes triggers. If a production Discord
webhook is loaded during replay, historical matches can post real alerts.

## 0. Operator Prerequisites

The operator needs enough skill to do each item below without guessing:

- Use a terminal: `cd`, edit a text file, copy/paste commands, read command
  output, and stop when output differs from the expected result.
- Use Git safely: check `git status`, inspect diffs, and avoid committing
  `.env` or other secrets.
- Read and edit JSON without changing unrelated keys.
- Use Docker Compose basics: `up`, `down`, `run`, `logs`, `ps`, and `restart`.
- Use AWS EC2 basics: instance, EBS volume, security group, SSH or Session
  Manager access, and IAM permissions.
- Understand Ethereum basics: chain ID, contract address, ABI function/event
  signature, transaction hash, and block number.
- Handle secrets safely: never paste production secrets into chat, tickets,
  screenshots, shell history, or git.

If any item above is not true, pair with someone who has that skill before
touching production.

## 1. Required Accounts And Access

Have all of this ready before starting host setup.

| Need | Required access | Safety requirement |
| --- | --- | --- |
| AWS | Account with permission to create/manage EC2, EBS, security groups, IAM roles, and snapshots | Do not use the AWS root user. Use MFA. Use least-privilege IAM or an approved admin session. |
| EC2 login | SSH key pair or AWS Session Manager access | Prefer Session Manager or SSH restricted to the operator's current IP. Do not expose SSH to `0.0.0.0/0`. |
| Git repository | Read access to this repository on the host | Use a deploy key, short-lived token, or approved SSH key. Do not put tokens in `.env`. |
| Discord | Permission to create webhooks in both a test channel and the production alert channel | Use the test webhook for validation and replay. Load the production webhook only at the final go-live gate. |
| Ethereum mainnet RPC | Private or trusted mainnet RPC URL with `eth_getLogs`, `eth_call`, and normal JSON-RPC support | Use a non-public production endpoint with quota/health monitoring. Public RPCs are only acceptable for local smoke checks. |
| Infura or equivalent RPC provider | New project/key for Sepolia, Polygon, and Optimism if EVMCall verification needs those chains | Do not reuse old or burned project IDs. Set provider-side quota alerts if available. |
| Gnosis and Chiado RPC | Reliable URLs for chain IDs 100 and 10200 if EVMCall verification needs those chains | Public URLs are acceptable only if the owner accepts the availability risk. |
| CoinMarketCap | Fresh `CMC_PRO_API_KEY` | Supplies one input to the TellorFlex trusted-price median. Rotate immediately if leaked. |
| ExchangeRate-API | Fresh `EXCHANGERATE_API_KEY` | Required by DVM BRL/CNY reference checks. Rotate immediately if leaked. |
| Contract source of truth | Authoritative Tellor owner, verified on-chain contract, deployment record, or official repo | Candidate addresses in this repo are not enough. Confirm before deploying or reloading a changed monitor. |

CoinGecko and CoinCap are used by the handlers without keys.

## 2. Non-Negotiable Stop Rules

Stop immediately and do not continue activation if any item is true:

- Any required account, credential, contract fact, ABI, historical block, or
  expected alert text is missing.
- `git status` shows `.env`, secret files, keys, or tokens staged or tracked.
- The production Discord webhook is present during handler tests or replay.
- `jq empty` fails on any config JSON.
- `python3 quickstart.py check` reports an error.
- Handler tests fail.
- A replay does not produce the expected alert in `logs/alerts.log`.
- Monitor logs contain `Script execution failed` or `Script content not found`
  during validation or after activation.
- RPC health checks return the wrong chain ID or repeatedly fail.
- The `data/ethereum_mainnet_last_block.txt` checkpoint stops updating after
  the service is running.
- You are unsure whether the bridge is V1 or V2.

Rollback first, then investigate.

## 3. Confirm Production Facts Before Touching The Host

Fill this out in the migration ticket, release issue, or an operator notebook.
Do not deploy a changed row until every fact for that row is confirmed.

| Config file | Exact monitor name used by handler dispatch | Contract/function/event facts to confirm | Historical replay evidence required |
| --- | --- | --- | --- |
| `config/monitors/staking.json` | `Tellor Staking` | Oracle address; `NewStaker`, `StakeWithdrawRequested`, `StakeWithdrawn` events and argument shapes | At least one known tx/block for each staking event, or written approval that one event has no recent replay sample |
| `config/monitors/address_updates.json` | `Tellor Address Updates` | Oracle address; `submitValue(bytes32,bytes,uint256,bytes)`; `updateStakeAmount()`; address-report query IDs in `tellor_lib.py` | Known tx/block for address-report query IDs and for `updateStakeAmount`, or written exception |
| `config/monitors/tellorflex_data_report.json` | `TellorFlex Data Report` | Oracle address; `submitValue(bytes32,bytes,uint256,bytes)`; tracked query IDs/assets in `TRUSTED_PRICE_ASSETS`; median of available price sources | Known tx/block for SpotPrice, EVMCall, and any other expected query type; each tracked asset has trusted-price evidence or a written exception |
| `config/monitors/dvm.json` | `Tellor DVM Price Deviation` | Oracle address; tracked query IDs/assets in `DVM_FEEDS`; 10% threshold is still desired | Known tx/block that exercises alert and drop paths |
| `config/monitors/evm_call.json` | `Tellor EVMCall Validation` | Oracle address; EVMCall queryData encoding; target-chain RPC map in `EVM_CALL_RPCS` | Known tx/block for a valid EVMCall and a mismatch/not-verified path if available |
| `config/monitors/token_bridge.json` | `Tellor Token Bridge` | Bridge address; V1 vs V2; exact function names and signatures | Known tx/block for every enabled bridge function |
| `config/monitors/deposit_to_layer.json` | `Tellor Deposit To Layer` | Bridge address; exact `depositToLayer(uint256,uint256,string)` signature | Known tx/block with expected amount, tip, and recipient |
| `config/monitors/withdraw_from_layer.json` | `Tellor Withdraw From Layer` | Bridge address; exact `Withdraw(uint256,string,address,uint256)` event | Known tx/block with expected deposit ID, sender, recipient, and amount |
| `config/monitors/update_oracle_data.json` | `Update Oracle Data Calls` | Relayer address; exact typed `updateOracleData` tuple signature and report decoding | Known tx/block with expected feed, value, timestamps, validator power, and signature count |
| `config/monitors/update_validator_set.json` | `Update Validator Set Calls` | Light-client address; exact typed `updateValidatorSet` signature | Known tx/block with expected threshold, timestamp, hash, validator power, and signature count |
| `config/monitors/guardian_reset_validator_set.json` | `Guardian Reset Validator Set Calls` | Light-client address; exact `GuardianResetValidatorSet` event shape | Known tx/block with expected threshold, timestamp, and validator-set hash |
| `config/monitors/smoke_usdc.json` | `Smoke Test USDC Transfer` | Must remain paused in production; temporarily unpause only for isolated webhooks-off replay | Existing replay block `25446155` proves the pipeline |

Important dispatch rule: do not rename a monitor's `"name"` unless you also
update `HANDLERS` in `config/triggers/scripts/handlers.py` and rerun tests.
The script trigger dispatches by exact monitor name.

Bridge warning: the current bridge monitor config is V1-shaped. If production
uses V2 names such as `claimExtraWithdrawByWithdrawId`, `proposePauseBridge`,
or `approvePause`, update the monitor JSON, handler text, and tests before
activation.

## 4. Prepare The Local Workstation

Work from the repository root unless a command says otherwise.

1. Confirm the target file set:

```sh
cd /Users/df/projects/dev/autoTasks
git status --short
```

Expected result:

- Existing user changes may be present.
- No secret file is tracked or staged.
- `migration/.env` must not appear as tracked.

2. Confirm `.env` is ignored (works regardless of how the ignore rule is
written, and needs no extra tools):

```sh
git check-ignore -v migration/.env
```

Expected result: prints the matching ignore rule (currently `.gitignore:1:.env`)
and exits 0. If it prints nothing and exits non-zero, stop and add an ignore
rule before creating `.env`. Rerun this same check on the EC2 host after
cloning in Section 7.

3. Enter the migration directory:

```sh
cd /Users/df/projects/dev/autoTasks/migration
pwd
```

Expected result: `/Users/df/projects/dev/autoTasks/migration`.

## 5. Create And Protect Secrets

Create the local env file:

```sh
test -e .env || cp .env.example .env
chmod 600 .env
git check-ignore -v .env
```

Edit `.env` with a local editor. Do not paste secrets into shell commands if
that would store them in shell history.

Required production values:

```sh
RPC_ETHEREUM_MAINNET=<private-or-trusted-mainnet-rpc>
DISCORD_WEBHOOK_URL=<production-discord-webhook-only-at-go-live>
CMC_PRO_API_KEY=<fresh-coinmarketcap-key>
EXCHANGERATE_API_KEY=<fresh-exchangerate-api-key>
```

Required when EVMCall verification needs these target chains:

```sh
RPC_POLYGON=<polygon-rpc-for-chain-137>
RPC_OPTIMISM=<optimism-rpc-for-chain-10>
RPC_GNOSIS=<gnosis-rpc-for-chain-100>
RPC_CHIADO=<chiado-rpc-for-chain-10200>
RPC_SEPOLIA=<sepolia-rpc-for-chain-11155111>
```

These target-chain RPCs do not make Monitor watch those chains. They only let
the Python EVMCall handler verify submitted values. To watch another chain,
add a network JSON and add that network slug to the monitor's `networks` array.

Before validation or replay, either leave `DISCORD_WEBHOOK_URL` blank or set it
to a test Discord webhook. Do not load the production webhook until Section 14.

Check that git is still safe:

```sh
git status --short -- .env .env.example config docker-compose.yaml WHAT_I_NEED_TO_DO.md
```

Expected result: `.env` does not appear. If `.env` appears, stop and fix
ignore rules before continuing.

## 6. Build The EC2 Host Safely

Use an approved AWS region and account. The host only needs outbound network
access for package installs, Docker pulls, RPC calls, Discord, and price APIs.
It does not need inbound web traffic.

Recommended EC2 baseline:

- Amazon Linux 2023.
- `t3.small` or larger to start. Increase if CPU, memory, or disk pressure
  appears in monitoring.
- Encrypted gp3 EBS root volume with enough room for Docker images, logs, and
  checkpoints. Start with at least 20 GiB unless your AWS standard says more.
- IMDSv2 required.
- Security group inbound:
  - Prefer no inbound ports if using Session Manager.
  - Otherwise allow TCP 22 only from the operator's current trusted IP.
  - Do not expose TCP 3000, 8081, or 9090 publicly.
- Security group outbound:
  - Allow DNS, NTP, HTTPS, and any approved RPC endpoints required by `.env`.
- Optional metrics profile:
  - Prometheus `:9090` and Grafana `:3000` are for private access only.
  - Put them behind VPN, SSM port forwarding, or another approved private path.
  - The compose file expects an OpenZeppelin Monitor sibling checkout at
    `../../openzeppelin-monitor` for metrics config files. If `autoTasks` is
    cloned to `~/apps/autoTasks`, clone the approved OpenZeppelin Monitor
    source to `~/apps/openzeppelin-monitor` and check out tag `v1.5.0` before
    using `docker compose --profile metrics up -d`.

Create a dedicated Linux user:

```sh
sudo useradd --create-home --shell /bin/bash monitor
```

Only add this user to `wheel` if your operations policy requires direct sudo
from the service account. If you do, remove that access after setup unless it
is explicitly approved.

Configure login for the `monitor` user before continuing:

- If using AWS Session Manager, confirm the EC2 instance profile allows Session
  Manager access and use `sudo -iu monitor` after connecting.
- If using SSH, install only the approved public key for the `monitor` user:

```sh
sudo install -d -m 700 -o monitor -g monitor /home/monitor/.ssh
sudo sh -c 'cat > /home/monitor/.ssh/authorized_keys'
sudo chown monitor:monitor /home/monitor/.ssh/authorized_keys
sudo chmod 600 /home/monitor/.ssh/authorized_keys
```

Paste the approved public key, then press `Ctrl-D`. Do not paste a private key
onto the server.

Install and harden base packages:

```sh
sudo dnf update -y
sudo dnf install -y docker git jq curl
sudo systemctl enable --now docker
docker --version
docker compose version
```

If `docker compose version` fails, stop and install the approved Docker Compose
plugin for Amazon Linux 2023 before continuing.

Limit Docker log growth:

```sh
sudo mkdir -p /etc/docker
printf '%s\n' \
  '{' \
  '  "log-driver": "json-file",' \
  '  "log-opts": {' \
  '    "max-size": "10m",' \
  '    "max-file": "5"' \
  '  }' \
  '}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

Grant Docker access only to the user that will operate the service:

```sh
sudo usermod -aG docker monitor
```

Log out and back in as `monitor`, or start a new session, then verify:

```sh
id
docker ps
```

Expected result: the user is in the `docker` group and `docker ps` works.
Treat Docker group access as root-equivalent.

## 7. Put The Repository On The Host

As the `monitor` user:

```sh
mkdir -p ~/apps
cd ~/apps
git clone <approved-repo-url> autoTasks
cd ~/apps/autoTasks/migration
```

If the repository is already present, update it deliberately:

```sh
cd ~/apps/autoTasks
git status --short
git fetch --all --prune
git pull --ff-only
cd migration
```

Expected result: no uncommitted production edits are overwritten. If the host
has local edits, stop and review them before pulling.

Create runtime directories:

```sh
mkdir -p data logs
chmod 700 data logs
```

Copy or create `.env` on the host using the values prepared in Section 5:

```sh
test -e .env || cp .env.example .env
chmod 600 .env
git check-ignore -v .env
```

Then edit `.env` on the host. Keep the production Discord webhook blank or set
to a test webhook until Section 14.

## 8. Pull The Image And Validate Static Config

From `~/apps/autoTasks/migration` on the host:

```sh
docker compose pull monitor
jq empty config/networks/*.json config/monitors/*.json config/triggers/*.json
```

Expected result:

- Docker pulls the content-pinned OpenZeppelin Monitor image.
- `jq empty` prints no errors.

Eleven production monitors are configured active, the smoke monitor is paused, and every
monitor declares only `ethereum_mainnet`. OpenZeppelin Monitor v1.5.0 can still
log errors while raw `--check` exits zero, so static JSON parsing is only the
first gate. Section 12 runs the hardened `python3 quickstart.py check` and
replays monitors with both Discord webhooks forcibly disabled.

If any command fails, do not start the service.

## 9. Check RPC Health Without Printing Secrets

Run this from `migration/`. It parses `.env` as data (it does not source or
execute the file), checks expected chain IDs without printing RPC URLs, treats
`YOUR_NEW_INFURA_KEY` template URLs as missing, and returns non-zero if any
required or configured endpoint fails:

```sh
python3 quickstart.py check-rpcs
```

Expected result: `RPC_ETHEREUM_MAINNET` prints `ok`. Every configured optional
EVMCall RPC prints `ok`. Placeholder or missing optional EVMCall RPCs print
`optional missing` and are acceptable only if
the owner confirms those chain IDs do not need verification.

## 10. Run Handler Tests Safely

These tests run the Python trigger directly inside the official image. They
write to `logs/alerts.log`; they must not post to production Discord.

From `migration/`:

```sh
docker run --rm \
  --env DISCORD_WEBHOOK_URL= \
  -v "$PWD":/work \
  -w /work \
  --entrypoint python3 \
  openzeppelin/openzeppelin-monitor@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635 \
  tests/run_tests.py
```

Expected result: all handler tests pass.

Price-source failures exercise explicit fallback behavior, so this suite is
valid without public API access. Any failing assertion is a real activation
blocker, not an expected offline exception.

## 11. Back Up Runtime State Before Any Replay Or Start

Before replaying or starting Monitor on a host with existing state:

```sh
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cp -a data "backups/data-$stamp"
cp -a logs "backups/logs-$stamp"
```

Expected result: backup directories exist under `migration/backups/`.

Do not commit backup logs if they contain alert content, transaction context,
or operational details that should stay private.

## 12. Replay Every Active Monitor Safely

Replay only with Discord disabled or pointed at a test channel.

Important: because `docker-compose.yaml` loads `.env` through `env_file`, do
not rely on `env -u DISCORD_WEBHOOK_URL` for compose commands. Override the
container variables explicitly with `-e DISCORD_WEBHOOK_URL=` and
`-e CUSTOM_DISCORD_WEBHOOK_URL=`.

Critical replay quirk (verified in v1.5.0 source): the Monitor pre-loads
script-trigger contents only for monitors that are not paused. Replaying a
monitor whose JSON still has `"paused": true` will find matches but fail the
notification step with `Script content not found`, and nothing is written to
`logs/alerts.log`. Production monitors are already active; the smoke monitor
must be temporarily unpaused only for its isolated replay and restored to
`"paused": true` immediately afterward.

Per-monitor replay procedure:

1. Confirm Discord is safe: `.env` has `DISCORD_WEBHOOK_URL` blank or set to a
   test webhook, and every replay command below still overrides it with
   both webhook overrides.
2. For a production monitor, verify it is active on mainnet only:

```sh
jq -e '.paused == false and .networks == ["ethereum_mainnet"]' \
  config/monitors/<monitor-file>.json
```

3. Run hardened config validation:

```sh
python3 quickstart.py check
```

Raw Monitor `--check` is not sufficient because v1.5.0 can log an error while
exiting zero. The wrapper fails on either the exit status or logged `ERROR`.

4. Run the replay:

```sh
docker compose run --rm \
  -e DISCORD_WEBHOOK_URL= -e CUSTOM_DISCORD_WEBHOOK_URL= \
  monitor \
  --monitor-path /app/config/monitors/<monitor-file>.json \
  --network ethereum_mainnet \
  --block <known_block_number>
```

5. Check the results. The replay runs in a one-off container, so its Monitor
   log output streams directly to your terminal; `docker compose logs` only
   shows the long-running service container and will not contain this replay.

```sh
tail -n 20 logs/alerts.log
```

6. Verify replay did not change monitor configuration. Do not use
`git checkout --` here because it can discard unrelated operator edits:

```sh
git diff -- config/monitors/<monitor-file>.json
git status --short -- config/monitors
```

Expected result:

- `logs/alerts.log` contains exactly the expected alert content for the replay.
- The replay terminal output does not contain `Script execution failed`,
  `Script content not found`, or `Error sending notifications`.
- No production Discord channel receives a message.
- After step 6, the monitor is still active on mainnet only and only
  reviewed/intentional edits remain.

Record this for each monitor:

| Monitor file | Block | Tx hash | Expected alert text checked | Result | Operator | Date |
| --- | --- | --- | --- | --- | --- | --- |
| `staking.json` | | | | | | |
| `address_updates.json` | | | | | | |
| `tellorflex_data_report.json` | | | | | | |
| `dvm.json` | | | | | | |
| `evm_call.json` | | | | | | |
| `token_bridge.json` | | | | | | |
| `deposit_to_layer.json` | | | | | | |
| `withdraw_from_layer.json` | | | | | | |
| `update_oracle_data.json` | | | | | | |
| `update_validator_set.json` | | | | | | |
| `guardian_reset_validator_set.json` | | | | | | |

Smoke/large-transfer replay is the only exception to step 2. Confirm both
webhooks are forced off, temporarily set `smoke_usdc.json` to `"paused": false`,
run `python3 quickstart.py check`, then run:

```sh
docker compose run --rm \
  -e DISCORD_WEBHOOK_URL= -e CUSTOM_DISCORD_WEBHOOK_URL= \
  monitor \
  --monitor-path /app/config/monitors/smoke_usdc.json \
  --network ethereum_mainnet \
  --block 25446155
```

Immediately restore `smoke_usdc.json` to `"paused": true` after replay and
verify the diff before continuing. Do not restart production while it is
temporarily unpaused.

## 13. Production Readiness Gate

Before loading the production Discord webhook or restarting Monitor, confirm:

- All rows in Section 3 have confirmed facts or a written exception.
- Every production monitor has a passing replay recorded in Section 12 or a
  written exception; the smoke replay remains historical pipeline evidence.
- All production monitors are active, smoke is paused, every watcher is
  mainnet-only, and `git status --short -- config/monitors` contains only
  reviewed changes:

```sh
jq -s -e 'all(.[];
  .networks == ["ethereum_mainnet"] and
  .triggers == ["tellor_alert"] and
  (if .name == "Smoke Test USDC Transfer" then .paused == true else .paused == false end)
)' \
  config/monitors/*.json
git status --short -- config/monitors
```

- `smoke_usdc.json` is `"paused": true`.
- `.env` contains fresh API keys and production RPCs.
- `.env` is mode `600`.
- `.env` is not tracked by git.
- `jq empty` passes.
- The active set passed `python3 quickstart.py check` before safe replay.
- Handler tests pass.
- Runtime state has been backed up.
- The owner has approved the 11 active production monitors and canceled live
  USDC smoke alerts.

Command checkpoint:

```sh
ls -l .env
git status --short -- .env config/monitors config/networks config/triggers
jq empty config/networks/*.json config/monitors/*.json config/triggers/*.json
```

Expected result: no errors and no `.env` in git output.

## 14. Load Production Discord Only At Go-Live

Edit `.env` and set:

```sh
DISCORD_WEBHOOK_URL=<production-discord-webhook>
```

Do not run replay after this unless you explicitly override Discord again and
follow the full Section 12 procedure:

```sh
docker compose run --rm \
  -e DISCORD_WEBHOOK_URL= -e CUSTOM_DISCORD_WEBHOOK_URL= \
  monitor \
  --monitor-path /app/config/monitors/<monitor-file>.json \
  --network ethereum_mainnet \
  --block <known_block_number>
```

If the production webhook is ever exposed or used incorrectly, delete it in
Discord and create a new one. Do not try to keep using a leaked webhook.

## 15. Start And Verify The Production Monitor Set

Verify the configured activation/network invariant and run the hardened check:

```sh
jq -s -e 'all(.[];
  .networks == ["ethereum_mainnet"] and
  .triggers == ["tellor_alert"] and
  (if .name == "Smoke Test USDC Transfer" then .paused == true else .paused == false end)
)' \
  config/monitors/*.json
python3 quickstart.py check
```

Start or restart Monitor:

```sh
docker compose up -d --force-recreate monitor
docker compose ps
docker compose logs --tail=200 monitor
```

Follow logs:

```sh
docker compose logs -f monitor
```

In another terminal:

```sh
tail -f logs/alerts.log
```

Observe at least one full polling and recovery interval. With the current
mainnet network config, polling runs every minute and recovery runs every five
minutes.

Healthy signs:

- Container stays `Up`.
- Logs show normal block processing.
- `data/ethereum_mainnet_last_block.txt` exists and advances over time.
- No `Script execution failed`.
- No repeated RPC errors.
- Alerts, if any, appear in both Discord and `logs/alerts.log`.

If any monitor is not safe to leave active, follow Section 16 and record the
temporary exception instead of silently weakening the documented state policy.

## 16. Rollback

Use the smallest rollback that makes the system safe.

Pause one monitor:

1. Set that monitor's `"paused"` value back to `true`.
2. Validate:

```sh
jq empty config/monitors/<monitor-file>.json
```

3. Restart:

```sh
docker compose up -d --force-recreate monitor
docker compose logs --tail=100 monitor
```

Stop all monitoring:

```sh
docker compose down
```

Restore a checkpoint backup only when you understand why the current checkpoint
is wrong:

```sh
docker compose down
mv data "data.bad-$(date -u +%Y%m%dT%H%M%SZ)"
cp -a backups/data-<stamp> data
docker compose up -d --force-recreate monitor
```

Secret rollback:

- Discord webhook leaked or wrong channel alerted: delete the webhook in
  Discord, create a new one, update `.env`, restart Monitor.
- API key leaked: revoke it at the provider, create a new key, update `.env`,
  restart Monitor.
- RPC URL leaked: rotate the provider key or URL if the provider supports it,
  update `.env`, restart Monitor.

## 17. Daily And Weekly Operations

Daily checks:

```sh
cd ~/apps/autoTasks/migration
docker compose ps
docker compose logs --tail=200 monitor
tail -n 50 logs/alerts.log
ls -l data/ethereum_mainnet_last_block.txt
cat data/ethereum_mainnet_last_block.txt
```

Investigate immediately if logs show:

- `Script execution failed`
- repeated RPC errors
- repeated `DVM NOT VERIFIED`
- repeated `EVMCall NOT VERIFIED`
- no new checkpoint in `data/ethereum_mainnet_last_block.txt`
- Discord delivery errors or HTTP 429s that do not recover

Weekly checks:

```sh
df -h
docker system df
docker compose images
docker image inspect openzeppelin/openzeppelin-monitor@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635 \
  --format '{{.Id}} {{range .RepoDigests}}{{.}} {{end}}'
python3 quickstart.py check
```

Do not change the image tag during weekly checks. Image upgrades require a
separate migration/test plan.

After any config or script change:

```sh
jq empty config/networks/*.json config/monitors/*.json config/triggers/*.json
python3 quickstart.py check
docker run --rm --env DISCORD_WEBHOOK_URL= -v "$PWD":/work -w /work \
  --entrypoint python3 openzeppelin/openzeppelin-monitor@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635 \
  tests/run_tests.py
docker compose up -d --force-recreate monitor
```

## 18. Troubleshooting Quick Reference

| Symptom | First checks | Safe action |
| --- | --- | --- |
| `Script execution failed` | `docker compose logs --tail=200 monitor`; latest `logs/alerts.log`; recent config/script diff | Pause the affected monitor, restart, reproduce with the Section 12 replay procedure |
| Replay finds matches but writes nothing to `logs/alerts.log`; output shows `Script content not found` | The replayed monitor violates the active-set invariant — v1.5.0 only pre-loads scripts for unpaused monitors | Restore `"paused": false` and `networks: ["ethereum_mainnet"]`, review the diff, then rerun with both webhook overrides (Section 12) |
| Replay posts to production Discord | A webhook override was omitted | Delete/rotate the webhook if needed; document the accidental alert; rerun with both webhooks disabled |
| Hardened `check` fails | JSON syntax, env vars, monitor schema, trigger path, or an active/network invariant violation | Fix config, rerun the Section 13 invariant, then rerun the hardened check |
| No alerts, but expected activity happened | Monitor still paused, wrong contract address, wrong ABI/signature, wrong network slug, RPC lag, checkpoint too far ahead | Keep paused until replay proves the exact tx/block |
| Repeated RPC errors | Provider status, quota, wrong URL, wrong chain ID, network egress | Fail over to approved RPC; rerun Section 9 and `python3 quickstart.py check` |
| Repeated `DVM NOT VERIFIED` | CoinGecko, ExchangeRate-API key, external API outage | Verify API health and key; decide whether to keep noisy monitor active |
| Repeated `EVMCall NOT VERIFIED` | Missing target-chain RPC var, wrong chain ID, target RPC outage | Add/fix target-chain RPC or pause EVMCall monitor |
| Disk usage high | `df -h`, `docker system df`, log sizes | Rotate/archive logs; prune Docker only after confirming containers/images in use |

## 19. Provenance

- Source map:
  - `migration/MIGRATION.md`: migration status, image pin, architecture,
    replay behavior, handler-test command, activation context, and known smoke
    replay block.
  - `migration/.env.example`: required environment variables and burned-key
    warning.
  - `migration/docker-compose.yaml`: image tag, `.env` loading, mounted config,
    data/log paths, restart policy, security option, and optional metrics ports.
  - `migration/config/networks/*.json`: watched network slugs, chain IDs,
    polling cadence, confirmation blocks, and mainnet recovery settings.
  - `migration/config/monitors/*.json`: monitor names, paused state, contract
    addresses, function/event signatures, and trigger wiring.
  - `migration/config/triggers/scripts/alert.py`: exact-name handler dispatch
    and trigger failure behavior.
  - `migration/config/triggers/scripts/handlers.py`: per-monitor handlers,
    filters, DVM/EVMCall behavior, and handler map.
  - `migration/config/triggers/scripts/tellor_lib.py`: alert delivery,
    `logs/alerts.log`, Discord behavior, price/API dependencies, and EVMCall
    target-chain RPC map.
  - `migration/tests/run_tests.py`: safe handler-test invocation and test
    behavior.
- Changed claims:
  - Expanded the short owner checklist into a start-to-finish safety runbook.
  - Added explicit operator skills, account/setup requirements, hard stop
    rules, host hardening, secret handling, RPC health checks, replay isolation,
    monitor-state policy checks, rollback, and operations checks.
  - Corrected replay safety guidance for Compose by requiring
    both webhook overrides because `.env` is loaded through `env_file`.
  - Kept replay safe after verifying in the
    OpenZeppelin Monitor v1.5.0 source (`bootstrap::initialize_services` →
    `filter_active_monitors` → `TriggerExecutionService::load_scripts`) that
    script-trigger contents are pre-loaded only for unpaused monitors, so
    replaying a paused monitor fails notification with
    `Script content not found` and writes nothing to `logs/alerts.log`; the
    runbook limits temporary state toggling to isolated smoke replay.
  - Recorded 11 active production monitor configs after consolidating datafeed
    and price reporting, plus the intentional cancellation of live USDC smoke
    alerts by keeping that monitor paused.
  - Corrected the replay log-checking step: `docker compose run` output goes
    to the invoking terminal, not to `docker compose logs monitor`.
  - Replaced the `.gitignore` regex grep with `git check-ignore -v`, which
    validates the effective ignore behavior instead of a specific rule shape.
- Assumptions:
  - Production remains a single Amazon Linux 2023 EC2 host using Docker Compose.
  - The image remains pinned to the reviewed v1.5.0 digest in Compose.
  - Eleven production monitors remain active on Ethereum mainnet only, and the
    USDC smoke monitor remains paused.
  - Exact Tellor contract facts and historical replay blocks are still owner
    inputs until confirmed.
- Validation:
  - Read back the migration runbook and referenced local config/script/test
    files listed above.
  - Verify commands again on the target host before a production config reload.
  - Run the 39-test quickstart suite, 50 handler/config checks on host and in the
    pinned image, and the all-file production-active/smoke-paused assertion before
    force-recreating the service.
  - Read back the recreated container plus the advancing mainnet checkpoint;
    confirm no testnet checkpoint exists.
- Residual risks:
  - AWS, Docker, Discord, RPC provider, CoinMarketCap, and ExchangeRate-API
    account UIs can change. If an external UI differs from this runbook, stop
    and use the provider's current official documentation.
  - This runbook does not prove contract addresses, bridge version, or
    historical blocks. Those remain required activation inputs.
  - Temporarily unpausing the USDC smoke monitor for replay can produce noisy
    alerts unless both webhook variables are forced empty as documented.
