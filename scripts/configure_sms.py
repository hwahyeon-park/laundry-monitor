"""Configure local SMS provider without printing API keys or putting them in shell history."""
import getpass
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values, set_key

path = Path(__file__).resolve().parents[1] / '.env'
if not path.exists():
    raise SystemExit('로컬 알림 설정 파일이 필요합니다.')
settings = dict(dotenv_values(path, interpolate=False))
provider = input('SMS 업체 [textbee / smsworks / puresms, 기본 textbee]: ').strip().lower() or 'textbee'
fields = {'textbee': ('TEXTBEE_API_KEY', 'TEXTBEE_DEVICE_ID'),
          'smsworks': ('SMSWORKS_JWT', 'SMSWORKS_SENDER'),
          'puresms': ('PURESMS_API_KEY', 'PURESMS_SENDER')}
if provider not in fields:
    raise SystemExit('textbee, smsworks 또는 puresms를 입력하세요. 저장하지 않았습니다.')
key_name, sender_name = fields[provider]
key = getpass.getpass(f'{key_name} (빈 입력은 기존 값 유지): ').strip()
if key:
    settings[key_name] = key
default_sender = settings.get(sender_name) or {'textbee': '', 'smsworks': 'SEtakgi', 'puresms': 'ConnectTest'}[provider]
label = 'TextBee Device ID' if provider == 'textbee' else 'Sender'
sender = input(f'{label} [{default_sender}]: ').strip() or default_sender
settings[sender_name] = sender
settings['SMS_PROVIDER'] = provider
from notifications import sms_config, SmsSendError
try:
    sms_config(settings)
except SmsSendError as error:
    raise SystemExit(str(error))
rows = json.loads(settings['NOTIFICATION_RECIPIENTS_JSON'])
for row in rows:
    phone = getpass.getpass(f"{row['name']} 전화번호 (+44…, 빈 입력은 기존 값 유지): ").strip()
    if phone:
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone):
            raise SystemExit('번호 형식이 올바르지 않습니다. 저장하지 않았습니다.')
        row['phone'] = phone
settings['NOTIFICATION_RECIPIENTS_JSON'] = json.dumps(rows)
if not settings.get(key_name) or not any(row['phone'] for row in rows):
    raise SystemExit('API key와 최소 한 명의 번호가 필요합니다. 저장하지 않았습니다.')
settings['SMS_ENABLED'] = 'true'
for name in ['SMS_PROVIDER', key_name, sender_name, 'NOTIFICATION_RECIPIENTS_JSON', 'SMS_ENABLED']:
    set_key(path, name, settings[name], quote_mode='always')
path.chmod(0o600)
print('서버 전용 SMS 설정을 저장했습니다. 서버를 재시작하세요. 지금 문자를 발송하지는 않았습니다.')
