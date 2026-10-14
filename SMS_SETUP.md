# Laundry Monitor — SMS 수신자 설정

## 이번 변경

기존 `app.py`, `templates/index.html`, `static/dashboard.js`에 알림 기능을 연결했습니다. `realtime.py`, `history.py`, Gunicorn 설정, 기존 센서/기록 API는 그대로입니다.

1. 숫자 4자리 공통 비밀번호로 잠금 해제합니다. 인증 유효 시간은 1분입니다. 전송 버튼 옆에 남은 초가 표시되고, 만료되면 선택 화면이 자동으로 잠깁니다. 인증은 브라우저별이며 같은 브라우저의 탭끼리는 쿠키를 공유합니다. 저장한 수신자는 인증 만료 후에도 그대로 유지됩니다. 새로고침이나 수신자 저장으로 인증 시간이 연장되지 않습니다.
2. 인증 후 표시되는 이름 세 개 중 한 명 이상을 체크박스로 선택합니다.
3. **전송**을 누르면 현재 cycle에 선택한 수신자 ID 목록만 저장합니다.
4. `Notification recipients: Wally, Flatmate A`처럼 저장된 이름을 표시합니다.

이 버튼은 SMS를 보내지 않습니다. 일반 화면에는 SMS 테스트/완료 처리 버튼과 endpoint가 없습니다. 실제 완료 감지와 SMS hook을 연결했습니다. 계정/번호 설정 전에는 `SMS_ENABLED=false`를 유지하고, 실제 시험할 때만 설정 도구로 활성화하세요. 전화번호, 비밀번호 hash, API key는 HTML/JS나 API 응답에 포함하지 않습니다. 인증 이전에는 이름 목록도 보내지 않습니다.

동시에 두 사람이 수정하면 먼저 저장된 변경을 보호하고, 나머지 요청은 409 오류와 새로고침 안내를 받습니다. 다른 기기에서 바꾼 상태는 **알림 상태 새로고침**으로 확인합니다. 설정이 누락되거나 알림 저장소가 실패해도 telemetry 화면은 계속 동작합니다.

## 필요한 서버 환경변수

| 이름 | 값/역할 |
|---|---|
| `NOTIFICATION_SECRET_KEY` | 무작위 서명 키. 아래 도구로 생성. 모든 재시작에서 같은 값 유지 |
| `NOTIFICATION_PASSWORD_HASH` | 숫자 4자리 공통 비밀번호의 Werkzeug hash. 평문 비밀번호 저장 불필요 |
| `NOTIFICATION_RECIPIENTS_JSON` | 정확히 세 명의 `id`, `name`, `phone` 배열 |
| `NOTIFICATION_STORAGE_ACCOUNT` | Azure 저장 계정. 현재 프로젝트 예: `testresourcegroupb5d7` |
| `NOTIFICATION_CONTAINER` | 기본 `laundry-notifications`. 비공개 Blob container |
| `NOTIFICATION_COOKIE_SECURE` | 기본 `true`. 로컬 HTTP 개발에서만 `false` |
| `SMS_ENABLED` | 실제 SMS 시험 시 `true`로 변경 |
| `PURESMS_API_KEY` | 추후 발송 연결 시 설정. 지금 없어도 수신자 저장 가능 |
| `PURESMS_SENDER` | 추후 승인된 sender 또는 테스트용 `ConnectTest`. 계산만 할 때는 없어도 됨 |

`id`는 저장 후 바꾸지 마세요. 이름/번호를 바꾸더라도 ID는 유지해야 기존 선택과 연결됩니다. 번호는 `+44...`와 같은 E.164 형식입니다. 아래 번호는 실제 수신자가 아닌 예시입니다.

```json
[{"id":"wally","name":"Wally","phone":"+447700900001"},{"id":"flatmate-a","name":"Flatmate A","phone":"+447700900002"},{"id":"flatmate-b","name":"Flatmate B","phone":"+447700900003"}]
```

실제 이름과 번호로 바꾼 JSON을 **Azure 서버 설정에만** 입력하세요. 프런트엔드 코드를 수정할 필요는 없습니다. 이름은 인증된 사용자에게 보여야 하므로 서버가 `id/name`만 반환합니다.

비밀번호와 서명 키 만들기:

```bash
cd /Users/wally/Documents/laundry-web
.venv/bin/python scripts/notification_secrets.py
```

숫자 4자리를 화면에 표시되지 않게 두 번 입력합니다. 앞자리가 0인 비밀번호도 사용할 수 있습니다. 기존 비밀번호가 4자리 숫자가 아니었다면 새 hash를 생성해 `NOTIFICATION_PASSWORD_HASH`를 교체해야 합니다. 도구가 출력한 `NOTIFICATION_SECRET_KEY`와 `NOTIFICATION_PASSWORD_HASH` 값을 아래 Azure 설정에 각각 넣으세요. 출력도 공개 저장소나 채팅에 올리지 마세요. hash에 `$`가 들어가므로 명령행에서 직접 입력한다면 반드시 작은따옴표를 사용해야 합니다. Portal에 붙여넣는 방법을 권장합니다.

