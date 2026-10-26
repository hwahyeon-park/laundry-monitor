"""Authenticated recipient selection and durable, at-most-once SMS attempts."""
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from uuid import uuid4

import requests
from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash


class SmsSendError(RuntimeError):
    """Safe diagnostic text: never includes credentials or full phone numbers."""


ENCOURAGEMENTS = (
    'The washing machine believes in you.',
    'Ahja ahja hwaiting!',
    'Sending you a tiny bit of good luck!',
    '세상이 거지같을땐 하이랜드 소를 생각해',
)

COMPLETION_HEADER = '[SEtakgi KEY] Laundry is finished!'

# Fixed, reviewed variants: each complete body fits one SMS segment.
SMS_VARIANTS = (
    dict(id='believing-cat', encouragement='The washing machine believes in you.', art=' /\\_/\\\n( o.o )\n > ^ <'),
    dict(id='hwaiting', encouragement='Ahja ahja hwaiting!', art='(ง •̀_•́)ง'),
    dict(id='lucky-rabbit', encouragement='Sending you a tiny bit of good luck!', art=' (\\_/)\n (o.o)\n / > <'),
    dict(id='highland-cow', encouragement='세상이 거지같을땐 하이랜드 소를 생각해', art='(^(oo)^)'),
    dict(id='little-cat', encouragement='', art=' /\\_/\\\n( o.o )\n > ^ <'),
    dict(id='heart-rabbit', encouragement='', art=' (\\_/)\n( •ㅅ• )\n /づ♡'),
    dict(id='heart-bear', encouragement='', art='ʕ•ᴥ•ʔ\n /つ♡'),
    dict(id='little-cow', encouragement='', art=' ^__^\n (oo)\\____\n (__)\\    )\\\n     ||--||'),
    dict(id='long-cat', encouragement='', art=' A___A\n| o.o |\n| > < |\n|     |\n|     |\n|     |\n U---U'),
    dict(id='little-duck', encouragement='', art="  __\n<(o )___\n ( ._> /\n  '---'"),
)

def completion_message(variant):
    return '\n'.join(part for part in (COMPLETION_HEADER, variant['encouragement'], variant['art']) if part)


SMS_LABELS = {'puresms': 'PureSMS', 'smsworks': 'The SMS Works', 'textbee': 'TextBee'}


def sms_config(env):
    """Resolve one explicit provider; never fall back to another after failure."""
    provider = env.get('SMS_PROVIDER', 'puresms').strip().lower()
    fields = {'puresms': ('PURESMS_API_KEY', 'PURESMS_SENDER'),
              'smsworks': ('SMSWORKS_JWT', 'SMSWORKS_SENDER'),
              'textbee': ('TEXTBEE_API_KEY', 'TEXTBEE_DEVICE_ID')}
    if provider not in fields:
        raise SmsSendError('설정 오류: SMS_PROVIDER는 puresms, smsworks 또는 textbee여야 합니다.')
    key_name, sender_name = fields[provider]
    key, sender = env.get(key_name, '').strip(), env.get(sender_name, '').strip()
    if not key or not sender:
        raise SmsSendError(f'설정 오류: {key_name} 또는 {sender_name}가 없습니다. API 요청은 보내지 않았습니다.')
    if provider == 'smsworks' and key.lower().startswith('jwt '):
        key = key[4:].strip()
    if not key:
        raise SmsSendError('설정 오류: SMSWORKS_JWT 토큰이 비어 있습니다.')
    if provider == 'smsworks' and (key.lower().startswith('bearer ') or any(c.isspace() for c in key)):
        raise SmsSendError('설정 오류: SMSWORKS_JWT에는 Bearer 없이 JWT 토큰만 입력하세요.')
    if provider == 'textbee':
        if any(c.isspace() for c in key):
            raise SmsSendError('설정 오류: TEXTBEE_API_KEY에는 접두사나 공백 없이 API key만 입력하세요.')
        if not re.fullmatch(r'[0-9a-fA-F]{24}', sender):
            raise SmsSendError('설정 오류: TEXTBEE_DEVICE_ID에는 대시보드의 24자리 기기 ID를 입력하세요.')
    return provider, key, sender


