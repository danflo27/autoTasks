# Tellor host workload contract

Status: **DRAFT — deployment blocked until every `REQUIRED DECISION` is owned
and approved.** This file defines local desired-state behavior; it is not
evidence of current EC2 state and does not authorize a host change.

## Desired-state declarations

| Workload | Version-controlled desired state | Required decision before change |
|---|---|---|
| OpenZeppelin Monitor | `disabled` | `REQUIRED DECISION`: owner, 24/7 requirement, delivery SLO, maximum block lag, RTO/RPO, maintenance window |
| Monitor watchdog | `derived-from: openzeppelin-monitor.service` | No independent enablement. It is disabled/stopped with the Monitor and enabled only through the coupled lifecycle command. |
| Confirmed-report freshness checker | `disabled-until-authorized` | `REQUIRED DECISION`: owner, route owners, RPC/archive SLO, response procedure |
| Reference-price prototype | `undecided-preserve-observed-state` | `REQUIRED DECISION`: research vs. production vs. removal, benchmark/data-quality gate, owner, backfill policy |
| Big Mac CPI | `undecided-preserve-observed-state` | `REQUIRED DECISION`: required coverage, useful-result threshold, owner, retry/backfill, browser isolation |

`disabled` is the safe repository default for the Monitor because the latest
dated host handoff observed it intentionally disabled. It does not claim that
the host is still disabled. `undecided-preserve-observed-state` means local
installation tooling must not start, stop, enable, or disable that workload.

## Monitor/watchdog lifecycle invariant

The Monitor and its watchdog have one desired state:

```text
enabled  = Monitor enabled/active + watchdog timer enabled/active
disabled = watchdog timer disabled/inactive + Monitor disabled/inactive
drift    = any other enablement pairing or an unexpected active process
```

Three controls implement the invariant:

1. `ops/monitor_state.py enable|disable` performs ordered transitions, validates
   redacted preflights before enablement, verifies readback, and restores the
   prior unit states if the transition fails.
2. The `openzeppelin-monitor.service` drop-in uses `Also=` for enable/disable
   propagation and `Wants=` for start propagation.
3. The watchdog timer drop-in uses `PartOf=` so stopping the Monitor also stops
   the timer. The watchdog process still reads Monitor enablement and remains
   quiet if a legacy or partial installation leaves the timer running.

Do not enable or disable `monitor-watchdog.timer` independently. Read-only
status is safe:

```sh
cd /opt/tellor/autoTasks/migration
python3 ops/monitor_state.py status
```

`desired_state: drift`, `healthy: false`, an inspection failure, or a nonzero
exit is an operator stop condition. Do not start a workload merely to make the
status command green.

## Idempotent install boundary

The reviewed installer writes only version-controlled unit files/drop-ins plus
the service user and state directories. It does not create, copy, print, or
replace `.env` or `secrets/discord_webhooks.json`, and it does not change
workload enablement or activity:

```sh
sudo /opt/tellor/autoTasks/migration/ops/install-host-ops.sh
```

Run the identity, backup, diff, route, RPC, image, replay, authorization, and
rollback gates in `AWS_DOCKER_OPERATIONS.md` before any `enable` command. The
historical `scripts/tellor-ops-setup.sh` remains non-idempotent and prohibited
as a reconciler.

## Reproducible release and deployment manifest

`ops/release_manifest.py` archives only `HEAD`, refuses every dirty or untracked
worktree change, rejects tracked secret-like paths, requires a content-pinned
Monitor image, and emits a deterministic tar plus `DEPLOYMENT_MANIFEST.json`.
The output directory must be outside the source checkout.

```sh
python3 migration/ops/release_manifest.py build \
  --output-dir /path/to/immutable/release \
  --rollback-release autoTasks@<previous-40-character-commit>
python3 migration/ops/release_manifest.py verify \
  --manifest /path/to/immutable/release/DEPLOYMENT_MANIFEST.json
```

The build manifest records the source commit/tree, dirty-patch prohibition,
archive SHA-256/size, Monitor image digest, destination, source timestamp, and
rollback release. It deliberately says `not-deployed`. After an authorized
deployment and complete host readback, record the deployment metadata locally:

```sh
python3 migration/ops/release_manifest.py record-deployment \
  --manifest /path/to/immutable/release/DEPLOYMENT_MANIFEST.json \
  --authorization-id <approved-change-id> \
  --account-id <12-digit-account> \
  --region <region> \
  --instance-id <instance-id> \
  --deployed-at-utc <YYYY-MM-DDTHH:MM:SSZ>
python3 migration/ops/release_manifest.py verify \
  --require-deployed \
  --manifest /path/to/immutable/release/DEPLOYMENT_MANIFEST.json
```

The schema is `DEPLOYMENT_MANIFEST.schema.json`. Publish the tar and verified
manifest under an immutable/versioned release key; never place `.env`, route
files, data, logs, or secret backups in the artifact.

## Secret-free inventory and status

Control-plane inventory uses only read/list/describe/get-caller APIs. It never
calls `ssm:GetParameter`, Session Manager, or Run Command:

```sh
migration/ops/inventory-readonly.sh aws \
  --region <region> --instance-id <instance-id>
```

Run host inventory locally inside an already authorized host session. It reads
unit/container/checkpoint metadata without environment values:

```sh
/opt/tellor/autoTasks/migration/ops/inventory-readonly.sh host
```

Keep both outputs with the authorization record. A control-plane inventory
cannot prove workload state, and a host inventory cannot prove account-level
backup/IAM/alarm configuration; both are required before a change.

## Required ownership record

The following fields intentionally remain unresolved and block unattended
production:

```text
Monitor owner:
Alert response owner:
RPC/archive owner:
Secret owner and authoritative store:
Deployment approver:
Host operator:
Rollback owner:
Backup/restore owner:
Monitor delivery SLO / maximum block lag / RTO / RPO:
Freshness response SLO:
CPI useful-result threshold and backfill policy:
Reference-price disposition and data-quality gate:
```

## Provenance

- **Source map:** `TELLOR_OPS_EC2_HANDOFF.md` supplied the point-in-time host
  observation and open controls; `TELLOR_MONITORING_PROGRESS.md` supplied the
  implemented Monitor/freshness/watchdog interfaces and evidence boundary;
  `AWS_DOCKER_OPERATIONS.md` supplied the authorization, secret, backup,
  activation, and rollback gates; `ops/monitor_watchdog.py` supplied the
  existing enablement-aware safety behavior.
- **Changed claims:** this contract makes Monitor/watchdog coupling explicit,
  provides an install-only boundary, defines a clean-commit release/manifest
  path, and separates AWS control-plane inventory from host workload status.
- **Assumptions:** systemd and Docker Compose v2 remain the host lifecycle
  mechanisms, `/opt/tellor/autoTasks/migration` remains the intended path, and
  the existing `openzeppelin-monitor.service` remains the Compose owner. The
  read-only inventory must revalidate them.
- **Validation:** Python/unit tests, shell syntax, JSON-schema parse, static
  command allowlist checks, systemd drop-in review, and Git diff/secret scans.
  No AWS, EC2, SSM session, host, secret, RPC, Docker, or Discord operation was
  performed by this local checkpoint.
- **Residual risks:** all ownership/SLO decisions above, current host drift,
  exposed-webhook rotation, target systemd verification, restore evidence, and
  live archive-RPC/destination behavior remain unverified.
