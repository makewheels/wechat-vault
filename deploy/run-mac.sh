#!/bin/sh
set -eu
# Account/device are explicit so credentials cannot bleed across machines.
account=${WECHAT_ACCOUNT_ID:?Set WECHAT_ACCOUNT_ID to the real WeChat wxid}
device=${WECHAT_DEVICE_ID:?Set WECHAT_DEVICE_ID to the secret folder for this machine}
case "$account:$device" in *[!a-zA-Z0-9_:\-]*) echo 'Invalid account/device identifier' >&2; exit 2;; esac
project_id=79c61872-d992-44c7-a5ae-d3e70e004e7f
exec infisical run --projectId="$project_id" --env=dev --path=/wechat-vault -- \
  infisical run --projectId="$project_id" --env=dev --path="/wechat-vault/$account/$device" -- "$@"
