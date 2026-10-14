import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from history import install_history, iso, history_source

NOW = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
ENV = {'DEVICE_ID': 'laundry-esp-paid', 'HISTORY_SOURCE': 'paid-hub-path'}


def row(seconds=5, seq=1, **overrides):
    result = dict(SchemaVersion=1, DeviceId=ENV['DEVICE_ID'], SequenceNumber=str(seq),
                  PartitionKey=ENV['DEVICE_ID']+'|'+(NOW-timedelta(seconds=seconds)).strftime('%Y%m%d'),
                  ReceivedAt=iso(NOW-timedelta(seconds=seconds)), SampleCount=2,
                  XMean=1., YMean=2., ZMean=3., VibrationRms=1., Temperature=25., Pressure=1000.)
    result.update(overrides)
    return result


def record(seconds=5, seq=1):
    return dict(deviceId=ENV['DEVICE_ID'], sequenceNumber=seq,
                enqueuedTime=iso(NOW-timedelta(seconds=seconds)),
                payload=dict(temperature=25., pressure=1000., samples=[
                    dict(x=0., y=2., z=3.), dict(x=2., y=2., z=3.)]))


class Table:
    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.calls = rows, error, []

    def query_entities(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        # Generator simulates all pages, not a first-page-only list.
        for item in self.rows:
            if item['PartitionKey'] == kwargs['parameters']['pk']:
                yield item


class HistoryTests(unittest.TestCase):
    def setup_app(self, rows=(), records=(), error=None, env=None):
        self.table = Table(rows, error)
        self.broker = SimpleNamespace(condition=threading.Condition(),
                                      events=[(str(i), r) for i, r in enumerate(records)], latest=None)
        self.app = Flask(__name__)
        self.app.extensions['realtime'] = {'broker': self.broker}
        install_history(self.app, lambda: self.table, lambda: NOW, ENV if env is None else env)
        return self.app.test_client()

    def test_midnight_queries_two_utc_partitions_and_range(self):
        client = self.setup_app([row(1200), row(5, 2)])
        data = client.get('/api/history').get_json()
        self.assertEqual(len(data['points']), 2)
        self.assertEqual([c['parameters']['pk'] for c in self.table.calls],
                         ['laundry-esp-paid|20261005', 'laundry-esp-paid|20261006'])
        self.assertEqual(self.table.calls[0]['parameters']['low'], '20261005T234000000000Z')
        self.assertEqual(self.table.calls[0]['parameters']['high'], '20261006T001000000000Z~')
        self.assertEqual(self.table.calls[0]['parameters']['source'], 'paid-hub-path')
        self.assertIn('Source eq @source', self.table.calls[0]['query_filter'])

    def test_reads_more_than_one_page(self):
        client = self.setup_app([row(i*5, i) for i in range(1, 721)])
        self.assertEqual(len(client.get('/api/history?minutes=60').get_json()['points']), 720)

    def test_invalid_range_does_not_read(self):
        client = self.setup_app()
        for value in ['1', '99999', 'garbage', '30.5']:
            self.assertEqual(client.get('/api/history?minutes='+value).status_code, 400)
        self.assertEqual(self.table.calls, [])

    def test_overlap_dedup_and_live_summary(self):
        client = self.setup_app([row(5, ReceivedAt='2026-10-06T01:09:55.000000+01:00')], [record()])
        data = client.get('/api/history').get_json()
        self.assertEqual(len(data['points']), 1)
        self.assertEqual(data['points'][0]['origin'], 'live')
        self.assertEqual(data['points'][0]['values'], [1, 2, 3, 1])

    def test_history_never_publishes_to_broker(self):
        client = self.setup_app([row()])
        client.get('/api/history')
        self.assertIsNone(self.broker.latest)
        self.assertEqual(self.broker.events, [])

    def test_failed_storage_keeps_recent_live(self):
        client = self.setup_app(records=[record()], error=RuntimeError('secret-not-for-response'))
        response = client.get('/api/history')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(response.get_json()['points']), 1)
        self.assertNotIn('secret', response.get_data(as_text=True))

    def test_invalid_rows_are_skipped(self):
        client = self.setup_app([row(), row(10, 2, VibrationRms=-1), row(15, 3, ReceivedAt=7)])
        data = client.get('/api/history').get_json()
        self.assertEqual(len(data['points']), 1)
        self.assertEqual(data['skippedRows'], 2)

    def test_cache_shared_by_browser_requests(self):
        client = self.setup_app([row()])
        client.get('/api/history')
        client.get('/api/history')
        self.assertEqual(len(self.table.calls), 2)  # two dates, once each
        client.get('/api/history?minutes=5')
        self.assertEqual(len(self.table.calls), 3)

    def test_empty_success_and_no_store(self):
        response = self.setup_app().get('/api/history')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['points'], [])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_missing_source_fails_explicitly(self):
        response = self.setup_app(env={'DEVICE_ID': ENV['DEVICE_ID']}).get('/api/history')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.table.calls, [])

    def test_max_rows_is_explicit_failure(self):
        client = self.setup_app([row(5, i) for i in range(2001)])
        response = client.get('/api/history')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()['error'], 'too_many_records')

    def test_window_and_device_filter_apply_to_live(self):
        wrong = record(5, 3)
        wrong['deviceId'] = 'other-device'
        client = self.setup_app(records=[record(2000), record(5, 2), wrong])
        self.assertEqual(len(client.get('/api/history').get_json()['points']), 1)

    def test_source_from_connection_string_does_not_split_key(self):
        self.assertEqual(history_source({'EVENTHUB_CONNECTION_STRING':
                                        'Endpoint=sb://test/;SharedAccessKey=abc==;EntityPath=hub-path'}), 'hub-path')


if __name__ == '__main__':
    unittest.main()
