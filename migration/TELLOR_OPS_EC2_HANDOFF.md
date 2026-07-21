# tellor-ops EC2 handoff

Snapshot for the next agent. Original inventory captured **2026-07-21 ~04:48 UTC** (`2026-07-21 ~00:48 America/New_York`) via AWS CLI + SSM Run Command.

**Audit note (2026-07-21 04:52–04:59 UTC):** the AWS control plane and host were re-read without changing them, and the local `autoTasks`, `CPI`, and `layer-daemons` worktrees were checked against this document. Runtime facts below remain point-in-time observations, not desired state. Secret values were not read during the audit. Do not treat values in chat history or backups as current; use the designated secret store after the source-of-truth problem below is resolved.

## TL;DR (revalidated point-in-time state)

| Item | State |
|------|--------|
| Instance | `i-00b865643d4023484` (`tellor-ops`), **running**, `t3a.large`, `us-east-2a` |
| OpenZeppelin Monitor | **OFF**: systemd `disabled` + `inactive`; container `migration-monitor-1` **Exited (137)** after SIGTERM did not finish within Docker's 10-second stop grace period and Docker sent SIGKILL; `OOMKilled=false` |
| Monitor watchdog | Timer **still enabled and active**; it fails and logs an alert every ~5 min while the monitor is intentionally off |
| Refprice daily | Timer **enabled** (14:50 America/New_York), but this is a prototype/manual comparison job and its last logged run failed with `context canceled` |
| CPI Big Mac | Timer **enabled** (16:00 America/New_York); it no-ops except on the 3rd Thursday and currently runs the USA scraper only |
| Access | Intended interactive admin path is SSM. There is no key pair and SG ingress is empty, but the public host runs `sshd`; the SG is the current inbound barrier, not an absent SSH service |
| Discord webhooks | Production monitor `.env` webhooks were rotated **2026-07-21 ~03:17 UTC** to a new Discord webhook (channel label `aws-defender`); full URL was pasted in operator chat — **rotate again if still that URL** |

**Intentional operator action (this session):** monitor was stopped and disabled on request. Restart is not automatic after reboot until someone re-enables the unit.

---

## AWS identity and connectivity

| Field | Value |
|-------|--------|
| Account | `075483720677` |
| Region | `us-east-2` |
| Instance ID | `i-00b865643d4023484` |
| Name / Project tags | `tellor-ops` |
| Type | `t3a.large` (x86_64) |
| AZ / subnet / VPC | `us-east-2a` / `subnet-03aa7de553c221216` / `vpc-0c8b7f62538e50596` |
| Private IP | `172.31.14.151` |
| Public IP | `3.16.207.22` (not an Elastic IP) |
| Launch time | `2026-07-20T21:20:19Z` |
| AMI | `ami-03499a87bbb39a09a` — Amazon Linux 2023 (`al2023-ami-2023.12.20260710.0-kernel-6.1-x86_64`) |
| Hostname | `ip-172-31-14-151.us-east-2.compute.internal` |
| Key pair | **none** (`KeyName: null`) |
| SSM | Online, agent `3.3.4624.0`; SSM reports `IsLatestVersion=false` |
| Instance profile | `TellorOpsEC2Role` |
| Metadata | IMDSv2 required (`HttpTokens=required`), endpoint enabled, hop limit 2 |
| EC2 monitoring | Basic/5-minute (`Monitoring=disabled`); no CloudWatch Agent metrics |
| CPU credits | `unlimited` |
| Root EBS | 40-GiB `gp3`, 3,000 IOPS / 125 MiB/s, encrypted with the AWS-managed EBS KMS key, `DeleteOnTermination=true` |

### Security group `tellor-ops-sg` (`sg-0cf58c65ac74501af`)

- **Inbound:** empty (no SSH/22, no public app ports).
- **Outbound:** TCP 80/443 and TCP+UDP 53 to `0.0.0.0/0`.

The instance is in the default public subnet, has an Internet Gateway route and a public non-EIP IPv4, and has no VPC endpoints. SSM is outbound-initiated. Host readback found `sshd` listening on IPv4 and IPv6 port 22; the empty SG ingress currently blocks it. A future SG mistake would expose SSH, so disable `sshd` if SSM-only administration is the desired control rather than relying on one network rule. Before doing that, test a break-glass host-recovery path such as an authorized EC2 Serial Console workflow or stop/detach/repair-volume procedure.

### IAM on the instance role

Attached:

- `AmazonSSMManagedInstanceCore`

Inline:

