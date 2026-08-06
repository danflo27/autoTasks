# Tellor production alert-only monitors

This directory contains the final local M1-M11 alert-only design dated 2026-08-06.

Use this production stack: `../docker-compose.production.yaml`.

This local implementation does not prove deployment, current host state, or Discord delivery. It does not authorize activation.

## Runtime design

The production runtime has two containers.

1. OpenZeppelin Monitor v1.5.0 loads the eight EVM sensor files in `config/monitors/`. The `tellor_alert` trigger script only appends each raw `MonitorMatch` record to a durable spool.
2. `alert-gate` consumes the spool. It validates final Ethereum receipts and state, replays Tellor Layer blocks, evaluates M1-M11, schedules the M9-M11 absence checks, groups incidents in SQLite, and performs final delivery.

M9-M11 do not have OpenZeppelin Monitor JSON files. Events cannot prove an absence.

The default external-delivery mode is `log-only`. The runtime does not send heartbeat, startup, success, recovery, RPC, parser, or service-health messages.

A fault can send one opening message. The alert gate stores later evidence in SQLite. If a live delivery result is ambiguous, the alert gate keeps the delivery reservation. It does not retry.

## Repository layout

- `config/monitors/` contains eight OpenZeppelin Monitor v1.5.0 EVM prefilters for M1-M8.
- `config/triggers/scripts/alert.py` appends each raw match to the durable spool.
- `policy/monitors.json` contains the exact M1-M11 logical catalog.
- `policy/approved_changes.json` contains the reviewed M1/M2 control-change approvals.
- `policy/enrolled_databridges.json` contains the pinned M3 DataBridge enrollment.
- `policy/bridge_ledger_seed.example.json` defines the reviewed M4 bridge-ledger checkpoint and pending-event schema.
- `policy/layer_minter_seed.example.json` defines the Layer minter checkpoint schema.
- `policy/discord_routes.example.json` contains the exact eleven live-delivery route names.
- `../alert_gate/` contains the receipt, state, Layer, correlation, schedule, incident, and delivery code.
- `../docker-compose.production.yaml` defines the production stack.
- `../deploy-production.sh` is the validation-first entry point.

## Host preparation

The intended deployment target is an EC2 `t3a.large` instance. This repository does not verify deployment capacity or current host state.

Run these commands from `migration/`:

```sh
cp production/environment.example .env.production
chmod 600 .env.production
mkdir -p secrets/release-manifests
chmod 700 secrets secrets/release-manifests
cp production/policy/bridge_ledger_seed.example.json secrets/bridge_ledger_seed.json
cp production/policy/layer_minter_seed.example.json secrets/layer_minter_seed.json
chmod 600 secrets/bridge_ledger_seed.json secrets/layer_minter_seed.json
```

The example seeds contain placeholders. They do not pass validation.

Configure these inputs in `.env.production`:

- Two independent Ethereum providers.
- The Tellor Layer endpoint.
- The required historical EVMCall providers.
- The Layer replay start height.

Set `.env.production` to mode 0600.

## Seed and checkpoint requirements

Create `secrets/bridge_ledger_seed.json` from an independently reviewed M4 checkpoint.

If `LAYER_REPLAY_START_HEIGHT` is greater than `1`, create `secrets/layer_minter_seed.json` from the same independently reviewed checkpoint.

Set each populated seed file to mode 0600.

Both seed files must contain the same Tellor Layer checkpoint identity and time. Start the replay at the next Layer height.

For a full replay from height `1`:

- Set the bridge seed Layer height to `0`.
- Set the Layer block hash to an empty value.
- Set the block time to zero.
- Do not use a minter seed.

The M4 seed must contain all unclaimed Ethereum deposits at the checkpoint. It must also contain all still-actionable Tellor Layer bridge aggregates and withdrawals at the checkpoint.

The alert gate imports the seed one time. It then backfills finalized Ethereum `Deposit` logs after the checkpoint and replays all later Tellor Layer blocks.

Do not run `--up-live` before an authorized operator validates the checkpoint and seed contents.

## Discord routes

Before live delivery, create `secrets/discord_webhooks.json`. Set the file to mode 0600.

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

Use `production/policy/discord_routes.example.json` as the route template.

Keep `TELLOR_ALERT_DELIVERY_MODE=log-only` during acceptance tests. Configured routes do not prove Discord delivery.

## Safe local checks

Run these commands from `migration/`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=alert_gate python3 -m unittest discover -s tests -p 'test_alert_gate_*.py'
TELLOR_ENV_FILE=production/environment.example docker compose --env-file production/environment.example -f docker-compose.production.yaml config --quiet
sh -n deploy-production.sh
```

These checks do not start the long-running services.

## Deployment entry point

Run all deployment commands from `migration/`.

### Configuration check

```sh
./deploy-production.sh --check
```

`--check` requires an existing `.env.production` file with mode 0600. It does the following work:

1. It creates the required runtime and secret directories.
2. It validates directory ownership and the Compose configuration.
3. It builds `alert-gate`.
4. It checks the `alert-gate` configuration.
5. It checks the Monitor configuration.

The command does not start the long-running services. However, it is not read-only.

### Log-only start

```sh
./deploy-production.sh --up-log-only
```

`--up-log-only` repeats the configuration checks. It validates the remote inputs and starts the stack in `log-only` mode. It permits the Ethereum and Tellor Layer replay processes to catch up.

`--up-log-only` refuses an environment that sets `TELLOR_ALERT_DELIVERY_MODE=live`.

A successful `--up-log-only` run does not prove live-delivery readiness.

### Live start

```sh
./deploy-production.sh --up-live
```

`--up-live` requires explicit live mode. It also requires all of these conditions:

- `.env.production` explicitly sets `TELLOR_ALERT_DELIVERY_MODE=live`.
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

Do not run `--up-live` until the deployment seed, two-RPC agreement, approved-change, route, replay, and live-host validation gates are complete. An authorized operator must approve activation.

This documentation describes required checks and gates. It does not assert that an operator ran them.

## Major provenance

- **Source map:** `policy/monitors.json`, the other files in `policy/`, `../alert_gate/`, `../docker-compose.production.yaml`, and `../deploy-production.sh` define the active implementation.
- **Changed claims:** The old Defender and candidate materials are not part of the active documentation or production guidance.
- **Assumptions:** The intended deployment target is an EC2 `t3a.large` instance. The current host and its capacity are not verified.
- **Validation:** The active evidence consists of the alert-gate tests, Compose configuration validation, and deployment-script syntax validation.
- **Residual risk:** Validation with real endpoints, reviewed seeds, configured routes, and a completed replay is pending.
