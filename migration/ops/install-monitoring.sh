#!/usr/bin/env bash
# Install only the Tellor freshness/watchdog units. This is intentionally not a
# replacement for, or a rerun of, the legacy tellor-ops bootstrap script.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "install-monitoring.sh must run as root" >&2
  exit 1
fi

case "${1:-}" in
  ""|--enable) ;;
  *)
    echo "usage: $0 [--enable]" >&2
    exit 2
    ;;
esac

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MIGRATION_DIR=/opt/tellor/autoTasks/migration
UNIT_DIR=/etc/systemd/system
ROUTE_FILE=${MIGRATION_DIR}/secrets/discord_webhooks.json
ENV_FILE=${MIGRATION_DIR}/.env
MONITORING_USER=tellor-monitoring

if ! getent group "${MONITORING_USER}" >/dev/null; then
  groupadd --system "${MONITORING_USER}"
fi
if ! getent passwd "${MONITORING_USER}" >/dev/null; then
  useradd --system --gid "${MONITORING_USER}" --home-dir /nonexistent \
    --shell /sbin/nologin "${MONITORING_USER}"
fi

install -d -o "${MONITORING_USER}" -g "${MONITORING_USER}" -m 0700 \
  /var/lib/tellor/report-freshness
install -d -o root -g root -m 0700 /var/lib/tellor/monitor-watchdog

for unit in \
  tellor-report-freshness.service \
  tellor-report-freshness.timer \
  monitor-watchdog.service \
  monitor-watchdog.timer
do
  install -o root -g root -m 0644 "${SOURCE_DIR}/systemd/${unit}" "${UNIT_DIR}/${unit}"
done

systemctl daemon-reload

if [[ "${1:-}" != "--enable" ]]; then
  echo "Units installed but not enabled. Run $0 --enable after the local and route gates pass."
  exit 0
fi

test -f "${MIGRATION_DIR}/report_freshness.py"
test -f "${MIGRATION_DIR}/ops/monitor_watchdog.py"
test -f "${ROUTE_FILE}"
test ! -L "${ROUTE_FILE}"

if [[ ! -f "${ENV_FILE}" || -L "${ENV_FILE}" ]]; then
  echo ".env must be a regular non-symlink file" >&2
  exit 1
fi
env_mode=$(stat -c '%a' "${ENV_FILE}")
if [[ "${env_mode}" != "600" ]]; then
  echo ".env must have mode 0600" >&2
  exit 1
fi
env_owner=$(stat -c '%U:%G' "${ENV_FILE}")
if [[ "${env_owner}" != "root:root" ]]; then
  echo ".env must be owned by root:root" >&2
  exit 1
fi

route_mode=$(stat -c '%a' "${ROUTE_FILE}")
if [[ "${route_mode}" != "600" ]]; then
  echo "route file must have mode 0600" >&2
  exit 1
fi
if ! runuser -u "${MONITORING_USER}" -- test -r "${ROUTE_FILE}"; then
  echo "route file must be readable by ${MONITORING_USER} without relaxing mode 0600" >&2
  exit 1
fi

if ! grep -Eq '^RPC_ETHEREUM_MAINNET(_FALLBACK_(BLOCKPI|DRPC))?=.+$' "${ENV_FILE}"; then
  echo "at least one Ethereum mainnet RPC must be configured" >&2
  exit 1
fi

(
  cd "${MIGRATION_DIR}"
  DISCORD_WEBHOOKS_FILE="${ROUTE_FILE}" python3 quickstart.py check-discord-routes
)

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify \
    "${UNIT_DIR}/tellor-report-freshness.service" \
    "${UNIT_DIR}/tellor-report-freshness.timer" \
    "${UNIT_DIR}/monitor-watchdog.service" \
    "${UNIT_DIR}/monitor-watchdog.timer"
fi

systemctl enable --now tellor-report-freshness.timer monitor-watchdog.timer
systemctl is-enabled tellor-report-freshness.timer monitor-watchdog.timer
systemctl is-active tellor-report-freshness.timer monitor-watchdog.timer
