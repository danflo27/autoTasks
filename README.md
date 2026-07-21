# AutoTasks

Legacy OpenZeppelin Defender autotasks plus a locally implemented,
self-hosted OpenZeppelin Monitor replacement for Tellor.

## Current status

The repository contains nine enabled Ethereum-mainnet monitor configurations
and one paused USDC smoke monitor. It also contains a confirmed-report
freshness checker, a desired-state-aware Monitor watchdog, secure per-producer
Discord routing, tests, and systemd installation artifacts.

This is a **local implementation boundary**, not deployment evidence. The
latest dated EC2 handoff says the Monitor was disabled and inactive. Nothing in
this repository authorizes activation, proves current host state, or proves
delivery to any Discord destination. See
[`migration/TELLOR_MONITORING_PROGRESS.md`](migration/TELLOR_MONITORING_PROGRESS.md)
for the evidence ledger and deployment gates.

## Start here

- [`migration/MIGRATION.md`](migration/MIGRATION.md) describes the monitor
  catalog, four report outcomes, routes, freshness policy, failure modes, and
  safe local verification.
- [`migration/AWS_DOCKER_OPERATIONS.md`](migration/AWS_DOCKER_OPERATIONS.md)
  is the beginner-oriented, authorization-gated AWS/Docker deployment and
  rollback runbook.
- [`migration/MONITORING_SOURCE_MAP.md`](migration/MONITORING_SOURCE_MAP.md)
  pins the Monitor, TellorFlex, data-specification, and Telliot sources used by
  the implementation.

## Safe local verification

These commands do not activate the service or intentionally contact Discord:

```sh
cd migration
python3 -m unittest tests/test_discord_routes.py
python3 -m unittest tests/test_delivery_integration.py
python3 -m unittest tests/test_dispute.py
python3 -m unittest tests/test_report_freshness.py
python3 -m unittest tests/test_monitoring_ops_hardening.py
python3 -m unittest tests/test_quickstart.py
python3 tests/run_tests.py
docker compose config --quiet
```

With a running Docker daemon, `python3 quickstart.py check` also validates the
configuration with the content-pinned Monitor image. It does not start the
long-running service.

## Custom Ethereum function quickstart

`migration/quickstart.py configure` can generate one gitignored custom
function monitor. Its webhook remains isolated on
`CUSTOM_DISCORD_WEBHOOK_URL`; production alerts use the per-producer route
file. Plain replay explicitly selects `log-only`. Starting the Compose service
also loads the production configurations, so do not choose `--start` until the
route preflight passes and an operator has authorized activation.

```sh
cd migration
python3 quickstart.py configure
python3 quickstart.py replay <confirmed-block-number>
```

Use [`migration/MIGRATION.md`](migration/MIGRATION.md#custom-function-quickstart)
for supported signatures, placeholders, replay behavior, and limitations.
