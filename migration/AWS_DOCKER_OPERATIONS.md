# AWS EC2 and Docker operations for Tellor monitoring

This is the operational runbook for installing, activating, reading back, and
rolling back the local Tellor monitoring implementation on the intended EC2
host.

It is a **procedure template**, not a deployment record. No command in this
document has been run on the host as part of the local implementation. The
latest dated handoff says the OpenZeppelin Monitor service was disabled and
inactive; verify current state before relying on that observation.

## 1. Safety rules

1. Do not deploy without an explicit authorization record naming the AWS
   account, host, window, approver, and rollback owner.
2. Treat any webhook that appeared in chat, logs, shell history, screenshots,
   or tickets as exposed. Rotate it before activation.
3. Never print, diff, checksum, archive into an ordinary backup, or commit
   `.env` or `secrets/discord_webhooks.json`.
4. Never run with `set -x`, `env`, `printenv`, unredacted `docker compose
   config`, or `docker inspect ...Config.Env` around secrets.
5. Never rerun `scripts/tellor-ops-setup.sh` wholesale. It is a historical,
   non-idempotent bootstrap. Use only the additive `ops/install-monitoring.sh`.
6. Back up and checksum the exact non-secret installed files before replacing
   them. Preserve state, checkpoints, logs, and failed artifacts.
7. Plain replay is `log-only`. A real destination proof is a separate,
   explicitly authorized external write.
8. Stop at the first failed gate. Do not weaken permissions or bypass a
   preflight to make activation succeed.

Commands below assume the application is installed at
`/opt/tellor/autoTasks/migration`. If the read-only inventory proves otherwise,
stop and update the reviewed plan; do not guess a second path.

## 2. Authorization record and placeholders

Complete this record outside the secret file:

```text
Change/authorization ID:
AWS account ID and role:
EC2 instance ID and hostname:
Maintenance window (UTC):
Approver:
Operator:
Rollback owner:
Previous Monitor desired state (enabled/disabled):
Previous freshness/watchdog timer states:
Authoritative secret store and secret owner:
Webhook rotation completed at (UTC):
Distinct destination owners/count (no URLs):
Backup directory:
Reviewed staging directory/checksum:
Go/no-go decision and time:
```

For the command templates, set task-specific paths after verifying them:

```sh
TELLOR_MIGRATION_DIR=/opt/tellor/autoTasks/migration
TELLOR_STAGE_DIR=/path/to/reviewed-staging-directory/migration
TELLOR_BACKUP_DIR=/opt/tellor/backups/monitoring/YYYYMMDDTHHMMSSZ
TELLOR_ROUTE_FILE=/opt/tellor/autoTasks/migration/secrets/discord_webhooks.json
```

Do not reuse a broad directory such as `/`, a home directory, or
`/opt/tellor` as the backup or staging target.

## 3. Read-only identity and current-state inventory

Run these before any write:

```sh
hostname
id
aws sts get-caller-identity
/usr/bin/python3 --version
sudo systemctl is-enabled openzeppelin-monitor.service
sudo systemctl is-active openzeppelin-monitor.service
sudo systemctl is-enabled tellor-report-freshness.timer monitor-watchdog.timer
sudo systemctl is-active tellor-report-freshness.timer monitor-watchdog.timer
sudo systemctl list-timers --all \
  tellor-report-freshness.timer monitor-watchdog.timer
sudo docker compose --project-directory "$TELLOR_MIGRATION_DIR" \
  -f "$TELLOR_MIGRATION_DIR/docker-compose.yaml" ps
sudo stat -c '%n type=%F mode=%a owner=%U group=%G' \
  "$TELLOR_MIGRATION_DIR" \
  "$TELLOR_MIGRATION_DIR/.env" \
  "$TELLOR_MIGRATION_DIR/secrets" \
  "$TELLOR_ROUTE_FILE"
```

`not-found` is valid inventory evidence for units that have never been
installed. A missing route file is expected before first installation. The
host Python must be 3.9 or newer; record its exact version because the host
freshness/watchdog services do not run inside the Monitor image. Record the
result; do not create anything yet.

