from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.models import GeneratedPost
from app.vk import VkPublisher, parse_vk_group_name, render_vk_crosspost


class FakeCoverService:
    def __init__(self) -> None:
        self.prepared: list[str] = []

    async def prepare(self, article_url: str) -> bytes:
        self.prepared.append(article_url)
        return b"jpeg-cover"


class FailingCoverService:
    async def prepare(self, article_url: str) -> bytes:
        raise RuntimeError(f"Could not download {article_url}")


def test_parse_vk_group_name_accepts_public_vk_urls() -> None:
    assert parse_vk_group_name("https://vk.ru/it_vibe") == "it_vibe"
    assert parse_vk_group_name("https://vk.com/it_vibe/") == "it_vibe"
    assert parse_vk_group_name("@it_vibe") == "it_vibe"


def test_vk_crosspost_renderer_points_to_teletype_article() -> None:
    article_url = "https://teletype.in/@itvibe/release"
    text = render_vk_crosspost(
        GeneratedPost(
            "Новый релиз",
            "Компания опубликовала [[source:0|отчёт]].",
            ["#IT", "#разработка", "#новости", "#лишний"],
        ),
        article_url,
    )

    assert text.startswith("Новый релиз\n\nКомпания опубликовала")
    assert "[[source:" not in text
    assert "опубликовала отчёт" in text
    assert "#IT #разработка #новости" in text
    assert "#лишний" not in text
    assert f"Читать статью в Teletype → {article_url}" in text


@pytest.mark.asyncio
async def test_vk_publisher_verifies_group_and_posts_without_cover_service() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = parse_qs(request.content.decode())
        assert body["access_token"] == ["community-token"]
        assert body["v"] == ["5.199"]
        if request.url.path.endswith("/groups.getTokenPermissions"):
            payload = {
                "response": {
                    "permissions": [{"name": "wall", "setting": 8192}],
                }
            }
        elif request.url.path.endswith("/groups.getById"):
            assert body["group_ids"] == ["42"]
            payload = {
                "response": {
                    "groups": [
                        {"id": 42, "name": "IT Vibe", "screen_name": "it_vibe"}
                    ]
                }
            }
        elif request.url.path.endswith("/wall.post"):
            assert body["owner_id"] == ["-42"]
            assert body["from_group"] == ["1"]
            assert "attachments" not in body
            assert body["guid"] == ["itvibe-test"]
            payload = {"response": {"post_id": 99}}
        else:  # pragma: no cover
            raise AssertionError(f"Unexpected VK request: {request.url}")
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher("community-token", 42, client=client)
    try:
        group = await publisher.resolve_group("https://vk.ru/it_vibe")
        post_id = await publisher.publish_article(
            group_id=group.group_id,
            message="Проверочный пост",
            article_url="https://teletype.in/@itvibe/release",
            guid="itvibe-test",
            fallback_title="Новый релиз",
        )
    finally:
        await client.aclose()

    assert group.title == "IT Vibe"
    assert group.url == "https://vk.ru/it_vibe"
    assert post_id == 99
    assert publisher.post_url(post_id) == "https://vk.ru/wall-42_99"
    assert calls == [
        "/method/groups.getTokenPermissions",
        "/method/groups.getById",
        "/method/wall.post",
    ]


@pytest.mark.asyncio
async def test_vk_publisher_posts_without_photo_when_cover_download_fails() -> None:
    wall_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal wall_calls
        body = parse_qs(request.content.decode())
        wall_calls += 1
        assert body["guid"] == ["itvibe-preview-fallback"]
        assert body["message"] == ["Проверочный пост"]
        assert "attachments" not in body
        payload = {"response": {"post_id": 100}}
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher(
        "community-token",
        42,
        client=client,
        cover_service=FailingCoverService(),  # type: ignore[arg-type]
    )
    try:
        post_id = await publisher.publish_article(
            group_id=42,
            message="Проверочный пост",
            article_url="https://teletype.in/@itvibe/release",
            guid="itvibe-preview-fallback",
            fallback_title="Новый релиз",
        )
    finally:
        await client.aclose()

    assert post_id == 100
    assert wall_calls == 1


@pytest.mark.asyncio
async def test_vk_publisher_uploads_message_photo_and_attaches_it_to_wall() -> None:
    cover = FakeCoverService()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "upload.vk.test":
            assert b'filename="cover.jpg"' in request.content
            assert b"jpeg-cover" in request.content
            payload = {"server": 123, "photo": "photo-payload", "hash": "hash-value"}
        else:
            body = parse_qs(request.content.decode())
            if request.url.path.endswith("/groups.setSettings"):
                assert body["group_id"] == ["42"]
                assert body["messages"] == ["1"]
                payload = {"response": 1}
            elif request.url.path.endswith("/photos.getMessagesUploadServer"):
                assert "peer_id" not in body
                payload = {
                    "response": {"upload_url": "https://upload.vk.test/photo"}
                }
            elif request.url.path.endswith("/photos.saveMessagesPhoto"):
                assert body["server"] == ["123"]
                assert body["photo"] == ["photo-payload"]
                assert body["hash"] == ["hash-value"]
                payload = {
                    "response": [
                        {"owner_id": -42, "id": 777, "access_key": "photo-key"}
                    ]
                }
            elif request.url.path.endswith("/wall.post"):
                assert body["attachments"] == ["photo-42_777_photo-key"]
                payload = {"response": {"post_id": 101}}
            else:  # pragma: no cover
                raise AssertionError(f"Unexpected VK request: {request.url}")
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher(
        "community-token",
        42,
        client=client,
        cover_service=cover,  # type: ignore[arg-type]
    )
    try:
        post_id = await publisher.publish_article(
            group_id=42,
            message="Проверочный пост",
            article_url="https://teletype.in/@itvibe/release",
            guid="itvibe-preview-test",
            fallback_title="Новый релиз",
        )
    finally:
        await client.aclose()

    assert post_id == 101
    assert cover.prepared == ["https://teletype.in/@itvibe/release"]
    assert calls == [
        "api.vk.com/method/groups.setSettings",
        "api.vk.com/method/photos.getMessagesUploadServer",
        "upload.vk.test/photo",
        "api.vk.com/method/photos.saveMessagesPhoto",
        "api.vk.com/method/wall.post",
    ]
