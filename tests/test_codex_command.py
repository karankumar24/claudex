"""
Tests for Codex command construction (flags and argument order).
"""

from unittest.mock import MagicMock

from claudex.providers.base import ProviderResult
from claudex.providers.codex import CodexProvider


def _setup_provider(monkeypatch):
    provider = CodexProvider()
    captured: dict[str, list[str]] = {}

    proc = MagicMock()
    proc.stdout = ""
    proc.stderr = ""
    proc.returncode = 0

    def fake_run(cmd, capture_output, text, timeout, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return proc

    monkeypatch.setattr("claudex.providers.codex.subprocess.run", fake_run)
    monkeypatch.setattr(
        provider,
        "_parse_jsonl",
        lambda _proc: ProviderResult(success=True, text="ok", session_id="thread_1"),
    )
    return provider, captured


def test_command_uses_full_auto_flag(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="hello",
        session_id=None,
        config={"codex": {"sandbox": "full-auto", "model": "o4-mini"}},
    )

    cmd = captured["cmd"]
    assert cmd[:2] == ["codex", "exec"]
    assert "--model" in cmd and "o4-mini" in cmd
    assert "--full-auto" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--approval-mode" not in cmd
    assert cmd[-2:] == ["--json", "hello"]
    assert captured["env"]["CLAUDEX_INNER_PROVIDER_CALL"] == "1"


def test_command_uses_workspace_write_sandbox(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="hello",
        session_id=None,
        config={"codex": {"sandbox": "workspace-write"}},
    )

    cmd = captured["cmd"]
    idx = cmd.index("--sandbox")
    assert cmd[idx + 1] == "workspace-write"
    assert "--json" in cmd


def test_command_uses_danger_full_access_sandbox(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="hello",
        session_id=None,
        config={"codex": {"sandbox": "danger-full-access"}},
    )

    cmd = captured["cmd"]
    idx = cmd.index("--sandbox")
    assert cmd[idx + 1] == "danger-full-access"


def test_command_falls_back_to_read_only_for_invalid_sandbox(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="hello",
        session_id=None,
        config={"codex": {"sandbox": "invalid-value"}},
    )

    cmd = captured["cmd"]
    idx = cmd.index("--sandbox")
    assert cmd[idx + 1] == "read-only"


def test_resume_command_keeps_resume_arguments(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="continue",
        session_id="sess_123",
        config={"codex": {"sandbox": "read-only"}},
    )

    cmd = captured["cmd"]
    assert "resume" in cmd
    ridx = cmd.index("resume")
    assert cmd[ridx + 1] == "sess_123"
    assert cmd[-2:] == ["--json", "continue"]