| Policy | Purpose |
|--------|---------|
| `TellorOpsSSMParametersRead` | `ssm:GetParameter(s)` on `arn:aws:ssm:us-east-2:075483720677:parameter/tellor-ops/*` |
| `TellorOpsSSMParams` | `ssm:GetParameter(s)` on `arn:aws:ssm:us-east-2:075483720677:parameter/tellor/ops/*` |
| `TellorOpsStagingS3Read` | `s3:GetObject` + `ListBucket` on `tellor-ops-staging-075483720677-use2` |

The instance role **cannot** `ssm:StartSession` (expected); the human principal starts a session and the managed node only opens the SSM channels. Operators recently used account root via `aws login`.

**Important IAM correction:** the prefix-scoped inline Parameter Store policies do not constrain effective access. The attached `AmazonSSMManagedInstanceCore` v2 policy also grants `ssm:GetParameter` and `ssm:GetParameters` on `Resource: "*"`; IAM allows are additive. The instance can therefore request named parameters outside the two Tellor prefixes, subject to KMS-key restrictions. Separate SSM host-management permissions from the application role (for example, Default Host Management Configuration or a reviewed custom managed-node policy), then keep only explicit workload prefixes on the application role.

A metadata-only check found three `SecureString` records: one watchdog value and two redundant migration-env records under the two prefixes. The checked-in code does not fetch them automatically, while the host also has materialized `.env` files. Choose one authoritative path and one secret per parameter, record owner/rotation date, and remove stale duplicates/backups only after rotation and rollback planning.

### How to connect

Local prerequisites:

1. Valid AWS creds (`aws login` if expired). CLI ≥ 2.32 for `aws login`.
2. Session Manager plugin on PATH. On this Mac it was installed user-local at `~/.local/bin/session-manager-plugin` (system pkg install needed sudo).

```bash
export PATH="$HOME/.local/bin:$PATH"
aws ssm start-session --target i-00b865643d4023484 --region us-east-2
```

Non-interactive work: `aws ssm send-command --document-name AWS-RunShellScript ...` (prefer JSON `file://` params; avoid embedding secrets in chat).

JSON `file://` parameters reduce quoting mistakes and local shell-history exposure; they are **not** a secret-transport boundary. Send parameter names and fetch values on-host through the instance role rather than putting values in Run Command parameters. The current CLI identity is account root. Replace it with a scoped, temporary-credential operator role before routine work; reserve root for root-only recovery. No regional `SSM-SessionManagerRunShell` preferences document exists, so session transcript delivery to CloudWatch Logs/S3, session KMS preferences, idle timeout, and Run As are not configured.

You are already on the host at `sh-5.2$` once the session starts. Do **not** re-run `aws ssm start-session` from inside the instance.

---

## Host layout

```
/opt/tellor/
  autoTasks/          # Defender legacy + OpenZeppelin Monitor migration (NOT a git checkout)
  CPI/                # Big Mac CPI scraper (NOT a git checkout); .venv + venv present
  layer-daemons/      # Go daemons + refprice prototype sources (NOT a git checkout)
  bin/
    cpi-bigmac-run.sh
    cpi-monthly.sh       # present on host, but not generated/referenced by current local source; treat as legacy/orphan
    monitor-watchdog.sh
    refprice-prototype   # compiled binary used by systemd

/etc/tellor/
  refprice.env              # mode 600
  monitor-watchdog.env      # mode 600

/var/lib/tellor/
  reference-price/          # brrny.json, live dumps, daily outputs

/var/log/tellor/
  monitor-watchdog.log
  refprice-daily.log
  cpi/
  userdata-complete
  refprice-live-*.log
```

Host bootstrap template used to create the units: local repo `autoTasks/migration/scripts/tellor-ops-setup.sh` (also conceptually applied on the host under `/opt/tellor/...`).

**Do not rerun it as a reconciler.** The untracked script is not idempotent: it unconditionally overwrites `/etc/tellor/refprice.env` and `/etc/tellor/monitor-watchdog.env` with empty placeholders, enables all three timers before validating dependencies, and may start the Monitor whenever its `.env` contains an RPC URL. Back up and diff installed files and units before reusing any part of it.

On-host ops doc present (may be ahead of local workspace): `/opt/tellor/autoTasks/migration/AWS_DOCKER_OPERATIONS.md`.

That runbook is absent from the current local worktree even though local README/MIGRATION links point to it. Copy the reviewed host version into version control and record its checksum before treating either copy as authoritative.

Local workspace docs:

- `migration/MIGRATION.md`
- `README.md`
- this handoff

