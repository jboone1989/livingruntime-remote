"""One opt-in placement tick on the existing Connector executor, no scheduler.

The write-ahead launch marker makes ambiguous crashes fail closed. Native
sessions, policies and durable job receipts remain owned by existing adapters.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid


class Worker:
    def __init__(self, cfg, placement, request, *, start=None, status=None, check=None):
        required = {'agent_id', 'adapter_id', 'project', 'local_device'}
        if set(placement) != required or any(not isinstance(v, str) or not v for v in placement.values()):
            raise ValueError("workboard placement requires agent, adapter, project and local device")
        self.cfg, self.placement, self.request = cfg, placement, request
        if start is None or status is None:
            import bridge
            start = start or bridge.start_agent
            status = status or bridge.get_agent_session
        self.start, self.status = start, status
        self.check = check
        key = hashlib.sha256(json.dumps([cfg['url'], cfg['device_id'], placement], sort_keys=True).encode()).hexdigest()
        self.path = Path(os.environ.get('LIVINGRUNTIME_WORKBOARD_JOURNALS',
                                       str(Path.home()/'.livingruntime'/'workboard'))) / (key+'.json')

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        with temp.open('x', encoding='utf-8') as handle:
            os.chmod(temp, 0o600)
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        if os.name != 'nt':
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def call(self, operation, **fields):
        return self.request(self.cfg['url'], '/device/workboard',
                            {'operation': operation, 'agent_id': self.placement['agent_id'], **fields},
                            self.cfg['device_token'], timeout=30)

    def tick(self):
        # A second Connector process must not race the same launch journal.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+b') as lock:
            if os.name == 'nt':
                import msvcrt
                lock.seek(0)
                if not lock.read(1):
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return
            else:
                import fcntl
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
            self._tick_locked()

    def archive(self, item):
        archive = self.path.with_name(self.path.stem+'.'+item['id']+'.'+str(item['fence'])+'.receipt.json')
        os.replace(self.path, archive)

    def _tick_locked(self):
        current = json.loads(self.path.read_text()) if self.path.exists() else None
        # At most one finished/blocked item and one new launch per tick. Return
        # immediately to the existing Connector loop while native work runs.
        if current:
            item = current['item']
            identity = {'item_id': item['id'], 'fence': item['fence']}
            try:
                leased = self.call('heartbeat', **identity)['item']
            except PermissionError:
                self.archive(item)
                raise
            if leased['status'] in {'BLOCKED_HUMAN', 'VERIFYING', 'SUCCEEDED'}:
                pass  # Lost terminal response or expired authority: never relaunch.
            elif not current.get('session'):
                self.call('blocked', **identity, reason='Ambiguous launch; reconcile local native process before retry')
            else:
                session = current['session']
                observed = self.status(session['session_id'])['agent_status']
                native = str(observed.get('status', 'UNKNOWN')).upper()
                # A returned session handle or PENDING/WAITING native state is
                # not running-process evidence. Retain the claim without lying.
                operation = 'started' if native in {'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED', 'BLOCKED_HUMAN'} else 'attached'
                self.call(operation, **identity, session_id=session['session_id'], runtime_job_id=session['runtime_job_id'])
                if native == 'SUCCEEDED':
                    argv = json.loads(item['acceptance_json'])
                    if argv:
                        check = self.check
                        if check is None:
                            import bridge
                            check = bridge.exec
                        # Fixed owner-authored, repeatable acceptance checks use
                        # the existing exact-command policy, never a shell eval.
                        result = check(argv=argv, project=item['project'], device=item['local_device'], timeout_seconds=30)
                        current['verification'] = {'argv': argv, 'returncode': result.get('returncode'),
                                                   'approval_required': result.get('approval_required', False)}
                        self.save(current)
                        if result.get('approval_required') or result.get('returncode') != 0:
                            self.call('blocked', **identity, execution_done=True,
                                      reason='Acceptance check failed or requires existing command approval')
                        else:
                            self.call('finished', **identity, checks_passed=True,
                                      evidence_refs=['runtime-job:'+session['runtime_job_id'],
                                                     'connector-journal:'+self.path.stem+':'+item['id']+':'+str(item['fence'])])
                    else:
                        self.call('finished', **identity)
                elif native in {'FAILED', 'CANCELLED', 'BLOCKED', 'BLOCKED_HUMAN'}:
                    self.call('blocked', **identity, reason='Native '+native+'; inspect session '+session['session_id'])
                elif native == 'UNKNOWN':
                    self.call('blocked', **identity, reason='Native process state unknown; reconcile before retry')
                else:
                    return
            self.archive(item)

        item = self.call('claim').get('item')
        if item is None:
            return
        current = {'item': item, 'session': None}
        self.save(current)  # crash after this point must never blindly relaunch
        identity = {'item_id': item['id'], 'fence': item['fence']}
        for field in ('agent_id', 'adapter_id', 'project', 'local_device'):
            if item[field] != self.placement[field]:
                self.call('blocked', **identity, reason='Claim differs from operator placement')
                self.archive(item)  # Preserve a durable receipt; never erase a rejected claim.
                return
        if self.call('heartbeat', **identity)['item']['status'] != 'CLAIMED':
            self.archive(item)
            return
        # bridge.start_agent enforces host_for + may_launch + native Adapter
        # policy; receipt creation stays in agent_sessions.start/jobs.create.
        if item['execution_done']:
            session = {'session_id': item['session_id'], 'runtime_job_id': item['runtime_job_id']}
        else:
            session = self.start(item['adapter_id'], item['goal'], project=item['project'], device=item['local_device'])
        current['session'] = session
        self.save(current)
        self.call('attached', **identity, session_id=session['session_id'], runtime_job_id=session['runtime_job_id'])


def configured_workers(cfg, request):
    if os.environ.get('LIVINGRUNTIME_WORKBOARD_DRAFT') != '1':
        return []
    placements = cfg.get('workboard_workers', [])
    if not isinstance(placements, list) or len(placements) > 2:
        raise ValueError('MVP supports at most two explicit workboard placements per Connector')
    if len({p['agent_id'] for p in placements}) != len(placements):
        raise ValueError('duplicate workboard agent identity')
    return [Worker(cfg, placement, request) for placement in placements]
