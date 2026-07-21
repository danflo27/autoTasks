# Tellor monitoring source and version map

This file pins the external behavior used by the local monitoring
implementation. It is evidence for implementation and review; it does not
authorize deployment or establish that the EC2 host is running these files.

## Runtime

| Component | Pinned version | Used for |
|---|---|---|
| OpenZeppelin Monitor | `v1.5.0`, image digest `sha256:8541bcfa869577aa6e44ea85f52700f5bb66c17a75b01f2eb2be66603d172635` | Monitor, match, script-trigger, and replay runtime |
| Monitor trigger Python | Monitor image Python 3.12, standard library only | In-container alert routing and report classification |
| Host service Python | `/usr/bin/python3`, Python 3.9+ required; exact target version must be captured during deployment preflight | Host freshness and watchdog services |

The image pin is declared in [`docker-compose.yaml`](docker-compose.yaml).
Changing the tag or digest requires the full local test suite, Monitor image
configuration validation, log-only replay, and staged runtime readback.

## Tellor protocol and reporter references

| Source | Pin | Behavior relied on |
|---|---|---|
| [TellorFlex](https://github.com/tellor-io/tellorFlex/tree/e2946ecc12b22e72e63bec6f298d31ff22967d5c) | `e2946ecc12b22e72e63bec6f298d31ff22967d5c` | Exact `NewReport(bytes32,uint256,bytes,uint256,bytes,address)` event; `getDataBefore` is strictly before its timestamp argument and searches backward past disputed values |
| [Tellor data specifications](https://github.com/tellor-io/dataSpecs/tree/8c7225defb04bf5aaf18080a33bfaa3feba23765) | `8c7225defb04bf5aaf18080a33bfaa3feba23765` | Canonical SpotPrice, EVMCall, TellorRNG, address-report, Ampleforth custom spot price, and Ampleforth USPCE encodings |
| [Telliot feeds v0.4.20](https://github.com/tellor-io/telliot-feeds/tree/fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2) | tag `v0.4.20`, commit `fb0cfbc6436d1f0d5003c8aeea23c2369c87b1c2` | EVMCall reporter behavior, including rejection of empty calldata and the canonical 32-byte zero result for non-empty 1–3-byte calldata, a no-code target, and a missing selector after a failed call |

The monitor deliberately tightens the pinned Telliot behavior in one place:
historical validation checks bytecode at the same historical block as the
call. An empty call result from an address that has code, a failed call whose
selector is present, a missing archive block, a non-unique block timestamp, or
a changing block hash is classified as `NOT VERIFIED`; it is never promoted to
a potential dispute from ambiguous evidence.

## Local policy source

[`TELLOR_MONITORING_PROGRESS.md`](TELLOR_MONITORING_PROGRESS.md) is the policy
source for the 15 route names, four report outcomes, 20% SpotPrice threshold,
six EVMCall chains, freshness deadlines, confirmation depth, state paths,
deployment gates, and rollback requirements. These are operator choices, not
claims made by the upstream projects.

## Review and update rule

Recheck this map whenever an upstream pin, contract address, query ID, Monitor
image, or target RPC policy changes. Record the new pin and the tests that
changed before deployment. Never replace a pin with a floating `main`, `latest`,
or unpinned image reference.

## Validation boundary

Local fixtures and tests can prove parsing, classification, routing, calendar
boundaries, retry behavior, and explicit log-only delivery. They cannot prove
archive retention, current chain state, EC2 installation, systemd activation,
or Discord destination delivery. Those require the separately authorized
deployment and runtime evidence in the progress ledger.