Inspect non-secret unit definitions and Compose syntax without expanding
environment values:

```sh
sudo systemctl cat openzeppelin-monitor.service
sudo systemctl cat tellor-report-freshness.service
sudo systemctl cat monitor-watchdog.service
cd "$TELLOR_MIGRATION_DIR"
sudo docker compose config --quiet
```

Stop if the AWS identity, hostname, install path, or existing desired state
differs from the authorization record.

## 4. Authorization and secret-rotation gate

Before staging files, confirm all of the following:

- The approver authorized this exact account, instance, window, and scope.
- A rollback owner is present for the window.
- The exposed/old webhook has been revoked or rotated by the destination owner.
- The authoritative secret manager—not this repository or a shell transcript—
  holds the new values.
- Destination owners approved one controlled proof per distinct destination.
- RPC owners confirmed chain IDs, archive retention for historical EVMCall, and
  quotas for the primary and fallbacks.

If rotation cannot be confirmed, the decision is **no-go**. A locally passing
route preflight does not prove that an old destination credential is safe.

## 5. Back up, diff, and checksum the reviewed delta

### 5.1 Create a protected non-secret backup

Create the exact backup directory from the authorization record:

```sh
sudo test ! -e "$TELLOR_BACKUP_DIR"
sudo install -d -o root -g root -m 0700 "$TELLOR_BACKUP_DIR"
sudo install -d -o root -g root -m 0700 "$TELLOR_BACKUP_DIR/files"
sudo install -d -o root -g root -m 0700 "$TELLOR_BACKUP_DIR/files/systemd"
sudo install -o root -g root -m 0600 /dev/null \
  "$TELLOR_BACKUP_DIR/files/systemd/.absent-units"
```

Back up only repository/runtime definitions. Explicitly exclude `.env`,
`secrets/`, `data/`, and `logs/` from this ordinary backup:

```sh
sudo cp -a "$TELLOR_MIGRATION_DIR/config" \
  "$TELLOR_BACKUP_DIR/files/config"
sudo cp -a "$TELLOR_MIGRATION_DIR/docker-compose.yaml" \
  "$TELLOR_MIGRATION_DIR/quickstart.py" \
  "$TELLOR_BACKUP_DIR/files/"
sudo test ! -e "$TELLOR_MIGRATION_DIR/report_freshness.py" || \
  sudo cp -a "$TELLOR_MIGRATION_DIR/report_freshness.py" \
    "$TELLOR_BACKUP_DIR/files/"
sudo test ! -e "$TELLOR_MIGRATION_DIR/ops" || \
  sudo cp -a "$TELLOR_MIGRATION_DIR/ops" "$TELLOR_BACKUP_DIR/files/ops"
```

The installer writes four system unit files outside the repository. Preserve
each prior file or explicitly record that it was absent; both facts are needed
for an exact rollback:

```sh
for unit in \
  tellor-report-freshness.service \
  tellor-report-freshness.timer \
  monitor-watchdog.service \
  monitor-watchdog.timer
do
  unit_path="/etc/systemd/system/$unit"
  if sudo test -L "$unit_path"; then
    sudo readlink -- "$unit_path" | \
      sudo tee "$TELLOR_BACKUP_DIR/files/systemd/$unit.symlink-target" >/dev/null
    sudo chmod 0600 \
      "$TELLOR_BACKUP_DIR/files/systemd/$unit.symlink-target"
    sudo cp -a -- "$unit_path" "$TELLOR_BACKUP_DIR/files/systemd/$unit"
  elif sudo test -e "$unit_path"; then
    sudo cp -a -- "$unit_path" "$TELLOR_BACKUP_DIR/files/systemd/$unit"
  else
    printf '%s\n' "$unit" | \
      sudo tee -a "$TELLOR_BACKUP_DIR/files/systemd/.absent-units" >/dev/null
  fi
done
sudo chmod 0600 "$TELLOR_BACKUP_DIR/files/systemd/.absent-units"
```

Review the absence manifest and the metadata of any captured unit without
printing environment-file contents. A unit that is neither captured nor
listed as previously absent is a no-go. Each captured symlink has a regular
`.symlink-target` record under `files/systemd/`; the checksum pass below covers
that record, and rollback compares it with the preserved symlink before use.

