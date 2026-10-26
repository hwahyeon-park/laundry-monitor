import contextlib
import io
from pathlib import Path
import runpy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests
from notifications import SMS_VARIANTS, completion_message, NotificationService, FileStore, SmsSendError, sms_config, sms_configured
from cycle_detector import CycleDetector
from test_cycle_detector import BASE, record
from datetime import timedelta


class SmsWorksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(SMS_ENABLED='true', SMS_PROVIDER='smsworks',
                        SMSWORKS_JWT='fake.jwt.token', SMSWORKS_SENDER='SEtakgi')
        self.post = Mock(return_value=SimpleNamespace(status_code=201))
        self.service = NotificationService(FileStore(Path(self.tmp.name)/'state.json'),
            {'wally': {'name':'Test', 'phone':'+447700900001'}}, self.env, self.post)

    def test_official_request_and_distinct_test_message(self):
        self.service.send_sms('wally', test=True)
        self.post.assert_called_once_with('https://api.thesmsworks.co.uk/v1/message/send',
            headers={'Authorization':'JWT fake.jwt.token','Content-Type':'application/json'},
            json={'sender':'SEtakgi','destination':'447700900001',
                  'content':'[TEST] Laundry Monitor SMS connection test.'},
            timeout=(5,15),allow_redirects=False)
        self.post.reset_mock()
        self.service.send_sms('wally')
        self.assertIn(self.post.call_args.kwargs['json']['content'], [completion_message(v) for v in SMS_VARIANTS])

    def test_wrong_provider_missing_jwt_and_bearer_fail_without_fallback(self):
        self.env.update(PURESMS_API_KEY='old-key', PURESMS_SENDER='Old')
        for fields in [dict(SMS_PROVIDER='typo'), dict(SMSWORKS_JWT=''),
                       dict(SMSWORKS_JWT='Bearer fake.jwt.token')]:
            with patch.dict(self.env, fields):
                self.assertFalse(sms_configured(self.env))
                with self.assertRaises(SmsSendError): self.service.send_sms('wally')
        self.post.assert_not_called()

    def test_enabled_and_recipient_checks_still_apply(self):
        with patch.dict(self.env, SMS_ENABLED='false'):
            with self.assertRaises(SmsSendError): self.service.send_sms('wally')
        with self.assertRaises(ValueError): self.service.send_sms('missing')
        self.post.assert_not_called()

    def test_provider_error_redacts_jwt_and_normalized_phone(self):
        self.post.return_value = SimpleNamespace(status_code=401, json=lambda:{
            'message':'Invalid fake.jwt.token 447700900001 +447700900001'})
        with self.assertRaises(SmsSendError) as exc: self.service.send_sms('wally')
        self.assertIn('The SMS Works HTTP 401',str(exc.exception))
        self.assertNotIn('fake.jwt.token',str(exc.exception))
        self.assertNotIn('447700900001',str(exc.exception))
        self.post.assert_called_once()

    def test_timeout_never_retries_or_falls_back(self):
        self.post.side_effect = requests.Timeout('private response')
        with self.assertRaises(SmsSendError): self.service.send_sms('wally')
        self.post.assert_called_once()

    def test_function_save_to_completion_uses_smsworks_once(self):
        now = BASE
        detector = CycleDetector(self.service, lambda:now, processor='function')
        c = self.service.current()
        detector.select_recipients(c['cycle_id'], ['wally'], c['revision'])
        self.assertTrue(detector.public_status()['sms_ready'])
        self.post.assert_not_called()
        for seconds in range(0,805,5):
            now = BASE + timedelta(seconds=seconds)
            detector.process(record(now,.02 if seconds <=600 else .0065))
        detector.recover_finished()
        self.post.assert_called_once()
        self.assertEqual(self.service.current()['status'],'accepted')
        self.assertIn('destination', self.post.call_args.kwargs['json'])

    def run_cli(self, args):
        output = io.StringIO()
        with patch.dict('sys.modules',app=SimpleNamespace(notifications=self.service)), \
             patch('sys.argv',['test_sms.py',*args]),contextlib.redirect_stdout(output):
            try: runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/test_sms.py'),run_name='__main__')
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
        return output.getvalue()

    def test_cli_check_never_calls_provider_or_mutates_cycles(self):
        before = self.service.current()
        output = self.run_cli(['--recipient','wally','--check'])
        self.assertIn('The SMS Works',output)
        self.assertNotIn('fake.jwt.token',output)
        self.post.assert_not_called()
        self.assertEqual(before,self.service.current())

    def test_cli_confirm_send_attempts_one_test_without_mutating_cycles(self):
        before = self.service.current()
        self.run_cli(['--recipient','wally','--confirm-send'])
        self.post.assert_called_once()
        self.assertIn('[TEST]',self.post.call_args.kwargs['json']['content'])
        self.assertEqual(before,self.service.current())

    def test_prefixed_env_token_is_normalized_without_duplicate_prefix(self):
        self.env['SMSWORKS_JWT'] = 'JWT fake.jwt.token'
        self.service.send_sms('wally',test=True)
        self.assertEqual(self.post.call_args.kwargs['headers']['Authorization'],'JWT fake.jwt.token')

    def test_read_only_auth_command_uses_jwt_scheme_without_send(self):
        before = self.service.current()
        with patch('notifications.requests.get', return_value=SimpleNamespace(status_code=200)) as get:
            output = self.run_cli(['--recipient','wally','--check-auth'])
        get.assert_called_once_with('https://api.thesmsworks.co.uk/v1/utils/test',
            headers={'Authorization':'JWT fake.jwt.token','Content-Type':'application/json'},
            timeout=(5,15),allow_redirects=False)
        self.assertIn('인증 성공', output)
        self.post.assert_not_called()
        self.assertEqual(before,self.service.current())

    def test_read_only_auth_rejection_never_sends_or_retries(self):
        with patch('notifications.requests.get', return_value=SimpleNamespace(status_code=401)) as get:
            with self.assertRaises(SmsSendError): self.service.check_sms_auth()
        get.assert_called_once()
        self.post.assert_not_called()
