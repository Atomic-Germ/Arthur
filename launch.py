#!/usr/bin/env python3
"""
Arthur project launcher - starts backend + frontend as background processes.

Phases:
1. Verify/Install backend requirements (from backend/requirements.txt)
2. Verify/Install frontend tools AND project dependencies (node_modules)
3. Start the backend (uvicorn) as a detached process
4. Start the frontend (npm run dev) as a detached process
5. Run a health-check loop until Ctrl+C
"""

import atexit
import os
import shutil
import signal
import subprocess
import sys
import time
import socket
from pathlib import Path

# Force unbuffered stdout so prints appear immediately even when
# stdout is a pipe (not a TTY). errors="replace" prevents
# UnicodeEncodeError when log output contains characters (like
# Vite's arrow) that the terminal encoding cannot represent.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    sys.stderr.reconfigure(line_buffering=True, errors="replace")

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
LOG_DIR = ROOT / "logs"

_backend_proc = None
_frontend_proc = None


def cleanup_children():
    """Terminate child processes. Safe to call multiple times, from anywhere.

    This is registered as an atexit callback — it must NOT call sys.exit().
    Calling sys.exit() inside atexit raises SystemExit during interpreter
    shutdown, which Python reports as "Exception ignored in atexit callback".
    """
    global _backend_proc, _frontend_proc
    for proc in (_backend_proc, _frontend_proc):
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    _backend_proc = None
    _frontend_proc = None


def on_signal(signum, frame):
    """Signal handler: clean up children, then exit.

    sys.exit() is correct HERE (signal handler), but never in atexit.
    """
    cleanup_children()
    sys.exit(0)


atexit.register(cleanup_children)
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)


def resolve_npm():
    """Return the correct npm command for this platform.

    On Windows, npm is npm.cmd — subprocess cannot execute .cmd files
    directly without shell=True. shutil.which resolves the full path.
    """
    npm = shutil.which("npm")
    if npm:
        return npm
    if sys.platform == "win32":
        for candidate in (
            os.path.expandvars(r"%ProgramFiles%\nodejs\npm.cmd"),
            os.path.expandvars(r"%ProgramFiles(x86)%\nodejs\npm.cmd"),
        ):
            if os.path.isfile(candidate):
                return candidate
    return "npm"


def run_detached(cmd, cwd, env=None, name="child"):
    """Start a process with output redirected to log files.

    Log files (not DEVNULL, not PIPE) serve two purposes:
    - PIPE: fills up if nobody reads → child blocks → appears to "die"
    - DEVNULL: discards all output → impossible to diagnose failures
    - Log file: output persists for inspection, never blocks the child
    """
    LOG_DIR.mkdir(exist_ok=True)
    out_f = open(LOG_DIR / (name + ".out.log"), "w", encoding="utf-8", buffering=1)
    err_f = open(LOG_DIR / (name + ".err.log"), "w", encoding="utf-8", buffering=1)
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=out_f,
        stderr=err_f,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def read_log_tail(name, max_bytes=500):
    """Read the last max_bytes of a log file for diagnostics."""
    try:
        path = LOG_DIR / (name + ".err.log")
        if not path.exists() or path.stat().st_size == 0:
            path = LOG_DIR / (name + ".out.log")
        data = path.read_bytes()
        return data[-max_bytes:].decode(errors="replace")
    except Exception:
        return ""


def verify_backend_reqs():
    """Check if core backend packages are importable."""
    required = {"fastapi", "uvicorn", "pydantic", "httpx"}
    ok = True
    for mod in required:
        try:
            __import__(mod)
            print("  + " + mod, flush=True)
        except ImportError:
            print("  - " + mod + " missing", flush=True)
            ok = False
    return ok


