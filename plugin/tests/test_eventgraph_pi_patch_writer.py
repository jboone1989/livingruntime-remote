"""No-network, no-credential tests for trusted Pi repair writer."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eventgraph_pi_patch_writer as writer

SHA = "a" * 40
NEXT = "b" * 40
TREE = "c" * 40
BASE_BLOB = "d" * 40
REPO = writer.REPOSITORY
PATH = "packages/backend/src/editorial/explain.ts"
BASE = {"path": PATH, "mode": "100644", "type": "blob", "sha": BASE_BLOB}


def manifest(path=PATH, expected=BASE_BLOB, content="export const x = 1;"):
    return {"repository": REPO, "pr_number": 175, "expected_head_sha": SHA,
            "changes": [{"path": path, "expected_blob_sha": expected, "content": content}]}


class PatchWriterTests(unittest.TestCase):
    def test_rejects_forbidden_paths_and_executable_artifacts(self):
        cases = [
            ".github/workflows/review.yml", "scripts/ci/check-spec.mjs",
            "packages/backend/src/db.ts", "packages/backend/src/auth/index.ts",
            "packages/backend/src/editorial/../auth.ts",
            "apps/web/app/features/../config.ts",
            "apps/web/app/features/config/adapter.ts",
            "apps/web/app/features/auth.ts",
            "apps/web/app/features/session-secret.ts",
            "apps/web/app/features/./okay.ts",
            "apps/web/app/features/.hidden.ts",
            "apps/web/app/features/build.sh",
            "apps/web/app/features/out.json",
            "apps/web/app/features/file.ts\\evil",
            "apps/web/app/features/tést.ts", "/apps/web/app/features/a.ts",
        ]
        for path in cases:
            with self.subTest(path=path), self.assertRaises(ValueError):
                writer.validate(manifest(path=path))

    def test_bounded_manifest_and_exact_blob_sha(self):
        writer.validate(manifest())
        for value in [{**manifest(), "repository": "someone/else"},
                      {**manifest(), "expected_head_sha": "bad"},
                      {**manifest(), "pr_number": True},
                      {**manifest(), "changes": []},
                      {**manifest(), "changes": manifest()["changes"] * 25},
                      manifest(content="A" * (writer.MAX_BYTES + 1)),
                      manifest(expected="bad")]:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                writer.validate(value)
        duplicated = manifest()
        duplicated["changes"].append(duplicated["changes"][0].copy())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            writer.validate(duplicated)

    def test_pr_must_match_current_head_and_target(self):
        valid = {"state": "open", "draft": False, "base": {"ref": "develop"},
                 "head": {"repo": {"full_name": REPO}, "sha": SHA, "ref": "fix/explain"}}
        with patch.object(writer, "api", return_value=valid):
            self.assertEqual(writer.current_pr(175, SHA), "fix/explain")
        for delta in [{"state": "closed"}, {"draft": True}, {"base": {"ref": "main"}},
                      {"head": {**valid["head"], "sha": NEXT}},
                      {"head": {**valid["head"], "ref": "-force"}},
                      {"head": {**valid["head"], "repo": {"full_name": "fork/repo"}}}]:
            with self.subTest(delta=delta), patch.object(writer, "api",
                    return_value={**valid, **delta}), self.assertRaises((ValueError, PermissionError)):
                writer.current_pr(175, SHA)

    def test_no_write_when_base_blob_or_mode_disagrees(self):
        def fake(path, *, method="GET", body=None):
            if path.startswith("git/commits/"):
                return {"tree": {"sha": TREE}}
            if path.startswith("git/trees/"):
                return {"truncated": False, "tree": [BASE]}
            self.fail("must not write when base has a conflict")
        with patch.object(writer, "api", side_effect=fake):
            with self.assertRaisesRegex(PermissionError, "version"):
                writer.prepare_tree(SHA, manifest(expected=NEXT)["changes"])
        with patch.object(writer, "api", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "new files"):
                writer.prepare_tree(SHA, manifest(path="apps/web/app/features/new.ts", expected=None)["changes"])

    def test_atomic_non_force_commit_pinned_to_pr_head(self):
        commands = []
        pr = {"state": "open", "draft": False, "base": {"ref": "develop"},
              "head": {"repo": {"full_name": REPO}, "sha": SHA, "ref": "fix/explain"}}
        def fake(path, *, method="GET", body=None):
            commands.append((path, method, body))
            if path.startswith("pulls/"):
                return pr
            if path == "git/commits/" + SHA:
                return {"tree": {"sha": TREE}}
            if path == "git/trees/" + TREE + "?recursive=1":
                return {"truncated": False, "tree": [BASE]}
            if path == "git/blobs":
                return {"sha": "e" * 40}
            if path == "git/trees":
                return {"sha": "f" * 40}
            if path == "git/commits":
                self.assertEqual(body["parents"], [SHA])
                self.assertEqual(body["tree"], "f" * 40)
                return {"sha": NEXT}
            if path == "git/refs/heads/fix/explain":
                self.assertEqual(body, {"sha": NEXT, "force": False})
                return {}
            self.fail("unexpected GitHub API operation: " + path)
        with patch.object(writer, "api", side_effect=fake):
            receipt = writer.write(manifest())
        self.assertEqual(receipt, {"pr_number": 175, "previous_sha": SHA, "new_sha": NEXT})
        self.assertEqual(sum(x[0] == "pulls/175" for x in commands), 2)
        self.assertEqual([x[0] for x in commands][-1], "git/refs/heads/fix/explain")
        self.assertTrue(all(x[2] is None or "force" not in x[2] or x[2]["force"] is False for x in commands))

    def test_no_op_tree_is_rejected(self):
        def fake(path, *, method="GET", body=None):
            if path == "git/commits/" + SHA:
                return {"tree": {"sha": TREE}}
            if path == "git/trees/" + TREE + "?recursive=1":
                return {"truncated": False, "tree": [BASE]}
            if path == "git/blobs":
                return {"sha": "e" * 40}
            if path == "git/trees":
                return {"sha": TREE}
            self.fail("no-op manifest must not create commit or update ref")
        with patch.object(writer, "api", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "did not change"):
                writer.prepare_tree(SHA, manifest()["changes"])

    def test_lost_ack_recovers_exact_committed_patch_without_new_write(self):
        import hashlib
        data = manifest()
        raw = data["changes"][0]["content"].encode()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\x00" + raw).hexdigest()
        expected_message = f"fix(pr-175): Pi repair of verified failure at {SHA[:12]}"
        paths = []
        def fake(path, *, method="GET", body=None):
            paths.append((path, method))
            self.assertEqual(method, "GET", "replay must never write")
            if path == "pulls/175":
                return {"state": "open", "draft": False, "base": {"ref": "develop"},
                        "head": {"repo": {"full_name": REPO}, "sha": NEXT, "ref": "fix/explain"}}
            if path == "git/commits/" + NEXT:
                return {"message": expected_message, "parents": [{"sha": SHA}],
                        "tree": {"sha": TREE}}
            if path == "git/trees/" + TREE + "?recursive=1":
                return {"truncated": False, "tree": [{**BASE, "sha": blob}]}
            self.fail("unexpected reconciliation call")
        with patch.object(writer, "api", side_effect=fake):
            result = writer.write(data)
        self.assertEqual(result["new_sha"], NEXT)
        self.assertNotIn(("git/refs/heads/fix/explain", "PATCH"), paths)

    def test_lost_ack_does_not_accept_unrelated_commit(self):
        with patch.object(writer, "api", side_effect=[
            {"state": "open", "draft": False, "base": {"ref": "develop"},
             "head": {"repo": {"full_name": REPO}, "sha": NEXT}},
            {"state": "open", "draft": False, "base": {"ref": "develop"},
             "head": {"repo": {"full_name": REPO}, "sha": NEXT}},
            {"message": "unrelated commit", "parents": [{"sha": SHA}], "tree": {"sha": TREE}},
        ]):
            with self.assertRaises(PermissionError):
                writer.write(manifest())

    def test_pre_commit_race_fails_without_pushing(self):
        pr = {"state": "open", "draft": False, "base": {"ref": "develop"},
              "head": {"repo": {"full_name": REPO}, "sha": SHA, "ref": "fix/explain"}}
        hits = []
        def fake(path, *, method="GET", body=None):
            hits.append(path)
            if path.startswith("pulls/"):
                return pr if hits.count("pulls/175") == 1 else {
                    **pr, "head": {**pr["head"], "sha": NEXT}}
            if path == "git/commits/" + SHA: return {"tree": {"sha": TREE}}
            if path.startswith("git/trees/" + TREE): return {"truncated": False, "tree": [BASE]}
            if path == "git/blobs": return {"sha": "e" * 40}
            if path == "git/trees": return {"sha": "f" * 40}
            if path == "git/commits": return {"sha": "f" * 40}
            self.fail("stale SHA must not update ref")
        with patch.object(writer, "api", side_effect=fake), self.assertRaises(PermissionError):
            writer.write(manifest())
        self.assertNotIn("git/refs/heads/fix/explain", hits)


if __name__ == "__main__":
    unittest.main()
