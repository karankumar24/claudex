"""
Tests for truncation / line-limit utilities in handoff.py.
"""

import pytest

from claudex.handoff import (
    _enforce_line_limit,
    _extract_section,
    _truncate,
    get_repo_snapshot,
    update_handoff,
)

# ── _truncate ─────────────────────────────────────────────────────────────────


def test_truncate_short_string_unchanged():
    assert _truncate("hello world", 100) == "hello world"


def test_truncate_empty_string():
    assert _truncate("", 10) == ""


def test_truncate_exactly_at_limit():
    text = "a" * 50
    assert _truncate(text, 50) == text


def test_truncate_long_string_adds_note():
    text = "x" * 200
    result = _truncate(text, 50)
    assert len(result) < 200
    assert "truncated" in result
    assert result.startswith("x" * 50)


# ── _enforce_line_limit ───────────────────────────────────────────────────────


def test_enforce_line_limit_within_limit():
    text = "\n".join(f"line {i}" for i in range(10))
    result = _enforce_line_limit(text, 20)
    assert result == text


def test_enforce_line_limit_exactly_at_limit():
    text = "\n".join(f"line {i}" for i in range(30))
    result = _enforce_line_limit(text, 30)
    assert result == text


def test_enforce_line_limit_truncates_middle():
    text = "\n".join(f"line {i}" for i in range(100))
    result = _enforce_line_limit(text, 30)
    lines = result.splitlines()
    # Should be at most 30 + 3 lines (30 content + omission marker lines)
    assert len(lines) <= 33
    assert "omitted" in result


def test_enforce_line_limit_preserves_first_and_last_lines():
    text = "\n".join(f"line {i}" for i in range(100))
    result = _enforce_line_limit(text, 30)
    assert "line 0" in result
    assert "line 99" in result


# ── _extract_section ──────────────────────────────────────────────────────────


def test_extract_section_found():
    text = """\
# Doc

## Current Goal

Do the thing.

## Current Plan

Step 1
Step 2

## Other Section

other stuff
"""
    result = _extract_section(text, "Current Goal")
    assert "Do the thing." in result
    assert "Step 1" not in result


def test_extract_section_not_found():
    text = "# Doc\n## Other\nstuff\n"
    result = _extract_section(text, "Nonexistent Section")
    assert result == ""


def test_extract_section_at_end_of_document():
    text = "## First Section\nfirst\n## Last Section\nlast line\n"
    result = _extract_section(text, "Last Section")
    assert "last line" in result


def test_extract_section_empty_body():
    text = "## Empty Section\n\n## Next Section\ncontent\n"
    result = _extract_section(text, "Empty Section")
    assert result == ""


# ── update_handoff ────────────────────────────────────────────────────────────


def test_update_handoff_contains_required_sections():
    config = {"limits": {"max_handoff_lines": 350}}
    result = update_handoff(
        user_prompt="Fix the login bug",
        assistant_text="I've identified the issue in auth.py line 42.",
        provider="claude",
        config=config,
    )
    assert "## Current Goal" in result
    assert "## Current Plan" in result
    assert "## What Changed This Turn" in result
    assert "## Open Questions / Blockers" in result
    assert "## Next Concrete Steps" in result


def test_update_handoff_carries_forward_goal(isolated_dir):
    config = {"limits": {"max_handoff_lines": 350}}
    previous = """\
# Handoff

## Current Goal

Build a REST API.

## Current Plan

Step 1, Step 2.

## What Changed This Turn

nothing

## Open Questions / Blockers

none

## Next Concrete Steps

do something
"""
    result = update_handoff(
        user_prompt="Continue",
        assistant_text="Done.",
        provider="codex",
        config=config,
        previous_handoff=previous,
    )
    assert "Build a REST API." in result


def test_update_handoff_respects_line_limit():
    config = {"limits": {"max_handoff_lines": 20}}
    long_text = "word " * 2000
    result = update_handoff(
        user_prompt="question",
        assistant_text=long_text,
        provider="claude",
        config=config,
    )
    assert len(result.splitlines()) <= 23  # 20 + 3 omission marker lines


# ── get_repo_snapshot ─────────────────────────────────────────────────────────


def test_get_repo_snapshot_skips_full_diff_when_numstat_exceeds_limit(monkeypatch):
    calls: list[tuple[str, ...]] = []
    outputs = {
        ("git", "rev-parse", "--is-inside-work-tree"): "true\n",
        ("git", "status", "--porcelain"): "",
        ("git", "log", "-n", "5", "--oneline"): "",
        ("git", "diff", "--stat"): " big.py | 250 ++++++++++++++++++++++++++\n",
        ("git", "diff", "--numstat"): "250\t0\tbig.py\n",
    }

    def fake_run_git(cmd: list[str]) -> str:
        key = tuple(cmd)
        calls.append(key)
        return outputs.get(key, "")

    monkeypatch.setattr("claudex.handoff._run_git", fake_run_git)

    snapshot = get_repo_snapshot({"limits": {"max_diff_lines": 200, "max_diff_bytes": 8000}})

    assert "Full diff omitted" in snapshot
    assert ("git", "diff") not in calls


def test_get_repo_snapshot_includes_full_diff_when_under_limits(monkeypatch):
    calls: list[tuple[str, ...]] = []
    outputs = {
        ("git", "rev-parse", "--is-inside-work-tree"): "true\n",
        ("git", "status", "--porcelain"): "M src/app.py\n",
        ("git", "log", "-n", "5", "--oneline"): "abc123 first commit\n",
        ("git", "diff", "--stat"): " src/app.py | 2 +-\n",
        ("git", "diff", "--numstat"): "1\t1\tsrc/app.py\n",
        ("git", "diff"): "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
    }

    def fake_run_git(cmd: list[str]) -> str:
        key = tuple(cmd)
        calls.append(key)
        return outputs.get(key, "")

    monkeypatch.setattr("claudex.handoff._run_git", fake_run_git)

    snapshot = get_repo_snapshot({"limits": {"max_diff_lines": 200, "max_diff_bytes": 8000}})

    assert "**Full diff:**" in snapshot
    assert ("git", "diff") in calls
