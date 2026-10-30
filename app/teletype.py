from __future__ import annotations

import html
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.models import GeneratedPost, SourceDocument
from app.renderer import SOURCE_MARKER_RE, normalize_body_spacing

TELETYPE_API_URL = "https://teletype.in/api"
TELETYPE_MEDIA_API_URL = "https://teletype.in/media"
TELETYPE_TARGET_ID_BASE = 9_000_000_000_000_000
TELETYPE_BLOG_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
TELETYPE_TELEGRAM_URL = "https://t.me/itvibenews"


class TeletypeApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TeletypeBlog:
    blog_id: int
    title: str
    uri: str
    can_publish: bool

    @property
    def url(self) -> str:
        return f"https://teletype.in/@{self.uri}"


@dataclass(frozen=True)
class TeletypeImage:
    url: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class TeletypeArticle:
    article_id: int
    uri: str


def teletype_target_id(blog_id: int) -> int:
    if blog_id <= 0:
        raise ValueError("Teletype blog ID must be positive")
    return TELETYPE_TARGET_ID_BASE + blog_id


def teletype_blog_id_from_target(target_id: int) -> int | None:
    blog_id = int(target_id) - TELETYPE_TARGET_ID_BASE
    return blog_id if blog_id > 0 else None


def parse_teletype_blog_name(value: str) -> str:
    cleaned = value.strip().removeprefix("@")
    if "://" in cleaned:
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host != "teletype.in":
            raise ValueError("Нужна ссылка на блог Teletype")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise ValueError("Нужна ссылка на главную страницу блога Teletype")
        cleaned = parts[0].removeprefix("@")
    cleaned = cleaned.strip().strip("/")
    if not TELETYPE_BLOG_RE.fullmatch(cleaned):
        raise ValueError("Не удалось распознать имя блога Teletype")
    return cleaned


def _render_inline_links(value: str, sources: list[SourceDocument]) -> str:
    result: list[str] = []
    position = 0
    for match in SOURCE_MARKER_RE.finditer(value):
        result.append(html.escape(value[position : match.start()]))
        index = int(match.group(1))
        anchor = html.escape(match.group(2).strip())
        if (
            0 <= index < len(sources)
            and sources[index].linkable
            and sources[index].url.startswith(("http://", "https://"))
        ):
            url = html.escape(sources[index].url, quote=True)
            result.append(f'<a href="{url}" unwrapped-href="{url}">{anchor}</a>')
        else:
            result.append(anchor)
        position = match.end()
    result.append(html.escape(value[position:]))
    return "".join(result)


def _anchor() -> str:
    return secrets.token_urlsafe(5)[:6]


def render_teletype_document(
    post: GeneratedPost,
    sources: list[SourceDocument],
    image: TeletypeImage,
) -> str:
    image_url = html.escape(image.url, quote=True)
    dimensions = ""
    if image.width and image.height:
        dimensions = (
            f" width={int(image.width)} height={int(image.height)}"
            f" naturalWidth={int(image.width)} naturalHeight={int(image.height)}"
        )
    blocks = [
        "<document>",
        (
            f'<image src="{image_url}" anchor="{_anchor()}" '
            f'size="original"{dimensions} />'
        ),
    ]
    body = normalize_body_spacing(post.body)
    for paragraph in re.split(r"\n\s*\n", body):
        text = paragraph.strip()
        if not text:
            continue
        blocks.append(
            f'<p anchor="{_anchor()}">{_render_inline_links(text, sources)}</p>'
        )
    hashtags = " ".join(tag.strip() for tag in post.hashtags[:3] if tag.strip())
    if hashtags:
        blocks.append(f'<p anchor="{_anchor()}">{html.escape(hashtags)}</p>')
    telegram_url = html.escape(TELETYPE_TELEGRAM_URL, quote=True)
    blocks.append(
        f'<p anchor="{_anchor()}"><a href="{telegram_url}" '
        f'unwrapped-href="{telegram_url}">IT Vibe News</a></p>'
    )
    blocks.append("</document>")
    return "\n".join(blocks)


