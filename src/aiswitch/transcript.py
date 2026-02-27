"""
Append-only transcript logger.

Each turn appends one JSON line to .aiswitch/transcript.ndjson containing:
  ts              — ISO-8601 UTC timestamp
  provider        — "claude" or "codex"
  user_prompt     — the original (un-augmented) user prompt
  assistant_text  — the response text, or null on error
  session_id      — provider session/thread id if available
  error           — "ERROR_CLASS: message" on failure, null on success
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import Provider
from .state import append_transcript


def record_turn(
    provider: Optional[Provider],
    user_prompt: str,
    assistant_text: Optional[str],
    session_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """
    Append one turn to the append-only transcript.
    Called after every run_with_retry(), whether successful or not.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": provider.value if provider else None,
        "user_prompt": user_prompt,
        "assistant_text": assistant_text,
        "session_id": session_id,
        "error": error,
    }
    append_transcript(entry)