## Azure App Service 재배포

기존 프로젝트에 기록된 대상은 resource group `laundry`, App Service `laundry-monitor`, storage account `testresourcegroupb5d7`입니다. 아래 절차는 작성만 되었으며 이번 작업에서 Azure 설정/배포/권한을 변경하지 않았습니다.

### 1. 알림 상태 전용 비공개 저장소

Azure Portal → 해당 Storage account → Containers에서 `laundry-notifications`를 **Private**으로 생성합니다. 기존 `telemetry` container를 사용하거나 공개 권한을 켜지 마세요.

App Service의 System-assigned Managed Identity가 켜져 있는지 확인합니다. 새 container의 Access control (IAM)에서 이 웹앱 identity에 **Storage Blob Data Contributor** 역할을 부여합니다. 범위는 이 container 하나로 제한합니다. 기존 telemetry/Table 권한은 유지합니다.

같은 작업을 CLI로 할 경우, container 생성 및 역할 부여 권한이 있는 계정으로 실행합니다:

```bash
az account show --query '{subscription:name,id:id}' -o table

az storage container create \
  --account-name testresourcegroupb5d7 \
  --name laundry-notifications --auth-mode login --public-access off -o none

NOTIFICATION_STORAGE_ID=$(az storage account show \
  --resource-group test-resourcegroup --name testresourcegroupb5d7 --query id -o tsv)
NOTIFICATION_WEB_ID=$(az webapp identity show \
  --resource-group laundry --name laundry-monitor --query principalId -o tsv)

# 두 값이 비어 있으면 실행하지 말고 계정/Managed Identity를 확인합니다.
az role assignment create \
  --assignee-object-id "$NOTIFICATION_WEB_ID" \
  --assignee-principal-type ServicePrincipal \
  --role 'Storage Blob Data Contributor' \
  --scope "$NOTIFICATION_STORAGE_ID/blobServices/default/containers/laundry-notifications" \
  -o none
```

권한 반영에 시간이 걸릴 수 있습니다. 이 저장소에는 수신자 ID 목록, cycle ID, 수정 번호, 발송 처리 상태만 저장합니다. 전화번호와 비밀번호는 저장하지 않습니다. Azure 실행 중에는 로컬 파일로 몰래 대체하지 않고 Blob 오류를 화면에 표시합니다.

### 2. App Service 환경변수

Azure Portal → `laundry-monitor` → Settings → Environment variables → App settings에서 위 표의 필수 값을 추가한 뒤 적용합니다. 기존 `EVENTHUB_*`, `TABLE_*`, `HISTORY_SOURCE`, `DEVICE_ID` 설정은 유지합니다.

- `NOTIFICATION_SECRET_KEY`, `NOTIFICATION_PASSWORD_HASH`: 방금 생성한 값
- `NOTIFICATION_RECIPIENTS_JSON`: 실제 3명으로 작성한 JSON
- `NOTIFICATION_STORAGE_ACCOUNT=testresourcegroupb5d7`
- `NOTIFICATION_CONTAINER=laundry-notifications`
- `NOTIFICATION_COOKIE_SECURE=true`
- `SMS_ENABLED=false`
- `SCM_DO_BUILD_DURING_DEPLOYMENT=true`

App Service의 **HTTPS Only**를 켭니다. Azure에서는 코드가 Secure cookie를 강제합니다. API key/sender는 지금 입력하지 않아도 됩니다. 환경변수 적용은 앱을 재시작할 수 있습니다.

### 3. ZIP 생성과 배포

```bash
cd /Users/wally/Documents/laundry-web
.venv/bin/python scripts/package_web.py

az webapp config appsettings set -g laundry -n laundry-monitor \
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true -o none
az webapp config set -g laundry -n laundry-monitor \
  --startup-file 'gunicorn -c gunicorn.conf.py app:app' -o none
az webapp deploy -g laundry -n laundry-monitor \
  --type zip --src-path dist/laundry-web.zip
```

제공된 배포 ZIP을 바로 사용해도 됩니다. ZIP 루트에 `app.py`, `notifications.py`, `requirements.txt` 등이 들어 있습니다. 비밀번호, 실제 번호, 로컬 상태, `.venv`는 포함하지 않습니다. 기존 실시간 fan-out 구조 때문에 **worker 1개, App Service instance 1개**를 유지하세요. 추가 패키지는 필요하지 않습니다.

### 4. 배포 후 확인

- 기존 실시간 상태·온도·대기압·그래프 및 저장 기록이 정상인지 확인합니다.
- 비밀번호를 입력하기 전에는 이름 선택 항목이 보이지 않아야 합니다.
- 틀린 비밀번호는 거절되고, 올바른 비밀번호로 세 개의 체크박스와 전송 버튼이 나와야 합니다.
- 한 명 이상 체크 → **전송** → 현재 이름 표시를 확인합니다. SMS는 오지 않아야 합니다.
- 페이지 재방문/웹앱 재시작 후 다시 인증하면 저장된 이름이 남아 있어야 합니다.
- 다른 브라우저에서도 같은 선택 상태를 확인합니다.

