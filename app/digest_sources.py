from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from app.extractor import is_solidot_url, is_telegram_url, normalize_url
from app.models import SourceDocument, StoryCandidate

logger = logging.getLogger(__name__)
DIGEST_SOURCE_POLICY_VERSION = 1
REPOST_HOSTS = {
    "ithome.com", "ithome.com.tw", "36kr.com", "nbd.com.cn", "stcn.com",
    "solidot.org", "cnbeta.com", "cnbeta.com.tw", "ithome.cn", "mydrivers.com",
    "news.sina.com.cn", "finance.sina.com.cn", "tech.sina.com.cn", "sohu.com",
    "news.163.com", "tech.163.com", "news.qq.com", "tech.qq.com", "toutiao.com",
}
SOCIAL_HOSTS = {"weibo.com", "weibo.cn", "facebook.com", "reddit.com", "x.com"}
PRIMARY_KINDS = {"company", "government", "research", "project", "original_reporting"}


def usable_source_url(url: str) -> bool:
    url = normalize_url(url)
    if not url or is_solidot_url(url) or is_telegram_url(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if any(host == domain or host.endswith("." + domain)
           for domain in REPOST_HOSTS | SOCIAL_HOSTS):
        return False
    return parsed.path not in ("", "/")


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _language(selection: dict[str, Any], document: SourceDocument) -> str:
    chinese = len(re.findall(r"[\u3400-\u9fff]", document.text))
    if chinese > max(40, len(document.text) // 5):
        return "zh"
    return str(selection.get("language") or "").lower()


def approved_source(selection: dict[str, Any], document: SourceDocument) -> bool:
    """A search suggestion alone is never enough to approve a publication link."""
    if document.error or not document.text or not usable_source_url(document.url):
        return False
    if selection.get("is_primary") is not True or selection.get("supports_item") is not True:
        return False
    kind = selection.get("kind")
    language = _language(selection, document)
    if kind not in PRIMARY_KINDS or language not in {"en", "zh", "other"}:
        return False
    if language == "zh" and kind not in {"company", "government"}:
        return False
    evidence = _normalized(str(selection.get("evidence") or ""))
    return 16 <= len(evidence) <= 400 and evidence in _normalized(document.text)


async def resolve_digest_sources(
    candidate: StoryCandidate,
    seeds: list[SourceDocument],
    extractor: Any,
    llm: Any,
) -> tuple[list[SourceDocument], dict[str, Any]]:
    resolved = [replace(source, linkable=False) for source in seeds]
    audit: dict[str, Any] = {
        "policy_version": DIGEST_SOURCE_POLICY_VERSION,
        "selected": [],
        "unresolved": list(range(len(seeds))),
    }
    try:
        discovered = await llm.discover_digest_sources(candidate, seeds)
        by_item: dict[int, list[str]] = {}
        for item in discovered.get("items", []):
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if type(index) is not int or not 0 <= index < len(seeds):
                continue
            urls = item.get("urls", [])
            if isinstance(urls, list):
                by_item.setdefault(index, []).extend(
                    normalize_url(url) for url in urls[:3]
                    if isinstance(url, str) and usable_source_url(url)
                )

        semaphore = asyncio.Semaphore(3)
        fetches: dict[str, asyncio.Task[SourceDocument]] = {}

        async def fetch(url: str) -> SourceDocument:
            async with semaphore:
                return await extractor.fetch_document(url)

        async def options(index: int, seed: SourceDocument) -> list[SourceDocument]:
            urls = list(dict.fromkeys([
                *by_item.get(index, []),
                *([normalize_url(seed.url)] if usable_source_url(seed.url) else []),
            ]))[:4]
            documents = []
            for url in urls:
                if url == normalize_url(seed.url) and not seed.error:
                    document = seed
                else:
                    if url not in fetches:
                        fetches[url] = asyncio.create_task(fetch(url))
                    document = await fetches[url]
                if not document.error and document.text and usable_source_url(document.url):
                    documents.append(document)
            return documents

        groups = await asyncio.gather(*(options(i, seed) for i, seed in enumerate(seeds)))
        review_items = [
            {
                "index": index,
                "original_title": seeds[index].title,
                "original_text": seeds[index].text[:2000],
                "pages": [
                    {"page_index": j, "url": doc.url, "title": doc.title,
                     "text": doc.text[:8000]}
                    for j, doc in enumerate(documents)
                ],
            }
            for index, documents in enumerate(groups)
        ]
        reviewed = await llm.review_digest_sources(candidate, review_items)
        choices: dict[int, list[tuple[dict[str, Any], SourceDocument]]] = {}
        for selection in reviewed.get("selections", []):
            if not isinstance(selection, dict):
                continue
            index, page = selection.get("index"), selection.get("page_index")
            if type(index) is not int or type(page) is not int:
                continue
            if not 0 <= index < len(groups) or not 0 <= page < len(groups[index]):
                continue
            document = groups[index][page]
            if approved_source(selection, document):
                choices.setdefault(index, []).append((selection, document))
        for index, approved in choices.items():
            approved.sort(key=lambda choice: (
                _language(*choice) != "en",
                choice[0]["kind"] == "original_reporting",
            ))
            selection, document = approved[0]
            resolved[index] = replace(document, linkable=True)
            audit["selected"].append({
                "index": index, "url": document.url, "kind": selection["kind"],
                "language": _language(selection, document), "evidence": selection["evidence"],
            })
        audit["unresolved"] = [i for i, source in enumerate(resolved) if not source.linkable]
    except Exception as exc:  # noqa: BLE001 - fail closed for provider or page-processing errors
        # Keep the digest for editorial review, but never fall back to unverified links.
        logger.warning("Digest primary-source lookup failed (%s)", type(exc).__name__)
        audit["lookup_error"] = type(exc).__name__
    return resolved, audit
