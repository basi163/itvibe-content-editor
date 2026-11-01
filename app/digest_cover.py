from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.models import GeneratedPost
from app.renderer import SOURCE_MARKER_RE

COVER_SIZE = (1600, 900)
MASCOT_SHEET = Path(__file__).with_name("assets") / "itvibe_mascot_sheet_orange.png"
ISSUE_RE = re.compile(r"(?:#|№)\s*(\d{1,4})")
ITEM_RE = re.compile(r"^\s*(?:\d{1,2}[.)]|[0-9]\ufe0f?\u20e3)\s*")
SPACE_RE = re.compile(r"\s+")

BRANDS = (
    "OpenAI",
    "Apple",
    "NVIDIA",
    "Xiaomi",
    "Google",
    "Microsoft",
    "Meta",
    "DeepSeek",
    "Anthropic",
    "Mistral",
    "Samsung",
    "Telegram",
    "Yandex",
    "Tesla",
    "Amazon",
    "Huawei",
    "Intel",
    "AMD",
    "Qualcomm",
    "Claude",
)

TOPIC_KEYWORDS = {
    "security": (
        "кибер",
        "взлом",
        "уязв",
        "утеч",
        "атака",
        "вредонос",
        "безопасност",
        "хакер",
    ),
    "ai": (
        "искусственн",
        "нейросет",
        "нейрон",
        "модел",
        "агент",
        "openai",
        "deepseek",
        "anthropic",
        "mistral",
        "nvidia",
        " ии ",
        " ai ",
    ),
    "gadgets": (
        "iphone",
        "macbook",
        "смартфон",
        "ноутбук",
        "гаджет",
        "устройств",
        "apple",
        "xiaomi",
        "samsung",
        "oled",
        "чип",
    ),
    "business": (
        "рынок",
        "инвест",
        "миллиард",
        "раунд",
        "акци",
        "сделк",
        "выручк",
        "бизнес",
    ),
}

PALETTES = {
    "security": ((124, 255, 0), (0, 255, 102), (2, 10, 4)),
    "ai": ((57, 255, 20), (0, 200, 83), (0, 10, 4)),
    "gadgets": ((182, 255, 46), (24, 216, 96), (7, 16, 7)),
    "business": ((0, 255, 133), (61, 255, 82), (3, 16, 10)),
    "technology": ((108, 255, 35), (0, 214, 90), (2, 11, 4)),
}

ORANGE = (255, 119, 22)
RED = (239, 32, 45)

POSES = ("thumbs", "excited", "alert", "thoughtful")
TOPIC_POSE = {
    "security": "alert",
    "ai": "excited",
    "gadgets": "thumbs",
    "business": "thoughtful",
    "technology": "thumbs",
}


@dataclass(frozen=True)
class DigestCoverSpec:
    issue: str
    headlines: tuple[str, ...]
    labels: tuple[str, ...]
    topic: str
    pose: str


def _plain(value: str) -> str:
    value = SOURCE_MARKER_RE.sub(lambda match: match.group(2).strip(), value)
    return SPACE_RE.sub(" ", value).strip()


