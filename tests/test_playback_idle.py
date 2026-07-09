"""Playback-aware idle: a display wake lock (video playback, presenting)
vetoes the idle cutoff, so watch time isn't clipped to ~2-minute segments."""
import subprocess
from appusage import daemon

SUMMARY = """Assertion status system-wide:
   BackgroundTask                 0
   UserIsActive                   1
   PreventUserIdleDisplaySleep    {n}
   PreventUserIdleSystemSleep     1
Listed by owning process:
   pid 641(Google Chrome Helper): PreventUserIdleDisplaySleep named: "Video Wake Lock"
"""

def _patch_pmset(monkeypatch, stdout=None, exc=None):
    def fake_run(cmd, **kw):
        assert cmd[0] == "pmset"
        if exc:
            raise exc
        class R:
            pass
        r = R()
        r.stdout = stdout
        return r
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

def test_wake_lock_parsing(monkeypatch):
    _patch_pmset(monkeypatch, SUMMARY.format(n=1))
    assert daemon.display_wake_lock() is True
    _patch_pmset(monkeypatch, SUMMARY.format(n=0))
    assert daemon.display_wake_lock() is False
    _patch_pmset(monkeypatch, "garbage with no summary section")
    assert daemon.display_wake_lock() is False
    _patch_pmset(monkeypatch, exc=subprocess.TimeoutExpired("pmset", 5))
    assert daemon.display_wake_lock() is False   # fail toward old behavior

def test_active_decision_is_lazy(monkeypatch):
    calls = []
    def wake(value):
        def probe():
            calls.append(value)
            return value
        return probe
    # input-active: pmset never consulted (hot path stays two subprocess calls)
    monkeypatch.setattr(daemon, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(daemon, "display_wake_lock", wake(True))
    assert daemon.is_active() is True
    assert calls == []
    # idle + wake lock held: still active (the fix)
    monkeypatch.setattr(daemon, "idle_seconds", lambda: 999.0)
    assert daemon.is_active() is True
    # idle + no lock: idle, exactly the old behavior
    monkeypatch.setattr(daemon, "display_wake_lock", wake(False))
    assert daemon.is_active() is False
