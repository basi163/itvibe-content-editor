from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Repository


@pytest.mark.asyncio
async def test_draft_round_trip(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()

    draft_id = await repository.create_draft(
        origin_type="test",
        origin_ref="https://example.org/story",
        title="Заголовок",
        body="Текст",
        hashtags=["#ИИ", "#наука", "#технологии"],
        sources=[{"url": "https://example.org/source"}],
        media=[],
        material={"candidate": {}, "sources": []},
        triage={"relevant": True, "score": 0.9},
    )

    draft = await repository.get_draft(draft_id)

    assert draft is not None
    assert draft["title"] == "Заголовок"
    assert draft["hashtags"] == ["#ИИ", "#наука", "#технологии"]
    assert draft["editor_media_message_id"] is None
    assert draft["target_media_message_id"] is None
    assert draft["scheduled_at"] is None
    assert draft["schedule_attempts"] == 0


@pytest.mark.asyncio
async def test_first_user_claims_owner(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()

    assert await repository.claim_owner(100)
    assert not await repository.claim_owner(200)
    assert await repository.is_owner(100)
    assert not await repository.is_owner(200)


@pytest.mark.asyncio
async def test_failed_feed_item_can_be_marked_for_retry(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()
    item_id = await repository.add_feed_item(
        "solidot",
        "story-1",
        "https://example.org/story",
        "Story",
        None,
        "processing",
        {},
    )
    await repository.update_feed_status(
        item_id,
        "failed",
        "ValueError: invalid generated post",
    )

    state = await repository.get_feed_item_state("solidot", "story-1")
    assert state is not None
    assert state["attempt_count"] == 1
    assert state["status"] == "failed"

    await repository.mark_feed_processing(item_id)
    state = await repository.get_feed_item_state("solidot", "story-1")
    assert state is not None
    assert state["attempt_count"] == 2
    assert state["status"] == "processing"
    assert state["error"] is None


@pytest.mark.asyncio
async def test_pending_manual_edits_can_be_cleared(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()
    draft_id = await repository.create_draft(
        origin_type="test",
        origin_ref="test",
        title="Заголовок",
        body="Текст",
        hashtags=["#ИИ", "#наука", "#технологии"],
        sources=[],
        media=[],
        material={},
        triage={},
    )
    await repository.set_pending_action(100, "title", draft_id)

    await repository.clear_pending_actions()

    assert await repository.pop_pending_action(100) is None


@pytest.mark.asyncio
async def test_scheduled_drafts_are_returned_when_due(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()
    draft_id = await repository.create_draft(
        origin_type="test",
        origin_ref="test",
        title="Заголовок",
        body="Текст",
        hashtags=["#ИИ", "#наука", "#технологии"],
        sources=[],
        media=[],
        material={},
        triage={},
    )
    now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    scheduled_at = now + timedelta(minutes=10)
    await repository.update_draft(
        draft_id,
        status="scheduled",
        scheduled_at=scheduled_at.isoformat(),
    )

    assert await repository.due_scheduled_drafts(now) == []
    due = await repository.due_scheduled_drafts(now + timedelta(minutes=11))
    future = await repository.scheduled_publication_times(now)

    assert [draft["id"] for draft in due] == [draft_id]
    assert future == [scheduled_at]
    assert await repository.next_scheduled_publication() == scheduled_at
    scheduled_drafts = await repository.scheduled_drafts()
    assert [draft["id"] for draft in scheduled_drafts] == [draft_id]
