"""Explicit operator-only real SMS test. No automatic execution on import/startup."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
parser=argparse.ArgumentParser(description='실제 테스트 SMS 한 통을 발송합니다.')
parser.add_argument('--recipient',required=True,help='등록된 수신자 ID, 예: wally')
mode=parser.add_mutually_exclusive_group(required=True)
mode.add_argument('--check',action='store_true',help='설정만 확인; API를 호출하지 않음')
mode.add_argument('--check-auth',action='store_true',help='TextBee / The SMS Works 인증 API만 확인; 문자 발송 없음')
mode.add_argument('--confirm-send',action='store_true',help='실제 문자 발송')
args=parser.parse_args()
from notifications import SmsSendError, sms_config, SMS_LABELS
from app import notifications
if notifications is None: raise SystemExit('서버 알림 설정이 필요합니다.')
try:
    if not args.check_auth:
        notifications.validate_sms(args.recipient)
    provider, _, _ = sms_config(notifications.env)
    print('선택한 SMS 업체:', SMS_LABELS[provider])
    if args.check_auth:
        notifications.check_sms_auth()
        print(f'{SMS_LABELS[provider]} 인증 성공. 기기 연결 및 실제 수신은 별도 확인이 필요합니다. SMS는 발송하지 않았습니다.')
        raise SystemExit(0)
    if args.check:
        print('로컬 설정 확인 완료. API 인증 정보/발신 설정/수신자 번호가 있습니다. 유효성·승인 상태는 API에서 확인해야 합니다.')
        print('API 인증/실제 수신은 아직 확인하지 않았습니다. SMS는 발송하지 않았습니다.')
        raise SystemExit(0)
    notifications.send_sms(args.recipient,test=True)
except (SmsSendError, ValueError) as error:
    raise SystemExit(str(error))
except Exception as error:
    raise SystemExit(f'예상하지 못한 오류 ({type(error).__name__}). 자동 재시도하지 않았습니다.')
print('테스트 SMS 요청이 서비스에 접수되었습니다. 휴대폰에서 실제 수신을 확인하세요.')
