# AutoTasks

This repository is the final local implementation of Tellor's alert-only production monitoring: an OpenZeppelin Monitor deployment paired with a Python alert gate that turns raw chain and Layer events into reviewed, deduplicated Discord alerts across eleven monitors, M1 through M11.

This repository does not prove deployment, current host state, or Discord delivery, and it does not authorize activation.

## Architecture

The production runtime is two containers.

**OpenZeppelin Monitor v1.5.0** loads eight EVM sensors, one per `config/monitors/*.json` file, for M1-M8. It watches Ethereum mainnet, matches the configured events and function calls, and appends each raw `MonitorMatch` to a durable spool. It does not evaluate or deliver anything itself.

**The alert gate** (`service/tellor_alert_gate/`) consumes that spool and does the actual work: it validates final Ethereum receipts and state, replays Tellor Layer blocks, evaluates all eleven monitors, runs the M9-M11 absence checks on a schedule, groups incidents in SQLite, and performs final delivery. M9-M11 have no Monitor JSON at all, because an absent report cannot be detected by watching for events — the gate has to actively check on a schedule instead.

The default delivery mode is `log-only`. In either mode, the runtime sends no heartbeat, startup, success, recovery, RPC, parser, or service-health messages to Discord. A fault sends exactly one opening message per incident; an ambiguous live-delivery result (network error, HTTP 5xx, or a reservation abandoned mid-attempt) preserves the delivery reservation rather than risking a duplicate, and is not automatically retried unless the reservation is later found to have been abandoned by a crashed process — see [`docs/operations.md`](docs/operations.md#healthcheck-interpretation) and [`MONITORING_SOURCE_MAP.md`](MONITORING_SOURCE_MAP.md).

For host preparation, secrets, Discord routes, and the deployment flow, see [`docs/operations.md`](docs/operations.md). For per-monitor response procedures, see [`docs/runbook.md`](docs/runbook.md).

## Repository layout

| Path | Contents |
|---|---|
| `config/monitors/` | Eight OpenZeppelin Monitor v1.5.0 EVM prefilters for M1-M8 |
| `config/networks/`, `config/triggers/` | Monitor network definition and the `tellor_alert` trigger script that appends matches to the spool |
| `policy/` | Operator policy: the M1-M11 catalog, approved changes, DataBridge enrollment, bridge/minter seed schemas, Discord route names |
| `service/tellor_alert_gate/` | The alert gate: receipt/state validation, Layer replay, correlation, scheduling, incidents, and delivery |
| `service/Dockerfile`, `service/requirements.txt` | The alert-gate container build |
| `pyproject.toml` | Packaging for the `tellor_alert_gate` package; installs a `tellor-alert-gate` console script |
| `docker-compose.production.yaml` | The production stack definition |
| `deploy-production.sh` | The validation-first deployment entry point |
| `.github/workflows/ci.yml` | Runs the safe local checks below on every pull request and push to `main` |
| `docs/operations.md` | Operator procedures: host prep, secrets, Discord routes, deployment, healthchecks |
| `docs/runbook.md` | Per-monitor response runbook (M1-M11) |
| `docs/monitor-inventory.csv` | Monitor inventory: targets, functions/events, descriptions |
| `MONITORING_SOURCE_MAP.md` | Pinned versions and source/provenance evidence |
| `legacy/` | Retired Defender/Sentinel autotasks this system replaced (see [`legacy/README.md`](legacy/README.md)) |

## Safe local checks

Run these commands from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=service python3 -m unittest discover -s tests -p 'test_alert_gate_*.py'
docker compose --env-file environment.example -f docker-compose.production.yaml config --quiet
sh -n deploy-production.sh
```

These checks do not start the long-running services and do not require chain access. `.github/workflows/ci.yml` runs all three on every pull request and on every push to `main`, using Python 3.9 (the package's `requires-python` floor; production runs Python 3.12 inside the container — see `service/Dockerfile`).

The alert-gate package is also installable standalone, outside the container, which is useful for local editing and IDE support:

```sh
pip install -e .
```

This exposes a `tellor-alert-gate` console script with the same subcommands as `python -m tellor_alert_gate` (`check-config`, `check-inputs`, `check-live`, `healthcheck`, `once`, `run`) — see [`docs/operations.md`](docs/operations.md) for what each does.

For a deeper (still non-running) validation pass, use `./deploy-production.sh --check` — see [`docs/operations.md`](docs/operations.md#configuration-check) for what it does and what it requires.
