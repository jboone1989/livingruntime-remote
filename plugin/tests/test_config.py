from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from configmodel import (  # noqa: E402
    join_project_path,
    list_projects,
    normalize,
    resolve_path,
    resolve_unit,
    seed_projects,
    to_storage,
)


class ConfigModelTests(unittest.TestCase):
    def test_legacy_config_still_loads(self) -> None:
        cfg = normalize(
            {
                "ssh_host": "livingruntime-vm",
                "roots": ["/home/ubuntu"],
                "systemd_units": ["content-agent.service"],
            }
        )
        self.assertEqual(cfg["hosts"]["main"]["ssh_host"], "livingruntime-vm")
        self.assertEqual(cfg["projects"], {})
        self.assertIn("content-agent.service", cfg["hosts"]["main"]["units"])

    def test_seeded_projects_cover_virtualbrain_and_ferro(self) -> None:
        cfg = seed_projects(
            normalize({"ssh_host": "livingruntime-vm", "roots": ["/home/ubuntu"]})
        )
        names = {row["name"] for row in list_projects(cfg)}
        self.assertEqual(names, {"agent-runtime", "virtualbrain", "ferro", "trading"})
        self.assertEqual(cfg["projects"]["virtualbrain"]["path"], "/home/ubuntu/virtualbrain/current")
        self.assertEqual(cfg["projects"]["ferro"]["units"], ["content-agent.service"])

    def test_project_relative_paths_and_units(self) -> None:
        cfg = seed_projects(normalize({"ssh_host": "livingruntime-vm", "roots": ["/home/ubuntu"]}))
        self.assertEqual(
            resolve_path(cfg, "pyproject.toml", project="virtualbrain"),
            "/home/ubuntu/virtualbrain/current/pyproject.toml",
        )
        self.assertEqual(
            resolve_path(cfg, "/home/ubuntu/virtualbrain/current/README.md", project="virtualbrain"),
            "/home/ubuntu/virtualbrain/current/README.md",
        )
        self.assertEqual(resolve_unit(cfg, None, project="ferro"), "content-agent.service")
        with self.assertRaises(PermissionError):
            resolve_unit(cfg, "ssh.service", project="ferro")
        with self.assertRaises(PermissionError):
            join_project_path("/home/ubuntu/virtualbrain", "../secret")

    def test_storage_round_trip(self) -> None:
        cfg = seed_projects(normalize({"ssh_host": "livingruntime-vm", "roots": ["/home/ubuntu"]}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote.json"
            path.write_text(json.dumps(to_storage(cfg), indent=2), encoding="utf-8")
            loaded = normalize(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(loaded["projects"]["ferro"]["path"], "/home/ubuntu/wechat-traffic-agent")


if __name__ == "__main__":
    unittest.main()
