"""Explicit operator-only real SMS test. No automatic execution on import/startup."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
parser=argparse.ArgumentParser(description='실제 테스트 SMS 한 통을 발송합니다.')
parser.add_argument('--recipient',required=True,help='등록된 수신자 ID, 예: wally')
parser.add_argument('--check',action='store_true',help='설정만 확인; API를 호출하지 않음')
parser.add_argument('--confirm-send',action='store_true',help='실제 문자 발송')
args=parser.parse_args()
if not args.confirm_send and not args.check: parser.error('실제 발송하려면 --confirm-send를 지정하세요.')
from notifications import SmsSendError
from app import notifications
if notifications is None: raise SystemExit('서버 알림 설정이 필요합니다.')
try:
    notifications.validate_sms(args.recipient)
    if args.check:
        print('로컬 설정 확인 완료. API key/sender/수신자 번호가 있습니다. 유효성·승인 상태는 API에서 확인해야 합니다.')
        print('Sender가 ConnectTest인지:', notifications.env['PURESMS_SENDER'] == 'ConnectTest')
        raise SystemExit(0)
    notifications.send_sms(args.recipient,test=True)
except (SmsSendError, ValueError) as error:
    raise SystemExit(str(error))
except Exception as error:
    raise SystemExit(f'예상하지 못한 오류 ({type(error).__name__}). 자동 재시도하지 않았습니다.')
print('테스트 SMS 요청이 서비스에 접수되었습니다. 휴대폰에서 실제 수신을 확인하세요.')
