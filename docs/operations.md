# Tellor production alert-only monitors

This document describes the final local M1-M11 alert-only production design, dated 2026-08-06.

Use this production stack: [`../docker-compose.production.yaml`](../docker-compose.production.yaml).

This local implementation does not prove deployment, current host state, or Discord delivery. It does not authorize activation.

## Runtime design

See [`../README.md`](../README.md#runtime-design) for the two-container architecture, the delivery-mode guarantees, and why M9-M11 have no OpenZeppelin Monitor JSON files. This document covers the operator procedures for taking that design to a running host.

## Repository layout

- `../config/monitors/` contains eight OpenZeppelin Monitor v1.5.0 EVM prefilters for M1-M8.
- `../config/triggers/scripts/alert.py` appends each raw match to the durable spool.
- `../policy/monitors.json` contains the exact M1-M11 logical catalog.
- `../policy/approved_changes.json` contains the reviewed M1/M2 control-change approvals.
- `../policy/enrolled_databridges.json` contains the pinned M3 DataBridge enrollment.
- `../policy/bridge_ledger_seed.example.json` defines the reviewed M4 bridge-ledger checkpoint and pending-event schema.
- `../policy/layer_minter_seed.example.json` defines the Layer minter checkpoint schema.
- `../policy/discord_routes.example.json` contains the exact eleven live-delivery route names.
- `../service/tellor_alert_gate/` contains the receipt, state, Layer, correlation, schedule, incident, and delivery code.
- `../docker-compose.production.yaml` defines the production stack.
- `../deploy-production.sh` is the validation-first entry point.

## Host preparation

The intended deployment target is an EC2 `t3a.large` instance. This repository does not verify deployment capacity or current host state.

Run these commands from the repository root:

```sh
cp environment.example .env.production
chmod 600 .env.production
mkdir -p secrets/release-manifests
chmod 700 secrets secrets/release-manifests
cp policy/bridge_ledger_seed.example.json secrets/bridge_ledger_seed.json
cp policy/layer_minter_seed.example.json secrets/layer_minter_seed.json
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

The alert gate imports the seed one time, then backfills finalized Ethereum `Deposit` logs after the checkpoint and replays all later Tellor Layer blocks.

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

Use `policy/discord_routes.example.json` as the route template.

Keep `TELLOR_ALERT_DELIVERY_MODE=log-only` during acceptance tests. Configured routes do not prove Discord delivery.

## Safe local checks

See [`../README.md`](../README.md#safe-local-checks) for the test, Compose-config, and shell-syntax commands. They do not start the long-running services.

## Deployment entry point

Run all deployment commands from the repository root.

### Configuration check

```sh
./deploy-production.sh --check
```

`--check` requires an existing `.env.production` file with mode 0600. It creates the required runtime and secret directories, validates directory ownership and the Compose configuration, builds `alert-gate`, and checks both the `alert-gate` and the Monitor configuration.

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

## Provenance

See [`../MONITORING_SOURCE_MAP.md`](../MONITORING_SOURCE_MAP.md) for pinned versions, source evidence, and the validation boundary.
