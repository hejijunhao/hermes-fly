"""
Hermes Fly — FastAPI web server serving the Hermes CLI via xterm.js over WebSocket.

Routes:
  GET  /              → redirect to /login, /onboarding, or /terminal
  GET  /login         → password login page
  POST /login         → authenticate, set session cookie
  GET  /onboarding    → first-run API key setup page
  POST /onboarding    → validate + persist API key, mark onboarded
  GET  /terminal      → xterm.js web terminal (hermes agent)
  WS   /ws            → PTY bridge (hermes chat)
  GET  /cli           → xterm.js web terminal (direct shell)
  WS   /ws/cli        → PTY bridge (bash)
"""

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import pty
import re
import secrets
import shutil
import struct
import sys
import termios
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("hermes.web")

app = FastAPI()

STATIC_DIR = Path(__file__).parent / "static"
HERMES_HOME = Path(os.getenv("HERMES_HOME", "/root/.hermes"))
ENV_FILE = HERMES_HOME / ".env"
ONBOARDED_MARKER = HERMES_HOME / ".onboarded"

AUTH_PASS = os.getenv("TTYD_PASS", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
COOKIE_NAME = "hermes_session"


# ── Auth helpers ─────────────────────────────────────────


def _make_token() -> str:
    return hmac.new(
        SESSION_SECRET.encode(), AUTH_PASS.encode(), hashlib.sha256
    ).hexdigest()


def _is_authed(request: Request) -> bool:
    if not AUTH_PASS:
        return True  # no password configured — skip login
    return request.cookies.get(COOKIE_NAME) == _make_token()


# ── Onboarding helpers ───────────────────────────────────


def _is_onboarded() -> bool:
    """Check if the user has completed first-run onboarding."""
    if ONBOARDED_MARKER.exists():
        return True
    # Treat a pre-existing provider key (e.g. set via fly secrets) as onboarded
    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HERMES_API_KEY"):
        if os.getenv(key):
            return True
    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
        for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HERMES_API_KEY"):
            # Match KEY=value where value is non-empty and not a placeholder
            if re.search(rf"^{key}=(?!sk-or-\.\.\.)(?!\s*$).+", content, re.MULTILINE):
                return True
    return False


def _write_env_key(key: str, value: str) -> None:
    """Write or update a single key in ~/.hermes/.env."""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
        # Remove any existing entry for this key
        content = re.sub(rf"^#?\s*{re.escape(key)}=.*\n?", "", content, flags=re.MULTILINE)
    else:
        content = ""

    content = content.rstrip("\n") + f"\n{key}={value}\n"
    ENV_FILE.write_text(content)
    ENV_FILE.chmod(0o600)

    # Inject into current process so the next PTY spawn inherits it
    os.environ[key] = value


def _read_env_file() -> dict[str, str]:
    """Parse ~/.hermes/.env into a dict (for PTY environment injection)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# ── Machine info ────────────────────────────────────────


def _get_machine_info() -> str:
    """Build an ANSI-styled machine info banner for the CLI terminal."""
    u = os.uname()

    # OS pretty name
    os_name = f"{u.sysname} {u.release}"
    os_release = Path("/etc/os-release")
    if os_release.exists():
        for line in os_release.read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
                break

    # Memory from /proc/meminfo
    mem_total = mem_avail = "?"
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        info = {}
        for line in meminfo.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        if "MemTotal" in info:
            mem_total = f"{info['MemTotal'] // 1024} MB"
        if "MemAvailable" in info:
            mem_avail = f"{info['MemAvailable'] // 1024} MB"

    # Disk usage
    def _disk(path: str) -> str:
        try:
            usage = shutil.disk_usage(path)
            used_gb = usage.used / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            return f"{used_gb:.1f} GB / {total_gb:.1f} GB used"
        except Exception:
            return "?"

    # Uptime from /proc/uptime
    uptime_str = "?"
    uptime_path = Path("/proc/uptime")
    if uptime_path.exists():
        try:
            secs = int(float(uptime_path.read_text().split()[0]))
            days, rem = divmod(secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins = rem // 60
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            parts.append(f"{mins}m")
            uptime_str = " ".join(parts)
        except Exception:
            pass

    # Fly metadata from env vars
    fly_region = os.getenv("FLY_REGION", "?")
    fly_app = os.getenv("FLY_APP_NAME", "?")
    fly_machine = os.getenv("FLY_MACHINE_ID", u.nodename)

    # Build rows: (label, value)
    rows = [
        ("OS", os_name),
        ("Kernel", f"{u.release} {u.machine}"),
        ("CPU", f"{os.cpu_count() or '?'} core(s)"),
        ("Memory", f"{mem_total} total / {mem_avail} available"),
        ("Disk /", _disk("/")),
        ("Volume", f"{_disk(str(HERMES_HOME))} ({HERMES_HOME})"),
        ("Region", fly_region),
        ("App", fly_app),
        ("Machine", fly_machine),
        ("Python", sys.version.split()[0]),
        ("Uptime", uptime_str),
    ]

    # ANSI escape codes matching the cyberpunk theme
    R = "\x1b[31m"      # red (borders)
    C = "\x1b[36m"      # cyan (labels)
    W = "\x1b[0m"       # reset (values)
    B = "\x1b[1m"       # bold
    D = "\x1b[2m"       # dim

    width = 58
    inner = width - 4  # inside the "│  " and "  │"

    lines = []
    lines.append(f"{R}┌{'─' * (width - 2)}┐{W}")
    lines.append(f"{R}│{W}  {B}{R}HERMES // FLY MACHINE{W}{' ' * (inner - 21)}{R}│{W}")
    lines.append(f"{R}├{'─' * (width - 2)}┤{W}")
    for label, value in rows:
        padded_label = f"{C}{label:<10}{W}"
        # Calculate visible length (label is 10 chars, value is variable)
        visible_content = f"{label:<10}{value}"
        padding = inner - len(visible_content)
        if padding < 0:
            padding = 0
        lines.append(f"{R}│{W}  {padded_label}{value}{' ' * padding}{R}│{W}")
    lines.append(f"{R}└{'─' * (width - 2)}┘{W}")
    lines.append("")
    lines.append(f"{D}Type 'exit' to end session. Navigate to /terminal for the Hermes agent.{W}")
    lines.append("")

    return "\r\n".join(lines) + "\r\n"


# ── Routes ───────────────────────────────────────────────


@app.get("/")
async def index(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    if not _is_onboarded():
        return RedirectResponse("/onboarding")
    return RedirectResponse("/terminal")


@app.get("/login")
async def login_page(request: Request):
    if not AUTH_PASS:
        return RedirectResponse("/")  # no password set — skip login
    if _is_authed(request):
        return RedirectResponse("/")
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
async def login(password: str = Form(...)):
    if password == AUTH_PASS:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME, _make_token(),
            httponly=True, secure=True, samesite="strict", max_age=86400,
        )
        return resp
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/onboarding")
async def onboarding_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    if _is_onboarded():
        return RedirectResponse("/terminal")
    return FileResponse(STATIC_DIR / "onboarding.html")


@app.post("/onboarding")
async def submit_onboarding(request: Request):
    if not _is_authed(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    api_key = body.get("api_key", "").strip()

    if not api_key:
        return JSONResponse({"error": "API key is required"}, status_code=400)

    # Validate the key against OpenRouter
    valid, detail = await _validate_openrouter_key(api_key)
    if not valid:
        return JSONResponse({"error": detail}, status_code=400)

    # Persist to ~/.hermes/.env
    _write_env_key("OPENROUTER_API_KEY", api_key)

    # Mark onboarding complete
    ONBOARDED_MARKER.touch()

    return JSONResponse({"ok": True})


@app.get("/terminal")
async def terminal_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "terminal.html")


@app.get("/cli")
async def cli_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "cli.html")


# ── Key validation ───────────────────────────────────────


async def _validate_openrouter_key(key: str) -> tuple[bool, str]:
    """Verify an OpenRouter API key via their /auth/key endpoint."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 401:
                return False, "Invalid API key — authentication failed"
            return False, f"OpenRouter returned status {resp.status_code}"
        except httpx.TimeoutException:
            # Network issues shouldn't block onboarding
            return True, ""
        except Exception:
            # Fail open on unexpected errors
            return True, ""


# ── PTY helpers ──────────────────────────────────────────


async def _spawn_pty_async(cmd: list[str], env: dict[str, str]):
    """Spawn a command in a new PTY. Returns (process, master_fd)."""
    master_fd, slave_fd = pty.openpty()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid,
        env=env,
    )
    os.close(slave_fd)

    # Non-blocking reads on master
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    return proc, master_fd


