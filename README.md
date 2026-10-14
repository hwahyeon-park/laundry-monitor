# Laundry Monitor

A sensor dashboard for a shared washing machine: ESP32 acceleration and environmental telemetry, Azure storage, live charts, and rule-based cycle completion notifications.

## Korean and English pages

- `kor`: the original Korean interface.
- `eng`: a professional English interface for project demonstrations.
- Only `templates/index.html` differs between the two branches. The English page includes the text catalogue used by the shared JavaScript files; backend state and SMS behaviour stay the same.
- English is an interface translation. It does not create a second Azure app or translate recipients’ names, stored records, or SMS messages.

Switch language and create a deployment package:

```sh
git switch kor
python scripts/package_web.py --output dist/laundry-web-kor.zip

git switch eng
python scripts/package_web.py --output dist/laundry-web-eng.zip
```

Deploy the selected ZIP to the existing App Service using your usual deployment process. Do not deploy a Git source archive: the packaging script includes only runtime files. Changing branches or building a ZIP does not change the live website. App Service environment variables, including `CYCLE_DETECTOR_BACKEND=function`, remain in Azure.

For shared code changes, make the change on `kor`, commit it separately from language edits, and cherry-pick that commit onto `eng`. Keep the page catalogue in `eng` updated when adding new user-facing text. Check the branches with `git diff kor eng --name-only`: only the page should differ after synchronising shared changes.

## Local configuration and secrets

Copy `.env.example` to `.env` only if `.env` does not already exist, then fill in your own values. `.env` is loaded locally with this precedence: explicit process environment → `.env` → legacy `.local-notifications.json`. Azure never loads these local files; use App Service environment variables there.

`.env`, legacy credentials, recipient state, virtual environments and ZIP files are excluded from Git. `.env.example` contains placeholders only. The deployment packager uses an allowlist and excludes all local settings. Existing local secrets were retained privately; production Azure credentials do not need to be copied into the repository.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

The optional `run_local.sh` is the original operator-specific Azure launcher. It retrieves the IoT connection string for that local process. For another Azure account, use your own `.env` instead. Local SMS should stay disabled unless you intentionally want to send messages.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -q
node tests/dashboard.test.cjs
node tests/notifications.test.cjs
node tests/language.test.cjs
```

The English and Korean interface use the same readings, cycle detection and notification requests. The UI distinguishes SMS provider acceptance from delivery confirmation.

---

# Laundry web

이 폴더 하나가 Flask 웹의 실행·배포 단위입니다. 실시간 SSE와 Table 과거 조회가 같은 app.py에 등록되어 있습니다. 별도 업데이트 폴더는 실행에 필요하지 않습니다.

```text
laundry-web/
  app.py                  웹 진입점
  realtime.py             IoT Hub 수신기 + SSE
  history.py              Table 과거 기록 조회
  static/dashboard.js     과거 기록 + 실시간 그래프
  templates/index.html    화면
  requirements.txt        웹 의존성 전체
  gunicorn.conf.py        Azure 웹 실행 설정
  run_local.sh            로컬 설정을 가져와 app.py 하나를 실행
  scripts/
    grant_history_read.sh Table 읽기 권한 설정(최초 1회)
    verify_history.py     저장 기록 조회 확인
    package_web.py        이 폴더에서 배포 ZIP 만들기
  tests/                  웹 테스트