If `.env` needs rollback protection, use the approved encrypted secret store.
Do not copy it into `TELLOR_BACKUP_DIR`. Do not back up webhook values; a
compromised or rotated webhook must never be restored.

Record a checksum of the non-secret backup:

```sh
set -o pipefail
sudo find "$TELLOR_BACKUP_DIR/files" -type f -exec sha256sum {} \; \
  | sudo sort -k2 \
  | sudo tee "$TELLOR_BACKUP_DIR/before.sha256" >/dev/null
sudo chmod 0600 "$TELLOR_BACKUP_DIR/before.sha256"
```

### 5.2 Review staging without secrets

The staging directory must come from the reviewed commit/artifact. It must not
contain `.env`, `secrets/`, runtime data, or logs:

```sh
test -d "$TELLOR_STAGE_DIR/config"
test -f "$TELLOR_STAGE_DIR/docker-compose.yaml"
test -f "$TELLOR_STAGE_DIR/quickstart.py"
test -f "$TELLOR_STAGE_DIR/report_freshness.py"
test -f "$TELLOR_STAGE_DIR/ops/install-monitoring.sh"
test ! -e "$TELLOR_STAGE_DIR/.env"
test ! -e "$TELLOR_STAGE_DIR/secrets"
```

Generate a non-secret staging checksum and compare it with the approved
artifact manifest:

```sh
find "$TELLOR_STAGE_DIR/config" "$TELLOR_STAGE_DIR/ops" -type f \
  -exec sha256sum {} \; | sort -k2
sha256sum \
  "$TELLOR_STAGE_DIR/docker-compose.yaml" \
  "$TELLOR_STAGE_DIR/quickstart.py" \
  "$TELLOR_STAGE_DIR/report_freshness.py"
```

Review a recursive diff of `config/` and explicit diffs of the top-level
files. `diff` exit code `1` means differences were found for review; any code
greater than `1` is an error and blocks deployment.

```sh
diff -ruN "$TELLOR_MIGRATION_DIR/config" "$TELLOR_STAGE_DIR/config"
diff -u "$TELLOR_MIGRATION_DIR/docker-compose.yaml" \
  "$TELLOR_STAGE_DIR/docker-compose.yaml"
diff -u "$TELLOR_MIGRATION_DIR/quickstart.py" \
  "$TELLOR_STAGE_DIR/quickstart.py"
```

Store the reviewed, secret-free diff with the change record. Do not run a broad
repository diff that can enter `secrets/` or expose `.env`.

## 6. Install the reviewed files without enabling services

### 6.1 Copy only reviewed deltas

Move the three retired monitor configs into the protected backup rather than
deleting them. A missing source is acceptable; an unexpected additional
`submitValue` monitor is not.

```sh
sudo install -d -o root -g root -m 0700 \
  "$TELLOR_BACKUP_DIR/retired-monitor-configs"
for name in dvm.json evm_call.json tellorflex_data_report.json; do
  if sudo test -e "$TELLOR_MIGRATION_DIR/config/monitors/$name"; then
    sudo mv "$TELLOR_MIGRATION_DIR/config/monitors/$name" \
      "$TELLOR_BACKUP_DIR/retired-monitor-configs/$name"
  fi
done
```

Copy the reviewed config and operator files. This deliberately does not use
`--delete` and does not touch secrets or runtime state:

```sh
sudo rsync -a --itemize-changes "$TELLOR_STAGE_DIR/config/" \
  "$TELLOR_MIGRATION_DIR/config/"
sudo install -o root -g root -m 0644 \
  "$TELLOR_STAGE_DIR/docker-compose.yaml" \
  "$TELLOR_MIGRATION_DIR/docker-compose.yaml"
sudo install -o root -g root -m 0755 \
  "$TELLOR_STAGE_DIR/quickstart.py" \
  "$TELLOR_MIGRATION_DIR/quickstart.py"
sudo install -o root -g root -m 0755 \
  "$TELLOR_STAGE_DIR/report_freshness.py" \
  "$TELLOR_MIGRATION_DIR/report_freshness.py"
sudo rsync -a --itemize-changes "$TELLOR_STAGE_DIR/ops/" \
  "$TELLOR_MIGRATION_DIR/ops/"
```

