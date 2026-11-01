from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.models import GeneratedPost
from app.vk import VkPublisher, parse_vk_group_name, render_vk_crosspost


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

    assert not text.startswith("Новый релиз\n\n")
    assert text.startswith("Компания опубликовала")
    assert "[[source:" not in text
    assert "опубликовала отчёт" in text
    assert "#IT #разработка #новости" in text
    assert "#лишний" not in text
    assert f"Читать статью в Teletype → {article_url}" in text


@pytest.mark.asyncio
async def test_vk_publisher_verifies_group_and_posts_teletype_link() -> None:
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
            assert body["attachments"] == ["https://teletype.in/@itvibe/release"]
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
async def test_vk_publisher_retries_without_rejected_link_preview() -> None:
    wall_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal wall_calls
        body = parse_qs(request.content.decode())
        wall_calls += 1
        assert body["guid"] == ["itvibe-preview-fallback"]
        if wall_calls == 1:
            assert body["message"] == ["Проверочный пост"]
            assert body["attachments"] == ["https://teletype.in/@itvibe/release"]
            payload = {
                "error": {
                    "error_code": 100,
                    "error_msg": (
                        "One of the parameters specified was missing or invalid: "
                        "Violated: link_photo_sizing_rule. No photo given"
                    ),
                }
            }
        else:
            assert body["message"] == ["Новый релиз\n\nПроверочный пост"]
            assert "attachments" not in body
            payload = {"response": {"post_id": 100}}
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = VkPublisher("community-token", 42, client=client)
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
    assert wall_calls == 2
