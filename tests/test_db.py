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


@pytest.mark.asyncio
async def test_first_user_claims_owner(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()

    assert await repository.claim_owner(100)
    assert not await repository.claim_owner(200)
    assert await repository.is_owner(100)
    assert not await repository.is_owner(200)
