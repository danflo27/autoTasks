# Operations

This is the operator's reference for taking the local M1-M11 alert-only design in [`../README.md`](../README.md) to a running host: preparing the host, provisioning secrets and seeds, configuring Discord routes, running the deployment gate, and reading the healthchecks.

This document does not prove deployment, current host state, or Discord delivery, and it does not authorize activation.

## Repository layout

- `../config/monitors/` — eight OpenZeppelin Monitor v1.5.0 EVM prefilters for M1-M8.
- `../config/triggers/scripts/alert.py` — appends each raw match to the durable spool.
- `../policy/monitors.json` — the exact M1-M11 logical catalog.
- `../policy/approved_changes.json` — the reviewed M1/M2 control-change approvals.
- `../policy/enrolled_databridges.json` — the pinned M3 DataBridge enrollment.
- `../policy/bridge_ledger_seed.example.json` — the reviewed M4 bridge-ledger checkpoint and pending-event schema.
- `../policy/layer_minter_seed.example.json` — the Layer minter checkpoint schema.
- `../policy/discord_routes.example.json` — the exact eleven live-delivery route names.
- `../service/tellor_alert_gate/` — the receipt, state, Layer, correlation, schedule, incident, and delivery code.
- `../docker-compose.production.yaml` — the production stack definition.
- `../deploy-production.sh` — the validation-first deployment entry point.

## Host preparation

The intended deployment target is an EC2 `t3a.large` instance. This repository does not verify deployment capacity or current host state.

Both containers run as a fixed non-root user, `10001:10001` (the `alertgate` user baked into `service/Dockerfile`, and the default for `TELLOR_RUNTIME_UID`/`TELLOR_RUNTIME_GID` in `environment.example`). This was previously `1000:1000`. **If runtime or secrets directories already exist from before this change, they must be `chown`'d to `10001:10001` before starting the stack** — `deploy-production.sh` validates ownership and refuses to proceed otherwise.

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

