#!/usr/bin/env bash
# Secret-free AWS control-plane or local-host inventory. This script contains
# no SSM Run Command/session calls and never queries Parameter Store values.
set -euo pipefail

usage() {
  echo "usage:" >&2
  echo "  $0 aws --region REGION --instance-id INSTANCE_ID" >&2
  echo "  $0 host" >&2
  exit 2
}

if [[ "$#" -lt 1 ]]; then
  usage
fi

mode=$1
shift
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

case "${mode}" in
  host)
    [[ "$#" -eq 0 ]] || usage
    printf '%s\n' 'inventory_scope=local-host-read-only'
    date -u +observed_at_utc=%Y-%m-%dT%H:%M:%SZ
    hostname
    id
    /usr/bin/python3 "${SCRIPT_DIR}/monitor_state.py" status || true
    systemctl is-enabled \
      openzeppelin-monitor.service \
      monitor-watchdog.timer \
      tellor-report-freshness.timer \
      refprice-daily.timer \
      cpi-bigmac.timer || true
    systemctl is-active \
      openzeppelin-monitor.service \
      monitor-watchdog.timer \
      tellor-report-freshness.timer \
      refprice-daily.timer \
      cpi-bigmac.timer || true
    systemctl list-timers --all \
      monitor-watchdog.timer \
      tellor-report-freshness.timer \
      refprice-daily.timer \
      cpi-bigmac.timer || true
    systemctl --failed --no-legend || true
    ;;
  aws)
    region=
    instance_id=
    while [[ "$#" -gt 0 ]]; do
      case "$1" in
        --region)
          [[ "$#" -ge 2 ]] || usage
          region=$2
          shift 2
          ;;
        --instance-id)
          [[ "$#" -ge 2 ]] || usage
          instance_id=$2
          shift 2
          ;;
        *)
          usage
          ;;
      esac
    done
    [[ "${region}" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]$ ]] || usage
    [[ "${instance_id}" =~ ^i-[0-9a-f]{8,17}$ ]] || usage

    AWS=(aws --no-cli-pager --region "${region}")
    printf '%s\n' 'inventory_scope=aws-control-plane-read-only'
    date -u +observed_at_utc=%Y-%m-%dT%H:%M:%SZ
    printf 'region=%s\n' "${region}"
    "${AWS[@]}" sts get-caller-identity --output json
    "${AWS[@]}" ec2 describe-instances \
      --instance-ids "${instance_id}" --output json

    security_group_ids=$("${AWS[@]}" ec2 describe-instances \
      --instance-ids "${instance_id}" \
      --query 'Reservations[].Instances[].SecurityGroups[].GroupId' \
      --output text)
    if [[ -n "${security_group_ids}" && "${security_group_ids}" != "None" ]]; then
      read -r -a security_group_array <<< "${security_group_ids}"
      "${AWS[@]}" ec2 describe-security-groups \
        --group-ids "${security_group_array[@]}" --output json
    fi

    volume_ids=$("${AWS[@]}" ec2 describe-instances \
      --instance-ids "${instance_id}" \
      --query 'Reservations[].Instances[].BlockDeviceMappings[].Ebs.VolumeId' \
      --output text)
    if [[ -n "${volume_ids}" && "${volume_ids}" != "None" ]]; then
      read -r -a volume_array <<< "${volume_ids}"
      "${AWS[@]}" ec2 describe-volumes \
        --volume-ids "${volume_array[@]}" --output json
    fi

    "${AWS[@]}" ssm describe-instance-information \
      --filters "Key=InstanceIds,Values=${instance_id}" --output json
    "${AWS[@]}" cloudwatch describe-alarms \
      --query "MetricAlarms[?contains(Dimensions[].Value, '${instance_id}') || contains(AlarmName, '${instance_id}')]" \
      --output json

    account_id=$("${AWS[@]}" sts get-caller-identity \
      --query Account --output text)
    resource_arn="arn:aws:ec2:${region}:${account_id}:instance/${instance_id}"
    "${AWS[@]}" backup list-recovery-points-by-resource \
      --resource-arn "${resource_arn}" --output json

    instance_profile_arn=$("${AWS[@]}" ec2 describe-instances \
      --instance-ids "${instance_id}" \
      --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' \
      --output text)
    if [[ -n "${instance_profile_arn}" && "${instance_profile_arn}" != "None" ]]; then
      instance_profile_name=${instance_profile_arn##*/}
      "${AWS[@]}" iam get-instance-profile \
        --instance-profile-name "${instance_profile_name}" --output json
      role_names=$("${AWS[@]}" iam get-instance-profile \
        --instance-profile-name "${instance_profile_name}" \
        --query 'InstanceProfile.Roles[].RoleName' --output text)
      for role_name in ${role_names}; do
        "${AWS[@]}" iam list-attached-role-policies \
          --role-name "${role_name}" --output json
        "${AWS[@]}" iam list-role-policies \
          --role-name "${role_name}" --output json
      done
    fi
    ;;
  *)
    usage
    ;;
esac