Install the freshness/watchdog units and create their service user/state
directories, but do not enable them:

```sh
sudo "$TELLOR_MIGRATION_DIR/ops/install-monitoring.sh"
sudo systemctl is-enabled tellor-report-freshness.timer monitor-watchdog.timer
```

The expected installer message says the units were installed but not enabled.

### 6.2 Install the route secret

The installer creates `tellor-monitoring`. Create the secret directory for that
user so both the unprivileged freshness checker and the root-run container can
read the `0600` file:

```sh
sudo install -d -o tellor-monitoring -g tellor-monitoring -m 0700 \
  "$TELLOR_MIGRATION_DIR/secrets"
sudo test ! -e "$TELLOR_ROUTE_FILE"
sudo install -o tellor-monitoring -g tellor-monitoring -m 0600 \
  "$TELLOR_MIGRATION_DIR/config/discord_webhooks.example.json" \
  "$TELLOR_ROUTE_FILE"
```

Fill the empty values through the approved secret-manager/editor workflow.
Never pass a webhook as a command-line argument. Then reassert and read back
metadata only:

```sh
sudo chown tellor-monitoring:tellor-monitoring "$TELLOR_ROUTE_FILE"
sudo chmod 0700 "$TELLOR_MIGRATION_DIR/secrets"
sudo chmod 0600 "$TELLOR_ROUTE_FILE"
sudo test ! -L "$TELLOR_ROUTE_FILE"
sudo stat -c '%n type=%F mode=%a owner=%U group=%G' \
  "$TELLOR_MIGRATION_DIR/secrets" "$TELLOR_ROUTE_FILE"
```

For later rotation, create a `0600` sibling file, validate it, and use `mv` on
the same filesystem for atomic replacement. Never edit the live file in place.

### 6.3 Update `.env` without revealing it

Use the approved secret editor. Keep RPC URLs and API keys out of shell
history. Ensure these interfaces are set appropriately:

- `RPC_ETHEREUM_MAINNET` and available mainnet fallbacks;
- target-chain RPCs needed for EVMCall validation;
- `DISCORD_WEBHOOKS_FILE=secrets/discord_webhooks.json` for host-side tools;
- `TELLOR_ALERT_DELIVERY_MODE=live` for long-running services;
- `CUSTOM_DISCORD_WEBHOOK_URL` only if the isolated quickstart monitor is used.

Retire `DISCORD_WEBHOOK_URL`, `MONITOR_WATCHDOG_WEBHOOK`, and
watchdog-specific Discord variables. Verify only their absence, never values:

```sh
if sudo grep -Eq \
  '^(DISCORD_WEBHOOK_URL|MONITOR_WATCHDOG_WEBHOOK|DISCORD_WEBHOOK)=' \
  "$TELLOR_MIGRATION_DIR/.env"; then
  echo 'legacy production webhook key remains; stop' >&2
  exit 1
fi
sudo test -f "$TELLOR_MIGRATION_DIR/.env"
sudo test ! -L "$TELLOR_MIGRATION_DIR/.env"
sudo chown root:root "$TELLOR_MIGRATION_DIR/.env"
sudo chmod 0600 "$TELLOR_MIGRATION_DIR/.env"
sudo stat -c '%n type=%F mode=%a owner=%U group=%G' \
  "$TELLOR_MIGRATION_DIR/.env"
```

## 7. Pull and validate before starting

Everything in this section is a gate. None of it enables the long-running
Monitor or timers.

### 7.1 Run local deterministic suites

Run the test suites from the reviewed staging artifact, then validate the
installed Compose file without printing its resolved environment:

