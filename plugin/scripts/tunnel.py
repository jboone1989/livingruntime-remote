from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from contract import DEFAULT_HTTP_PORT, LOOPBACK_HOSTS

RELEASES_API = "https://api.github.com/repos/openai/tunnel-client/releases/latest"
PROFILE = "livingruntime-remote"


def config_path() -> Path:
    return Path.home() / ".livingruntime" / "remote.json"


def bin_dir() -> Path:
    return Path.home() / ".livingruntime" / "bin"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("remote config must be a JSON object")
    return value


def launcher_command(python: str | None = None) -> list[str]:
    launcher = Path(__file__).resolve().with_name("launcher.py")
    return [python or sys.executable, str(launcher)]


def _cli_path(value: str) -> str:
    # tunnel-client treats backslashes as escapes in --mcp-command.
    # Preserve absolute executable paths so a venv Python is not resolved
    # through its symlink to the system interpreter.
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if os.name == "nt":
        return path.absolute().as_posix()
    return str(path.absolute())


def quoted_mcp_command(python: str | None = None) -> str:
    command = [_cli_path(part) for part in launcher_command(python)]
    return " ".join(f'"{part}"' if (" " in part or os.name == "nt") else part for part in command)


def resolve_tunnel_id(explicit: str | None = None) -> str:
    value = (explicit or os.environ.get("CONTROL_PLANE_TUNNEL_ID") or load_config().get("tunnel_id") or "").strip()
    if not value:
        raise SystemExit(
            "Missing tunnel id. Create one in OpenAI Platform tunnel settings, then pass --tunnel-id "
            "or set CONTROL_PLANE_TUNNEL_ID. Do not put the runtime API key in the plugin config."
        )
    if value.startswith("sk-") or " " in value:
        raise SystemExit("That value looks like a secret. Use the tunnel_... identifier, not an API key.")
    if not value.startswith("tunnel_"):
        raise SystemExit("tunnel id must start with tunnel_")
    return value


def runtime_key_path() -> Path:
    return Path.home() / ".livingruntime" / "control-plane.key"


def load_runtime_key() -> str:
    key = os.environ.get("CONTROL_PLANE_API_KEY", "").strip()
    if key:
        return key
    path = runtime_key_path()
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise SystemExit(
        "CONTROL_PLANE_API_KEY is required to run tunnel-client. Export the runtime key from "
        "Platform tunnel settings into the environment or ~/.livingruntime/control-plane.key; "
        "never store it in remote.json or the plugin."
    )


def require_runtime_key() -> str:
    return load_runtime_key()


def health_url() -> str:
    return "http://127.0.0.1:8080/readyz"