def _cleanup_pty(loop, master_fd, proc):
    """Safely clean up PTY resources."""
    try:
        loop.remove_reader(master_fd)
    except Exception:
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


# ── WebSocket PTY bridge ─────────────────────────────────


async def _ws_pty_bridge(
    ws: WebSocket,
    cmd: list[str],
    env: dict[str, str],
    banner: str | None = None,
    exit_message: str = "process exited",
) -> None:
    """Generic WebSocket ↔ PTY bridge. Used by both /ws and /ws/cli."""
    proc, master_fd = await _spawn_pty_async(cmd, env)
    logger.info("PTY spawned: pid=%s cmd=%s", proc.pid, cmd)

    # Send banner before PTY output starts
    if banner:
        await ws.send_text(banner)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_pty_data():
        try:
            data = os.read(master_fd, 65536)
            if data:
                queue.put_nowait(data)
            else:
                queue.put_nowait(None)
        except OSError:
            queue.put_nowait(None)

    loop.add_reader(master_fd, _on_pty_data)

    async def _watch_proc():
        retcode = await proc.wait()
        logger.warning("PTY process exited: pid=%s retcode=%s", proc.pid, retcode)
        queue.put_nowait(None)

    async def pty_to_ws():
        while True:
            data = await queue.get()
            if data is None:
                try:
                    await ws.send_text(
                        f"\r\n\x1b[31m[{exit_message} — refresh to reconnect]\x1b[0m\r\n"
                    )
                    await ws.close(code=1000, reason="PTY exited")
                except Exception:
                    pass
                break
            try:
                await ws.send_text(data.decode("utf-8", errors="replace"))
            except Exception:
                break

    async def ws_to_pty():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                text = msg.get("text", "")
                if not text:
                    continue
                # Handle resize messages from xterm.js
                if text.startswith("{"):
                    try:
                        payload = json.loads(text)
                        if payload.get("type") == "resize":
                            winsize = struct.pack(
                                "HHHH", payload["rows"], payload["cols"], 0, 0
                            )
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                            continue
                    except (json.JSONDecodeError, KeyError):
                        pass
                try:
                    os.write(master_fd, text.encode("utf-8"))
                except OSError:
                    break
        except Exception:
            pass

    watcher = asyncio.create_task(_watch_proc())
    task_pty = asyncio.create_task(pty_to_ws())
    task_ws = asyncio.create_task(ws_to_pty())

    try:
        done, pending = await asyncio.wait(
            [task_pty, task_ws], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    finally:
        watcher.cancel()
        _cleanup_pty(loop, master_fd, proc)
        logger.info("WebSocket session cleaned up: pid=%s", proc.pid)


@app.websocket("/ws")
async def ws_terminal(ws: WebSocket, provider: str = "openrouter"):
    if AUTH_PASS and ws.cookies.get(COOKIE_NAME) != _make_token():
        await ws.close(code=4001)
        return

    await ws.accept()

    env = {**os.environ, "TERM": "xterm-256color"}
    env.update(_read_env_file())

    cmd = ["hermes", "chat"]
    if provider in ("nous", "openrouter"):
        cmd.extend(["--provider", provider])

    await _ws_pty_bridge(ws, cmd, env, exit_message="hermes process exited")


@app.websocket("/ws/cli")
async def ws_cli(ws: WebSocket):
    if AUTH_PASS and ws.cookies.get(COOKIE_NAME) != _make_token():
        await ws.close(code=4001)
        return

    await ws.accept()

    env = {**os.environ, "TERM": "xterm-256color"}
    env.update(_read_env_file())

    banner = _get_machine_info()
    cmd = ["/bin/bash", "--login"]

    await _ws_pty_bridge(ws, cmd, env, banner=banner, exit_message="shell exited")


# Static assets (must be last to avoid catching other routes)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
