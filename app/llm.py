from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.prompts import (
    COMPONENT_REWRITE_SYSTEM_PROMPT,
    IMAGE_SEARCH_PROMPT,
    REPAIR_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
    render_writer_system_prompt,
)
from app.renderer import (
    SIGNATURE_TEXT,
    normalize_body_spacing,
    normalize_generated_post,
    validate_post,
    visible_body,
)

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


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
        content_limit = self.max_post_chars - 2 - len(SIGNATURE_TEXT)
        system = render_writer_system_prompt(content_limit)
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
            post = await self.repair(normalized, sources, normalized_errors)
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

        title_character_limit = _title_character_limit(post, self.max_post_chars)
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
                system=COMPONENT_REWRITE_SYSTEM_PROMPT,
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
                    normalized = normalize_generated_post(raw_proposal, self.max_post_chars)
                    fitted_title = _fit_title_to_limit(
                        normalized.title,
                        title_character_limit,
                    )
                    was_truncated = fitted_title != normalized.title
                    prepared_titles.append((was_truncated, fitted_title))

                for _, fitted_title in sorted(prepared_titles, key=lambda item: item[0]):
                    proposal = GeneratedPost(fitted_title, post.body, post.hashtags)
                    title_key = proposal.title.casefold()
                    errors = validate_post(proposal, self.max_post_chars)
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
            normalized = normalize_generated_post(proposal, self.max_post_chars)
            proposal = GeneratedPost(post.title, post.body, normalized.hashtags)
            changed = {tag.casefold() for tag in proposal.hashtags} != {
                tag.casefold() for tag in post.hashtags
            }
            remaining = validate_post(proposal, self.max_post_chars)
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
            normalized = normalize_generated_post(current, self.max_post_chars)
            remaining = validate_post(normalized, self.max_post_chars)
            if not remaining:
                return normalized
            current = normalized
        raise ValueError("Post failed validation after repair: " + "; ".join(remaining))
