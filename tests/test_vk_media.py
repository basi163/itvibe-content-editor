from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from app.vk_media import TeletypeCoverService


@pytest.mark.asyncio
async def test_cover_service_downloads_and_normalizes_teletype_image() -> None:
    source = io.BytesIO()
    Image.new("RGB", (640, 360), "navy").save(source, format="PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/@itvibe/article":
            return httpx.Response(
                200,
                text=(
                    '<meta property="og:title" content="Заголовок">'
                    '<meta property="og:image" content="/images/cover.webp">'
                ),
                request=request,
            )
        assert request.url.path == "/images/cover.webp"
        assert request.headers["referer"] == "https://teletype.in/@itvibe/article"
        return httpx.Response(200, content=source.getvalue(), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = TeletypeCoverService(client=client)
    try:
        image_bytes = await service.prepare(
            "https://teletype.in/@itvibe/article",
        )
    finally:
        await client.aclose()

    with Image.open(io.BytesIO(image_bytes)) as prepared:
        assert prepared.format == "JPEG"
        assert prepared.size == (1200, 630)
