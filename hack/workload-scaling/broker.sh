#!/usr/bin/env bash
# © Broadcom. All Rights Reserved.
# The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
# SPDX-License-Identifier: Apache-2.0
# Run on the dedicated broker VM. Requires Docker and a mode-0600 env file.
set -euo pipefail
operation=${1:?Usage: broker.sh start|stop RUN_ID ENV_FILE}
run_id=${2:?Run ID required}
[[ "$run_id" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || exit 2
name="vmrs-rabbitmq-$run_id"
case "$operation" in
  start)
    env_file=${3:?Env file with RABBITMQ_DEFAULT_USER and RABBITMQ_DEFAULT_PASS required}
    [[ $(stat -c %a "$env_file") == 600 ]] || { echo 'Env file must be mode 0600' >&2; exit 2; }
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    broker_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["rabbitmq"])' "$script_dir/dependencies.json")
    docker run -d --name "$name" --label "vmrs-lab-run=$run_id" \
      --env-file "$env_file" -p 5672:5672 -p 15672:15672 \
      "$broker_image"
    ;;
  stop)
    [[ $(docker inspect --format '{{index .Config.Labels "vmrs-lab-run"}}' "$name") == "$run_id" ]] || exit 2
    docker rm -f "$name"
    ;;
  *) echo 'Expected start or stop' >&2; exit 2 ;;
esac
