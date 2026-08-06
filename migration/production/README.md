# Tellor production alert-only monitors

This directory contains the final local M1-M11 alert-only design dated
2026-08-06.

The production stack is `../docker-compose.production.yaml`. The older files in
`../config/` and the older `../docker-compose.yaml` remain as migration
evidence. The production stack does not load those files.

This local implementation does not prove deployment, current host state, or
Discord delivery. It does not authorize activation.

## Runtime design

The production runtime has two containers.

1. OpenZeppelin Monitor v1.5.0 loads the eight EVM sensor files in
   `config/monitors/`. Its `tellor_alert` script only appends the raw
   `MonitorMatch` record to a persistent spool.
2. `alert-gate` consumes the spool. It validates final Ethereum receipts and
   state, replays committed Tellor Layer blocks, evaluates M1-M11, groups
   incidents in SQLite, and performs final delivery.

M9-M11 are scheduled absence checks. They do not have OpenZeppelin Monitor JSON
files. An event match cannot prove an absence.

The default external-delivery mode is `log-only`. The runtime does not send
heartbeat, startup, success, recovery, RPC, parser, or service-health messages.

A fault can send only one opening message. The alert gate stores later evidence
in SQLite. If a live delivery response is ambiguous, the alert gate keeps the
delivery reservation. It does not retry and risk a duplicate opening.

## Repository layout

- `config/monitors/` contains eight OpenZeppelin Monitor v1.5.0 EVM prefilters
  for M1-M8.
- `config/triggers/scripts/alert.py` writes each raw match to the durable spool.
- `policy/monitors.json` contains the exact M1-M11 logical catalog.
- `policy/approved_changes.json` contains reviewed M1/M2 control-change
  approvals.
- `policy/enrolled_databridges.json` contains the pinned M3 DataBridge
  enrollment.
- `policy/bridge_ledger_seed.example.json` defines the reviewed M4 cross-chain
  checkpoint and pending-event schema.
- `policy/layer_minter_seed.example.json` defines the Layer minter checkpoint
  schema.
- `policy/discord_routes.example.json` contains the exact eleven live-delivery
  route names.
- `../alert_gate/` contains the receipt, state, Layer, correlation, schedule,
  incident, and delivery code.
- `../docker-compose.production.yaml` defines the pinned two-container stack.
- `../deploy-production.sh` is the validation-first entry point.

## Host preparation

The target `t3a.large` has 2 vCPUs and 8 GiB of memory. Compose limits the
Monitor to 0.8 CPU and 2 GiB. Compose limits `alert-gate` to 0.9 CPU and 2 GiB.
These limits leave host capacity for Docker, the kernel, log rotation, and
short load spikes.

Both containers use the same non-root host UID and GID. Both containers use the
shared durable spool.

From `migration/`, create the environment and seed paths:

```sh
cp production/environment.example .env.production
chmod 600 .env.production
mkdir -p secrets/release-manifests
chmod 700 secrets secrets/release-manifests
cp production/policy/bridge_ledger_seed.example.json \
  secrets/bridge_ledger_seed.json
cp production/policy/layer_minter_seed.example.json \
  secrets/layer_minter_seed.json
chmod 600 secrets/bridge_ledger_seed.json secrets/layer_minter_seed.json
```

The example seeds contain deliberate placeholders. They cannot pass
validation.

Configure these inputs in `.env.production`:

- Two independent Ethereum providers.
- The Tellor Layer endpoint.
- All required historical EVMCall providers.
- The Layer replay start height.

Set `.env.production` to mode 0600.

## Seed and checkpoint requirements

Always create `secrets/bridge_ledger_seed.json` from an independently reviewed
checkpoint.

If `LAYER_REPLAY_START_HEIGHT` is greater than `1`, also create
`secrets/layer_minter_seed.json` from the same independently reviewed
checkpoint.

Set each populated seed file to mode 0600.

The Layer checkpoint block identity and time must be identical in both seed
files. Set `LAYER_REPLAY_START_HEIGHT` to the next height.

For a full replay from height `1`, use these values:

- Set the bridge seed Layer height to `0`.
- Set the Layer block hash to an empty value.
- Set the block time to zero.
- Do not use a minter seed.

The M4 seed must contain all unclaimed Ethereum deposits at the checkpoint. It
must also contain all still-actionable Layer bridge aggregates and withdrawals
at the checkpoint.

The alert gate imports the seed state one time. It then backfills finalized
Ethereum `Deposit` logs after the checkpoint. It replays every later Tellor
Layer block.

Do not run `--up-live` before an authorized operator validates the checkpoint
and seed contents.

## Discord routes

Before live delivery, create `secrets/discord_webhooks.json`. Set the file to
mode 0600.

The route object must contain these exact eleven names:

- `tellormaster-control`
- `bridge-control`
- `databridge-integrity`
- `bridge-ledger-integrity`
- `tellorflex-value-integrity`
- `databank-value-integrity`
- `governance-dispute`
- `issuance-integrity`
- `tellorflex-eth-usd-freshness`
- `tellorflex-ampl-usd-deadline`
- `tellorflex-uspce-deadline`

Use `production/policy/discord_routes.example.json` as the route-name template.
Replace all placeholder URLs before live validation.

Keep `TELLOR_ALERT_DELIVERY_MODE=log-only` during all acceptance tests. Do not
claim that a configured route proves Discord delivery.

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

## Deployment entry point

Run all deployment commands from `migration/`.

### Configuration check

```sh
./deploy-production.sh --check
```

`--check` requires an existing `.env.production` file with mode 0600. It does
the following work:

1. It creates the required local runtime and secret directories.
2. It validates their ownership.
3. It validates the production Compose configuration.
4. It builds the `alert-gate` image.
5. It runs the `alert-gate` service configuration check.
6. It runs the Monitor service configuration check.

The command can build or retrieve container images. It does not start the
long-running services, but it is not a read-only command.

### Log-only start

```sh
./deploy-production.sh --up-log-only
```

`--up-log-only` repeats the configuration checks. It then validates the remote
inputs and starts the stack in `log-only` mode. It permits the Ethereum and
Tellor Layer replay processes to catch up.

`--up-log-only` refuses an environment that sets
`TELLOR_ALERT_DELIVERY_MODE=live`.

A successful log-only start does not prove live-delivery readiness.

### Live start

```sh
./deploy-production.sh --up-live
```

`--up-live` repeats the configuration, seed, and remote-input checks. It also
requires all of these conditions:

- `.env.production` explicitly sets
  `TELLOR_ALERT_DELIVERY_MODE=live`.
- `secrets/discord_webhooks.json` exists and has mode 0600.
- The route file contains the complete set of eleven routes.
- The imported seed digest is present.
- The Ethereum cursor is caught up.
- The Tellor Layer cursor is caught up.
- The unresolved queue is empty.
- No log-only incident remains open.
- The live Ethereum and Tellor Layer input checks pass.
- The configured route-file permissions pass.
- No unsettled delivery remains in the runtime state.
- The runtime state is complete and caught up.

Do not run `--up-live` before the deployment seed, two-RPC agreement,
approved-change, route, replay, and live-host validation gates are complete.
An authorized operator must approve activation.
