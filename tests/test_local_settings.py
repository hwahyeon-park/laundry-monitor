import json
import tempfile
import unittest
from pathlib import Path
from local_settings import load_local_settings


class LocalSettingsTests(unittest.TestCase):
    def test_local_loading_and_explicit_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps({'SMS_ENABLED': 'false', 'NOTIFICATION_COOKIE_SECURE': 'false'}))
            env = {'SMS_ENABLED': 'true'}
            load_local_settings(env, path)
            self.assertEqual(env, {'SMS_ENABLED': 'true', 'NOTIFICATION_COOKIE_SECURE': 'false'})
            azure = {'WEBSITE_HOSTNAME': 'example.azurewebsites.net'}
            load_local_settings(azure, path)
            self.assertEqual(azure, {'WEBSITE_HOSTNAME': 'example.azurewebsites.net'})

    def test_invalid_or_missing_settings_do_not_break_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            env = {}
            load_local_settings(env, path)
            for contents in ['broken-json', '{"UNRELATED_SETTING":"value"}', '[]']:
                path.write_text(contents)
                load_local_settings(env, path)
            self.assertEqual(env, {})
