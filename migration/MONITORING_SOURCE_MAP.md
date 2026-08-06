# Tellor production monitoring source and version map

This file records the external behavior and pinned runtime versions used by the
final local production stack in `docker-compose.production.yaml`.

This source map is implementation and review evidence. It does not authorize
deployment. It does not prove current EC2 host state. It does not prove Discord
delivery.

## Production runtime pins

| Component | Pinned version | Use |
|---|---|---|
| OpenZeppelin Monitor | `v1.5.0`, image digest `sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635` | Loads the eight M1-M8 EVM sensors and appends raw `MonitorMatch` records to the durable spool |
| Alert gate image | Built from `alert_gate/Dockerfile` | Validates final Ethereum receipts and state, replays Tellor Layer blocks, evaluates M1-M11, groups incidents in SQLite, and performs final delivery |
| Alert gate Python base | `python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49` | Runs the alert gate |

The OpenZeppelin Monitor image pin is declared in
[`docker-compose.production.yaml`](docker-compose.production.yaml).

The alert gate image builds from
[`alert_gate/Dockerfile`](alert_gate/Dockerfile). That Dockerfile pins
`python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49`.

If an operator changes a tag or digest, run the full local test suite, validate
the Monitor image configuration, complete a log-only replay, and perform staged
runtime readback before deployment.

## Tellor protocol and reporter references

| Source | Pin | Behavior used by the implementation |
|---|---|---|
| [TellorFlex](https://github.com/tellor-io/tellorFlex/tree/e2946ecc12b22e72e63bec6f298d31ff22967d5c) | `e2946ecc12b22e72e63bec6f298d31ff22967d5c` | Exact `NewReport(bytes32,uint256,bytes,uint256,bytes,address)` event; `getDataBefore` is strictly before its timestamp argument and searches backward past disputed values |
| [Tellor data specifications](https://github.com/tellor-io/dataSpecs/tree/8c7225defb04bf5aaf18080a33bfaa3feba23765) | `8c7225defb04bf5aaf18080a33bfaa3feba23765` | Canonical SpotPrice, EVMCall, TellorRNG, address-report, Ampleforth custom spot price, and Ampleforth USPCE encodings |
| [Telliot feeds v0.4.20](https://github.com/tellor-io/telliot-feeds/tree/fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2) | tag `v0.4.20`, commit `fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2` | EVMCall reporter behavior, including rejection of empty calldata and the canonical 32-byte zero result for non-empty 1–3-byte calldata, a no-code target, and a missing selector after a failed call |

The production M5 EVMCall check requires canonical ABI input, a configured
source-chain client, one exact source block at the reported timestamp, code at
that block, nonempty replay bytes, and a stable second read of the block and
code.

If the check cannot prove those conditions, it leaves the match unresolved. It
creates a finding only when the exact finalized replay bytes differ from the
reported bytes.

The production check intentionally rejects the Telliot zero-result fallback. A
target without code or an empty replay is unresolved. It is not a matching
response.

## Local production policy

The following production files define the local M1-M11 policy:

- [`production/policy/monitors.json`](production/policy/monitors.json) defines
  the exact eleven monitor IDs, route slugs, severities, and kinds.
- [`production/policy/approved_changes.json`](production/policy/approved_changes.json)
  contains reviewed M1/M2 control-change approvals.
- [`production/policy/enrolled_databridges.json`](production/policy/enrolled_databridges.json)
  contains the pinned M3 DataBridge enrollment.
- [`production/policy/bridge_ledger_seed.example.json`](production/policy/bridge_ledger_seed.example.json)
  defines the M4 bridge-ledger checkpoint schema.
- [`production/policy/layer_minter_seed.example.json`](production/policy/layer_minter_seed.example.json)
  defines the Layer minter checkpoint schema.
- [`production/policy/discord_routes.example.json`](production/policy/discord_routes.example.json)
  defines the exact eleven live-delivery route names.

These files contain operator policy. The upstream projects do not make these
operator-policy claims.

`TELLOR_MONITORING_PROGRESS.md` records the older candidate policy, including
its 15 route names, four report outcomes, 20% SpotPrice threshold, six EVMCall
chains, freshness deadlines, confirmation depth, state paths, deployment
gates, and rollback requirements. The older `config/` files and
`docker-compose.yaml` use that candidate design. The production stack does not
load those files. Do not use the candidate policy as the authoritative
production runtime definition.

## Review and update rule

Recheck this map if any of these items changes:

- An upstream pin.
- A contract address.
- A query ID.
- A Monitor image.
- The alert gate base image.
- A target RPC policy.

Record the new pin and the changed tests before deployment.

Do not replace a pin with a floating `main`, `latest`, or unpinned image
reference.

## Validation boundary

Local fixtures and tests can prove these properties:

- Parsing.
- Classification.
- Routing.
- Calendar boundaries.
- Retry behavior.
- Explicit `log-only` delivery.

Local fixtures and tests cannot prove these properties:

- Archive retention.
- Current chain state.
- EC2 installation.
- Service activation.
- Current runtime state.
- Discord destination delivery.

Those properties require separate authorization and current deployment or
runtime evidence.
