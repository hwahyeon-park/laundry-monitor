# SMS 설정

현재는 TextBee를 사용한다. 등록된 Android 휴대폰이 실제 문자를 보내므로 휴대폰 전원, 인터넷 연결, SIM의 문자 발송 설정이 필요하다.

## 로컬 설정

`.env.example`을 참고해 `.env`를 만든다. 이미 있다면 그대로 사용한다.

| 환경변수 | 넣을 값 |
|---|---|
| `NOTIFICATION_SECRET_KEY` | 세션 서명 키 |
| `NOTIFICATION_PASSWORD_HASH` | 공통 4자리 비밀번호의 해시 |
| `NOTIFICATION_RECIPIENTS_JSON` | 수신자 3명의 `id`, `name`, `phone` 배열 |
| `NOTIFICATION_COOKIE_SECURE` | 로컬 HTTP에서만 `false` |
| `SMS_PROVIDER` | `textbee` |
| `TEXTBEE_API_KEY` | TextBee API 키 |
| `TEXTBEE_DEVICE_ID` | TextBee에 등록한 기기 ID |
| `SMS_ENABLED` | 발송하지 않을 때 `false`, 발송할 때 `true` |

비밀번호 해시와 서명 키는 아래 명령으로 만든다. 출력된 값은 `.env`에 넣고 공개하지 않는다.

```sh
.venv/bin/python scripts/notification_secrets.py
```

수신자는 다음 형식으로 입력한다. 번호는 예시이며 실제 번호로 바꿔야 한다. 기존 수신자의 이름이나 번호를 수정할 때도 `id`는 유지한다.

```json
[{"id":"wally","name":"Wally","phone":"+447700900001"},{"id":"flatmate-a","name":"Flatmate A","phone":"+447700900002"},{"id":"flatmate-b","name":"Flatmate B","phone":"+447700900003"}]
```

API 키와 번호를 대화형으로 입력하려면:

```sh
.venv/bin/python scripts/configure_sms.py
```

이 도구는 기존 `.env`의 알림 설정을 읽어 SMS 설정을 수정하고 `SMS_ENABLED=true`로 저장한다. 명령 자체는 문자를 보내지 않는다. 설정 후 실행 중인 서버를 다시 시작한다.

번호는 `+44…`처럼 국가 코드를 포함해 저장한다. 이 프로젝트에서는 휴대폰에서 확인한 동작에 맞춰 TextBee로 보낼 때만 맨 앞의 `+`를 뺀다. 전화번호와 API 키는 브라우저에 전달하지 않는다.

## 테스트

`wally`는 `.env`에 등록된 수신자 ID다. 다른 수신자를 확인하려면 해당 ID로 바꾼다.

```sh
# 설정 확인만 한다. API를 호출하지 않는다. SMS_ENABLED=true가 필요하다.
.venv/bin/python scripts/test_sms.py --recipient wally --check

# 인증 API만 호출한다. 문자는 보내지 않는다.
.venv/bin/python scripts/test_sms.py --recipient wally --check-auth

# 실제 테스트 문자 한 통을 보낸다.
.venv/bin/python scripts/test_sms.py --recipient wally --confirm-send
```

인증 성공은 휴대폰이나 SIM의 발송 상태까지 확인한 결과는 아니다. `accepted`도 API가 요청을 받았다는 뜻이므로, 실제 수신은 휴대폰에서 확인한다. 결과가 불명확하면 다시 보내기 전에 TextBee 발송 기록을 확인한다.

## Azure에 적용

로컬 `.env`는 배포되지 않는다. 웹 앱과 완료 감지 Function의 환경변수에 SMS 업체, API 키, 기기 ID와 수신자 설정을 각각 넣어야 한다. 비밀번호 해시와 세션 서명 키도 웹 앱에 설정한다.

알림 상태는 두 앱이 같은 `LaundryState` 테이블을 사용한다. 저장소 설정과 권한은 [STATE_STORAGE.md](STATE_STORAGE.md)를 참고한다. 기존 IoT Hub와 기록 조회 설정은 유지하고, 웹 앱의 HTTPS Only를 켠다.

웹은 README의 [배포 명령](README.md#deploy)으로, Function은 별도 프로젝트에서 배포한다. 웹 배포만으로 Function 코드나 SMS 환경변수가 바뀌지는 않는다.

## 사용 방법

비밀번호를 입력하고 수신자를 저장하면 감지가 켜진다. 설정 화면은 60초 뒤에 잠기지만 저장한 수신자와 알림은 유지된다. 수신자를 저장하는 것만으로 문자가 발송되지는 않는다.

다음 빨래도 같은 수신자에게 알린다. 받지 않으려면 **알림 끄기**를 누른다. 이미 TextBee에 접수된 문자는 취소할 수 없다.

완료 문자는 준비된 10종 중 하나를 골라 보낸다. 같은 세탁의 수신자들은 같은 문구를 받는다. 발송이 불명확하거나 실패했을 때 자동으로 다시 보내지는 않는다.

## 이전 SMS 업체

기존 연동 코드는 남아 있다. 사용할 업체를 `SMS_PROVIDER`로 지정하며 실패했다고 다른 업체로 바꾸어 보내지는 않는다.

- The SMS Works: `smsworks`, `SMSWORKS_JWT`, `SMSWORKS_SENDER`. 토큰은 Bearer 접두사 없이 입력한다.
- PureSMS: `puresms`, `PURESMS_API_KEY`, `PURESMS_SENDER`.