def sms_configured(env):
    try:
        sms_config(env)
        return True
    except SmsSendError:
        return False


def sms_error_detail(response, secrets_to_hide):
    from urllib.parse import quote
    try:
        data = response.json()
    except (ValueError, TypeError):
        return ''
    if not isinstance(data, dict):
        return ''
    selected = {k: data[k] for k in ('message', 'error', 'errors', 'detail', 'title', 'code') if k in data}
    text = json.dumps(selected, ensure_ascii=False)
    for secret in secrets_to_hide:
        if secret:
            for variant in (secret, quote(secret, safe=''), json.dumps(secret)[1:-1]):
                text = text.replace(variant, '[redacted]')
    text = re.sub(r'\+?\d[\d ()-]{7,}\d', '[phone redacted]', text)
    text = re.sub(r'[A-Za-z0-9_./+=-]{40,}', '[token redacted]', text)
    return text[:600] if selected else ''


class Conflict(Exception):
    pass


def empty_state():
    return {'current': None, 'cycles': {}}


class FileStore:
    """Local development only. Atomic replace + cross-process lock, no phone numbers."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def transact(self, change):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, open(str(self.path) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else empty_state()
            before = json.dumps(state, sort_keys=True)
            result = change(state)
            if before != json.dumps(state, sort_keys=True):
                temp = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
                try:
                    with open(temp, 'x', encoding='utf-8') as stream:
                        os.chmod(temp, 0o600)
                        json.dump(state, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, self.path)
                finally:
                    temp.unlink(missing_ok=True)
            return result


class BlobStore:
    """Private Blob + conditional writes preserve claims across restarts/requests."""
    def __init__(self, get_blob):
        self.get_blob = get_blob

    def transact(self, change):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError, ResourceModifiedError
        blob = self.get_blob()
        for _ in range(6):
            try:
                download = blob.download_blob()
                state = json.loads(download.readall())
                etag = download.properties.etag
            except ResourceNotFoundError:
                state, etag = empty_state(), None
            before = json.dumps(state, sort_keys=True)
            # change must be pure: it can run again after a competing write.
            result = change(state)
            if before == json.dumps(state, sort_keys=True):
                return result
            try:
                data = json.dumps(state)
                if etag is None:
                    blob.upload_blob(data, overwrite=False)
                else:
                    blob.upload_blob(data, overwrite=True, etag=etag,
                                     match_condition=MatchConditions.IfNotModified)
                return result
            except (ResourceExistsError, ResourceModifiedError):
                continue
        raise Conflict('설정이 동시에 변경되었습니다. 다시 불러온 후 시도하세요.')


def make_store(app, env):
    from functools import lru_cache
    cloud = bool(env.get('WEBSITE_HOSTNAME')) or app is None
    backend = env.get('NOTIFICATION_STORAGE', 'table' if cloud else 'file')
    if backend not in ('file', 'blob', 'table') or (cloud and backend == 'file'):
        raise ValueError('invalid notification storage backend')
    if backend in ('blob', 'table'):
        from azure.identity import DefaultAzureCredential
        account = env.get('NOTIFICATION_STORAGE_ACCOUNT', env.get('STORAGE_ACCOUNT', ''))
        if not re.fullmatch(r'[a-z0-9]{3,24}', account):
            raise ValueError('notification storage account required')
        if backend == 'table':
            from azure.data.tables import TableClient
            from table_state import TableStore
            table_name = env.get('NOTIFICATION_TABLE_NAME', 'LaundryState')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{2,62}', table_name):
                raise ValueError('invalid notification table name')
            @lru_cache(maxsize=1)
            def get_table():
                return TableClient(f'https://{account}.table.core.windows.net', table_name,
                    credential=DefaultAzureCredential(), connection_timeout=5, read_timeout=10,
                    retry_total=0)
            return TableStore(get_table, env.get('NOTIFICATION_PARTITION_KEY', env.get('DEVICE_ID', '')))
        # Explicit legacy mode is retained only for a controlled migration/rollback.
        from azure.storage.blob import BlobClient
        @lru_cache(maxsize=1)
        def get_blob():
            return BlobClient(
                account_url=f'https://{account}.blob.core.windows.net',
                container_name=env.get('NOTIFICATION_CONTAINER', 'laundry-notifications'),
                blob_name='recipient-state.json', credential=DefaultAzureCredential(),
                connection_timeout=5, read_timeout=10, retry_total=0)
        return BlobStore(get_blob)
    return FileStore(env.get('NOTIFICATION_STATE_FILE', str(Path(app.instance_path) / 'notifications.json')))


class NotificationService:
    def __init__(self, store, recipients, env, post=None):
        self.store, self.recipients, self.env = store, recipients, env
        self.post = post or requests.post

    @staticmethod
    def normalize_cycle(cycle):
        # Upgrade the previous single-recipient format without clearing send claims.
        if 'recipient_ids' not in cycle:
            old = cycle.pop('recipient', None)
            cycle['recipient_ids'] = [old] if old is not None else []
        return cycle

    @staticmethod
    def alert_settings(state):
        # Migrate the last selection once; an explicitly cancelled alert stays off.
        if 'alerts' not in state:
            c = state['cycles'].get(state['current'], {})
            ids = c.get('recipient_ids', [])
            state['alerts'] = dict(enabled=bool(ids and c.get('detection')
                and c.get('status') != 'cancelled'), recipient_ids=list(ids), points=[])
        return state['alerts']

    def current(self):
        cycle_id = uuid4().hex
        def change(state):
            if state['current'] is None:
                state['current'] = cycle_id
                state['cycles'][cycle_id] = {'recipient_ids': [], 'status': 'pending', 'revision': 0}
            current_id = state['current']
            return {'cycle_id': current_id, **self.normalize_cycle(state['cycles'][current_id]),
                    'alerts_enabled': state.get('alerts', {}).get('enabled', bool(state['cycles'][current_id].get('detection') and state['cycles'][current_id].get('recipient_ids') and state['cycles'][current_id]['status'] != 'cancelled'))}
        return self.store.transact(change)

    def select(self, cycle_id, recipient_ids, revision, *, detection=None):
        if (not isinstance(recipient_ids, list) or not 1 <= len(recipient_ids) <= 3
                or any(not isinstance(rid, str) or rid not in self.recipients for rid in recipient_ids)
                or len(set(recipient_ids)) != len(recipient_ids)):
            raise ValueError('수신자를 한 명 이상 선택하세요.')
        # Stable display/send order, regardless of checkbox click order.
        selected = [rid for rid in self.recipients if rid in recipient_ids]
        if self.env.get('SMS_ENABLED', 'false').lower() == 'true' and any(not self.recipients[rid]['phone'] for rid in selected):
            raise ValueError('selected phone not configured')
        next_id = uuid4().hex
        def change(state):
            if cycle_id != state['current'] or cycle_id not in state['cycles']:
                raise Conflict('현재 cycle이 변경되었습니다. 다시 불러오세요.')
            cycle = self.normalize_cycle(state['cycles'][cycle_id])
            if cycle['revision'] != revision:
                raise Conflict('수신자 또는 cycle 상태가 변경되었습니다. 다시 불러오세요.')
            selected_id = cycle_id
            # A fresh, explicit save can start the next cycle only after dispatch
            # has settled. Preserve old recipients, evidence and delivery claims.
            terminal = cycle['status'] in (
                'accepted', 'partial', 'unknown', 'skipped', 'disabled',
                'not_configured', 'missed', 'cancelled')
            if detection is not None and terminal:
                selected_id = next_id
                cycle = dict(recipient_ids=[], status='pending', revision=0)
                state['cycles'][selected_id] = cycle
                state['current'] = selected_id
            elif cycle['status'] != 'pending' or cycle.get('detection', {}).get('phase') in ('FINISHED', 'CANCELLED'):
                raise Conflict('수신자 또는 cycle 상태가 변경되었습니다. 다시 불러오세요.')
            cycle['recipient_ids'] = selected
            # Updating recipients during a run must not reset its timer/evidence.
            if detection is not None and not cycle.get('detection'):
                cycle['detection'] = detection
            if detection is not None:
                alerts = self.alert_settings(state)
                alerts.update(enabled=True, recipient_ids=list(selected), points=[], last_at=None)
            cycle['revision'] += 1
            return {'cycle_id': selected_id, **cycle}

        return self.store.transact(change)

    def begin_cycle(self, cycle_id):
        """Future detector must persist/reuse a unique cycle ID. Not an HTTP endpoint.

        Call at cycle creation BEFORE the user selects a recipient. Never copy a
        previous cycle's recipient. For the initial cycle use current()['cycle_id'].
        Repeated/late start events for known IDs never switch the current cycle.
        """
        if not isinstance(cycle_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', cycle_id):
            raise ValueError('invalid cycle id')
        def change(state):
            if cycle_id in state['cycles']:
                return False
            if state['current']:
                old = state['cycles'][state['current']]
                if old['status'] == 'pending':
                    raise Conflict('이전 cycle이 아직 완료되지 않았습니다.')
            state['cycles'][cycle_id] = {'recipient_ids': [], 'status': 'pending', 'revision': 0}
            state['current'] = cycle_id
            return True
        return self.store.transact(change)

    def validate_sms(self, recipient):
        if self.env.get('SMS_ENABLED', 'false').lower() != 'true':
            raise SmsSendError('설정 오류: SMS_ENABLED=true가 아닙니다. API 요청은 보내지 않았습니다.')
        if recipient not in self.recipients or not self.recipients[recipient]['phone']:
            raise ValueError('설정 오류: 해당 수신자 ID 또는 전화번호가 없습니다. API 요청은 보내지 않았습니다.')
        sms_config(self.env)

    def check_sms_auth(self):
        """Read-only provider authentication check; never sends a message."""
        provider, key, _ = sms_config(self.env)
        if provider == 'smsworks':
            url = 'https://api.thesmsworks.co.uk/v1/utils/test'
            headers = {'Authorization': 'JWT ' + key, 'Content-Type': 'application/json'}
        elif provider == 'textbee':
            url = 'https://api.textbee.dev/api/v1/gateway/stats'
            headers = {'x-api-key': key, 'Content-Type': 'application/json'}
        else:
            raise SmsSendError('인증 확인은 smsworks 또는 textbee에서만 지원합니다.')
        try:
            response = requests.get(url, headers=headers,
                timeout=(5, 15), allow_redirects=False)
        except requests.RequestException as error:
            raise SmsSendError(f'인증 확인 통신 오류 ({type(error).__name__}). SMS는 발송하지 않았습니다.') from None
        if not 200 <= response.status_code < 300:
            raise SmsSendError(f'{SMS_LABELS[provider]} 인증 확인 HTTP {response.status_code}. SMS는 발송하지 않았습니다.')

    def send_sms(self, recipient, test=False, *, encouragement=None, message=None):
        """Official REST request; no redirects or automatic retries."""
        self.validate_sms(recipient)
        provider, key, sender = sms_config(self.env)
        if test:
            content = '[TEST] Laundry Monitor SMS connection test.'
        elif message is not None:
            content = message
        elif encouragement is not None:
            content = COMPLETION_HEADER + '\n' + encouragement
        else:
            content = completion_message(secrets.choice(SMS_VARIANTS))
        phone = self.recipients[recipient]['phone']
        if provider == 'smsworks':
            url = 'https://api.thesmsworks.co.uk/v1/message/send'
            headers = {'Authorization': 'JWT ' + key, 'Content-Type': 'application/json'}
            payload = {'sender': sender, 'destination': phone.lstrip('+'), 'content': content}
        elif provider == 'textbee':
            url = 'https://api.textbee.dev/api/v1/gateway/send-sms'
            headers = {'x-api-key': key, 'Content-Type': 'application/json'}
            # One recipient per durable claim, using the explicitly selected phone.
            # Keep stored E.164 numbers intact; this gateway uses digits on send.
            payload = {'deviceId': sender, 'recipients': [phone.removeprefix('+')], 'message': content}
        else:
            url = 'https://connect-api.divergent.cloud/sms/send'
            headers = {'X-Api-Key': key}
            payload = {'sender': sender, 'recipient': phone, 'content': content}
        try:
            response = self.post(url, headers=headers, json=payload,
                                 timeout=(5, 15), allow_redirects=False)
        except requests.Timeout:
            raise SmsSendError('통신 시간 초과: 접수 여부가 불명확합니다. 재실행 전에 공급자 발송 기록을 확인하세요.') from None
        except requests.RequestException as error:
            raise SmsSendError(f'통신 오류 ({type(error).__name__}): API 응답을 확인하지 못했습니다. 재실행 전에 공급자 기록을 확인하세요.') from None
        if not 200 <= response.status_code < 300:
            detail = sms_error_detail(response, [key, sender] + [v for r in self.recipients.values() for v in (r['phone'], r['phone'].lstrip('+'))])
            hint = {400:'sender/번호/요청 필드 및 테스트 번호 등록 상태를 확인하세요.',
                    401:'API 인증 정보를 확인하세요.', 402:'SMS 크레딧 잔액을 확인하세요.', 403:'권한, sender 승인 및 Test Mode 제한을 확인하세요.',
                    429:'API 요청 제한 응답입니다. 자동 재시도하지 않았습니다.'}.get(response.status_code, '공급자 발송 기록을 확인하세요.')
            if provider == 'textbee':
                hint = {400: '이메일 인증, 기기 활성화 및 수신자 번호를 확인하세요.',
                        401: 'TextBee API key를 확인하세요.',
                        403: 'TextBee 계정 및 API 권한을 확인하세요.',
                        404: 'TEXTBEE_DEVICE_ID가 계정에 등록된 기기인지 확인하세요.',
                        429: 'TextBee 발송 한도 또는 요청 제한입니다. 자동 재시도하지 않았습니다.'}.get(response.status_code, 'TextBee 발송 기록을 확인하세요.')
            label = SMS_LABELS[provider]
            raise SmsSendError(f'{label} HTTP {response.status_code}: {hint}' + (f'\n서버 응답: {detail}' if detail else ''))
        # API acceptance is not a delivery receipt.

    def on_state_transition(self, cycle_id, previous_state, new_state):
        """Detector hook: caller explicitly invokes for RUNNING -> FINISHED.

        Durable claim precedes network I/O. This guarantees at-most-one attempt,
        per recipient, not exactly-once delivery. Crashes/timeouts are NOT retried.
        """
        if previous_state != 'RUNNING' or new_state != 'FINISHED':
            return 'ignored'
        if self.env.get('SMS_ENABLED', 'false').lower() != 'true':
            return 'disabled'
        if not sms_configured(self.env):
            return 'not_configured'
        # Choose outside the retryable storage transaction; persist with the claim.
        variant = secrets.choice(SMS_VARIANTS)
        def claim(state):
            cycle = state['cycles'].get(cycle_id)
            if cycle is None or cycle['status'] != 'pending':
                return None
            if state.get('alerts', {}).get('enabled') is False or cycle.get('notifications_cancelled'):
                cycle['status'] = 'disabled'
                return None
            self.normalize_cycle(cycle)
            if not cycle['recipient_ids']:

                cycle['status'] = 'skipped'
                return None
            cycle['status'] = 'claimed'
            cycle['revision'] += 1
            if 'sms_message' not in cycle:
                if 'encouragement' in cycle:
                    # Preserve an already chosen legacy message, without adding art.
                    cycle['sms_message'] = COMPLETION_HEADER + '\n' + cycle['encouragement']
                    cycle['sms_variant'] = 'legacy'
                else:
                    cycle['sms_message'] = completion_message(variant)
                    cycle['sms_variant'] = variant['id']
                    cycle['encouragement'] = variant['encouragement']
            return list(cycle['recipient_ids']), cycle['sms_message']
        claimed = self.store.transact(claim)
        if claimed is None:
            return 'skipped'
        recipient_ids, message = claimed
        outcomes = {}
        for recipient in recipient_ids:
            if self.store.transact(lambda state: state.get('alerts', {}).get('enabled') is False or state['cycles'][cycle_id].get('notifications_cancelled', False)):
                outcomes[recipient] = 'disabled'
                continue
            try:

                self.send_sms(recipient, message=message)
                outcomes[recipient] = 'accepted'
            except Exception:
                # One failed recipient must not prevent the other selected sends.
                outcomes[recipient] = 'unknown'
        statuses = set(outcomes.values())
        outcome = next(iter(statuses)) if len(statuses) == 1 else 'partial'
        def finish(state):
            state['cycles'][cycle_id]['status'] = outcome
            state['cycles'][cycle_id]['send_outcomes'] = outcomes
        self.store.transact(finish)
        return outcome


def install_notifications(app, env=None, store=None, post=None, clock=None):
    env = dict(os.environ if env is None else env)
    clock = clock or time.time
    api = Blueprint('notifications', __name__, url_prefix='/api/notifications')
    configured = False
    service = None
    password_hash = env.get('NOTIFICATION_PASSWORD_HASH', '')
    try:
        if len(env.get('NOTIFICATION_SECRET_KEY', '')) < 32 or not password_hash.startswith(('scrypt:', 'pbkdf2:')):
            raise ValueError('authentication configuration missing')
        rows = json.loads(env.get('NOTIFICATION_RECIPIENTS_JSON', '[]'))
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError('exactly three recipients required')
        recipients = {}
        for row in rows:
            rid, name, phone = row['id'], row['name'], row['phone']
            if (not isinstance(rid, str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', rid)
                    or not isinstance(name, str) or not 1 <= len(name.strip()) <= 60
                    or not isinstance(phone, str)
                    or (phone != '' and not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone))
                    
                    or rid in recipients):
                raise ValueError('invalid recipient configuration')
            recipients[rid] = {'name': name.strip(), 'phone': phone}
        if len({r['name'] for r in recipients.values()}) != 3:
            raise ValueError('recipient names must be distinct')
        service = NotificationService(store or make_store(app, env), recipients, env, post)
        app.secret_key = env['NOTIFICATION_SECRET_KEY']
        app.config.update(SESSION_COOKIE_NAME='laundry_notification_session',
                          SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                          SESSION_COOKIE_SECURE=bool(env.get('WEBSITE_HOSTNAME')) or
                          env.get('NOTIFICATION_COOKIE_SECURE', 'true').lower() != 'false',
                          SESSION_COOKIE_PATH='/api/notifications')
        configured = True
    except (ValueError, KeyError, TypeError):
        # Missing config disables only this feature, never the live dashboard.
        app.logger.warning('Notification settings missing or invalid; recipient selection disabled.')
    app.extensions['notifications'] = service
    auth_version = hashlib.sha256((password_hash + ':60-second-session').encode()).hexdigest()
    attempts, attempt_lock = deque(), threading.Lock()

    def authenticated():
        return session.get('notification_auth') == auth_version and session.get('until', 0) > clock()

    def status_payload():
        cycle = service.current()
        selected = [service.recipients.get(rid, {}).get('name', '등록되지 않은 수신자')
                    for rid in cycle['recipient_ids']]
        return {'cycle_id': cycle['cycle_id'], 'revision': cycle['revision'],
                'recipient_ids': cycle['recipient_ids'], 'recipient_names': selected,
                'status': cycle['status'], 'alerts_enabled': cycle['alerts_enabled'],
                'detection_phase': cycle.get('detection', {}).get('phase', 'NOT_STARTED'),
                'observed_end_at': cycle.get('detection', {}).get('observed_end_at')}

    @api.before_request
    def protect():
        if not configured:
            return jsonify(error='알림 설정이 아직 준비되지 않았습니다.'), 503
        if request.method == 'POST':
            request.max_content_length = 4096
            csrf = request.headers.get('X-CSRF-Token', '')
            expected = session.get('notification_csrf', '')
            if not expected or not secrets.compare_digest(csrf, expected):
                return jsonify(error='인증 상태를 다시 불러오세요.'), 403
            if not request.is_json:
                return jsonify(error='JSON 요청이 필요합니다.'), 415
            if request.endpoint != 'notifications.login' and not authenticated():
                return jsonify(error='비밀번호를 다시 입력하세요.'), 401

    @api.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @api.errorhandler(Conflict)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @api.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error='요청 값을 확인하세요.'), 400

    @api.errorhandler(Exception)
    def unavailable(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return jsonify(error='요청을 처리할 수 없습니다.'), error.code
        # Do not log raw exception strings from cloud/provider SDKs.
        app.logger.warning('Notification operation failed (%s).', type(error).__name__)
        return jsonify(error='알림 설정 저장소에 연결하지 못했습니다. 다시 시도하세요.'), 503

    @api.get('/session')
    def get_session():
        if 'notification_csrf' not in session:
            session['notification_csrf'] = secrets.token_urlsafe(32)
        data = {'authenticated': authenticated(), 'csrf_token': session['notification_csrf']}
        if data['authenticated']:
            data['recipients'] = [{'id': rid, 'name': row['name']} for rid, row in service.recipients.items()]
            data['cycle'] = status_payload()
            data['expires_in'] = max(0, int(session['until'] - clock()))
        return jsonify(data)

    @api.post('/login')
    def login():
        # Existing deployment uses exactly one worker/instance: bounded shared limit.
        with attempt_lock:
            now = clock()
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return jsonify(error='시도가 너무 많습니다. 1분 후 다시 시도하세요.'), 429, {'Retry-After': '60'}
            attempts.append(now)
        data = request.get_json()
        password = data.get('password') if isinstance(data, dict) else None
        if not isinstance(password, str) or not re.fullmatch(r'[0-9]{4}', password) or not check_password_hash(password_hash, password):
            return jsonify(error='비밀번호가 맞지 않습니다.'), 401
        session.clear()
        session.update(notification_auth=auth_version, until=clock() + 60,
                       notification_csrf=secrets.token_urlsafe(32))
        return jsonify(authenticated=True, csrf_token=session['notification_csrf'])

    @api.post('/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @api.post('/recipient')
    def select_recipient():
        data = request.get_json()
        if not isinstance(data, dict) or set(data) != {'recipient_ids', 'cycle_id', 'revision'}:
            raise ValueError('invalid fields')
        if not isinstance(data['cycle_id'], str) or type(data['revision']) is not int:
            raise ValueError('invalid cycle')
        app.extensions['cycle_detector'].select_recipients(
            data['cycle_id'], data['recipient_ids'], data['revision'])
        return jsonify(cycle=status_payload())

    @api.post('/start')
    def start_cycle():
        data = request.get_json()
        if not isinstance(data, dict) or set(data) != {'cycle_id', 'revision', 'mode'} or type(data['revision']) is not int:
            raise ValueError('invalid start')
        app.extensions['cycle_detector'].arm(data['cycle_id'], data['revision'], data['mode'])
        return jsonify(cycle=status_payload())

    @api.post('/prepare')
    def prepare_cycle():
        data = request.get_json()
        if not isinstance(data, dict) or not isinstance(data.get('cycle_id'), str):
            raise ValueError('invalid cycle')
        app.extensions['cycle_detector'].prepare(data['cycle_id'])
        return jsonify(cycle=status_payload())

    @api.post('/cancel')
    def cancel_cycle():
        data = request.get_json()
        if not isinstance(data, dict) or not isinstance(data.get('cycle_id'), str):
            raise ValueError('invalid cycle')
        app.extensions['cycle_detector'].cancel(data['cycle_id'])
        return jsonify(cycle=status_payload())

    @api.post('/observed-end')
    def observed_end():
        data = request.get_json()
        if not isinstance(data, dict) or not isinstance(data.get('cycle_id'), str):
            raise ValueError('invalid cycle')
        app.extensions['cycle_detector'].observe_end(data['cycle_id'])
        return jsonify(cycle=status_payload())

    @api.get('/cycles')
    def cycle_records():
        if not authenticated():
            return jsonify(error='비밀번호를 다시 입력하세요.'), 401
        cycles = service.store.transact(lambda state: dict(state['cycles'].items()))
        return jsonify(cycles=cycles)

    app.register_blueprint(api)
    return service
