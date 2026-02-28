from claudex.main import (
    AutoSwitchPolicy,
    _coerce_auto_switch,
    _is_claudex_wrapper,
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
    codex_script = _wrapper_script(Provider.CODEX)
    claude_script = _wrapper_script(Provider.CLAUDE)

    assert "--prefer-provider codex" in codex_script
    assert "--prefer-provider claude" in claude_script
    assert "claudex chat" in codex_script
    assert "claudex ask" in codex_script


def test_write_wrapper_marks_file_as_claudex_wrapper(isolated_dir):
    path = isolated_dir / "bin" / "codex"
    _write_wrapper(path, _wrapper_script(Provider.CODEX))
    assert _is_claudex_wrapper(path) is True
