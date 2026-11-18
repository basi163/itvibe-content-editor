from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ImageDownload = Callable[[str], Awaitable[tuple[bytes, str] | None]]
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _cached_path(root: Path, key: str) -> Path | None:
    for extension in IMAGE_EXTENSIONS:
        path = root / "media" / f"{key}{extension}"
        if path.is_file() and path.stat().st_size:
            return path
    return None


def _store(root: Path, key: str, content: bytes, filename: str) -> Path:
    directory = root / "media"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise ValueError("Unsupported cached image extension")
    destination = directory / f"{key}{suffix}"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return destination


async def cache_media(
    media: dict[str, Any],
    data_root: Path,
    download: ImageDownload,
    *,
    downloaded: tuple[bytes, str] | None = None,
) -> dict[str, Any] | None:
    """Keep image bytes in the persistent data volume before queuing a draft."""
    result = dict(media)
    if result.get("telegram_file_id") or result.get("type") != "photo":
        return result
    root = data_root.resolve()
    local = str(result.get("local_path") or "")
    if local:
        path = Path(local).resolve()
        if root in path.parents and path.is_file() and path.stat().st_size:
            return result
    url = str(result.get("image_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    key = hashlib.sha256(url.encode()).hexdigest()
    path = await asyncio.to_thread(_cached_path, root, key)
    if path is None:
        downloaded = downloaded or await download(url)
        if not downloaded or not downloaded[0]:
            return None
        path = await asyncio.to_thread(_store, root, key, *downloaded)
    result["local_path"] = str(path)
    return result
