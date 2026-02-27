"""
Tests for router.py — provider selection, retry, backoff, and failover logic.

We use patch.dict to swap the module-level PROVIDERS dict with mocks so that
no real CLI calls are made.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from claudex.models import ClaudexState, ErrorClass, Provider, ProviderState
from claudex.providers.base import ProviderResult
from claudex.router import PROVIDERS, get_available_providers, run_with_retry

# ── Config fixture ────────────────────────────────────────────────────────────

BASE_CONFIG = {
    "provider_order": ["claude", "codex"],
    "retry": {
        "max_retries": 2,
        "backoff_base": 0,       # zero sleep in tests
        "backoff_max": 0,
        "cooldown_minutes": 60,
    },
    "limits": {},
    "claude": {},
    "codex": {},
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok(text="response", session_id="s1") -> ProviderResult:
    return ProviderResult(success=True, text=text, session_id=session_id)


def _err(cls: ErrorClass) -> ProviderResult:
    return ProviderResult(success=False, error_class=cls, error_message="err")


def _mock_provider(result: ProviderResult) -> MagicMock:
    m = MagicMock()
    m.run.return_value = result
    return m


# ── get_available_providers ───────────────────────────────────────────────────


def test_available_both_ready():
    state = ClaudexState()
    now = datetime.now(timezone.utc)
    available = get_available_providers(state, BASE_CONFIG, now=now)
    assert available == [Provider.CLAUDE, Provider.CODEX]


def test_available_claude_in_cooldown():
    state = ClaudexState()
    state.claude.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)
    available = get_available_providers(state, BASE_CONFIG)
    assert available == [Provider.CODEX]


def test_available_both_in_cooldown():
    state = ClaudexState()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    state.claude.cooldown_until = future
    state.codex.cooldown_until = future
    available = get_available_providers(state, BASE_CONFIG)
    assert available == []


def test_available_cooldown_expired():
    """A provider whose cooldown has passed should be available again."""
    state = ClaudexState()
    state.claude.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    available = get_available_providers(state, BASE_CONFIG)
    assert Provider.CLAUDE in available


def test_available_respects_custom_order():
    state = ClaudexState()
    config = dict(BASE_CONFIG, provider_order=["codex", "claude"])
    available = get_available_providers(state, config)
    assert available == [Provider.CODEX, Provider.CLAUDE]


def test_available_ignores_unknown_provider_name():
    state = ClaudexState()
    config = dict(BASE_CONFIG, provider_order=["claude", "gpt5", "codex"])
    available = get_available_providers(state, config)
    assert available == [Provider.CLAUDE, Provider.CODEX]


# ── run_with_retry — success cases ────────────────────────────────────────────


def test_success_on_first_try(isolated_dir):
    claude_mock = _mock_provider(_ok(text="hello from claude", session_id="c1"))
    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: MagicMock()}):
        result, provider, state = run_with_retry("hi", ClaudexState(), BASE_CONFIG)

    assert result.success is True
    assert result.text == "hello from claude"
    assert provider == Provider.CLAUDE
    assert state.turn_count == 1
    assert state.claude.session_id == "c1"
    assert state.last_provider == Provider.CLAUDE


def test_success_updates_session_id(isolated_dir):
    claude_mock = _mock_provider(_ok(session_id="new_session"))
    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: MagicMock()}):
        initial_state = ClaudexState()
        initial_state.claude.session_id = "old_session"
        _, _, state = run_with_retry("hi", initial_state, BASE_CONFIG)

    assert state.claude.session_id == "new_session"


# ── run_with_retry — failover cases ──────────────────────────────────────────


def test_failover_on_quota_exhausted(isolated_dir):
    """Claude QUOTA_EXHAUSTED → should transparently fall back to Codex."""
    claude_mock = _mock_provider(_err(ErrorClass.QUOTA_EXHAUSTED))
    codex_mock = _mock_provider(_ok(text="codex rescued you", session_id="t1"))

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: codex_mock}):
        result, provider, state = run_with_retry("fix it", ClaudexState(), BASE_CONFIG)

    assert result.success is True
    assert result.text == "codex rescued you"
    assert provider == Provider.CODEX
    # Claude should be in a long cooldown
    assert state.claude.cooldown_until is not None
    assert state.claude.cooldown_until > datetime.now(timezone.utc)


def test_failover_injects_handoff_for_fallback_provider(isolated_dir):
    """
    When falling back, the fallback provider should receive the handoff-
    augmented prompt, not the bare user prompt.
    """
    claude_mock = _mock_provider(_err(ErrorClass.QUOTA_EXHAUSTED))
    codex_mock = _mock_provider(_ok())

    handoff = "# Handoff\n\n## Current Goal\n\nFix auth bug.\n"

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: codex_mock}):
        run_with_retry(
            "continue",
            ClaudexState(),
            BASE_CONFIG,
            handoff_content=handoff,
        )

    # Codex should have received a prompt that contains the handoff content
    call_args = codex_mock.run.call_args
    prompt_sent = call_args[1]["prompt"] if call_args[1] else call_args[0][0]
    assert "Fix auth bug" in prompt_sent


def test_preferred_provider_gets_bare_prompt(isolated_dir):
    """The first (preferred) provider should get the original prompt, not augmented."""
    claude_mock = _mock_provider(_ok())

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: MagicMock()}):
        run_with_retry(
            "hello",
            ClaudexState(),
            BASE_CONFIG,
            handoff_content="# some old handoff",
        )

    call_args = claude_mock.run.call_args
    prompt_sent = call_args[1]["prompt"] if call_args[1] else call_args[0][0]
    assert prompt_sent == "hello"


# ── run_with_retry — retry logic ─────────────────────────────────────────────


def test_retry_on_transient_rate_limit_then_success(isolated_dir):
    """
    Provider returns TRANSIENT_RATE_LIMIT on first attempt,
    then succeeds on the second — should retry on the SAME provider.
    """
    claude_mock = MagicMock()
    claude_mock.run.side_effect = [
        _err(ErrorClass.TRANSIENT_RATE_LIMIT),
        _ok(text="eventually ok"),
    ]

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: MagicMock()}):
        result, provider, state = run_with_retry("hi", ClaudexState(), BASE_CONFIG)

    assert result.success is True
    assert claude_mock.run.call_count == 2
    assert provider == Provider.CLAUDE


def test_exhausted_retries_switches_provider(isolated_dir):
    """
    After max_retries TRANSIENT_RATE_LIMIT errors on Claude,
    should switch to Codex.
    """
    claude_mock = MagicMock()
    # max_retries=2 → 3 total attempts (0, 1, 2)
    claude_mock.run.return_value = _err(ErrorClass.TRANSIENT_RATE_LIMIT)
    codex_mock = _mock_provider(_ok(text="codex steps in"))

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: codex_mock}):
        result, provider, state = run_with_retry("hi", ClaudexState(), BASE_CONFIG)

    assert result.success is True
    assert provider == Provider.CODEX
    assert claude_mock.run.call_count == 3  # 1 initial + 2 retries


# ── run_with_retry — non-retriable errors ────────────────────────────────────


def test_auth_error_surfaces_immediately(isolated_dir):
    """AUTH_REQUIRED must not retry or failover — surface to the caller immediately."""
    claude_mock = _mock_provider(_err(ErrorClass.AUTH_REQUIRED))

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: MagicMock()}):
        result, provider, state = run_with_retry("hi", ClaudexState(), BASE_CONFIG)

    assert result.success is False
    assert result.error_class == ErrorClass.AUTH_REQUIRED
    assert claude_mock.run.call_count == 1  # No retries


def test_other_error_surfaces_immediately(isolated_dir):
    """OTHER_ERROR must not retry or failover."""
    claude_mock = _mock_provider(_err(ErrorClass.OTHER_ERROR))
    codex_mock = _mock_provider(_ok())

    with patch.dict(PROVIDERS, {Provider.CLAUDE: claude_mock, Provider.CODEX: codex_mock}):
        result, provider, state = run_with_retry("hi", ClaudexState(), BASE_CONFIG)

    assert result.success is False
    assert result.error_class == ErrorClass.OTHER_ERROR
    codex_mock.run.assert_not_called()


# ── run_with_retry — all providers unavailable ────────────────────────────────


def test_all_providers_in_cooldown_returns_none(isolated_dir):
    """When all providers are in cooldown, result should be None."""
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    state = ClaudexState()
    state.claude.cooldown_until = future
    state.codex.cooldown_until = future

    result, provider, _ = run_with_retry("hi", state, BASE_CONFIG)
    assert result is None
    assert provider is None
