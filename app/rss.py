from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup


@dataclass
class FeedEntry:
    external_id: str
    title: str
    link: str
    summary: str
    published_at: str | None
    urls: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class FeedSnapshot:
    title: str
    url: str
    entries: list[FeedEntry]


def normalize_rss_url(value: str) -> str:
    cleaned = value.strip()
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Нужна публичная RSS/Atom-ссылка http:// или https://")
    if parsed.username or parsed.password:
        raise ValueError("Ссылки с логином или паролем не поддерживаются")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Для RSS поддерживаются только обычные HTTP/HTTPS-порты")
    host = parsed.hostname.casefold().rstrip(".")
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    return urlunparse(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


async def _ensure_public_url(value: str) -> str:
    normalized = normalize_rss_url(value)
    parsed = urlparse(normalized)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise ValueError("Не удалось найти сервер RSS-ленты") from error
    ips = {ipaddress.ip_address(item[4][0]) for item in addresses}
    if not ips or any(not address.is_global for address in ips):
        raise ValueError("RSS-лента должна быть доступна по публичному адресу")
    return normalized


def _media_urls(item: Any) -> list[str]:
    urls: list[str] = []
    for media in item.get("media_content", []):
        url = str(media.get("url") or "").strip()
        if url:
            urls.append(url)
    for media in item.get("media_thumbnail", []):
        url = str(media.get("url") or "").strip()
        if url:
            urls.append(url)
    for enclosure in item.get("enclosures", []):
        content_type = str(enclosure.get("type") or "").casefold()
        url = str(enclosure.get("href") or enclosure.get("url") or "").strip()
        if url and content_type.startswith(("image/", "video/")):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_feed(content: bytes, source_url: str) -> FeedSnapshot:
    parsed = feedparser.parse(content)
    entries: list[FeedEntry] = []
    for item in parsed.entries:
        link = str(item.get("link", "")).strip()
        title = BeautifulSoup(str(item.get("title", "")), "html.parser").get_text(
            " ", strip=True
        )
        summary_html = str(item.get("summary", item.get("description", "")))
        soup = BeautifulSoup(summary_html, "html.parser")
        summary = soup.get_text(" ", strip=True)
        urls = [anchor.get("href", "") for anchor in soup.find_all("a", href=True)]
        if link:
            urls.insert(0, link)
        external_id = str(item.get("id", item.get("guid", link))).strip()
        if not external_id:
            external_id = hashlib.sha256(f"{title}|{summary}".encode()).hexdigest()
        published_at = item.get("published", item.get("updated"))
        media_urls = _media_urls(item)
        entries.append(
            FeedEntry(
                external_id=external_id,
                title=title or "Материал из RSS",
                link=link or source_url,
                summary=summary,
                published_at=str(published_at) if published_at else None,
                urls=list(dict.fromkeys(url for url in urls if url)),
                raw={
                    "id": external_id,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published_at": published_at,
                    "media_urls": media_urls,
                },
            )
        )
    feed_title = BeautifulSoup(
        str(parsed.feed.get("title", "")), "html.parser"
    ).get_text(" ", strip=True)
    if not entries:
        raise ValueError("По этому адресу не найдено записей RSS/Atom")
    return FeedSnapshot(
        title=feed_title or (urlparse(source_url).hostname or "RSS"),
        url=source_url,
        entries=entries,
    )


class RssFeed:
    max_bytes = 5_000_000

    def __init__(self, url: str, timeout: float) -> None:
        self.url = normalize_rss_url(url)
        self.timeout = timeout

    async def fetch(self) -> FeedSnapshot:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers={"User-Agent": "ITVibeContentEditor/0.1"},
        ) as client:
            current_url = self.url
            for _ in range(6):
                current_url = await _ensure_public_url(current_url)
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            response.raise_for_status()
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ValueError("RSS-лента слишком большая")
                        chunks.append(chunk)
                    return parse_feed(b"".join(chunks), current_url)
            raise ValueError("Слишком много переадресаций RSS-ленты")


class SolidotFeed(RssFeed):
    source_key = "solidot"

    async def fetch(self) -> list[FeedEntry]:
        return (await super().fetch()).entries
