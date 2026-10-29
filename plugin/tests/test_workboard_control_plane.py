"""Draft Workboard view reuses the existing MCP Control Plane and owner-scoped tool."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "relay" / "server.py"


class WorkboardControlPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SOURCE.read_text(encoding="utf-8")
        match = re.search(r'CONTROL_PLANE_WIDGET_V12_HTML = r"""([\s\S]*?)"""', source)
        if not match:
            raise AssertionError("v12 control plane widget missing")
        cls.html = match.group(1)

    def test_all_four_views_share_one_owner_scoped_snapshot(self):
        for section in ("wb-goals", "wb-items", "wb-actions", "wb-inbox"):
            self.assertIn('id="' + section + '"', self.html)
        self.assertIn('id="tab-workboard"', self.html)
        self.assertIn('callTool("workboard_list",{})', self.html)
        self.assertIn('array(board.human_actions)', self.html)
        self.assertIn('array(board.inbox)', self.html)

    def test_unavailable_tool_is_not_misrepresented_as_empty_workboard(self):
        self.assertIn('workboard-status', self.html)
        self.assertIn('共享任务试验尚不可用', self.html)
        self.assertIn('esc(', self.html)

    def test_inbox_receipts_require_explicit_ack_with_fence_and_revision(self):
        self.assertIn('data-ack-index=', self.html)
        self.assertIn('callTool("workboard_ack",args)', self.html)
        self.assertIn('fence:receipt.fence', self.html)
        self.assertIn('source_revision:receipt.source_revision', self.html)
        self.assertIn('receipt.acknowledged_at!=null', self.html)
        self.assertIn('esc(txt(x.evidence_json).slice(0,160))', self.html)
        # Loading or rendering a snapshot must never acknowledge on its own.
        load = self.html.split('async function loadWorkboard()', 1)[1].split('function render(snapshot)', 1)[0]
        self.assertNotIn('workboard_ack', load)

    def test_human_actions_are_explicit_and_fenced_not_auto_approved(self):
        self.assertIn('data-action-index=', self.html)
        self.assertIn('addEventListener("submit"', self.html)
        self.assertIn('tool="workboard_verify"', self.html)
        self.assertIn('tool="workboard_resolve"', self.html)
        self.assertIn('evidence_refs:[evidence]', self.html)
        self.assertIn('fd.get("stopped")!=="on"', self.html)
        self.assertIn('stopped:true', self.html)
        self.assertIn('item.fence!==action.fence', self.html)
        self.assertIn('item.source_revision!==action.source_revision', self.html)
        self.assertIn('callTool(tool,args)', self.html)
        self.assertIn('已确认原生进程停止并核对外部副作用', self.html)
        # No human action is executed during refresh or rendering.
        load = self.html.split('async function loadWorkboard()', 1)[1].split('function render(snapshot)', 1)[0]
        self.assertNotIn('workboard_resolve', load)
        self.assertNotIn('workboard_verify', load)

    @unittest.skipUnless(shutil.which("node"), "Node syntax checker unavailable")
    def test_widget_script_syntax(self):
        script = re.search(r'<script>([\s\S]*?)</script>', self.html)
        self.assertIsNotNone(script)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "widget.js"
            path.write_text(script.group(1), encoding="utf-8")
            result = subprocess.run(["node", "--check", str(path)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
