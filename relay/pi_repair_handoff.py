"""Durable owner/device-scoped handoff of native Pi patches to a trusted writer.

The Relay stores untrusted repair data, not GitHub credentials, and never pushes
a branch. GitHub writes are only performed by the separate validated writer.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
import time

SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_RECEIPT_BYTES = 220_000


class RepairHandoff:
    def __init__(self, store):
        self.store = store
        with closing(store.db()) as db, db:
            db.execute("""
              CREATE TABLE IF NOT EXISTS pi_repair_handoffs(
                session_id TEXT PRIMARY KEY,
                user_sub TEXT NOT NULL, connector_id TEXT NOT NULL,
                pr_number INTEGER NOT NULL, original_sha TEXT NOT NULL,
                manifest_json TEXT, state TEXT NOT NULL DEFAULT 'RUNNING',
                new_sha TEXT, created_at REAL NOT NULL,
                UNIQUE(user_sub, pr_number, original_sha)
              )""")

    def record(self, user, connector, number, sha, session):
        if not SHA.fullmatch(sha) or not 1 <= number <= 100000 or not session.startswith("lra_"):
            raise ValueError("invalid repair launch receipt")
        with closing(self.store.db()) as db, db:
            db.execute("""INSERT OR IGNORE INTO pi_repair_handoffs
              (session_id,user_sub,connector_id,pr_number,original_sha,created_at)
              VALUES(?,?,?,?,?,?)""", (session, user, connector, number, sha, time.time()))
            row = db.execute("SELECT * FROM pi_repair_handoffs WHERE user_sub=? AND pr_number=? AND original_sha=?",
                             (user, number, sha)).fetchone()
            if row is None or row["session_id"] != session or row["connector_id"] != connector:
                raise PermissionError("another repair session is already bound to this HEAD")

    def submit(self, user, connector, session, manifest):
        if not isinstance(manifest, dict) or set(manifest) != {
            "repository", "pr_number", "expected_head_sha", "changes"
        } or manifest["repository"] != "jboone1989/EventGraph":
            raise ValueError("invalid patch envelope")
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise ValueError("patch exceeds relay handoff limit")
        with closing(self.store.db()) as db, db:
            row = db.execute("SELECT * FROM pi_repair_handoffs WHERE session_id=? AND user_sub=? AND connector_id=?",
                             (session, user, connector)).fetchone()
            if (row is None or manifest["pr_number"] != row["pr_number"] or
                    manifest["expected_head_sha"] != row["original_sha"]):
                raise PermissionError("patch is not from this owner/device/repair session and HEAD")
            if row["state"] == "READY" and row["manifest_json"] == encoded:
                return
            if row["state"] != "RUNNING":
                raise PermissionError("repair handoff has already been consumed")
            db.execute("UPDATE pi_repair_handoffs SET state='READY',manifest_json=? WHERE session_id=?",
                       (encoded, session))

    def pending(self, user):
        with closing(self.store.db()) as db:
            rows = db.execute("""SELECT session_id,pr_number,original_sha,manifest_json
              FROM pi_repair_handoffs WHERE user_sub=? AND state='READY'
              ORDER BY created_at LIMIT 5""", (user,)).fetchall()
            return [dict(session_id=r["session_id"], pr_number=r["pr_number"],
                         original_sha=r["original_sha"], manifest=json.loads(r["manifest_json"]))
                    for r in rows]

    def acknowledge(self, user, session, original_sha, new_sha):
        if not SHA.fullmatch(original_sha) or not SHA.fullmatch(new_sha) or original_sha == new_sha:
            raise ValueError("invalid pinned head transition")
        with closing(self.store.db()) as db, db:
            result = db.execute("""UPDATE pi_repair_handoffs SET state='DONE',new_sha=?
              WHERE session_id=? AND user_sub=? AND original_sha=? AND state='READY'""",
                                (new_sha, session, user, original_sha))
            if result.rowcount != 1:
                raise PermissionError("stale or already consumed handoff")