def install_backend_reqs():
    """Install backend requirements from requirements.txt."""
    reqs = BACKEND_DIR / "requirements.txt"
    if not reqs.exists():
        print("  - " + str(reqs) + " not found, skipping", flush=True)
        return False
    print("  + Installing backend requirements...", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(reqs)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("  - pip install failed:", flush=True)
        print(result.stderr, flush=True)
        return False
    print("  + Backend requirements installed", flush=True)
    return True


def verify_frontend_tools():
    """Check if node/npm are available and node version is reasonable."""
    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        print("  - node not found - cannot start frontend", flush=True)
        return False
    version_str = result.stdout.strip()
    try:
        major = int(version_str.lstrip("v").split(".")[0])
        if major < 18:
            print("  ! node " + version_str + " is old - YMMV with Vite/plugins", flush=True)
    except (ValueError, IndexError):
        print("  ! cannot parse node version " + version_str, flush=True)
    print("  + node " + version_str, flush=True)
    result = subprocess.run(["npm", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        print("  + npm " + result.stdout.strip(), flush=True)
    return True


def verify_frontend_deps():
    """Check if node_modules exists AND contains the vite binary.

    This is the critical check that was missing: having node/npm installed
    does NOT mean project dependencies are installed. npm run dev fails
    with 'vite is not recognized' when node_modules is absent.
    """
    vite_bin = FRONTEND_DIR / "node_modules" / ".bin"
    if sys.platform == "win32":
        vite_bin = vite_bin / "vite.cmd"
    else:
        vite_bin = vite_bin / "vite"
    return vite_bin.exists()


def install_frontend_deps():
    """Run npm install to populate node_modules."""
    pkg = FRONTEND_DIR / "package.json"
    if not pkg.exists():
        print("  - " + str(pkg) + " not found, skipping", flush=True)
        return False
    print("  + Installing frontend dependencies (npm install)...", flush=True)
    npm = resolve_npm()
    result = subprocess.run(
        [npm, "install"],
        cwd=str(FRONTEND_DIR),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("  - npm install failed:", flush=True)
        print(result.stderr, flush=True)
        return False
    print("  + Frontend dependencies installed", flush=True)
    return True


def wait_for_port(port, host="127.0.0.1", timeout=30):
    """Poll until the given port accepts connections, or timeout.

    Tries both IPv4 and IPv6 — Vite and some servers bind to ::1 (IPv6)
    while other services bind to 127.0.0.1 (IPv4). Checking only one
    family causes false "did not start" results.
    """
    hosts = [host]
    if host in ("127.0.0.1", "localhost"):
        hosts.append("::1")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for h in hosts:
            try:
                family = socket.AF_INET6 if ":" in h else socket.AF_INET
                with socket.socket(family, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect((h, port))
                    return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                pass
        time.sleep(0.5)
    return False


def main():
    global _backend_proc, _frontend_proc

    print("=" * 60, flush=True)
    print("Arthur - Intelligent writing companion", flush=True)
    print("=" * 60, flush=True)

    # ---- Phase 1: Backend requirements ----
    print("\n--- Phase 1: Backend requirements ---", flush=True)
    if not verify_backend_reqs():
        print("  - Missing backend packages, installing...", flush=True)
        if not install_backend_reqs():
            print("  - Failed to install backend requirements. Exiting.", flush=True)
            sys.exit(1)
    else:
        print("  + All backend requirements are already installed", flush=True)

    # ---- Phase 2: Frontend tools + project dependencies ----
    print("\n--- Phase 2: Frontend tools and dependencies ---", flush=True)
    if not verify_frontend_tools():
        print("  - Cannot start frontend without node/npm", flush=True)
    elif not verify_frontend_deps():
        print("  - node_modules missing or incomplete (vite not found)", flush=True)
        if not install_frontend_deps():
            print("  - Failed to install frontend dependencies.", flush=True)
            print("  - Continuing without frontend.", flush=True)
        else:
            print("  + Frontend tools and dependencies are ready", flush=True)
    else:
        print("  + Frontend tools and dependencies are ready", flush=True)

    # ---- Phase 3: Start backend ----
    print("\n--- Phase 3: Starting backend ---", flush=True)
    env = None
    gw_vars = {k: v for k, v in os.environ.items() if k.startswith("GW_")}
    if gw_vars:
        env = os.environ.copy()
        env.update(gw_vars)

    _backend_proc = run_detached(
        [sys.executable, "run.py"],
        cwd=str(BACKEND_DIR),
        env=env,
        name="backend",
    )
    print("  + Backend PID: " + str(_backend_proc.pid), flush=True)
    print("  + Backend logs: logs/backend.out.log, logs/backend.err.log", flush=True)

    if not wait_for_port(8000, host="127.0.0.1", timeout=30):
        print("  - Backend did not start within 30s", flush=True)
        tail = read_log_tail("backend")
        if tail:
            print("  + Last output:", flush=True)
            for line in tail.strip().splitlines()[-5:]:
                print("    " + line, flush=True)
    else:
        print("  + Backend is up: http://127.0.0.1:8000", flush=True)

    # ---- Phase 4: Start frontend ----
    print("\n--- Phase 4: Starting frontend ---", flush=True)
    npm = resolve_npm()
    print("  + Using npm: " + npm, flush=True)

    _frontend_proc = run_detached(
        [npm, "run", "dev"],
        cwd=str(FRONTEND_DIR),
        name="frontend",
    )
    print("  + Frontend PID: " + str(_frontend_proc.pid), flush=True)
    print("  + Frontend logs: logs/frontend.out.log, logs/frontend.err.log", flush=True)

    print("  + Waiting for frontend dev server...", flush=True)
    if wait_for_port(5173, host="127.0.0.1", timeout=45):
        print("  + Frontend is up: http://127.0.0.1:5173", flush=True)
    else:
        print("  - Frontend did not start (port 5173 not reached)", flush=True)
        tail = read_log_tail("frontend")
        if tail:
            print("  + Last output:", flush=True)
            for line in tail.strip().splitlines()[-5:]:
                print("    " + line, flush=True)

    # ---- Phase 5: Health-check loop ----
    print("\n--- Arthur is running ---", flush=True)
    print("  Backend API:    http://127.0.0.1:8000", flush=True)
    print("  API Docs:       http://127.0.0.1:8000/docs", flush=True)
    print("  Frontend:       http://127.0.0.1:5173", flush=True)
    print("  Press Ctrl+C to shut down...", flush=True)
    print("=" * 60, flush=True)

    backend_reported = False
    frontend_reported = False

    try:
        while True:
            if _backend_proc and _backend_proc.poll() is not None and not backend_reported:
                print("  - Backend exited (code " + str(_backend_proc.returncode) + ")", flush=True)
                backend_reported = True
            if _frontend_proc and _frontend_proc.poll() is not None and not frontend_reported:
                print("  - Frontend exited (code " + str(_frontend_proc.returncode) + ")", flush=True)
                frontend_reported = True
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n  - Shutting down...", flush=True)


if __name__ == "__main__":
    main()