**Important:** `/opt/tellor/{autoTasks,CPI,layer-daemons}` are **copied trees without `.git`**. Local developer clones under `~/projects/dev/` are the editable sources; deploying means copying/rsync/S3 staging, not `git pull` on the box unless someone re-gitifies the trees. At audit time all three worktrees were dirty, the setup/handoff files were untracked, and the entire refprice implementation was untracked. The deployed binary and copied trees therefore cannot be reproduced from Git as-is.

Staging bucket the role can read: `s3://tellor-ops-staging-075483720677-use2`. It has SSE-S3 and public-access blocks, but versioning is disabled and the role can read the whole bucket rather than a release prefix.

---

## Workloads

### 1) OpenZeppelin Monitor (Tellor Discord alerts) — **currently STOPPED**

| Piece | Detail |
|-------|--------|
| Compose project | `/opt/tellor/autoTasks/migration` |
| Compose file | `docker-compose.yaml` |
| Image | `openzeppelin/openzeppelin-monitor:v1.5.0@sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635` |
| Container name | `migration-monitor-1` |
| Systemd | `openzeppelin-monitor.service` (**disabled**, **inactive**) |
| Secrets | `/opt/tellor/autoTasks/migration/.env` (mode 600) |
| Config mount | `./config` → `/app/config` (ro) |
| Checkpoints | `./data` (e.g. `ethereum_mainnet_last_block.txt`) |
| Logs | `./logs` (+ Docker logs) |
| Metrics profile | Prometheus/Grafana defined but **profile `metrics` not in use** by default |

The final Monitor image does not declare a non-root user, and Compose has no CPU or memory limits. `no-new-privileges` and the content-pinned digest are useful controls, but they are not a complete container sandbox.

Systemd unit behavior:

- `Type=oneshot` + `RemainAfterExit=yes`
- `ExecStart` / `ExecReload`: `docker compose up -d --force-recreate monitor`
- `ExecStop`: `docker compose stop monitor`
- Requires `.env` to exist
- No `User=` is set, so the host-side systemd command runs as root

**Reload rule:** `.env` changes require container recreation. Both `systemctl reload openzeppelin-monitor` and `systemctl restart openzeppelin-monitor` suffice because `ExecReload` and `ExecStart` use `docker compose up -d --force-recreate`. A plain `docker compose restart` does **not** reread changed environment values. Mounted config/script changes require a process restart; force-recreate remains the clearest uniform procedure.

#### Discord / env keys (names only)

Present in migration `.env` at inventory time:

- `RPC_ETHEREUM_MAINNET` — the only RPC consumed by the active Monitor network config
- `RPC_SEPOLIA`, `RPC_POLYGON`, `RPC_OPTIMISM`, `RPC_GNOSIS`, `RPC_CHIADO` — EVMCall target RPCs; Sepolia also has a network file, but no active monitor watches it
- BlockPI, dRPC, and Alchemy entries, if present, are **not** operational fallbacks in the checked-in config
- `DISCORD_WEBHOOK_URL` — used by all checked-in production handlers
- `CUSTOM_DISCORD_WEBHOOK_URL` — used only by generated quickstart monitors, not the 12 listed below
- `UPDATE_VALIDATOR_SET_DISCORD_WEBHOOK_URL` — present but currently unused; remove it or wire it explicitly
- `CMC_PRO_API_KEY`, `EXCHANGERATE_API_KEY`; `COINCAP_API_KEY` is supported by code but was not present in the audited env key list

Backup before last webhook edit: `.env.bak.20260721T031742Z` (pre-rotation values).

**2026-07-21 operator change:** both `DISCORD_WEBHOOK_URL` and `CUSTOM_DISCORD_WEBHOOK_URL` were set to the same new Discord incoming webhook (Discord metadata name `aws-defender`). That URL appeared in operator chat; treat as compromised until rotated.

Changing `CUSTOM_DISCORD_WEBHOOK_URL` did not change delivery for the 12 checked-in production monitors. The `.env.bak.*` name is not covered by the repository's current `.gitignore`, so never copy that backup into the Git worktree; retire it after the relevant webhook values are rotated and recovery is no longer needed.

#### Monitors on disk (`config/monitors`; all declare `ethereum_mainnet` only)

