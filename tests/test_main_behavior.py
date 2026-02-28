from claudex.main import (
    AutoSwitchPolicy,
    _coerce_auto_switch,
    _extract_real_provider_bin_from_wrapper,
    _find_real_binary,
    _is_claudex_wrapper,
    _real_binary_for_provider,
    _write_wrapper,
    _with_preferred_provider,
    _wrapper_script,
)
from claudex.models import Provider


def test_with_preferred_provider_moves_provider_first():
    cfg = {"provider_order": ["claude", "codex"], "retry": {}}
    merged = _with_preferred_provider(cfg, Provider.CODEX)
    assert merged["provider_order"] == ["codex", "claude"]
    # Original dict should remain unchanged.
    assert cfg["provider_order"] == ["claude", "codex"]


def test_coerce_auto_switch_aliases():
    assert _coerce_auto_switch("always") == AutoSwitchPolicy.YES
    assert _coerce_auto_switch("never") == AutoSwitchPolicy.NO
    assert _coerce_auto_switch("ask") == AutoSwitchPolicy.ASK
    assert _coerce_auto_switch("unknown") == AutoSwitchPolicy.ASK


def test_wrapper_script_prefers_requested_provider():
    codex_script = _wrapper_script(Provider.CODEX, real_provider_bin="/usr/local/bin/codex")
    claude_script = _wrapper_script(Provider.CLAUDE, real_provider_bin="/usr/local/bin/claude")

    assert "--prefer-provider codex" in codex_script
    assert "--prefer-provider claude" in claude_script
    assert "claudex launch" in codex_script
    assert "CLAUDEX_INNER_PROVIDER_CALL" in codex_script
    assert "CLAUDEX_INNER_PROVIDER_CALL" in claude_script
    assert 'if [ "$#" -gt 0 ] && [ "${1#-}" != "$1" ]; then' in codex_script
    assert '-- "$@"' in codex_script


def test_write_wrapper_marks_file_as_claudex_wrapper(isolated_dir):
    path = isolated_dir / "bin" / "codex"
    _write_wrapper(path, _wrapper_script(Provider.CODEX, real_provider_bin="/usr/local/bin/codex"))
    assert _is_claudex_wrapper(path) is True


def test_real_binary_for_claude_falls_back_to_claudecode(monkeypatch, isolated_dir):
    calls: list[str] = []

    def fake_find(name, _dir):
        calls.append(name)
        if name == "claude":
            return None
        if name == "claudecode":
            return "/usr/local/bin/claudecode"
        return None

    monkeypatch.setattr("claudex.main._find_real_binary", fake_find)
    resolved = _real_binary_for_provider(Provider.CLAUDE, isolated_dir / "bin")
    assert resolved == "/usr/local/bin/claudecode"
    assert calls == ["claude", "claudecode"]


def test_extract_real_provider_bin_from_wrapper(isolated_dir):
    real = isolated_dir / "real" / "claude"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("#!/usr/bin/env sh\nexit 0\n")
    real.chmod(0o755)

    wrapper = isolated_dir / "bin" / "claude"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/usr/bin/env sh\n# CLAUDEX_WRAPPER\nREAL_PROVIDER_BIN='"
        + str(real)
        + "'\nexec \"$REAL_PROVIDER_BIN\" \"$@\"\n"
    )
    resolved = _extract_real_provider_bin_from_wrapper(wrapper)
    assert resolved == str(real)


def test_find_real_binary_uses_embedded_wrapper_target(isolated_dir, monkeypatch):
    real_dir = isolated_dir / "real"
    wrapper_dir = isolated_dir / "bin"
    real_dir.mkdir(parents=True, exist_ok=True)
    wrapper_dir.mkdir(parents=True, exist_ok=True)

    real = real_dir / "claude"
    real.write_text("#!/usr/bin/env sh\nexit 0\n")
    real.chmod(0o755)

    wrapper = wrapper_dir / "claude"
    wrapper.write_text(
        "#!/usr/bin/env sh\n# CLAUDEX_WRAPPER\nREAL_PROVIDER_BIN='"
        + str(real)
        + "'\nexec \"$REAL_PROVIDER_BIN\" \"$@\"\n"
    )
    wrapper.chmod(0o755)

    monkeypatch.setenv("PATH", str(wrapper_dir))
    resolved = _find_real_binary("claude", wrapper_dir)
    assert resolved == str(real)


def test_extract_real_provider_bin_rejects_self_reference(isolated_dir):
    wrapper = isolated_dir / "bin" / "claude"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/usr/bin/env sh\n# CLAUDEX_WRAPPER\nREAL_PROVIDER_BIN='"
        + str(wrapper)
        + "'\nexec \"$REAL_PROVIDER_BIN\" \"$@\"\n"
    )
    wrapper.chmod(0o755)
    assert _extract_real_provider_bin_from_wrapper(wrapper) is None
