"""
Low-level IO for the .claudex/ directory.

All paths are relative to the current working directory so the tool
works correctly in any git repo without configuration.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ClaudexState

# ── Directory / file paths (relative to CWD) ─────────────────────────────────

AISWITCH_DIR = Path(".claudex")
STATE_FILE = AISWITCH_DIR / "state.json"
HANDOFF_FILE = AISWITCH_DIR / "handoff.md"
TRANSCRIPT_FILE = AISWITCH_DIR / "transcript.ndjson"
REPO_CONFIG_FILE = AISWITCH_DIR / "config.toml"

# User-global config (lower priority than repo config)
USER_CONFIG_FILE = Path.home() / ".config" / "claudex" / "config.toml"


# ── Directory management ──────────────────────────────────────────────────────


def ensure_dir() -> None:
    """Create .claudex/ if it doesn't exist."""
    AISWITCH_DIR.mkdir(exist_ok=True)


# ── State read/write ──────────────────────────────────────────────────────────


def load_state() -> ClaudexState:
    """
    Load state.json from .claudex/.
    Returns a fresh default state if the file doesn't exist or is corrupt.
    """
    if not STATE_FILE.exists():
        return ClaudexState()
    try:
        return ClaudexState.model_validate_json(STATE_FILE.read_text())
    except Exception:
        # Corrupt / schema-changed state — start fresh rather than crash
        return ClaudexState()


def save_state(state: ClaudexState) -> None:
    """Persist state to .claudex/state.json, updating the updated_at timestamp."""
    ensure_dir()
    state.updated_at = datetime.now(timezone.utc)
    STATE_FILE.write_text(state.model_dump_json(indent=2))


# ── Handoff read/write ────────────────────────────────────────────────────────


def load_handoff() -> Optional[str]:
    """Return the contents of handoff.md, or None if it doesn't exist."""
    if not HANDOFF_FILE.exists():
        return None
    return HANDOFF_FILE.read_text(encoding="utf-8")


def save_handoff(content: str) -> None:
    """Overwrite handoff.md with new content."""
    ensure_dir()
    HANDOFF_FILE.write_text(content, encoding="utf-8")


# ── Transcript ────────────────────────────────────────────────────────────────


def append_transcript(entry: dict) -> None:
    """
    Append one JSON line to transcript.ndjson.
    The transcript is append-only; never truncated.
    Entries contain: ts, provider, user_prompt, assistant_text, session_id, error.
    """
    ensure_dir()
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with TRANSCRIPT_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Reset ─────────────────────────────────────────────────────────────────────


def clear_claudex() -> None:
    """Delete the entire .claudex/ directory (used by `claudex reset`)."""
    import shutil
    if AISWITCH_DIR.exists():
        shutil.rmtree(AISWITCH_DIR)
