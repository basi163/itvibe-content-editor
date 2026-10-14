from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.models import GeneratedPost, SourceDocument

SOURCE_MARKER_RE = re.compile(r"\[\[source:(\d+)\|([^\]]+)]]", re.IGNORECASE)
TAG_RE = re.compile(r"^#[\wА-Яа-яЁё]+$", re.UNICODE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "]+",
    flags=re.UNICODE,
)

BANNED_PHRASES = (
    "и здесь появляется интересный парадокс",
    "получается интересная ситуация",
    "и вот тут начинается самое важное",
    "и вот тут начинается необычный разворот",
    "вот тут начинается самое интересное",
)


@dataclass
class RenderedPost:
    html: str
    visible_text: str
    char_count: int


def _safe_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def visible_body(body: str) -> str:
    return SOURCE_MARKER_RE.sub(lambda match: match.group(2), body)


def _body_to_html(body: str, sources: list[SourceDocument]) -> str:
    result: list[str] = []
    position = 0
    for match in SOURCE_MARKER_RE.finditer(body):
        result.append(html.escape(body[position : match.start()]))
        index = int(match.group(1))
        anchor = html.escape(match.group(2).strip())
        if 0 <= index < len(sources) and _safe_url(sources[index].url):
            url = html.escape(sources[index].url, quote=True)
            result.append(f'<a href="{url}">{anchor}</a>')
        else:
            result.append(anchor)
        position = match.end()
    result.append(html.escape(body[position:]))
    return "".join(result)


def validate_post(post: GeneratedPost, max_chars: int) -> list[str]:
    errors: list[str] = []
    title = post.title.strip()
    body = visible_body(post.body).strip()
    hashtags = [tag.strip() for tag in post.hashtags]
    visible = f"{title}\n\n{body}\n\n{' '.join(hashtags)}"
    lowered = visible.casefold()

    if not title:
        errors.append("Пустой заголовок")
    if not body:
        errors.append("Пустой основной текст")
    if ":" in visible or "：" in visible:
        errors.append("В видимом тексте есть двоеточие")
    if EMOJI_RE.search(visible):
        errors.append("В тексте есть эмодзи")
    if visible.count("—") > 3:
        errors.append("Слишком много длинных тире")
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            errors.append(f"Запрещенный шаблон: {phrase}")
    if len(hashtags) != 3:
        errors.append("Нужно ровно три хэштега")
    elif any(not TAG_RE.match(tag) for tag in hashtags):
        errors.append("Некорректный формат хэштегов")
    if len(visible) > max_chars:
        errors.append(f"Текст длиннее лимита на {len(visible) - max_chars} знаков")
    if post.body.count("[[source:") > 3:
        errors.append("Использовано больше трех ссылок")
    return errors


def render_post(
    post: GeneratedPost,
    sources: list[SourceDocument],
    max_chars: int,
) -> RenderedPost:
    errors = validate_post(post, max_chars)
    if errors:
        raise ValueError("; ".join(errors))

    title = html.escape(post.title.strip())
    body = _body_to_html(post.body.strip(), sources)
    hashtags = " ".join(html.escape(tag.strip()) for tag in post.hashtags)
    rendered = f"<b>{title}</b>\n\n{body}\n\n{hashtags}"
    visible = HTML_TAG_RE.sub("", rendered)
    visible = html.unescape(visible)
    return RenderedPost(rendered, visible, len(visible))
