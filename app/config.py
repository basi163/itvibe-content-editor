from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _clock_minutes(value: str) -> int:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Invalid clock time: {value!r}") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"Invalid clock time: {value!r}")
    return hour * 60 + minute


def _active_windows(name: str, default: str) -> tuple[tuple[int, int], ...]:
    raw_value = os.getenv(name, default).strip()
    windows: list[tuple[int, int]] = []
    for raw_window in raw_value.split(","):
        try:
            start_text, end_text = raw_window.strip().split("-", 1)
        except ValueError as error:
            raise RuntimeError(
                f"{name} must contain comma-separated HH:MM-HH:MM windows"
            ) from error
        start = _clock_minutes(start_text)
        end = _clock_minutes(end_text)
        if start == end:
            raise RuntimeError(f"{name} windows cannot start and end at the same time")
        windows.append((start, end))
    if not windows:
        raise RuntimeError(f"{name} must contain at least one time window")
    return tuple(windows)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    qwen_api_key: str
    llm_base_url: str
    triage_model: str
    writer_model: str
    database_path: Path
    solidot_rss_url: str
    rss_poll_interval_seconds: int
    rss_offpeak_poll_interval_seconds: int
    rss_active_windows: tuple[tuple[int, int], ...]
    rss_schedule_utc_offset_hours: int
    rss_bootstrap_items: int
    max_post_chars: int
    relevance_threshold: float
    http_timeout_seconds: float
    max_source_links: int
    editor_chat_id: int | None
    target_chat_id: int | None

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        settings = cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            qwen_api_key=os.getenv("QWEN_API_KEY", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            triage_model=os.getenv("TRIAGE_MODEL", "qwen3.8-flash").strip(),
            writer_model=os.getenv("WRITER_MODEL", "qwen3.7-plus").strip(),
            database_path=Path(os.getenv("DATABASE_PATH", "data/editor.sqlite3")),
            solidot_rss_url=os.getenv(
                "SOLIDOT_RSS_URL", "https://www.solidot.org/index.rss"
            ).strip(),
            rss_poll_interval_seconds=int(os.getenv("RSS_POLL_INTERVAL_SECONDS", "120")),
            rss_offpeak_poll_interval_seconds=int(
                os.getenv("RSS_OFFPEAK_POLL_INTERVAL_SECONDS", "1800")
            ),
            rss_active_windows=_active_windows(
                "RSS_ACTIVE_WINDOWS", "07:30-15:30,18:00-22:30"
            ),
            rss_schedule_utc_offset_hours=int(
                os.getenv("RSS_SCHEDULE_UTC_OFFSET_HOURS", "4")
            ),
            rss_bootstrap_items=int(os.getenv("RSS_BOOTSTRAP_ITEMS", "1")),
            max_post_chars=int(os.getenv("MAX_POST_CHARS", "2000")),
            relevance_threshold=float(os.getenv("RELEVANCE_THRESHOLD", "0.72")),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "25")),
            max_source_links=int(os.getenv("MAX_SOURCE_LINKS", "6")),
            editor_chat_id=_optional_int("EDITOR_CHAT_ID"),
            target_chat_id=_optional_int("TARGET_CHAT_ID"),
        )
        settings.validate()
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        return settings

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.qwen_api_key:
            missing.append("QWEN_API_KEY")
        if not self.llm_base_url:
            missing.append("LLM_BASE_URL")
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if not 512 <= self.max_post_chars <= 4096:
            raise RuntimeError("MAX_POST_CHARS must be between 512 and 4096")
        if not 0 <= self.relevance_threshold <= 1:
            raise RuntimeError("RELEVANCE_THRESHOLD must be between 0 and 1")
        if self.rss_poll_interval_seconds < 30:
            raise RuntimeError("RSS_POLL_INTERVAL_SECONDS must be at least 30")
        if self.rss_offpeak_poll_interval_seconds < self.rss_poll_interval_seconds:
            raise RuntimeError(
                "RSS_OFFPEAK_POLL_INTERVAL_SECONDS must not be shorter than "
                "RSS_POLL_INTERVAL_SECONDS"
            )
        if not -12 <= self.rss_schedule_utc_offset_hours <= 14:
            raise RuntimeError("RSS_SCHEDULE_UTC_OFFSET_HOURS must be between -12 and 14")
