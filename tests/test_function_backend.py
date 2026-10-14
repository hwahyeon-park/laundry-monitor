"""Durable hand-off and event-time processing; all SMS calls are mocked."""
import sys
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notifications import FileStore, NotificationService
from cycle_detector import CycleDetector, install_detector
from test_cycle_detector import record

BASE = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)


class FunctionBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = dict(SMS_ENABLED='true', PURESMS_API_KEY='test', PURESMS_SENDER='test',
                        CYCLE_DETECTOR_BACKEND='function')
        self.post = Mock(return_value=SimpleNamespace(status_code=202))
        self.service = NotificationService(FileStore(Path(self.tmp.name)/'state.json'),
                                           {'a': {'name':'A','phone':'+447700900001'}},self.env,self.post)
        c=self.service.current();self.cid=c['cycle_id']
        self.service.select(self.cid,['a'],0)
        self.time=BASE
        self.web=CycleDetector(self.service,lambda:self.time,processor='function')
        self.web.arm(self.cid,1,'wash_dry')
        self.worker=CycleDetector(self.service,lambda:self.time,processor='function')

    def feed(self, seconds, rms, lag=1, restart=False):
        at=BASE+timedelta(seconds=seconds);self.time=at+timedelta(seconds=lag)
        if restart:self.worker=CycleDetector(self.service,lambda:self.time,processor='function')
        return self.worker.process(record(at,rms))

    def complete(self,lag=1,restart=False):
        for t in range(0,605,5):self.feed(t,.02,lag,restart)
        for t in range(605,805,5):self.feed(t,.0065,lag,restart)

    def test_website_registers_control_only_without_telemetry_callback(self):
        from flask import Flask
        consumer=SimpleNamespace(on_telemetry=Mock())
        d=install_detector(Flask(__name__),self.service,consumer)
        self.assertEqual(d.processor,'function')
        self.assertIsNone(consumer.on_telemetry)

    def test_function_finishes_without_website_and_survives_every_event_restart(self):
        self.complete(restart=True)
        self.assertEqual(self.service.current()['detection']['phase'],'FINISHED')
        self.post.assert_called_once()

    def test_60_second_cold_start_backlog_is_processed_using_event_time(self):
        self.complete(lag=60)
        self.assertEqual(self.service.current()['status'],'accepted')
        self.post.assert_called_once()

    def test_old_completed_backlog_is_marked_missed_without_surprise_sms(self):
        self.complete(lag=400)
        self.assertEqual(self.service.current()['status'],'missed')
        self.post.assert_not_called()

    def test_old_web_worker_cannot_process_or_recover_function_cycles(self):
        old=CycleDetector(self.service,lambda:self.time)
        for t in range(0,805,5):
            self.time=BASE+timedelta(seconds=t)
            old.process(record(self.time,.02 if t<=600 else .0065))
        self.assertIsNone(self.service.current()['detection']['last_at'])
        def finish(s):
            s['cycles'][self.cid]['detection'].update(phase='FINISHED',detected_end_at=self.time.isoformat())
        self.service.store.transact(finish)
        old.recover_finished()
        self.assertEqual(old.dispatch(self.cid),'ignored')
        self.post.assert_not_called()
        self.worker.recover_finished()
        self.post.assert_called_once()

    def test_legacy_cycle_without_owner_is_not_claimed_by_function(self):
        self.service.store.transact(lambda s:s['cycles'][self.cid]['detection'].pop('processor'))
        self.complete()
        self.assertIsNone(self.service.current()['detection']['last_at'])
        self.post.assert_not_called()

    def test_event_retry_and_recovery_do_not_duplicate_sms(self):
        self.complete()
        self.feed(800,.0065,restart=True)
        self.worker.recover_finished()
        self.post.assert_called_once()

    def test_gap_is_not_invented_quiet_time_and_is_visible(self):
        for t in range(0,605,5):self.feed(t,.02)
        for t in range(605,700,5):self.feed(t,.0065)
        self.feed(1000,.0065)
        self.assertEqual(self.service.current()['detection']['gaps'],1)
        self.assertEqual(self.service.current()['detection']['window_seconds'],0)
        self.post.assert_not_called()

    def test_cancelled_function_cycle_never_sends(self):
        self.web.cancel(self.cid)
        self.complete()
        self.post.assert_not_called()

    def test_future_data_is_rejected(self):
        self.assertEqual(self.worker.process(record(BASE+timedelta(seconds=10),.02)),'stale')
        self.assertIsNone(self.service.current()['detection']['last_at'])

    def test_processing_failure_propagates_to_trigger_for_retry(self):
        self.service.store.transact=Mock(side_effect=RuntimeError('offline'))
        with self.assertRaises(RuntimeError):self.feed(0,.02)

    def test_timeout_is_exposed_without_automatic_duplicate_attempt(self):
        self.post.side_effect=TimeoutError('test timeout')
        self.complete();self.worker.recover_finished()
        self.assertEqual(self.service.current()['status'],'unknown')
        self.post.assert_called_once()
