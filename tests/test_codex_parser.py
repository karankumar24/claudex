"""
Tests for the Codex JSONL event stream parser (CodexProvider._parse_jsonl).
"""

import json
from unittest.mock import MagicMock

import pytest

from claudex.models import ErrorClass
from claudex.providers.codex import CodexProvider


# ── Helpers ───────────────────────────────────────────────────────────────────


def _proc(stdout: str, returncode: int = 0):
    """Build a minimal CompletedProcess-like mock."""
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


def _jsonl(*events) -> str:
    """Serialize a sequence of dicts as newline-delimited JSON."""
    return "\n".join(json.dumps(e) for e in events)


PROVIDER = CodexProvider()


# ── Success cases ─────────────────────────────────────────────────────────────


def test_parse_successful_response():
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "thread_abc"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [{"type": "output_text", "text": "Hello world!"}],
            },
        },
    )
    result = PROVIDER._parse_jsonl(_proc(stdout))
    assert result.success is True
    assert result.text == "Hello world!"
    assert result.session_id == "thread_abc"


def test_parse_multi_block_content():
    """Multiple content blocks should be joined with newlines."""
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [
                    {"type": "output_text", "text": "Part one."},
                    {"type": "output_text", "text": "Part two."},
                ],
            },
        },
    )
    result = PROVIDER._parse_jsonl(_proc(stdout))
    assert result.success is True
    assert "Part one." in result.text
    assert "Part two." in result.text


def test_parse_uses_last_agent_message():
    """When multiple agent_message items arrive, keep the last one."""
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [{"text": "Intermediate thinking…"}],
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [{"text": "Final answer."}],
            },
        },
    )
    result = PROVIDER._parse_jsonl(_proc(stdout))
    assert result.success is True
    assert result.text == "Final answer."


def test_parse_session_id_from_id_field():
    """Supports 'id' as a fallback field name for the thread id."""
    stdout = _jsonl(
        {"type": "thread.started", "id": "alt_id_format"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "content": [{"text": "ok"}]},
        },
    )
    result = PROVIDER._parse_jsonl(_proc(stdout))
    assert result.session_id == "alt_id_format"


def test_parse_ignores_non_json_lines():
    """Non-JSON progress lines should be silently skipped."""
    stdout = (
        "Initializing sandbox…\n"
        + json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "content": [{"text": "Hi!"}]},
        })
    )
    result = PROVIDER._parse_jsonl(_proc(stdout))
    assert result.success is True
    assert result.text == "Hi!"


# ── Error cases ───────────────────────────────────────────────────────────────


def test_parse_quota_exhausted_error():
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "error", "message": "quota exhausted for this billing period", "status": 429},
    )
    result = PROVIDER._parse_jsonl(_proc(stdout, returncode=1))
    assert result.success is False
    assert result.error_class == ErrorClass.QUOTA_EXHAUSTED


def test_parse_transient_rate_limit():
    stdout = _jsonl(
        {"type": "error", "message": "rate limit exceeded, please retry", "status": 429},
    )
    result = PROVIDER._parse_jsonl(_proc(stdout, returncode=1))
    assert result.success is False
    assert result.error_class == ErrorClass.TRANSIENT_RATE_LIMIT


def test_parse_auth_error():
    stdout = _jsonl(
        {"type": "error", "message": "unauthorized — check your authentication", "status": 401},
    )
    result = PROVIDER._parse_jsonl(_proc(stdout, returncode=1))
    assert result.success is False
    assert result.error_class == ErrorClass.AUTH_REQUIRED


def test_parse_generic_error():
    stdout = _jsonl(
        {"type": "error", "message": "internal server error", "status": 500},
    )
    result = PROVIDER._parse_jsonl(_proc(stdout, returncode=1))
    assert result.success is False
    assert result.error_class == ErrorClass.OTHER_ERROR


def test_parse_no_assistant_message_exit_zero():
    """Exit 0 but no agent_message → OTHER_ERROR (unexpected empty response)."""
    stdout = _jsonl({"type": "thread.started", "thread_id": "t1"})
    result = PROVIDER._parse_jsonl(_proc(stdout, returncode=0))
    assert result.success is False
    assert result.error_class == ErrorClass.OTHER_ERROR


def test_parse_nonzero_exit_no_json():
    """Non-zero exit with no parseable JSONL should return a classified error."""
    result = PROVIDER._parse_jsonl(_proc("some error text with rate limit", returncode=1))
    assert result.success is False
    assert result.error_class == ErrorClass.TRANSIENT_RATE_LIMIT


def test_parse_captures_session_id_on_error():
    """Even when an error occurs, we should preserve any thread_id we captured."""
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "saved_id"},
        {"type": "error", "message": "something went wrong", "status": 500},
    )
    result = PROVIDER._parse_jsonl(_proc(stdout, returncode=1))
    assert result.session_id == "saved_id"
