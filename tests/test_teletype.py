from __future__ import annotations

import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.models import GeneratedPost, SourceDocument
from app.teletype import (
    TELETYPE_TARGET_ID_BASE,
    TeletypeImage,
    TeletypePublisher,
    build_teletype_payload,
    build_teletype_sharing_image,
    parse_teletype_blog_name,
    teletype_blog_id_from_target,
    teletype_target_id,
)


def test_parse_teletype_blog_name_accepts_public_urls() -> None:
    assert parse_teletype_blog_name("https://teletype.in/@itvibe") == "itvibe"
    assert parse_teletype_blog_name("@itvibe") == "itvibe"
    assert parse_teletype_blog_name("itvibe") == "itvibe"


def test_teletype_target_id_round_trip() -> None:
    target_id = teletype_target_id(1_931_042)

    assert target_id == TELETYPE_TARGET_ID_BASE + 1_931_042
    assert teletype_blog_id_from_target(target_id) == 1_931_042


def test_teletype_payload_contains_cover_links_and_three_hashtags() -> None:
    payload = build_teletype_payload(
        1_931_042,
        GeneratedPost(
            "Новый релиз",
            "Компания опубликовала [[source:0|официальный отчёт]].",
            ["#IT", "#разработка", "#новости", "#лишний"],
        ),
        [
            SourceDocument(
                url="https://example.com/report",
                title="Report",
                linkable=True,
            )
        ],
        TeletypeImage("https://example.com/cover.jpg", 900, 1600),
        sharing_image=TeletypeImage(
            "https://example.com/sharing-cover.jpg",
            1200,
            630,
        ),
        article_uri="itvibe-42",
    )

    assert payload["title"] == "Новый релиз"
    assert payload["uri"] == "itvibe-42"
    assert '<image src="https://example.com/cover.jpg"' in payload["text"]
    assert 'href="https://example.com/report"' in payload["text"]
    assert '>IT Vibe News</a>' in payload["text"]
    assert 'href="https://t.me/itvibenews"' in payload["text"]
    assert "#IT #разработка #новости" in payload["text"]
    assert "#лишний" not in payload["text"]
    assert payload["cut_image"] == "https://example.com/cover.jpg"
    assert payload["sharing_title"] == "Новый релиз"
    assert payload["sharing_image"] == "https://example.com/sharing-cover.jpg"
    assert payload["sharing_type"] == "image"


def test_build_teletype_sharing_image_makes_landscape_png() -> None:
    source_buffer = BytesIO()
    Image.new("RGB", (600, 1200), color=(220, 40, 40)).save(
        source_buffer,
        format="PNG",
    )

    result = build_teletype_sharing_image(source_buffer.getvalue())

    with Image.open(BytesIO(result)) as rendered:
        assert rendered.format == "PNG"
        assert rendered.size == (1200, 630)


@pytest.mark.asyncio
async def test_teletype_publisher_resolves_blog_uploads_and_publishes() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "test-token"
        assert request.headers["x-client-version"] == "Teletype.Web/4.41.26"
        if request.url.path == "/api/blogs/uri/itvibe":
            payload = {
                "id": 1_931_042,
                "uri": "itvibe",
                "name": "IT Vibe",
                "author": True,
            }
        elif request.url.path.rstrip("/") == "/media":
            assert b'name="type"' in request.content
            assert b"images" in request.content
            assert b"image-bytes" in request.content
            payload = {
                "url": "https://img1.teletype.in/files/cover.jpg",
                "width": 1200,
                "height": 630,
            }
        elif request.url.path == "/api/articles":
            body = json.loads(request.content)
            assert body["author_id"] == 1_931_042
            assert body["uri"] == "itvibe-42"
            assert body["title"] == "Проверочная статья"
            assert body["sharing_title"] == "Проверочная статья"
            assert (
                body["sharing_image"]
                == "https://img1.teletype.in/files/sharing-cover.jpg"
            )
            assert "https://img1.teletype.in/files/cover.jpg" in body["text"]
            payload = {"id": 4_831_645, "uri": "test-article"}
        else:  # pragma: no cover
            raise AssertionError(f"Unexpected Teletype request: {request.url}")
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = TeletypePublisher("test-token", client=client)
    try:
        blog = await publisher.resolve_blog("https://teletype.in/@itvibe")
        image = await publisher.upload_image(
            b"image-bytes",
            "cover.jpg",
        )
        article = await publisher.publish_article(
            blog_id=blog.blog_id,
            blog_uri=blog.uri,
            article_uri="itvibe-42",
            post=GeneratedPost(
                "Проверочная статья",
                "Текст материала.",
                ["#IT", "#Teletype", "#новости"],
            ),
            sources=[],
            image=image,
            sharing_image=TeletypeImage(
                "https://img1.teletype.in/files/sharing-cover.jpg",
                1200,
                630,
            ),
        )
    finally:
        await client.aclose()

    assert blog.can_publish is True
    assert blog.url == "https://teletype.in/@itvibe"
    assert article.article_id == 4_831_645
    assert article.url == "https://teletype.in/@itvibe/test-article"
    assert calls == [
        ("GET", "/api/blogs/uri/itvibe"),
        ("PUT", "/media/"),
        ("POST", "/api/articles"),
    ]


@pytest.mark.asyncio
async def test_teletype_publisher_rejects_blog_without_author_access() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": 1_931_042, "uri": "itvibe", "name": "IT Vibe"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = TeletypePublisher("test-token", client=client)
    try:
        blog = await publisher.resolve_blog("@itvibe")
    finally:
        await client.aclose()

    assert blog.can_publish is False