```sh
cd "$TELLOR_STAGE_DIR"
python3 -m unittest tests/test_discord_routes.py
python3 -m unittest tests/test_delivery_integration.py
python3 -m unittest tests/test_dispute.py
python3 -m unittest tests/test_report_freshness.py
python3 -m unittest tests/test_monitoring_ops_hardening.py
python3 -m unittest tests/test_quickstart.py
python3 tests/run_tests.py
cd "$TELLOR_MIGRATION_DIR"
sudo docker compose config --quiet
```

Any failure is a no-go.

### 7.2 Run redacted route and RPC preflights

```sh
cd "$TELLOR_MIGRATION_DIR"
sudo -u tellor-monitoring test -r "$TELLOR_ROUTE_FILE"
sudo env \
  DISCORD_WEBHOOKS_FILE="$TELLOR_ROUTE_FILE" \
  python3 quickstart.py check-discord-routes
sudo python3 quickstart.py check-rpcs
```

The route command may print names and statuses only. It must not print URLs or
fragments. The paused smoke route may be absent; every enabled/host producer
route must be valid. An unknown extra route is a warning requiring review.

### 7.3 Pull and validate the pinned Monitor image

Pulling changes the local image cache but does not start a service:

```sh
cd "$TELLOR_MIGRATION_DIR"
sudo docker compose pull monitor
sudo python3 quickstart.py check
```

The Compose file pins both v1.5.0 and its digest. Stop if the resolved image or
validation output differs from the reviewed source map.

### 7.4 Explicit log-only replay

Use a reviewed, confirmed historical block. This template uses the local
`NewReport` fixture block; verify the block remains appropriate for the
configured RPC:

```sh
cd "$TELLOR_MIGRATION_DIR"
sudo docker compose run --rm \
  -e TELLOR_ALERT_DELIVERY_MODE=log-only \
  monitor \
  --monitor-path /app/config/monitors/tellorflex_disputable_value.json \
  --network ethereum_mainnet \
  --block 25526730
```

Confirm the expected local `NewReport` outcome and zero intended Discord
delivery. Do not add `--send` or switch the mode to `live` during this gate.

## 8. Go/no-go and activation

Hold a go/no-go review after every Section 7 check passes. The approver must
confirm:

- current backup and checksum paths;
- current secret rotation and route preflight;
- rollback owner availability;
- prior desired state and intended new desired state;
- authorized destination-proof scope.

### 8.1 Activate the Monitor

The existing host unit owns Compose lifecycle. Enable/start it only after the
go decision:

```sh
sudo systemctl enable --now openzeppelin-monitor.service
sudo systemctl is-enabled openzeppelin-monitor.service
sudo systemctl is-active openzeppelin-monitor.service
```

Because Monitor caches configuration/scripts, any later reviewed config or
script change requires a force-recreate through the unit's reviewed reload or
restart procedure.

### 8.2 Enable freshness and watchdog timers

Only after the Monitor is running and its checkpoint advances:

```sh
sudo "$TELLOR_MIGRATION_DIR/ops/install-monitoring.sh" --enable
```

The installer reruns route, ownership, RPC, and systemd verification before it
enables both timers.

## 9. Required runtime readback

Capture UTC timestamps and command results without environment dumps.

### 9.1 Service, container, and checkpoint

```sh
date -u +%Y-%m-%dT%H:%M:%SZ
sudo systemctl is-enabled openzeppelin-monitor.service
sudo systemctl is-active openzeppelin-monitor.service
sudo docker compose --project-directory "$TELLOR_MIGRATION_DIR" \
  -f "$TELLOR_MIGRATION_DIR/docker-compose.yaml" ps monitor
sudo test -s "$TELLOR_MIGRATION_DIR/data/ethereum_mainnet_last_block.txt"
sudo stat -c '%n type=%F mode=%a mtime=%y' \
  "$TELLOR_MIGRATION_DIR/data/ethereum_mainnet_last_block.txt"
sudo tail -n 1 "$TELLOR_MIGRATION_DIR/data/ethereum_mainnet_last_block.txt"
```

Read the checkpoint twice after at least one polling/confirmation interval and
record that it advances. A running container without an advancing checkpoint
is not healthy.

### 9.2 Timers, heartbeat, and state