| File | Name | paused |
|------|------|--------|
| `address_updates.json` | Tellor Address Updates | false |
| `deposit_to_layer.json` | Tellor Deposit To Layer | false |
| `dvm.json` | Tellor DVM Price Deviation | false |
| `evm_call.json` | Tellor EVMCall Validation | false |
| `guardian_reset_validator_set.json` | Guardian Reset Validator Set Calls | false |
| `smoke_usdc.json` | Smoke Test USDC Transfer | **true** |
| `staking.json` | Tellor Staking | false |
| `tellorflex_data_report.json` | TellorFlex Data Report | false |
| `token_bridge.json` | Tellor Token Bridge | false |
| `update_oracle_data.json` | Update Oracle Data Calls | false |
| `update_validator_set.json` | Update Validator Set Calls | false |
| `withdraw_from_layer.json` | Tellor Withdraw From Layer | false |

Policy from migration docs: 11 active + paused smoke; mainnet-only. Triggers under `config/triggers/` include script handlers in `config/triggers/scripts/` (`tellor_lib.py`, etc.).

#### Start / stop / reload

```bash
# On host
cd /opt/tellor/autoTasks/migration

# Start (and persist across reboot)
sudo systemctl enable --now openzeppelin-monitor.service

# Stop (and prevent reboot start) — current desired state as of this handoff
sudo systemctl stop openzeppelin-monitor.service
sudo systemctl disable openzeppelin-monitor.service

# After .env or config/script changes
sudo systemctl reload openzeppelin-monitor.service
# or:
sudo docker compose up -d --force-recreate monitor
```

Use `sudo` for `systemctl`/Docker commands unless the session is explicitly running as root or the operator has the required delegated group permissions. The last operator stop was not an OOM: Docker sent SIGTERM, waited 10 seconds, then SIGKILL. Investigate graceful shutdown and an appropriate `stop_grace_period` before treating future exit 137 events as clean stops.

Safe historical replay (no Discord): see `MIGRATION.md` — force empty webhook envs.

### 2) Monitor watchdog — **still ON**

| Piece | Detail |
|-------|--------|
| Timer | `monitor-watchdog.timer` enabled, every 5 minutes (+ 2 min after boot) |
| Service | `monitor-watchdog.service` |
| Script | `/opt/tellor/bin/monitor-watchdog.sh` |
| Env | `/etc/tellor/monitor-watchdog.env` (`MONITOR_WATCHDOG_WEBHOOK` / `DISCORD_WEBHOOK`) |
| Log | `/var/log/tellor/monitor-watchdog.log` |

Behavior: the current runtime defines no container healthcheck, so this is only a container-running probe. It cannot detect stalled block progress, bad RPC/config, handler errors, or failed Discord delivery. On every failed check it logs and, if configured, posts again without deduplication or a recovery notification. The generated payload also contains the literal text `$(hostname -s)` instead of expanding the hostname.

**While monitor is intentionally stopped, this timer will keep failing the health check.** Inventory showed:

- Early: `No webhook configured; alert suppressed`
- Later (`2026-07-21T00:48:37-04:00` and `00:53:42-04:00`): ALERT without a suppression line. This is consistent with a configured webhook but does not prove delivery because secret values and Discord were not queried.

If keeping monitor off, strongly consider:

```bash
sudo systemctl stop monitor-watchdog.timer
sudo systemctl disable monitor-watchdog.timer
```

### 3) Reference-price prototype capture — timer ON, not production reporting

| Piece | Detail |
|-------|--------|
| Timer | `refprice-daily.timer` — `*-*-* 14:50:00 America/New_York` |
| Service | `refprice-daily.service` |
| Binary | `/opt/tellor/bin/refprice-prototype` |
| Env | `/etc/tellor/refprice.env` → `REFERENCE_PRICE_ETH_RPC_URL` |
| BRRNY | `/var/lib/tellor/reference-price/brrny.json` |
| Output | `/var/lib/tellor/reference-price/reference-price-daily.json` |
| Log | `/var/log/tellor/refprice-daily.log` |

Last log snippet at inventory: scheduled window `2026-07-20T19:00:00Z`–`20:00:00Z`, then `collect live dataset: context canceled`. There are large live JSON dumps under `/var/lib/tellor/reference-price/` from Jul 20.

This is explicitly prototype/testnet-grade. It is not wired to the reporter daemon and submits no Tellor report. A successful canonical comparison requires an operator to populate `brrny.json` with matching BTC and ETH New York rates after the 3–4pm collection window. The program writes an unbenchmarked recovery capture before waiting up to 15 minutes for that file, so output existence or mtime does not prove service success.

The observed July 20 window predates this instance's stated launch (`21:20Z`) and could not be selected by the current scheduler after launch. Treat the log as copied/stale or evidence of deployed-binary/source drift until journal timestamps, file mtimes, invocation arguments, and the deployed binary SHA-256 are reconciled.

