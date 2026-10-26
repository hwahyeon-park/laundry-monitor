"""Experimental live-only completion detector; persistent rules/evidence per cycle."""
import atexit
import math
import queue
import threading
import logging
from datetime import datetime, timezone
from uuid import uuid4

from flask import jsonify
from notifications import Conflict, sms_configured

log = logging.getLogger(__name__)
RULES = dict(version='2026-10-07-v1', quiet_rms=0.008, active_rms=0.012,
             quiet_seconds=180, quiet_fraction=0.95, min_cycle_seconds=600,
             max_gap_seconds=15, max_age_seconds=30)


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def summarize(record):
    from realtime import valid_payload
    payload = record['payload']
    if not valid_payload(payload) or len(payload['samples']) < 200:
        raise ValueError('incomplete batch')
    axes = [[s[k] for s in payload['samples']] for k in ('x', 'y', 'z')]
    means = [math.fsum(a) / len(a) for a in axes]
    rms = math.sqrt(sum(math.fsum((v - m) ** 2 for v in a) / len(a) for a, m in zip(axes, means)))
    return parse(record['enqueuedTime']), rms


class CycleDetector:
    def __init__(self, service, clock=now, processor='web'):
        if processor not in ('web', 'function'):
            raise ValueError('invalid detector processor')
        self.processor = processor
        self.service, self.clock = service, clock
        self.queue = queue.Queue(maxsize=60)
        self.lock = threading.Lock()
        self.thread = None
        self.stopping = threading.Event()
        self.last_error = None

    def select_recipients(self, cycle_id, recipient_ids, revision):
        """Save recipients and arm a new run in one durable transaction."""
        if self.service.env.get('SMS_ENABLED', 'false').lower() == 'true':
            if not sms_configured(self.service.env):
                raise ValueError('SMS configuration missing')
        detection = dict(phase='ARMED', started_at=stamp(self.clock()), mode='unspecified',
                         rules=dict(RULES), processor=self.processor, points=[],
                         activity_confirmed=False, last_at=None, gaps=0, observed_end_at=None)
        return self.service.select(cycle_id, recipient_ids, revision, detection=detection)

    def arm(self, cycle_id, revision, mode):
        if mode not in ('wash', 'dry', 'wash_dry'):
            raise ValueError('invalid mode')
        start = stamp(self.clock())
        def change(state):
            if cycle_id != state['current']:
                raise Conflict('현재 코스를 다시 불러오세요.')
            c = state['cycles'][cycle_id]
            if c['revision'] != revision or c['status'] != 'pending' or c.get('detection'):
                raise Conflict('이미 시작했거나 변경된 코스입니다.')
            if not c.get('recipient_ids'):
                raise ValueError('save recipients first')
            if self.service.env.get('SMS_ENABLED', 'false').lower() == 'true':
                if (not sms_configured(self.service.env)
                        or any(not self.service.recipients.get(rid, {}).get('phone') for rid in c['recipient_ids'])):
                    raise ValueError('SMS configuration missing')
            c['detection'] = dict(phase='ARMED', started_at=start, mode=mode, rules=dict(RULES),
                                  processor=self.processor,
                                  points=[], activity_confirmed=False, last_at=None,
                                  gaps=0, observed_end_at=None)
            self.service.alert_settings(state).update(enabled=True, recipient_ids=list(c['recipient_ids']))
            c['revision'] += 1
        self.service.store.transact(change)

    def prepare(self, cycle_id):
        new_id = uuid4().hex
        def change(state):
            if cycle_id != state['current']:
                raise Conflict('현재 코스를 다시 불러오세요.')
            old = state['cycles'][cycle_id]
            if old.get('detection', {}).get('phase') not in ('FINISHED', 'CANCELLED'):
                raise Conflict('현재 코스가 아직 끝나지 않았습니다.')
            self.service.alert_settings(state).update(enabled=False, points=[])
            state['current'] = new_id
            state['cycles'][new_id] = dict(recipient_ids=[], status='pending', revision=0)
        self.service.store.transact(change)

    def cancel(self, cycle_id):
        at = stamp(self.clock())
        def change(state):
            if cycle_id != state['current']:
                raise Conflict('현재 코스가 변경되었습니다.')
            c = state['cycles'][cycle_id]
            self.service.alert_settings(state).update(enabled=False, points=[], changed_at=at)
            for old_id in state.get('_open_cycle_ids', state['cycles']):
                old = state['cycles'][old_id]
                if old['status'] in ('pending', 'claimed'):
                    old['notifications_cancelled'] = True
            # Never erase a completed result or an in-flight dispatch claim.
            if c['status'] == 'pending':
                c['status'] = 'cancelled'
                if c.get('detection') and c['detection']['phase'] != 'FINISHED':
                    c['detection'].update(phase='CANCELLED', cancelled_at=at)
            c['revision'] += 1
        self.service.store.transact(change)

    def observe_end(self, cycle_id):
        at = stamp(self.clock())
        def change(state):
            c = state['cycles'].get(cycle_id)
            if not c or not c.get('detection'):
                raise Conflict('시작한 코스가 없습니다.')
            d = c['detection']
            # A reference label only; never forces FINISHED or sends SMS.
            if not d.get('observed_end_at'):
                d['observed_end_at'] = at
        self.service.store.transact(change)

    def process(self, record):
        at, rms = summarize(record)
        age = (self.clock() - at).total_seconds()
        # Event-triggered processing can resume from a checkpoint after a cold start.
        # Replay real event times; dispatch separately rejects completions over 5 min old.
        if age < -5 or (self.processor == 'web' and age > RULES['max_age_seconds']):
            return 'stale'
        next_id = uuid4().hex
        def change(state):
            cid = state['current']
            if cid is None:
                return None
            c = state['cycles'][cid]
            d = c.get('detection')
            if not d or at < parse(d['started_at']):
                return None
            if d.get('processor', 'web') != self.processor:
                return None
            alerts = self.service.alert_settings(state)
            if not alerts['enabled']:
                return None
            if d['phase'] in ('FINISHED', 'CANCELLED'):
                # Recover unclaimed completions first; a crashed claim is never retried.
                # Start another cycle only from
                # sustained, recent activity, never from quiet or replayed batches.
                if d['phase'] != 'FINISHED' or c['status'] == 'pending':
                    return None
                if age > RULES['max_age_seconds'] or at <= parse(d['detected_end_at']):
                    return None
                t = at.timestamp()
                last = alerts.get('last_at')
                if last is not None and t <= last:
                    return None
                points = alerts.get('points', [])
                if last is not None and t - last > RULES['max_gap_seconds']:
                    points = []
                points = [p for p in points if p[0] >= t - 60] + [[t, rms]]
                alerts.update(points=points, last_at=t)
                active = [p for p in points if p[1] >= RULES['active_rms']]
                if len(points) < 10 or points[-1][0] - points[0][0] < 50 or len(active) < 6:
                    return None
                start = stamp(datetime.fromtimestamp(active[0][0], timezone.utc))
                d = dict(phase='RUNNING', started_at=start, activity_started_at=start,
                         activity_at=stamp(at), mode='unspecified', rules=dict(RULES),
                         processor=self.processor, points=points[:-1], activity_confirmed=True,
                         last_at=None, gaps=0, observed_end_at=None)
                c = dict(recipient_ids=list(alerts['recipient_ids']), status='pending',
                         revision=0, detection=d)
                cid = next_id
                state['cycles'][cid] = c
                state['current'] = cid
                alerts.update(points=[], last_at=None)

            rules = d['rules']
            last = parse(d['last_at']) if d['last_at'] else None
            if last and at <= last:
                return None
            if last and (at - last).total_seconds() > rules['max_gap_seconds']:
                d['points'] = []
                d['gaps'] += 1
            t = at.timestamp()
            points = [p for p in d['points'] if p[0] >= t - rules['quiet_seconds']]
            points.append([t, rms])
            d.update(points=points, last_at=stamp(at), rms=rms,
                     last_processed_at=stamp(self.clock()), record_age_seconds=age)
            recent = [p for p in points if p[0] >= t - 60]
            if (len(recent) >= 10 and recent[-1][0] - recent[0][0] >= 50
                    and sum(p[1] >= rules['active_rms'] for p in recent) >= 6):
                if not d['activity_confirmed']:
                    d['activity_at'] = stamp(at)
                    first = next(p[0] for p in recent if p[1] >= rules['active_rms'])
                    d['activity_started_at'] = stamp(datetime.fromtimestamp(first, timezone.utc))
                d['activity_confirmed'] = True
            ratio = sum(p[1] < rules['quiet_rms'] for p in points) / len(points)
            span = points[-1][0] - points[0][0]
            d['quiet_fraction'] = ratio
            d['window_seconds'] = span
            d['phase'] = 'RUNNING' if d['activity_confirmed'] else 'ARMED'
            if d['activity_confirmed'] and ratio >= .8:
                d['phase'] = 'QUIET_CANDIDATE'
            if (d['activity_confirmed'] and (at - parse(d.get('activity_started_at', d['started_at']))).total_seconds() >= rules['min_cycle_seconds']
                    and span >= rules['quiet_seconds'] - 6 and len(points) >= 33
                    and ratio >= rules['quiet_fraction']):
                d.update(phase='FINISHED', detected_end_at=stamp(at), decision=dict(
                    rms=rms, quiet_fraction=ratio, window_seconds=span, batches=len(points)))
                c['revision'] += 1
                # Snapshot selection before leaving the transaction; selection now locks.
                d['recipient_ids_at_finish'] = list(c['recipient_ids'])
                return cid
            return None
        completed = self.service.store.transact(change)
        if completed:
            return self.dispatch(completed)
        return 'processed'

    def dispatch(self, cycle_id):
        def expire(state):
            c = state['cycles'][cycle_id]
            if c.get('detection', {}).get('processor', 'web') != self.processor:
                return 'foreign'
            end = c.get('detection', {}).get('detected_end_at')
            if c['status'] == 'pending' and end and (self.clock() - parse(end)).total_seconds() > 300:
                c['status'] = 'missed'
                return True
            return False
        expired = self.service.store.transact(expire)
        if expired == 'foreign':
            return 'ignored'
        if expired:
            return 'missed'
        result = self.service.on_state_transition(cycle_id, 'RUNNING', 'FINISHED')
        if result in ('disabled', 'not_configured'):
            # Record non-send terminal result; later enabling SMS must not send old cycles.
            def change(state):
                c = state['cycles'][cycle_id]
                if c['status'] == 'pending':
                    c['status'] = result
            self.service.store.transact(change)
        return result

    def recover_finished(self):
        def read(state):
            return [cid for cid in state.get('_open_cycle_ids', state['cycles'])
                    for c in [state['cycles'][cid]]
                    if c.get('detection', {}).get('phase') == 'FINISHED' and c['status'] == 'pending'
                    and c['detection'].get('processor', 'web') == self.processor]
        for cid in self.service.store.transact(read):
            self.dispatch(cid)

    def submit(self, record):
        # Event Hubs callback never waits for state storage or SMS network calls.
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.stopping.clear()
                self.thread = threading.Thread(target=self.run, name='cycle-detector', daemon=True)
                self.thread.start()
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.last_error = '처리가 지연되고 있습니다. 최신 데이터를 기다립니다.'

    def run(self):
        while not self.stopping.is_set():
            try:
                record = self.queue.get(timeout=1)
                self.recover_finished()
                try:
                    self.process(record)
                    self.last_error = None
                finally:
                    self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as error:
                self.last_error = '완료 판정 처리를 다시 시도하고 있습니다.'
                log.warning('Cycle detection failed (%s)', type(error).__name__)
                self.stopping.wait(1)

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)

    def public_status(self):
        def read(state):
            c = state['cycles'].get(state['current'], {})
            d = c.get('detection', {})
            enabled = self.service.alert_settings(state)['enabled']
            last = d.get('last_at')
            fresh = last and (self.clock() - parse(last)).total_seconds() < RULES['max_age_seconds']
            return dict(phase=(d.get('phase', 'NOT_STARTED') if enabled else 'OFF'),
                        alerts_enabled=enabled, auto_repeat=enabled, fresh=bool(fresh),
                        sms_status=c.get('status', 'pending'),
                        **{k: d.get(k) for k in ['mode', 'started_at', 'activity_at', 'observed_end_at',
                            'detected_end_at', 'rms', 'quiet_fraction', 'window_seconds', 'gaps',
                            'processor', 'last_processed_at', 'record_age_seconds']})
        result = self.service.store.transact(read)
        result['error'] = self.last_error
        result['sms_ready'] = (self.service.env.get('SMS_ENABLED', 'false').lower() == 'true'
            and sms_configured(self.service.env))
        return result


def install_detector(app, service, consumer):
    processor = service.env.get('CYCLE_DETECTOR_BACKEND', 'web') if service else 'web'
    detector = CycleDetector(service, processor=processor) if service else None
    app.extensions['cycle_detector'] = detector
    if detector:
        # The Free web app controls cycles but does not own Function-backed detection.
        consumer.on_telemetry = detector.submit if processor == 'web' else None
        atexit.register(detector.stop)

    @app.get('/api/cycle')
    def cycle_status():
        if not detector:
            return jsonify(error='알림 서버 설정이 필요합니다.'), 503
        try:
            return jsonify(detector.public_status()), {'Cache-Control': 'no-store'}
        except Exception:
            return jsonify(error='코스 기록 저장소에 연결하지 못했습니다.'), 503
    return detector
