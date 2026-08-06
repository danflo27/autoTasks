#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="$script_dir/docker-compose.production.yaml"
environment_file="$script_dir/.env.production"
action=${1:---check}

case "$action" in
  --check|--up-log-only|--up-live) ;;
  *)
    echo "usage: $0 [--check|--up-log-only|--up-live]" >&2
    exit 2
    ;;
esac

if [ ! -f "$environment_file" ]; then
  echo "missing $environment_file; copy environment.example first" >&2
  exit 1
fi

permissions=$(stat -f '%Lp' "$environment_file" 2>/dev/null || stat -c '%a' "$environment_file")
if [ "$permissions" != "600" ]; then
  echo "$environment_file must have mode 0600" >&2
    exit 1
fi

read_env_setting() {
  setting_name=$1
  default_value=$2
  setting_count=$(awk -F= -v key="$setting_name" '$1 == key { count++ } END { print count + 0 }' "$environment_file")
  if [ "$setting_count" -gt 1 ]; then
    echo "$environment_file contains duplicate $setting_name entries" >&2
    return 1
  fi
  setting_value=$(awk -F= -v key="$setting_name" '$1 == key { print substr($0, index($0, "=") + 1) }' "$environment_file")
  # Strip CR (CRLF line endings) and surrounding whitespace so this matches
  # the Python config loader's `.strip()` semantics exactly. Without this, a
  # value like "live\r" would fail an exact-match comparison here while the
  # service still reads it as "live", defeating the safety gates below.
  setting_value=$(printf '%s' "$setting_value" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  if [ -n "$setting_value" ]; then
    printf '%s\n' "$setting_value"
  else
    printf '%s\n' "$default_value"
  fi
}

runtime_uid=$(read_env_setting TELLOR_RUNTIME_UID 1000)
runtime_gid=$(read_env_setting TELLOR_RUNTIME_GID 1000)
case "$runtime_uid:$runtime_gid" in
  *[!0-9:]*|:*|*:)
    echo "TELLOR_RUNTIME_UID and TELLOR_RUNTIME_GID must be decimal IDs" >&2
    exit 1
    ;;
esac

install -d -m 0700 \
  "$script_dir/runtime/monitor-data" \
  "$script_dir/runtime/spool" \
  "$script_dir/runtime/alert-gate" \
  "$script_dir/secrets" \
  "$script_dir/secrets/release-manifests"

for runtime_directory in \
  "$script_dir/runtime/monitor-data" \
  "$script_dir/runtime/spool" \
  "$script_dir/runtime/alert-gate" \
  "$script_dir/secrets" \
  "$script_dir/secrets/release-manifests"
do
  owner=$(stat -f '%u:%g' "$runtime_directory" 2>/dev/null || stat -c '%u:%g' "$runtime_directory")
  if [ "$owner" != "$runtime_uid:$runtime_gid" ]; then
    echo "$runtime_directory is owned by $owner; expected $runtime_uid:$runtime_gid" >&2
    echo "fix ownership before running containers" >&2
    exit 1
  fi
done

compose() {
  docker compose --env-file "$environment_file" -f "$compose_file" "$@"
}

compose config --quiet
compose build alert-gate
compose run --rm --no-deps alert-gate check-config
compose run --rm --no-deps monitor --check

if [ "$action" = "--check" ]; then
  echo "production configuration checks passed; no services were changed"
  exit 0
fi

for seed_file in "$script_dir/secrets/bridge_ledger_seed.json"
do
  if [ ! -f "$seed_file" ]; then
    echo "missing required verified M4 bridge-ledger seed: $seed_file" >&2
    exit 1
  fi
  seed_permissions=$(stat -f '%Lp' "$seed_file" 2>/dev/null || stat -c '%a' "$seed_file")
  if [ "$seed_permissions" != "600" ]; then
    echo "$seed_file must have mode 0600" >&2
    exit 1
  fi
done

layer_replay_start_height=$(read_env_setting LAYER_REPLAY_START_HEIGHT "")
if [ "$layer_replay_start_height" != "1" ]; then
  seed_file="$script_dir/secrets/layer_minter_seed.json"
  if [ ! -f "$seed_file" ]; then
    echo "missing required verified minter seed: $seed_file" >&2
    exit 1
  fi
  seed_permissions=$(stat -f '%Lp' "$seed_file" 2>/dev/null || stat -c '%a' "$seed_file")
  if [ "$seed_permissions" != "600" ]; then
    echo "$seed_file must have mode 0600" >&2
    exit 1
  fi
fi

tellor_alert_delivery_mode=$(read_env_setting TELLOR_ALERT_DELIVERY_MODE "")
if [ "$action" = "--up-live" ]; then
  if [ "$tellor_alert_delivery_mode" != "live" ]; then
    echo "--up-live requires TELLOR_ALERT_DELIVERY_MODE=live" >&2
    exit 1
  fi
  for secret_file in "$script_dir/secrets/discord_webhooks.json"; do
    if [ ! -f "$secret_file" ]; then
      echo "missing required secret/seed file: $secret_file" >&2
      exit 1
    fi
    secret_permissions=$(stat -f '%Lp' "$secret_file" 2>/dev/null || stat -c '%a' "$secret_file")
    if [ "$secret_permissions" != "600" ]; then
      echo "$secret_file must have mode 0600" >&2
      exit 1
    fi
  done
elif [ "$tellor_alert_delivery_mode" = "live" ]; then
  echo "--up-log-only refuses an environment configured for live delivery" >&2
  exit 1
fi

if [ "$action" = "--up-live" ]; then
  compose run --rm --no-deps alert-gate check-live
else
  compose run --rm --no-deps alert-gate check-inputs
fi
compose up -d --remove-orphans
compose ps
