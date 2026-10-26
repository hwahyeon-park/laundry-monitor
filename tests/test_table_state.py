import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from notifications import NotificationService, make_store
from table_state import TableStore, cycle_entity, decode_cycle
from cycle_detector import CycleDetector
from test_cycle_detector import BASE, record


class Entity(dict):
    def __init__(self, value, etag):
        super().__init__(copy.deepcopy(value))
        self.metadata = {'etag': str(etag)}


class MemoryTable:
    """Azure contract fake: atomic batch + strict ETag validation, shared by clients."""
    def __init__(self):
        self.rows, self.revision = {}, 0
        self.lock = threading.RLock()
        self.queries = self.writes = 0
        self.committed_then_timeout = False
        self.before_submit = None

    def get_entity(self, partition_key, row_key):
        with self.lock:
            key = partition_key, row_key
            if key not in self.rows:
                raise ResourceNotFoundError('not found')
            return copy.deepcopy(self.rows[key])

    def create_entity(self, row):
        self.submit_transaction([('create', row)])

    def update_entity(self, row, **kwargs):
        self.submit_transaction([('update', row, kwargs)])

    def query_entities(self, query_filter, parameters):
        self.queries += 1
        with self.lock:
            return [copy.deepcopy(row) for (pk, rk), row in self.rows.items()
                    if pk == parameters['pk'] and rk.startswith('cycle|')]

    def submit_transaction(self, operations):
        hook, self.before_submit = self.before_submit, None
        if hook:
            hook()
        with self.lock:
            for op in operations:
                mode, row = op[:2]; args = op[2] if len(op) > 2 else {}
                key = row['PartitionKey'], row['RowKey']
                if mode == 'create' and key in self.rows:
                    error=HttpResponseError('exists');error.status_code=409;raise error
                if mode == 'update' and (key not in self.rows or
                        args.get('etag') != self.rows[key].metadata['etag']):
                    error=HttpResponseError('etag mismatch');error.status_code=412;raise error
            self.revision += 1
            for op in operations:
                row=op[1]; self.rows[row['PartitionKey'],row['RowKey']] = Entity(row,self.revision)
            self.writes += len(operations)
            if self.committed_then_timeout:
                self.committed_then_timeout=False
                raise TimeoutError('commit response lost')


def initial_state(count=0):
    return {'current': None, 'cycles': {f'old{i}': dict(status='accepted', revision=1, recipient_ids=['a'])
                                       for i in range(count)}}


