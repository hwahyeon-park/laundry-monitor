import contextlib
import io
from datetime import timedelta
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from notifications import SMS_VARIANTS, completion_message, NotificationService, FileStore, SmsSendError, sms_configured
from cycle_detector import CycleDetector
from test_cycle_detector import BASE, record


class TextBeeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(SMS_ENABLED='true', SMS_PROVIDER='textbee',
                        TEXTBEE_API_KEY='test-only-key', TEXTBEE_DEVICE_ID='a' * 24)
        self.post = Mock(return_value=SimpleNamespace(status_code=200))
        self.service = NotificationService(FileStore(Path(self.tmp.name) / 'state.json'),
            {'wally': {'name': 'Test', 'phone': '+447700900001'},
             'other': {'name': 'Other', 'phone': '+447700900002'}}, self.env, self.post)

    def test_request_uses_explicit_device_and_registered_recipient(self):
        self.service.send_sms('wally', test=True)
        self.post.assert_called_once_with('https://api.textbee.dev/api/v1/gateway/send-sms',
            headers={'x-api-key': 'test-only-key', 'Content-Type': 'application/json'},
            json={'deviceId': 'a' * 24, 'recipients': ['447700900001'],
                  'message': '[TEST] Laundry Monitor SMS connection test.'},
            timeout=(5, 15), allow_redirects=False)
        self.post.reset_mock()
        self.service.send_sms('other')
        self.assertEqual(self.post.call_args.kwargs['json']['recipients'], ['447700900002'])
        self.assertEqual(self.service.recipients['wally']['phone'], '+447700900001')
        self.assertEqual(self.service.recipients['other']['phone'], '+447700900002')
        self.assertIn(self.post.call_args.kwargs['json']['message'], [completion_message(v) for v in SMS_VARIANTS])

    def test_bad_config_cannot_fall_back_to_old_provider(self):
        self.env.update(PURESMS_API_KEY='old-key', PURESMS_SENDER='Old')
        for fields in ({'TEXTBEE_API_KEY': ''}, {'TEXTBEE_DEVICE_ID': ''},
                       {'TEXTBEE_DEVICE_ID': '../wrong'}, {'TEXTBEE_API_KEY': 'Bearer token'}):
            with self.subTest(fields=fields), patch.dict(self.env, fields):
                self.assertFalse(sms_configured(self.env))
                with self.assertRaises(SmsSendError):
                    self.service.send_sms('wally')
        with patch.dict(self.env, SMS_ENABLED='false'):
            with self.assertRaises(SmsSendError):
                self.service.send_sms('wally')
        with self.assertRaises(ValueError):
            self.service.send_sms('missing')
        self.post.assert_not_called()

    def test_errors_redact_secrets_and_do_not_retry(self):
        for status in (400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.return_value = SimpleNamespace(status_code=status, json=lambda: {
                    'message': 'test-only-key ' + 'a' * 24 + ' +447700900001 447700900002'})
                with self.assertRaises(SmsSendError) as error:
                    self.service.send_sms('wally')
                message = str(error.exception)
                self.assertIn(f'TextBee HTTP {status}', message)
                for secret in ('test-only-key', 'a' * 24, '447700900001', '447700900002'):
                    self.assertNotIn(secret, message)
                self.post.assert_called_once()
        self.post.reset_mock()
        self.post.side_effect = requests.Timeout('private network diagnostic')
        with self.assertRaises(SmsSendError) as error:
            self.service.send_sms('wally')
        self.assertNotIn('private network diagnostic', str(error.exception))
        self.post.assert_called_once()

    def run_cli(self, mode):
        output = io.StringIO()
        with patch.dict('sys.modules', app=SimpleNamespace(notifications=self.service)), \
             patch('sys.argv', ['test_sms.py', '--recipient', 'wally', mode]), \
             contextlib.redirect_stdout(output):
            try:
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/test_sms.py'), run_name='__main__')
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
        return output.getvalue()

    def test_local_check_and_auth_check_never_send_or_change_cycle(self):
        before = self.service.current()
        with patch('notifications.requests.get', return_value=SimpleNamespace(status_code=200)) as get:
            self.assertIn('TextBee', self.run_cli('--check'))
            get.assert_not_called()
            # Authentication is safe even with actual sending disabled.
            with patch.dict(self.env, SMS_ENABLED='false'):
                self.assertIn('인증 성공', self.run_cli('--check-auth'))
            get.assert_called_once_with('https://api.textbee.dev/api/v1/gateway/stats',
                headers={'x-api-key': 'test-only-key', 'Content-Type': 'application/json'},
                timeout=(5, 15), allow_redirects=False)
        self.post.assert_not_called()
        self.assertEqual(before, self.service.current())

    def test_failed_auth_is_safe_and_does_not_send(self):
        with patch('notifications.requests.get', return_value=SimpleNamespace(status_code=401)) as get:
            with self.assertRaisesRegex(SmsSendError, 'TextBee 인증 확인 HTTP 401'):
                self.service.check_sms_auth()
        get.assert_called_once()
        self.post.assert_not_called()

    def test_confirm_send_cli_only_sends_one_test_to_selected_id(self):
        before = self.service.current()
        self.assertIn('접수', self.run_cli('--confirm-send'))
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.kwargs['json']['recipients'], ['447700900001'])
        self.assertIn('[TEST]', self.post.call_args.kwargs['json']['message'])
        self.assertEqual(before, self.service.current())

    def test_function_completion_attempts_each_selected_recipient_once(self):
        now = BASE
        detector = CycleDetector(self.service, lambda: now, processor='function')
        current = self.service.current()
        detector.select_recipients(current['cycle_id'], ['wally', 'other'], current['revision'])
        self.assertTrue(detector.public_status()['sms_ready'])
        self.post.assert_not_called()
        # A failed first request must not block the second recipient or be retried.
        self.post.side_effect = [requests.Timeout(), SimpleNamespace(status_code=200)]
        for seconds in range(0, 805, 5):
            now = BASE + timedelta(seconds=seconds)
            detector.process(record(now, .02 if seconds <= 600 else .0065))
        detector.recover_finished()
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual([c.kwargs['json']['recipients'] for c in self.post.call_args_list],
                         [['447700900001'], ['447700900002']])
        current = self.service.current()
        self.assertEqual(current['status'], 'partial')
        self.assertEqual(current['send_outcomes'], {'wally': 'unknown', 'other': 'accepted'})
