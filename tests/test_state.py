"""
Tests for state.py — read/write of .claudex/{state.json, handoff.md, transcript.ndjson}.
"""

import json
from pathlib import Path

import pytest

from claudex.models import ClaudexState, Provider, ProviderState
from claudex.state import (
    append_transcript,
    clear_claudex,
    load_handoff,
    load_state,
    save_handoff,
    save_state,
)


# ── load_state ────────────────────────────────────────────────────────────────


def test_load_state_returns_default_when_file_missing(isolated_dir):
    state = load_state()
    assert isinstance(state, ClaudexState)
    assert state.last_provider is None
    assert state.turn_count == 0
    assert state.claude.session_id is None


def test_save_and_load_state_roundtrip(isolated_dir):
    state = ClaudexState(last_provider=Provider.CLAUDE, turn_count=7)
    state.claude = ProviderState(session_id="sess_abc")
    state.codex = ProviderState(session_id="thread_xyz", consecutive_errors=2)

    save_state(state)

    loaded = load_state()
    assert loaded.last_provider == Provider.CLAUDE
    assert loaded.turn_count == 7
    assert loaded.claude.session_id == "sess_abc"
    assert loaded.codex.session_id == "thread_xyz"
    assert loaded.codex.consecutive_errors == 2


def test_save_state_creates_claudex_dir(isolated_dir):
    assert not (isolated_dir / ".claudex").exists()
    save_state(ClaudexState())
    assert (isolated_dir / ".claudex" / "state.json").exists()


def test_load_state_survives_corrupt_json(isolated_dir):
    (isolated_dir / ".claudex").mkdir()
    (isolated_dir / ".claudex" / "state.json").write_text("{ broken json %%%")

    state = load_state()  # Should not raise
    assert isinstance(state, ClaudexState)
    assert state.turn_count == 0


def test_save_state_updates_updated_at(isolated_dir):
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    save_state(ClaudexState())
    loaded = load_state()
    assert loaded.updated_at >= before


# ── handoff ───────────────────────────────────────────────────────────────────


def test_load_handoff_returns_none_when_missing(isolated_dir):
    assert load_handoff() is None


def test_save_and_load_handoff_roundtrip(isolated_dir):
    content = "# Handoff\n\n## Current Goal\n\nFix the bug.\n"
    save_handoff(content)
    loaded = load_handoff()
    assert loaded == content


def test_save_handoff_overwrites(isolated_dir):
    save_handoff("first version")
    save_handoff("second version")
    assert load_handoff() == "second version"


# ── transcript ────────────────────────────────────────────────────────────────


def test_append_transcript_creates_ndjson(isolated_dir):
    append_transcript({"provider": "claude", "user_prompt": "hello", "ts": "t1"})
    append_transcript({"provider": "codex", "user_prompt": "world", "ts": "t2"})

    path = isolated_dir / ".claudex" / "transcript.ndjson"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["provider"] == "claude"
    assert first["user_prompt"] == "hello"

    second = json.loads(lines[1])
    assert second["provider"] == "codex"


def test_append_transcript_is_append_only(isolated_dir):
    for i in range(5):
        append_transcript({"i": i})

    path = isolated_dir / ".claudex" / "transcript.ndjson"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[4])["i"] == 4


# ── clear_claudex ────────────────────────────────────────────────────────────


def test_clear_claudex_removes_directory(isolated_dir):
    save_state(ClaudexState())
    save_handoff("something")
    assert (isolated_dir / ".claudex").exists()

    clear_claudex()
    assert not (isolated_dir / ".claudex").exists()


def test_clear_claudex_is_safe_when_missing(isolated_dir):
    clear_claudex()  # Should not raise