```sh
sudo systemctl is-enabled tellor-report-freshness.timer monitor-watchdog.timer
sudo systemctl is-active tellor-report-freshness.timer monitor-watchdog.timer
sudo systemctl list-timers --all \
  tellor-report-freshness.timer monitor-watchdog.timer
sudo systemctl status --no-pager tellor-report-freshness.service
sudo systemctl status --no-pager monitor-watchdog.service
sudo stat -c '%n type=%F mode=%a owner=%U group=%G mtime=%y' \
  /var/lib/tellor/report-freshness/state.json \
  /var/lib/tellor/report-freshness/heartbeat \
  /var/lib/tellor/monitor-watchdog/state.json
sudo cat /var/lib/tellor/report-freshness/heartbeat
```

The heartbeat may contain only timestamp, status, and confirmed block. Expected
healthy status is `ok`; `rpc-failure` and `delivery-failure` require action.

### 9.3 Logs and redacted preflight

```sh
cd "$TELLOR_MIGRATION_DIR"
sudo -u tellor-monitoring test -r "$TELLOR_ROUTE_FILE"
sudo env \
  DISCORD_WEBHOOKS_FILE="$TELLOR_ROUTE_FILE" \
  python3 quickstart.py check-discord-routes
sudo journalctl --since '30 minutes ago' \
  -u openzeppelin-monitor.service \
  -u tellor-report-freshness.service \
  -u monitor-watchdog.service \
  --no-pager
sudo tail -n 50 "$TELLOR_MIGRATION_DIR/logs/alerts.log"
sudo tail -n 50 /var/lib/tellor/report-freshness/alerts.log
sudo tail -n 50 /var/lib/tellor/monitor-watchdog/alerts.log
```

Stop and investigate any URL/token fragment, script failure, stale checkpoint,
unexplained missing heartbeat, or repeated service failure.

## 10. Controlled destination proof

This is an external write. Do not perform it unless the authorization record
names the destination owners and proof window.

The secret owner determines which route names share a destination without
printing URLs. Send one controlled message per **distinct destination**, not per
route. The following template names a route but never exposes its URL:

```sh
cd "$TELLOR_MIGRATION_DIR"
sudo -u tellor-monitoring env \
  DISCORD_WEBHOOKS_FILE="$TELLOR_ROUTE_FILE" \
  PYTHONPATH="$TELLOR_MIGRATION_DIR/config/triggers/scripts" \
  python3 -c 'from discord_routes import deliver_alert; deliver_alert("Tellor ETH/USD Freshness", "Controlled Tellor monitoring delivery proof; no incident.", mode="live", log_path="/var/lib/tellor/report-freshness/delivery-proof.log", context={"source":"authorized-delivery-proof","status":"test"})'
```

Replace only the route name with another exact registered name. Record UTC,
route name, destination owner/label, operator, confirmation result/message ID,
and authorization ID. Never record the URL or token.

If a proof fails, do not loop across destinations. Stop, preserve the local
record, and let the destination owner investigate.

## 11. Daily operations and troubleshooting

Daily/incident readback is read-only:

```sh
sudo systemctl --failed
sudo systemctl list-timers --all \
  tellor-report-freshness.timer monitor-watchdog.timer
sudo docker compose --project-directory "$TELLOR_MIGRATION_DIR" \
  -f "$TELLOR_MIGRATION_DIR/docker-compose.yaml" ps monitor
sudo cat /var/lib/tellor/report-freshness/heartbeat
sudo stat -c '%n mtime=%y' \
  "$TELLOR_MIGRATION_DIR/data/ethereum_mainnet_last_block.txt"
```

| Symptom | First checks | Do not do |
|---|---|---|
| Route preflight fails | File type/modes/owner, exact names, secure JSON edit | Do not relax `0700`/`0600` or restore an exposed URL |
| Freshness `rpc-failure` | Mainnet chain ID, confirmed-head age, all configured providers | Do not convert RPC failure into a missing-report alert |
| Freshness `delivery-failure` | Route status, destination ownership/status, local alert log | Do not manually edit state to suppress retry |
| Watchdog warning | Desired state, container state, checkpoint age/advance, RPC logs | Do not restart blindly before preserving evidence |
| Monitor intentionally stopped | Confirm unit is disabled and authorization record agrees | Do not enable merely to silence the watchdog |
| Corrupt state artifact | Preserve `.corrupt.*`, inspect permissions/disk/process history | Do not delete the artifact or backdate deduplication state |
| Discord delivery exhausted | Local record, destination status, rate limiting | Do not paste the webhook into diagnostics |

