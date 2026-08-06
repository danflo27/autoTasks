# AutoTasks

This repository contains legacy OpenZeppelin Defender autotasks and a local,
self-hosted OpenZeppelin Monitor implementation for Tellor.

## Current implementation

`migration/production/` contains the final local M1-M11 alert-only design dated
2026-08-06.

The production stack uses two containers:

1. `migration/docker-compose.production.yaml` starts the pinned OpenZeppelin
   Monitor v1.5.0 container.
2. The same Compose file builds and starts the `alert-gate` container.

The Monitor loads eight EVM sensors for M1-M8. Its trigger appends raw EVM
`MonitorMatch` records to a durable spool. The trigger does not make the final
alert decision.

The alert gate does the following work:

- It validates final Ethereum receipts and state.
- It replays Tellor Layer blocks.
- It evaluates M1-M11.
- It runs the scheduled M9-M11 absence checks.
- It groups incidents in SQLite.
- It performs final delivery.

M9-M11 do not use OpenZeppelin Monitor JSON files. An event match cannot prove
an absence.

The default delivery mode is `log-only`. The production stack does not send
heartbeat, startup, success, recovery, RPC, parser, or service-health messages.
A fault can send only one opening message. If a live delivery response is
ambiguous, the alert gate keeps the delivery reservation. It does not retry and
risk a duplicate opening.

This repository does not prove deployment, current host state, or Discord
delivery. It does not authorize activation.

Read
[`migration/production/README.md`](migration/production/README.md) for the
final local runtime and its validation-first procedure.

## Historical candidate files

The following files remain as migration evidence:

- `migration/config/`
- `migration/docker-compose.yaml`
- [`migration/MIGRATION.md`](migration/MIGRATION.md)
- [`migration/AWS_DOCKER_OPERATIONS.md`](migration/AWS_DOCKER_OPERATIONS.md)

These files describe the older candidate implementation. The production stack
does not load them. Do not use them as the authoritative production procedure.

[`migration/MONITORING_SOURCE_MAP.md`](migration/MONITORING_SOURCE_MAP.md)
records the external sources and pinned runtime versions for the production
stack.

## Safe local checks

Run these commands from `migration/`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=alert_gate python3 -m unittest discover -s tests -p 'test_alert_gate_*.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_dispute.py tests/test_report_freshness.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/run_tests.py
TELLOR_ENV_FILE=production/environment.example docker compose --env-file production/environment.example -f docker-compose.production.yaml config --quiet
```

These checks do not start the long-running services. They do not intentionally
contact Discord.

`./deploy-production.sh --check` performs additional local checks. It requires
an existing mode-0600 `.env.production`. It creates the required local runtime
and secret directories, validates the Compose configuration, builds
`alert-gate`, and checks both service configurations. It can build or retrieve
container images. It does not start the long-running services, but it is not a
read-only command. Read the production guide before you run it.

## Historical custom-function quickstart

`migration/quickstart.py configure` generates one gitignored custom-function
monitor for the older candidate implementation. Its webhook remains isolated
on `CUSTOM_DISCORD_WEBHOOK_URL`. Plain replay selects `log-only`.

These commands do not configure the final production stack:

```sh
cd migration
python3 quickstart.py configure
python3 quickstart.py replay <confirmed-block-number>
```

Do not use `--start` unless the route preflight passes and an authorized
operator approves activation.

Read the custom-function section in the retained
[`migration/MIGRATION.md`](migration/MIGRATION.md#custom-function-quickstart)
for supported signatures, placeholders, replay behavior, and limitations.
