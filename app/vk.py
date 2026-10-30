from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.models import GeneratedPost, SourceDocument
from app.renderer import (
    SOURCE_MARKER_RE,
    normalize_body_spacing,
)

VK_API_URL = "https://api.vk.com/method"
VK_TARGET_ID_BASE = 8_000_000_000_000_000
VK_SCREEN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
VK_TELEGRAM_URL = "https://t.me/itvibenews"


class VkApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class VkGroup:
    group_id: int
    title: str
    screen_name: str
    is_admin: bool


def vk_target_id(group_id: int) -> int:
    if group_id <= 0:
        raise ValueError("VK group ID must be positive")
    return VK_TARGET_ID_BASE + group_id


def vk_group_id_from_target(target_id: int) -> int | None:
    group_id = int(target_id) - VK_TARGET_ID_BASE
    return group_id if group_id > 0 else None


def parse_vk_group_name(value: str) -> str:
    cleaned = value.strip()
    cleaned = cleaned.removeprefix("@")
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


def render_vk_post(
    post: GeneratedPost,
    sources: list[SourceDocument],
) -> str:
    body = normalize_body_spacing(post.body)
    result: list[str] = []
    position = 0
    for match in SOURCE_MARKER_RE.finditer(body):
        result.append(body[position : match.start()])
        index = int(match.group(1))
        anchor = match.group(2).strip()
        if (
            0 <= index < len(sources)
            and sources[index].linkable
            and sources[index].url.startswith(("http://", "https://"))
        ):
            result.append(f"{anchor} — {sources[index].url}")
        else:
            result.append(anchor)
        position = match.end()
    result.append(body[position:])
    visible_body = normalize_body_spacing("".join(result))
    hashtags = " ".join(tag.strip() for tag in post.hashtags)
    title = post.title.strip().upper()
    signature = f"IT Vibe News → {VK_TELEGRAM_URL}"
    return (
        f"▰ {title}\n\n"
        f"{visible_body}\n\n"
        f"{hashtags}\n\n"
        f"{signature}"
    )


class VkPublisher:
    def __init__(
        self,
        access_token: str = "",
        *,
        community_access_token: str = "",
        community_group_id: int | None = None,
        api_version: str = "5.199",
        timeout: float = 25.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.user_access_token = access_token.strip()
        self.community_access_token = community_access_token.strip()
        self.community_group_id = community_group_id
        self.access_token = self.community_access_token or self.user_access_token
        if not self.access_token:
            raise ValueError("VK access token is required")
        if self.community_access_token and not self.community_group_id:
            raise ValueError("VK community group ID is required")
        self.api_version = api_version
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @property
    def requires_photo_upload(self) -> bool:
        return bool(self.user_access_token)

    async def _call(
        self,
        method: str,
        *,
        access_token: str | None = None,
        **parameters: Any,
    ) -> Any:
        response = await self.client.post(
            f"{VK_API_URL}/{method}",
            data={
                **parameters,
                "access_token": access_token or self.access_token,
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
        if self.community_access_token:
            group_id = int(self.community_group_id or 0)
            permissions = await self._call("groups.getTokenPermissions")
            names = {
                str(item.get("name") or "")
                for item in permissions.get("permissions", [])
                if isinstance(item, dict)
            }
            if "wall" not in names:
                raise ValueError("У ключа сообщества нет права «Стена»")
            lookup = str(group_id)
        else:
            resolved = await self._call(
                "utils.resolveScreenName",
                screen_name=screen_name,
            )
            if not isinstance(resolved, dict) or resolved.get("type") not in {
                "group",
                "page",
                "event",
            }:
                raise ValueError("По этой ссылке не найдено сообщество VK")
            group_id = int(resolved.get("object_id") or 0)
            lookup = str(group_id)
        response = await self._call(
            "groups.getById",
            group_ids=lookup,
            fields="description",
        )
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if not isinstance(groups, list) or not groups:
            raise ValueError("VK не вернул данные сообщества")
        group = groups[0]
        resolved_group_id = int(group.get("id") or group_id)
        if self.community_group_id and resolved_group_id != self.community_group_id:
            raise ValueError("Этот ключ VK принадлежит другому сообществу")
        resolved_screen_name = str(group.get("screen_name") or "")
        if self.community_group_id:
            allowed_names = {
                resolved_screen_name.casefold(),
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
            screen_name=resolved_screen_name or screen_name,
            is_admin=bool(group.get("is_admin")) or bool(self.community_access_token),
        )

    async def publish_photo(
        self,
        *,
        group_id: int,
        message: str,
        content: bytes,
        filename: str,
        guid: str,
        fallback_url: str | None = None,
    ) -> int:
        if not self.user_access_token:
            raise VkApiError(
                "VK не позволяет ключу сообщества загрузить фотографию на стену. "
                "Публикация остановлена, чтобы не отправлять пост без изображения."
            )

        upload_server = await self._call(
            "photos.getWallUploadServer",
            access_token=self.user_access_token,
            group_id=group_id,
        )
        upload_url = str(upload_server.get("upload_url") or "")
        if not upload_url.startswith(("http://", "https://")):
            raise VkApiError("VK did not return a photo upload URL")
        extension = Path(filename).suffix.casefold()
        content_type = "image/png" if extension == ".png" else "image/jpeg"
        upload = await self.client.post(
            upload_url,
            files={"photo": (filename or "image.jpg", content, content_type)},
        )
        upload.raise_for_status()
        upload_payload = upload.json()
        saved = await self._call(
            "photos.saveWallPhoto",
            access_token=self.user_access_token,
            group_id=group_id,
            photo=upload_payload.get("photo", ""),
            server=upload_payload.get("server", ""),
            hash=upload_payload.get("hash", ""),
        )
        if not isinstance(saved, list) or not saved:
            raise VkApiError("VK did not save the uploaded photo")
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        if access_key := photo.get("access_key"):
            attachment += f"_{access_key}"
        published = await self._call(
            "wall.post",
            owner_id=-group_id,
            from_group=1,
            message=message,
            attachments=attachment,
            guid=guid,
        )
        post_id = int(published.get("post_id") or 0)
        if post_id <= 0:
            raise VkApiError("VK did not return the published post ID")
        return post_id

    async def edit_post(
        self,
        *,
        group_id: int,
        post_id: int,
        message: str,
        attachment_url: str | None = None,
    ) -> None:
        attachments = ""
        if attachment_url and attachment_url.startswith(("http://", "https://")):
            attachments = attachment_url
        edited = await self._call(
            "wall.edit",
            owner_id=-group_id,
            post_id=post_id,
            message=message,
            attachments=attachments,
        )
        if edited not in (1, True) and not isinstance(edited, dict):
            raise VkApiError("VK did not confirm the edited post")
