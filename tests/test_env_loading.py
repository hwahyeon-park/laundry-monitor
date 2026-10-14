import json
import tempfile
import unittest
from pathlib import Path
from local_settings import load_local_settings


class EnvLoadingTests(unittest.TestCase):
    def test_env_precedence_literal_secrets_and_azure_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / 'legacy.json'
            settings.write_text(json.dumps({'PURESMS_API_KEY': 'legacy', 'SMS_ENABLED': 'false'}))
            (root / '.env').write_text("PURESMS_API_KEY='literal-${HOME}-value'\nSMS_ENABLED=true\nNOTIFICATION_RECIPIENTS_JSON='[{\"id\":\"test\"}]'\n")
            env = {'SMS_ENABLED': 'false'}
            load_local_settings(env, settings)
            self.assertEqual(env['PURESMS_API_KEY'], 'literal-${HOME}-value')
            self.assertEqual(env['SMS_ENABLED'], 'false')
            self.assertEqual(json.loads(env['NOTIFICATION_RECIPIENTS_JSON']), [{'id': 'test'}])
            azure = {'WEBSITE_HOSTNAME': 'test.azurewebsites.net'}
            load_local_settings(azure, settings)
            self.assertEqual(azure, {'WEBSITE_HOSTNAME': 'test.azurewebsites.net'})
