import sys
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notifications import FileStore, NotificationService, Conflict
from cycle_detector import CycleDetector

BASE = datetime(2026,10,7,8,0,tzinfo=timezone.utc)


def record(at, rms):
    return {'enqueuedTime':at.isoformat(), 'payload':{'sampleCount':250, 'temperature':25, 'pressure':1000,
        'samples':[{'t':i*20, 'x':rms if i%2 else -rms, 'y':0, 'z':1} for i in range(250)]}}


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.time=BASE
        self.post=Mock(return_value=SimpleNamespace(status_code=202))
        self.env={'SMS_ENABLED':'true','PURESMS_API_KEY':'test-only','PURESMS_SENDER':'ConnectTest'}
        self.service=NotificationService(FileStore(Path(self.temp.name)/'state.json'),
            {'a':{'name':'A','phone':'+447700900001'},'b':{'name':'B','phone':'+447700900002'}},self.env,self.post)
        c=self.service.current(); self.cid=c['cycle_id']
        self.service.select(self.cid,['a','b'],0)
        self.detector=CycleDetector(self.service,lambda:self.time)
        self.detector.arm(self.cid,1,'wash_dry')

    def feed(self,seconds,rms):
        self.time=BASE+timedelta(seconds=seconds)
        return self.detector.process(record(self.time,rms))

    def active(self,end=600):
        for t in range(0,end+1,5): self.feed(t,.02)

    def finish(self):
        self.active()
        for t in range(605,805,5): self.feed(t,.0065)

    def test_completion_sends_each_selected_once_and_locks_selection(self):
        self.finish()
        d=self.service.current()['detection']
        self.assertEqual(d['phase'],'FINISHED'); self.assertEqual(self.post.call_count,2)
        for t in range(810,900,5): self.feed(t,.0065)
        self.detector.recover_finished()
        self.assertEqual(self.post.call_count,2)
        with self.assertRaises(Conflict): self.service.select(self.cid,['a'],self.service.current()['revision'])
        self.assertIn('decision',d)

    def test_quiet_machine_never_started_and_short_in_cycle_pause_do_not_finish(self):
        for t in range(0,1000,5): self.feed(t,.0065)
        self.assertEqual(self.service.current()['detection']['phase'],'ARMED')
        for t in range(1000,1100,5): self.feed(t,.02)
        for t in range(1100,1190,5): self.feed(t,.0065)
        self.assertNotEqual(self.service.current()['detection']['phase'],'FINISHED')
        self.post.assert_not_called()

    def test_gap_and_stale_replay_are_not_quiet(self):
        self.active()
        for t in range(605,700,5): self.feed(t,.0065)
        self.feed(1000,.0065)
        self.assertEqual(self.service.current()['detection']['window_seconds'],0)
        self.assertEqual(self.service.current()['detection']['gaps'],1)
        self.assertEqual(self.detector.process(record(BASE+timedelta(seconds=500),.0065)),'stale')
        for t in range(1005,1100,5): self.feed(t,.0065)
        self.post.assert_not_called()

    def test_minimum_runtime_duplicate_out_of_order_and_restart(self):
        self.active(60)
        for t in range(65,300,5): self.feed(t,.0065)
        self.post.assert_not_called()
        count=len(self.service.current()['detection']['points'])
        self.detector.process(record(self.time,.0065))
        self.detector.process(record(self.time-timedelta(seconds=5),.0065))
        self.assertEqual(len(self.service.current()['detection']['points']),count)
        self.detector=CycleDetector(self.service,lambda:self.time)
        self.active(600)
        for t in range(605,805,5): self.feed(t,.0065)
        self.assertEqual(self.post.call_count,2)

    def test_manual_observed_end_does_not_force_finish_or_send(self):
        self.time+=timedelta(minutes=5)
        self.detector.observe_end(self.cid)
        first=self.service.current()['detection']['observed_end_at']
        self.time+=timedelta(minutes=1); self.detector.observe_end(self.cid)
        self.assertEqual(self.service.current()['detection']['observed_end_at'],first)
        self.post.assert_not_called()

    def test_disabled_finishes_without_later_retroactive_sms(self):
        self.env['SMS_ENABLED']='false'
        self.finish()
        self.assertEqual(self.service.current()['status'],'disabled')
        self.env['SMS_ENABLED']='true';self.detector.recover_finished()
        self.post.assert_not_called()
        self.detector.prepare(self.cid)
        self.assertEqual(self.service.current()['recipient_ids'],[])

    def test_sms_failure_keeps_other_recipient_attempt_and_never_retries(self):
        self.post.side_effect=[TimeoutError('secret'),SimpleNamespace(status_code=202)]
        self.finish(); self.detector.recover_finished()
        self.assertEqual(self.service.current()['status'],'partial')
        self.assertEqual(self.post.call_count,2)

    def test_missing_sms_setup_prevents_arming_when_enabled(self):
        self.finish();self.detector.prepare(self.cid)
        c=self.service.current();self.service.select(c['cycle_id'],['a'],0)
        self.env.pop('PURESMS_API_KEY')
        with self.assertRaises(ValueError): self.detector.arm(c['cycle_id'],1,'dry')

    def test_browser_not_needed_and_public_status_has_no_recipient_secrets(self):
        self.active(60)
        status=self.detector.public_status()
        self.assertEqual(status['phase'],'RUNNING')
        self.assertTrue(status['fresh'])
        self.time+=timedelta(seconds=31)
        self.assertFalse(self.detector.public_status()['fresh'])
        self.assertNotIn('phone',str(status));self.assertNotIn('test-only',str(status))

    def test_cancelled_cycle_never_sends_and_next_cycle_starts_empty(self):
        self.active(60); self.detector.cancel(self.cid)
        for t in range(600,1000,5):self.feed(t,.0065)
        self.post.assert_not_called()
        self.detector.prepare(self.cid)
        self.assertEqual(self.service.current()['recipient_ids'],[])

    def test_restart_recovers_committed_finish_without_duplicate_attempt(self):
        self.active()
        def committed(state):
            c=state['cycles'][self.cid]
            c['detection'].update(phase='FINISHED',detected_end_at=self.time.isoformat())
        self.service.store.transact(committed)
        self.detector.recover_finished(); self.detector.recover_finished()
        self.assertEqual(self.post.call_count,2)

    def test_very_late_recovery_does_not_send_old_sms(self):
        self.active()
        def committed(state):
            state['cycles'][self.cid]['detection'].update(phase='FINISHED',detected_end_at=self.time.isoformat())
        self.service.store.transact(committed)
        self.time+=timedelta(seconds=301);self.detector.recover_finished()
        self.assertEqual(self.service.current()['status'],'missed')
        self.post.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def test_authenticated_cycle_workflow_and_export(self):
        from flask import Flask
        from notifications import install_notifications
        from cycle_detector import install_detector
        from test_notifications import ENV, PASSWORD
        with tempfile.TemporaryDirectory() as tmp:
            app=Flask(__name__); app.testing=True
            svc=install_notifications(app,ENV,FileStore(Path(tmp)/'state.json'))
            detector=install_detector(app,svc,SimpleNamespace())
            client=app.test_client()
            csrf=client.get('/api/notifications/session').get_json()['csrf_token']
            self.assertEqual(client.post('/api/notifications/start',json={},headers={'X-CSRF-Token':csrf}).status_code,401)
            self.assertEqual(client.get('/api/notifications/cycles').status_code,401)
            client.post('/api/notifications/login',json={'password':PASSWORD},headers={'X-CSRF-Token':csrf})
            data=client.get('/api/notifications/session').get_json();headers={'X-CSRF-Token':data['csrf_token']};c=data['cycle']
            saved=client.post('/api/notifications/recipient',json=dict(cycle_id=c['cycle_id'],recipient_ids=['wally'],revision=c['revision']),headers=headers).get_json()['cycle']
            result=client.post('/api/notifications/start',json=dict(cycle_id=c['cycle_id'],revision=saved['revision'],mode='dry'),headers=headers)
            self.assertEqual(result.status_code,200)
            self.assertEqual(client.get('/api/cycle').get_json()['phase'],'ARMED')
            self.assertEqual(client.post('/api/notifications/observed-end',json=dict(cycle_id=c['cycle_id']),headers=headers).status_code,200)
            exported=client.get('/api/notifications/cycles').get_json()
            self.assertTrue(exported['cycles'][c['cycle_id']]['detection']['observed_end_at'])
            self.assertNotIn(ENV['PURESMS_API_KEY'],str(exported))
            self.assertEqual(client.post('/api/notifications/cancel',json=dict(cycle_id=c['cycle_id']),headers=headers).status_code,200)
            self.assertEqual(client.post('/api/notifications/prepare',json=dict(cycle_id=c['cycle_id']),headers=headers).status_code,200)
            self.assertFalse(svc.current()['recipient_ids'])

    def test_live_callback_receives_only_accepted_records(self):
        from realtime import Consumer,LiveBroker
        import json
        at=datetime.now(timezone.utc)
        consumer=Consumer(LiveBroker(),{'DEVICE_ID':'device'})
        callback=Mock();consumer.on_telemetry=callback
        event=SimpleNamespace(sequence_number=1,system_properties={'iothub-connection-device-id':'device'},enqueued_time=at,
                              body_as_str=lambda **kw:json.dumps(record(at,.02)['payload']))
        context=SimpleNamespace(partition_id='0')
        consumer.on_event(context,event);consumer.on_event(context,event)
        callback.assert_called_once()
        self.assertEqual(callback.call_args.args[0]['deviceId'],'device')
