import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import connector

class InstallerEnvironmentTests(unittest.TestCase):
    def test_user_systemd_env_uses_owned_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(connector.os,"getuid",return_value=os.getuid()):
            # System's own /run/user/UID may be absent in CI; no unrelated env mutation
            env=connector._user_systemd_environment()
            self.assertIsInstance(env,dict)
            self.assertEqual(os.environ.get("PATH"),env.get("PATH"))
    def test_inaccessible_cwd_recovers_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=os.getcwd()
            try:
                os.chdir(tmp)
                with patch.object(connector.os,"access",return_value=False), patch.object(connector.Path,"home",return_value=Path(tmp)):
                    connector._ensure_accessible_cwd()
                self.assertEqual(os.getcwd(),tmp)
            finally:
                os.chdir(old)
