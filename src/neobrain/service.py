"""Run the daemon under the OS service manager (launchd on macOS, systemd --user
on Linux), and restart it from the Observatory.

A daemon started by hand (``nohup neobrain start``) has no supervisor: nothing
can restart it, and nothing brings it back after a crash or reboot. This module
owns the three things that need a supervisor:

* ``install`` / ``uninstall`` / ``status`` — the ``neobrain service`` command;
* ``restart`` — the Settings tab's Restart button (``POST /api/settings/restart``);
* ``supervisor`` / ``tail_logs`` — what the Debug tab reports.

The process under a manager is recognised by an environment variable: systemd
sets ``INVOCATION_ID`` itself, and the launchd plist we write sets
``NEOBRAIN_SUPERVISED=launchd``. A daemon without either is an *orphan*, and
Restart refuses to pretend it can restart one.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import config

LABEL = "com.neobrain.daemon"  # launchd job label
UNIT = "neobrain.service"  # systemd user unit

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- discovery -----------------------------------------------------------


def manager() -> Optional[str]:
    """``"launchd"`` / ``"systemd"`` for this host, or None when neither exists."""
    if sys.platform == "darwin":
        return "launchd" if shutil.which("launchctl") else None
    return "systemd" if shutil.which("systemctl") else None


def service_name() -> str:
    return LABEL if manager() == "launchd" else UNIT


def is_supervised() -> bool:
    """True when *this* process was started by the service manager."""
    return bool(os.environ.get("INVOCATION_ID") or os.environ.get("NEOBRAIN_SUPERVISED"))


def supervisor() -> dict[str, Any]:
    """How this daemon is supervised, for the Debug tab."""
    mgr = manager()
    supervised = is_supervised()
    if supervised:
        label = "launchd (LaunchAgent)" if os.environ.get("NEOBRAIN_SUPERVISED") == "launchd" else "systemd --user"
    elif mgr:
        label = f"none (orphan — run `neobrain service install` to supervise it with {mgr})"
    else:
        label = "none (orphan — no restart on crash)"
    return {"supervised": supervised, "manager": label, "service": service_name()}


def log_path() -> Path:
    return config.DATA_DIR / "daemon.log"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _workdir() -> Path:
    # The repo holds the .env the daemon reads; a non-editable install has none.
    return REPO_ROOT if (REPO_ROOT / "pyproject.toml").exists() else Path.home()


# --- definitions ---------------------------------------------------------


def render_plist(python: str, workdir: Path, log: Path, path_env: str) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [python, "-m", "neobrain.cli", "start"],
            "WorkingDirectory": str(workdir),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "ProcessType": "Background",
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
            "EnvironmentVariables": {"NEOBRAIN_SUPERVISED": "launchd", "PATH": path_env},
        }
    )


def render_unit(python: str, workdir: Path) -> str:
    return (
        "[Unit]\n"
        "Description=neoBrain daemon\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={python} -m neobrain.cli start\n"
        f"WorkingDirectory={workdir}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# --- status --------------------------------------------------------------


def _port_busy() -> bool:
    from .cli import _parse_bind

    host, port = _parse_bind(config.settings.bind)
    host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def status() -> dict[str, Any]:
    mgr = manager()
    out: dict[str, Any] = {"manager": mgr, "service": service_name(), "installed": False, "running": False, "pid": None}
    if mgr == "launchd":
        out["definition"] = str(_plist_path())
        out["installed"] = _plist_path().exists()
        proc = _run(["launchctl", "print", f"{_domain()}/{LABEL}"])
        if proc.returncode == 0:
            out["loaded"] = True
            for line in proc.stdout.splitlines():
                key, _, val = line.strip().partition(" = ")
                if key == "pid" and val.isdigit():
                    out["pid"], out["running"] = int(val), True
    elif mgr == "systemd":
        out["definition"] = str(_unit_path())
        out["installed"] = _unit_path().exists()
        out["running"] = _run(["systemctl", "--user", "is-active", UNIT]).stdout.strip() == "active"
    return out


# --- install / uninstall -------------------------------------------------


def install() -> dict[str, Any]:
    mgr = manager()
    if mgr is None:
        return {"ok": False, "error": "no supported service manager (needs launchd or systemd --user)"}
    config.ensure_dirs()
    st = status()
    if not st["running"] and _port_busy():
        return {
            "ok": False,
            "error": f"something already listens on {config.settings.bind} (a daemon started by hand?). "
            "Stop it first, then re-run `neobrain service install`.",
        }
    python, workdir = sys.executable, _workdir()
    if mgr == "launchd":
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_plist(python, workdir, log_path(), os.environ.get("PATH", "/usr/bin:/bin")))
        _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])  # reload cleanly; absent is fine
        proc = _run(["launchctl", "bootstrap", _domain(), str(path)])
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout).strip() or "launchctl bootstrap failed"}
        return {"ok": True, "manager": mgr, "definition": str(path), "log": str(log_path())}
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(python, workdir))
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT]):
        proc = _run(cmd)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout).strip() or f"{' '.join(cmd)} failed"}
    return {"ok": True, "manager": mgr, "definition": str(path), "log": "journalctl --user -u " + UNIT}


def uninstall() -> dict[str, Any]:
    mgr = manager()
    if mgr == "launchd":
        _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
        _plist_path().unlink(missing_ok=True)
    elif mgr == "systemd":
        _run(["systemctl", "--user", "disable", "--now", UNIT])
        _unit_path().unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    else:
        return {"ok": False, "error": "no supported service manager"}
    return {"ok": True, "manager": mgr}


# --- restart -------------------------------------------------------------


def restart() -> dict[str, Any]:
    """Restart the supervised daemon from inside it.

    The restart kills the process serving this request, so it is launched
    detached (own session) with a short delay, letting the HTTP answer go out.
    Refuses — truthfully — when nothing supervises this process.
    """
    mgr = manager()
    if mgr is None:
        return {"ok": False, "error": "no service manager on this host (needs launchd or systemd --user)"}
    if not is_supervised():
        return {
            "ok": False,
            "error": f"this daemon was started by hand, so nothing can restart it. "
            f"Stop it and run `neobrain service install` to put it under {mgr}.",
        }
    if mgr == "launchd":
        cmd = ["sh", "-c", f"sleep 2; launchctl kickstart -k {_domain()}/{LABEL}"]
    else:
        cmd = ["sh", "-c", f"sleep 2; systemctl --user restart {UNIT}"]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:  # noqa: BLE001 - report, never crash the API
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "method": mgr, "detail": "restarting in ~2s"}


def restart_now() -> dict[str, Any]:
    """Synchronous restart for the CLI (no HTTP response to protect)."""
    mgr = manager()
    if mgr is None:
        return {"ok": False, "error": "no service manager on this host (needs launchd or systemd --user)"}
    if not status()["installed"]:
        return {"ok": False, "error": "service is not installed — run `neobrain service install`"}
    cmd = (["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"] if mgr == "launchd"
           else ["systemctl", "--user", "restart", UNIT])
    proc = _run(cmd)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip() or f"{' '.join(cmd)} failed"}
    return {"ok": True, "method": mgr}


# --- logs ----------------------------------------------------------------


def tail_logs(lines: int) -> dict[str, Any]:
    """Last ``lines`` log lines: journalctl under systemd, the log file under launchd."""
    unit = service_name()
    mgr = manager()
    try:
        if mgr == "launchd":
            path = log_path()
            if not path.exists():
                return {"unit": unit, "lines": [], "count": 0, "error": f"no log yet at {path}"}
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 256 * 1024))
                rows = [ln for ln in fh.read().decode("utf-8", "replace").splitlines() if ln.strip()][-lines:]
            return {"unit": unit, "lines": rows, "count": len(rows), "error": None}
        proc = subprocess.run(
            ["journalctl", "--user", "-u", UNIT, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 - logs are a convenience, never fatal
        return {"unit": unit, "lines": [], "count": 0, "error": f"{type(exc).__name__}: {exc}"}
    rows = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    err = (proc.stderr or "").strip() if proc.returncode != 0 else None
    return {"unit": unit, "lines": rows, "count": len(rows), "error": err}
