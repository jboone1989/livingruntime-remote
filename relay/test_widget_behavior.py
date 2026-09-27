import json
import shutil
import subprocess
import unittest
from pathlib import Path
from test_server import server


class WidgetBehaviorTests(unittest.TestCase):
    def test_real_widget_scripts_under_transport_failures(self):
        node = shutil.which("node")
        if not node:
            self.fail("Node.js is required for watcher behavior tests")
        result = subprocess.run([node, str(Path(__file__).with_name("widget_behavior.cjs"))],
            input=json.dumps({"long": server.LONG_JOB_WIDGET_HTML, "pi": server.PI_JOB_WIDGET_HTML}),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
