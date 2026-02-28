"""
Core routing logic: provider selection, retry, backoff, and failover.

The routing algorithm:
  1. Build the ordered list of available providers (skip those in cooldown).
  2. For each provider in order:
     a. If this is NOT the first provider we're trying (i.e. we're falling
        back), rebuild the prompt with handoff context prepended so the new
        provider has full continuity.
     b. Run the provider, retrying on TRANSIENT_RATE_LIMIT with exponential
        backoff up to max_retries times.
     c. On success → return immediately.
     d. On QUOTA_EXHAUSTED → put provider in a long cooldown, try the next.
     e. On AUTH_REQUIRED / OTHER_ERROR → surface immediately (no retry/switch).
  3. If all providers fail or are in cooldown, return the last result.

This module is IO-free except for the subprocess calls inside each provider.
All state mutations are returned (not written to disk) — callers save to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .handoff import build_provider_prompt
from .models import ClaudexState, ErrorClass, Provider, ProviderState
from .providers.base import BaseProvider, ProviderResult
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider

# ── Provider registry ─────────────────────────────────────────────────────────

# Module-level singletons — both providers are stateless objects.
# Tests can patch this dict to inject mocks.
PROVIDERS: dict[Provider, BaseProvider] = {
    Provider.CLAUDE: ClaudeProvider(),
    Provider.CODEX: CodexProvider(),
}

# Defensive fallback patterns for quota/plan exhaustion text.
# Used only when a provider returns OTHER_ERROR with a clearly limit-like message.
_LIMIT_TEXT_PATTERNS = (
    "usage limit",
    "quota",
    "hit your limit",
    "limit reached",
    "billing period",
    "resets ",
    "claude.ai/settings/limits",
)

_RESET_TIME_12H_PATTERN = re.compile(
    r"resets?\s+(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>am|pm)\s*[.,:;\-·]?\s*\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)
_RESET_TIME_24H_PATTERN = re.compile(
    r"resets?\s+(?:at\s+)?(?P<hour>(?:[01]?\d|2[0-3])):(?P<minute>[0-5]\d)"
    r"\s*[.,:;\-·]?\s*\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _CooldownDecision:
    until: datetime
    source: str
    reason: str
    message_excerpt: Optional[str] = None


# ── Provider availability ─────────────────────────────────────────────────────


def get_available_providers(
    state: ClaudexState,
    config: dict,
    now: Optional[datetime] = None,
) -> list[Provider]:
    """
    Return providers in configured preference order, excluding those in cooldown.

    Parameters
    ----------
    state:
        Current ClaudexState (read-only here).
    config:
        Merged config dict.
    now:
        Override current time (useful for tests).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    order = config.get("provider_order", ["claude", "codex"])
    available: list[Provider] = []

    for name in order:
        try:
            p = Provider(name)
        except ValueError:
            continue  # Unknown name in config — ignore

        ps = state.get_provider_state(p)
        if ps.cooldown_until and ps.cooldown_until > now:
            continue  # Still cooling down

        available.append(p)

    return available


# ── Main routing entry point ──────────────────────────────────────────────────


