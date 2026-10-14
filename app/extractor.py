from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from app.models import SourceDocument

TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "yclid", "mc_")


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith(TRACKING_PREFIXES)
    ]
    cleaned = parsed._replace(fragment="", query=urlencode(query))
    return urlunparse(cleaned)


async def _is_public_host(hostname: str) -> bool:
    try:
        direct_ip = ipaddress.ip_address(hostname)
        addresses = [direct_ip]
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        except OSError:
            return False
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses:
        return False
    return all(
        not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
        for address in addresses
    )


class ArticleExtractor:
    def __init__(self, timeout: float, max_source_links: int) -> None:
        self.timeout = timeout
        self.max_source_links = max_source_links
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/126 Safari/537.36 ITVibeContentEditor/0.1"
                )
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch_document(self, url: str) -> SourceDocument:
        normalized = normalize_url(url)
        if not normalized:
            return SourceDocument(url=url, error="unsupported_url")
        parsed = urlparse(normalized)
        if not parsed.hostname or not await _is_public_host(parsed.hostname):
            return SourceDocument(url=normalized, error="non_public_host")

        try:
            async with self.client.stream("GET", normalized) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
                    return SourceDocument(url=str(response.url), error="unsupported_content_type")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 5_000_000:
                        return SourceDocument(url=str(response.url), error="document_too_large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                final_url = str(response.url)
                encoding = response.encoding or "utf-8"
        except (httpx.HTTPError, OSError) as exc:
            return SourceDocument(url=normalized, error=type(exc).__name__)

        html_text = raw.decode(encoding, errors="replace")
        soup = BeautifulSoup(html_text, "html.parser")
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = str(og_title["content"]).strip()
        og_image_tag = soup.find("meta", attrs={"property": "og:image"})
        og_image = None
        if og_image_tag and og_image_tag.get("content"):
            og_image = urljoin(final_url, str(og_image_tag["content"]).strip())

        text = trafilatura.extract(
            html_text,
            url=final_url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        ) or soup.get_text(" ", strip=True)
        text = text[:40_000]

        links: list[str] = []
        main_content = soup.find(["article", "main"]) or soup.find(
            attrs={
                "class": lambda value: value
                and any(
                    token in " ".join(value if isinstance(value, list) else [value]).casefold()
                    for token in ("article", "story", "content", "p_main")
                )
            }
        )
        anchors = main_content.find_all("a", href=True) if main_content else soup.find_all("a", href=True)
        for anchor in anchors:
            candidate = normalize_url(urljoin(final_url, str(anchor["href"])))
            if candidate and candidate not in links:
                links.append(candidate)

        return SourceDocument(
            url=final_url,
            title=title,
            text=text,
            publisher=urlparse(final_url).netloc.removeprefix("www."),
            og_image=og_image,
            links=links,
        )

    async def collect_sources(self, seed_urls: list[str]) -> list[SourceDocument]:
        seeds = list(dict.fromkeys(filter(None, (normalize_url(url) for url in seed_urls))))
        if not seeds:
            return []

        first = await self.fetch_document(seeds[0])
        documents = [first]
        first_host = urlparse(first.url).netloc.casefold()
        discovered = seeds[1:]
        for link in first.links:
            if urlparse(link).netloc.casefold() != first_host:
                discovered.append(link)
        discovered = list(dict.fromkeys(discovered))[: self.max_source_links]

        semaphore = asyncio.Semaphore(3)

        async def limited_fetch(url: str) -> SourceDocument:
            async with semaphore:
                return await self.fetch_document(url)

        if discovered:
            documents.extend(await asyncio.gather(*(limited_fetch(url) for url in discovered)))
        return [document for document in documents if document.url]