Source code for the prototype lives under `/opt/tellor/layer-daemons` (and local `~/projects/dev/layer-daemons`), but the local implementation is untracked and the deployed binary has no recorded build checksum. The service also has no `User=`, so it runs as root.

### 4) Big Mac CPI — timer ON (conditional run)

| Piece | Detail |
|-------|--------|
| Timer | `cpi-bigmac.timer` — daily `16:00:00 America/New_York` |
| Service | `cpi-bigmac.service` |
| Wrapper | `/opt/tellor/bin/cpi-bigmac-run.sh` (exits unless **3rd Thursday** NY) |
| App | `/opt/tellor/CPI` with `.venv` Python; Chrome at `/usr/bin/google-chrome` (v150) |
| Runtime config | The scheduled path does not read CPI `.env` or require API secrets. The wrapper sets `CHROME_BIN`; optional `CPI_US_STATES` / `CPI_HEADED` are not wired through an `EnvironmentFile` |
| Logs | `/var/log/tellor/cpi/cpi.log` (empty at inventory) plus detailed `/var/log/tellor/cpi/cpi-*.log` on an actual scrape |
| Data | `/opt/tellor/CPI/big_mac_prices.csv` (updated Jul 20) |

Current `main.py` runs the USA scraper only. Per-state failures are converted to `N/A` and do not fail the process, so systemd can report success even when no useful prices were collected. Validate coverage, non-`N/A` count, freshness, duplicates, and duration after each run. There are no discovered CPI tests.

**Security blocker:** this systemd service defaults to root, while the Selenium code browses public web content with Chrome's `--no-sandbox` flag. Move it to a dedicated unprivileged service account, restore browser sandboxing, and add systemd write-path/resource restrictions before unattended production use.

`Persistent=true` does not provide full backfill: a catch-up after the third Thursday has passed no-ops because the wrapper tests the current date. Refprice catch-up at or after its window schedules the next complete day, but the unit's two-hour timeout can kill that long wait. Define explicit missed-run/backfill behavior.

---

## Recent operator session (context for continuity)

Chronology relevant to this handoff (local laptop + SSM, account `075483720677`):

1. AWS CLI session expired → `aws login` as root.
2. Connected via SSM to `tellor-ops` (Session Manager plugin installed under `~/.local/bin` on the Mac).
3. Switched Discord webhooks in `/opt/tellor/autoTasks/migration/.env` for `DISCORD_WEBHOOK_URL` + `CUSTOM_DISCORD_WEBHOOK_URL`, backed up prior `.env`, `systemctl reload openzeppelin-monitor`.
4. Later **stopped and disabled** `openzeppelin-monitor.service` on request; the container did not exit within the 10-second grace period and was SIGKILLed (`ExitCode=137`, `OOMKilled=false`).
5. Watchdog timer was **not** disabled.

---

## Host resources (inventory)

- Root disk: 40G, ~15% used (~5.8G)
- RAM: 7.7 GiB, largely idle with monitor off
- Load ~0
- No swap
- Listening ports: Monitor metrics 8081 is not host-published by default, but host `sshd` listens on port 22 and is blocked only by SG ingress

---

## Common agent tasks (recipes)

### Change Discord webhook (monitor)

1. Current/manual path: edit `/opt/tellor/autoTasks/migration/.env` (mode 600). Target path: make one Parameter Store/Secrets Manager record per secret authoritative and materialize it through a reviewed deployment step.
2. Update `DISCORD_WEBHOOK_URL` for the listed production monitors; `CUSTOM_DISCORD_WEBHOOK_URL` affects generated quickstart monitors only.
3. Recreate: `sudo systemctl reload openzeppelin-monitor` (only if it is meant to be running).
4. Never commit `.env` or `.env.bak.*`. Rotate any webhook pasted into chat and retire stale host backups after rollback needs expire.

### Turn monitor back on

```bash
sudo systemctl enable --now openzeppelin-monitor.service
sudo docker compose -f /opt/tellor/autoTasks/migration/docker-compose.yaml ps
```

Also re-enable/confirm watchdog if you want downtime alerts.

### Turn monitor off cleanly (recommended pair)

```bash
sudo systemctl stop openzeppelin-monitor.service
sudo systemctl disable openzeppelin-monitor.service
sudo systemctl stop monitor-watchdog.timer
sudo systemctl disable monitor-watchdog.timer
```

### Deploy code updates

