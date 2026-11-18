from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BufferedInputFile

from app.bot import EditorBot
from app.media_cache import cache_media
from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.pipeline import ContentPipeline


@pytest.mark.asyncio
async def test_draft_image_survives_source_expiry_before_publication(tmp_path: Path) -> None:
    image = b"original image bytes"
    url = "https://cdn.example/news.jpg"
    settings = SimpleNamespace(
        database_path=tmp_path / "editor.sqlite3",
        relevance_threshold=0.7,
        max_post_chars=2000,
    )
    extractor = SimpleNamespace(
        collect_sources=AsyncMock(return_value=[SourceDocument(url="https://example.com/news")]),
        download_image=AsyncMock(return_value=(image, "preview.jpg")),
    )
    repository = SimpleNamespace(create_draft=AsyncMock(return_value=513))
    pipeline = ContentPipeline(
        settings=settings,
        repository=repository,
        extractor=extractor,
        llm=SimpleNamespace(
            triage=AsyncMock(return_value=TriageDecision(True, 0.9, "technology", "relevant")),
            generate=AsyncMock(return_value=GeneratedPost(
                "Новость о технологиях", "Компания обновила продукт.", ["#технологии", "#IT", "#ИИ"]
            )),
        ),
    )

    async def deliver(draft_id: int) -> None:
        assert draft_id == 513
        saved = repository.create_draft.call_args.kwargs["media"][0]
        assert Path(saved["local_path"]).read_bytes() == image

    pipeline.set_delivery(deliver)
    assert await pipeline._process(StoryCandidate(
        "telegram_web", "https://t.me/example/1", "Title", "Text", media_urls=[url]
    )) == 513
    saved = repository.create_draft.call_args.kwargs["media"][0]
    extractor.download_image.reset_mock()
    extractor.download_image.return_value = None

    # A fresh bot process can publish using only the saved draft and its data volume.
    editor = object.__new__(EditorBot)
    editor.settings = settings
    editor.pipeline = SimpleNamespace(extractor=extractor)
    upload = await editor._media_input(saved)
    assert isinstance(upload, BufferedInputFile)
    assert upload.data == image
    extractor.download_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_url_reuses_persistent_cache_without_new_download(tmp_path: Path) -> None:
    media = {"type": "photo", "image_url": "https://cdn.example/temporary.png"}
    download = AsyncMock(return_value=(b"cached bytes", "preview.png"))
    saved = await cache_media(media, tmp_path, download)
    assert saved is not None
    assert "local_path" not in media
    download.reset_mock()
    download.return_value = None
    recovered = await cache_media(dict(media), tmp_path, download)
    assert recovered == saved
    assert Path(recovered["local_path"]).read_bytes() == b"cached bytes"
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_download_never_becomes_a_cached_image(tmp_path: Path) -> None:
    media = {"type": "photo", "image_url": "https://cdn.example/missing.jpg"}
    assert await cache_media(media, tmp_path, AsyncMock(return_value=None)) is None
    assert not (tmp_path / "media").exists()


@pytest.mark.asyncio
async def test_local_file_outside_data_volume_is_not_trusted(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"untrusted")
    saved = await cache_media(
        {"type": "photo", "local_path": str(outside), "image_url": "https://example.com/a.jpg"},
        tmp_path / "data",
        AsyncMock(return_value=(b"downloaded", "preview.jpg")),
    )
    assert saved is not None
    assert Path(saved["local_path"]).read_bytes() == b"downloaded"


@pytest.mark.asyncio
async def test_storage_failure_does_not_leave_partial_image(tmp_path: Path, monkeypatch) -> None:
    def fail_replace(*args: object) -> None:
        raise OSError("Disk full")

    monkeypatch.setattr("app.media_cache.os.replace", fail_replace)
    with pytest.raises(OSError, match="Disk full"):
        await cache_media(
            {"type": "photo", "image_url": "https://example.com/a.jpg"},
            tmp_path,
            AsyncMock(return_value=(b"image", "preview.jpg")),
        )
    assert not list((tmp_path / "media").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,content_type", [
    ("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp"), ("gif", "image/gif"),
])
async def test_teletype_uploads_cached_image_with_correct_type(
    tmp_path: Path, suffix: str, content_type: str,
) -> None:
    path = tmp_path / f"cover.{suffix}"
    path.write_bytes(b"cached image")
    editor = object.__new__(EditorBot)
    editor.settings = SimpleNamespace(database_path=tmp_path / "editor.sqlite3")
    editor.teletype_publisher = SimpleNamespace(upload_image=AsyncMock(return_value="uploaded"))
    media = {"type": "photo", "local_path": str(path)}
    assert await editor._teletype_image({"media": [media]}, media) == "uploaded"
    editor.teletype_publisher.upload_image.assert_awaited_once_with(
        b"cached image", path.name, content_type,
    )
