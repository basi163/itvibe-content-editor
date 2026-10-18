from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from app.models import SourceDocument

TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "yclid", "mc_")
BARE_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
SOLIDOT_HOSTS = {"solidot.org", "www.solidot.org"}
SECONDARY_SOURCE_HOSTS = {
    "arstechnica.com",
    "bbc.com",
    "bloomberg.com",
    "cnn.com",
    "engadget.com",
    "forbes.com",
    "lwn.net",
    "neowin.net",
    "reuters.com",
    "techcrunch.com",
    "techradar.com",
    "theguardian.com",
    "theregister.com",
    "theverge.com",
    "wired.com",
    "yahoo.com",
    "zdnet.com",
}
IGNORED_DISCOVERY_HOSTS = {
    "facebook.com",
    "linkedin.com",
    "reddit.com",
    "share.flipboard.com",
    "t.me",
    "x.com",
}


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except (UnicodeError, ValueError):
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith(TRACKING_PREFIXES)
    ]
    cleaned = parsed._replace(fragment="", query=urlencode(query))
    return urlunparse(cleaned)


def is_solidot_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return host in SOLIDOT_HOSTS or host.endswith(".solidot.org")


def is_known_secondary_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in SECONDARY_SOURCE_HOSTS)


def extract_bare_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in BARE_URL_RE.findall(text):
        candidate = normalize_url(match.rstrip(".,;:!?)\u3002，；：！？）"))
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _is_ignored_discovery_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    return any(host == ignored or host.endswith(f".{ignored}") for ignored in IGNORED_DISCOVERY_HOSTS)


def _discovery_priority(url: str) -> tuple[int, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    official_hints = (
        ".gov",
        ".government",
        "europa.eu",
        "overheid.nl",
        "tweedekamer.nl",
        "adviescollegeicttoetsing.nl",
    )
    research_hints = ("/research", "/report", "/publication", "/document", ".pdf")
    if any(hint in host for hint in official_hints):
        return (0, url)
    if any(hint in path for hint in research_hints):
        return (1, url)
    if is_known_secondary_url(url):
        return (3, url)
    return (2, url)


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

    async def download_image(self, url: str) -> tuple[bytes, str] | None:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        if not normalized or not parsed.hostname or not await _is_public_host(parsed.hostname):
            return None
        try:
            async with self.client.stream("GET", normalized) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
                extensions = {
                    "image/jpeg": "jpg",
                    "image/png": "png",
                    "image/webp": "webp",
                    "image/gif": "gif",
                }
                extension = extensions.get(content_type)
                if not extension:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 10_000_000:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks), f"preview.{extension}"
        except (httpx.HTTPError, OSError):
            return None

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
            candidate_image = normalize_url(
                urljoin(final_url, str(og_image_tag["content"]).strip())
            )
            image_host = urlparse(candidate_image).hostname
            if candidate_image and image_host and await _is_public_host(image_host):
                og_image = candidate_image

        text = trafilatura.extract(
            html_text,
            url=final_url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        ) or soup.get_text(" ", strip=True)
        text = text[:40_000]

        links: list[str] = []
        main_content = soup.select_one(
            "article, main, .p_mainnew, .p_main, .article-content, .story-content"
        ) or soup.find(
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
        content_text = main_content.get_text(" ", strip=True) if main_content else html_text
        for candidate in extract_bare_urls(content_text):
            if candidate not in links:
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
        visited = {normalize_url(first.url)}

        def external_links(document: SourceDocument) -> list[str]:
            host = urlparse(document.url).netloc.casefold()
            return [
                link
                for link in document.links
                if urlparse(link).netloc.casefold() != host
                and not _is_ignored_discovery_url(link)
            ]

        discovered = [*seeds[1:], *external_links(first)]
        discovered = sorted(dict.fromkeys(discovered), key=_discovery_priority)

        semaphore = asyncio.Semaphore(3)

        async def limited_fetch(url: str) -> SourceDocument:
            async with semaphore:
                return await self.fetch_document(url)

        first_wave = [url for url in discovered if url not in visited][: self.max_source_links]
        if first_wave:
            wave_documents = await asyncio.gather(*(limited_fetch(url) for url in first_wave))
            documents.extend(wave_documents)
            visited.update(first_wave)

            remaining = self.max_source_links - len(first_wave)
            if remaining > 0:
                second_wave: list[str] = []
                for document in wave_documents:
                    second_wave.extend(external_links(document))
                second_wave = sorted(dict.fromkeys(second_wave), key=_discovery_priority)
                second_wave = [url for url in second_wave if url not in visited][:remaining]
                if second_wave:
                    documents.extend(
                        await asyncio.gather(*(limited_fetch(url) for url in second_wave))
                    )
        return [document for document in documents if document.url]
