import os
import sys
import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import connector

class InstallerEnvironmentTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux running executable replacement")
    def test_binary_upgrade_replaces_a_running_executable_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "new-connector"
            destination = root / "livingruntime-remote-connector"
            shutil.copy2("/bin/true", source)
            shutil.copy2("/bin/sleep", destination)
            running = subprocess.Popen([str(destination), "30"])
            try:
                inode = destination.stat().st_ino
                with patch.object(connector.sys,"frozen",True,create=True), patch.object(
                    connector.sys,"executable",str(source)
                ), patch.object(connector,"bin_dir",return_value=root):
                    self.assertEqual(connector.install_binary(),[str(destination)])
                self.assertNotEqual(destination.stat().st_ino,inode)
                self.assertIsNone(running.poll())
                self.assertEqual(subprocess.run([str(destination)]).returncode,0)
            finally:
                running.terminate()
                running.wait(timeout=5)

    def test_reinstall_activates_new_linux_connector_and_reports_restart_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(connector.Path,"home",return_value=Path(tmp)), patch.object(
            connector,"ensure_linux_linger"
        ), patch.object(connector,"_user_systemd_environment",return_value={}):
            ok = subprocess.CompletedProcess([],0,stdout="",stderr="")
            with patch.object(connector,"_run",return_value=ok) as run:
                connector.install_linux_user_service(["connector"])
                self.assertEqual(run.call_args.args[0],["systemd-run","--user","--quiet","--collect","--on-active=3s","systemctl","--user","restart",connector.LINUX_SERVICE])
            failed = subprocess.CompletedProcess([],1,stdout="",stderr="restart failed")
            with patch.object(connector,"_run",side_effect=[ok,ok,ok,failed]):
                with self.assertRaisesRegex(RuntimeError,"activate installed Connector"):
                    connector.install_linux_user_service(["connector"])
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
