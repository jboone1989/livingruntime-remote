"""Static compatibility guard for the embedded MCP Control Plane."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "relay" / "server.py"

class ControlPlaneAgentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=SRC.read_text(encoding="utf-8")
        widget=re.search(r'CONTROL_PLANE_WIDGET_HTML = r"""([\s\S]*?)"""',source)
        if not widget:
            raise AssertionError("Control Plane widget not found")
        cls.html=widget.group(1)

    def test_agent_and_direct_work_separated(self):
        for text in ('id="agents"','id="sum-agents"','agent-adapter',
                     'id="running"','id="waiting"','id="completions"'):
            self.assertIn(text,self.html)
        self.assertIn('directJobs.filter',self.html)
        self.assertIn('delegatedJobs.filter',self.html)
        self.assertIn('completion receipts',self.html)

    def test_mobile_history_is_collapsed_and_compact(self):
        self.assertIn('History & diagnostics</summary>', self.html)
        self.assertIn('id="activity"', self.html)
        self.assertIn('activity.slice(-6).reverse()', self.html)
        self.assertIn('class="history-row"', self.html)
        self.assertIn('@media (max-width:560px)', self.html)
        self.assertNotIn('void requestDisplayMode("pip");', self.html)

    def test_live_work_and_pending_approvals_are_visible(self):
        for name in ('agents-section', 'running-section', 'waiting-section', 'attention-section'):
            self.assertIn('id="'+name+'" hidden', self.html)
        self.assertIn('q("attention-section").hidden=!(approvals.length||completions.length)', self.html)
        self.assertIn('q("idle-note").hidden=Boolean(', self.html)
        self.assertIn('q("sync-note").textContent="Updated "', self.html)
        self.assertNotIn('q("headline").textContent="Refreshing…"', self.html)

    @unittest.skipUnless(shutil.which("node"),"Node syntax checker unavailable")
    def test_embedded_javascript_is_syntactically_valid(self):
        match=re.search(r'<script>([\s\S]*?)</script>',self.html)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as td:
            script=Path(td)/"widget.js"
            script.write_text(match.group(1),encoding="utf-8")
            run=subprocess.run(["node","--check",str(script)],
                               capture_output=True,text=True,timeout=15,check=False)
            self.assertEqual(run.returncode,0,run.stderr)

if __name__=="__main__":
    unittest.main()