def _shorten(value: str, limit: int = 78) -> str:
    value = _plain(value)
    if len(value) <= limit:
        return value
    shortened = value[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{shortened}…"


def digest_headlines(body: str, *, limit: int = 6) -> tuple[str, ...]:
    items: list[str] = []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        first = lines[0]
        if not ITEM_RE.match(first):
            continue
        value = ITEM_RE.sub("", " ".join(lines), count=1)
        value = _shorten(value)
        if value:
            items.append(value)
        if len(items) >= limit:
            break
    if items:
        return tuple(items)

    for line in body.splitlines():
        value = _shorten(ITEM_RE.sub("", line, count=1))
        if value:
            items.append(value)
        if len(items) >= limit:
            break
    return tuple(items)


def _labels(text: str, topic: str) -> tuple[str, ...]:
    folded = text.casefold()
    found = [brand for brand in BRANDS if brand.casefold() in folded]
    found.sort(key=lambda brand: folded.index(brand.casefold()))
    unique = list(dict.fromkeys(found))[:4]
    if not unique:
        fallback = {
            "security": ("CYBERSECURITY", "DATA"),
            "ai": ("AI", "TECH"),
            "gadgets": ("GADGETS", "DEVICES"),
            "business": ("TECH BUSINESS", "MARKET"),
            "technology": ("TECH", "IT VIBE"),
        }
        return fallback[topic]
    return tuple(unique)


def _topic(text: str) -> str:
    padded = f" {text.casefold()} "
    scores = {
        topic: sum(padded.count(keyword) for keyword in keywords)
        for topic, keywords in TOPIC_KEYWORDS.items()
    }
    topic, score = max(scores.items(), key=lambda item: item[1])
    return topic if score else "technology"


def build_digest_cover_spec(
    candidate_title: str,
    post: GeneratedPost,
    *,
    variation: int = 0,
) -> DigestCoverSpec:
    issue_match = ISSUE_RE.search(f"{candidate_title}\n{post.title}\n{post.body}")
    issue = f"#{issue_match.group(1)}" if issue_match else "DIGEST"
    headlines = digest_headlines(post.body)
    combined = "\n".join((candidate_title, post.title, *headlines))
    topic = _topic(combined)
    base_pose = TOPIC_POSE[topic]
    pose_index = (POSES.index(base_pose) + max(0, variation)) % len(POSES)
    return DigestCoverSpec(
        issue=issue,
        headlines=headlines,
        labels=_labels(combined, topic),
        topic=topic,
        pose=POSES[pose_index],
    )


def _font(size: int, *, bold: bool = False, condensed: bool = False) -> ImageFont.FreeTypeFont:
    family = "DejaVuSansCondensed" if condensed else "DejaVuSans"
    weight = "-Bold" if bold else ""
    candidates = (
        f"/usr/share/fonts/truetype/dejavu/{family}{weight}.ttf",
        f"/usr/local/share/fonts/{family}{weight}.ttf",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{weight}.ttf",
        f"/usr/local/share/fonts/DejaVuSans{weight}.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("No Cyrillic font is available for digest covers")


def _cover_image(content: bytes) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except (OSError, ValueError):
        return None
    if image.width < 240 or image.height < 180:
        return None
    return image.convert("RGB")


def _add_background(canvas: Image.Image, backdrops: list[bytes], topic: str) -> None:
    accent, secondary, base = PALETTES[topic]
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = canvas.size
    for y in range(0, height, 6):
        ratio = y / max(1, height - 1)
        color = tuple(int(base[index] * (1 - ratio) + accent[index] * ratio * 0.18) for index in range(3))
        draw.rectangle((0, y, width, y + 6), fill=(*color, 255))

    decoded = [image for content in backdrops if (image := _cover_image(content))]
    if decoded:
        hero = ImageOps.fit(decoded[0], canvas.size, method=Image.Resampling.LANCZOS)
        hero = ImageOps.colorize(hero.convert("L"), black=(0, 4, 2), white=accent)
        hero = hero.filter(ImageFilter.GaussianBlur(radius=14)).convert("RGBA")
        hero.putalpha(150)
        canvas.alpha_composite(hero)
    if len(decoded) > 1:
        panel = ImageOps.fit(decoded[1], (660, 520), method=Image.Resampling.LANCZOS)
        panel = ImageOps.colorize(panel.convert("L"), black=(0, 5, 2), white=secondary)
        panel = panel.filter(ImageFilter.GaussianBlur(radius=2)).convert("RGBA")
        panel.putalpha(125)
        canvas.alpha_composite(panel, (930, 145))

    draw = ImageDraw.Draw(canvas, "RGBA")
    for x in range(0, width, 16):
        alpha = int(232 * (1 - x / width))
        draw.rectangle((x, 0, x + 16, height), fill=(3, 4, 7, max(70, alpha)))
    draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 35))
    for index in range(9):
        x = 960 + (index * 83) % 590
        y = 90 + (index * 137) % 690
        draw.line((x, y, x + 150, y), fill=(*secondary, 75), width=3)
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(*accent, 130))


def _mascot(pose: str) -> Image.Image:
    """Return a tight head-and-shoulders mascot portrait for the cover."""
    sheet = Image.open(MASCOT_SHEET).convert("RGBA")
    half_width = sheet.width // 2
    half_height = sheet.height // 2
    boxes = {
        "thumbs": (0, 0, half_width, half_height),
        "excited": (half_width, 0, sheet.width, half_height),
        "alert": (0, half_height, half_width, sheet.height),
        "thoughtful": (half_width, half_height, sheet.width, sheet.height),
    }
    image = sheet.crop(boxes[pose])
    alpha_box = image.getchannel("A").getbbox()
    if alpha_box:
        image = image.crop(alpha_box)
    # Let the bust dominate the right side of the poster. Upscaling is
    # intentional: hands may overlap the story panel while copy stays above.
    scale = min(1160 / image.width, 880 / image.height)
    image = image.resize(
        (round(image.width * scale), round(image.height * scale)),
        Image.Resampling.LANCZOS,
    )

    # The production sprite already contains material-aware orange rim and
    # bounce light. Keep it intact instead of flattening the highlights with
    # a per-pixel colour replacement.
    return image


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return int(draw.textbbox((0, 0), text, font=font)[2])


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    width: int,
) -> str:
    """Fit text on one line without ever crossing the supplied pixel width."""
    text = SPACE_RE.sub(" ", text).strip()
    if not text or width <= 0:
        return ""
    if _text_width(draw, text, font) <= width:
        return text

    ellipsis = "…"
    available = width - _text_width(draw, ellipsis, font)
    if available <= 0:
        return ellipsis if _text_width(draw, ellipsis, font) <= width else ""

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _text_width(draw, text[:middle].rstrip(), font) <= available:
            low = middle
        else:
            high = middle - 1
    fitted = text[:low].rstrip(" ,;:—-")
    if " " in fitted and low < len(text) and text[low : low + 1] not in {"", " "}:
        fitted = fitted.rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{fitted}{ellipsis}" if fitted else ellipsis


