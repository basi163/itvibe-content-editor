from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI

from app.models import (
    FactCheckResult,
    GeneratedPost,
    SourceDocument,
    StoryCandidate,
    TriageDecision,
)
from app.prompts import (
    COMPONENT_REWRITE_SYSTEM_PROMPT,
    DIGEST_REPAIR_SYSTEM_PROMPT,
    FACT_CHECK_EVIDENCE_PROMPT,
    FACT_CHECK_PROMPT,
    IMAGE_SEARCH_PROMPT,
    REPAIR_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
    append_editorial_rules,
    render_digest_writer_system_prompt,
    render_writer_system_prompt,
)
from app.renderer import (
    SIGNATURE_TEXT,
    SOURCE_MARKER_RE,
    normalize_body_spacing,
    normalize_generated_post,
    validate_post,
    visible_body,
)

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
DIGEST_MAX_LINKS = 10
GENERIC_DIGEST_LINK_ANCHORS = {
    "здесь",
    "источник",
    "оригинал",
    "подробнее",
    "по ссылке",
    "ссылка",
    "читать",
    "читать далее",
    "узнать больше",
}
DIGEST_SOURCE_ATTRIBUTION_RE = re.compile(
    r"\b(?:"
    r"по\s+данным|пишет|написал(?:а|и)?|сообщает|сообщил(?:а|и)?|"
    r"отмечает|отметил(?:а|и)?|уточняет|уточнил(?:а|и)?|"
    r"рассказывает|рассказал(?:а|и)?|считает|считают"
    r")\b",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def _source_link_limit(triage: TriageDecision) -> int:
    return DIGEST_MAX_LINKS if triage.category == "digest" else 3


def _digest_validation_errors(
    post: GeneratedPost,
    max_post_chars: int,
    source_count: int,
) -> list[str]:
    errors = validate_post(
        post,
        max_post_chars,
        max_source_links=DIGEST_MAX_LINKS,
    )
    indices = [int(match.group(1)) for match in SOURCE_MARKER_RE.finditer(post.body)]
    expected = list(range(min(source_count, DIGEST_MAX_LINKS)))
    if indices != expected:
        errors.append(
            "Ссылки дайджеста должны идти по одной в порядке источников"
        )
    generic_anchors = [
        match.group(2).strip().casefold().strip(" .,!—-–")
        for match in SOURCE_MARKER_RE.finditer(post.body)
    ]
    if any(
        anchor in GENERIC_DIGEST_LINK_ANCHORS
        or DIGEST_SOURCE_ATTRIBUTION_RE.search(anchor)
        for anchor in generic_anchors
    ):
        errors.append(
            "Ссылка должна быть встроена в сам факт новости, без «Подробнее» "
            "и упоминания СМИ"
        )
    detached_markers = []
    late_markers = []
    for match in SOURCE_MARKER_RE.finditer(post.body):
        paragraph_start = post.body.rfind("\n", 0, match.start()) + 1
        prefix = post.body[paragraph_start:match.start()]
        prefix = re.sub(r"^\s*\d+\.\s*", "", prefix)
        if re.search(r"[.!?](?:\s|$)", prefix):
            detached_markers.append(match.group(0))
        if len(prefix.strip()) > 70:
            late_markers.append(match.group(0))
    if detached_markers:
        errors.append(
            "Ссылка должна стоять внутри основного предложения новости, "
            "а не отдельной фразой после точки"
        )
    if late_markers:
        errors.append(
            "Ссылка должна быть встроена в ключевые слова в начале новости "
            "(в первых 70 знаках), а не приписана в конце"
        )
    return errors


def _title_character_limit(post: GeneratedPost, max_post_chars: int) -> int:
    """Return the title budget while leaving every other post component unchanged."""
    body = visible_body(normalize_body_spacing(post.body))
    hashtags = " ".join(tag.strip() for tag in post.hashtags)
    fixed_length = len(body) + len(hashtags) + len(SIGNATURE_TEXT) + 6
    return max(0, max_post_chars - fixed_length)


def _fit_title_to_limit(title: str, limit: int) -> str:
    if len(title) <= limit:
        return title
    if limit <= 0:
        return ""
    if limit == 1:
        return title[:1]

    candidate = title[: limit - 1].rstrip()
    if " " in candidate:
        word_boundary = candidate.rsplit(" ", 1)[0].rstrip(".,;!?—- ")
        if word_boundary:
            candidate = word_boundary
    candidate = candidate.rstrip(".,;!?—- ")
    return f"{candidate}…" if candidate else title[:limit]


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
        editorial_rules_provider: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=90, max_retries=2)
        self.triage_model = triage_model
        self.writer_model = writer_model
        self.max_post_chars = max_post_chars
        self.editorial_rules_provider = editorial_rules_provider

    async def close(self) -> None:
        await self.client.close()

    async def _editorial_rules(self) -> str:
        if self.editorial_rules_provider is None:
            return ""
        value = await self.editorial_rules_provider()
        return str(value or "").strip()[:3000]

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
        interest_keys = (
            "personal_significance",
            "practical_value",
            "surprise",
            "evidence",
            "technology_story",
            "business_significance",
        )
        raw_breakdown = data.get("reader_interest_breakdown", {})
        if not isinstance(raw_breakdown, dict):
            raw_breakdown = {}
        interest_breakdown: dict[str, int] = {}
        for key in interest_keys:
            try:
                value = int(raw_breakdown.get(key, 0))
            except (TypeError, ValueError):
                value = 0
            interest_breakdown[key] = max(0, min(2, value))
        interest_score = sum(interest_breakdown.values())
        if not any(interest_breakdown.values()):
            try:
                interest_score = int(data.get("reader_interest_score", 0))
            except (TypeError, ValueError):
                interest_score = 0
            interest_score = max(0, min(12, interest_score))
        return TriageDecision(
            relevant=bool(data.get("relevant", False)),
            score=max(0.0, min(1.0, float(data.get("score", 0)))),
            category=str(data.get("category", "")).strip(),
            reason=str(data.get("reason", "")).strip(),
            industry_critical=bool(data.get("industry_critical", False)),
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
            reader_interest_score=interest_score,
            reader_interest_breakdown=interest_breakdown,
        )

    async def fact_check(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> FactCheckResult:
        del sources
        context = {
            "current_date": datetime.now(UTC).date().isoformat(),
            "candidate": candidate.to_dict(),
        }
        response = await self.client.chat.completions.create(
            model=self.writer_model,
            messages=[
                {"role": "system", "content": FACT_CHECK_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                },
            ],
            temperature=0.1,
            max_tokens=2500,
            extra_body={"enable_search": True, "enable_thinking": False},
        )
        content = str(response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("Fact-check model returned an empty response")
        data = _parse_json(content)

        source_urls: list[str] = []
        source_evidence: dict[str, str] = {}
        for item in data.get("sources", []):
            value = item.get("url", "") if isinstance(item, dict) else item
            url = str(value or "").strip()
            if url.startswith(("http://", "https://")) and url not in source_urls:
                source_urls.append(url)
            if isinstance(item, dict) and url:
                evidence = str(item.get("evidence", "") or "").strip()
                if evidence:
                    source_evidence[url] = evidence[:3000]

        return FactCheckResult(
            facts=[str(item).strip() for item in data.get("facts", []) if str(item).strip()],
            uncertainties=[
                str(item).strip()
                for item in data.get("uncertainties", [])
                if str(item).strip()
            ],
            source_urls=source_urls[:6],
            source_evidence={
                url: source_evidence[url]
                for url in source_urls[:6]
                if url in source_evidence
            },
        )

    async def verify_fact_check(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> FactCheckResult:
        payload = {
            "current_date": datetime.now(UTC).date().isoformat(),
            "candidate": candidate.to_dict(),
            "evidence_pages": _source_payload(sources, per_source_chars=10_000),
        }
        data = await self._completion(
            model=self.writer_model,
            system=FACT_CHECK_EVIDENCE_PROMPT,
            user=json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
            max_tokens=1800,
        )
        selected_indices = [
            int(item)
            for item in data.get("source_indices", [])
            if str(item).lstrip("-").isdigit()
            and 0 <= int(item) < len(sources)
        ]
        if not selected_indices:
            selected_indices = list(range(len(sources)))
        source_urls = list(
            dict.fromkeys(sources[index].url for index in selected_indices)
        )
        return FactCheckResult(
            facts=[str(item).strip() for item in data.get("facts", []) if str(item).strip()],
            uncertainties=[
                str(item).strip()
                for item in data.get("uncertainties", [])
                if str(item).strip()
            ],
            source_urls=source_urls,
        )

    async def generate(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
    ) -> GeneratedPost:
        content_limit = self.max_post_chars - 2 - len(SIGNATURE_TEXT)
        editorial_rules = await self._editorial_rules()
        system = render_writer_system_prompt(content_limit, editorial_rules)
        if candidate.force_editorial:
            linkable_indices = set(triage.linkable_source_indices)
            linkable_sources = []
            for index, source in enumerate(sources):
                if index not in linkable_indices:
                    continue
                linkable_sources.append(
                    {
                        "index": index,
                        "url": source.url,
                        "title": source.title,
                        "publisher": source.publisher,
                        "text": source.text[:12_000],
                        "linkable": True,
                    }
                )
            payload = {
                "editorial_mode": "verified_facts_only",
                "verified_facts": triage.facts,
                "uncertainties": triage.uncertainties,
                "category": triage.category,
                "industry_critical": triage.industry_critical,
                "linkable_sources": linkable_sources,
            }
        else:
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
            normalized = normalize_generated_post(post, self.max_post_chars)
            normalized_errors = validate_post(normalized, self.max_post_chars)
            if not normalized_errors:
                return normalized
            post = await self.repair(
                normalized,
                sources,
                normalized_errors,
                editorial_rules=editorial_rules,
            )
        return post

    async def generate_digest(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
    ) -> GeneratedPost:
        content_limit = self.max_post_chars - 2 - len(SIGNATURE_TEXT)
        editorial_rules = await self._editorial_rules()
        payload = {
            "candidate": candidate.to_dict(),
            "editorial_decision": triage.to_dict(),
            "sources": _source_payload(sources),
        }
        data = await self._completion(
            model=self.writer_model,
            system=render_digest_writer_system_prompt(
                content_limit,
                editorial_rules,
            ),
            user=json.dumps(payload, ensure_ascii=False),
            temperature=0.45,
            max_tokens=3200,
        )
        post = GeneratedPost(
            title=str(data.get("title", "")).strip(),
            body=str(data.get("body", "")).strip(),
            hashtags=[str(tag).strip() for tag in data.get("hashtags", [])],
        )
        errors = _digest_validation_errors(post, self.max_post_chars, len(sources))
        if errors:
            normalized = normalize_generated_post(
                post,
                self.max_post_chars,
                max_source_links=DIGEST_MAX_LINKS,
            )
            normalized_errors = _digest_validation_errors(
                normalized,
                self.max_post_chars,
                len(sources),
            )
            if not normalized_errors:
                return normalized
            return await self.repair_digest(
                normalized,
                sources,
                normalized_errors,
                editorial_rules=editorial_rules,
            )
        return post

    async def search_images(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
        post: GeneratedPost,
        excluded_urls: list[str] | None = None,
    ) -> list[dict[str, str | int]]:
        context = {
            "post_title": post.title,
            "post_body": post.body,
            "original_title": candidate.title,
            "category": triage.category,
            "verified_facts": triage.facts,
            "source_titles": [source.title for source in sources if source.title],
            "do_not_use_image_urls": excluded_urls or [],
        }
        response = await self.client.responses.create(
            model=self.writer_model,
            input=(
                f"{IMAGE_SEARCH_PROMPT}\n\n"
                f"Контекст поста\n{json.dumps(context, ensure_ascii=False)}"
            ),
            tools=[{"type": "web_search_image"}],
            max_output_tokens=200,
        )

        images: list[dict[str, str | int]] = []
        for item in response.output:
            if getattr(item, "type", "") != "web_search_image_call":
                continue
            output = getattr(item, "output", None)
            if not output:
                continue
            parsed = json.loads(output)
            if not isinstance(parsed, list):
                continue
            for image in parsed:
                if not isinstance(image, dict):
                    continue
                url = str(image.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    continue
                images.append(
                    {
                        "index": int(image.get("index", len(images) + 1)),
                        "title": str(image.get("title", "")).strip(),
                        "url": url,
                    }
                )

        if not images:
            return []

        try:
            selection = _parse_json(response.output_text)
            selected_index = int(selection.get("selected_index", images[0]["index"]))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            selected_index = int(images[0]["index"])

        return sorted(
            images,
            key=lambda image: (0 if int(image["index"]) == selected_index else 1),
        )

    async def regenerate_component(
        self,
        component: str,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
        post: GeneratedPost,
    ) -> GeneratedPost:
        if component not in {"title", "hashtags"}:
            raise ValueError(f"Unsupported post component: {component}")

        editorial_rules = await self._editorial_rules()
        title_character_limit = _title_character_limit(post, self.max_post_chars)
        max_source_links = _source_link_limit(triage)
        remaining: list[str] = []
        rejected_titles: list[str] = []
        seen_titles = {post.title.casefold()}
        for _ in range(3):
            payload = {
                "component": component,
                "current_post": post.to_dict(),
                "candidate": candidate.to_dict(),
                "editorial_decision": triage.to_dict(),
                "sources": _source_payload(sources, per_source_chars=3000),
                "previous_errors": remaining,
                "maximum_visible_characters": self.max_post_chars,
                "maximum_title_characters": title_character_limit,
                "rejected_titles": rejected_titles,
            }
            data = await self._completion(
                model=self.writer_model,
                system=append_editorial_rules(
                    COMPONENT_REWRITE_SYSTEM_PROMPT,
                    editorial_rules,
                ),
                user=json.dumps(payload, ensure_ascii=False),
                temperature=0.7,
                max_tokens=500,
            )
            if component == "title":
                raw_titles = data.get("titles", [])
                if not isinstance(raw_titles, list) or not raw_titles:
                    raw_titles = [data.get("title", "")]
                attempt_errors: list[str] = []
                prepared_titles: list[tuple[bool, str]] = []
                for raw_title in raw_titles[:5]:
                    raw_proposal = GeneratedPost(
                        title=str(raw_title).strip(),
                        body=post.body,
                        hashtags=post.hashtags,
                    )
                    normalized = normalize_generated_post(
                        raw_proposal,
                        self.max_post_chars,
                        max_source_links=max_source_links,
                    )
                    fitted_title = _fit_title_to_limit(
                        normalized.title,
                        title_character_limit,
                    )
                    was_truncated = fitted_title != normalized.title
                    prepared_titles.append((was_truncated, fitted_title))

                for _, fitted_title in sorted(prepared_titles, key=lambda item: item[0]):
                    proposal = GeneratedPost(fitted_title, post.body, post.hashtags)
                    title_key = proposal.title.casefold()
                    errors = validate_post(
                        proposal,
                        self.max_post_chars,
                        max_source_links=max_source_links,
                    )
                    if title_key in seen_titles:
                        errors.append("Новый вариант совпадает с текущим или отклоненным")
                    if not errors:
                        return proposal
                    seen_titles.add(title_key)
                    if proposal.title and proposal.title not in rejected_titles:
                        rejected_titles.append(proposal.title)
                    attempt_errors.extend(errors)
                remaining = list(dict.fromkeys(attempt_errors))
                continue

            raw_hashtags = data.get("hashtags", [])
            if not isinstance(raw_hashtags, list):
                raw_hashtags = []
            proposal = GeneratedPost(
                title=post.title,
                body=post.body,
                hashtags=[str(tag).strip() for tag in raw_hashtags],
            )
            normalized = normalize_generated_post(
                proposal,
                self.max_post_chars,
                max_source_links=max_source_links,
            )
            proposal = GeneratedPost(post.title, post.body, normalized.hashtags)
            changed = {tag.casefold() for tag in proposal.hashtags} != {
                tag.casefold() for tag in post.hashtags
            }
            remaining = validate_post(
                proposal,
                self.max_post_chars,
                max_source_links=max_source_links,
            )
            if not changed:
                remaining.append("Новый вариант совпадает с текущим")
            if not remaining:
                return proposal

        raise ValueError(
            f"{component} failed validation after regeneration: " + "; ".join(remaining)
        )

    async def repair(
        self,
        post: GeneratedPost,
        sources: list[SourceDocument],
        errors: list[str],
        *,
        editorial_rules: str = "",
    ) -> GeneratedPost:
        current = post
        remaining = errors
        for _ in range(3):
            payload = {
                "post": current.to_dict(),
                "errors": remaining,
                "maximum_visible_characters": self.max_post_chars
                - 2
                - len(SIGNATURE_TEXT),
                "allowed_sources": _source_payload(sources, per_source_chars=3000),
            }
            data = await self._completion(
                model=self.writer_model,
                system=append_editorial_rules(
                    REPAIR_SYSTEM_PROMPT,
                    editorial_rules,
                ),
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
            normalized = normalize_generated_post(current, self.max_post_chars)
            remaining = validate_post(normalized, self.max_post_chars)
            if not remaining:
                return normalized
            current = normalized
        raise ValueError("Post failed validation after repair: " + "; ".join(remaining))

    async def repair_digest(
        self,
        post: GeneratedPost,
        sources: list[SourceDocument],
        errors: list[str],
        *,
        editorial_rules: str = "",
    ) -> GeneratedPost:
        current = post
        remaining = errors
        for _ in range(3):
            payload = {
                "post": current.to_dict(),
                "errors": remaining,
                "maximum_visible_characters": self.max_post_chars
                - 2
                - len(SIGNATURE_TEXT),
                "allowed_sources": _source_payload(sources, per_source_chars=3000),
            }
            data = await self._completion(
                model=self.writer_model,
                system=append_editorial_rules(
                    DIGEST_REPAIR_SYSTEM_PROMPT,
                    editorial_rules,
                ),
                user=json.dumps(payload, ensure_ascii=False),
                temperature=0.15,
                max_tokens=3000,
            )
            current = GeneratedPost(
                title=str(data.get("title", "")).strip(),
                body=str(data.get("body", "")).strip(),
                hashtags=[str(tag).strip() for tag in data.get("hashtags", [])],
            )
            remaining = _digest_validation_errors(
                current,
                self.max_post_chars,
                len(sources),
            )
            if not remaining:
                return current
            normalized = normalize_generated_post(
                current,
                self.max_post_chars,
                max_source_links=DIGEST_MAX_LINKS,
            )
            remaining = _digest_validation_errors(
                normalized,
                self.max_post_chars,
                len(sources),
            )
            if not remaining:
                return normalized
            current = normalized
        raise ValueError("Digest failed validation after repair: " + "; ".join(remaining))


class DeepSeekClient(QwenClient):
    """DeepSeek implementation for text tasks used by the editorial pipeline."""

    async def _completion(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            # DeepSeek enables high-effort thinking by default. These pipeline
            # calls need deterministic structured output instead, otherwise the
            # hidden reasoning can consume the whole output budget before JSON
            # is emitted. Keep a generous visible-output budget for digests.
            token_budget = max(8192, max_tokens * 2) * (attempt + 1)
            response = await self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=token_budget,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            choice = response.choices[0]
            content = choice.message.content
            finish_reason = str(getattr(choice, "finish_reason", "stop") or "")
            if content and content.strip() and finish_reason != "length":
                try:
                    return _parse_json(content)
                except (json.JSONDecodeError, TypeError) as exc:
                    last_error = exc
            else:
                last_error = RuntimeError(
                    "DeepSeek returned empty or truncated JSON"
                )
            if attempt == 0:
                logger.warning(
                    "DeepSeek JSON response was unusable (%s); retrying",
                    finish_reason or "unknown",
                )
        raise RuntimeError("DeepSeek failed to return valid JSON") from last_error

    async def fact_check(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> FactCheckResult:
        del sources
        context = {
            "current_date": datetime.now(UTC).date().isoformat(),
            "candidate": candidate.to_dict(),
        }
        last_error: Exception | None = None
        data: dict[str, Any] | None = None
        for attempt in range(2):
            response = await self.client.responses.create(
                model=self.writer_model,
                input=(
                    f"{FACT_CHECK_PROMPT}\n\n"
                    f"Return only JSON.\n{json.dumps(context, ensure_ascii=False)}"
                ),
                tools=[{"type": "web_search"}],
                text={"format": {"type": "json_object"}},
                reasoning={"effort": "none"},
                max_output_tokens=8192 * (attempt + 1),
            )
            content = str(response.output_text or "").strip()
            status = str(getattr(response, "status", "completed") or "")
            if content and status != "incomplete":
                try:
                    data = _parse_json(content)
                    break
                except (json.JSONDecodeError, TypeError) as exc:
                    last_error = exc
            else:
                last_error = RuntimeError(
                    "DeepSeek fact-check returned empty or truncated JSON"
                )
            if attempt == 0:
                logger.warning(
                    "DeepSeek fact-check JSON was unusable (%s); retrying",
                    status or "unknown",
                )
        if data is None:
            raise RuntimeError(
                "DeepSeek fact-check failed to return valid JSON"
            ) from last_error

        source_urls: list[str] = []
        source_evidence: dict[str, str] = {}
        for item in data.get("sources", []):
            value = item.get("url", "") if isinstance(item, dict) else item
            url = str(value or "").strip()
            if url.startswith(("http://", "https://")) and url not in source_urls:
                source_urls.append(url)
            if isinstance(item, dict) and url:
                evidence = str(item.get("evidence", "") or "").strip()
                if evidence:
                    source_evidence[url] = evidence[:3000]

        return FactCheckResult(
            facts=[str(item).strip() for item in data.get("facts", []) if str(item).strip()],
            uncertainties=[
                str(item).strip()
                for item in data.get("uncertainties", [])
                if str(item).strip()
            ],
            source_urls=source_urls[:6],
            source_evidence={
                url: source_evidence[url]
                for url in source_urls[:6]
                if url in source_evidence
            },
        )

    async def search_images(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
        post: GeneratedPost,
        excluded_urls: list[str] | None = None,
    ) -> list[dict[str, str | int]]:
        del candidate, sources, triage, post, excluded_urls
        # DeepSeek has text web search, but no equivalent of Qwen's
        # web_search_image tool. Source and Open Graph media remain available.
        return []


class FallbackLLMClient:
    """Use the secondary provider while the primary provider is unavailable."""

    def __init__(
        self,
        primary: QwenClient,
        fallback: DeepSeekClient,
        *,
        cooldown_seconds: int = 300,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.cooldown_seconds = cooldown_seconds
        self._primary_unavailable_until = 0.0

    async def close(self) -> None:
        await self.primary.close()
        await self.fallback.close()

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        now = time.monotonic()
        if now >= self._primary_unavailable_until:
            try:
                result = await getattr(self.primary, method_name)(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - provider failover boundary
                self._primary_unavailable_until = (
                    time.monotonic() + self.cooldown_seconds
                )
                logger.warning(
                    "Primary LLM failed in %s (%s); using fallback for %s seconds",
                    method_name,
                    type(error).__name__,
                    self.cooldown_seconds,
                )
            else:
                if self._primary_unavailable_until:
                    logger.info("Primary LLM provider recovered")
                self._primary_unavailable_until = 0.0
                return result
        return await getattr(self.fallback, method_name)(*args, **kwargs)

    async def triage(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> TriageDecision:
        return await self._call("triage", candidate, sources)

    async def fact_check(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> FactCheckResult:
        return await self._call("fact_check", candidate, sources)

    async def verify_fact_check(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
    ) -> FactCheckResult:
        return await self._call("verify_fact_check", candidate, sources)

    async def generate(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
    ) -> GeneratedPost:
        return await self._call("generate", candidate, sources, triage)

    async def generate_digest(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
    ) -> GeneratedPost:
        return await self._call("generate_digest", candidate, sources, triage)

    async def search_images(
        self,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
        post: GeneratedPost,
        excluded_urls: list[str] | None = None,
    ) -> list[dict[str, str | int]]:
        return await self._call(
            "search_images",
            candidate,
            sources,
            triage,
            post,
            excluded_urls,
        )

    async def regenerate_component(
        self,
        component: str,
        candidate: StoryCandidate,
        sources: list[SourceDocument],
        triage: TriageDecision,
        post: GeneratedPost,
    ) -> GeneratedPost:
        return await self._call(
            "regenerate_component",
            component,
            candidate,
            sources,
            triage,
            post,
        )

    async def repair(
        self,
        post: GeneratedPost,
        sources: list[SourceDocument],
        errors: list[str],
        *,
        editorial_rules: str = "",
    ) -> GeneratedPost:
        return await self._call(
            "repair",
            post,
            sources,
            errors,
            editorial_rules=editorial_rules,
        )

    async def repair_digest(
        self,
        post: GeneratedPost,
        sources: list[SourceDocument],
        errors: list[str],
        *,
        editorial_rules: str = "",
    ) -> GeneratedPost:
        return await self._call(
            "repair_digest",
            post,
            sources,
            errors,
            editorial_rules=editorial_rules,
        )
