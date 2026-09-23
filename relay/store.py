from __future__ import annotations

import hashlib, json, secrets, sqlite3, time, uuid
from pathlib import Path
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
            CREATE TABLE IF NOT EXISTS continuations(
              user_sub TEXT NOT NULL,session_id TEXT NOT NULL,job_id TEXT NOT NULL,
              pi_remote_dir TEXT NOT NULL,job_root TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
              PRIMARY KEY(user_sub,session_id));
            CREATE INDEX IF NOT EXISTS idx_continuations_updated ON continuations(updated_at);
            """)

    def db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

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
                "DELETE FROM tasks WHERE status IN ('queued','claimed') AND created_at < ?",
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
                "DELETE FROM tasks WHERE task_id=? AND user_sub=? AND status='queued'",
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

    def complete(self, device_id: str, task_id: str, result: dict[str, Any]) -> None:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 1048576:
            payload = '{"ok":false,"error":"device result exceeds relay limit"}'
        with self.db() as db:
            cur = db.execute(
                "UPDATE tasks SET status='complete',result_json=?,completed_at=? "
                "WHERE task_id=? AND device_id=? AND status='claimed'",
                (payload, time.time(), task_id, device_id),
            )
            if cur.rowcount != 1:
                raise KeyError("task missing, complete, or owned by another device")

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
