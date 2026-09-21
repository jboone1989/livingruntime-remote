from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import tunnel

TASK_NAME = "LivingRuntimeRemoteTunnelCompanion"
DESKTOP_IMAGES = {"chatgpt.exe"}
DESKTOP_PATH_MARKERS = ("openai.codex", r"\app\chatgpt.exe", "windowsapps")
POLL_SECONDS = 8.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def state_dir() -> Path:
    path = Path.home() / ".livingruntime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return state_dir() / "companion.log"


def pid_path() -> Path:
    return state_dir() / "companion.pid"


def log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {message}"
    with log_path().open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    try:
        print(line, file=sys.stderr)
    except OSError:
        return


def is_chatgpt_desktop(image: str, path: str = "") -> bool:
    name = image.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name not in DESKTOP_IMAGES:
        return False
    if not path.strip():
        return True
    lowered = path.lower()
    return any(marker in lowered for marker in DESKTOP_PATH_MARKERS)


def pythonw_path() -> Path:
    current = Path(sys.executable)
    candidate = current.with_name("pythonw.exe")
    if os.name == "nt" and candidate.exists():
        return candidate
    return current


def run_hidden(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("errors", "replace")
    return subprocess.run(argv, **kwargs)


def current_user_id() -> str:
    domain = os.environ.get("USERDOMAIN") or ""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if domain and user:
        return f"{domain}\\{user}"
    completed = run_hidden(["whoami"])
    return (completed.stdout or "").strip() or user


def task_xml(python: Path, script: Path, user_id: str) -> str:
    command = escape(str(python))
    arguments = escape(f'"{script}" --watch')
    user = escape(user_id)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Start LivingRuntime Secure MCP Tunnel when ChatGPT Desktop is running.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except SystemError:
        return False
    return True


def take_watch_lock() -> bool:
    path = pid_path()
    if path.exists():
        try:
            old = int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            old = 0
        if old and old != os.getpid() and pid_is_alive(old):
            return False
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def desktop_running() -> bool:
    if os.name != "nt":
        return False
    for name, pid in tunnel.list_windows_processes():
        if name.lower() not in DESKTOP_IMAGES:
            continue
        path = tunnel.process_image_path(pid)
        if is_chatgpt_desktop(name, path):
            return True
    return False


def ensure_tunnel() -> bool:
    ready = tunnel.ensure_tunnel_running()
    if ready:
        log("tunnel ready")
    else:
        log("tunnel did not become ready")
    return ready


def watch(poll_seconds: float = POLL_SECONDS) -> int:
    if not take_watch_lock():
        log("companion already running")
        return 0
    log("watching for ChatGPT Desktop")
    while True:
        try:
            if desktop_running():
                if not tunnel.health_ready():
                    ensure_tunnel()
        except Exception as exc:
            log(f"watch error: {exc}")
        time.sleep(poll_seconds)


def install_task() -> None:
    if os.name != "nt":
        raise SystemExit("ChatGPT Desktop companion is a Windows scheduled task.")
    python = pythonw_path()
    script = Path(__file__).resolve()
    xml_path = state_dir() / "companion-task.xml"
    xml_path.write_text(task_xml(python, script, current_user_id()), encoding="utf-16")
    run_hidden(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    completed = run_hidden(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path)])
    if completed.returncode != 0:
        raise SystemExit((completed.stdout or "") + (completed.stderr or "") or "schtasks /Create failed")
    run = run_hidden(["schtasks", "/Run", "/TN", TASK_NAME])
    if run.returncode != 0:
        log((run.stdout or "") + (run.stderr or "") or "schtasks /Run failed")
    print(f"Registered Windows task {TASK_NAME}")
    print("It waits for ChatGPT Desktop, then starts tunnel-client in the background.")


def uninstall_task() -> None:
    if os.name != "nt":
        return
    run_hidden(["schtasks", "/End", "/TN", TASK_NAME])
    run_hidden(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    print(f"Removed Windows task {TASK_NAME}")


def status() -> int:
    desktop = desktop_running()
    ready = tunnel.health_ready()
    client = tunnel.tunnel_client_running()
    print(f"chatgpt_desktop: {'running' if desktop else 'stopped'}")
    print(f"tunnel_client: {'running' if client else 'stopped'}")
    print(f"readyz: {'ready' if ready else 'not ready'}")
    return 0 if (not desktop or ready) else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Start LivingRuntime tunnel-client when ChatGPT Desktop is running."
    )
    parser.add_argument("--install", action="store_true", help="Register the Windows logon companion task")
    parser.add_argument("--uninstall", action="store_true", help="Remove the Windows companion task")
    parser.add_argument("--watch", action="store_true", help="Wait for ChatGPT Desktop and keep the tunnel up")
    parser.add_argument("--once", action="store_true", help="Start the tunnel now if ChatGPT Desktop is running")
    parser.add_argument("--status", action="store_true", help="Print Desktop and tunnel status")
    args = parser.parse_args(argv)
    if args.uninstall:
        uninstall_task()
        return
    if args.install:
        install_task()
        return
    if args.status:
        raise SystemExit(status())
    if args.once:
        if not desktop_running():
            print("ChatGPT Desktop is not running; tunnel was left unchanged.")
            return
        raise SystemExit(0 if ensure_tunnel() else 1)
    if args.watch:
        raise SystemExit(watch())
    parser.print_help()
    raise SystemExit(2)


if __name__ == "__main__":
    main()
