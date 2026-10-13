from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

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


class SolidotFeed:
    source_key = "solidot"

    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout

    async def fetch(self) -> list[FeedEntry]:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "ITVibeContentEditor/0.1"},
        ) as client:
            response = await client.get(self.url)
            response.raise_for_status()

        parsed = feedparser.parse(response.content)
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
            entries.append(
                FeedEntry(
                    external_id=external_id,
                    title=title,
                    link=link,
                    summary=summary,
                    published_at=str(published_at) if published_at else None,
                    urls=list(dict.fromkeys(url for url in urls if url)),
                    raw={
                        "id": external_id,
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "published_at": published_at,
                    },
                )
            )
        return entries