def run_with_retry(
    user_prompt: str,
    state: ClaudexState,
    config: dict,
    handoff_content: Optional[str] = None,
) -> Tuple[Optional[ProviderResult], Optional[Provider], ClaudexState]:
    """
    Execute a single user prompt against the best available provider,
    handling retries and failover transparently.

    Parameters
    ----------
    user_prompt:
        The raw user prompt (without any handoff context prepended).
    state:
        Current ClaudexState; returned mutated on success/failure.
    config:
        Merged config dict (from load_config()).
    handoff_content:
        Contents of handoff.md if it exists, used when falling back to
        a provider that needs context injected.

    Returns
    -------
    (result, provider_used, updated_state)
      - result is None only if ALL providers are in cooldown.
      - provider_used is None only if result is None.
    """
    retry_cfg = config.get("retry", {})
    max_retries: int = retry_cfg.get("max_retries", 3)
    backoff_base: float = retry_cfg.get("backoff_base", 2.0)
    backoff_max: float = retry_cfg.get("backoff_max", 30.0)
    cooldown_minutes: int = retry_cfg.get("cooldown_minutes", 60)
    transient_cooldown_minutes: int = retry_cfg.get(
        "transient_cooldown_minutes", 5
    )

    available = get_available_providers(state, config)
    if not available:
        return None, None, state

    result: Optional[ProviderResult] = None
    last_provider: Optional[Provider] = None

    for idx, provider in enumerate(available):
        last_provider = provider
        ps: ProviderState = state.get_provider_state(provider)
        provider_obj: BaseProvider = PROVIDERS[provider]

        # ── Build the prompt for this provider ────────────────────────────────
        # If we are NOT on the first (preferred) provider, it means the previous
        # one failed and we're falling back. Inject handoff + repo context so
        # the new provider has full continuity without an active session.
        is_fallback = idx > 0
        if is_fallback:
            prompt = build_provider_prompt(
                user_prompt=user_prompt,
                config=config,
                is_resuming=True,
                handoff_content=handoff_content,
            )
        else:
            # First provider: use its session_id for resumption if available.
            # The session already contains the conversation history.
            prompt = user_prompt

        provider_session_id = None if is_fallback else ps.session_id

        # ── Retry loop for this provider ──────────────────────────────────────
        for attempt in range(max_retries + 1):
            result = provider_obj.run(
                prompt=prompt,
                session_id=provider_session_id,
                config=config,
            )

            if result.success:
                # ✓ Update session ID and clear error bookkeeping
                ps.session_id = result.session_id or ps.session_id
                ps.last_used = datetime.now(timezone.utc)
                ps.consecutive_errors = 0
                _clear_cooldown(ps)
                state.set_provider_state(provider, ps)
                state.last_provider = provider
                state.turn_count += 1
                return result, provider, state

            # ── Handle failure ────────────────────────────────────────────────

            ps.consecutive_errors += 1
            state.set_provider_state(provider, ps)

            effective_error = result.error_class
            if (
                effective_error == ErrorClass.OTHER_ERROR
                and _looks_like_limit_exhaustion(result.error_message)
            ):
                # Defensive behavior: if provider parsing misses a limit phrase,
                # still trigger quota cooldown + automatic failover.
                effective_error = ErrorClass.QUOTA_EXHAUSTED

            if effective_error == ErrorClass.QUOTA_EXHAUSTED:
                # Hard quota hit — long cooldown, immediately try next provider
                now_utc = datetime.now(timezone.utc)
                decision = _quota_cooldown_decision(
                    error_message=result.error_message,
                    now_utc=now_utc,
                    default_minutes=cooldown_minutes,
                )
                _apply_cooldown(ps, decision=decision, now_utc=now_utc)
                state.set_provider_state(provider, ps)
                break  # Go to next provider

            elif effective_error == ErrorClass.TRANSIENT_RATE_LIMIT:
                if attempt < max_retries:
                    # Wait with exponential backoff, then retry the SAME provider
                    wait = min(backoff_base ** attempt, backoff_max)
                    if wait > 0:
                        time.sleep(wait)
                    continue  # Retry
                else:
                    # Exhausted retries — short cooldown, try next provider
                    now_utc = datetime.now(timezone.utc)
                    decision = _transient_cooldown_decision(
                        now_utc=now_utc,
                        cooldown_minutes=transient_cooldown_minutes,
                        error_message=result.error_message,
                    )
                    _apply_cooldown(ps, decision=decision, now_utc=now_utc)
                    state.set_provider_state(provider, ps)
                    break

            elif effective_error in (
                ErrorClass.AUTH_REQUIRED,
                ErrorClass.OTHER_ERROR,
            ):
                # Non-retriable — surface immediately without trying other providers
                return result, provider, state

    # All providers failed or are now in cooldown
    return result, last_provider, state


def _looks_like_limit_exhaustion(message: Optional[str]) -> bool:
    if not message:
        return False
    lower = message.lower()
    return any(pattern in lower for pattern in _LIMIT_TEXT_PATTERNS)


def _clear_cooldown(ps: ProviderState) -> None:
    ps.cooldown_until = None
    ps.cooldown_started_at = None
    ps.cooldown_source = None
    ps.cooldown_reason = None
    ps.cooldown_message_excerpt = None


