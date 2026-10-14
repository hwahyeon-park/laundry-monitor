#!/usr/bin/env bash
set -euo pipefail
# 매번 이 실행 프로세스 안에서만 설정. 연결 문자열은 파일/터미널에 출력하지 않음.
[[ "$(az account show --query id -o tsv)" == 'ff415929-a3cd-4239-8d48-1c55f6deefb5' ]] || {
    printf 'Azure 구독을 확인하세요.\n' >&2; exit 1;
}
cd "$(dirname "$0")"
EVENTHUB_CONNECTION_STRING="$(az iot hub connection-string show \
    --hub-name laundry-hub-paid --resource-group laundry \
    --policy-name web-live --default-eventhub --query connectionString -o tsv)"
HISTORY_SOURCE="$(az iot hub show --name laundry-hub-paid --resource-group laundry \
    --query properties.eventHubEndpoints.events.path -o tsv)"
[[ "$EVENTHUB_CONNECTION_STRING" == Endpoint=sb://* && -n "$HISTORY_SOURCE" ]] || {
    printf 'IoT Hub 연결 설정을 가져오지 못했습니다.\n' >&2; exit 1;
}
export EVENTHUB_CONNECTION_STRING HISTORY_SOURCE
export EVENTHUB_AUTH_MODE=connection_string
export EVENTHUB_CONSUMER_GROUP=laundry-web
export DEVICE_ID=laundry-esp-paid
export REALTIME_ENABLED=true
export TELEMETRY_STALE_SECONDS=30
export TABLE_ACCOUNT_URL=https://testresourcegroupb5d7.table.core.windows.net
export TABLE_NAME=LaundryTelemetry
exec .venv/bin/python app.py
