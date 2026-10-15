import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

class DistributionTests(unittest.TestCase):
    def test_install_script_installs_without_pairing(self):
        script=(ROOT/"plugin/scripts/install-connector.sh").read_text()
        self.assertIn("mktemp",script)
        self.assertIn("mv -f",script)
        self.assertIn("--version",script)
        self.assertNotIn('read -r PAIR_CODE',script)
        self.assertNotIn(' --pair "$PAIR_CODE"',script)
    def test_npm_package_not_published_claim(self):
        manifest=json.loads((ROOT/"packages/npm/package.json").read_text())
        self.assertTrue(manifest["private"])
        self.assertEqual(manifest["bin"]["livingruntime-remote"],"bin/remote.js")
        launcher=(ROOT/"packages/npm/bin/remote.js").read_text()
        self.assertIn("spawnSync",launcher)
        self.assertIn("releases/download/v",launcher)
        self.assertIn("Executable version mismatch",launcher)