def health_ready(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(health_url(), timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace").strip().lower()
            return 200 <= int(response.status) < 300 and "ready" in body
    except OSError:
        return False


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 260


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def list_windows_processes() -> list[tuple[str, int]]:
    if os.name != "nt":
        return []
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        found: list[tuple[str, int]] = []
        while True:
            found.append((entry.szExeFile, int(entry.th32ProcessID)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return found
    finally:
        kernel32.CloseHandle(snapshot)


def process_image_path(pid: int) -> str:
    if os.name != "nt" or pid <= 0:
        return ""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def image_running(image: str) -> bool:
    target = image.lower()
    if os.name == "nt":
        return any(name.lower() == target for name, _pid in list_windows_processes())
    completed = subprocess.run(["pgrep", "-x", image], capture_output=True, text=True)
    return completed.returncode == 0


def tunnel_client_running() -> bool:
    return image_running("tunnel-client.exe" if os.name == "nt" else "tunnel-client")


def tunnel_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CONTROL_PLANE_TUNNEL_ID"] = resolve_tunnel_id()
    env["CONTROL_PLANE_API_KEY"] = load_runtime_key()
    org_id = (
        os.environ.get("CONTROL_PLANE_ORGANIZATION_ID") or str(load_config().get("tunnel_organization_id") or "")
    ).strip()
    if org_id:
        env["CONTROL_PLANE_ORGANIZATION_ID"] = org_id
    return env


def start_tunnel_detached(client: Path | None = None) -> subprocess.Popen | None:
    if health_ready():
        return None
    if tunnel_client_running():
        return None
    resolved = find_tunnel_client(str(client) if client else None)
    if resolved is None:
        raise SystemExit(
            "tunnel-client was not found. Install it with tunnel.py --install-client before starting the companion."
        )
    log_path = Path.home() / ".livingruntime" / "tunnel-client.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    else:
        start_new_session = True
    return subprocess.Popen(
        [str(resolved), "run", "--profile", PROFILE],
        env=tunnel_env(),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        start_new_session=start_new_session,
        close_fds=os.name != "nt",
    )


def wait_until_ready(timeout: float = 20.0) -> bool:
    deadline = timeout
    step = 0.4
    elapsed = 0.0
    while elapsed < deadline:
        if health_ready():
            return True
        time.sleep(step)
        elapsed += step
    return health_ready()


def ensure_tunnel_running(timeout: float = 20.0) -> bool:
    if health_ready():
        return True
    start_tunnel_detached()
    return wait_until_ready(timeout)


def validate_http_bind(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("HTTP MCP bind must stay on loopback; do not expose 8765/8766 to the public internet")
    return host


def validate_health_listen_addr(value: str) -> str:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError("health listen address must be host:port on loopback")
    validate_http_bind(host)
    return value


def _cpu_arch() -> str:
    if hasattr(os, "uname"):
        return os.uname().machine.lower()
    return (os.environ.get("PROCESSOR_ARCHITECTURE") or os.environ.get("PROCESSOR_ARCHITEW6432") or "").lower()


def platform_asset_tokens() -> tuple[str, ...]:
    system = sys.platform
    machine = _cpu_arch()
    if system.startswith("win"):
        arch = "arm64" if "arm" in machine else "amd64"
        return ("windows", "win", arch)
    if system == "darwin":
        arch = "arm64" if "arm" in machine else "amd64"
        return ("darwin", "macos", arch)
    arch = "arm64" if "arm" in machine else "amd64"
    return ("linux", arch)


def choose_release_asset(assets: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = platform_asset_tokens()
    scored = []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if "tunnel-client" not in name and "tunnel_client" not in name:
            continue
        if any(skip in name for skip in ("license", "spdx", "provenance", "openvex", "source", "scan-manifest", ".txt", ".json")):
            continue
        if not name.endswith((".zip", ".exe", ".tgz", ".tar.gz")):
            continue
        if not all(token in name for token in tokens[-1:]):
            continue
        score = sum(1 for token in tokens if token in name)
        if name.endswith(".zip") or name.endswith(".exe"):
            score += 3
        if "runtime-cloudflared" not in name and "-runtime-" not in name:
            score += 2
        if os.name == "nt" and name.endswith(".exe"):
            score += 2
        scored.append((score, name, asset))
    if not scored:
        raise SystemExit(f"No tunnel-client release asset matched this platform: {tokens}")
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][2]


def find_tunnel_client(explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    for name in ("tunnel-client.exe", "tunnel-client"):
        found = shutil.which(name)
        if found:
            return Path(found)
        candidate = bin_dir() / name
        if candidate.exists():
            return candidate
    return None


def install_tunnel_client() -> Path:
    request = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "livingruntime-remote"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    asset = choose_release_asset(payload.get("assets") or [])
    url = asset.get("browser_download_url")
    if not url:
        raise SystemExit("tunnel-client release asset is missing a download URL")
    destination_dir = bin_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "tunnel-client.exe" if os.name == "nt" else "tunnel-client"
    destination = destination_dir / binary_name
    download_name = str(asset.get("name") or Path(url).name)
    archive = destination_dir / download_name
    urllib.request.urlretrieve(url, archive)
    if download_name.endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(archive) as bundle:
            member = next(
                (
                    item
                    for item in bundle.namelist()
                    if item.replace("\\", "/").rsplit("/", 1)[-1].lower()
                    in {binary_name.lower(), "tunnel-client", "tunnel-client.exe"}
                ),
                None,
            )
            if member is None:
                raise SystemExit(f"zip {download_name} does not contain {binary_name}")
            with bundle.open(member) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(archive, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IEXEC)
    return destination


def chatgpt_steps(tunnel_id: str) -> str:
    return f"""ChatGPT ordinary chat setup (Secure MCP Tunnel)

1. Keep this tunnel-client process running. Discovery and every tool call need it.
2. In ChatGPT, enable Developer mode if your plan allows it.
3. Open ChatGPT Plugins and select the plus button.
4. Name the app LivingRuntime Remote.
5. Under Connection, choose Tunnel and select or paste:
   {tunnel_id}
6. Create the app, review the discovered tools, and confirm the snapshot matches REMOTE_TOOLS
   including capabilities and apply_patch.
7. Open a new ordinary chat, enable LivingRuntime Remote from the tools/plugins menu,
   then call capabilities -> connection_status -> read_file -> apply_patch -> git -> logs.

Do not paste a localhost URL into ChatGPT. Do not expose port 8765 or 8766 to the public internet.
A local marketplace entry or Codex stdio config is not evidence that ChatGPT ordinary chat can see this plugin.
"""


def build_init_argv(
    client: Path,
    tunnel_id: str,
    *,
    http: bool,
    host: str,
    port: int,
    health_listen_addr: str = "127.0.0.1:8080",
    force: bool = False,
) -> list[str]:
    argv = [
        str(client),
        "init",
        "--sample",
        "sample_mcp_stdio_local",
        "--profile",
        PROFILE,
        "--tunnel-id",
        tunnel_id,
        "--health-listen-addr",
        validate_health_listen_addr(health_listen_addr),
    ]
    if force:
        argv.append("--force")
    if http:
        validate_http_bind(host)
        argv.extend(["--mcp-server-url", f"http://{host}:{int(port)}/mcp"])
    else:
        argv.extend(["--mcp-command", quoted_mcp_command()])
    return argv


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Connect LivingRuntime Remote to ChatGPT through OpenAI Secure MCP Tunnel."
    )
    parser.add_argument("--tunnel-id", help="OpenAI tunnel_... identifier")
    parser.add_argument("--client", help="Path to tunnel-client binary")
    parser.add_argument("--install-client", action="store_true", help="Download the latest public tunnel-client binary")
    parser.add_argument("--http", action="store_true", help="Tunnel a loopback streamable HTTP MCP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--doctor", action="store_true", help="Run tunnel-client doctor for this profile")
    parser.add_argument("--print-steps", action="store_true", help="Print ChatGPT connection steps and exit")
    parser.add_argument("--init-only", action="store_true", help="Write the tunnel profile without starting the daemon")
    parser.add_argument("--force", action="store_true", help="Replace an existing tunnel-client profile")
    parser.add_argument(
        "--health-listen-addr",
        default="127.0.0.1:8080",
        help="Loopback health address for tunnel-client. Do not bind 0.0.0.0.",
    )
    args = parser.parse_args(argv)

    tunnel_id = resolve_tunnel_id(args.tunnel_id)
    if args.print_steps:
        print(chatgpt_steps(tunnel_id))
        return

    if args.http:
        validate_http_bind(args.host)
    health_listen_addr = validate_health_listen_addr(args.health_listen_addr)

    client = find_tunnel_client(args.client)
    if client is None and args.install_client:
        client = install_tunnel_client()
    if client is None:
        raise SystemExit(
            "tunnel-client was not found. Install it from Platform tunnel settings or "
            "https://github.com/openai/tunnel-client/releases/latest, then rerun with --client or --install-client."
        )

    env = tunnel_env()
    env["CONTROL_PLANE_TUNNEL_ID"] = tunnel_id

    init_argv = build_init_argv(
        client,
        tunnel_id,
        http=args.http,
        host=args.host,
        port=args.port,
        health_listen_addr=health_listen_addr,
        force=args.force,
    )
    subprocess.check_call(init_argv, env=env)
    if args.doctor:
        subprocess.check_call([str(client), "doctor", "--profile", PROFILE, "--explain"], env=env)
        return
    if args.init_only:
        print(chatgpt_steps(tunnel_id))
        return
    print(chatgpt_steps(tunnel_id))
    raise SystemExit(subprocess.call([str(client), "run", "--profile", PROFILE], env=env))


if __name__ == "__main__":
    main()
