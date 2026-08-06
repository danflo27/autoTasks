# Legacy autotasks

This directory holds the retired OpenZeppelin Defender/Sentinel Autotask JavaScript code that the current M1-M11 alert gate replaced. It is kept for historical reference only and is **not deployable as-is** — it targeted the Defender/Sentinel runtime, not the OpenZeppelin Monitor plus alert-gate stack described in the top-level [`README.md`](../README.md) and [`docs/operations.md`](../docs/operations.md).

## Mapping to the current monitors

| Legacy directory | Superseded by |
|---|---|
| `bridges/` | M4 bridge-ledger-integrity |
| `disputes/` | M7 governance-dispute |
| `datafeed/` | M5 tellorflex-value-integrity |
| `dvm/` | M5 tellorflex-value-integrity, plus the M9-M11 scheduled freshness checks |
| `priceMonitor/` | M5 tellorflex-value-integrity |
| `tokenBridge/` (including `DepositToLayer`, `WithdrawFromLayer`) | M4 bridge-ledger-integrity; the staking-rewards path also relates to M8 issuance-integrity |
| `EVMCall/` | No current equivalent |
| `addressUpdates/` | No direct equivalent; closest in spirit to M1 tellormaster-control |
| `staking/` | No current equivalent |
| `tips/` | No current equivalent |

Four directories — `EVMCall/`, `addressUpdates/`, `staking/`, and `tips/` — have no current counterpart in the M1-M11 catalog. Their checks were dropped deliberately during the redesign, not lost silently. In particular, `EVMCall/`'s live-recompute query-type check was intentionally not carried forward into the new implementation.

For what each active monitor actually watches for and how it fires today, see [`../docs/runbook.md`](../docs/runbook.md).
