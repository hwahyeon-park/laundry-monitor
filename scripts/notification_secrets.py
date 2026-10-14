"""Generate settings locally. Password input is hidden; output stays with operator."""
import getpass
import secrets
import re
from werkzeug.security import generate_password_hash

password = getpass.getpass('공통 비밀번호 (숫자 4자리): ')
if not re.fullmatch(r'[0-9]{4}', password):
    raise SystemExit('숫자 4자리를 입력하세요. (앞자리 0도 가능)')
if password != getpass.getpass('다시 입력: '):
    raise SystemExit('비밀번호가 일치하지 않습니다.')
print('NOTIFICATION_SECRET_KEY=' + secrets.token_urlsafe(48))
print('NOTIFICATION_PASSWORD_HASH=' + generate_password_hash(password))
