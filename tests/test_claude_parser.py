"""
Tests for Claude provider output parsing.
"""

import json
from unittest.mock import MagicMock

from claudex.models import ErrorClass
from claudex.providers.claude import ClaudeProvider


def _proc(stdout: str, returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


PROVIDER = ClaudeProvider()


def test_parse_json_array_result_success():
    stdout = json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "result": "Hello from Claude", "session_id": "sess_1", "is_error": False},
        ]
    )

    result = PROVIDER._parse(_proc(stdout), stdout)
    assert result.success is True
    assert result.text == "Hello from Claude"
    assert result.session_id == "sess_1"


def test_parse_json_array_error_classified():
    stdout = json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "result": "Usage limit reached for this billing period",
                "session_id": "sess_2",
                "is_error": True,
            },
        ]
    )

    result = PROVIDER._parse(_proc(stdout, returncode=1), stdout)
    assert result.success is False
    assert result.error_class == ErrorClass.QUOTA_EXHAUSTED
    assert result.session_id == "sess_2"


def test_parse_empty_result_is_still_success():
    stdout = json.dumps(
        [{"type": "result", "result": "", "session_id": "sess_empty", "is_error": False}]
    )

    result = PROVIDER._parse(_proc(stdout), stdout)
    assert result.success is True
    assert result.text == ""
    assert result.session_id == "sess_empty"


def test_parse_bare_result_object_is_supported():
    stdout = json.dumps({"result": "Legacy format", "session_id": "sess_legacy", "is_error": False})

    result = PROVIDER._parse(_proc(stdout), stdout)
    assert result.success is True
    assert result.text == "Legacy format"
    assert result.session_id == "sess_legacy"


def test_parse_error_subtype_with_empty_result_is_failure():
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "result": "",
            "session_id": "sess_err",
            "is_error": False,
            "errors": ["EPERM: operation not permitted"],
        }
    )

    result = PROVIDER._parse(_proc(stdout), stdout)
    assert result.success is False
    assert result.error_class == ErrorClass.OTHER_ERROR
    assert result.session_id == "sess_err"
