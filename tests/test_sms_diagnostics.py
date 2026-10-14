import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import requests
from notifications import NotificationService, SmsSendError

class SmsDiagnosticsTests(unittest.TestCase):
    def service(self, post):
        return NotificationService(None,{'wally':{'phone':'+447700900001'}},
            {'SMS_ENABLED':'true','PURESMS_API_KEY':'secret-api-key','PURESMS_SENDER':'ConnectTest'},post)

    def test_http_reason_redacts_credentials_and_number(self):
        post=Mock(return_value=SimpleNamespace(status_code=403,json=lambda:{'message':'Sender unapproved secret-api-key +447700900001'}))
        with self.assertRaises(SmsSendError) as result:self.service(post).send_sms('wally')
        text=str(result.exception)
        self.assertIn('HTTP 403',text); self.assertIn('Sender unapproved',text)
        self.assertNotIn('secret-api-key',text);self.assertNotIn('+447700900001',text)
        post.assert_called_once()

    def test_timeout_and_check_never_retry(self):
        post=Mock(side_effect=requests.Timeout('secret details'))
        service=self.service(post);service.validate_sms('wally');post.assert_not_called()
        with self.assertRaises(SmsSendError) as result:service.send_sms('wally')
        self.assertIn('시간 초과',str(result.exception));self.assertNotIn('secret details',str(result.exception))
        post.assert_called_once()
