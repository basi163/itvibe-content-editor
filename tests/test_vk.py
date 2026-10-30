from __future__ import annotations

import httpx
import pytest

from app.models import GeneratedPost, SourceDocument
from app.vk import (
    VK_TARGET_ID_BASE,
    VkPublisher,
    parse_vk_group_name,
    render_vk_post,
    vk_group_id_from_target,
    vk_target_id,
)


def test_parse_vk_group_name_accepts_public_vk_urls() -> None:
    assert parse_vk_group_name("https://vk.ru/it_vibe") == "it_vibe"
    assert parse_vk_group_name("https://vk.com/it_vibe/") == "it_vibe"
    assert parse_vk_group_name("@it_vibe") == "it_vibe"


def test_vk_target_id_round_trip() -> None:
    target_id = vk_target_id(123456)

    assert target_id == VK_TARGET_ID_BASE + 123456
    assert vk_group_id_from_target(target_id) == 123456


def test_vk_renderer_expands_official_source_links() -> None:
    text = render_vk_post(
        GeneratedPost(
            "Новый релиз",
            "Компания опубликовала [[source:0|официальный отчёт]].",
            ["#IT", "#разработка", "#новости"],
        ),
        [
            SourceDocument(
                url="https://example.com/report",
                title="Report",
                linkable=True,
            )
        ],
    )

    assert text.startswith("▰ НОВЫЙ РЕЛИЗ\n\n")
    assert "официальный отчёт — https://example.com/report" in text
    assert "#IT #разработка #новости" in text
    assert text.endswith("[https://t.me/+u5H6mukl7zg4NzZi|IT Vibe News]")


@pytest.mark.asyncio
async def test_vk_publisher_resolves_group_and_publishes_photo() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/utils.resolveScreenName"):
            payload = {"response": {"type": "group", "object_id": 42}}
        elif request.url.path.endswith("/groups.getById"):
            payload = {
                "response": {
                    "groups": [
                        {
                            "id": 42,
                            "name": "IT Vibe",
                            "screen_name": "it_vibe",
                            "is_admin": 1,
                        }
                    ]
                }
            }
        elif request.url.path.endswith("/photos.getWallUploadServer"):
            payload = {"response": {"upload_url": "https://upload.vk.test/photo"}}
        elif request.url.host == "upload.vk.test":
            payload = {"photo": "[]", "server": 123, "hash": "upload-hash"}
        elif request.url.path.endswith("/photos.saveWallPhoto"):
            payload = {"response": [{"owner_id": -42, "id": 7}]}
        elif request.url.path.endswith("/wall.post"):
            payload = {"response": {"post_id": 99}}
        else:  # pragma: no cover - useful failure for an unexpected API call
            raise AssertionError(f"Unexpected VK request: {request.url}")
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher("test-token", client=client)
    try:
        group = await publisher.resolve_group("https://vk.ru/it_vibe")
        post_id = await publisher.publish_photo(
            group_id=group.group_id,
            message="Проверочный пост",
            content=b"image-bytes",
            filename="cover.jpg",
            guid="itvibe-test-42",
        )
    finally:
        await client.aclose()

    assert group.is_admin is True
    assert group.title == "IT Vibe"
    assert post_id == 99
    assert calls == [
        "/method/utils.resolveScreenName",
        "/method/groups.getById",
        "/method/photos.getWallUploadServer",
        "/photo",
        "/method/photos.saveWallPhoto",
        "/method/wall.post",
    ]


@pytest.mark.asyncio
async def test_vk_community_token_publishes_link_preview_without_photo_upload() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/groups.getTokenPermissions"):
            payload = {
                "response": {
                    "mask": 8192,
                    "permissions": [{"name": "wall", "setting": 8192}],
                }
            }
        elif request.url.path.endswith("/groups.getById"):
            payload = {
                "response": {
                    "groups": [
                        {"id": 42, "name": "IT Vibe", "screen_name": "it_vibe"}
                    ]
                }
            }
        elif request.url.path.endswith("/wall.post"):
            body = request.content.decode()
            assert "https%3A%2F%2Fexample.com%2Fofficial" in body
            payload = {"response": {"post_id": 100}}
        else:  # pragma: no cover
            raise AssertionError(f"Unexpected VK request: {request.url}")
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher(
        community_access_token="community-token",
        community_group_id=42,
        client=client,
    )
    try:
        group = await publisher.resolve_group("https://vk.ru/it_vibe")
        post_id = await publisher.publish_photo(
            group_id=group.group_id,
            message="Проверочный пост",
            content=b"",
            filename="",
            guid="itvibe-community-42",
            fallback_url="https://example.com/official",
        )
    finally:
        await client.aclose()

    assert publisher.requires_photo_upload is False
    assert group.is_admin is True
    assert post_id == 100
    assert calls == [
        "/method/groups.getTokenPermissions",
        "/method/groups.getById",
        "/method/wall.post",
    ]


@pytest.mark.asyncio
async def test_vk_publisher_edits_existing_community_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/wall.edit")
        body = request.content.decode()
        assert "post_id=100" in body
        assert "https%3A%2F%2Fexample.com%2Fimage.jpg" in body
        return httpx.Response(200, json={"response": 1}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher(
        community_access_token="community-token",
        community_group_id=42,
        client=client,
    )
    try:
        await publisher.edit_post(
            group_id=42,
            post_id=100,
            message="▰ ПРОВЕРОЧНЫЙ ПОСТ",
            attachment_url="https://example.com/image.jpg",
        )
    finally:
        await client.aclose()
