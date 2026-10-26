import os
import unittest
from unittest.mock import patch
from app import app


class PageLanguageTests(unittest.TestCase):
    def test_default_and_explicit_language_render_the_complete_page(self):
        with patch.dict(os.environ, {'UI_LANGUAGE': 'kor'}):
            client = app.test_client()
            korean = client.get('/')
            self.assertEqual(korean.status_code, 200)
            self.assertIn('<html lang="ko">', korean.text)
            self.assertIn('비밀번호', korean.text)
            english = client.get('/?lang=eng')
            self.assertEqual(english.status_code, 200)
            self.assertIn('<html lang="en">', english.text)
            self.assertIn('4-digit access code', english.text)
            for response in [korean, english]:
                self.assertIn('/static/images/highland-cow.jpg', response.text)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            # An explicit English page does not change the global Korean default.
            self.assertIn('<html lang="ko">', client.get('/').text)
        with patch.dict(os.environ, {'UI_LANGUAGE': 'eng'}):
            self.assertIn('<html lang="en">', client.get('/').text)
            self.assertIn('<html lang="ko">', client.get('/?lang=kor').text)

    def test_unknown_language_is_not_used_as_a_template_path(self):
        self.assertEqual(app.test_client().get('/?lang=../../.env').status_code, 400)
