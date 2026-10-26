"""Per-device settings + per-cycle records, protected by a Table ETag transaction.

All writes include the settings row as a serialization fence. Routine detector
updates read only the settings and the active cycle, never the history partition.
Nested, bounded detector evidence is JSON; cycles are separate Table entities.
"""
import copy
import json
import re
from collections.abc import MutableMapping

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.data.tables import UpdateMode

SCHEMA = 1
OPEN = {'pending', 'claimed'}


def pack(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def check_entity(entity):
    # Table string properties are limited to 64 KiB (UTF-16).
    for value in entity.values():
        if isinstance(value, str) and len(value.encode('utf-16-le')) > 65536:
            raise ValueError('State property exceeds Table limit')
    if len(pack(entity).encode('utf-8')) > 900000:
        raise ValueError('State entity exceeds safe size')
    return entity


def cycle_key(cid):
    if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', cid):
        raise ValueError('invalid cycle id')
    return 'cycle|' + cid


FIELDS = {
    'status': ('Status', False), 'revision': ('Revision', False),
    'recipient_ids': ('RecipientIdsJson', True), 'detection': ('DetectionJson', True),
    'send_outcomes': ('SendOutcomesJson', True), 'sms_message': ('SmsMessage', False),
    'sms_variant': ('SmsVariant', False), 'encouragement': ('Encouragement', False),
    'notifications_cancelled': ('NotificationsCancelled', False),
}


def cycle_entity(pk, cid, cycle):
    row = dict(PartitionKey=pk, RowKey=cycle_key(cid), SchemaVersion=SCHEMA, Kind='cycle')
    for key, (field, encoded) in FIELDS.items():
        if key in cycle and cycle[key] is not None:
            row[field] = pack(cycle[key]) if encoded else cycle[key]
    extras = {k: v for k, v in cycle.items() if k not in FIELDS or v is None}
    row['ExtraJson'] = pack(extras)
    detection = cycle.get('detection', {})
    for key, field in [('phase', 'Phase'), ('started_at', 'StartedAt'),
                       ('activity_started_at', 'ActivityStartedAt'), ('detected_end_at', 'DetectedEndAt')]:
        if detection.get(key) is not None:
            row[field] = detection[key]
    return check_entity(row)


def decode_cycle(row):
    if row['SchemaVersion'] != SCHEMA or row['Kind'] != 'cycle':
        raise ValueError('Unsupported cycle schema')
    result = json.loads(row.get('ExtraJson', '{}'))
    for key, (field, encoded) in FIELDS.items():
        if field in row:
            result[key] = json.loads(row[field]) if encoded else row[field]
    return result


def root_entity(pk, state, open_ids, version, **metadata):
    row = dict(PartitionKey=pk, RowKey='settings', SchemaVersion=SCHEMA, Kind='settings',
               Ready=True, Version=version, CurrentCycle=state['current'] or '',
               OpenCycleIdsJson=pack(sorted(open_ids)),
               ExtraJson=pack({k: v for k, v in state.items()
                               if k not in ('current', 'cycles', 'alerts', '_open_cycle_ids')}))
    if 'alerts' in state:
        row['AlertsJson'] = pack(state['alerts'])
        row['AlertsEnabled'] = bool(state['alerts']['enabled'])
        row['RecipientIdsJson'] = pack(state['alerts'].get('recipient_ids', []))
    row.update(metadata)
    return check_entity(row)


class Cycles(MutableMapping):
    """Load individual cycles on demand; iteration is for explicit history export."""
    def __init__(self, client, pk):
        self.client, self.pk = client, pk
        self.cache, self.before, self.etags = {}, {}, {}

    def __getitem__(self, cid):
        if cid not in self.cache:
            try:
                row = self.client.get_entity(self.pk, cycle_key(cid))
            except ResourceNotFoundError as error:
                raise KeyError(cid) from error
            value = decode_cycle(row)
            self.cache[cid], self.before[cid], self.etags[cid] = value, pack(value), row.metadata['etag']
        return self.cache[cid]

    def __setitem__(self, cid, value):
        cycle_key(cid)
        if cid not in self.cache:
            try:
                self[cid]
            except KeyError:
                self.before[cid], self.etags[cid] = None, None
        self.cache[cid] = value

    def __delitem__(self, cid):
        raise ValueError('Cycle history deletion is not supported')

    def __iter__(self):
        rows = self.client.query_entities(
            "PartitionKey eq @pk and RowKey ge 'cycle|' and RowKey lt 'cycle}'",
            parameters={'pk': self.pk})
        for row in rows:
            cid = row['RowKey'][6:]
            if cid not in self.cache:
                value = decode_cycle(row)
                self.cache[cid], self.before[cid], self.etags[cid] = value, pack(value), row.metadata['etag']
        return iter(list(self.cache))

    def __len__(self):
        return len(list(iter(self)))


class TableStore:
    def __init__(self, get_table, partition):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', partition or ''):
            raise ValueError('valid notification partition required')
        self.get_table, self.partition = get_table, partition

    def transact(self, change):
        from notifications import Conflict
        client, pk = self.get_table(), self.partition
        for _ in range(8):
            # Never silently recreate missing state: that could discard send claims.
            root = client.get_entity(pk, 'settings')
            if root.get('SchemaVersion') != SCHEMA or not root.get('Ready'):
                raise RuntimeError('Notification state migration is not complete')
            cycles = Cycles(client, pk)
            state = json.loads(root.get('ExtraJson', '{}'))
            state.update(current=root['CurrentCycle'] or None, cycles=cycles,
                         _open_cycle_ids=json.loads(root['OpenCycleIdsJson']))
            if 'AlertsJson' in root:
                state['alerts'] = json.loads(root['AlertsJson'])
            original = pack({k: v for k, v in state.items() if k != 'cycles'})
            try:
                result = change(state)
            except KeyError:
                if client.get_entity(pk, 'settings').metadata['etag'] != root.metadata['etag']:
                    continue
                raise
            changed = [cid for cid, value in cycles.cache.items() if pack(value) != cycles.before[cid]]
            if not changed and original == pack({k: v for k, v in state.items() if k != 'cycles'}):
                # Separate reads must belong to the same logical version.
                if client.get_entity(pk, 'settings').metadata['etag'] != root.metadata['etag']:
                    continue
                return result
            open_ids = set(state['_open_cycle_ids'])
            for cid in changed:
                if cycles.cache[cid]['status'] in OPEN:
                    open_ids.add(cid)
                else:
                    open_ids.discard(cid)
            metadata = {k: root[k] for k in ('MigrationSourceETag',) if k in root}
            new_root = root_entity(pk, state, open_ids, root['Version'] + 1, **metadata)
            operations = [('update', new_root, dict(mode=UpdateMode.REPLACE,
                etag=root.metadata['etag'], match_condition=MatchConditions.IfNotModified))]
            for cid in changed:
                entity = cycle_entity(pk, cid, cycles.cache[cid])
                if cycles.etags[cid] is None:
                    operations.append(('create', entity))
                else:
                    operations.append(('update', entity, dict(mode=UpdateMode.REPLACE,
                        etag=cycles.etags[cid], match_condition=MatchConditions.IfNotModified)))
            if len(operations) > 100 or len(pack([op[1] for op in operations]).encode('utf-8')) > 3500000:
                raise ValueError('State transaction exceeds safe Table batch limits')
            try:
                client.submit_transaction(operations)
                return result
            except HttpResponseError as error:
                # Conflicting claims re-read committed state. Transport errors do NOT
                # retry here: the commit outcome may be unknown, so no SMS is sent.
                if error.status_code in (409, 412):
                    continue
                raise
        raise Conflict('설정이 동시에 변경되었습니다. 다시 불러온 후 시도하세요.')

    def snapshot(self):
        def read(state):
            return {**{k: copy.deepcopy(v) for k, v in state.items()
                       if k not in ('cycles', '_open_cycle_ids')},
                    'cycles': {cid: copy.deepcopy(c) for cid, c in state['cycles'].items()}}
        return self.transact(read)

    def import_legacy(self, state, source_etag):
        """Offline migration only. Stop both writers first; never overwrite ready state.

        Retryable staging is fenced by Ready=False. Source ETag must match on resume.
        Verification precedes the final settings-row commit that enables app access.
        """
        client, pk = self.get_table(), self.partition
        if state['current'] is not None and state['current'] not in state['cycles']:
            raise ValueError('Current cycle missing from migration source')
        rows = [cycle_entity(pk, cid, c) for cid, c in state['cycles'].items()]
        open_ids = [cid for cid, c in state['cycles'].items() if c['status'] in OPEN]
        ready = root_entity(pk, state, open_ids, 0, MigrationSourceETag=source_etag)
        try:
            root = client.get_entity(pk, 'settings')
        except ResourceNotFoundError:
            staging = {**ready, 'Ready': False}
            client.create_entity(staging)
            root = client.get_entity(pk, 'settings')
        if root.get('Ready'):
            raise ValueError('Table state already active; refusing to overwrite it')
        if root.get('MigrationSourceETag') != source_etag:
            raise ValueError('Source changed since migration staging began')
        for row in rows:
            try:
                client.create_entity(row)
            except HttpResponseError as error:
                if error.status_code != 409:
                    raise
                old = client.get_entity(pk, row['RowKey'])
                if decode_cycle(old) != decode_cycle(row):
                    raise ValueError('Staged cycle differs; refusing to overwrite') from None
        for row in rows:
            if decode_cycle(client.get_entity(pk, row['RowKey'])) != decode_cycle(row):
                raise ValueError('Migration verification failed')
        client.update_entity(ready, mode=UpdateMode.REPLACE, etag=root.metadata['etag'],
                             match_condition=MatchConditions.IfNotModified)
        if self.snapshot() != state:
            raise ValueError('Migrated snapshot differs from source')
        return len(rows)