## 12. Rollback

Rollback is a controlled change. The rollback owner must confirm the target
backup and previous desired state before commands run.

### 12.1 Quiesce new producers

```sh
sudo systemctl disable --now tellor-report-freshness.timer
sudo systemctl disable --now monitor-watchdog.timer
sudo systemctl disable --now openzeppelin-monitor.service
```

This order prevents freshness/watchdog transitions during file restoration.
If the Monitor was enabled before the change, that fact belongs in the
authorization record and is restored only after file verification.

### 12.2 Preserve failure evidence

Create a new, exact incident directory; never reuse the backup root or remove
the originals:

```sh
TELLOR_ROLLBACK_EVIDENCE=/var/lib/tellor/rollback-evidence/YYYYMMDDTHHMMSSZ
sudo test ! -e "$TELLOR_ROLLBACK_EVIDENCE"
sudo install -d -o root -g root -m 0700 "$TELLOR_ROLLBACK_EVIDENCE"
sudo cp -a "$TELLOR_MIGRATION_DIR/data" \
  "$TELLOR_MIGRATION_DIR/logs" \
  /var/lib/tellor/report-freshness \
  /var/lib/tellor/monitor-watchdog \
  "$TELLOR_ROLLBACK_EVIDENCE/"
```

These artifacts can contain alert content and infrastructure metadata; keep
the evidence directory restricted. They should not contain webhook URLs.

### 12.3 Verify and restore the non-secret backup

```sh
sudo sha256sum -c "$TELLOR_BACKUP_DIR/before.sha256"
```

The checksum file records absolute backup paths, so validate it before using
the restore. Restore only the reviewed paths. For `config/`, `--delete` is
intentional and scoped: it removes post-change config files so the directory
exactly matches the backup.

```sh
sudo rsync -a --delete "$TELLOR_BACKUP_DIR/files/config/" \
  "$TELLOR_MIGRATION_DIR/config/"
sudo install -o root -g root -m 0644 \
  "$TELLOR_BACKUP_DIR/files/docker-compose.yaml" \
  "$TELLOR_MIGRATION_DIR/docker-compose.yaml"
sudo install -o root -g root -m 0755 \
  "$TELLOR_BACKUP_DIR/files/quickstart.py" \
  "$TELLOR_MIGRATION_DIR/quickstart.py"
```

Restore `report_freshness.py` and `ops/` only if they existed in the recorded
pre-change manifest. If they were newly introduced, leave them inert while
their timers remain disabled; remove them only under a separate reviewed
cleanup.

Restore the exact pre-change systemd unit state. A captured unit replaces the
installed version; a unit recorded as previously absent is removed. Stop if a
name is represented by neither form:

```sh
for unit in \
  tellor-report-freshness.service \
  tellor-report-freshness.timer \
  monitor-watchdog.service \
  monitor-watchdog.timer
do
  saved_unit="$TELLOR_BACKUP_DIR/files/systemd/$unit"
  saved_target="$saved_unit.symlink-target"
  unit_path="/etc/systemd/system/$unit"
  if sudo test -e "$saved_target" || sudo test -L "$saved_target"; then
    if ! sudo test -f "$saved_target" || sudo test -L "$saved_target"; then
      echo "missing regular symlink-target record for $unit; stop" >&2
      exit 1
    fi
    if ! sudo test -L "$saved_unit"; then
      echo "saved $unit no longer matches its symlink-target record; stop" >&2
      exit 1
    fi
    expected_target=$(sudo cat -- "$saved_target")
    actual_target=$(sudo readlink -- "$saved_unit")
    if [[ "$actual_target" != "$expected_target" ]]; then
      echo "saved symlink target mismatch for $unit; stop" >&2
      exit 1
    fi
    sudo cp -a --remove-destination -- "$saved_unit" "$unit_path"
  elif sudo test -L "$saved_unit"; then
    echo "missing symlink-target record for $unit; stop" >&2
    exit 1
  elif sudo test -f "$saved_unit"; then
    sudo cp -a --remove-destination -- "$saved_unit" "$unit_path"
  elif sudo test -e "$saved_unit"; then
    echo "saved $unit is not a regular file or recorded symlink; stop" >&2
    exit 1
  elif sudo grep -Fxq "$unit" "$TELLOR_BACKUP_DIR/files/systemd/.absent-units"; then
    sudo rm -f -- "$unit_path"
  else
    echo "missing prior-state record for $unit; stop" >&2
    exit 1
  fi
done
```

