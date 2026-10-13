from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.db import Repository
from app.extractor import ArticleExtractor
from app.llm import QwenClient
from app.models import SourceDocument, StoryCandidate, TriageDecision
from app.renderer import render_post


logger = logging.getLogger(__name__)
DraftDelivery = Callable[[int], Awaitable[None]]


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
            await self.repository.update_feed_status(item_id, "failed", type(exc).__name__)
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
        if not sources:
            sources = [
                SourceDocument(
                    url=candidate.origin_ref if candidate.origin_ref.startswith("http") else "",
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
                "Story rejected with score %.2f: %s",
                triage.score,
                candidate.title,
            )
            return None

        post = await self.llm.generate(candidate, sources, triage)
        render_post(post, sources, self.settings.max_post_chars)
        media = self._media_candidates(candidate, sources, triage)
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
        ordered = list(range(len(sources)))
        if 0 <= triage.primary_source_index < len(sources):
            ordered.remove(triage.primary_source_index)
            ordered.insert(0, triage.primary_source_index)
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
