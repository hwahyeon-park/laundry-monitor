"""Read-only endpoint probe; prints metadata only, never bodies or keys.

Run from the project root. By default uses EVENTHUB_CONNECTION_STRING.
--azure-hub laundry-hub --resource-group laundry reads the existing service
policy through your Azure CLI login. It does not create routes/groups.
Use a dedicated probe group once one exists. The default $Default connection
is non-exclusive and has no checkpoint store (do not run many copies).
"""
import argparse
import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone

from azure.eventhub import EventHubConsumerClient, TransportType

logging.getLogger('azure').setLevel(logging.CRITICAL)
parser = argparse.ArgumentParser()
parser.add_argument('--azure-hub')
parser.add_argument('--resource-group', default='laundry')
parser.add_argument('--consumer-group', default='$Default')
parser.add_argument('--seconds', type=int, default=30)
args = parser.parse_args()
connection = os.getenv('EVENTHUB_CONNECTION_STRING')
if args.azure_hub:
    env = dict(os.environ, AZURE_LOGGING_ENABLE_LOG_FILE='false', AZURE_CORE_COLLECT_TELEMETRY='false')
    def az(*arguments):
        result = subprocess.run(['az', *arguments, '-o', 'json'], env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Azure CLI lookup failed; check login and resource permissions')
        return json.loads(result.stdout)
    endpoint = az('iot', 'hub', 'show', '-g', args.resource_group, '-n', args.azure_hub,
                  '--query', 'properties.eventHubEndpoints.events')
    policy = az('iot', 'hub', 'policy', 'show', '-g', args.resource_group, '--hub-name', args.azure_hub,
                '-n', 'service')
    connection = f"Endpoint={endpoint['endpoint']};SharedAccessKeyName=service;SharedAccessKey={policy['primaryKey']};EntityPath={endpoint['path']}"
if not connection:
    parser.error('Set EVENTHUB_CONNECTION_STRING or specify --azure-hub')
client = EventHubConsumerClient.from_connection_string(
    connection, consumer_group=args.consumer_group, transport_type=TransportType.AmqpOverWebsocket,
    retry_total=1, auth_timeout=15)
count = 0
failures = []
seen = set()
started = datetime.now(timezone.utc)
lock = threading.Lock()


def on_event(context, event):
    global count
    with lock:
        seen.add(context.partition_id)
        if event is not None:
            count += 1
            props = event.system_properties
            device = props.get(b'iothub-connection-device-id', props.get('iothub-connection-device-id', ''))
            if isinstance(device, bytes):
                device = device.decode('utf-8', errors='replace')
            print(json.dumps({'partition': context.partition_id, 'sequence': event.sequence_number,
                              'enqueuedTime': str(event.enqueued_time), 'deviceId': device}), flush=True)


def on_error(context, error):
    failures.append(type(error).__name__)


def receive():
    try:
        client.receive(on_event, starting_position=started, max_wait_time=5, on_error=on_error)
    except Exception as error:
        failures.append(type(error).__name__)


try:
    partitions = client.get_partition_ids()
    print(json.dumps({'authenticated': True, 'partitions': partitions, 'started': str(started)}), flush=True)
    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    threading.Event().wait(max(5, min(args.seconds, 60)))
    client.close()
    thread.join(timeout=5)
    print(json.dumps({'events': count, 'activePartitions': sorted(seen), 'errors': failures}), flush=True)
    if not count:
        print('No new messages observed. Check device publishing and explicit events route; this is not proof that the device is offline.')
except Exception as error:
    print(json.dumps({'authenticated': False, 'errorType': type(error).__name__}))
    raise SystemExit(1)
finally:
    client.close()