The current deployment is not reproducible from Git alone. Before the next deployment, create an immutable artifact and manifest recording each repository commit, any intentional dirty-patch checksum, source archive SHA-256, refprice binary SHA-256/build metadata, Monitor image digest, destination paths, deployment time, and rollback artifact. Enable staging-bucket versioning or use immutable release keys. The current code does not fetch Parameter Store values or S3 artifacts automatically.

### Validate Monitor config (when running or via compose run)

See `migration/MIGRATION.md` and, until it is restored to Git, the on-host `AWS_DOCKER_OPERATIONS.md`: `python3 quickstart.py check`, webhooks-off replay patterns, etc. A secret-free local audit passed 54/54 handler/config checks and 39/39 quickstart tests; this verifies the local working tree, not the unmanifested host copy.

---

## Audited risks and missing controls

1. **Intentional-off mismatch:** the Monitor is intentionally off while its watchdog remains enabled, failed, and noisy.
2. **Compromised webhook:** rotate the webhook pasted into chat; then eliminate stale materialized/backup copies.
3. **Root and browser sandboxing:** all generated systemd services run as root, and CPI launches Chrome with `--no-sandbox` against public sites. This is the highest-priority guest hardening defect.
4. **Container credential reachability:** IMDSv2 is correctly required, but hop limit 2 is specifically sufficient for a typical bridge-container hop and may make instance-role credentials reachable from the Monitor network; this path was not live-probed. The Monitor does not need AWS credentials, while the role currently has broad Parameter Store access. After compatibility testing, use hop limit 1 and/or block container access to `169.254.169.254`.
5. **No durable audit trail:** no CloudTrail trail was found. Event History may retain recent management events, but there is no durable multi-Region delivery. Session Manager transcript logging/preferences are also absent.
6. **Alarms have no direct actions:** three CloudWatch alarms exist (`tellor-ops-cpu-high`, `tellor-ops-cpu-credits-low`, `tellor-ops-status-check`) and were `OK`, but all have empty `AlarmActions`/`OKActions`. They perform no direct notification or recovery action; an exhaustive check for external EventBridge/composite consumers was not performed.
7. **No recovery proof:** the root volume deletes on termination. No self-owned snapshot, AWS Backup recovery point, launch template, stop protection, or termination protection was found, and no restore test is documented.
8. **No managed patch baseline:** no SSM association/patch state was found and the SSM Agent is not latest.
9. **Non-reproducible deployment:** host trees lack Git metadata, local sources are dirty/untracked, the staging bucket is unversioned, and there is no artifact manifest or rollback mapping.
10. **Refprice not trustworthy yet:** the last run failed, its log time conflicts with instance launch, its required benchmark is manual, and file existence can represent only an unbenchmarked recovery capture.
11. **CPI false success:** it is USA-only and can exit 0 with all states recorded as `N/A`; its empty `.env` is not the problem.
12. **Metrics profile exposure:** it references sibling bind mounts that may not exist and publishes Grafana 3000 / Prometheus 9090 on all host interfaces. If used, bind to loopback and reach it through SSM port forwarding.
13. **Unbounded logs:** append-only systemd/app logs and Docker logs have no documented rotation or retention; 40 GiB free-space inventory is not a retention policy.

---

## Required setup before choosing the final instance design

### 1) Agree on the workload contract

The first decision is what should run here; instance size and hardening follow from that contract.

| Workload | Known current behavior | Decision still required |
|---|---|---|
| OpenZeppelin Monitor | Intentionally off; if enabled it is a continuous Ethereum-mainnet alert service with local checkpoints | Should it be 24/7? Alert-delivery SLO, maximum block lag, RTO/RPO, owner, and maintenance window |
| Watchdog | Independent timer that treats intentional off as failure; shallow container-running check only | Derive enablement from the same desired-state value as Monitor; define deduplication, recovery alerts, and end-to-end health |
| Reference price | Daily prototype/testnet-grade capture; manual BRRNY dependency; no reporter submission | Keep as research, promote, or remove? Define data-quality gate, owner, backfill behavior, and whether it belongs on this host |
| CPI | Monthly third-Thursday USA Selenium scrape; starts at 16:00 while refprice can still be finishing | Required countries/coverage, useful-result threshold, retry/backfill, owner, and whether to isolate the browser job |

Also decide the availability boundary: one replaceable instance with accepted downtime, a self-healing single-instance design, or separate services/failure domains. Do not size a 24/7 host for a monthly browser spike until co-location is an explicit choice.

### 2) Establish the minimum security and recovery baseline

