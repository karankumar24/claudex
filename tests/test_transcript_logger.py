import json
from datetime import datetime, timezone

from claudex.models import Provider
from claudex.transcript import record_turn


def test_record_turn_persists_cooldown_metadata(isolated_dir):
    record_turn(
        provider=Provider.CLAUDE,
        user_prompt="continue",
        assistant_text=None,
        session_id="sess_123",
        cooldown_until=datetime(2026, 2, 28, 2, 0, tzinfo=timezone.utc),
        cooldown_source="quota_reset_time",
        cooldown_reason="quota-exhausted:provider-reset-time",
        error="QUOTA_EXHAUSTED: You've hit your limit",
        switch_from="claude",
        switch_to="codex",
        switch_prompt_decision="approved",
    )

    path = isolated_dir / ".claudex" / "transcript.ndjson"
    line = path.read_text().strip()
    entry = json.loads(line)

    assert entry["provider"] == "claude"
    assert entry["session_id"] == "sess_123"
    assert entry["cooldown_until"] == "2026-02-28T02:00:00+00:00"
    assert entry["cooldown_source"] == "quota_reset_time"
    assert entry["cooldown_reason"] == "quota-exhausted:provider-reset-time"
    assert entry["switch_from"] == "claude"
    assert entry["switch_to"] == "codex"
    assert entry["switch_prompt_decision"] == "approved"
