from datetime import datetime, timezone

from seekarr.engine import _quiet_hours_end_utc


def test_quiet_hours_uses_configured_timezone() -> None:
    # 08:00 UTC is 03:00 in -05:00.
    now_utc = datetime(2026, 2, 27, 8, 0, tzinfo=timezone.utc)
    quiet_end = _quiet_hours_end_utc(
        now_utc,
        start_hhmm="23:00",
        end_hhmm="06:00",
        quiet_timezone="-05:00",
    )
    assert quiet_end == datetime(2026, 2, 27, 11, 0, tzinfo=timezone.utc)


def test_quiet_hours_end_is_exclusive() -> None:
    # 11:00 UTC is exactly 06:00 in -05:00.
    now_utc = datetime(2026, 2, 27, 11, 0, tzinfo=timezone.utc)
    quiet_end = _quiet_hours_end_utc(
        now_utc,
        start_hhmm="23:00",
        end_hhmm="06:00",
        quiet_timezone="-05:00",
    )
    assert quiet_end is None
