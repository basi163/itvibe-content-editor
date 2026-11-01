from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.models import GeneratedPost
from app.renderer import SOURCE_MARKER_RE, normalize_body_spacing

VK_API_URL = "https://api.vk.com/method"
VK_SCREEN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
VK_TELEGRAM_URL = "https://t.me/itvibenews"


class VkApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class VkGroup:
    group_id: int
    title: str
    screen_name: str

    @property
    def url(self) -> str:
        return f"https://vk.ru/{self.screen_name}"


def parse_vk_group_name(value: str) -> str:
    cleaned = value.strip().removeprefix("@")
    if "://" in cleaned:
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host not in {"vk.com", "vk.ru", "m.vk.com", "m.vk.ru"}:
            raise ValueError("Нужна ссылка на сообщество VK")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise ValueError("Нужна ссылка на главную страницу сообщества VK")
        cleaned = parts[0]
    cleaned = cleaned.strip().strip("/")
    if not VK_SCREEN_NAME_RE.fullmatch(cleaned):
        raise ValueError("Не удалось распознать короткое имя сообщества VK")
    return cleaned


def render_vk_crosspost(post: GeneratedPost, article_url: str) -> str:
    body = SOURCE_MARKER_RE.sub(lambda match: match.group(2).strip(), post.body)
    body = normalize_body_spacing(body)
    hashtags = " ".join(tag.strip() for tag in post.hashtags[:3] if tag.strip())
    blocks = [body]
    if hashtags:
        blocks.append(hashtags)
    blocks.extend(
        [
            f"Читать статью в Teletype → {article_url}",
            f"IT Vibe News → {VK_TELEGRAM_URL}",
        ]
    )
    return "\n\n".join(block for block in blocks if block)


class VkPublisher:
    def __init__(
        self,
        community_access_token: str,
        community_group_id: int,
        *,
        api_version: str = "5.199",
        timeout: float = 25.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = community_access_token.strip()
        self.community_group_id = int(community_group_id)
        if not self.access_token:
            raise ValueError("VK community access token is required")
        if self.community_group_id <= 0:
            raise ValueError("VK community group ID must be positive")
        self.api_version = api_version.strip() or "5.199"
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _call(self, method: str, **parameters: Any) -> Any:
        response = await self.client.post(
            f"{VK_API_URL}/{method}",
            data={
                **parameters,
                "access_token": self.access_token,
                "v": self.api_version,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if error := payload.get("error"):
            code = error.get("error_code", "unknown")
            message = error.get("error_msg", "VK API rejected the request")
            raise VkApiError(f"VK API error {code}: {message}")
        if "response" not in payload:
            raise VkApiError("VK API returned an invalid response")
        return payload["response"]

    async def resolve_group(self, value: str) -> VkGroup:
        screen_name = parse_vk_group_name(value)
        permissions = await self._call("groups.getTokenPermissions")
        permission_names = {
            str(item.get("name") or "")
            for item in (
                permissions.get("permissions", [])
                if isinstance(permissions, dict)
                else []
            )
            if isinstance(item, dict)
        }
        if permission_names and "wall" not in permission_names:
            raise ValueError("У ключа сообщества VK нет права «Стена»")

        response = await self._call(
            "groups.getById",
            group_ids=str(self.community_group_id),
        )
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if not isinstance(groups, list) or not groups:
            raise ValueError("VK не вернул данные сообщества")
        group = groups[0]
        resolved_group_id = int(group.get("id") or 0)
        if resolved_group_id != self.community_group_id:
            raise ValueError("Этот ключ VK принадлежит другому сообществу")
        resolved_name = str(group.get("screen_name") or "")
        allowed_names = {
            resolved_name.casefold(),
            str(resolved_group_id),
            f"club{resolved_group_id}",
            f"public{resolved_group_id}",
            f"event{resolved_group_id}",
        }
        if screen_name.casefold() not in allowed_names:
            raise ValueError("Ссылка ведёт не на сообщество этого ключа VK")
        return VkGroup(
            group_id=resolved_group_id,
            title=str(group.get("name") or screen_name),
            screen_name=resolved_name or screen_name,
        )

    async def publish_article(
        self,
        *,
        group_id: int,
        message: str,
        article_url: str,
        guid: str,
        fallback_title: str | None = None,
    ) -> int:
        if int(group_id) != self.community_group_id:
            raise ValueError("VK group does not match the configured community token")
        if not article_url.startswith(("http://", "https://")):
            raise ValueError("Teletype article URL is invalid")
        try:
            published = await self._call(
                "wall.post",
                owner_id=-self.community_group_id,
                from_group=1,
                message=message,
                attachments=article_url,
                guid=guid,
            )
        except VkApiError as error:
            # VK occasionally rejects Teletype link previews when the source
            # image does not satisfy its sizing rules. The URL is already in
            # the message, so publish without an attachment card in that case.
            detail = str(error).casefold()
            if (
                "link_photo_sizing_rule" not in detail
                and "no photo given" not in detail
            ):
                raise
            fallback_message = message
            if fallback_title and fallback_title.strip():
                fallback_message = f"{fallback_title.strip()}\n\n{message.strip()}"
            published = await self._call(
                "wall.post",
                owner_id=-self.community_group_id,
                from_group=1,
                message=fallback_message,
                guid=guid,
            )
        post_id = int(published.get("post_id") or 0)
        if post_id <= 0:
            raise VkApiError("VK did not return the published post ID")
        return post_id

    def post_url(self, post_id: int) -> str:
        return f"https://vk.ru/wall-{self.community_group_id}_{int(post_id)}"
