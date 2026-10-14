from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.prompts import REPAIR_SYSTEM_PROMPT, TRIAGE_SYSTEM_PROMPT, render_writer_system_prompt
from app.renderer import validate_post

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json(content: str) -> dict[str, Any]:
    cleaned = JSON_FENCE_RE.sub("", content.strip()).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise TypeError("Model returned a non-object JSON value")
    return value


def _source_payload(sources: list[SourceDocument], per_source_chars: int = 12_000) -> list[dict[str, Any]]:
    return [
        {
            "index": index,
            "url": source.url,
            "title": source.title,
            "publisher": source.publisher,
            "text": source.text[:per_source_chars],
            "has_preview_image": bool(source.og_image),
            "linkable": source.linkable,
            "error": source.error,
        }
        for index, source in enumerate(sources)
    ]


class QwenClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        triage_model: str,
        writer_model: str,
        max_post_chars: int,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=90, max_retries=2)
        self.triage_model = triage_model
        self.writer_model = writer_model
        self.max_post_chars = max_post_chars

    async def _completion(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        response = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Model returned an empty response")
        return _parse_json(content)

    async def triage(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> TriageDecision:
        payload = {
            "candidate": candidate.to_dict(),
            "sources": _source_payload(sources),
        }
        data = await self._completion(
            model=self.triage_model,
            system=TRIAGE_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False),
            temperature=0.1,
            max_tokens=1400,
        )
        return TriageDecision(
            relevant=bool(data.get("relevant", False)),
            score=max(0.0, min(1.0, float(data.get("score", 0)))),
            category=str(data.get("category", "")).strip(),
            reason=str(data.get("reason", "")).strip(),
            primary_source_index=max(0, int(data.get("primary_source_index", 0))),
            linkable_source_indices=[
                int(item)
                for item in data.get("linkable_source_indices", [])
                if str(item).lstrip("-").isdigit()
            ],
            facts=[str(item).strip() for item in data.get("facts", []) if str(item).strip()],
            uncertainties=[
                str(item).strip() for item in data.get("uncertainties", []) if str(item).strip()
            ],
        )

    async def generate(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
    ) -> GeneratedPost:
        system = render_writer_system_prompt(self.max_post_chars)
        payload = {
            "candidate": candidate.to_dict(),
            "editorial_decision": triage.to_dict(),
            "sources": _source_payload(sources),
        }
        data = await self._completion(
            model=self.writer_model,
            system=system,
            user=json.dumps(payload, ensure_ascii=False),
            temperature=0.55,
            max_tokens=3000,
        )
        post = GeneratedPost(
            title=str(data.get("title", "")).strip(),
            body=str(data.get("body", "")).strip(),
            hashtags=[str(tag).strip() for tag in data.get("hashtags", [])],
        )
        errors = validate_post(post, self.max_post_chars)
        if errors:
            post = await self.repair(post, sources, errors)
        return post

    async def repair(
        self,
        post: GeneratedPost,
        sources: list[SourceDocument],
        errors: list[str],
    ) -> GeneratedPost:
        current = post
        remaining = errors
        for _ in range(3):
            payload = {
                "post": current.to_dict(),
                "errors": remaining,
                "maximum_visible_characters": self.max_post_chars,
                "allowed_sources": _source_payload(sources, per_source_chars=3000),
            }
            data = await self._completion(
                model=self.writer_model,
                system=REPAIR_SYSTEM_PROMPT,
                user=json.dumps(payload, ensure_ascii=False),
                temperature=0.15,
                max_tokens=2600,
            )
            current = GeneratedPost(
                title=str(data.get("title", "")).strip(),
                body=str(data.get("body", "")).strip(),
                hashtags=[str(tag).strip() for tag in data.get("hashtags", [])],
            )
            remaining = validate_post(current, self.max_post_chars)
            if not remaining:
                return current
        raise ValueError("Post failed validation after repair: " + "; ".join(remaining))