def _headline_parts(text: str, labels: tuple[str, ...]) -> tuple[str, str]:
    for label in labels:
        if text.casefold().startswith(label.casefold()):
            suffix = text[len(label) :].lstrip(" :—-")
            return label, suffix
    if ":" in text:
        prefix, suffix = text.split(":", 1)
        if len(prefix) <= 22:
            return prefix.strip(), suffix.strip()
    return "", text


def render_digest_cover(
    spec: DigestCoverSpec,
    backdrops: list[bytes] | None = None,
) -> bytes:
    width, height = COVER_SIZE
    canvas = Image.new("RGBA", COVER_SIZE, (*PALETTES[spec.topic][2], 255))
    _add_background(canvas, backdrops or [], spec.topic)

    # Place the mascot below all typography so its enlarged hands and
    # shoulders can enter the copy area without hurting readability.
    mascot = _mascot(spec.pose)
    # Crop the mascot deliberately at the right edge. The face remains the
    # visual anchor while only the near hand reaches into the story panel.
    mascot_x = width - mascot.width + 268
    mascot_y = height - mascot.height + 18

    # Build the orange halo on a padded layer so the blur is not clipped at
    # the tightly cropped edges of the mascot sprite.
    glow_pad = 58
    glow_size = (mascot.width + glow_pad * 2, mascot.height + glow_pad * 2)
    glow_source = Image.new("L", glow_size, 0)
    glow_source.paste(mascot.getchannel("A"), (glow_pad, glow_pad))
    for radius, opacity in ((30, 85), (12, 165)):
        glow_mask = glow_source.filter(ImageFilter.GaussianBlur(radius=radius))
        glow_mask = glow_mask.point(
            lambda value, limit=opacity: min(limit, round(value * limit / 255))
        )
        glow = Image.new("RGBA", glow_size, (*ORANGE, 0))
        glow.putalpha(glow_mask)
        canvas.alpha_composite(glow, (mascot_x - glow_pad, mascot_y - glow_pad))
    canvas.alpha_composite(mascot, (mascot_x, mascot_y))

    draw = ImageDraw.Draw(canvas, "RGBA")
    accent, secondary, _ = PALETTES[spec.topic]

    title_font = _font(94, bold=True, condensed=True)
    headline_font = _font(31, bold=True, condensed=True)
    label_font = _font(27, bold=True, condensed=True)
    brand_font = _font(41, bold=True, condensed=True)

    title_polygon = ((38, 75), (1060, 49), (1094, 190), (16, 221))
    title_glow = Image.new("RGBA", COVER_SIZE, (0, 0, 0, 0))
    title_glow_draw = ImageDraw.Draw(title_glow, "RGBA")
    title_glow_draw.line(
        (*title_polygon, title_polygon[0]),
        fill=(*ORANGE, 235),
        width=11,
        joint="curve",
    )
    title_glow = title_glow.filter(ImageFilter.GaussianBlur(radius=10))
    canvas.alpha_composite(title_glow)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.polygon(title_polygon, fill=(*ORANGE, 252))
    draw.line(
        (*title_polygon, title_polygon[0]),
        fill=(*ORANGE, 255),
        width=4,
        joint="curve",
    )
    draw.text(
        (54, 79),
        "ТЕХНОДАЙДЖЕСТ",
        font=title_font,
        fill=(4, 5, 7, 255),
        stroke_width=1,
        stroke_fill=(4, 5, 7),
    )

    story_panel = Image.new("RGBA", COVER_SIZE, (0, 0, 0, 0))
    story_panel_draw = ImageDraw.Draw(story_panel, "RGBA")
    story_panel_draw.rounded_rectangle(
        (28, 238, 1006, 730),
        radius=20,
        fill=(2, 3, 6, 128),
        outline=(*accent, 155),
        width=2,
    )
    canvas.alpha_composite(story_panel)
    # Draw all story copy on its own clipped layer. The dynamic line budget
    # allows a second line only when it still leaves room for every remaining
    # item; otherwise the item is safely shortened with an ellipsis.
    story_copy = Image.new("RGBA", COVER_SIZE, (0, 0, 0, 0))
    story_draw = ImageDraw.Draw(story_copy, "RGBA")
    headlines = spec.headlines[:6]
    y = 262
    row_stride = 71
    second_line_offset = 37
    text_height = 38
    story_text_bottom = 704
    for position, headline in enumerate(headlines):
        index = position + 1
        remaining = len(headlines) - position
        can_use_second_line = (
            y
            + (remaining - 1) * row_stride
            + text_height
            + second_line_offset
            <= story_text_bottom
        )
        number_box = (48, y + 2, 91, y + 44)
        story_draw.rounded_rectangle(number_box, radius=6, fill=(*accent, 255))
        number = str(index)
        number_width = _text_width(story_draw, number, headline_font)
        story_draw.text(
            (70 - number_width / 2, y + 2),
            number,
            font=headline_font,
            fill=(0, 0, 0, 255),
        )

        prefix, suffix = _headline_parts(headline, spec.labels)
        line_x = 111
        if prefix:
            prefix = _ellipsize(story_draw, prefix, headline_font, 250)
            story_draw.text(
                (line_x, y),
                prefix,
                font=headline_font,
                fill=(*accent, 255),
            )
            line_x += _text_width(story_draw, prefix, headline_font) + 11
        available = 970 - line_x
        suffix_lines = _wrap(story_draw, suffix, headline_font, max(240, available))
        used_second_line = False
        if prefix and suffix_lines:
            first = (
                suffix_lines[0]
                if can_use_second_line and len(suffix_lines) > 1
                else _ellipsize(story_draw, suffix, headline_font, available)
            )
            story_draw.text(
                (line_x, y),
                first,
                font=headline_font,
                fill=(220, 255, 224, 255),
            )
            if can_use_second_line and len(suffix_lines) > 1:
                remainder = _ellipsize(
                    story_draw,
                    " ".join(suffix_lines[1:]),
                    headline_font,
                    859,
                )
                story_draw.text(
                    (111, y + second_line_offset),
                    remainder,
                    font=headline_font,
                    fill=(220, 255, 224, 255),
                )
                used_second_line = True
        else:
            lines = _wrap(story_draw, suffix, headline_font, 846)
            first = (
                lines[0]
                if can_use_second_line and len(lines) > 1
                else _ellipsize(story_draw, suffix, headline_font, 846)
            )
            story_draw.text(
                (111, y),
                first,
                font=headline_font,
                fill=(220, 255, 224, 255),
            )
            if can_use_second_line and len(lines) > 1:
                second = _ellipsize(
                    story_draw,
                    " ".join(lines[1:]),
                    headline_font,
                    846,
                )
                story_draw.text(
                    (111, y + second_line_offset),
                    second,
                    font=headline_font,
                    fill=(220, 255, 224, 255),
                )
                used_second_line = True
        y += row_stride + (second_line_offset if used_second_line else 0)

    story_clip = Image.new("L", COVER_SIZE, 0)
    ImageDraw.Draw(story_clip).rectangle((28, 238, 1006, 730), fill=255)
    story_copy.putalpha(
        Image.composite(
            story_copy.getchannel("A"),
            Image.new("L", COVER_SIZE, 0),
            story_clip,
        )
    )
    canvas.alpha_composite(story_copy)

    labels = "  •  ".join(spec.labels)
    labels_panel = Image.new("RGBA", COVER_SIZE, (0, 0, 0, 0))
    labels_panel_draw = ImageDraw.Draw(labels_panel, "RGBA")
    labels_panel_draw.rounded_rectangle(
        (28, 749, 1006, 817),
        radius=14,
        fill=(0, 0, 0, 128),
        outline=(*secondary, 155),
        width=2,
    )
    canvas.alpha_composite(labels_panel)
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.text((55, 764), labels, font=label_font, fill=(*secondary, 255))

    draw.polygon(((27, 839), (458, 825), (484, 891), (16, 897)), fill=(*RED, 250))
    draw.rectangle((54, 850, 87, 883), fill=(0, 9, 3, 255))
    draw.rectangle((62, 858, 79, 875), fill=(255, 255, 255, 255))
    draw.text((104, 836), "IT Vibe News", font=brand_font, fill=(255, 255, 255, 255))

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=91, optimize=True, progressive=True)
    return output.getvalue()


def cover_filename(spec: DigestCoverSpec, post: GeneratedPost, variation: int) -> str:
    fingerprint = hashlib.sha256(
        f"{post.title}\n{post.body}\n{spec.issue}\n{variation}".encode()
    ).hexdigest()[:18]
    return f"digest-{fingerprint}.jpg"
