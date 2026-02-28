"""Pydantic models for claudex state."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Provider(str, Enum):
    """The two supported AI CLI providers."""
    CLAUDE = "claude"
    CODEX = "codex"


class ErrorClass(str, Enum):
    """
    Classification of provider errors, used to decide whether to retry,
    switch, or surface the error immediately.
    """
    # Monthly/plan quota hit — switch immediately, long cooldown
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    # Temporary 429 / backpressure — retry with backoff, then switch
    TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"
    # OAuth / API key problem — surface and stop, no retry
    AUTH_REQUIRED = "AUTH_REQUIRED"
    # Anything else (CLI crash, parse failure, etc.)
    OTHER_ERROR = "OTHER_ERROR"


class ProviderState(BaseModel):
    """Per-provider runtime state tracked across turns."""
    # The session/thread ID from the last successful turn (used for resumption)
    session_id: Optional[str] = None
    last_used: Optional[datetime] = None
    # If set, this provider is in cooldown until this UTC timestamp
    cooldown_until: Optional[datetime] = None
    # Timestamp when the current cooldown was applied
    cooldown_started_at: Optional[datetime] = None
    # Why this cooldown exists: quota_reset_time | quota_default | transient_retry_exhausted
    cooldown_source: Optional[str] = None
    # Additional machine-readable reason
    cooldown_reason: Optional[str] = None
    # Bounded excerpt from provider error text used to derive cooldown
    cooldown_message_excerpt: Optional[str] = None
    # Running count of consecutive errors (reset on success)
    consecutive_errors: int = 0


class ClaudexState(BaseModel):
    """
    Root state object serialized to .claudex/state.json.
    One file per repo (lives next to .git/).
    """
    last_provider: Optional[Provider] = None
    claude: ProviderState = Field(default_factory=ProviderState)
    codex: ProviderState = Field(default_factory=ProviderState)
    turn_count: int = 0
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def get_provider_state(self, provider: Provider) -> ProviderState:
        return self.claude if provider == Provider.CLAUDE else self.codex

    def set_provider_state(self, provider: Provider, ps: ProviderState) -> None:
        if provider == Provider.CLAUDE:
            self.claude = ps
        else:
            self.codex = ps
