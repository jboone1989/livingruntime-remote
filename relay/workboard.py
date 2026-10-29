"""Opt-in T16 draft: coordination on RelayStore's existing SQLite database.

No process execution, credentials, GitHub writes, or scheduler lives here.
BEGIN IMMEDIATE serializes selection and grant:
https://www.sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import re
import time
import uuid


class Workboard:
    def __init__(self, store):
        self.store = store
        with closing(store.db()) as db, db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS workboard_goals(
              id TEXT PRIMARY KEY, user_sub TEXT NOT NULL, title TEXT NOT NULL,
              intent TEXT NOT NULL, source_revision TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, UNIQUE(user_sub,idempotency_key));
            CREATE TABLE IF NOT EXISTS workboard_items(
              id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
              user_sub TEXT NOT NULL, device_id TEXT NOT NULL,
              agent_id TEXT NOT NULL, adapter_id TEXT NOT NULL, project TEXT NOT NULL,
              local_device TEXT NOT NULL, goal TEXT NOT NULL, source_revision TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, exclusive_key TEXT NOT NULL,
              acceptance_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
              fence INTEGER NOT NULL DEFAULT 0, lease_until REAL,
              session_id TEXT, runtime_job_id TEXT, execution_done INTEGER NOT NULL DEFAULT 0,
              evidence_json TEXT NOT NULL DEFAULT '[]',
              reason TEXT, created_at REAL NOT NULL,
              UNIQUE(user_sub,idempotency_key));
            CREATE TABLE IF NOT EXISTS workboard_dependencies(
              item_id TEXT NOT NULL, predecessor_id TEXT NOT NULL,
              PRIMARY KEY(item_id,predecessor_id));
            CREATE TABLE IF NOT EXISTS workboard_actions(
              id TEXT PRIMARY KEY, item_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              fence INTEGER NOT NULL, source_revision TEXT NOT NULL,
              status TEXT NOT NULL, reason TEXT NOT NULL, resolution TEXT);
            CREATE TABLE IF NOT EXISTS workboard_events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
              fence INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS workboard_receipts(
              item_id TEXT PRIMARY KEY, user_sub TEXT NOT NULL,
              fence INTEGER NOT NULL, source_revision TEXT NOT NULL,
              evidence_json TEXT NOT NULL, finished_at REAL NOT NULL,
              acknowledged_at REAL);
            CREATE INDEX IF NOT EXISTS workboard_inbox
              ON workboard_receipts(user_sub,acknowledged_at,finished_at);
            CREATE INDEX IF NOT EXISTS workboard_ready
              ON workboard_items(user_sub,device_id,agent_id,status,created_at);
            """)

    @contextmanager
    def transaction(self):
        db = self.store.db()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def text(value, name, maximum=256):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"invalid {name}")
        return value

    @staticmethod
    def event(db, row):
        db.execute("INSERT INTO workboard_events(item_id,fence,status,created_at) VALUES(?,?,?,?)",
                   (row['id'], row['fence'], row['status'], time.time()))

    @staticmethod
    def receipt(db, row):
        # Same transaction as acceptance. A repeated/replayed completion cannot
        # make a second Inbox item or silently substitute its evidence.
        db.execute("""INSERT INTO workboard_receipts
          (item_id,user_sub,fence,source_revision,evidence_json,finished_at)
          VALUES (?,?,?,?,?,?)""",
          (row['id'], row['user_sub'], row['fence'],
           row['source_revision'], row['evidence_json'], time.time()))

    @staticmethod
    def row(db, user, item_id):
        row = db.execute("SELECT * FROM workboard_items WHERE id=? AND user_sub=?",
                         (item_id, user)).fetchone()
        if row is None:
            raise KeyError("unknown work item")
        return dict(row)

    def create_goal(self, user, *, title, intent, source_revision, idempotency_key):
        for name, value in dict(title=title, intent=intent, source_revision=source_revision,
                                idempotency_key=idempotency_key).items():
            self.text(value, name, 4000 if name == 'intent' else 256)
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source_revision):
            raise ValueError('goal source_revision must be immutable')
        with self.transaction() as db:
            existing = db.execute("""SELECT * FROM workboard_goals
                WHERE user_sub=? AND idempotency_key=?""", (user, idempotency_key)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in
                       dict(title=title, intent=intent, source_revision=source_revision).items()):
                    raise ValueError('goal idempotency key reused with changed request')
                return dict(existing)
            goal_id = 'lrgoal_' + uuid.uuid4().hex
            db.execute("""INSERT INTO workboard_goals
              (id,user_sub,title,intent,source_revision,idempotency_key,created_at)
              VALUES (?,?,?,?,?,?,?)""",
              (goal_id, user, title, intent, source_revision, idempotency_key, time.time()))
            return dict(db.execute("SELECT * FROM workboard_goals WHERE id=?", (goal_id,)).fetchone())

    def list_goals(self, user):
        with closing(self.store.db()) as db:
            return [dict(r) for r in db.execute("""SELECT g.*,
              COUNT(w.id) AS total_items,
              COUNT(CASE WHEN w.status='SUCCEEDED' THEN 1 END) AS succeeded_items
              FROM workboard_goals g LEFT JOIN workboard_items w ON w.goal_id=g.id
              WHERE g.user_sub=? GROUP BY g.id ORDER BY g.created_at,g.id""", (user,))]

    def set_goal_paused(self, user, goal_id, paused):
        if type(paused) is not bool:
            raise ValueError('paused must be boolean')
        with self.transaction() as db:
            updated = db.execute("""UPDATE workboard_goals SET paused=?
              WHERE id=? AND user_sub=?""", (int(paused), goal_id, user))
            if updated.rowcount != 1:
                raise PermissionError('unknown or foreign goal')
            return dict(db.execute("SELECT * FROM workboard_goals WHERE id=?", (goal_id,)).fetchone())

    def create(self, user, *, goal_id, device_id, agent_id, adapter_id, project, local_device,
               goal, source_revision, idempotency_key, exclusive_key, depends_on=None, acceptance_argv=None):
        values = dict(goal_id=goal_id, device_id=device_id, agent_id=agent_id, adapter_id=adapter_id,
                      project=project, local_device=local_device, goal=goal,
                      source_revision=source_revision, idempotency_key=idempotency_key,
                      exclusive_key=exclusive_key)
        for name, value in values.items():
            self.text(value, name, 8000 if name == 'goal' else 256)
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source_revision):
            raise ValueError('source_revision must be an immutable commit or content hash')
        acceptance = acceptance_argv if acceptance_argv is not None else []
        if not isinstance(acceptance, list) or len(acceptance) > 64:
            raise ValueError('acceptance_argv must be a bounded argv list')
        for argument in acceptance:
            self.text(argument, 'acceptance argument', 2000)
        values['acceptance_json'] = json.dumps(acceptance)
        dependencies = depends_on or []
        if not isinstance(dependencies, list) or len(dependencies) > 100:
            raise ValueError("invalid dependencies")
        with self.transaction() as db:
            goal_row = db.execute("SELECT id FROM workboard_goals WHERE id=? AND user_sub=?",
                                  (goal_id, user)).fetchone()
            if not goal_row:
                raise PermissionError("work item requires an owned, explicit Goal")
            device = db.execute("SELECT 1 FROM devices WHERE device_id=? AND user_sub=? AND enabled=1",
                                (device_id, user)).fetchone()
            if not device:
                raise PermissionError("device not owned or enabled")
            existing = db.execute("SELECT * FROM workboard_items WHERE user_sub=? AND idempotency_key=?",
                                  (user, idempotency_key)).fetchone()
            if existing:
                old_deps = {r[0] for r in db.execute(
                    "SELECT predecessor_id FROM workboard_dependencies WHERE item_id=?", (existing['id'],))}
                if any(existing[k] != v for k, v in values.items()) or old_deps != set(dependencies):
                    raise ValueError("idempotency key reused with different request")
                return dict(existing)
            for dep in dependencies:
                prior = self.row(db, user, dep)
                if prior['goal_id'] != goal_id:
                    raise PermissionError('work item dependency cannot cross Goal authority')
            item_id = 'lrwork_' + uuid.uuid4().hex
            columns = ','.join(values)
            db.execute(f"INSERT INTO workboard_items(id,user_sub,{columns},status,created_at) "
                       f"VALUES({','.join('?' for _ in range(len(values)+4))})",
                       (item_id, user, *values.values(), 'WAITING' if dependencies else 'READY', time.time()))
            db.executemany("INSERT INTO workboard_dependencies VALUES(?,?)",
                           [(item_id, dep) for dep in set(dependencies)])
            row = self.row(db, user, item_id)
            self.event(db, row)
            return row

    def list_items(self, user):
        with closing(self.store.db()) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM workboard_items WHERE user_sub=? ORDER BY created_at,id", (user,))]

    def claim(self, user, device_id, agent_id):
        self.text(agent_id, 'agent_id')
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM devices WHERE device_id=? AND user_sub=? AND enabled=1",
                              (device_id, user)).fetchone():
                raise PermissionError("device revoked or belongs to another owner")
            # Expiry is NOT proof that a native process has stopped. Keep its
            # resource reserved and expose it to the human queue; never rerun.
            expired = db.execute("SELECT * FROM workboard_items WHERE user_sub=? "
                                 "AND status IN ('CLAIMED','RUNNING') AND lease_until<=?",
                                 (user, time.time())).fetchall()
            for row in expired:
                self._block(db, dict(row), 'Lease expired; reconcile native process before retry')
            waiting = db.execute("SELECT * FROM workboard_items WHERE user_sub=? AND status='WAITING'", (user,)).fetchall()
            for row in waiting:
                states = [r[0] for r in db.execute(
                    'SELECT p.status FROM workboard_dependencies d JOIN workboard_items p ON p.id=d.predecessor_id WHERE d.item_id=?',
                    (row['id'],))]
                if any(s == 'FAILED' for s in states):
                    self._block(db, dict(row), 'Predecessor failed; owner must decide replacement or termination')
                elif all(s == 'SUCCEEDED' for s in states):
                    db.execute("UPDATE workboard_items SET status='READY' WHERE id=?", (row['id'],))
                    self.event(db, {**dict(row), 'status': 'READY'})
            if db.execute("SELECT 1 FROM workboard_items WHERE device_id=? AND agent_id=? "
                          "AND status IN ('CLAIMED','RUNNING')", (device_id, agent_id)).fetchone():
                return None
            if db.execute("SELECT COUNT(*) FROM workboard_items WHERE device_id=? "
                          "AND status IN ('CLAIMED','RUNNING')", (device_id,)).fetchone()[0] >= 2:
                return None
            row = db.execute("""
                SELECT w.* FROM workboard_items w
                JOIN workboard_goals g ON g.id=w.goal_id AND g.user_sub=w.user_sub
                WHERE w.user_sub=? AND w.device_id=? AND w.agent_id=? AND w.status='READY'
                AND g.paused=0
                AND NOT EXISTS (
                  SELECT 1 FROM workboard_dependencies d JOIN workboard_items p ON p.id=d.predecessor_id
                  WHERE d.item_id=w.id AND p.status!='SUCCEEDED')
                AND NOT EXISTS (
                  SELECT 1 FROM workboard_items busy WHERE busy.user_sub=w.user_sub
                  AND busy.exclusive_key=w.exclusive_key
                  AND busy.status IN ('CLAIMED','RUNNING','VERIFYING','BLOCKED_HUMAN'))
                ORDER BY w.created_at,w.id LIMIT 1
                """, (user, device_id, agent_id)).fetchone()
            if not row:
                return None
            db.execute("UPDATE workboard_items SET status='CLAIMED',attempt=attempt+1,fence=fence+1,"
                       "lease_until=?,session_id=CASE WHEN execution_done=1 THEN session_id ELSE NULL END,"
                       "runtime_job_id=CASE WHEN execution_done=1 THEN runtime_job_id ELSE NULL END,reason=NULL WHERE id=?",
                       (time.time()+180, row['id']))
            row = self.row(db, user, row['id'])
            self.event(db, row)
            return row

    def _block(self, db, row, reason):
        db.execute("UPDATE workboard_items SET status='BLOCKED_HUMAN',reason=? WHERE id=?", (reason, row['id']))
        db.execute("INSERT INTO workboard_actions VALUES(?,?,?,?,?, 'PENDING',?,NULL)",
                   ('lrhuman_'+uuid.uuid4().hex, row['id'], row['attempt'], row['fence'], row['source_revision'], reason))
        self.event(db, {**row, 'status': 'BLOCKED_HUMAN'})

    def update(self, user, device_id, agent_id, item_id, fence, *, operation, **kwargs):
        row = self._update(user, device_id, agent_id, item_id, fence, operation=operation, **kwargs)
        if row.pop('_expired_lease', False):
            raise PermissionError('lease expired; native process must be reconciled')
        return row

    def _update(self, user, device_id, agent_id, item_id, fence, *, operation,
               session_id=None, runtime_job_id=None, reason=None, checks_passed=False, evidence_refs=None,
               execution_done=False):
        if isinstance(fence, bool) or not isinstance(fence, int):
            raise ValueError('fence must be an integer')
        with self.transaction() as db:
            row = self.row(db, user, item_id)
            if not db.execute('SELECT 1 FROM devices WHERE device_id=? AND user_sub=? AND enabled=1',
                              (device_id, user)).fetchone():
                raise PermissionError('device disabled or owner mismatch')
            if (row['device_id'], row['agent_id'], row['fence']) != (device_id, agent_id, fence):
                raise PermissionError("stale fence or different task owner")
            if row['status'] in {'CLAIMED', 'RUNNING'} and row['lease_until'] <= time.time():
                self._block(db, row, 'Lease expired; reconcile native process before retry')
                return {**self.row(db, user, item_id), '_expired_lease': True}
            if row['status'] not in {'CLAIMED', 'RUNNING'}:
                if row['status'] in {'BLOCKED_HUMAN', 'VERIFYING', 'SUCCEEDED'} and operation == 'heartbeat':
                    return row
                raise PermissionError("task no longer executable")
            if operation == 'heartbeat':
                db.execute("UPDATE workboard_items SET lease_until=? WHERE id=?", (time.time()+180, item_id))
            elif operation in {'attached', 'started'}:
                self.text(session_id, 'session_id')
                self.text(runtime_job_id, 'runtime_job_id')
                if row['session_id'] and (row['session_id'] != session_id or row['runtime_job_id'] != runtime_job_id):
                    raise ValueError("attempt already attached to another native execution")
                db.execute("UPDATE workboard_items SET status=?,session_id=?,runtime_job_id=? WHERE id=?",
                           ('RUNNING' if operation == 'started' or row['status'] == 'RUNNING' else 'CLAIMED',
                            session_id, runtime_job_id, item_id))
            elif operation == 'finished':
                if row['status'] != 'RUNNING':
                    raise ValueError("cannot verify an execution that never started")
                status = 'VERIFYING'
                if checks_passed is True:
                    if not json.loads(row['acceptance_json']) or not evidence_refs or len(evidence_refs) > 20:
                        raise ValueError('automatic acceptance requires frozen checks and evidence')
                    for ref in evidence_refs:
                        self.text(ref, 'evidence_ref', 2000)
                    status = 'SUCCEEDED'
                db.execute("UPDATE workboard_items SET status=?,lease_until=NULL,evidence_json=? WHERE id=?",
                           (status, json.dumps(evidence_refs or []), item_id))
                if status == 'SUCCEEDED':
                    self.receipt(db, self.row(db, user, item_id))
                if status == 'VERIFYING':
                    db.execute("INSERT INTO workboard_actions VALUES(?,?,?,?,?, 'PENDING',?,NULL)",
                               ('lrhuman_'+uuid.uuid4().hex, item_id, row['attempt'],
                                row['fence'], row['source_revision'],
                                'Execution finished; manual acceptance evidence required'))
            elif operation == 'blocked':
                if execution_done is True:
                    if row['status'] != 'RUNNING' or not row['session_id']:
                        raise ValueError('completed execution must have an attached session')
                    db.execute('UPDATE workboard_items SET execution_done=1 WHERE id=?', (item_id,))
                self._block(db, row, self.text(reason, 'reason', 2000))
            else:
                raise ValueError("unknown workboard operation")
            row = self.row(db, user, item_id)
            self.event(db, row)
            return row

    def inbox(self, user, *, include_acknowledged=False):
        with closing(self.store.db()) as db:
            where = '' if include_acknowledged else 'AND acknowledged_at IS NULL'
            return [dict(r) for r in db.execute(
                "SELECT * FROM workboard_receipts WHERE user_sub=? " + where +
                " ORDER BY finished_at DESC,item_id LIMIT 100", (user,))]

    def acknowledge(self, user, item_id, fence, source_revision):
        if type(fence) is not int or fence < 1:
            raise ValueError('invalid receipt fence')
        self.text(item_id, 'item_id')
        self.text(source_revision, 'source_revision')
        with self.transaction() as db:
            result = db.execute("""UPDATE workboard_receipts
              SET acknowledged_at=? WHERE item_id=? AND user_sub=? AND fence=?
              AND source_revision=? AND acknowledged_at IS NULL""",
              (time.time(), item_id, user, fence, source_revision))
            if result.rowcount != 1:
                raise PermissionError('unknown, stale or already acknowledged Inbox receipt')
            return dict(db.execute("SELECT * FROM workboard_receipts WHERE item_id=?", (item_id,)).fetchone())

    def human_actions(self, user):
        with closing(self.store.db()) as db:
            return [dict(r) for r in db.execute(
                "SELECT a.* FROM workboard_actions a JOIN workboard_items w ON w.id=a.item_id "
                "WHERE w.user_sub=? AND a.status='PENDING' ORDER BY a.id", (user,))]

    def resolve(self, user, action_id, *, decision, resolution, stopped=False):
        self.text(resolution, 'resolution', 2000)
        if decision not in {'retry', 'fail'}:
            raise ValueError("decision must be retry or fail")
        with self.transaction() as db:
            action = db.execute("SELECT * FROM workboard_actions WHERE id=?", (action_id,)).fetchone()
            if not action:
                raise KeyError("unknown human action")
            row = self.row(db, user, action['item_id'])
            if (action['status'] != 'PENDING' or row['status'] != 'BLOCKED_HUMAN' or
                    any(action[k] != row[k] for k in ('attempt', 'fence', 'source_revision'))):
                raise PermissionError("stale or consumed human action")
            if stopped is not True:
                raise ValueError("confirm native process stopped and reconcile external side effects first")
            if decision == 'retry' and row['attempt'] >= 3:
                raise ValueError('MVP attempt limit reached; create a separately authorized successor')
            # This is new owner authorization for exactly the next attempt of
            # this immutable revision; it is NOT a transferable tool approval.
            db.execute("UPDATE workboard_actions SET status='RESOLVED',resolution=? WHERE id=?",
                       (resolution, action_id))
            db.execute("UPDATE workboard_items SET status=?,lease_until=NULL,reason=NULL WHERE id=?",
                       ('READY' if decision == 'retry' else 'FAILED', row['id']))
            row = self.row(db, user, row['id'])
            self.event(db, row)
            return row

    def verify(self, user, item_id, *, fence, source_revision, evidence_refs):
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 20:
            raise ValueError("bounded acceptance evidence required")
        for ref in evidence_refs:
            self.text(ref, 'evidence_ref', 2000)
        with self.transaction() as db:
            row = self.row(db, user, item_id)
            if row['status'] != 'VERIFYING' or (row['fence'], row['source_revision']) != (fence, source_revision):
                raise PermissionError("stale acceptance or item not verifying")
            db.execute("UPDATE workboard_items SET status='SUCCEEDED',evidence_json=? WHERE id=?",
                       (json.dumps(evidence_refs), item_id))
            self.receipt(db, self.row(db, user, item_id))
            db.execute("UPDATE workboard_actions SET status='RESOLVED',resolution='Verified evidence' "
                       "WHERE item_id=? AND attempt=? AND fence=? AND source_revision=? "
                       "AND status='PENDING'",
                       (item_id, row['attempt'], row['fence'], source_revision))
            row = self.row(db, user, item_id)
            self.event(db, row)
            return row
