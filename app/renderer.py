from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.models import GeneratedPost, SourceDocument

SOURCE_MARKER_RE = re.compile(r"\[\[source:(\d+)\|([^\]]+)]]", re.IGNORECASE)
SOURCE_MARKER_LIKE_RE = re.compile(
    r"\[\[\s*source\b[^\]\r\n]*]]",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"^#[\wА-Яа-яЁё]+$", re.UNICODE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
RAW_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
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
    "этот шаг показывает",
    "этот случай показывает",
    "этот пример показывает",
    "это показывает",
    "мы наблюдаем классический пример",
)
META_LEAD_IN_RE = re.compile(
    r"(?P<prefix>^|(?<=[.!?\n])\s*)"
    r"(?:"
    r"(?:этот шаг|этот случай|этот пример|это)\s+(?:хорошо\s+)?показывает,\s+что"
    r"|мы\s+наблюдаем\s+(?:классический\s+)?пример\s+того,\s+как"
    r")\s+(?P<next>[A-Za-zА-Яа-яЁё])",
    re.IGNORECASE,
)

SIGNATURE_TEXT = "🔳 IT Vibe News"
SIGNATURE_URL = "https://t.me/+u5H6mukl7zg4NzZi"


@dataclass
class RenderedPost:
    html: str
    rich_html: str
    visible_text: str
    char_count: int


def _safe_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def visible_body(body: str) -> str:
    return SOURCE_MARKER_RE.sub(lambda match: match.group(2), body)


def normalize_body_spacing(body: str) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [part.strip() for part in re.split(r"\n+", normalized) if part.strip()]
    return "\n\n".join(paragraphs)


def _clean_visible_fragment(value: str) -> str:
    cleaned = EMOJI_RE.sub("", value)
    cleaned = RAW_URL_RE.sub("", cleaned)
    cleaned = SOURCE_MARKER_LIKE_RE.sub("", cleaned)
    cleaned = cleaned.replace("：", ",").replace(":", ",")
    cleaned = META_LEAD_IN_RE.sub(
        lambda match: match.group("prefix") + match.group("next").upper(),
        cleaned,
    )
    for phrase in BANNED_PHRASES:
        cleaned = re.sub(re.escape(phrase), "Важно", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;!?])", r"\1", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned


def _clean_body(body: str) -> str:
    result: list[str] = []
    position = 0
    marker_count = 0
    for match in SOURCE_MARKER_RE.finditer(body):
        result.append(_clean_visible_fragment(body[position : match.start()]))
        anchor = _clean_visible_fragment(match.group(2)).strip()
        if anchor:
            marker_count += 1
            if marker_count <= 3:
                result.append(f"[[source:{match.group(1)}|{anchor}]]")
            else:
                result.append(anchor)
        position = match.end()
    result.append(_clean_visible_fragment(body[position:]))
    return normalize_body_spacing("".join(result))


def _limit_em_dashes(value: str, allowed: int) -> str:
    parts = value.split("—")
    if len(parts) - 1 <= allowed:
        return value
    result = parts[0]
    for index, part in enumerate(parts[1:]):
        result += ("—" if index < allowed else "-") + part
    return result


def _clean_hashtags(hashtags: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for hashtag in hashtags:
        value = re.sub(r"[^\wА-Яа-яЁё]", "", hashtag.lstrip("#"), flags=re.UNICODE)
        if not value:
            continue
        tag = f"#{value}"
        key = tag.casefold()
        if key not in seen:
            cleaned.append(tag)
            seen.add(key)
    for fallback in ("#технологии", "#новости", "#IT"):
        if fallback.casefold() not in seen:
            cleaned.append(fallback)
            seen.add(fallback.casefold())
        if len(cleaned) == 3:
            break
    return cleaned[:3]


def _truncate_plain_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    candidate = value[: limit - 1].rstrip()
    boundary = max(candidate.rfind(". "), candidate.rfind("! "), candidate.rfind("? "))
    if boundary >= max(20, limit // 2):
        return candidate[: boundary + 1]
    if " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0]
    return candidate.rstrip(".,;!? ") + "…"


def normalize_generated_post(post: GeneratedPost, max_chars: int) -> GeneratedPost:
    """Deterministically repair formal style violations without inventing facts."""
    title = _clean_visible_fragment(post.title).strip()
    body = _clean_body(post.body)
    hashtags = _clean_hashtags(post.hashtags)

    title = _limit_em_dashes(title, 3)
    body = _limit_em_dashes(body, max(0, 3 - title.count("—")))

    hashtag_text = " ".join(hashtags)
    fixed_length = len(title) + 2 + 2 + len(hashtag_text) + 2 + len(SIGNATURE_TEXT)
    body_limit = max_chars - fixed_length
    if body_limit > 0 and len(visible_body(body)) > body_limit:
        body = _truncate_plain_text(visible_body(body), body_limit)

    return GeneratedPost(title=title, body=body, hashtags=hashtags)


def _body_to_html(body: str, sources: list[SourceDocument]) -> str:
    result: list[str] = []
    position = 0
    for match in SOURCE_MARKER_RE.finditer(body):
        result.append(html.escape(body[position : match.start()]))
        index = int(match.group(1))
        anchor = html.escape(match.group(2).strip())
        if (
            0 <= index < len(sources)
            and sources[index].linkable
            and _safe_url(sources[index].url)
        ):
            url = html.escape(sources[index].url, quote=True)
            result.append(f'<a href="{url}">{anchor}</a>')
        else:
            result.append(anchor)
        position = match.end()
    result.append(html.escape(body[position:]))
    return "".join(result)


def _body_to_rich_html(body: str, sources: list[SourceDocument]) -> str:
    paragraphs = normalize_body_spacing(body).split("\n\n")
    blocks: list[str] = []
    for paragraph in paragraphs:
        content = _body_to_html(paragraph, sources)
        blocks.append(f"<p>{content}</p>")
    return "".join(blocks)


def validate_post(post: GeneratedPost, max_chars: int) -> list[str]:
    errors: list[str] = []
    title = post.title.strip()
    body = visible_body(normalize_body_spacing(post.body))
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
    total_visible_length = len(visible) + 2 + len(SIGNATURE_TEXT)
    if total_visible_length > max_chars:
        errors.append(
            f"Текст длиннее лимита на {total_visible_length - max_chars} знаков"
        )
    if post.body.count("[[source:") > 3:
        errors.append("Использовано больше трех ссылок")
    if RAW_URL_RE.search(visible):
        errors.append("В видимом тексте есть URL")
    if SOURCE_MARKER_LIKE_RE.search(visible):
        errors.append("В видимом тексте есть некорректный маркер источника")
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
    normalized_body = normalize_body_spacing(post.body)
    body = _body_to_html(normalized_body, sources)
    rich_body = _body_to_rich_html(normalized_body, sources)
    hashtags = " ".join(html.escape(tag.strip()) for tag in post.hashtags)
    signature_url = html.escape(SIGNATURE_URL, quote=True)
    signature_text = html.escape(SIGNATURE_TEXT)
    signature = f'<a href="{signature_url}">{signature_text}</a>'
    rendered = f"<b>{title}</b>\n\n{body}\n\n{hashtags}\n\n{signature}"
    rich_rendered = (
        f"<p><b>{title}</b></p>{rich_body}<p>{hashtags}</p><p>{signature}</p>"
    )
    visible = HTML_TAG_RE.sub("", rendered)
    visible = html.unescape(visible)
    return RenderedPost(rendered, rich_rendered, visible, len(visible))
