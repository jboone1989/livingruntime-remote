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
            CREATE TABLE IF NOT EXISTS event_subscriptions(
              subscription_id TEXT PRIMARY KEY,user_sub TEXT NOT NULL,name TEXT NOT NULL,
              arguments_json TEXT NOT NULL,callback_url TEXT NOT NULL,secret TEXT NOT NULL,
              previous_secret TEXT,previous_secret_until REAL,expires_at REAL NOT NULL,
              created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_event_subscriptions_user
              ON event_subscriptions(user_sub,name,expires_at);
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
            db.execute("DELETE FROM event_subscriptions WHERE expires_at <= ?", (now,))

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

    def clear_continuations_for_job(self, user_sub: str, job_id: str) -> int:
        with self.db() as db:
            cur = db.execute(
                "DELETE FROM continuations WHERE user_sub=? AND job_id=?",
                (user_sub, job_id),
            )
        return int(cur.rowcount or 0)

    def upsert_event_subscription(
        self,
        *,
        subscription_id: str,
        user_sub: str,
        name: str,
        arguments: dict[str, Any],
        callback_url: str,
        secret: str,
        expires_at: float,
        rotation_seconds: int = 300,
    ) -> dict[str, Any]:
        now = time.time()
        arguments_json = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.db() as db:
            current = db.execute(
                "SELECT secret,created_at FROM event_subscriptions "
                "WHERE subscription_id=? AND user_sub=?",
                (subscription_id, user_sub),
            ).fetchone()
            previous_secret = None
            previous_secret_until = None
            created_at = now
            if current is not None:
                created_at = float(current["created_at"])
                if str(current["secret"]) != secret:
                    previous_secret = str(current["secret"])
                    previous_secret_until = now + max(1, int(rotation_seconds))
            db.execute(
                "INSERT INTO event_subscriptions("
                "subscription_id,user_sub,name,arguments_json,callback_url,secret,"
                "previous_secret,previous_secret_until,expires_at,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(subscription_id) DO UPDATE SET "
                "name=excluded.name,arguments_json=excluded.arguments_json,"
                "callback_url=excluded.callback_url,secret=excluded.secret,"
                "previous_secret=excluded.previous_secret,"
                "previous_secret_until=excluded.previous_secret_until,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (
                    subscription_id,
                    user_sub,
                    name,
                    arguments_json,
                    callback_url,
                    secret,
                    previous_secret,
                    previous_secret_until,
                    float(expires_at),
                    created_at,
                    now,
                ),
            )
        return self.event_subscription(user_sub, subscription_id) or {}

    def event_subscription(
        self, user_sub: str, subscription_id: str
    ) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM event_subscriptions "
                "WHERE subscription_id=? AND user_sub=?",
                (subscription_id, user_sub),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        return value

    def active_event_subscriptions(
        self, user_sub: str, name: str
    ) -> list[dict[str, Any]]:
        now = time.time()
        with self.db() as db:
            db.execute("DELETE FROM event_subscriptions WHERE expires_at <= ?", (now,))
            rows = db.execute(
                "SELECT * FROM event_subscriptions "
                "WHERE user_sub=? AND name=? AND expires_at>? "
                "ORDER BY created_at,subscription_id",
                (user_sub, name, now),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["arguments"] = json.loads(value.pop("arguments_json"))
            result.append(value)
        return result

    def remove_event_subscription(
        self, user_sub: str, subscription_id: str
    ) -> bool:
        with self.db() as db:
            cur = db.execute(
                "DELETE FROM event_subscriptions "
                "WHERE subscription_id=? AND user_sub=?",
                (subscription_id, user_sub),
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

    def devices_for_user(self, user_sub: str) -> list[dict[str, Any]]:
        with self.db() as db:
            rows = db.execute(
                "SELECT device_id,name,created_at,last_seen FROM devices "
                "WHERE user_sub=? AND enabled=1 ORDER BY created_at ASC,device_id ASC",
                (user_sub,),
            ).fetchall()
        return [dict(row) for row in rows]

    def device_for_user(
        self, user_sub: str, selector: str | None = None
    ) -> dict[str, Any] | None:
        devices = self.devices_for_user(user_sub)
        if not devices:
            return None
        if selector:
            token = str(selector).strip()
            exact = [
                item
                for item in devices
                if item["device_id"] == token or item["name"] == token
            ]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise RuntimeError(
                    f"connector selector {token!r} matches multiple paired connectors; "
                    "use the connector device_id"
                )
            raise RuntimeError(f"unknown paired connector {token!r}")
        # The oldest enabled connector is the stable account default. Heartbeats
        # must never change routing when multiple connectors are online.
        return devices[0]

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
