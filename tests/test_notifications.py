import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notifications import BlobStore, Conflict, FileStore, NotificationService, install_notifications

PASSWORD = '0123'
RECIPIENTS = [dict(id='wally', name='Wally', phone='+447700900001'),
              dict(id='a', name='Flatmate A', phone='+447700900002'),
              dict(id='b', name='Flatmate B', phone='+447700900003')]
ENV = dict(NOTIFICATION_SECRET_KEY='test-key-' * 8,
           NOTIFICATION_PASSWORD_HASH=generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'),
           NOTIFICATION_RECIPIENTS_JSON=json.dumps(RECIPIENTS), NOTIFICATION_COOKIE_SECURE='false',
           SMS_ENABLED='true', PURESMS_API_KEY='test-api-key', PURESMS_SENDER='ConnectTest')


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.post = Mock(return_value=SimpleNamespace(status_code=202))
        self.now = 1000
        self.app, self.service = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, env=None, store=None):
        app = Flask(__name__)
        app.testing = True
        service = install_notifications(app, ENV if env is None else env,
                                        store or FileStore(self.path), self.post, lambda: self.now)
        from cycle_detector import install_detector
        install_detector(app, service, SimpleNamespace())
        return app, service

    def bootstrap(self, client=None):
        client = client or self.client
        data = client.get('/api/notifications/session').get_json()
        return data['csrf_token']

    def submit(self, endpoint, data, csrf, client=None):
        return (client or self.client).post('/api/notifications/' + endpoint, json=data,
                                          headers={'X-CSRF-Token': csrf})

    def login(self, client=None):
        client = client or self.client
        csrf = self.bootstrap(client)
        response = self.submit('login', {'password': PASSWORD}, csrf, client)
        self.assertEqual(response.status_code, 200)
        return client.get('/api/notifications/session').get_json()

    def select(self, rid='wally'):
        data = self.login()
        cycle = data['cycle']
        response = self.submit('recipient', dict(recipient_ids=rid if isinstance(rid, list) else [rid], cycle_id=cycle['cycle_id'],
                                                revision=cycle['revision']), data['csrf_token'])
        self.assertEqual(response.status_code, 200)
        return response.get_json()['cycle']

    def test_auth_required_and_no_disclosure(self):
        response = self.client.get('/api/notifications/session')
        body = response.get_data(as_text=True)
        self.assertNotIn('Wally', body)
        self.assertNotIn('phone', body)
        csrf = response.get_json()['csrf_token']
        self.assertEqual(self.submit('recipient', {}, csrf).status_code, 401)
        self.assertEqual(self.submit('login', {'password': 'wrong'}, csrf).status_code, 401)
        self.assertEqual(self.submit('login', {'password': PASSWORD}, 'wrong').status_code, 403)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', response.headers['Set-Cookie'])

    def test_exactly_three_public_options_and_selection_never_sends(self):
        data = self.login()
        self.assertEqual(data['recipients'], [{'id': r['id'], 'name': r['name']} for r in RECIPIENTS])
        serialized = json.dumps(data)
        for secret in [PASSWORD, ENV['NOTIFICATION_PASSWORD_HASH'], ENV['PURESMS_API_KEY']] + [r['phone'] for r in RECIPIENTS]:
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.select()['recipient_names'], ['Wally'])
        self.post.assert_not_called()
        self.assertNotIn('phone', self.path.read_text())

    def test_multiple_recipients_saved_replaced_and_restored_without_sending(self):
        cycle = self.select(['b', 'wally'])
        self.assertEqual(cycle['recipient_ids'], ['wally', 'b'])
        self.assertEqual(cycle['recipient_names'], ['Wally', 'Flatmate B'])
        self.assertEqual(self.make_app()[1].current()['recipient_ids'], ['wally', 'b'])
        self.assertEqual(self.select(['a'])['recipient_ids'], ['a'])
        self.assertEqual(len(self.select(['wally', 'a', 'b'])['recipient_ids']), 3)
        self.post.assert_not_called()

    def test_recipient_selection_without_phone_numbers_when_sms_disabled(self):
        rows = [{**r, 'phone': ''} for r in RECIPIENTS]
        env = {**ENV, 'SMS_ENABLED': 'false', 'NOTIFICATION_RECIPIENTS_JSON': json.dumps(rows)}
        app, service = self.make_app(env)
        self.assertIsNotNone(service)
        data = self.login(app.test_client())
        cycle = data['cycle']
        service.select(cycle['cycle_id'], ['wally', 'a'], cycle['revision'])
        self.assertEqual(service.current()['recipient_ids'], ['wally', 'a'])
        self.post.assert_not_called()
        _, enabled_service = self.make_app({**env, 'SMS_ENABLED': 'true'})
        self.assertIsNotNone(enabled_service)
        with self.assertRaises(ValueError): enabled_service.send_sms('wally')

    def test_four_digit_pin_including_leading_zero(self):
        csrf = self.bootstrap()
        for pin in ['123', '12345', 'abcd', '１２３４', 1234, ' 0123']:
            self.assertEqual(self.submit('login', {'password': pin}, csrf).status_code, 401)
        self.assertEqual(self.submit('login', {'password': '0123'}, csrf).status_code, 200)

    def test_concurrent_completion_each_selected_recipient_once_despite_one_failure(self):
        cycle = self.select(['wally', 'a', 'b'])
        def post(*args, **kwargs):
            if kwargs['json']['recipient'] == RECIPIENTS[1]['phone']:
                raise TimeoutError('provider secret')
            return SimpleNamespace(status_code=202)
        self.post.side_effect = post
        services = [self.make_app()[1] for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda svc: svc.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), services))
        self.assertEqual(len({c.kwargs['json']['content'] for c in self.post.call_args_list}), 1)
        self.assertEqual(results.count('partial'), 1)
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual([call.kwargs['json']['recipient'] for call in self.post.call_args_list],
                         [r['phone'] for r in RECIPIENTS])
        self.assertEqual(self.make_app()[1].on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), 'skipped')
        self.assertEqual(self.service.current()['send_outcomes'], {'wally': 'accepted', 'a': 'unknown', 'b': 'accepted'})
        self.assertEqual(self.post.call_count, 3)

    def test_legacy_single_recipient_migrates_without_resending_completed_cycles(self):
        for status in ['pending', 'accepted', 'claimed', 'unknown']:
            self.path.write_text(json.dumps({'current': 'legacy', 'cycles': {
                'legacy': {'recipient': 'wally', 'status': status, 'revision': 4}}}))
            cycle = self.service.current()
            self.assertEqual(cycle['recipient_ids'], ['wally'])
            self.assertEqual(cycle['revision'], 4)
            self.assertEqual(cycle['status'], status)
            self.assertNotIn('recipient', cycle)
            if status != 'pending':
                self.assertEqual(self.service.on_state_transition('legacy', 'RUNNING', 'FINISHED'), 'skipped')
        self.post.assert_not_called()

    def test_empty_duplicates_invalid_and_arbitrary_numbers_rejected(self):
        data = self.login()
        for recipient in [None, [], ['wally', 'wally'], ['unknown'], ['+447700900999'], ['wally', 'a', 'b', 'wally'], ['wally', {}], 'wally', {'id': 'wally'}]:
            response = self.submit('recipient', dict(recipient_ids=recipient,
                cycle_id=data['cycle']['cycle_id'], revision=0), data['csrf_token'])
            self.assertEqual(response.status_code, 400)
        self.post.assert_not_called()

    def test_restart_persistence_and_two_browsers_stale_selection(self):
        data = self.login()
        other = self.app.test_client()
        other_data = self.login(other)
        self.select('a')
        stale = self.submit('recipient', dict(recipient_ids=['wally'],
            cycle_id=other_data['cycle']['cycle_id'], revision=0), other_data['csrf_token'], other)
        self.assertEqual(stale.status_code, 409)
        _, restarted = self.make_app()
        self.assertEqual(restarted.current()['recipient_ids'], ['a'])

    def test_expiry_logout_and_password_rotation(self):
        data = self.login()
        self.now += 61
        self.assertEqual(self.submit('recipient', {}, data['csrf_token']).status_code, 401)
        data = self.login()
        self.assertEqual(self.submit('logout', {}, data['csrf_token']).status_code, 200)
        self.assertFalse(self.client.get('/api/notifications/session').get_json()['authenticated'])
        data = self.login()
        app, _ = self.make_app({**ENV, 'NOTIFICATION_PASSWORD_HASH': generate_password_hash('changed')})
        other = app.test_client()
        other.set_cookie('laundry_notification_session', self.client.get_cookie(
            'laundry_notification_session', path='/api/notifications').value, path='/api/notifications')
        self.assertFalse(other.get('/api/notifications/session').get_json()['authenticated'])

    def test_one_minute_expiry_is_per_browser_and_keeps_saved_recipients(self):
        self.select(['wally', 'a'])
        first = self.client.get('/api/notifications/session').get_json()
        self.assertEqual(first['expires_in'], 60)
        other = self.app.test_client()
        self.assertFalse(other.get('/api/notifications/session').get_json()['authenticated'])
        self.now += 30
        self.assertEqual(self.client.get('/api/notifications/session').get_json()['expires_in'], 30)
        second = self.login(other)
        self.now += 30
        self.assertFalse(self.client.get('/api/notifications/session').get_json()['authenticated'])
        self.assertTrue(other.get('/api/notifications/session').get_json()['authenticated'])
        response = self.submit('recipient', {'recipient_ids': ['b'], 'cycle_id': first['cycle']['cycle_id'],
                              'revision': first['cycle']['revision']}, first['csrf_token'])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.service.current()['recipient_ids'], ['wally', 'a'])

    def test_bruteforce_limit_and_recovery(self):
        csrf = self.bootstrap()
        for _ in range(10):
            self.assertEqual(self.submit('login', {'password': 'wrong'}, csrf).status_code, 401)
        self.assertEqual(self.submit('login', {'password': PASSWORD}, csrf).status_code, 429)
        self.now += 61
        self.assertEqual(self.submit('login', {'password': PASSWORD}, csrf).status_code, 200)

    def test_missing_bad_settings_do_not_break_dashboard(self):
        for env in [{}, {**ENV, 'NOTIFICATION_RECIPIENTS_JSON': '[]'},
                    {**ENV, 'NOTIFICATION_RECIPIENTS_JSON': '[null, null, null]'}]:
            app, service = self.make_app(env)
            app.add_url_rule('/telemetry-example', view_func=lambda: 'ok')
            self.assertIsNone(service)
            self.assertEqual(app.test_client().get('/api/notifications/session').status_code, 503)
            self.assertEqual(app.test_client().get('/telemetry-example').status_code, 200)

    def test_azure_forces_secure_cookie(self):
        app, _ = self.make_app({**ENV, 'WEBSITE_HOSTNAME': 'test.azurewebsites.net'})
        cookie = app.test_client().get('/api/notifications/session').headers['Set-Cookie']
        self.assertIn('Secure', cookie)

    def test_completed_cycles_locked_and_new_cycle_clears_selection(self):
        cycle = self.select()
        self.service.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED')
        with self.assertRaises(Conflict):
            self.service.select(cycle['cycle_id'], ['b'], cycle['revision'])
        self.assertTrue(self.service.begin_cycle('second'))
        self.assertEqual(self.service.current()['recipient_ids'], [])
        self.assertFalse(self.service.begin_cycle(cycle['cycle_id']))
        self.assertEqual(self.service.current()['cycle_id'], 'second')
        with self.assertRaises(Conflict):
            self.service.begin_cycle('third')

    def test_concurrent_completion_claim_and_restart_only_one_attempt(self):
        cycle = self.select()
        services = [self.make_app()[1] for _ in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(lambda s: s.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), services))
        self.assertEqual(outcomes.count('accepted'), 1)
        self.post.assert_called_once()
        self.assertEqual(self.make_app()[1].on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), 'skipped')
        args = self.post.call_args
        self.assertEqual(args.args[0], 'https://connect-api.divergent.cloud/sms/send')
        self.assertEqual(args.kwargs['headers'], {'X-Api-Key': 'test-api-key'})
        self.assertEqual(args.kwargs['json']['recipient'], RECIPIENTS[0]['phone'])
        self.assertFalse(args.kwargs['allow_redirects'])

    def test_timeout_and_http_error_never_retry_or_leak(self):
        cycle = self.select()
        self.post.side_effect = TimeoutError('sensitive provider body')
        self.assertEqual(self.service.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), 'unknown')
        self.assertEqual(self.service.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), 'skipped')
        self.assertNotIn('sensitive', self.path.read_text())
        self.post.assert_called_once()
        self.service.begin_cycle('next')
        self.service.select('next', ['a'], 0)
        self.post.side_effect = None
        self.post.return_value.status_code = 500
        self.assertEqual(self.service.on_state_transition('next', 'RUNNING', 'FINISHED'), 'unknown')

    def test_disabled_wrong_transition_and_missing_recipient_send_nothing(self):
        cycle = self.service.current()
        self.assertEqual(self.service.on_state_transition(cycle['cycle_id'], 'FINISHED', 'FINISHED'), 'ignored')
        disabled = NotificationService(self.service.store, self.service.recipients, {**ENV, 'SMS_ENABLED': 'false'}, self.post)
        self.assertEqual(disabled.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), 'disabled')
        self.assertEqual(self.service.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), 'skipped')
        self.assertEqual(self.service.current()['status'], 'skipped')
        self.post.assert_not_called()

    def test_no_http_completion_or_test_send_routes_and_oversized_login(self):
        self.assertEqual(self.client.post('/api/notifications/test').status_code, 404)
        self.assertEqual(self.client.post('/api/notifications/finish').status_code, 404)
        csrf = self.bootstrap()
        self.assertEqual(self.submit('login', {'password': 'x' * 5000}, csrf).status_code, 413)

    def test_storage_failure_isolated_sanitized_no_sending(self):
        store = Mock()
        store.transact.side_effect = RuntimeError('secret-storage-account-key')
        app, _ = self.make_app(store=store)
        client = app.test_client()
        csrf = self.bootstrap(client)
        self.submit('login', {'password': PASSWORD}, csrf, client)
        response = client.get('/api/notifications/session')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('secret', response.get_data(as_text=True))
        self.post.assert_not_called()