| Area | Required checkpoint before unattended production |
|---|---|
| Human access | Use IAM Identity Center or another federated identity source for short-lived credentials; map reviewed `Observer`, `Operator`, `Deploy`, and break-glass capability sets to least-privilege permissions using explicit resources and protected tag conditions where supported; require MFA; alert on root use; make mutation preflight reject root; document and test root-only recovery |
| Network | Keep zero ingress; after testing a break-glass recovery path, disable `sshd` if SSM-only is intended; decide explicitly between the present public-egress design and private subnets plus NAT/endpoints; bind any metrics UI to loopback |
| IAM/secrets | Remove effective account-wide Parameter Store reads; make one SecureString/secret per value authoritative; fetch by name on-host; rotate exposed webhook; prevent `.env.bak.*` from entering Git/artifacts |
| Guest/process isolation | Dedicated non-login users, Chrome sandbox enabled, systemd `NoNewPrivileges`/filesystem restrictions, explicit writable paths, and measured CPU/memory limits; for Monitor, test a non-root container UID, dropped capabilities, read-only paths, and resource limits or evaluate a rootless runtime—Docker-group membership remains root-equivalent host control |
| Audit | Multi-Region CloudTrail to encrypted/versioned S3 with validation; encrypted Session Manager transcripts, idle/max duration, and Run As where practical |
| Patching | SSM Quick Setup/Patch Manager: scan first, scheduled install with reboot/health/rollback plan; update the SSM Agent; install and configure the CloudWatch Agent |
| Recovery | Versioned immutable releases, encrypted scheduled backups/snapshots to agreed RPO, durable workload data separated from replaceable OS where useful, and a demonstrated restore |
| Alarms | Attach notification actions or document/test an external routing path and owner; add surplus-credit, memory, disk/inodes, service health, Monitor block lag, refprice freshness/success, and CPI useful-result alarms |
| Reproducibility | Versioned launch template/IaC, current AMI policy, bootstrap that is idempotent and secret-safe, deployment manifest, and tested rollback |

The encrypted current volume is good, but regional EBS encryption-by-default is disabled; enable the regional default for future volumes after choosing the account KMS policy.

### 3) Measure before resizing

Current `t3a.large` facts: 2 vCPU, 8 GiB RAM, Unlimited credits. Roughly the first eight bootstrap-heavy hours showed CPU average about 7.5%, a five-minute maximum about 60%, rising `CPUCreditBalance`, and zero `CPUSurplusCreditBalance`/`CPUSurplusCreditsCharged`. The Monitor is now off, and there are no RAM, filesystem, or process metrics. This evidence does **not** justify downsizing.

Measurement plan:

1. Enable one-minute EC2 monitoring and the CloudWatch Agent for memory, filesystem/inodes, swap/OOM, and per-workload process/container RSS/CPU; ship relevant systemd/Docker/app logs with encryption and retention.
2. Emit low-cardinality workload metrics: Monitor block lag/restarts/errors, refprice success/duration/result freshness, and CPI duration/non-`N/A` coverage/output freshness.
3. Run the intended workload mix until at least one real third-Thursday CPI run and its 16:00 overlap with refprice have been captured; the gap between such runs can exceed 32 days. Include a notification-disabled Monitor catch-up/replay test. If waiting is impractical, run a controlled CPI load test as supplemental evidence.
4. Opt in to Compute Optimizer (currently `Inactive`) after telemetry exists. Its free 32-day preference is better than the default 14 days but may still miss a monthly CPI run; supplement it with the captured/load-test evidence or consider the paid 93-day enhanced lookback. Review p95/p99 plus agreed headroom; do not accept a recommendation that violates the workload SLO.

Candidate comparison set (reported as offered in `us-east-2a` at audit time; recheck offering, quota, capacity, and price before launch):

| Candidate | When it earns a test |
|---|---|
| `t3a.medium` (x86, 2 vCPU / 4 GiB) | Peak memory plus headroom fits below 4 GiB and the lower burst baseline/credit balance remains safe |
| Current `t3a.large` (x86, 2 / 8) | Memory needs 4–8 GiB and CPU remains bursty without surplus charges |
| `c8a.large` (x86, 2 / 4) | CPU is sustained, memory fits 4 GiB, and fixed CPU outperforms Unlimited economics |
| `m8a.large` (x86, 2 / 8) | CPU is sustained and 8 GiB is required |
| `r8a.large` (x86, 2 / 16) | Only if measured memory pressure requires it |
| Arm64 `t4g` / `c8g` / `m8g` / `r8g` | Only after verifying every image, Go binary, Python dependency, browser, and agent; the current x86 AMI/Chrome stack cannot resize in place to Arm |

