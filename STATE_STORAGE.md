# 저장소 메모

원본은 Blob에, 자주 읽거나 바뀌는 값은 Table에 저장한다.

| 저장소 | 내용 |
|---|---|
| Blob `laundry-raw` | 센서 원본 AVRO 파일 |
| Table `LaundryTelemetry` | 배치별 평균 가속도, RMS, 온도·기압 |
| Table `LaundryState` | 수신자 선택, 현재 감지 상태, 세탁별 발송 기록 |

API 키, 실제 전화번호와 인증 정보는 서버 환경변수에 둔다.

## LaundryState 구조

`PartitionKey`는 기기 ID다. `RowKey=settings`에 현재 cycle과 알림 설정을, `RowKey=cycle|<id>`에 각 세탁 기록을 저장한다. 처리 중인 cycle ID도 settings에 보관하므로 매 배치마다 전체 기록을 읽지는 않는다.

설정을 바꾸거나 감지 결과를 저장할 때 settings와 변경된 cycle을 같은 트랜잭션으로 쓴다. ETag를 확인해 동시에 들어온 수정이 서로 덮어쓰지 않게 한다. 일관된 기록을 읽어야 할 때는 조회 전후의 settings ETag도 비교한다.

SMS 요청 전에 발송 시도를 먼저 기록한다. 저장 결과가 불명확하면 그 시도에서는 보내지 않고, 이미 발송 시도가 기록된 수신자는 재시작 후에도 다시 보내지 않는다. 중복을 피하는 대신 서버 중단 시 문자가 빠질 수 있다.

## 설정

웹 앱과 Function에 같은 값을 넣는다.

```dotenv
NOTIFICATION_STORAGE=table
NOTIFICATION_STORAGE_ACCOUNT=YOUR_STORAGE_ACCOUNT
NOTIFICATION_TABLE_NAME=LaundryState
NOTIFICATION_PARTITION_KEY=YOUR_DEVICE_ID
```

partition을 생략하면 `DEVICE_ID`를 사용한다. 두 앱의 Managed Identity에는 `LaundryState` 테이블 범위의 **Storage Table Data Contributor** 권한이 필요하다. 원본과 그래프 기록을 읽는 권한은 별도다.

로컬 기본값은 `instance/notifications.json`이다. 운영 데이터를 일부러 확인하는 경우가 아니라면 로컬에 운영 Table 설정을 복사하지 않는다. `NOTIFICATION_CONTAINER`는 예전 Blob 방식에서만 사용한다.

초기 상태가 없거나 이전 작업이 끝나지 않은 테이블은 오류로 처리한다. 빈 상태로 자동 초기화하지 않는다. 현재 초기화 도구는 기존 Blob 상태를 가져오는 방식이므로 새 설치에도 유효한 초기 상태가 필요하다.

## Blob에서 Table로 옮길 때

이미 이전한 운영 환경에서 다시 실행하는 절차는 아니다.

1. `LaundryState`를 만들고 웹 앱과 Function에 테이블 권한을 부여한다. 이전을 실행하는 계정에도 작업 중에만 권한이 필요하다.
2. 두 앱을 `NOTIFICATION_STORAGE=blob`으로 유지한 채 Table 지원 코드를 먼저 배포한다.
3. 웹 앱과 Function을 모두 중지한다. 원본 Blob 라우팅은 계속되며, 처리 대기 이벤트는 IoT Hub 보존 기간 안에서 다시 읽을 수 있다.
4. 새 백업 경로를 지정해 이전 도구를 실행한다.

```sh
.venv/bin/python scripts/migrate_notification_state.py \
  --account YOUR_STORAGE_ACCOUNT --partition YOUR_DEVICE_ID \
  --resource-group YOUR_RESOURCE_GROUP --web YOUR_WEB_APP --function YOUR_FUNCTION_APP \
  --backup .history-backups/recipient-state-before-table.private.json --apply
```

도구는 두 앱의 중지 여부를 확인하고, 접근 권한 0600으로 원본을 백업한다. Blob ETag와 복사 결과를 비교한 뒤 Table 상태를 사용 가능으로 바꾼다. 완료된 대상은 덮어쓰지 않는다. 중간에 실패했다면 원본 ETag가 같을 때 새 백업 경로로 이어서 실행할 수 있다.

5. 두 앱을 위의 Table 설정으로 바꾸고 다시 시작한다. SMS와 수신자 설정은 유지한다.
6. 이전 전후 기록, 웹 상태, 새 데이터 처리를 확인한다. 작업 계정에 임시로 준 권한은 회수한다.
7. 예전 상태 Blob은 복구용 사본으로 남기되 더 이상 운영에 사용하지 않는다.

## 되돌릴 때

Table을 사용하기 전이라면 원래 Blob 상태를 계속 사용할 수 있다. Table에 새 변경이 생긴 뒤에는 예전 Blob으로 설정만 돌리면 안 된다.

두 앱을 중지하고 `TableStore.snapshot()`으로 현재 상태를 읽어 두 저장소를 백업한다. 현재 상태를 ETag 조건과 함께 Blob에 복사하고 일치 여부를 확인한 뒤 두 앱을 `blob`으로 바꾼다. `claimed`, `accepted`, `unknown` 발송 기록을 지우면 중복 발송 위험이 있다.

## 이전 기록

2026-10-22에 기존 cycle 7건과 수신자 설정을 옮기고 전체 상태가 같은지 확인했다. 두 앱의 Table 설정과 웹 기록 조회를 확인한 뒤 임시 작업 권한을 회수했다. 예전 Blob은 백업으로 남아 있다.

당시 Python 테스트 147개와 Function 테스트 33개가 통과했다. 별도 임시 partition에서는 한글 저장·조회와 동시 갱신 충돌도 확인했다. 이전 작업 중에는 새 센서 데이터가 없었고 실제 SMS는 보내지 않았다.

## 크기 제한

같은 기기의 수정은 settings 행을 공유하므로 순서대로 처리된다. Table 트랜잭션은 최대 100개 작업과 4 MiB까지이며, 코드는 더 작은 제한에서 큰 쓰기를 거절한다. 문자열 속성은 UTF-16 기준 64 KiB를 확인한다. 오래된 기록의 보관 기간과 다운로드 페이지 나누기는 아직 추가하지 않았다.
