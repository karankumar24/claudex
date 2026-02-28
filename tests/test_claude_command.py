"""
Tests for Claude command construction and subprocess environment wiring.
"""

from unittest.mock import MagicMock

from claudex.providers.base import ProviderResult
from claudex.providers.claude import ClaudeProvider


def _setup_provider(monkeypatch):
    provider = ClaudeProvider()
    captured: dict[str, object] = {}

    proc = MagicMock()
    proc.stdout = "[]"
    proc.stderr = ""
    proc.returncode = 0

    def fake_run(cmd, capture_output, text, timeout, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return proc

    monkeypatch.setattr("claudex.providers.claude.subprocess.run", fake_run)
    monkeypatch.setattr(
        provider,
        "_parse",
        lambda _proc, _raw: ProviderResult(success=True, text="ok", session_id="sess_1"),
    )
    return provider, captured


def test_command_uses_json_output_and_resume(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="hello",
        session_id="sess_abc",
        config={},
    )

    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "-r" in cmd and "sess_abc" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert captured["env"]["CLAUDEX_INNER_PROVIDER_CALL"] == "1"


def test_command_appends_allowed_tools(monkeypatch):
    provider, captured = _setup_provider(monkeypatch)
    provider.run(
        prompt="hello",
        session_id=None,
        config={"claude": {"allowed_tools": ["Bash", "Edit"]}},
    )
    cmd = captured["cmd"]
    # Order should preserve configured tools.
    assert cmd.count("--allowedTools") == 2
    assert cmd[-4:] == ["--allowedTools", "Bash", "--allowedTools", "Edit"]


def test_falls_back_to_claudecode_if_claude_not_found(monkeypatch):
    provider = ClaudeProvider()
    calls: list[list[str]] = []

    proc = MagicMock()
    proc.stdout = "[]"
    proc.stderr = ""
    proc.returncode = 0

    def fake_run(cmd, capture_output, text, timeout, env):
        calls.append(cmd)
        if cmd[0] == "claude":
            raise FileNotFoundError
        return proc

    monkeypatch.setattr("claudex.providers.claude.subprocess.run", fake_run)
    monkeypatch.setattr(
        provider,
        "_parse",
        lambda _proc, _raw: ProviderResult(success=True, text="ok", session_id="sess_1"),
    )

    result = provider.run(prompt="hello", session_id=None, config={})
    assert result.success is True
    assert calls[0][0] == "claude"
    assert calls[1][0] == "claudecode"
