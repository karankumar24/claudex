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

import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

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

        # ── Retry loop for this provider ──────────────────────────────────────
        for attempt in range(max_retries + 1):
            result = provider_obj.run(
                prompt=prompt,
                session_id=ps.session_id,
                config=config,
            )

            if result.success:
                # ✓ Update session ID and clear error bookkeeping
                ps.session_id = result.session_id or ps.session_id
                ps.last_used = datetime.now(timezone.utc)
                ps.consecutive_errors = 0
                state.set_provider_state(provider, ps)
                state.last_provider = provider
                state.turn_count += 1
                return result, provider, state

            # ── Handle failure ────────────────────────────────────────────────

            ps.consecutive_errors += 1
            state.set_provider_state(provider, ps)

            if result.error_class == ErrorClass.QUOTA_EXHAUSTED:
                # Hard quota hit — long cooldown, immediately try next provider
                ps.cooldown_until = datetime.now(timezone.utc) + timedelta(
                    minutes=cooldown_minutes
                )
                state.set_provider_state(provider, ps)
                break  # Go to next provider

            elif result.error_class == ErrorClass.TRANSIENT_RATE_LIMIT:
                if attempt < max_retries:
                    # Wait with exponential backoff, then retry the SAME provider
                    wait = min(backoff_base ** attempt, backoff_max)
                    if wait > 0:
                        time.sleep(wait)
                    continue  # Retry
                else:
                    # Exhausted retries — short cooldown, try next provider
                    ps.cooldown_until = datetime.now(timezone.utc) + timedelta(
                        minutes=transient_cooldown_minutes
                    )
                    state.set_provider_state(provider, ps)
                    break

            elif result.error_class in (
                ErrorClass.AUTH_REQUIRED,
                ErrorClass.OTHER_ERROR,
            ):
                # Non-retriable — surface immediately without trying other providers
                return result, provider, state

    # All providers failed or are now in cooldown
    return result, last_provider, state
