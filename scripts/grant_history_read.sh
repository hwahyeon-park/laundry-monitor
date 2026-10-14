#!/usr/bin/env bash
set -euo pipefail
SUBSCRIPTION=ff415929-a3cd-4239-8d48-1c55f6deefb5
SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/test-resourcegroup/providers/Microsoft.Storage/storageAccounts/testresourcegroupb5d7/tableServices/default/tables/LaundryTelemetry"
[[ "$(az account show --query id -o tsv)" == "$SUBSCRIPTION" ]] || { printf 'Azure 구독을 확인하세요.\n' >&2; exit 1; }
LOCAL_USER="$(az ad signed-in-user show --query id -o tsv)"
WEB_IDENTITY="$(az webapp identity show --name laundry-monitor --resource-group laundry --query principalId -o tsv)"
[[ -n "$LOCAL_USER" && -n "$WEB_IDENTITY" ]] || { printf '로컬 사용자 또는 웹앱 identity가 없습니다.\n' >&2; exit 1; }

# 조회 대상 테이블 한 개에만 읽기 권한. 저장 Function의 권한은 변경하지 않음.
az role assignment create --assignee-object-id "$LOCAL_USER" --assignee-principal-type User \
    --role 'Storage Table Data Reader' --scope "$SCOPE" --output none
az role assignment create --assignee-object-id "$WEB_IDENTITY" --assignee-principal-type ServicePrincipal \
    --role 'Storage Table Data Reader' --scope "$SCOPE" --output none
printf '로컬 사용자와 laundry-monitor의 Table 읽기 권한 설정 완료. 반영에 시간이 걸릴 수 있습니다.\n'
