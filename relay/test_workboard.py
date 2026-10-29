import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from store import RelayStore
from workboard import Workboard

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'plugin'/'scripts'))
from workboard_worker import Worker, configured_workers
import agent_sessions
import jobs
from agent_adapters import AdapterRegistry, AgentCapabilities


class Native:
    adapter_id = 'test-native'

    def __init__(self):
        self.started = []
        self.states = {}

    def capabilities(self):
        return AgentCapabilities(cancel=True)

    def start(self, goal, **kwargs):
        sid = 'native-'+str(len(self.started))
        self.started.append((goal, kwargs))
        self.states[sid] = 'RUNNING'
        return sid

    def status(self, sid):
        return {'status': self.states[sid], 'worker_alive': self.states[sid] == 'RUNNING'}


class WorkboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RelayStore(str(Path(self.tmp.name)/'relay.db'))
        self.board = Workboard(self.store)
        self.a = self.store.pair_device(self.store.create_pairing_code('owner')['code'], 'A')
        self.b = self.store.pair_device(self.store.create_pairing_code('owner')['code'], 'B')
        self.other = self.store.pair_device(self.store.create_pairing_code('other')['code'], 'Other')
        self.counter = 0

    def create(self, **overrides):
        self.counter += 1
        args = dict(device_id=self.a['device_id'], agent_id='agent-a', adapter_id='test-native',
                    project='demo', local_device='main', goal='Check the fixed revision',
                    source_revision='a'*40, idempotency_key=str(self.counter),
                    exclusive_key='workspace-'+str(self.counter))
        args.update(overrides)
        return self.board.create('owner', **args)

    def claim(self, device=None, agent='agent-a'):
        return self.board.claim('owner', (device or self.a)['device_id'], agent)

    def update(self, item, operation, **kwargs):
        return self.board.update('owner', item['device_id'], item['agent_id'], item['id'], item['fence'],
                                 operation=operation, **kwargs)

    def finish(self, item):
        self.update(item, 'started', session_id='session', runtime_job_id='job')
        return self.update(item, 'finished')

    def accept(self, item):
        return self.board.verify('owner', item['id'], fence=item['fence'], source_revision=item['source_revision'],
                                 evidence_refs=['test://checked-results'])

    def test_atomic_claim_across_independent_connections(self):
        item = self.create()
        def claim(_):
            return Workboard(RelayStore(self.store.path)).claim('owner', self.a['device_id'], 'agent-a')
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(claim, range(24)))
        self.assertEqual([r['id'] for r in claims if r], [item['id']])
        self.assertEqual(next(r for r in claims if r)['fence'], 1)

    def test_machine_agent_and_owner_are_exact(self):
        self.create()
        self.assertIsNone(self.claim(self.b))
        self.assertIsNone(self.claim(agent='another-agent'))
        self.assertIsNone(self.board.claim('other', self.other['device_id'], 'agent-a'))
        claimed = self.claim()
        for device, agent, fence in [(self.b['device_id'], 'agent-a', 1),
                                     (self.a['device_id'], 'another-agent', 1),
                                     (self.a['device_id'], 'agent-a', 0)]:
            with self.assertRaises(PermissionError):
                self.board.update('owner', device, agent, claimed['id'], fence, operation='heartbeat')
        with self.assertRaises(KeyError):
            self.board.verify('other', claimed['id'], fence=1, source_revision='a'*40, evidence_refs=['x'])

    def test_dependency_requires_verified_success_and_unread_receipt_does_not_gate(self):
        first = self.create()
        second = self.create(depends_on=[first['id']])
        self.assertEqual(second['status'], 'WAITING')
        active = self.claim()
        self.finish(active)
        self.assertIsNone(self.claim())
        actions = self.board.human_actions('owner')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['item_id'], active['id'])
        self.assertIn('manual acceptance', actions[0]['reason'])
        with self.assertRaises(ValueError):
            self.board.verify('owner', active['id'], fence=1, source_revision='a'*40, evidence_refs=[])
        self.assertEqual(len(self.board.human_actions('owner')), 1)
        self.accept(active)
        self.assertEqual(self.board.human_actions('owner'), [])
        self.assertEqual(self.claim()['id'], second['id'])

    def test_human_block_does_not_freeze_independent_work_and_resolution_is_single_use(self):
        self.create()
        next_item = self.create()
        active = self.claim()
        self.update(active, 'blocked', reason='Owner decision needed')
        self.assertEqual(self.claim()['id'], next_item['id'])
        action = self.board.human_actions('owner')[0]
        with self.assertRaises(ValueError):
            self.board.resolve('owner', action['id'], decision='retry', resolution='go')
        self.board.resolve('owner', action['id'], decision='retry', resolution='checked process stopped', stopped=True)
        with self.assertRaises(PermissionError):
            self.board.resolve('owner', action['id'], decision='retry', resolution='replay', stopped=True)

    def test_expired_lease_is_not_automatic_reexecution_and_old_fence_cannot_write(self):
        self.create()
        old = self.claim()
        with self.store.db() as db:
            db.execute('UPDATE workboard_items SET lease_until=0 WHERE id=?', (old['id'],))
        self.assertIsNone(self.claim())
        action = self.board.human_actions('owner')[0]
        self.board.resolve('owner', action['id'], decision='retry', resolution='native stopped, no external effect', stopped=True)
        new = self.claim()
        self.assertEqual(new['fence'], old['fence']+1)
        for operation in ('heartbeat', 'started', 'finished', 'blocked'):
            with self.assertRaises(PermissionError):
                self.update(old, operation)

    def test_exclusive_resource_remains_reserved_during_human_and_verification_wait(self):
        self.create(exclusive_key='repo')
        self.create(device_id=self.b['device_id'], agent_id='agent-b', exclusive_key='repo')
        active = self.claim()
        self.finish(active)
        self.assertIsNone(self.claim(self.b, 'agent-b'))
        self.accept(active)
        self.assertIsNotNone(self.claim(self.b, 'agent-b'))

    def test_independent_machines_can_claim_concurrently(self):
        first = self.create()
        second = self.create(device_id=self.b['device_id'], agent_id='agent-b')
        self.assertEqual(self.claim()['id'], first['id'])
        self.assertEqual(self.claim(self.b, 'agent-b')['id'], second['id'])

    def test_failed_predecessor_becomes_human_action(self):
        first = self.create()
        child = self.create(depends_on=[first['id']])
        active = self.claim()
        self.update(active, 'blocked', reason='native failed')
        action = self.board.human_actions('owner')[0]
        self.board.resolve('owner', action['id'], decision='fail', resolution='confirmed stopped', stopped=True)
        self.assertIsNone(self.claim())
        action = self.board.human_actions('owner')[0]
        self.assertEqual(action['item_id'], child['id'])

    def test_idempotency_conflict_and_unknown_cross_owner_dependencies(self):
        first = self.create(idempotency_key='same')
        args = {k: first[k] for k in ('device_id','agent_id','adapter_id','project','local_device','goal',
                                       'source_revision','idempotency_key','exclusive_key')}
        self.assertEqual(self.board.create('owner', **args)['id'], first['id'])
        with self.assertRaises(ValueError):
            self.board.create('owner', **{**args, 'goal': 'different'})
        with self.assertRaises(KeyError):
            self.create(depends_on=['unknown'])
        with self.assertRaises(ValueError):
            self.create(source_revision='main')

    def test_revoked_device_cannot_renew(self):
        self.create()
        active = self.claim()
        self.store.revoke_device('owner', self.a['device_id'])
        with self.assertRaises(PermissionError):
            self.update(active, 'heartbeat')

    def test_stale_acceptance_and_human_action_revision(self):
        self.create()
        active = self.claim()
        self.finish(active)
        with self.assertRaises(PermissionError):
            self.board.verify('owner', active['id'], fence=0, source_revision='a'*40, evidence_refs=['x'])
        # Fault injection: even an out-of-band revision mutation cannot reuse an old decision.
        with self.store.db() as db:
            db.execute("UPDATE workboard_items SET status='RUNNING',lease_until=9999999999 WHERE id=?", (active['id'],))
        self.update(active, 'blocked', reason='input')
        action = self.board.human_actions('owner')[0]
        with self.store.db() as db:
            db.execute("UPDATE workboard_items SET source_revision=? WHERE id=?", ('b'*40, active['id']))
        with self.assertRaises(PermissionError):
            self.board.resolve('owner', action['id'], decision='retry', resolution='old', stopped=True)

    def test_transaction_rollback_does_not_partial_create(self):
        with self.assertRaises(KeyError):
            self.create(depends_on=['missing'])
        self.assertEqual(self.board.list_items('owner'), [])

    def test_worker_reuses_native_sessions_and_receipts_and_auto_next(self):
        native = Native()
        registry = AdapterRegistry()
        registry.register(native)
        env = {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal',
               'LIVINGRUNTIME_AGENT_SESSIONS': self.tmp.name+'/sessions',
               'LIVINGRUNTIME_REMOTE_JOBS': self.tmp.name+'/jobs'}
        first = self.create()
        second = self.create()
        def request(base, path, body, token, **kwargs):
            device = self.store.authenticate_device(token)
            operation = body.pop('operation')
            agent = body.pop('agent_id')
            if operation == 'claim':
                item = self.board.claim(device['user_sub'], device['device_id'], agent)
            else:
                item = self.board.update(device['user_sub'], device['device_id'], agent, operation=operation, **body)
            return {'item': item}
        cfg = {'url': 'https://relay.invalid', **self.a}
        placement = dict(agent_id='agent-a', adapter_id='test-native', project='demo', local_device='main')
        def start(adapter, goal, **kwargs):
            return agent_sessions.start(adapter, goal, authorized=True, **kwargs)
        with patch.dict(os.environ, env), patch.object(agent_sessions, 'registry', registry):
            worker = Worker(cfg, placement, request, start=start, status=agent_sessions.status)
            worker.tick()
            current = __import__('json').loads(worker.path.read_text())
            job_id = current['session']['runtime_job_id']
            self.assertEqual(jobs.get(job_id)['backend']['type'], 'agent-adapter')
            # New Worker instance simulates Connector restart; reattach, no duplicate start.
            worker = Worker(cfg, placement, request, start=start, status=agent_sessions.status)
            worker.tick()
            self.assertEqual(len(native.started), 1)
            native.states['native-0'] = 'SUCCEEDED'
            worker.tick()
            self.assertEqual(len(native.started), 2)
            self.assertEqual(jobs.get(job_id)['completion_event']['delivery_state'], 'PENDING')
            items = {r['id']: r for r in self.board.list_items('owner')}
            self.assertEqual(items[first['id']]['status'], 'VERIFYING')
            self.assertEqual(items[second['id']]['status'], 'CLAIMED')

    def test_draft_disabled_has_no_workers(self):
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_DRAFT': '0'}):
            self.assertEqual(configured_workers({}, None), [])

    def worker(self, start, status, *, lose_response=None):
        def request(base, path, body, token, **kwargs):
            device = self.store.authenticate_device(token)
            operation = body.pop('operation')
            agent = body.pop('agent_id')
            if operation == 'claim':
                item = self.board.claim(device['user_sub'], device['device_id'], agent)
            else:
                item = self.board.update(device['user_sub'], device['device_id'], agent, operation=operation, **body)
            if lose_response and operation == lose_response.get('operation'):
                lose_response.clear()
                raise RuntimeError('response lost after commit')
            return {'item': item}
        return Worker({'url': 'https://relay.invalid', **self.a},
                      dict(agent_id='agent-a', adapter_id='test-native', project='demo', local_device='main'),
                      request, start=start, status=status)

    def test_worker_rejected_placement_keeps_durable_receipt_and_advances(self):
        rejected = self.create(project='different-project')
        accepted = self.create()
        launches = []
        def start(*args, **kwargs):
            launches.append((args, kwargs))
            return {'session_id': 'next-session', 'runtime_job_id': 'next-job'}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: {'agent_status': {'status': 'RUNNING'}})
            worker.tick()
            self.assertEqual(launches, [])
            self.assertFalse(worker.path.exists())
            receipts = list(worker.path.parent.glob('*.receipt.json'))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(__import__('json').loads(receipts[0].read_text())['item']['id'], rejected['id'])
            self.assertEqual(self.board.list_items('owner')[0]['status'], 'BLOCKED_HUMAN')
            worker.tick()
            self.assertEqual(len(launches), 1)
            self.assertEqual(__import__('json').loads(worker.path.read_text())['item']['id'], accepted['id'])

    def test_worker_crash_marker_blocks_without_duplicate_launch_and_dispatches_independent_item(self):
        self.create()
        next_item = self.create()
        calls = []
        def start(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError('process started but launch reply lost')
            return {'session_id': 'next-session', 'runtime_job_id': 'next-job'}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: {})
            with self.assertRaises(RuntimeError):
                worker.tick()
            worker.tick()
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(self.board.human_actions('owner')), 1)
            self.assertEqual(__import__('json').loads(worker.path.read_text())['item']['id'], next_item['id'])

    def test_worker_recovers_lost_finished_response_without_rerunning(self):
        first = self.create()
        next_item = self.create()
        calls = []
        def start(*args, **kwargs):
            calls.append(args)
            return {'session_id': 'session-'+str(len(calls)), 'runtime_job_id': 'job-'+str(len(calls))}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: {'agent_status': {'status': 'SUCCEEDED'}})
            worker.tick()
            worker = self.worker(start, worker.status, lose_response={'operation': 'finished'})
            with self.assertRaises(RuntimeError):
                worker.tick()
            worker.tick()
            self.assertEqual(len(calls), 2)
            self.assertEqual(__import__('json').loads(worker.path.read_text())['item']['id'], next_item['id'])
            self.assertEqual(self.board.list_items('owner')[0]['status'], 'VERIFYING')

    def test_worker_expiry_archives_session_and_continues_independent_work(self):
        first = self.create()
        self.create()
        calls = []
        def start(*args, **kwargs):
            calls.append(args)
            return {'session_id': 'session-'+str(len(calls)), 'runtime_job_id': 'job-'+str(len(calls))}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: self.fail('expired attempt must not be queried as current'))
            worker.tick()
            with self.store.db() as db:
                db.execute('UPDATE workboard_items SET lease_until=0 WHERE id=?', (first['id'],))
            with self.assertRaises(PermissionError):
                worker.tick()
            worker.tick()
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(list(worker.path.parent.glob('*.receipt.json'))), 1)
            self.assertEqual(self.board.human_actions('owner')[0]['item_id'], first['id'])

    def test_frozen_acceptance_check_automatically_releases_dependency(self):
        first = self.create(acceptance_argv=['python3', '-m', 'unittest'])
        child = self.create(depends_on=[first['id']])
        calls, checks = [], []
        def start(*args, **kwargs):
            calls.append(args)
            return {'session_id': 'session-'+str(len(calls)), 'runtime_job_id': 'job-'+str(len(calls))}
        def check(**kwargs):
            checks.append(kwargs)
            return {'returncode': 0}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: {'agent_status': {'status': 'SUCCEEDED'}})
            worker.check = check
            worker.tick()
            worker.tick()
            self.assertEqual(checks, [dict(argv=['python3', '-m', 'unittest'], project='demo', device='main', timeout_seconds=30)])
            self.assertEqual(len(calls), 2)
            self.assertEqual(__import__('json').loads(worker.path.read_text())['item']['id'], child['id'])
            completed = self.board.list_items('owner')[0]
            self.assertEqual(completed['status'], 'SUCCEEDED')
            self.assertIn('runtime-job:job-1', completed['evidence_json'])

    def test_command_approval_is_not_bypassed_or_treated_as_success(self):
        first = self.create(acceptance_argv=['private-check'])
        child = self.create(depends_on=[first['id']])
        independent = self.create()
        def start(*args, **kwargs):
            return {'session_id': 'session', 'runtime_job_id': 'job'}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: {'agent_status': {'status': 'SUCCEEDED'}})
            worker.check = lambda **kwargs: {'approval_required': True}
            worker.tick()
            worker.tick()
            items = {r['id']: r for r in self.board.list_items('owner')}
            self.assertEqual(items[first['id']]['status'], 'BLOCKED_HUMAN')
            self.assertEqual(items[child['id']]['status'], 'WAITING')
            self.assertEqual(items[independent['id']]['status'], 'CLAIMED')

    def test_human_resolution_retries_only_acceptance_without_relaunching_native_work(self):
        self.create(acceptance_argv=['check'])
        launches = []
        def start(*args, **kwargs):
            launches.append(args)
            return {'session_id': 'session', 'runtime_job_id': 'job'}
        with patch.dict(os.environ, {'LIVINGRUNTIME_WORKBOARD_JOURNALS': self.tmp.name+'/journal'}):
            worker = self.worker(start, lambda sid: {'agent_status': {'status': 'SUCCEEDED'}})
            worker.check = lambda **kwargs: {'approval_required': True}
            worker.tick()
            worker.tick()
            action = self.board.human_actions('owner')[0]
            self.board.resolve('owner', action['id'], decision='retry', resolution='check command now authorized', stopped=True)
            worker.check = lambda **kwargs: {'returncode': 0}
            worker.tick()
            worker.tick()
            self.assertEqual(len(launches), 1)
            self.assertEqual(self.board.list_items('owner')[0]['status'], 'SUCCEEDED')

    def test_expired_completion_is_rejected_and_human_request_is_durable(self):
        self.create()
        active = self.claim()
        self.update(active, 'started', session_id='session', runtime_job_id='job')
        with self.store.db() as db:
            db.execute('UPDATE workboard_items SET lease_until=0')
        with self.assertRaises(PermissionError):
            self.update(active, 'finished')
        self.assertEqual(self.board.list_items('owner')[0]['status'], 'BLOCKED_HUMAN')
        self.assertEqual(len(self.board.human_actions('owner')), 1)


if __name__ == '__main__':
    unittest.main()
