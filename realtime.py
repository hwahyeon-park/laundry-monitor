"""Single-process Event Hubs receiver with bounded SSE replay/fan-out."""
import atexit
import json
import logging
import math
import os
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from flask import Response, jsonify, request

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def frame(kind, data, event_id=None):
    prefix = f'id: {event_id}\n' if event_id else ''
    return prefix + f'event: {kind}\ndata: ' + json.dumps(data, allow_nan=False) + '\n\n'


def valid_payload(payload):
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not isinstance(payload, dict):
        return False
    samples = payload.get('samples')
    return (number(payload.get('temperature')) and number(payload.get('pressure'))
            and isinstance(samples, list) and 0 < len(samples) <= 2000
            and type(payload.get('sampleCount')) is int and payload['sampleCount'] == len(samples)
            and all(isinstance(s, dict) and all(number(s.get(k)) for k in ('t', 'x', 'y', 'z')) for s in samples))


class LiveBroker:
    def __init__(self, stale_seconds=30, capacity=120, max_clients=8):
        if stale_seconds < 10:
            raise ValueError('TELEMETRY_STALE_SECONDS must be >= 10')
        self.stale_seconds = stale_seconds
        self.max_clients = max_clients
        self.condition = threading.Condition(threading.RLock())
        self.events = deque(maxlen=capacity)
        self.latest = self.latest_time = self.received_monotonic = None
        self.epoch = uuid.uuid4().hex
        self.sequence = self.clients = 0
        self.consumer = 'not_started'

    def set_consumer(self, state):
        with self.condition:
            if self.consumer != state:
                self.consumer = state
                self.condition.notify_all()

    def publish(self, record, enqueued):
        with self.condition:
            if self.latest_time is not None and enqueued <= self.latest_time:
                return False
            self.latest, self.latest_time = record, enqueued
            self.received_monotonic = time.monotonic()
            self.sequence += 1
            self.events.append((f'{self.epoch}:{self.sequence}', record))
            self.condition.notify_all()
            return True

    def status(self):
        with self.condition:
            age = None if self.latest_time is None else max(0, (now() - self.latest_time).total_seconds())
            elapsed = None if self.received_monotonic is None else time.monotonic() - self.received_monotonic
            state = 'NO DATA' if age is None else (
                'NO RECENT DATA' if max(age, elapsed) >= self.stale_seconds else 'RECEIVING DATA')
            return {'dataStatus': state, 'consumerStatus': self.consumer,
                    'lastEnqueuedTime': iso(self.latest_time) if self.latest_time else None,
                    'ageSeconds': age, 'staleSeconds': self.stale_seconds, 'serverTime': iso(now())}

    def reserve(self):
        with self.condition:
            if self.clients >= self.max_clients:
                return False
            self.clients += 1
            return True

    def release(self):
        with self.condition:
            self.clients -= 1

    def batch_after(self, cursor):
        with self.condition:
            events = list(self.events)
            ids = [item[0] for item in events]
            if cursor in ids:
                return events[ids.index(cursor) + 1:], False
            # On restart or replay expiry send a latest snapshot, not fake history.
            return events[-1:], bool(cursor)

    def stream(self, cursor, heartbeat=10):
        yield 'retry: 3000\n\n'
        first = True
        while True:
            with self.condition:
                batch, reset = self.batch_after(cursor)
                if not first and not batch and not reset:
                    self.condition.wait(timeout=heartbeat)
                    batch, reset = self.batch_after(cursor)
                status = self.status()
            first = False
            if reset:
                yield 'id:\nevent: reset\ndata: {"reason":"replay_unavailable"}\n\n'
                cursor = None
            for event_id, record in batch:
                yield frame('telemetry', {'record': record}, event_id)
                cursor = event_id
            # Heartbeats never update the telemetry timestamp.
            yield frame('status', status)


