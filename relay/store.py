from __future__ import annotations

import hashlib, json, secrets, sqlite3, time, uuid
from pathlib import Path
from contextlib import contextmanager
from typing import Any

def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

class RelayStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS pairing_codes(
              code_hash TEXT PRIMARY KEY,user_sub TEXT NOT NULL,expires_at REAL NOT NULL,used_at REAL);
            CREATE TABLE IF NOT EXISTS devices(
              device_id TEXT PRIMARY KEY,user_sub TEXT NOT NULL,name TEXT NOT NULL,
              token_hash TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,last_seen REAL NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_sub,enabled,last_seen DESC);
            CREATE TABLE IF NOT EXISTS tasks(
              task_id TEXT PRIMARY KEY,user_sub TEXT NOT NULL,device_id TEXT NOT NULL,
              tool TEXT NOT NULL,args_json TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,
              created_at REAL NOT NULL,claimed_at REAL,completed_at REAL);
            CREATE INDEX IF NOT EXISTS idx_tasks_device ON tasks(device_id,status,created_at);
            CREATE TABLE IF NOT EXISTS result_receipts(
              task_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,payload_hash TEXT NOT NULL,received_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS completion_events(
              event_id TEXT PRIMARY KEY,user_sub TEXT NOT NULL,job_key TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',claim_token TEXT,lease_until REAL,
              updated_at REAL NOT NULL,UNIQUE(user_sub,job_key));
            CREATE TABLE IF NOT EXISTS continuations(
              user_sub TEXT NOT NULL,session_id TEXT NOT NULL,job_id TEXT NOT NULL,
              pi_remote_dir TEXT NOT NULL,job_root TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
              PRIMARY KEY(user_sub,session_id));
            CREATE INDEX IF NOT EXISTS idx_continuations_updated ON continuations(updated_at);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "claim_token" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN claim_token TEXT")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cleanup(self, retention_seconds: int = 86400) -> None:
        now = time.time()
        cutoff = now - max(0, int(retention_seconds))
        with self.db() as db:
            db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
            db.execute(
                "DELETE FROM tasks WHERE status='complete' AND completed_at IS NOT NULL AND completed_at < ?",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM tasks WHERE status IN ('queued','offered') AND created_at < ?",
                (cutoff,),
            )
            db.execute("DELETE FROM continuations WHERE updated_at < ?", (cutoff,))

    def bind_continuation(
        self,
        user_sub: str,
        session_id: str,
        job_id: str,
        pi_remote_dir: str,
        job_root: str | None = None,
    ) -> dict[str, Any]:
        self.cleanup()
        now = time.time()
        with self.db() as db:
            db.execute(
                "INSERT INTO continuations(user_sub,session_id,job_id,pi_remote_dir,job_root,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(user_sub,session_id) DO UPDATE SET "
                "job_id=excluded.job_id,pi_remote_dir=excluded.pi_remote_dir,job_root=excluded.job_root,updated_at=excluded.updated_at",
                (user_sub, session_id, job_id, pi_remote_dir, job_root, now, now),
            )
        return {
            "session_id": session_id,
            "job_id": job_id,
            "pi_remote_dir": pi_remote_dir,
            "job_root": job_root,
        }

    def continuation_for_user(self, user_sub: str, session_id: str) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT session_id,job_id,pi_remote_dir,job_root,created_at,updated_at "
                "FROM continuations WHERE user_sub=? AND session_id=?",
                (user_sub, session_id),
            ).fetchone()
        return dict(row) if row else None

    def clear_continuation(self, user_sub: str, session_id: str) -> bool:
        with self.db() as db:
            cur = db.execute(
                "DELETE FROM continuations WHERE user_sub=? AND session_id=?",
                (user_sub, session_id),
            )
        return cur.rowcount == 1

    def create_pairing_code(self, user_sub: str, ttl: int = 600) -> dict[str, Any]:
        self.cleanup()
        now = time.time()
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
        with self.db() as db:
            db.execute("INSERT INTO pairing_codes VALUES(?,?,?,NULL)", (digest(code), user_sub, now + ttl))
        return {"code": code, "expires_at": now + ttl}

    def pair_device(self, code: str, name: str) -> dict[str, str]:
        now = time.time()
        key = digest(code.strip().upper())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM pairing_codes WHERE code_hash=?", (key,)).fetchone()
            if not row or row["used_at"] is not None or row["expires_at"] < now:
                raise PermissionError("pairing code is invalid or expired")
            device_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO devices VALUES(?,?,?,?,?,?,1)",
                (device_id, row["user_sub"], (name or "LivingRuntime Remote")[:120], digest(token), now, now),
            )
            db.execute("UPDATE pairing_codes SET used_at=? WHERE code_hash=?", (now, key))
        return {"device_id": device_id, "device_token": token}

    def authenticate_device(self, token: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM devices WHERE token_hash=? AND enabled=1", (digest(token),)
            ).fetchone()
            if not row:
                raise PermissionError("invalid device token")
            db.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (time.time(), row["device_id"]))
        return dict(row)

    def device_for_user(self, user_sub: str) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT device_id,name,last_seen FROM devices WHERE user_sub=? AND enabled=1 "
                "ORDER BY last_seen DESC LIMIT 1", (user_sub,)
            ).fetchone()
        return dict(row) if row else None

    def cancel_if_queued(self, user_sub: str, task_id: str) -> bool:
        """Cancel a task only if no device has claimed it yet.

        This prevents an MCP request that already timed out from executing later
        when an offline connector eventually comes back. Claimed tasks are left
        alone because the remote operation may already be running.
        """
        with self.db() as db:
            cur = db.execute(
                "DELETE FROM tasks WHERE task_id=? AND user_sub=? AND status IN ('queued','offered')",
                (task_id, user_sub),
            )
        return cur.rowcount == 1

    def revoke_device(self, user_sub: str, device_id: str | None = None) -> bool:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if device_id is None:
                row = db.execute(
                    "SELECT device_id FROM devices WHERE user_sub=? AND enabled=1 "
                    "ORDER BY last_seen DESC LIMIT 1", (user_sub,)
                ).fetchone()
                if not row:
                    return False
                device_id = row["device_id"]
            cur = db.execute(
                "UPDATE devices SET enabled=0 WHERE device_id=? AND user_sub=? AND enabled=1",
                (device_id, user_sub),
            )
            if cur.rowcount:
                db.execute("DELETE FROM tasks WHERE device_id=? AND user_sub=?", (device_id, user_sub))
            return cur.rowcount == 1

    def enqueue(self, user_sub: str, device_id: str, tool: str, args: dict[str, Any]) -> str:
        self.cleanup()
        task_id = str(uuid.uuid4())
        payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 524288:
            raise ValueError("task payload exceeds relay limit")
        with self.db() as db:
            db.execute(
                "INSERT INTO tasks(task_id,user_sub,device_id,tool,args_json,status,created_at) "
                "VALUES(?,?,?,?,?,'queued',?)",
                (task_id, user_sub, device_id, tool, payload, time.time()),
            )
        return task_id

    def claim(self, device_id: str) -> dict[str, Any] | None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT task_id,tool,args_json FROM tasks WHERE device_id=? AND status='queued' "
                "ORDER BY created_at LIMIT 1", (device_id,)
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE tasks SET status='claimed',claimed_at=? WHERE task_id=? AND status='queued'",
                (time.time(), row["task_id"]),
            )
        return {"task_id": row["task_id"], "tool": row["tool"], "args": json.loads(row["args_json"])}

    def offer(self, device_id: str) -> dict[str, Any] | None:
        """Re-offer until a Connector has durably prepared and acknowledged it."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT task_id,tool,args_json FROM tasks WHERE device_id=? "
                             "AND status IN ('queued','offered') ORDER BY created_at LIMIT 1", (device_id,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='offered' WHERE task_id=?", (row["task_id"],))
        return {"task_id": row["task_id"], "tool": row["tool"], "args": json.loads(row["args_json"])}

    def accept_offer(self, device_id: str, task_id: str, token: str) -> bool:
        if not token or len(token) > 128:
            raise ValueError("invalid preparation token")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,claim_token FROM tasks WHERE device_id=? AND task_id=?",
                             (device_id, task_id)).fetchone()
            if row is None:
                return False
            if row["status"] == "offered":
                db.execute("UPDATE tasks SET status='claimed',claimed_at=?,claim_token=? WHERE task_id=?",
                           (time.time(), token, task_id))
                return True
            return row["status"] == "claimed" and row["claim_token"] == token

    def task_receipt(self, user_sub: str, task_id: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=? AND user_sub=?", (task_id, user_sub)).fetchone()
        if row is None:
            raise KeyError("relay task not found or expired")
        return dict(row)

    def complete(self, device_id: str, task_id: str, result: dict[str, Any]) -> None:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload.encode()) > 1048576:
            payload = '{"ok":false,"error":"device result exceeds relay limit"}'
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute("SELECT device_id,payload_hash FROM result_receipts WHERE task_id=?", (task_id,)).fetchone()
            if receipt:
                if receipt["device_id"] == device_id and receipt["payload_hash"] == digest(payload):
                    return
                raise KeyError("conflicting result or wrong device")
            cur = db.execute(
                "UPDATE tasks SET status='complete',result_json=?,completed_at=? "
                "WHERE task_id=? AND device_id=? AND status='claimed'",
                (payload, time.time(), task_id, device_id),
            )
            if cur.rowcount != 1:
                raise KeyError("task missing, complete, or owned by another device")
            db.execute("INSERT INTO result_receipts VALUES(?,?,?,?)", (task_id, device_id, digest(payload), time.time()))

    def ensure_completion(self, user_sub: str, job_key: str) -> dict[str, Any]:
        event_id = "completion_" + digest(user_sub + "\n" + job_key)[:40]
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO completion_events(event_id,user_sub,job_key,updated_at) VALUES(?,?,?,?)",
                       (event_id, user_sub, job_key, time.time()))
        return self.completion(user_sub, event_id)

    def completion(self, user_sub: str, event_id: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT event_id,state,lease_until FROM completion_events WHERE user_sub=? AND event_id=?",
                             (user_sub, event_id)).fetchone()
        if row is None:
            raise KeyError("completion not found")
        value = dict(row)
        if value["state"] == "sending" and (value["lease_until"] or 0) <= time.time():
            value["state"] = "uncertain"
        return value

    def claim_completion(self, user_sub: str, event_id: str, retry_uncertain: bool = False) -> dict[str, Any]:
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM completion_events WHERE user_sub=? AND event_id=?", (user_sub, event_id)).fetchone()
            if row is None:
                raise KeyError("completion not found")
            state = row["state"]
            if state == "sending" and (row["lease_until"] or 0) <= now:
                state = "uncertain"
            if state == "pending" or (state == "uncertain" and retry_uncertain):
                token = secrets.token_urlsafe(24)
                db.execute("UPDATE completion_events SET state='sending',claim_token=?,lease_until=?,updated_at=? WHERE event_id=?",
                           (token, now + 90, now, event_id))
                return {"event_id": event_id, "state": "sending", "claim_token": token}
            return {"event_id": event_id, "state": state}

    def settle_completion(self, user_sub: str, event_id: str, token: str, outcome: str) -> dict[str, Any]:
        if outcome not in {"pending", "uncertain", "accepted"}:
            raise ValueError("invalid delivery outcome")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,claim_token FROM completion_events WHERE user_sub=? AND event_id=?", (user_sub, event_id)).fetchone()
            if row is None:
                raise KeyError("completion not found")
            if row["state"] == "observed":
                return {"event_id": event_id, "state": "observed"}
            if row["claim_token"] != token:
                raise PermissionError("completion claim does not match")
            if row["state"] == "accepted":
                return {"event_id": event_id, "state": "accepted"}
            db.execute("UPDATE completion_events SET state=?,updated_at=? WHERE event_id=?", (outcome, time.time(), event_id))
        return {"event_id": event_id, "state": outcome}

    def observe_completion(self, user_sub: str, event_id: str) -> dict[str, Any]:
        with self.db() as db:
            changed = db.execute("UPDATE completion_events SET state='observed',updated_at=? WHERE user_sub=? AND event_id=?",
                                 (time.time(), user_sub, event_id)).rowcount
        if not changed:
            raise KeyError("completion not found")
        return {"event_id": event_id, "state": "observed"}

    def result(self, user_sub: str, task_id: str, consume: bool = False) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT status,result_json FROM tasks WHERE task_id=? AND user_sub=?",
                (task_id, user_sub),
            ).fetchone()
            if not row:
                raise KeyError("task not found")
            if row["status"] != "complete":
                return None
            result = json.loads(row["result_json"])
            if consume:
                db.execute("DELETE FROM tasks WHERE task_id=? AND user_sub=?", (task_id, user_sub))
        return result
