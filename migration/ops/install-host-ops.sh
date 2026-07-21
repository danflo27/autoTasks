#!/usr/bin/env bash
# Install reviewed host-operations artifacts without changing workload state.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "install-host-ops.sh must run as root" >&2
  exit 1
fi
if [[ "$#" -ne 0 ]]; then
  echo "usage: $0" >&2
  exit 2
fi

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SYSTEMD_DIR=/etc/systemd/system
MONITOR_UNIT=openzeppelin-monitor.service
WATCHDOG_UNIT=monitor-watchdog.timer

unit_state() {
  local operation=$1
  local unit=$2
  local state
  state=$(systemctl "${operation}" "${unit}" 2>/dev/null || true)
  case "${state}" in
    active|inactive|failed|activating|deactivating|reloading|maintenance|\
    enabled|disabled|masked|masked-runtime|enabled-runtime|linked|linked-runtime|\
    static|indirect|generated|transient|not-found)
      printf '%s\n' "${state}"
      ;;
    *)
      echo "could not read ${operation} state for ${unit}" >&2
      exit 1
      ;;
  esac
}

monitor_enabled_before=$(unit_state is-enabled "${MONITOR_UNIT}")
monitor_active_before=$(unit_state is-active "${MONITOR_UNIT}")
watchdog_enabled_before=$(unit_state is-enabled "${WATCHDOG_UNIT}")
watchdog_active_before=$(unit_state is-active "${WATCHDOG_UNIT}")

# The additive installer creates the service user/state directories and writes
# only version-controlled, non-secret unit definitions. With no --enable flag,
# it never enables or starts a timer.
"${SOURCE_DIR}/install-monitoring.sh"

install -d -o root -g root -m 0755 \
  "${SYSTEMD_DIR}/openzeppelin-monitor.service.d" \
  "${SYSTEMD_DIR}/monitor-watchdog.timer.d"
install -o root -g root -m 0644 \
  "${SOURCE_DIR}/systemd/openzeppelin-monitor.service.d/10-watchdog-coupling.conf" \
  "${SYSTEMD_DIR}/openzeppelin-monitor.service.d/10-watchdog-coupling.conf"
install -o root -g root -m 0644 \
  "${SOURCE_DIR}/systemd/monitor-watchdog.timer.d/10-monitor-coupling.conf" \
  "${SYSTEMD_DIR}/monitor-watchdog.timer.d/10-monitor-coupling.conf"

systemctl daemon-reload

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify \
    "${SYSTEMD_DIR}/monitor-watchdog.service" \
    "${SYSTEMD_DIR}/monitor-watchdog.timer"
fi

monitor_enabled_after=$(unit_state is-enabled "${MONITOR_UNIT}")
monitor_active_after=$(unit_state is-active "${MONITOR_UNIT}")
watchdog_enabled_after=$(unit_state is-enabled "${WATCHDOG_UNIT}")
watchdog_active_after=$(unit_state is-active "${WATCHDOG_UNIT}")

if [[ "${monitor_enabled_before}" != "${monitor_enabled_after}" ]] || \
   [[ "${monitor_active_before}" != "${monitor_active_after}" ]]; then
  echo "installer changed Monitor lifecycle state; stop and reconcile" >&2
  exit 1
fi
if [[ "${watchdog_enabled_before}" != "not-found" ]] && \
   { [[ "${watchdog_enabled_before}" != "${watchdog_enabled_after}" ]] || \
     [[ "${watchdog_active_before}" != "${watchdog_active_after}" ]]; }; then
  echo "installer changed watchdog lifecycle state; stop and reconcile" >&2
  exit 1
fi

echo "Host operations installed without changing workload desired state."
echo "Read back with: python3 ${SOURCE_DIR}/monitor_state.py status"
echo "Use monitor_state.py enable|disable only after the reviewed deployment gates."
