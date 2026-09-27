"""Durable delivery of executed results. Retrying delivery never executes a tool."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any


@contextmanager
def connector_lock(path: Path):
    """Do not recover another live Connector's in-flight execution as a crash."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("A Connector is already using this pairing configuration") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class ResultOutbox:
    def __init__(self, path: Path, owner: str):
        self.path, self.owner = path, owner
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS results(
                owner TEXT NOT NULL, task_id TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(owner,task_id))""")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, task_id: str) -> bool:
        with self.db() as db:
            return db.execute(
                "INSERT OR IGNORE INTO results(owner,task_id,state) VALUES(?,?,'executing')",
                (self.owner, task_id),
            ).rowcount == 1

    def finish(self, task_id: str, result: dict[str, Any]) -> None:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 1048576:
            payload = '{"ok":false,"error":"device result exceeds relay limit"}'
        with self.db() as db:
            db.execute("UPDATE results SET state='ready',payload=? WHERE owner=? AND task_id=?",
                       (payload, self.owner, task_id))

    def recover_interrupted(self) -> None:
        # A crash may occur after side effects and before finish(). Never replay
        # the command when its outcome is unknown.
        payload = json.dumps({"ok": False, "error":
            "Connector restarted during execution; outcome unknown. Inspect durable receipts and state before retrying the command."})
        with self.db() as db:
            db.execute("UPDATE results SET state='ready',payload=? WHERE owner=? AND state='executing'",
                       (payload, self.owner))

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT task_id,payload,attempts FROM results WHERE owner=? AND state='ready' AND retry_at<=? LIMIT 8",
                (self.owner, time.time() if now is None else now))]

    def retry(self, task_id: str, attempts: int) -> None:
        with self.db() as db:
            db.execute("UPDATE results SET attempts=?,retry_at=? WHERE owner=? AND task_id=?",
                       (attempts + 1, time.time() + min(60, 2 ** min(attempts, 6)), self.owner, task_id))

    def delivered(self, task_id: str) -> None:
        with self.db() as db:
            # Keep a payload-free tombstone so duplicate claims cannot re-execute.
            db.execute("UPDATE results SET state='delivered',payload=NULL WHERE owner=? AND task_id=?",
                       (self.owner, task_id))
