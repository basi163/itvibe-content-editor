import json
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
    assert draft["editor_touched_at"] is None

    await repository.update_draft(
        draft_id,
        editor_chat_id=-1001234567890,
        editor_message_id=55,
    )
    by_message = await repository.get_draft_by_editor_message(-1001234567890, 55)
    assert by_message is not None
    assert by_message["id"] == draft_id


@pytest.mark.asyncio
async def test_unpublished_queue_and_navigation_context(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()
    draft_ids: list[int] = []
    for index in range(3):
        draft_ids.append(
            await repository.create_draft(
                origin_type="test",
                origin_ref=f"story-{index}",
                title=f"Заголовок {index}",
                body="Текст",
                hashtags=["#ИИ", "#наука", "#технологии"],
                sources=[],
                media=[],
                material={},
                triage={},
            )
        )
    await repository.update_draft(
        draft_ids[0],
        status="published",
    )
    await repository.update_draft(
        draft_ids[1],
        status="scheduled",
        editor_chat_id=-1001234567890,
        editor_message_id=55,
        editor_touched_at=datetime.now(UTC).isoformat(),
    )

    assert await repository.unpublished_draft_count() == 2
    unpublished = await repository.unpublished_drafts(limit=1, offset=0)
    assert [draft["id"] for draft in unpublished] == [draft_ids[2]]

    await repository.set_navigation_context(77, -1001234567890, draft_ids[1])
    context = await repository.get_navigation_context(77, -1001234567890)

    assert context is not None
    assert context["id"] == draft_ids[1]
    assert context["editor_touched_at"] is not None


@pytest.mark.asyncio
async def test_existing_edited_drafts_are_backfilled_once(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()
    draft_id = await repository.create_draft(
        origin_type="test",
        origin_ref="story",
        title="Заголовок",
        body="Текст",
        hashtags=["#ИИ", "#наука", "#технологии"],
        sources=[],
        media=[],
        material={},
        triage={},
    )
    created_at = datetime(2026, 10, 21, 10, 0, tzinfo=UTC).isoformat()
    updated_at = datetime(2026, 10, 21, 10, 2, tzinfo=UTC).isoformat()
    async with repository.connect() as db:
        await db.execute(
            """
            UPDATE drafts
            SET editor_chat_id = ?, editor_message_id = ?,
                created_at = ?, updated_at = ?, editor_touched_at = NULL
            WHERE id = ?
            """,
            (-1001234567890, 55, created_at, updated_at, draft_id),
        )
        await db.execute(
            "DELETE FROM settings WHERE key = 'editor_touched_backfill_v1'"
        )
        await db.commit()

    await repository.initialize()
    draft = await repository.get_draft(draft_id)

    assert draft is not None
    assert draft["editor_touched_at"] == updated_at


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
    first_at = now + timedelta(minutes=10)
    second_at = now + timedelta(minutes=20)
    await repository.update_draft(
        draft_id,
        status="scheduled",
        target_chat_id=-1001,
        scheduled_at=first_at.isoformat(),
        publications_json=json.dumps(
            [
                {
                    "target_chat_id": -1001,
                    "scheduled_at": first_at.isoformat(),
                    "status": "scheduled",
                    "attempts": 0,
                    "error": None,
                    "target_message_id": None,
                },
                {
                    "target_chat_id": -1002,
                    "scheduled_at": second_at.isoformat(),
                    "status": "scheduled",
                    "attempts": 0,
                    "error": None,
                    "target_message_id": None,
                },
            ]
        ),
    )

    assert await repository.due_scheduled_drafts(now) == []
    due = await repository.due_scheduled_drafts(now + timedelta(minutes=11))
    future = await repository.scheduled_publication_times(now)
    target_future = await repository.scheduled_publication_times(
        now,
        target_chat_id=-1001,
    )
    second_target_future = await repository.scheduled_publication_times(
        now,
        target_chat_id=-1002,
    )

    assert [draft["id"] for draft in due] == [draft_id]
    assert due[0]["publication_target_chat_id"] == -1001
    assert future == [first_at, second_at]
    assert target_future == [first_at]
    assert second_target_future == [second_at]
    assert await repository.next_scheduled_publication() == first_at
    scheduled_drafts = await repository.scheduled_drafts()
    assert [draft["id"] for draft in scheduled_drafts] == [draft_id]