```

## 실행

현재 실행 중인 웹이 있으면 Ctrl+C로 종료합니다. 모든 명령은 이 폴더에서 실행합니다.

```bash
cd /Users/wally/Documents/laundry-web
.venv/bin/python -m pip install -r requirements.txt
# 최초 한 번: 이미 권한이 있다면 생략 가능
bash scripts/grant_history_read.sh
# az login 세션으로 설정을 읽어 웹 한 개 실행
bash run_local.sh
```

run_local.sh는 Azure CLI로 laundry-hub-paid의 web-live 연결 문자열을 읽어 프로세스 환경변수에 넣고 app.py를 실행합니다. 별도 서버나 저장 프로세스를 만들지 않습니다. 연결 문자열을 파일에 저장하거나 출력하지 않습니다. 필요한 환경변수가 이미 설정되어 있다면 기존처럼 `.venv/bin/python app.py`로 실행해도 됩니다. `.env.example`은 참고용이며 자동으로 읽지 않습니다.

주소: http://localhost:5001

Table 조회는 로컬 az login 사용자, Azure에서는 웹앱의 system-assigned Managed Identity로 인증합니다. grant_history_read.sh는 현재 로그인 사용자와 laundry-monitor에 해당 테이블의 Storage Table Data Reader 역할을 부여합니다. 실제 권한 변경은 사용자가 이 스크립트를 실행할 때만 수행됩니다.

## 데이터 흐름

- 실시간: ESP32 → laundry-hub-paid → events → laundry-web consumer group → Flask → SSE → 브라우저.
- 5초 요약 저장: 별도로 Azure에 배포한 laundry-writer-wally Function → LaundryTelemetry Table.
- 원본 보관: IoT Hub의 Blob route → Blob Storage.
- 웹 기록 조회: Flask history.py → Table. 원본 Blob을 반복 다운로드하지 않습니다.

저장 Function은 웹이 꺼져도 수집하도록 Azure에서 독립 실행됩니다. 웹의 실행·배포를 위해 Function 소스나 별도 로컬 실행이 필요하지 않습니다. 클라우드 라우팅/Function의 실제 저장 성공 여부는 저장 검증 결과로 확인합니다.

## 그래프와 상태

처음 열 때 Table의 최근 30분 기록을 읽고 서버 메모리의 최근 메시지와 합칩니다. SSE로 도착한 점은 과거 조회 응답이 늦어도 보존됩니다. 같은 이벤트는 한 점으로 합칩니다. 5분/30분/1시간을 선택할 수 있습니다. 저장소 조회는 처음 열기, 범위 변경, 수동 갱신, 연결 복구 때 실행하며 같은 범위 결과는 8초간 공유합니다.

5초 배치당 X/Y/Z 평균과 평균을 뺀 진동 RMS를 한 점씩 표시합니다. 가로축은 Hub 수신 시각, X/Y/Z는 왼쪽 눈금, RMS는 오른쪽 눈금입니다. 배치 사이 15초 초과 공백은 선을 끊습니다. 오래된 점이 화면 범위를 벗어나도 Table에서 삭제하지 않습니다.

수신 상태는 그래프의 과거 기록과 독립적입니다. SSE status와 30초 타임아웃으로 판단하고, 웹 연결이 끊기면 수신 상태를 확인할 수 없다고 표시합니다. 저장소 오류가 나도 실시간 그래프는 계속 동작합니다. Function 저장이 지연된 기록은 수동 다시 불러오기로 확인할 수 있습니다.

| API | 역할 |
|---|---|
| /api/stream | 새 telemetry와 상태를 SSE로 전달 |
| /api/status | 메모리의 실시간 수신 상태 |
| /api/telemetry | 호환용 최신 메모리 snapshot |
| /api/history?minutes=30 | Table 기록과 메모리의 최근 메시지 병합 |
| /api/history/latest | 기존 AVRO 수동 조회. 대시보드는 호출하지 않음 |

## 확인

웹이 실행 중일 때 새 터미널에서:

```bash
cd /Users/wally/Documents/laundry-web
.venv/bin/python scripts/verify_history.py
```

Table 개수가 0보다 커야 실제 저장 기록을 읽었다고 확인할 수 있습니다. 최근 선택 기간 안에 저장한 메시지가 있는 상태에서 보드를 끄고 새로고침해도 그래프가 복원되는지 확인합니다. 보드를 켜면 점이 추가되고 끈 뒤 약 30초가 지나면 미수신 표시가 되어야 합니다. 브라우저 Network에는 SSE 연결이 유지되고 5초마다 API 조회가 반복되지 않아야 합니다.

```bash
.venv/bin/python -m unittest discover -s tests -v
node tests/dashboard.test.cjs
```

로컬 테스트는 Azure 접속 없이 실행됩니다. Azure 권한·저장 행·실제 보드 수신은 위 수동 검사로 확인합니다.

## 같은 폴더에서 Azure 배포 준비

```bash
.venv/bin/python scripts/package_web.py
```

결과는 `dist/laundry-web.zip`입니다. app.py, realtime.py, history.py, requirements.txt, gunicorn.conf.py, static, templates를 포함합니다. 가상환경·비밀 환경변수·백업·이전 ZIP은 포함하지 않습니다. Azure 앱 설정은 이 ZIP과 별도로 설정합니다.

App Service startup:

```text
gunicorn -c gunicorn.conf.py app:app
```

필요한 App Service 환경변수:

| 이름 | 값 |
|---|---|
| REALTIME_ENABLED | true |
| EVENTHUB_AUTH_MODE | connection_string |
| EVENTHUB_CONNECTION_STRING | laundry-hub-paid의 web-live 정책 Event Hub-compatible 문자열 |
| EVENTHUB_CONSUMER_GROUP | laundry-web |
| DEVICE_ID | laundry-esp-paid |
| TELEMETRY_STALE_SECONDS | 30 |
| TABLE_ACCOUNT_URL | https://testresourcegroupb5d7.table.core.windows.net |
| TABLE_NAME | LaundryTelemetry |
| HISTORY_SOURCE | Hub의 properties.eventHubEndpoints.events.path 값(생략하면 연결 문자열 EntityPath 사용) |
| SCM_DO_BUILD_DURING_DEPLOYMENT | true |

Table은 Managed Identity로 읽지만 built-in IoT Hub endpoint는 현재 SAS 연결 문자열을 사용합니다. 웹 consumer와 저장 Function은 각각 laundry-web/laundry-writer consumer group을 사용합니다.

배포할 때 사용하는 명령(이 문서를 만드는 작업에서는 실행하지 않음):

```bash
az webapp config set -g laundry -n laundry-monitor --startup-file 'gunicorn -c gunicorn.conf.py app:app' -o none
az webapp deploy -g laundry -n laundry-monitor --type zip --src-path dist/laundry-web.zip
```

앱 설정과 읽기 권한을 먼저 설정해야 합니다. 메모리 기반 SSE 전달이므로 기존처럼 worker 1개 / instance 1개를 유지합니다. F1의 지속 실행 제약은 별개이며 이 기능 추가로 없어지지 않습니다. 웹이 정지해도 별도 Function과 Blob 경로의 저장은 웹 프로세스에 의존하지 않습니다. 배포 후 실제 SSE 전달 및 기록 복원을 확인해야 합니다.

## 문서

- [Table 인증/권한](https://learn.microsoft.com/en-us/azure/storage/tables/authorize-access-azure-active-directory)
- [IoT Hub 연결 문자열](https://learn.microsoft.com/en-us/cli/azure/iot/hub/connection-string)
- [Python App Service 실행 설정](https://learn.microsoft.com/en-us/azure/app-service/configure-language-python)

## SMS 알림 수신자 선택

숫자 4자리 공통 비밀번호 → 체크박스로 1~3명 선택 → 전송 버튼으로 수신자 저장 기능이 추가되었습니다.
확정 버튼은 SMS를 전송하지 않습니다. 기존 telemetry/기록 처리와 완료 hook은 분리되어 있습니다.
환경변수, 비공개 Blob 설정, Azure 재배포, 향후 완료 감지 연결은 [SMS_SETUP.md](SMS_SETUP.md)를 참고하세요.

### 로컬 알림 설정

`app.py`는 Azure 밖에서만 `.local-notifications.json`의 서버 설정을 자동으로 읽습니다. 이 파일은 Git/배포 ZIP에서 제외됩니다. 현재 로컬 테스트 비밀번호는 `0123`입니다. 실제 전화번호가 없어도 `SMS_ENABLED=false`일 때는 수신자 선택을 확인할 수 있습니다.

권장 실행: 프로젝트 폴더에서 `bash run_local.sh`. 알림 UI만 실행하려면 `.venv/bin/python app.py`를 사용하세요. 일반 `python`은 Anaconda를 가리킬 수 있으므로 가상환경을 활성화하지 않았다면 사용하지 마세요. 변경 적용 후 실행 중인 서버를 Ctrl+C로 종료하고 다시 실행해야 합니다.

## 실시간 완료 판정과 SMS 실험

현재 업로드 자료를 바탕으로 완료 감지기를 연결했습니다. 수신자 저장 후 코스 시작을 기록하면 서버가 계속 계산합니다. 실제 SMS 설정, 통신 테스트, 종료 시각 기록과 알려진 미탐은 [CYCLE_TESTING.md](CYCLE_TESTING.md)를 먼저 읽으세요.
