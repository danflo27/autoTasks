# AutoTasks

Legacy OpenZeppelin Defender autotasks and a self-hosted OpenZeppelin Monitor
replacement. The Ethereum mainnet production monitor set was activated on
2026-07-13. Eleven production monitors are configured for Ethereum mainnet
only; the USDC smoke monitor is retained but paused. OpenZeppelin labels
Monitor alpha software.

## Quickstart: Ethereum function to Discord

Prerequisites are Python 3.9+, Docker Compose v2, and a running Docker daemon.
Launch the interactive configurator:

```sh
cd migration
python3 quickstart.py configure
```

It securely prompts for an Ethereum mainnet RPC URL and Discord webhook, then
accepts a contract address, function name or canonical signature, and message
template. It validates the inputs and generates the Monitor configuration, then
offers to send a confirmed test message, validate the content-pinned image, and
start the service. The final startup readback must say
`Monitor container is running with the generated configuration.`

See [`migration/MIGRATION.md`](migration/MIGRATION.md#five-minute-custom-function-quickstart)
for placeholders, replay testing, limitations, and production migration notes.
