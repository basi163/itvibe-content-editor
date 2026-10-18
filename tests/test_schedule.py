from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.main import _retryable_feed_item, rss_poll_delay_seconds


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        rss_poll_interval_seconds=120,
        rss_offpeak_poll_interval_seconds=1800,
        rss_active_windows=((7 * 60 + 30, 15 * 60 + 30), (18 * 60, 22 * 60 + 30)),
        rss_schedule_utc_offset_hours=4,
    )


def test_active_morning_window_uses_two_minutes() -> None:
    now = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)
    assert rss_poll_delay_seconds(_settings(), now) == 120


def test_active_evening_window_uses_two_minutes() -> None:
    now = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
    assert rss_poll_delay_seconds(_settings(), now) == 120


def test_offpeak_uses_thirty_minutes() -> None:
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert rss_poll_delay_seconds(_settings(), now) == 1800


def test_offpeak_wait_does_not_cross_active_window_start() -> None:
    now = datetime(2026, 10, 16, 3, 20, tzinfo=UTC)
    assert rss_poll_delay_seconds(_settings(), now) == 600


def test_active_window_end_is_exclusive() -> None:
    now = datetime(2026, 10, 16, 18, 30, tzinfo=UTC)
    assert rss_poll_delay_seconds(_settings(), now) == 1800


def test_validation_failure_is_retried_after_cooldown() -> None:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    state = {
        "status": "failed",
        "error": "ValueError: Пост содержит двоеточие",
        "attempt_count": 1,
        "updated_at": (now - timedelta(minutes=31)).isoformat(),
    }

    assert _retryable_feed_item(state, now)


def test_validation_failure_retry_is_bounded() -> None:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    state = {
        "status": "failed",
        "error": "ValueError: Пост содержит двоеточие",
        "attempt_count": 3,
        "updated_at": (now - timedelta(hours=1)).isoformat(),
    }

    assert not _retryable_feed_item(state, now)
