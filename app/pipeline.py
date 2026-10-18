from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.db import Repository
from app.extractor import (
    ArticleExtractor,
    is_known_secondary_url,
    is_solidot_url,
    normalize_url,
)
from app.llm import QwenClient
from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.renderer import render_post

logger = logging.getLogger(__name__)
DraftDelivery = Callable[[int], Awaitable[None]]
SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9._-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})",
    re.IGNORECASE,
)


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    message = SECRET_RE.sub("[redacted]", message)
    return f"{type(exc).__name__}: {message}"[:1000]


class ContentPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        extractor: ArticleExtractor,
        llm: QwenClient,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.extractor = extractor
        self.llm = llm
        self.deliver: DraftDelivery | None = None

    def set_delivery(self, callback: DraftDelivery) -> None:
        self.deliver = callback

    async def process_feed_item(self, item_id: int, candidate: StoryCandidate) -> int | None:
        try:
            draft_id = await self._process(candidate)
            await self.repository.update_feed_status(
                item_id,
                "draft" if draft_id else "rejected",
            )
            return draft_id
        except Exception as exc:
            logger.exception("Failed to process feed item %s", item_id)
            await self.repository.update_feed_status(item_id, "failed", _safe_error(exc))
            return None

    async def process_manual(self, candidate: StoryCandidate) -> int | None:
        fingerprint = hashlib.sha256(
            f"{candidate.origin_ref}|{candidate.title}|{candidate.text}".encode()
        ).hexdigest()[:20]
        candidate.origin_ref = candidate.origin_ref or f"manual:{fingerprint}"
        try:
            return await self._process(candidate)
        except Exception:
            logger.exception("Failed to process manually submitted story")
            return None

    async def _process(self, candidate: StoryCandidate) -> int | None:
        sources = await self.extractor.collect_sources(candidate.urls)
        if candidate.origin_type == "solidot_rss":
            sources = [source for source in sources if not is_solidot_url(source.url)]
        if not sources:
            sources = [
                SourceDocument(
                    url=(
                        candidate.origin_ref
                        if candidate.origin_ref.startswith("http")
                        and not is_solidot_url(candidate.origin_ref)
                        else ""
                    ),
                    title=candidate.title,
                    text=candidate.text,
                    publisher=candidate.origin_type,
                )
            ]
        elif candidate.text and not sources[0].text:
            sources[0].text = candidate.text
        triage = await self.llm.triage(candidate, sources)
        if not triage.relevant or triage.score < self.settings.relevance_threshold:
            logger.info(
                "Story rejected with score %.2f (%s): %s",
                triage.score,
                triage.reason or "no reason supplied",
                candidate.title,
            )
            return None

        requested_linkable = set(triage.linkable_source_indices)
        for index, source in enumerate(sources):
            source.linkable = (
                index in requested_linkable
                and not is_solidot_url(source.url)
                and not is_known_secondary_url(source.url)
            )
        triage.linkable_source_indices = [
            index for index, source in enumerate(sources) if source.linkable
        ]

        post = await self.llm.generate(candidate, sources, triage)
        render_post(post, sources, self.settings.max_post_chars)
        media = self._media_candidates(candidate, sources, triage)
        if not media:
            media = await self._contextual_media(candidate, sources, triage, post)
        if not media:
            logger.info("Story skipped because no usable image was discovered: %s", candidate.title)
            return None
        material = {
            "candidate": candidate.to_dict(),
            "sources": [source.to_dict() for source in sources],
        }
        draft_id = await self.repository.create_draft(
            origin_type=candidate.origin_type,
            origin_ref=candidate.origin_ref,
            title=post.title,
            body=post.body,
            hashtags=post.hashtags,
            sources=[source.to_dict() for source in sources],
            media=media,
            material=material,
            triage=triage.to_dict(),
        )
        if self.deliver:
            await self.deliver(draft_id)
        return draft_id

    async def _contextual_media(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
        post: GeneratedPost,
    ) -> list[dict[str, Any]]:
        try:
            search_results = await self.llm.search_images(candidate, sources, triage, post)
        except Exception:
            logger.exception("Contextual image search failed for: %s", candidate.title)
            return []

        checked: set[str] = set()
        for result in search_results[:10]:
            image_url = normalize_url(str(result.get("url") or ""))
            if not image_url or image_url in checked:
                continue
            checked.add(image_url)
            downloaded = await self.extractor.download_image(image_url)
            if not downloaded or len(downloaded[0]) < 15_000:
                continue
            logger.info("Contextual illustration selected for: %s", candidate.title)
            return [
                {
                    "type": "photo",
                    "telegram_file_id": None,
                    "page_url": image_url,
                    "image_url": image_url,
                    "provider": "qwen_web_search_image",
                    "search_title": str(result.get("title") or ""),
                }
            ]
        return []

    async def regenerate(self, draft_id: int) -> bool:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            return False
        candidate = StoryCandidate.from_dict(draft["material"]["candidate"])
        sources = [SourceDocument.from_dict(item) for item in draft["material"]["sources"]]
        triage = TriageDecision.from_dict(draft["triage"])
        post = await self.llm.generate(candidate, sources, triage)
        render_post(post, sources, self.settings.max_post_chars)
        await self.repository.update_draft(
            draft_id,
            title=post.title,
            body=post.body,
            hashtags_json=_json(post.hashtags),
        )
        return True

    async def regenerate_component(self, draft_id: int, component: str) -> bool:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            return False
        candidate = StoryCandidate.from_dict(draft["material"]["candidate"])
        sources = [SourceDocument.from_dict(item) for item in draft["material"]["sources"]]
        triage = TriageDecision.from_dict(draft["triage"])
        current = GeneratedPost(draft["title"], draft["body"], draft["hashtags"])
        post = await self.llm.regenerate_component(
            component,
            candidate,
            sources,
            triage,
            current,
        )
        render_post(post, sources, self.settings.max_post_chars)
        if component == "title":
            await self.repository.update_draft(draft_id, title=post.title)
        elif component == "hashtags":
            await self.repository.update_draft(
                draft_id,
                hashtags_json=_json(post.hashtags),
            )
        else:
            raise ValueError(f"Unsupported post component: {component}")
        return True

    async def regenerate_media(self, draft_id: int) -> bool:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            return False

        media = list(draft["media"])
        if len(media) > 1:
            next_index = (int(draft["preview_index"]) + 1) % len(media)
            await self.repository.update_draft(draft_id, preview_index=next_index)
            return True

        candidate = StoryCandidate.from_dict(draft["material"]["candidate"])
        sources = [SourceDocument.from_dict(item) for item in draft["material"]["sources"]]
        triage = TriageDecision.from_dict(draft["triage"])
        post = GeneratedPost(draft["title"], draft["body"], draft["hashtags"])
        existing_urls = {
            normalize_url(str(item.get("image_url") or ""))
            for item in media
            if item.get("image_url")
        }
        try:
            search_results = await self.llm.search_images(
                candidate,
                sources,
                triage,
                post,
                excluded_urls=sorted(existing_urls),
            )
        except Exception:
            logger.exception("Alternative image search failed for draft %s", draft_id)
            return False

        for result in search_results[:10]:
            image_url = normalize_url(str(result.get("url") or ""))
            if not image_url or image_url in existing_urls:
                continue
            downloaded = await self.extractor.download_image(image_url)
            if not downloaded or len(downloaded[0]) < 15_000:
                continue
            alternative = {
                "type": "photo",
                "telegram_file_id": None,
                "page_url": image_url,
                "image_url": image_url,
                "provider": "qwen_web_search_image",
                "search_title": str(result.get("title") or ""),
            }
            await self.repository.update_draft(
                draft_id,
                media_json=_json([alternative, *media]),
                preview_index=0,
            )
            return True
        return False

    @staticmethod
    def _media_candidates(
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
    ) -> list[dict[str, Any]]:
        media: list[dict[str, Any]] = []
        if candidate.telegram_file_id and candidate.telegram_media_type:
            media.append(
                {
                    "type": candidate.telegram_media_type,
                    "telegram_file_id": candidate.telegram_file_id,
                    "page_url": candidate.origin_ref,
                    "image_url": None,
                }
            )
        ordered = sorted(
            range(len(sources)),
            key=lambda index: (
                0 if is_known_secondary_url(sources[index].url) else 1,
                0 if index == triage.primary_source_index else 1,
                index,
            ),
        )
        for index in ordered:
            source = sources[index]
            if not source.og_image:
                continue
            item = {
                "type": "photo",
                "telegram_file_id": None,
                "page_url": source.url,
                "image_url": source.og_image,
                "source_index": index,
            }
            if item not in media:
                media.append(item)
        return media


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
