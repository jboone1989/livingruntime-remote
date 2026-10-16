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
    def test_shell_installer_offline_smoke(self):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            fake_curl=root/"curl"
            fake_curl.write_text("""#!/bin/sh
while [ "$#" -gt 0 ]; do
 if [ "$1" = "--output" ]; then
  shift
  cat > "$1" <<'END'
#!/bin/sh
[ "$1" = "--version" ] && echo 0.4.67
END
  chmod 700 "$1"
  exit 0
 fi
 shift
done
exit 1
""")
            fake_curl.chmod(0o755)
            env=dict(os.environ,PATH=str(root)+":"+os.environ.get("PATH",""),
                     LIVINGRUNTIME_INSTALL_DIR=str(root/"bin"))
            script=str(ROOT/"plugin/scripts/install-connector.sh")
            syntax=subprocess.run(["sh","-n",script],capture_output=True,text=True)
            self.assertEqual(syntax.returncode,0,syntax.stderr)
            result=subprocess.run(["sh",script],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            binary=root/"bin"/"livingruntime-remote"
            self.assertTrue(binary.exists())
            check=subprocess.run([str(binary),"--version"],capture_output=True,text=True)
            self.assertEqual(check.stdout.strip(),"0.4.67")
    def test_npm_package_not_published_claim(self):
        manifest=json.loads((ROOT/"packages/npm/package.json").read_text())
        self.assertTrue(manifest["private"])
        self.assertEqual(manifest["bin"]["livingruntime-remote"],"bin/remote.js")
        launcher=(ROOT/"packages/npm/bin/remote.js").read_text()
        self.assertIn("spawnSync",launcher)
        self.assertIn("releases/download/v",launcher)
        self.assertIn("Executable version mismatch",launcher)
