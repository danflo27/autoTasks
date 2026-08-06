# Tellor production monitoring source and version map

This file records the external behavior and pinned runtime versions for the final local stack in `docker-compose.production.yaml`.

This source map is implementation and review evidence. It does not authorize deployment. It does not prove current EC2 host state or Discord delivery.

## Production runtime pins

| Component | Pinned version | Use |
|---|---|---|
| OpenZeppelin Monitor | `v1.5.0`, image digest `sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635` | Loads the eight M1-M8 EVM sensors and appends raw `MonitorMatch` records to the durable spool |
| Alert gate image | Built from `service/Dockerfile` | Validates final Ethereum receipts and state, replays Tellor Layer blocks, evaluates M1-M11, schedules the M9-M11 absence checks, groups incidents in SQLite, and performs final delivery |
| Alert gate Python base | `python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49` | Runs the alert gate in production; `pyproject.toml` sets `requires-python = ">=3.9"` for local development and CI, which use Python 3.9 |

The OpenZeppelin Monitor image pin is in [`docker-compose.production.yaml`](docker-compose.production.yaml).

The alert gate image builds from [`service/Dockerfile`](service/Dockerfile). The Dockerfile pins `python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49` and creates the fixed non-root `alertgate` user at uid:gid `10001:10001`, which both containers run as by default (see `docker-compose.production.yaml` and [`docs/operations.md`](docs/operations.md#host-preparation)).

If an operator changes a tag or digest, run the full local test suite. Validate the Monitor image configuration. Complete a log-only replay. Then perform staged runtime readback before deployment.

## Tellor protocol and reporter references

| Source | Pin | Behavior used by the implementation |
|---|---|---|
| [TellorFlex](https://github.com/tellor-io/tellorFlex/tree/e2946ecc12b22e72e63bec6f298d31ff22967d5c) | `e2946ecc12b22e72e63bec6f298d31ff22967d5c` | Exact `NewReport(bytes32,uint256,bytes,uint256,bytes,address)` event; `getDataBefore` is strictly before its timestamp argument and searches backward past disputed values |
| [Tellor data specifications](https://github.com/tellor-io/dataSpecs/tree/8c7225defb04bf5aaf18080a33bfaa3feba23765) | `8c7225defb04bf5aaf18080a33bfaa3feba23765` | Canonical SpotPrice, EVMCall, TellorRNG, address-report, Ampleforth custom spot price, and Ampleforth USPCE encodings |
| [Telliot feeds v0.4.20](https://github.com/tellor-io/telliot-feeds/tree/fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2) | tag `v0.4.20`, commit `fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2` | EVMCall reporter behavior, including rejection of empty calldata and the canonical 32-byte zero result for non-empty 1–3-byte calldata, a no-code target, and a missing selector after a failed call |

The production M5 EVMCall check requires all of these conditions:

- The ABI input is canonical.
- A source-chain client is configured.
- One exact source block exists at the reported timestamp.
- Code exists at that block.
- The replay bytes are not empty.
- A second read confirms that the block and code are stable.

If the check cannot prove these conditions, the match stays unresolved.

The check creates a finding only when the exact finalized replay bytes differ from the reported bytes.

The production check intentionally rejects the Telliot zero-result fallback. A target without code or an empty replay is unresolved. It is not a matching response.

## Local production policy

The following files define the local M1-M11 production policy:

- [`policy/monitors.json`](policy/monitors.json) defines the exact eleven monitor IDs, route slugs, severities, and kinds.
- [`policy/approved_changes.json`](policy/approved_changes.json) contains the reviewed M1/M2 control-change approvals.
- [`policy/enrolled_databridges.json`](policy/enrolled_databridges.json) contains the pinned M3 DataBridge enrollment.
- [`policy/bridge_ledger_seed.example.json`](policy/bridge_ledger_seed.example.json) defines the M4 bridge-ledger checkpoint schema.
- [`policy/layer_minter_seed.example.json`](policy/layer_minter_seed.example.json) defines the Layer minter checkpoint schema.
- [`policy/discord_routes.example.json`](policy/discord_routes.example.json) defines the exact eleven live-delivery route names.

These files contain operator policy. The upstream projects do not make these operator-policy claims.

Use `policy/monitors.json` as the local source of truth for the exact M1-M11 catalog.

## Review and update rule

Recheck this source map if one of these items changes:

- An upstream pin.
- A contract address.
- A query ID.
- A Monitor image.
- The alert gate base image or runtime uid/gid.
- A target RPC policy, including which providers are file-backed secrets versus plain environment variables.

Before deployment, record the new pin and the changed tests.

Do not replace a pin with a floating `main`, `latest`, or unpinned image reference.

## Validation boundary

Local fixtures and tests can prove these properties:

- Parsing.
- Classification.
- Routing.
- Calendar boundaries.
- Retry behavior, including delivery-reservation reclaim and the deploy gate's env-value parsing.
- Explicit `log-only` delivery.

The suite that proves these properties is `tests/`: 61 tests as of this writing, covering M1-M11 evaluation logic (including M2, M3, M6, and the Tellor Layer side of M7), the deploy gate, and the healthcheck subcommand. `.github/workflows/ci.yml` runs this suite, a `docker compose config` validation of `docker-compose.production.yaml`, and a shell syntax check of `deploy-production.sh` on every pull request and on every push to `main` — the same three commands documented in [`README.md`](README.md#safe-local-checks).

Local fixtures and tests cannot prove these properties:

- Archive retention.
- Current chain state.
- EC2 installation.
- Service activation.
- Current runtime state.
- Discord destination delivery.

These properties require separate authorization and current deployment or runtime evidence.

## Major provenance

- **Source map:** `policy/monitors.json`, the other policy files, `service/`, `pyproject.toml`, `docker-compose.production.yaml`, `deploy-production.sh`, and `.github/workflows/ci.yml` define the active implementation and its operating checks.
- **Changed claims:** The old Defender and candidate documentation is not part of this active source map.
- **Assumptions:** The map describes the intended local production design. It does not assume a current EC2 deployment or working Discord delivery.
- **Validation:** The active evidence consists of local tests, Compose configuration validation, and deployment-script validation, all reproduced by CI on every pull request and push to `main`.
- **Residual risks:** Validation with real endpoints, reviewed seeds, configured routes, and a completed replay is pending. Separately, the `monitor` container's `RPC_ETHEREUM_MAINNET` and the alert gate's EVMCall archive provider URLs remain plain environment variables rather than mounted secret files — see [`docs/operations.md`](docs/operations.md#known-residual-exposure-the-monitor-containers-rpc-url) for why and what is and is not covered.