- Two independent Ethereum providers (see [RPC provider secrets](#rpc-provider-secrets) below).
- The Tellor Layer endpoint.
- The required historical EVMCall providers.
- The Layer replay start height.

Set `.env.production` to mode 0600.

`deploy-production.sh` and the alert gate both read settings from `.env.production` by name — see [What reaches the alert gate](#what-reaches-the-alert-gate) below before assuming a new variable you add there is automatically visible to a container.

## RPC provider secrets

The alert gate's two Ethereum providers are read from mounted 0600 secret files, not from plain environment variables:

| Setting | File | Purpose |
|---|---|---|
| `RPC_ETHEREUM_MAINNET_FILE` | `secrets/rpc_ethereum_mainnet.txt` | Primary Ethereum provider |
| `RPC_ETHEREUM_MAINNET_SECONDARY_FILE` | `secrets/rpc_ethereum_mainnet_secondary.txt` | Independent secondary provider, required to agree with the primary for M9-M11 |

This follows the same mounted-secrets convention already used for `DISCORD_WEBHOOKS_FILE`, `BRIDGE_LEDGER_SEED_FILE`, and `LAYER_MINTER_SEED_FILE`. Create the files with:

```sh
install -m 0600 /dev/null secrets/rpc_ethereum_mainnet.txt
printf '%s' "https://<primary-provider-url>" > secrets/rpc_ethereum_mainnet.txt
install -m 0600 /dev/null secrets/rpc_ethereum_mainnet_secondary.txt
printf '%s' "https://<secondary-provider-url>" > secrets/rpc_ethereum_mainnet_secondary.txt
```

`deploy-production.sh` requires both files to exist with mode 0600 before `--up-log-only` or `--up-live` will start the stack. The alert gate also accepts the plain `RPC_ETHEREUM_MAINNET` / `RPC_ETHEREUM_MAINNET_SECONDARY` environment variables as an alternative to the `_FILE` settings, but the production Compose file wires only the file-based path for this container.

### Known residual exposure: the Monitor container's RPC URL

The `monitor` container still receives `RPC_ETHEREUM_MAINNET` as a **plain environment variable**, and that value is readable via `docker inspect` or `/proc/<pid>/environ` for that specific container. This is a known, accepted limitation, not an oversight: OpenZeppelin Monitor v1.5.0's own configuration schema (`config/networks/ethereum_mainnet.json`, and the `SecretValue` type in Monitor's source) can source `rpc_urls[].url` only from a literal value, a plain environment variable, or a HashiCorp Cloud Vault reference — there is no file-based secret type. Given the pinned Monitor image, a plain environment variable is the only way to hand it this URL.

The primary Ethereum URL is necessarily the same commercial endpoint as the one in `secrets/rpc_ethereum_mainnet.txt` (Monitor and the alert gate must observe the same chain); only the transport differs.

The alert gate's historical EVMCall archive providers — `RPC_OPTIMISM`, `RPC_GNOSIS`, `RPC_POLYGON`, `RPC_CHIADO`, and `RPC_SEPOLIA` — are equally sensitive commercial endpoints and also remain plain environment variables. Converting them to the mounted-secrets-file pattern was out of scope for the RPC-secrets change; they carry the same exposure `RPC_ETHEREUM_MAINNET` had before it.

## What reaches the alert gate

`docker-compose.production.yaml` does not give the `alert-gate` service a blanket `env_file:`. Instead it lists every setting the container actually needs, one by one, under its `environment:` key. **Adding a variable to `.env.production` does not by itself make it visible to the alert gate** — the container only sees a value if it is also named in `docker-compose.production.yaml`'s `alert-gate.environment` block (either as a literal or as `${VARIABLE_NAME}`). This is deliberate: it keeps values that exist only for the `monitor` service's benefit, such as the plain `RPC_ETHEREUM_MAINNET`, from also reaching the alert gate by accident.

If you add a new setting the alert gate should read, add it to both `.env.production` and the `alert-gate.environment` block.

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

See [`../README.md`](../README.md#safe-local-checks) for the test, Compose-config, and shell-syntax commands, and for installing the package locally with `pip install -e .`. None of these start the long-running services.

## Deployment entry point

Run all deployment commands from the repository root.

### Configuration check

```sh
./deploy-production.sh --check
```

`--check` requires an existing `.env.production` file with mode 0600. It creates the required runtime and secret directories, validates their ownership against `TELLOR_RUNTIME_UID`/`TELLOR_RUNTIME_GID` (10001 by default), validates the Compose configuration, builds `alert-gate`, and runs `alert-gate check-config` and `monitor --check`.

The command does not start the long-running services. However, it is not read-only: it creates directories and builds an image.

### Log-only start

```sh
./deploy-production.sh --up-log-only
```

`--up-log-only` repeats the configuration checks, additionally requires the RPC provider secret files and the M4 bridge-ledger seed (and the minter seed, unless the replay starts at height 1), runs `alert-gate check-inputs` against the live RPC and Layer endpoints, and starts the stack in `log-only` mode. It permits the Ethereum and Tellor Layer replay processes to catch up.

`--up-log-only` refuses to start an environment that sets `TELLOR_ALERT_DELIVERY_MODE=live`. This refusal parses the actual value of that setting out of `.env.production` (handling a trailing space or a CRLF line ending the same way the Python config loader does), not a whole-line text match — a stray CRLF or trailing whitespace in the file cannot defeat the check.

A successful `--up-log-only` run does not prove live-delivery readiness.

### Live start

```sh
./deploy-production.sh --up-live
```

`--up-live` requires explicit live mode. It also requires all of these conditions:

- `.env.production` explicitly sets `TELLOR_ALERT_DELIVERY_MODE=live`.
- `secrets/discord_webhooks.json` exists and has mode 0600.
- The route file contains the complete set of eleven routes.
- The imported seed digest is present in runtime state.
- The Ethereum deposit-replay cursor is caught up to the confirmed block.
- The Tellor Layer evaluation and minter cursors are caught up and mutually consistent.
- The unresolved match queue is empty.
- No log-only incident remains open.
- The live Ethereum and Tellor Layer input checks (`alert-gate check-live`) pass, including that both configured Ethereum providers report chain ID 1, historical contract code and event logs are readable, and every active DataBridge is enrolled in M3 policy.
- No delivery remains in the `sending` or `uncertain` state.

Do not run `--up-live` until the deployment seed, two-RPC agreement, approved-change, route, replay, and live-host validation gates are complete. An authorized operator must approve activation.

This documentation describes required checks and gates. It does not assert that an operator ran them.

## Healthcheck interpretation

Both containers in `docker-compose.production.yaml` define a Docker healthcheck. Each proves a real, specific unit of progress — not just that the process is running — and each has a specific blind spot.

**`monitor`** checks that `data/ethereum_mainnet_last_block.txt` was modified within the last 240 seconds. Monitor rewrites that file unconditionally on every scheduled cycle (`config/networks/ethereum_mainnet.json`'s `cron_schedule`, once a minute) after a successful `get_latest_block_number()` call — even when there are no new blocks. A stalled mtime means the block-watcher loop is wedged, typically against a dead RPC. It cannot catch the chain genuinely halting upstream, a hung metrics endpoint (metrics are disabled here), or a poll that succeeds while producing wrong results.

**`alert-gate`** runs a dedicated CLI subcommand:

```sh
docker compose -f docker-compose.production.yaml exec alert-gate python -m tellor_alert_gate healthcheck
```

or, with the package installed locally, `tellor-alert-gate healthcheck`. It reads `last_scheduled_minute` from the SQLite state file, which the gate writes only after a full M9-M11 scheduled evaluation *and* the M3 stale-DataBridge check both complete successfully against the configured Ethereum RPC providers in the same pass. It reports healthy if that value is no more than 300 seconds stale, and prints the age in seconds either way. This deliberately does not require every runtime secret to already be mounted and never touches the network beyond what the scheduled pass itself does, so it keeps telling the truth about the running process even during startup. A `None` age means no scheduled pass has completed yet — normal briefly after startup (the Docker `start_period` of 300s covers this), but a genuine failure if it persists.

A deadlock (for example, on the SQLite state file) or a poll loop wedged against a dead RPC stops this value from advancing even though the process keeps running, which is exactly the failure mode a bare "is the process alive" check would miss. It cannot catch a live RPC returning wrong data, a delivery-only failure (Discord unreachable while RPC reads keep succeeding), or corruption in state that this pass does not touch.

To check current container health status:

```sh
docker compose -f docker-compose.production.yaml ps
```

## Troubleshooting

- **`--check` or `--up-*` fails on directory ownership.** A runtime or secrets directory is owned by a uid:gid that does not match `TELLOR_RUNTIME_UID:TELLOR_RUNTIME_GID` (10001:10001 by default). This is expected if the directories predate the uid change from 1000 — `chown` them to the configured uid:gid.
- **`--up-log-only` or `--up-live` refuses to start, citing a missing RPC secret file.** Create `secrets/rpc_ethereum_mainnet.txt` and `secrets/rpc_ethereum_mainnet_secondary.txt` per [RPC provider secrets](#rpc-provider-secrets); both must exist at mode 0600.
- **A setting added to `.env.production` does not seem to reach the alert gate.** See [What reaches the alert gate](#what-reaches-the-alert-gate) — the variable also needs to be named in the `alert-gate.environment` block of `docker-compose.production.yaml`.
- **`alert-gate` healthcheck reports unhealthy.** Check the reported age against the container logs. An age just past 300 seconds shortly after a restart is expected during warmup; a persistently growing age indicates the scheduled pass is not completing — check RPC connectivity and the SQLite state file for lock contention.
- **A live-delivery reservation is stuck as `sending`.** If the alert-gate process crashed between reserving a delivery and contacting Discord, that reservation is automatically reclaimed and retried once it has been in the `sending` state for longer than `STALE_SENDING_RESERVATION_SECONDS` (600 seconds; see `service/tellor_alert_gate/delivery.py`). A reservation in the `uncertain` state (an ambiguous Discord response — a network error or an HTTP 5xx) is never automatically retried, to avoid a duplicate delivery; it requires operator review.

## Provenance

See [`../MONITORING_SOURCE_MAP.md`](../MONITORING_SOURCE_MAP.md) for pinned versions, source evidence, and the validation boundary.