If CPI creates the only large monthly peak, compare moving it to an on-demand x86 task instead of paying for that headroom continuously.

### 4) Make future collaboration reproducible

Before the next infrastructure mutation, add and review these version-controlled artifacts:

- `WORKLOAD_CONTRACT.md`: the decisions/SLOs/owners above and one explicit desired-state declaration per independent workload; derive watchdog state from Monitor state.
- One IaC implementation (`CDK`, CloudFormation, or Terraform) containing the launch template, IAM, SG, logging, alarms, backups, and tags.
- A secret-free `inventory-readonly` script that prints caller/account/Region and captures EC2, SG, IAM, SSM, volume, alarm, backup, and workload status without parameter values.
- `DEPLOYMENT_MANIFEST.json`: Git commits, dirty-patch prohibition or hash, artifact/binary/image digests, deployed destinations, time, and rollback release.
- The missing `AWS_DOCKER_OPERATIONS.md`, the existing multi-root `scripts/AWS.code-workspace`, restore test, and incident/rollback runbooks.
- Version-controlled, narrowly scoped SSM documents for status, deploy, enable/disable, and rollback instead of arbitrary ad hoc shell strings.

That is the next clean checkpoint: agree on the workload contract and identity/IaC choice, then implement security/telemetry, capture every intended workload (including a real or controlled CPI peak), and select the instance shape from evidence.

---

## Quick status commands (on host)

```bash
sudo systemctl is-enabled openzeppelin-monitor.service; sudo systemctl is-active openzeppelin-monitor.service
sudo systemctl list-timers --all refprice-daily.timer cpi-bigmac.timer monitor-watchdog.timer
sudo docker ps -a --filter name=migration-monitor
sudo docker inspect migration-monitor-1 --format '{{json .State}}'
sudo ls -la /opt/tellor/autoTasks/migration/.env*
sudo tail -n 50 /var/log/tellor/monitor-watchdog.log
sudo tail -n 50 /var/log/tellor/refprice-daily.log
```

From laptop:

```bash
aws sts get-caller-identity  # stop before mutations if Arn ends in :root
aws ec2 describe-instances --region us-east-2 --instance-ids i-00b865643d4023484 \
  --query 'Reservations[0].Instances[0].State.Name'
aws ssm describe-instance-information --region us-east-2 \
  --filters Key=InstanceIds,Values=i-00b865643d4023484
```

---

## Related local workspaces

| Path | Role |
|------|------|
| `/Users/df/projects/dev/autoTasks` | Monitor migration + this handoff |
| `/Users/df/projects/dev/CPI` | Big Mac CPI scraper |
| `/Users/df/projects/dev/layer-daemons` | refprice prototype / daemons |

## Audit provenance

- **Live AWS/host readback:** account/instance/AMI/network/SG/metadata/credit/volume/IAM/Parameter Store metadata/SSM state, alarms/metrics, Compute Optimizer, CloudTrail, backup/snapshots, and installed service/container state at 2026-07-21 04:52–04:59 UTC. Secret values and external webhook delivery were not queried.
- **Local source map:** `migration/docker-compose.yaml`, `migration/scripts/tellor-ops-setup.sh`, all Monitor JSON/handlers/tests, CPI `main.py` + USA Selenium path, and `layer-daemons` refprice command/README/tests.
- **Validation:** Compose config and shell syntax passed; Monitor handler/config suite 54/54; quickstart suite 39/39; focused refprice packages passed. CPI test discovery found 0 tests. These validate local working trees, not the unmanifested deployed copies.
- **Changed claims:** clarified Exit 137, SSM-vs-SSH exposure, effective IAM parameter access, env reload behavior, unused env variables, watchdog depth, refprice maturity/time contradiction, CPI configuration/data-quality behavior, and the insufficiency of current sizing data.
- **Residual uncertainty:** exact secret values/rotation state, root MFA/access-key posture, Discord delivery, external alarm consumers, OS package patch level, external and container-to-IMDS reachability, deployed-file/binary equivalence, Arm compatibility, and representative full-load memory/process data remain unverified.

Current AWS primary references used for the security/sizing guidance:

- [Unlimited burstable-instance behavior](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode-concepts.html)
- [Configure IMDS options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html)
- [Session Manager preferences and logging](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-configure-preferences.html)
- [Compute Optimizer rightsizing preferences](https://docs.aws.amazon.com/compute-optimizer/latest/ug/rightsizing-preferences.html)
- [EBS encryption by default](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-by-default.html)
- [AWS account root-user best practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-best-practices.html)

End of handoff.
