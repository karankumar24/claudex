from typer.testing import CliRunner

import claudex.main as main_module
from claudex.models import ClaudexState, Provider


RUNNER = CliRunner()


def test_launch_picks_available_provider_and_execs(monkeypatch):
    state = ClaudexState()
    state.claude.cooldown_until = None
    state.codex.cooldown_until = None

    monkeypatch.setattr(main_module, "load_config", lambda: {"provider_order": ["claude", "codex"]})
    monkeypatch.setattr(main_module, "load_state", lambda: state)
    monkeypatch.setattr(main_module, "_real_binary_for_provider", lambda p, _dir: f"/bin/{p.value}")

    captured = {}

    def fake_execvpe(binary, argv, env):
        captured["binary"] = binary
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(main_module.os, "execvpe", fake_execvpe)

    result = RUNNER.invoke(main_module.app, ["launch", "--prefer-provider", "claude"])
    assert result.exit_code == 0
    assert captured["binary"] == "/bin/claude"
    assert captured["argv"] == ["/bin/claude"]
    assert captured["env"]["CLAUDEX_INNER_PROVIDER_CALL"] == "1"


def test_launch_falls_back_and_sets_codex_recursion_guard(monkeypatch):
    state = ClaudexState()
    # Force fallback to codex by putting claude in cooldown.
    from datetime import datetime, timedelta, timezone
    state.claude.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=10)

    monkeypatch.setattr(main_module, "load_config", lambda: {"provider_order": ["claude", "codex"]})
    monkeypatch.setattr(main_module, "load_state", lambda: state)
    monkeypatch.setattr(
        main_module,
        "_real_binary_for_provider",
        lambda p, _dir: "/bin/codex" if p == Provider.CODEX else "/bin/claude",
    )

    captured = {}

    def fake_execvpe(binary, argv, env):
        captured["binary"] = binary
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(main_module.os, "execvpe", fake_execvpe)

    result = RUNNER.invoke(
        main_module.app,
        ["launch", "--prefer-provider", "claude", "--", "--help"],
    )
    assert result.exit_code == 0
    assert captured["binary"] == "/bin/codex"
    assert captured["argv"] == ["/bin/codex", "--help"]
    assert captured["env"]["CLAUDEX_INNER_PROVIDER_CALL"] == "1"
    assert "claudex: switched claude -> codex" in result.output


def test_launch_skips_provider_with_missing_binary(monkeypatch):
    state = ClaudexState()
    state.claude.cooldown_until = None
    state.codex.cooldown_until = None

    monkeypatch.setattr(main_module, "load_config", lambda: {"provider_order": ["claude", "codex"]})
    monkeypatch.setattr(main_module, "load_state", lambda: state)
    monkeypatch.setattr(
        main_module,
        "_real_binary_for_provider",
        lambda p, _dir: None if p == Provider.CLAUDE else "/bin/codex",
    )

    captured = {}

    def fake_execvpe(binary, argv, env):
        captured["binary"] = binary
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(main_module.os, "execvpe", fake_execvpe)

    result = RUNNER.invoke(main_module.app, ["launch", "--prefer-provider", "claude"])
    assert result.exit_code == 0
    assert captured["binary"] == "/bin/codex"
    assert captured["argv"] == ["/bin/codex"]
    assert "claudex: switched claude -> codex" in result.output


def test_install_wrappers_creates_claude_claudecode_and_codex(monkeypatch, isolated_dir):
    wrapper_dir = isolated_dir / "bin"
    monkeypatch.setattr(
        main_module,
        "_find_real_binary",
        lambda name, _dir: f"/real/{name}",
    )

    result = RUNNER.invoke(
        main_module.app,
        ["install-wrappers", "--dir", str(wrapper_dir), "--overwrite"],
    )
    assert result.exit_code == 0

    claude = (wrapper_dir / "claude").read_text()
    claudecode = (wrapper_dir / "claudecode").read_text()
    codex = (wrapper_dir / "codex").read_text()

    assert "claudex launch --prefer-provider claude" in claude
    assert "claudex launch --prefer-provider claude" in claudecode
    assert "claudex launch --prefer-provider codex" in codex
    assert "REAL_PROVIDER_BIN=/real/claude" in claude
    assert "REAL_PROVIDER_BIN=/real/codex" in codex


def test_install_wrappers_accepts_claudecode_binary_when_claude_missing(
    monkeypatch, isolated_dir
):
    wrapper_dir = isolated_dir / "bin"

    def fake_find(name, _dir):
        if name == "codex":
            return "/real/codex"
        if name == "claude":
            return None
        if name == "claudecode":
            return "/real/claudecode"
        return None

    monkeypatch.setattr(main_module, "_find_real_binary", fake_find)

    result = RUNNER.invoke(
        main_module.app,
        ["install-wrappers", "--dir", str(wrapper_dir), "--overwrite"],
    )
    assert result.exit_code == 0
    claude = (wrapper_dir / "claude").read_text()
    assert "REAL_PROVIDER_BIN=/real/claudecode" in claude


def test_install_wrappers_refuses_in_place_claude_overwrite(monkeypatch, isolated_dir):
    wrapper_dir = isolated_dir / "bin"

    def fake_find(name, _dir):
        if name == "codex":
            return "/real/codex"
        if name == "claude":
            return str(wrapper_dir / "claude")
        if name == "claudecode":
            return None
        return None

    monkeypatch.setattr(main_module, "_find_real_binary", fake_find)
    result = RUNNER.invoke(
        main_module.app,
        ["install-wrappers", "--dir", str(wrapper_dir), "--overwrite"],
    )
    assert result.exit_code == 1
    assert "Refusing to overwrite the real claude/claudecode binary in-place" in result.output


def test_install_wrappers_refuses_in_place_codex_overwrite(monkeypatch, isolated_dir):
    wrapper_dir = isolated_dir / "bin"

    def fake_find(name, _dir):
        if name == "codex":
            return str(wrapper_dir / "codex")
        if name == "claude":
            return "/real/claude"
        if name == "claudecode":
            return None
        return None

    monkeypatch.setattr(main_module, "_find_real_binary", fake_find)
    result = RUNNER.invoke(
        main_module.app,
        ["install-wrappers", "--dir", str(wrapper_dir), "--overwrite"],
    )
    assert result.exit_code == 1
    assert "Refusing to overwrite the real codex binary in-place" in result.output