Do **not** restore `.env` or a webhook from this backup. Reinstall the currently
authorized secret version from the secret manager. If compromise is suspected,
rotate again.

### 12.4 Reload and restore the previous desired state

```sh
sudo systemctl daemon-reload
cd "$TELLOR_MIGRATION_DIR"
sudo docker compose config --quiet
```

- If the Monitor was previously disabled, leave it disabled. The watchdog is
  desired-state-aware and should remain quiet.
- If it was previously enabled, run the restored configuration validation,
  then enable/start `openzeppelin-monitor.service`. A recreated container is
  required for restored cached scripts/config.
- Re-enable freshness/watchdog timers only if they were enabled before the
  change and their restored files/routes remain valid.

Repeat all applicable Section 9 readbacks and record the final state. Rollback
is not complete until desired state, container, checkpoint, heartbeat, timers,
logs, route preflight, and preserved evidence are accounted for.

## 13. Updates and decommissioning

For any update, repeat Sections 3–10 with a new authorization, backup, staging
checksum, and rollback directory. Changing the Monitor image tag/digest,
TellorFlex address, query ID, source pin, route registry, RPC policy, or systemd
hardening requires source-map review and the full local suite.

For decommissioning, first authorize and record the desired stopped state,
then disable the freshness and watchdog timers and the Monitor unit. Preserve
state/logs/checkpoints under the retention policy. Revoke webhooks and RPC
credentials through their owners. Do not delete evidence or secrets merely
because a service is disabled.

## 14. Evidence template

```text
Authorization ID:
Operator / approver / rollback owner:
AWS account / instance / hostname:
Window start/end UTC:
Previous desired state:
Backup path and checksum result:
Reviewed staging checksum:
Webhook rotation confirmation (owner/time only; no URL):
Route preflight result:
RPC preflight result:
Local suites and pinned-image check:
Log-only replay block/result:
Go/no-go decision:
Monitor enabled/active/container/checkpoint readback:
Freshness timer/state/heartbeat readback:
Watchdog timer/state readback:
Controlled destinations proved (labels only):
Errors/deviations:
Rollback invoked? result/evidence path:
Final desired and observed state:
```

## Provenance

- **Source map:** [`MONITORING_SOURCE_MAP.md`](MONITORING_SOURCE_MAP.md) pins
  the external runtime/protocol/reporter sources. `MIGRATION.md` defines local
  behavior; `TELLOR_MONITORING_PROGRESS.md` owns evidence and deployment gates.
- **Changed claims:** this runbook replaces the missing/stale operations link
  with an additive, authorization-gated procedure for the implemented routes,
  unified report monitor, freshness checker, and desired-state watchdog.
- **Assumptions:** the intended path is `/opt/tellor/autoTasks/migration`, the
  host uses systemd and Docker Compose v2, and the existing
  `openzeppelin-monitor.service` remains the Compose lifecycle owner. Section 3
  must verify each assumption.
- **Validation:** local source/unit readback, command review for secret exposure
  and destructive scope, Markdown-link validation, and secret-pattern scan. No
  EC2, AWS, systemd, Docker runtime, or Discord mutation is claimed.
- **Residual risks:** the host is a copied non-Git tree; current state may have
  drifted since the handoff; Monitor is alpha; Docker access is privileged; no
  durable Discord outbox exists; target-host ownership and archive-RPC behavior
  require runtime proof.
