"""Configure local PureSMS without printing API keys or putting them in shell history."""
import getpass
import json
import os
from pathlib import Path
import re
from dotenv import dotenv_values, set_key

path = Path(__file__).resolve().parents[1] / '.env'
if not path.exists():
    raise SystemExit('로컬 알림 설정 파일이 필요합니다.')
settings = dict(dotenv_values(path, interpolate=False))
key = getpass.getpass('PureSMS API key (빈 입력은 기존 값 유지): ').strip()
if key:
    settings['PURESMS_API_KEY'] = key
sender = input('Sender [ConnectTest]: ').strip() or 'ConnectTest'
settings['PURESMS_SENDER'] = sender
rows = json.loads(settings['NOTIFICATION_RECIPIENTS_JSON'])
for row in rows:
    phone = getpass.getpass(f"{row['name']} 전화번호 (+44…, 빈 입력은 기존 값 유지): ").strip()
    if phone:
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone):
            raise SystemExit('번호 형식이 올바르지 않습니다. 저장하지 않았습니다.')
        row['phone'] = phone
settings['NOTIFICATION_RECIPIENTS_JSON'] = json.dumps(rows)
if not settings.get('PURESMS_API_KEY') or not any(row['phone'] for row in rows):
    raise SystemExit('API key와 최소 한 명의 번호가 필요합니다. 저장하지 않았습니다.')
settings['SMS_ENABLED'] = 'true'
for name in ['PURESMS_API_KEY', 'PURESMS_SENDER', 'NOTIFICATION_RECIPIENTS_JSON', 'SMS_ENABLED']:
    set_key(path, name, settings[name], quote_mode='always')
path.chmod(0o600)
print('서버 전용 SMS 설정을 저장했습니다. 서버를 재시작하세요. 지금 문자를 발송하지는 않았습니다.')