def _apply_cooldown(
    ps: ProviderState,
    decision: _CooldownDecision,
    now_utc: datetime,
) -> None:
    ps.cooldown_started_at = now_utc
    ps.cooldown_until = decision.until
    ps.cooldown_source = decision.source
    ps.cooldown_reason = decision.reason
    ps.cooldown_message_excerpt = decision.message_excerpt


def _message_excerpt(message: Optional[str], limit: int = 240) -> Optional[str]:
    if not message:
        return None
    normalized = " ".join(message.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _quota_cooldown_decision(
    error_message: Optional[str],
    now_utc: datetime,
    default_minutes: int,
) -> _CooldownDecision:
    reset_until = _extract_reset_time_utc(error_message, now_utc)
    if reset_until and reset_until > now_utc:
        return _CooldownDecision(
            until=reset_until,
            source="quota_reset_time",
            reason="quota-exhausted:provider-reset-time",
            message_excerpt=_message_excerpt(error_message),
        )

    return _CooldownDecision(
        until=now_utc + timedelta(minutes=default_minutes),
        source="quota_default",
        reason="quota-exhausted:default-cooldown",
        message_excerpt=_message_excerpt(error_message),
    )


def _transient_cooldown_decision(
    now_utc: datetime,
    cooldown_minutes: int,
    error_message: Optional[str],
) -> _CooldownDecision:
    return _CooldownDecision(
        until=now_utc + timedelta(minutes=cooldown_minutes),
        source="transient_retry_exhausted",
        reason="transient-rate-limit:retries-exhausted",
        message_excerpt=_message_excerpt(error_message),
    )


def _quota_cooldown_until(
    error_message: Optional[str],
    now_utc: datetime,
    default_minutes: int,
) -> datetime:
    """
    Prefer explicit provider reset timestamps (if present in error text).
    Fall back to fixed-duration cooldown when parsing is not possible.
    """
    return _quota_cooldown_decision(error_message, now_utc, default_minutes).until


def _extract_reset_time_utc(
    message: Optional[str],
    now_utc: datetime,
) -> Optional[datetime]:
    if not message:
        return None

    parsed_12h = _extract_12h_reset_time_utc(message, now_utc)
    if parsed_12h:
        return parsed_12h

    return _extract_24h_reset_time_utc(message, now_utc)


def _extract_12h_reset_time_utc(message: str, now_utc: datetime) -> Optional[datetime]:
    match = _RESET_TIME_12H_PATTERN.search(message)
    if not match:
        return None

    try:
        hour_12 = int(match.group("hour"))
        minute = int(match.group("minute") or "0")
    except ValueError:
        return None
    if not 1 <= hour_12 <= 12:
        return None

    ampm = (match.group("ampm") or "").lower()
    if ampm not in {"am", "pm"}:
        return None

    hour_24 = (hour_12 % 12) + (12 if ampm == "pm" else 0)
    return _build_reset_time_utc(
        now_utc=now_utc,
        tz_name=match.group("tz").strip(),
        hour_24=hour_24,
        minute=minute,
    )


def _extract_24h_reset_time_utc(message: str, now_utc: datetime) -> Optional[datetime]:
    match = _RESET_TIME_24H_PATTERN.search(message)
    if not match:
        return None

    try:
        hour_24 = int(match.group("hour"))
        minute = int(match.group("minute"))
    except ValueError:
        return None

    return _build_reset_time_utc(
        now_utc=now_utc,
        tz_name=match.group("tz").strip(),
        hour_24=hour_24,
        minute=minute,
    )


def _build_reset_time_utc(
    now_utc: datetime,
    tz_name: str,
    hour_24: int,
    minute: int,
) -> Optional[datetime]:
    if not 0 <= hour_24 <= 23 or not 0 <= minute <= 59:
        return None

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None

    local_now = now_utc.astimezone(tz)
    local_reset = local_now.replace(
        hour=hour_24,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if local_reset <= local_now:
        local_reset += timedelta(days=1)

    return local_reset.astimezone(timezone.utc)