def _plain_excerpt(post: GeneratedPost, limit: int) -> str:
    value = SOURCE_MARKER_RE.sub(lambda match: match.group(2).strip(), post.body)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def build_teletype_payload(
    blog_id: int,
    post: GeneratedPost,
    sources: list[SourceDocument],
    image: TeletypeImage,
    *,
    article_uri: str | None = None,
    article_id: int | None = None,
) -> dict[str, Any]:
    excerpt = _plain_excerpt(post, 500)
    payload: dict[str, Any] = {
        "author_id": blog_id,
        "creator_id": blog_id,
        "title": post.title.strip(),
        "text": render_teletype_document(post, sources, image),
        "cut_text": excerpt,
        "cut_image": image.url,
        "sharing_title": None,
        "sharing_text": _plain_excerpt(post, 180),
        "sharing_image": image.url,
        "sharing_type": "image",
        "heading_style": {},
        "visibility": "all",
        "allow_indexing": True,
        "comments_enabled": True,
        "reactions_enabled": True,
        "subscriptions_enabled": True,
        "donations_disabled": True,
        "reposts_enabled": True,
        "related_enabled": True,
        "sharing_enabled": True,
        "topics": [],
        "pin": False,
        "pin_options": {},
        "related_articles": [],
        "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if article_uri:
        payload["uri"] = article_uri
    if article_id is not None:
        payload["id"] = article_id
    return payload


class TeletypePublisher:
    def __init__(
        self,
        access_token: str,
        *,
        client_version: str = "4.41.26",
        timeout: float = 25.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token.strip()
        if not self.access_token:
            raise ValueError("Teletype access token is required")
        self.client_version = client_version.strip() or "4.41.26"
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": self.access_token,
            "X-Client-Version": f"Teletype.Web/{self.client_version}",
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, str] | None = None,
        media: bool = False,
    ) -> Any:
        base_url = TELETYPE_MEDIA_API_URL if media else TELETYPE_API_URL
        response = await self.client.request(
            method,
            f"{base_url}/{path.lstrip('/')}",
            headers=self.headers,
            json=json,
            files=files,
            data=data,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_error:
            detail = ""
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message")
                    or payload.get("error_description")
                    or payload.get("error")
                    or ""
                )
            raise TeletypeApiError(
                f"Teletype API error {response.status_code}"
                + (f": {detail}" if detail else ""),
                status_code=response.status_code,
            )
        if not isinstance(payload, (dict, list)):
            raise TeletypeApiError("Teletype API returned an invalid response")
        if isinstance(payload, dict) and payload.get("error"):
            raise TeletypeApiError(f"Teletype API error: {payload['error']}")
        return payload

    async def resolve_blog(self, value: str) -> TeletypeBlog:
        uri = parse_teletype_blog_name(value)
        payload = await self._request("GET", f"blogs/uri/{quote(uri)}")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("По этой ссылке не найден блог Teletype")
        can_publish = any(
            bool(payload.get(role)) for role in ("owner", "admin", "author")
        )
        return TeletypeBlog(
            blog_id=int(payload["id"]),
            title=str(payload.get("name") or uri),
            uri=str(payload.get("uri") or uri),
            can_publish=can_publish,
        )

    async def upload_image(
        self,
        content: bytes,
        filename: str,
        content_type: str = "image/jpeg",
    ) -> TeletypeImage:
        payload = await self._request(
            "PUT",
            "",
            files={"file": (filename or "cover.jpg", content, content_type)},
            data={"type": "images"},
            media=True,
        )
        if not isinstance(payload, dict) or not str(payload.get("url") or "").startswith(
            ("http://", "https://")
        ):
            raise TeletypeApiError("Teletype did not return an uploaded image URL")
        return TeletypeImage(
            url=str(payload["url"]),
            width=int(payload["width"]) if payload.get("width") else None,
            height=int(payload["height"]) if payload.get("height") else None,
        )

    async def publish_article(
        self,
        *,
        blog_id: int,
        blog_uri: str,
        article_uri: str,
        post: GeneratedPost,
        sources: list[SourceDocument],
        image: TeletypeImage,
        article_id: int | None = None,
    ) -> TeletypeArticle:
        payload = build_teletype_payload(
            blog_id,
            post,
            sources,
            image,
            article_uri=article_uri,
            article_id=article_id,
        )
        if article_id is None:
            try:
                result = await self._request("POST", "articles", json=payload)
            except TeletypeApiError as error:
                if error.status_code != 409:
                    raise
                result = await self._request(
                    "GET",
                    (
                        f"blogs/uri/{quote(blog_uri)}/articles/"
                        f"{quote(article_uri)}"
                    ),
                )
        else:
            result = await self._request(
                "PUT",
                f"articles/{article_id}",
                json=payload,
            )
        if not isinstance(result, dict) or not result.get("id"):
            raise TeletypeApiError("Teletype did not return the published article ID")
        return TeletypeArticle(
            article_id=int(result["id"]),
            uri=str(result.get("uri") or ""),
        )
