# AutoTasks

This repository contains the final local Tellor M1-M11 alert-only implementation.

Use only these active sources: `migration/production/` for policy and configuration, `migration/alert_gate/` for the durable gate, `migration/docker-compose.production.yaml` for the stack, `migration/deploy-production.sh` for procedures, and `migration/MONITORING_SOURCE_MAP.md` for source and version evidence.

## Runtime design

OpenZeppelin Monitor v1.5.0 loads eight EVM sensors for M1-M8. The Monitor only appends raw `MonitorMatch` records to a durable spool.

The alert gate does the following work:

- It validates final Ethereum receipts and state.
- It replays Tellor Layer blocks.
- It evaluates M1-M11.
- It schedules the M9-M11 absence checks.
- It groups incidents in SQLite.
- It performs final delivery.

M9-M11 do not have Monitor JSON files. Events cannot prove an absence.

The default delivery mode is `log-only`. The runtime does not send heartbeat, startup, success, recovery, RPC, parser, or service-health messages.

A fault can send one opening message. If a live delivery result is ambiguous, the alert gate keeps the delivery reservation. It does not retry.

This repository does not prove deployment, current host state, or Discord delivery. It does not authorize activation.

## Safe local checks

Run these commands from `migration/`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=alert_gate python3 -m unittest discover -s tests -p 'test_alert_gate_*.py'
TELLOR_ENV_FILE=production/environment.example docker compose --env-file production/environment.example -f docker-compose.production.yaml config --quiet
sh -n deploy-production.sh
```

These checks do not start the long-running services.

For additional checks, run:

```sh
./deploy-production.sh --check
```

`--check` requires an existing `.env.production` file with mode 0600. The command does the following work:

1. It creates the required runtime and secret directories.
2. It validates directory ownership and the Compose configuration.
3. It builds `alert-gate`.
4. It checks the `alert-gate` configuration.
5. It checks the Monitor configuration.

The command does not start the long-running services. However, it is not read-only.
