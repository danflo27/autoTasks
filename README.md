# AutoTasks

This repository contains the final local Tellor M1-M11 alert-only implementation: an OpenZeppelin Monitor deployment paired with a Python alert gate that turns raw chain events into reviewed, deduplicated Discord alerts.

Active sources: `config/` and `policy/` for configuration and operator policy, `service/` for the durable gate, `docker-compose.production.yaml` for the stack, `deploy-production.sh` for deployment procedures, and `MONITORING_SOURCE_MAP.md` for source and version evidence.

## Runtime design

The production runtime has two containers. OpenZeppelin Monitor v1.5.0 loads eight EVM sensors for M1-M8 and only appends raw `MonitorMatch` records to a durable spool; it does not evaluate or deliver anything itself. The alert gate consumes that spool and does the real work: it validates final Ethereum receipts and state, replays Tellor Layer blocks, evaluates all eleven monitors (M1-M11), runs the M9-M11 absence checks on a schedule, groups incidents in SQLite, and performs final delivery. M9-M11 have no Monitor JSON because events cannot prove an absence.

The default delivery mode is `log-only`. The runtime sends no heartbeat, startup, success, recovery, RPC, parser, or service-health messages — a fault sends one opening message, and an ambiguous live-delivery result keeps the delivery reservation rather than retrying it.

This repository does not prove deployment, current host state, or Discord delivery, and it does not authorize activation. For host preparation, seeds, Discord routes, and the deployment flow, see [`docs/operations.md`](docs/operations.md). For per-monitor response procedures, see [`docs/runbook.md`](docs/runbook.md).

## Repository layout

| Path | Contents |
|---|---|
| `config/monitors/` | Eight OpenZeppelin Monitor v1.5.0 EVM prefilters for M1-M8 |
| `config/networks/`, `config/triggers/` | Monitor network definition and the `tellor_alert` trigger script that appends matches to the spool |
| `policy/` | Operator policy: the M1-M11 catalog, approved changes, DataBridge enrollment, bridge/minter seed schemas, Discord route names |
| `service/tellor_alert_gate/` | The alert gate: receipt/state validation, Layer replay, correlation, scheduling, incidents, and delivery |
| `docker-compose.production.yaml` | The production stack definition |
| `deploy-production.sh` | The validation-first deployment entry point |
| `docs/operations.md` | Operator procedures: host prep, seeds, Discord routes, deployment |
| `docs/runbook.md` | Per-monitor response runbook (M1-M11) |
| `docs/monitor-inventory.csv` | Monitor inventory: targets, functions/events, descriptions |
| `MONITORING_SOURCE_MAP.md` | Pinned versions and source/provenance evidence |
| `legacy/` | Retired Defender/Sentinel autotasks this system replaced (see [`legacy/README.md`](legacy/README.md)) |

## Safe local checks

Run these commands from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=service python3 -m unittest discover -s tests -p 'test_alert_gate_*.py'
TELLOR_ENV_FILE=environment.example docker compose --env-file environment.example -f docker-compose.production.yaml config --quiet
sh -n deploy-production.sh
```

These checks do not start the long-running services and do not require chain access.

For a deeper (still non-running) validation pass, use `./deploy-production.sh --check` — see [`docs/operations.md`](docs/operations.md#configuration-check) for what it does and what it requires.
