from __future__ import annotations

import io
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageOps


class VkMediaError(RuntimeError):
    pass


class TeletypeCoverService:
    """Download and normalize a Teletype Open Graph cover for VK."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "ITVibe-VK-Crossposter/1.0"},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def prepare(self, article_url: str) -> bytes:
        parsed = urlparse(article_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "teletype.in":
            raise VkMediaError("Only public Teletype articles can provide VK covers")

        page = await self.client.get(article_url)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        image_value = self._meta(soup, "og:image")
        if not image_value:
            raise VkMediaError("Teletype article has no Open Graph image")
        image_url = urljoin(article_url, image_value)

        image_response = await self.client.get(
            image_url,
            headers={"Referer": article_url},
        )
        image_response.raise_for_status()
        try:
            with Image.open(io.BytesIO(image_response.content)) as source:
                prepared = ImageOps.contain(source.convert("RGB"), (1200, 630))
                canvas = Image.new("RGB", (1200, 630), "white")
                offset = (
                    (canvas.width - prepared.width) // 2,
                    (canvas.height - prepared.height) // 2,
                )
                canvas.paste(prepared, offset)
                output = io.BytesIO()
                canvas.save(output, format="JPEG", quality=91, optimize=True)
                return output.getvalue()
        except (OSError, ValueError) as error:
            raise VkMediaError("Teletype preview image is invalid") from error

    @staticmethod
    def _meta(soup: BeautifulSoup, property_name: str) -> str:
        tag = soup.find("meta", attrs={"property": property_name})
        return str(tag.get("content") or "").strip() if tag else ""
