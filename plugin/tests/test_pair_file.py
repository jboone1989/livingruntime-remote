import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from connector import read_pair_file

class PairFileTests(unittest.TestCase):
    def test_owner_only_pair_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"pair"
            p.write_text("ABCD-1234\n")
            p.chmod(0o600)
            self.assertEqual(read_pair_file(p),"ABCD-1234")
            p.chmod(0o644)
            with self.assertRaisesRegex(ValueError,"group"):
                read_pair_file(p)
    def test_rejects_invalid_pair(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"pair"
            p.write_text("invalid")
            p.chmod(0o600)
            with self.assertRaisesRegex(ValueError,"format"):
                read_pair_file(p)