## 로컬 실행

현재 로컬 프로젝트에는 비밀번호 `0123`을 사용하도록 `.env`을 설정했습니다. 실제 파일에는 비밀번호 hash와 서버 서명 키를 저장하며, Git과 배포 ZIP에서 제외합니다. `app.py`가 이 파일을 자동으로 읽으므로 매번 환경변수를 직접 입력하지 않아도 됩니다. Azure에서는 이 파일을 읽지 않고 App Service 환경변수만 사용합니다. 명시적으로 설정한 셸 환경변수가 있다면 그 값이 로컬 파일보다 우선합니다.

```bash
cd /Users/wally/Documents/laundry-web
bash run_local.sh
```

센서 연결 없이 알림 UI만 확인하려면 `.venv/bin/python app.py`로 실행할 수 있습니다. `http://localhost:5001`에서 `0123`을 입력하세요. 코드/설정을 바꾼 뒤에는 기존 서버를 Ctrl+C로 종료한 다음 다시 실행하세요. 일반 `python app.py`를 사용하려면 먼저 `source .venv/bin/activate`가 필요합니다. `.env.example`은 자동으로 읽지 않습니다.

로컬 기본 이름은 Wally / Flatmate A / Flatmate B입니다. 실제 번호는 아직 설정하지 않았습니다. `SMS_ENABLED=false`일 때는 전화번호를 빈 문자열로 두고 수신자 선택만 사용할 수 있습니다. 실제 발송을 켜려면 선택할 사람의 올바른 번호를 먼저 설정해야 합니다. 미등록 번호의 수신자는 선택할 수 없습니다. 로컬 HTTP에서는 `NOTIFICATION_COOKIE_SECURE=false`를 사용하며, Azure는 Secure cookie를 강제합니다.

로컬 수신자 상태는 기본 `instance/notifications.json`에 저장됩니다. 필요하면 `NOTIFICATION_STATE_FILE`로 변경합니다. Azure에서는 `WEBSITE_HOSTNAME`을 감지해 항상 Blob을 사용합니다. 로컬에서 Blob을 검증하려면 `NOTIFICATION_STORAGE=blob`과 Azure 인증/해당 container 권한을 설정합니다.

테스트는 실제 Azure나 SMS를 호출하지 않습니다:

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/dashboard.test.cjs tests/notifications.test.cjs
```

## 완료 감지 연결 완료

이제 `cycle_detector.py`가 실시간 수신 경로와 연결되어 있습니다. 수신자 저장 후 **이번 코스 시작 기록**을 누른 코스만 계산하며, 완료 시 `on_state_transition()`을 호출합니다. 실제 사용법·SMS 설정·재생 결과·제약은 [CYCLE_TESTING.md](CYCLE_TESTING.md)를 참고하세요. 코스 모드는 현재 기록용 라벨이고 모든 모드에 같은 실험 규칙을 적용합니다.

## 검증 범위와 복구

Python 테스트 67개와 기존 dashboard 시나리오 9개, 알림 UI 이벤트 흐름 테스트가 통과했습니다. 인증/CSRF/4자리 숫자 인증/체크박스 복수 선택/빈 선택·중복 ID 거절/기존 단일 수신자 데이터 변환/비밀값 비노출/저장소 오류/재시작/동시 수정/중복 완료/발송 타임아웃을 검증합니다. Azure Blob과 SMS 네트워크 호출은 mock으로 검증했으며, 실서비스 배포나 실번호 SMS 수신은 검증하지 않았습니다. 이 실행 환경에서 로컬 서버 포트 열기가 허용되지 않아 실제 브라우저 화면 검증은 수행하지 못했습니다.

배포 전 기존 소스를 백업하세요. 롤백은 이전 배포 ZIP으로 재배포하면 됩니다. 알림 Blob을 삭제하면 중복 방지 기록도 사라지므로 이전 cycle ID로 hook을 재실행하지 마세요. 새 기능의 서버 설정을 비워두면 기존 dashboard는 동작하고 알림 영역만 설정 안내를 표시합니다.

## 공식 문서

- [PureSMS 개발자 시작 가이드](https://the.divergent.guide/puresms/developers/)
- [Flask 웹 보안](https://flask.palletsprojects.com/en/stable/web-security/)
- [Azure Blob 동시성 제어](https://learn.microsoft.com/en-us/azure/storage/blobs/concurrency-manage)
- [Azure Python 앱 설정](https://learn.microsoft.com/en-us/azure/app-service/configure-language-python)
- [Azure Python ZIP 배포](https://learn.microsoft.com/en-us/azure/app-service/quickstart-python)
