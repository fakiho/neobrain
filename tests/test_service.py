"""Service supervision: definitions, supervised detection, honest restart, logs.

Hermetic — the service manager is never invoked; ``subprocess`` and the
manager lookup are patched.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from neobrain import service


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("NEOBRAIN_SUPERVISED", raising=False)


def _manager(monkeypatch, name):
    monkeypatch.setattr(service, "manager", lambda: name)


def test_plist_keeps_daemon_alive_and_marks_it_supervised():
    data = plistlib.loads(service.render_plist("/venv/bin/python", Path("/repo"), Path("/log/d.log"), "/usr/bin"))
    assert data["Label"] == service.LABEL
    assert data["ProgramArguments"] == ["/venv/bin/python", "-m", "neobrain.cli", "start"]
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["WorkingDirectory"] == "/repo"
    assert data["StandardOutPath"] == data["StandardErrorPath"] == "/log/d.log"
    # this marker is how the daemon knows launchd (not a shell) started it
    assert data["EnvironmentVariables"]["NEOBRAIN_SUPERVISED"] == "launchd"


def test_unit_restarts_on_failure():
    unit = service.render_unit("/venv/bin/python", Path("/repo"))
    assert "ExecStart=/venv/bin/python -m neobrain.cli start" in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit


def test_supervised_detection(monkeypatch):
    _manager(monkeypatch, "launchd")
    assert service.is_supervised() is False
    sup = service.supervisor()
    assert sup["supervised"] is False
    assert "neobrain service install" in sup["manager"]  # tells the user the fix

    monkeypatch.setenv("NEOBRAIN_SUPERVISED", "launchd")
    assert service.supervisor() == {"supervised": True, "manager": "launchd (LaunchAgent)", "service": service.LABEL}

    monkeypatch.delenv("NEOBRAIN_SUPERVISED")
    monkeypatch.setenv("INVOCATION_ID", "abc")
    _manager(monkeypatch, "systemd")
    assert service.supervisor()["manager"] == "systemd --user"


def test_restart_refuses_an_orphan_instead_of_faking_success(monkeypatch):
    _manager(monkeypatch, "launchd")
    spawned = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    res = service.restart()
    assert res["ok"] is False
    assert "neobrain service install" in res["error"]
    assert spawned == []  # nothing detached, nothing pretended


def test_restart_without_a_manager(monkeypatch):
    _manager(monkeypatch, None)
    res = service.restart()
    assert res["ok"] is False and "service manager" in res["error"]


@pytest.mark.parametrize(
    "mgr,needle",
    [("launchd", "launchctl kickstart -k"), ("systemd", "systemctl --user restart neobrain.service")],
)
def test_supervised_restart_is_detached_and_delayed(monkeypatch, mgr, needle):
    _manager(monkeypatch, mgr)
    monkeypatch.setenv("NEOBRAIN_SUPERVISED", "x")
    calls = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    res = service.restart()
    assert res["ok"] is True and res["method"] == mgr
    cmd, kw = calls[0]
    assert needle in cmd[-1] and cmd[-1].startswith("sleep 2;")  # the HTTP reply gets out first
    assert kw["start_new_session"] is True  # survives the death of the process serving the request


def test_install_refuses_when_something_already_listens(monkeypatch, tmp_path):
    _manager(monkeypatch, "launchd")
    monkeypatch.setattr(service, "status", lambda: {"running": False})
    monkeypatch.setattr(service, "_port_busy", lambda: True)
    monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "x.plist")
    res = service.install()
    assert res["ok"] is False and "already listens" in res["error"]
    assert not (tmp_path / "x.plist").exists()  # nothing written


def test_install_launchd_writes_plist_and_bootstraps(monkeypatch, tmp_path):
    _manager(monkeypatch, "launchd")
    monkeypatch.setattr(service, "status", lambda: {"running": False})
    monkeypatch.setattr(service, "_port_busy", lambda: False)
    plist = tmp_path / "LaunchAgents" / "x.plist"
    monkeypatch.setattr(service, "_plist_path", lambda: plist)
    ran = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(service, "_run", lambda cmd, timeout=15: (ran.append(cmd), Done())[1])
    res = service.install()
    assert res["ok"] is True and plist.exists()
    assert ran[-1][:3] == ["launchctl", "bootstrap", service._domain()]
    assert plistlib.loads(plist.read_bytes())["KeepAlive"] is True


def test_install_surfaces_launchctl_failure(monkeypatch, tmp_path):
    _manager(monkeypatch, "launchd")
    monkeypatch.setattr(service, "status", lambda: {"running": False})
    monkeypatch.setattr(service, "_port_busy", lambda: False)
    monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "x.plist")

    class Bad:
        returncode, stdout, stderr = 5, "", "Bootstrap failed: 5: Input/output error"

    monkeypatch.setattr(service, "_run", lambda cmd, timeout=15: Bad())
    res = service.install()
    assert res["ok"] is False and "Bootstrap failed" in res["error"]


def test_launchd_logs_read_the_log_file(monkeypatch, tmp_path):
    _manager(monkeypatch, "launchd")
    log = tmp_path / "daemon.log"
    monkeypatch.setattr(service, "log_path", lambda: log)
    assert "no log yet" in service.tail_logs(5)["error"]
    log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    out = service.tail_logs(3)
    assert out["lines"] == ["line 7", "line 8", "line 9"] and out["error"] is None