class Consumer:
    def __init__(self, broker, env=None):
        self.broker = broker
        self.env = os.environ if env is None else env
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = self.client = None
        self.on_telemetry = None
        self.positions = {}
        self.partition_seen = {}
        self.bootstrap = now() - timedelta(seconds=30)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            if self.env.get('REALTIME_ENABLED', 'true').lower() != 'true':
                self.broker.set_consumer('disabled')
                return
            mode = self.env.get('EVENTHUB_AUTH_MODE', 'connection_string')
            configured = (mode == 'connection_string' and self.env.get('EVENTHUB_CONNECTION_STRING')) or (
                mode == 'managed_identity' and self.env.get('EVENTHUB_NAMESPACE') and self.env.get('EVENTHUB_NAME'))
            if not self.env.get('DEVICE_ID') or not configured:
                self.broker.set_consumer('not_configured')
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.run, name='telemetry-consumer', daemon=True)
            self.thread.start()

    def make_client(self):
        from azure.eventhub import EventHubConsumerClient, TransportType
        credential = None
        options = {'consumer_group': self.env.get('EVENTHUB_CONSUMER_GROUP', 'laundry-web'),
                   'transport_type': TransportType.AmqpOverWebsocket, 'retry_total': 3}
        if self.env.get('EVENTHUB_AUTH_MODE', 'connection_string') == 'managed_identity':
            from azure.identity import ManagedIdentityCredential
            client_id = self.env.get('AZURE_CLIENT_ID')
            credential = ManagedIdentityCredential(**({'client_id': client_id} if client_id else {}))
            client = EventHubConsumerClient(self.env['EVENTHUB_NAMESPACE'], self.env['EVENTHUB_NAME'],
                                           credential=credential, **options)
        else:
            client = EventHubConsumerClient.from_connection_string(self.env['EVENTHUB_CONNECTION_STRING'], **options)
        return client, credential

    def on_event(self, context, event):
        partition = context.partition_id
        with self.lock:
            self.partition_seen[partition] = time.monotonic()
            if all(t and time.monotonic() - t < 30 for t in self.partition_seen.values()):
                self.broker.set_consumer('connected')
        if event is None:
            return
        sequence = event.sequence_number
        if sequence <= self.positions.get(partition, -1):
            return
        try:
            # Routed custom Event Hubs may expose IoT metadata as properties.
            def metadata(name):
                for props in (event.system_properties, getattr(event, 'properties', None) or {}):
                    value = props.get(name.encode(), props.get(name))
                    if value is not None:
                        return value
                return None
            device = metadata('iothub-connection-device-id')
            if isinstance(device, bytes):
                device = device.decode('utf-8')
            if device != self.env['DEVICE_ID']:
                return
            body = event.body_as_str(encoding='UTF-8')
            if len(body) > 262144:
                raise ValueError('oversized telemetry')
            payload = json.loads(body)
            if not valid_payload(payload):
                raise ValueError('invalid telemetry shape')
            enqueued = metadata('iothub-enqueuedtime')
            if isinstance(enqueued, bytes):
                enqueued = enqueued.decode('utf-8')
            if isinstance(enqueued, str):
                enqueued = datetime.fromisoformat(enqueued.replace('Z', '+00:00'))
            elif isinstance(enqueued, (int, float)):
                enqueued = datetime.fromtimestamp(enqueued / 1000, timezone.utc)
            if enqueued is None:
                enqueued = event.enqueued_time
            if not isinstance(enqueued, datetime) or enqueued.tzinfo is None or enqueued > now() + timedelta(seconds=5):
                raise ValueError('invalid enqueue time')
            record = {'payload': payload, 'enqueuedTime': iso(enqueued), 'receivedAt': iso(now()),
                      'deviceId': device, 'partitionId': partition, 'sequenceNumber': sequence}
            published = self.broker.publish(record, enqueued)
            if published and self.on_telemetry:
                try:
                    self.on_telemetry(record)
                except Exception as error:
                    log.warning('Cycle callback failed (%s)', type(error).__name__)
        except (ValueError, TypeError, UnicodeError, OverflowError):
            log.warning('Invalid telemetry skipped (partition %s, sequence %s)', partition, sequence)
        finally:
            # Also advance past other devices and poison messages. No Blob I/O.
            self.positions[partition] = sequence

    def on_error(self, context, error):
        with self.lock:
            if context:
                self.partition_seen[context.partition_id] = 0
            else:
                self.partition_seen = dict.fromkeys(self.partition_seen, 0)
        self.broker.set_consumer('retrying')
        log.warning('Event Hubs receiver retry: %s', type(error).__name__)

    def run(self):
        delay = 1
        while not self.stop_event.is_set():
            credential = None
            try:
                self.broker.set_consumer('connecting')
                client, credential = self.make_client()
                with self.lock:
                    self.client = client
                with client:
                    if self.stop_event.is_set():
                        break
                    partitions = client.get_partition_ids()
                    with self.lock:
                        self.partition_seen = dict.fromkeys(partitions, 0)
                    positions = {p: self.positions.get(p, self.bootstrap) for p in partitions}
                    if self.stop_event.is_set():
                        break
                    client.receive(self.on_event, starting_position=positions, starting_position_inclusive=False,
                                   max_wait_time=10, prefetch=50, on_error=self.on_error)
            except Exception as error:
                self.broker.set_consumer('retrying')
                # No exception bodies: they may include credentials/connection details.
                log.warning('Consumer reconnect: %s', type(error).__name__)
            finally:
                if credential:
                    credential.close()
                with self.lock:
                    self.client = None
            if self.stop_event.wait(delay + random.random()):
                break
            delay = min(delay * 2, 30)
        self.broker.set_consumer('stopped')

    def stop(self):
        self.stop_event.set()
        with self.lock:
            client = self.client
        if client:
            client.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)


def install_realtime(app):
    broker = LiveBroker(stale_seconds=int(os.getenv('TELEMETRY_STALE_SECONDS', '30')))
    consumer = Consumer(broker)
    app.extensions['realtime'] = {'broker': broker, 'consumer': consumer}
    atexit.register(consumer.stop)

    @app.get('/api/telemetry')
    def live_snapshot():
        consumer.start()
        with broker.condition:
            result = {'records': [broker.latest] if broker.latest else [], 'status': broker.status()}
        return jsonify(result), {'Cache-Control': 'no-store'}

    @app.get('/api/stream')
    def live_stream():
        consumer.start()
        if not broker.reserve():
            return Response('Too many live clients', status=503, headers={'Retry-After': '10'})
        response = Response(broker.stream(request.headers.get('Last-Event-ID')), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache, no-store, no-transform', 'X-Accel-Buffering': 'no'})
        response.call_on_close(broker.release)
        return response

    @app.get('/api/status')
    def live_status():
        return jsonify(broker.status()), {'Cache-Control': 'no-store'}

    return consumer