class FakeBlob:
    def __init__(self):
        self.data, self.etag = None, 0
        self.lock = threading.Lock()

    def download_blob(self):
        from azure.core.exceptions import ResourceNotFoundError
        with self.lock:
            if self.data is None:
                raise ResourceNotFoundError('not found')
            data, etag = self.data, str(self.etag)
            return SimpleNamespace(readall=lambda: data, properties=SimpleNamespace(etag=etag))

    def upload_blob(self, data, overwrite=False, etag=None, match_condition=None):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
        with self.lock:
            if not overwrite and self.data is not None:
                raise ResourceExistsError('exists')
            if overwrite:
                assert match_condition == MatchConditions.IfNotModified
                if etag != str(self.etag):
                    raise ResourceModifiedError('race')
            self.data = data
            self.etag += 1


class BlobTests(unittest.TestCase):
    def test_cas_retries_competing_initialization_and_recipient_edits(self):
        blob = FakeBlob()
        store = BlobStore(lambda: blob)
        def increment(state):
            state['count'] = state.get('count', 0) + 1
            return state['count']
        self.assertEqual(store.transact(increment), 1)
        original = blob.upload_blob
        raced = [False]
        def competing_upload(*args, **kwargs):
            if not raced[0]:
                raced[0] = True
                store2 = BlobStore(lambda: blob)
                blob.upload_blob = original
                store2.transact(increment)
            return original(*args, **kwargs)
        blob.upload_blob = competing_upload
        self.assertEqual(store.transact(increment), 3)
        self.assertEqual(json.loads(blob.data)['count'], 3)

    def test_blob_completion_claim_once(self):
        blob = FakeBlob()
        post = Mock(return_value=SimpleNamespace(status_code=200))
        recipients = {r['id']: r for r in RECIPIENTS}
        services = [NotificationService(BlobStore(lambda: blob), recipients, ENV, post) for _ in range(6)]
        cycle = services[0].current()
        services[0].select(cycle['cycle_id'], ['wally'], 0)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda s: s.on_state_transition(cycle['cycle_id'], 'RUNNING', 'FINISHED'), services))
        post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
