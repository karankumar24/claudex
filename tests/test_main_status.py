from datetime import datetime, timedelta, timezone

from claudex.main import _format_cooldown, _format_cooldown_source, _format_cooldown_until
from claudex.models import ProviderState


def test_format_cooldown_ready_returns_dash():
    now = datetime(2026, 2, 27, 23, 0, tzinfo=timezone.utc)
    assert _format_cooldown(ProviderState(), now) == "—"


def test_format_cooldown_includes_remaining_until_and_source():
    now = datetime(2026, 2, 27, 23, 0, tzinfo=timezone.utc)
    ps = ProviderState(
        cooldown_until=now + timedelta(minutes=90),
        cooldown_source="quota_reset_time",
    )

    rendered = _format_cooldown(ps, now)
    until = _format_cooldown_until(ps, now)
    source = _format_cooldown_source(ps, now)
    assert "90 min" in rendered
    assert "2026-02-28 00:30 UTC" in until
    assert source == "quota_reset_time"


def test_format_cooldown_source_hidden_when_cooldown_expired():
    now = datetime(2026, 2, 27, 23, 0, tzinfo=timezone.utc)
    ps = ProviderState(
        cooldown_until=now - timedelta(minutes=1),
        cooldown_source="quota_reset_time",
    )
    assert _format_cooldown_until(ps, now) == "—"
    assert _format_cooldown_source(ps, now) == "—"
