import json
import os
import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ['REALTIME_ENABLED'] = 'false'
from app import app
from realtime import Consumer, LiveBroker, iso, now, valid_payload


def payload():
    return {'temperature': 25.0, 'pressure': 1001.2, 'sampleCount': 2,
            'samples': [{'t': i * 20, 'x': 0.1, 'y': 0.2, 'z': 1.0} for i in range(2)]}


def event(seq, device=b'washer', body=None, when=None):
    return SimpleNamespace(sequence_number=seq, enqueued_time=when or now(),
                           system_properties={b'iothub-connection-device-id': device},
                           body_as_str=lambda **kw: json.dumps(payload()) if body is None else body)


class BrokerTests(unittest.TestCase):
    def test_fresh_and_timeout_without_new_messages(self):
        b = LiveBroker()
        self.assertEqual(b.status()['dataStatus'], 'NO DATA')
        b.publish({'value': 1}, now())
        self.assertEqual(b.status()['dataStatus'], 'RECEIVING DATA')
        b.received_monotonic -= 31
        self.assertEqual(b.status()['dataStatus'], 'NO RECENT DATA')

    def test_old_backlog_is_not_live(self):
        b = LiveBroker()
        b.publish({}, now() - timedelta(minutes=10))
        self.assertEqual(b.status()['dataStatus'], 'NO RECENT DATA')

    def test_duplicate_and_out_of_order_do_not_refresh(self):
        b = LiveBroker()
        t = now()
        b.publish({'n': 1}, t)
        received = b.received_monotonic
        self.assertFalse(b.publish({'n': 2}, t))
        self.assertFalse(b.publish({'n': 0}, t - timedelta(seconds=5)))
        self.assertEqual(b.received_monotonic, received)
        self.assertEqual(len(b.events), 1)

    def test_replay_and_slow_client_reset(self):
        b = LiveBroker(capacity=2)
        t = now()
        b.publish({'n': 1}, t)
        old = b.events[-1][0]
        b.publish({'n': 2}, t + timedelta(seconds=1))
        self.assertEqual([r['n'] for _, r in b.batch_after(old)[0]], [2])
        b.publish({'n': 3}, t + timedelta(seconds=2))
        rows, reset = b.batch_after(old)
        self.assertTrue(reset)
        self.assertEqual([r['n'] for _, r in rows], [3])

    def test_restart_clears_cursor(self):
        stream = LiveBroker().stream('previous-process:4', heartbeat=0)
        self.assertIn('retry:', next(stream))
        self.assertIn('id:\nevent: reset', next(stream))
        self.assertIn('NO DATA', next(stream))
        stream.close()

    def test_two_clients_receive_same_event_and_heartbeat_does_not_refresh(self):
        b = LiveBroker()
        streams = [b.stream(None, heartbeat=0) for _ in range(2)]
        for s in streams:
            next(s)
            self.assertIn('NO DATA', next(s))
        b.publish({'hello': 'both'}, now())
        received = b.received_monotonic
        frames = [next(s) for s in streams]
        self.assertEqual(frames[0], frames[1])
        self.assertIn('telemetry', frames[0])
        for s in streams:
            next(s)
            s.close()
        self.assertEqual(received, b.received_monotonic)


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.broker = LiveBroker()
        self.consumer = Consumer(self.broker, {'DEVICE_ID': 'washer'})
        self.context = SimpleNamespace(partition_id='0')

    def test_valid_raw_iot_message_and_duplicate(self):
        e = event(1)
        self.consumer.on_event(self.context, e)
        self.consumer.on_event(self.context, e)
        self.assertEqual(len(self.broker.events), 1)
        self.assertEqual(self.broker.latest['payload']['sampleCount'], 2)
        self.assertEqual(self.broker.latest['deviceId'], 'washer')

    def test_custom_eventhub_preserves_original_iot_time(self):
        e = event(1)
        e.system_properties = {}
        e.properties = {b'iothub-connection-device-id': b'washer',
                        b'iothub-enqueuedtime': iso(now() - timedelta(minutes=5)).encode()}
        self.consumer.on_event(self.context, e)
        self.assertIsNotNone(self.broker.latest)
        self.assertEqual(self.broker.status()['dataStatus'], 'NO RECENT DATA')

    def test_other_device_and_malformed_do_not_refresh(self):
        for e in [event(1, device=b'other'), event(2, body='garbage'), event(3, body='[]')]:
            self.consumer.on_event(self.context, e)
        self.assertIsNone(self.broker.latest)
        self.assertEqual(self.consumer.positions['0'], 3)
        self.consumer.on_event(self.context, event(4))
        self.assertIsNotNone(self.broker.latest)

    def test_nonfinite_and_missing_fields(self):
        p = payload()
        p['samples'][0]['x'] = float('nan')
        self.assertFalse(valid_payload(p))
        self.assertFalse(valid_payload({'sampleCount': 0, 'samples': []}))
        p = payload()
        p['sampleCount'] = 3
        self.assertFalse(valid_payload(p))

    def test_all_partitions_recover_before_connected(self):
        self.consumer.partition_seen = {'0': 0, '1': 0}
        self.consumer.on_event(self.context, None)
        self.assertNotEqual(self.broker.consumer, 'connected')
        self.consumer.on_event(SimpleNamespace(partition_id='1'), None)
        self.assertEqual(self.broker.consumer, 'connected')
        self.consumer.on_error(self.context, RuntimeError('secret must not be logged'))
        self.assertEqual(self.broker.consumer, 'retrying')

    def test_reconnect_uses_per_partition_sequence(self):
        c = self.consumer
        c.positions = {'0': 42}
        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get_partition_ids(self): return ['0', '1']
            def receive(self, callback, **kwargs):
                self.positions = kwargs['starting_position']
                self.inclusive = kwargs['starting_position_inclusive']
                c.stop_event.set()
        fake = FakeClient()
        with patch.object(c, 'make_client', return_value=(fake, None)):
            c.run()
        self.assertEqual(fake.positions['0'], 42)
        self.assertEqual(fake.positions['1'], c.bootstrap)
        self.assertFalse(fake.inclusive)

    def test_start_is_singleton_under_concurrent_requests(self):
        c = Consumer(self.broker, {'DEVICE_ID': 'washer', 'EVENTHUB_CONNECTION_STRING': 'test'})
        gate = threading.Event()
        with patch.object(c, 'run', side_effect=lambda: gate.wait(2)) as run:
            threads = [threading.Thread(target=c.start) for _ in range(10)]
            for t in threads: t.start()
            for t in threads: t.join()
            gate.set()
            c.stop()
            self.assertEqual(run.call_count, 1)


class FlaskTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.broker = app.extensions['realtime']['broker']

    def test_live_routes_never_touch_blob(self):
        with patch('app.get_archive_container', side_effect=AssertionError('Blob read')):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.get('/api/telemetry').status_code, 200)
            self.assertEqual(self.client.get('/api/status').status_code, 200)
            r = self.client.get('/api/stream', buffered=False)
            self.assertEqual(r.mimetype, 'text/event-stream')
            self.assertIn('retry:', next(r.response).decode())
            r.close()

    def test_client_limit_and_disconnect_cleanup(self):
        responses = [self.client.get('/api/stream', buffered=False) for _ in range(8)]
        self.assertEqual(self.broker.clients, 8)
        self.assertEqual(self.client.get('/api/stream').status_code, 503)
        for r in responses: r.close()
        self.assertEqual(self.broker.clients, 0)
        r = self.client.get('/api/stream', buffered=False)
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_archive_is_explicit_separate_route(self):
        fake = SimpleNamespace(list_blobs=lambda: [])
        with patch('app.get_archive_container', return_value=fake) as archive:
            self.assertEqual(self.client.get('/api/history/latest').status_code, 404)
            archive.assert_called_once()


if __name__ == '__main__':
    unittest.main()