class TableStateTests(unittest.TestCase):
    def setUp(self):
        self.client = MemoryTable()
        self.store = TableStore(lambda: self.client, 'device')
        self.store.import_legacy(initial_state(200), 'source-etag')
        self.client.queries=self.client.writes=0
        self.post=Mock(return_value=SimpleNamespace(status_code=200))
        self.env=dict(SMS_ENABLED='true', SMS_PROVIDER='textbee',TEXTBEE_API_KEY='fake',TEXTBEE_DEVICE_ID='a'*24)
        self.recipients={'a':{'name':'A','phone':'+447700900001'},'b':{'name':'B','phone':'+447700900002'}}
        self.service=NotificationService(self.store,self.recipients,self.env,self.post)

    def select(self):
        c=self.service.current()
        CycleDetector(self.service,lambda:BASE,processor='function').select_recipients(
            c['cycle_id'],['a','b'],c['revision'])
        return c['cycle_id']

    def test_routine_processing_only_touches_settings_and_active_cycle(self):
        cid=self.select(); self.client.writes=0
        detector=CycleDetector(self.service,lambda: BASE+timedelta(seconds=10),processor='function')
        detector.recover_finished()
        detector.process(record(BASE + timedelta(seconds=5), .02))
        detector.public_status()
        self.assertEqual(self.client.queries,0)
        self.assertEqual(self.client.writes,2)
        self.assertEqual(self.store.transact(lambda s: s['_open_cycle_ids']),[cid])

    def test_concurrent_finish_only_one_claim_and_no_resend_on_restart(self):
        cid=self.select()
        services=[NotificationService(TableStore(lambda:self.client,'device'),self.recipients,self.env,self.post)
                  for _ in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda s:s.on_state_transition(cid,'RUNNING','FINISHED'),services))
        self.assertEqual(results.count('accepted'),1)
        self.assertEqual(self.post.call_count,2)
        self.assertEqual(len({c.kwargs['json']['message'] for c in self.post.call_args_list}),1)
        self.assertEqual(services[0].on_state_transition(cid,'RUNNING','FINISHED'),'skipped')
        self.assertEqual(self.store.transact(lambda s:s['_open_cycle_ids']),[])

    def test_disable_wins_against_competing_claim(self):
        cid=self.select()
        self.client.before_submit=lambda:CycleDetector(self.service,lambda:BASE).cancel(cid)
        self.assertEqual(self.service.on_state_transition(cid,'RUNNING','FINISHED'),'skipped')
        self.post.assert_not_called()
        self.assertFalse(self.service.current()['alerts_enabled'])
        self.assertEqual(self.client.queries,0)

    def test_unknown_commit_does_not_send_or_retry_claim(self):
        cid=self.select();self.client.committed_then_timeout=True
        with self.assertRaises(TimeoutError):
            self.service.on_state_transition(cid,'RUNNING','FINISHED')
        self.post.assert_not_called()
        self.assertEqual(self.service.current()['status'],'claimed')
        self.assertEqual(self.service.on_state_transition(cid,'RUNNING','FINISHED'),'skipped')
        self.post.assert_not_called()

    def test_read_retries_when_root_changes_between_reads(self):
        cid=self.select(); count=[0]
        def read(state):
            c=state['cycles'][state['current']]
            if count[0]==0:
                count[0]+=1
                self.store.transact(lambda s:s['cycles'][cid].update(revision=50))
            return c['revision']
        self.assertEqual(self.store.transact(read),50)

    def test_export_preserves_history_and_unknown_legacy_fields(self):
        before=self.store.snapshot()
        self.assertEqual(before, initial_state(200))
        self.assertEqual(len(self.store.transact(lambda s:dict(s['cycles'].items()))),200)
        value=dict(status='claimed',revision=8,recipient='a',detection={'phase':'FINISHED','points':[[1,.01]]},
                   encouragement='한글',sms_message='message',send_outcomes={'a':'accepted'},future=None)
        self.assertEqual(decode_cycle(cycle_entity('device','legacy',value)),value)

    def test_oversized_property_is_rejected_before_commit(self):
        cid=self.select();writes=self.client.writes
        with self.assertRaises(ValueError):
            self.store.transact(lambda s:s['cycles'][cid].update(sms_message='x'*40000))
        self.assertEqual(self.client.writes,writes)

    def test_import_preserves_current_disabled_claimed_and_message(self):
        target=TableStore(lambda:self.client,'another-device')
        state={'current':'one','alerts':{'enabled':False,'recipient_ids':['a'],'points':[]},
               'cycles':{'one':dict(status='claimed',revision=5,recipient_ids=['a'],sms_message='한글',
                                    detection={'phase':'FINISHED','last_at':BASE.isoformat()})}}
        target.import_legacy(state,'new-source')
        self.assertEqual(target.snapshot(),state)
        with self.assertRaises(ValueError):target.import_legacy(initial_state(),'new-source')
        self.assertEqual(target.snapshot(),state)

    def test_missing_or_staging_state_fails_closed(self):
        missing=TableStore(lambda:self.client,'missing')
        with self.assertRaises(ResourceNotFoundError):missing.transact(lambda s:s)
        root=self.client.get_entity('device','settings');root['Ready']=False
        self.client.update_entity(root,etag=root.metadata['etag'])
        with self.assertRaises(RuntimeError):self.service.current()

    def test_partial_import_can_resume_without_overwriting(self):
        target=TableStore(lambda:self.client,'partial')
        original=self.client.create_entity; count=[0]
        def fail(row):
            count[0]+=1
            if count[0]==3:raise TimeoutError('interrupted')
            original(row)
        self.client.create_entity=fail
        with self.assertRaises(TimeoutError):target.import_legacy(initial_state(3),'etag')
        with self.assertRaises(RuntimeError):target.transact(lambda s:s)
        self.client.create_entity=original
        self.assertEqual(target.import_legacy(initial_state(3),'etag'),3)
        self.assertEqual(target.snapshot(),initial_state(3))

    def test_store_selection_cannot_silently_fall_back(self):
        env=dict(WEBSITE_HOSTNAME='test',NOTIFICATION_STORAGE_ACCOUNT='exampleaccount',DEVICE_ID='device')
        self.assertIsInstance(make_store(None,env),TableStore)
        for value in ('bad','file'):
            with self.assertRaises(ValueError):make_store(None,{**env,'NOTIFICATION_STORAGE':value})
