from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_install_host():
    path = SCRIPTS / "install-host.py"
    spec = importlib.util.spec_from_file_location("install_host", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_host"] = module
    spec.loader.exec_module(module)
    return module


install_host = load_install_host()


class InstallHostTests(unittest.TestCase):
    def test_localize_config_uses_localhost_and_keeps_tunnel_ids(self) -> None:
        payload = {
            "tunnel_id": "tunnel_abc",
            "tunnel_organization_id": "org-abc",
            "hosts": {"main": {"ssh_host": "livingruntime-vm", "roots": ["/home/ubuntu"], "units": []}},
            "projects": {"virtualbrain": {"host": "main", "path": "/home/ubuntu/virtualbrain/current"}},
        }
        localized = install_host.localize_config(payload)
        self.assertEqual(localized["hosts"]["main"]["ssh_host"], "localhost")
        self.assertEqual(localized["tunnel_id"], "tunnel_abc")
        self.assertEqual(localized["tunnel_organization_id"], "org-abc")
        self.assertEqual(payload["hosts"]["main"]["ssh_host"], "livingruntime-vm")

    def test_systemd_unit_stays_loopback_and_secret_free(self) -> None:
        unit = install_host.systemd_unit(client=Path("/home/ubuntu/.livingruntime/bin/tunnel-client"))
        self.assertIn("livingruntime-remote", unit)
        self.assertIn("User=ubuntu", unit)
        self.assertIn("EnvironmentFile=", unit)
        self.assertNotIn("sk-", unit)
        self.assertNotIn("0.0.0.0", unit)
        self.assertNotIn("CONTROL_PLANE_API_KEY=", unit)
