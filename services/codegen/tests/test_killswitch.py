"""Unit tests for the code-automation kill switch."""

from app.safety.killswitch import automation_enabled


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CODEGEN_KILL_SWITCH", raising=False)
    monkeypatch.delenv("CODEGEN_DISABLED_PROJECTS", raising=False)
    assert automation_enabled("demo") is True


def test_global_kill_switch_halts_everything(monkeypatch):
    monkeypatch.setenv("CODEGEN_KILL_SWITCH", "true")
    assert automation_enabled("demo") is False


def test_per_project_denylist(monkeypatch):
    monkeypatch.delenv("CODEGEN_KILL_SWITCH", raising=False)
    monkeypatch.setenv("CODEGEN_DISABLED_PROJECTS", "demo, other")
    assert automation_enabled("demo") is False
    assert automation_enabled("safe") is True
