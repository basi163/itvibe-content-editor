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
MASCOT_SHEET = Path(__file__).with_name("assets") / "itvibe_mascot_sheet.png"
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

    # The source portraits have green rim light baked into the clothing and
    # head. Replace those reflected highlights with the poster's warm orange
    # accent while preserving neutral blacks, whites and skin-independent
    # mascot details.
    recolored = bytearray(image.tobytes())
    for offset in range(0, len(recolored), 4):
        red, green, blue, _alpha = recolored[offset : offset + 4]
        if green > red + 16 and green > blue + 10 and green > 34:
            intensity = max(red, green, blue)
            recolored[offset] = intensity
            recolored[offset + 1] = round(intensity * ORANGE[1] / 255)
            recolored[offset + 2] = round(intensity * ORANGE[2] / 255)
    return Image.frombytes("RGBA", image.size, bytes(recolored))


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
    # Crop the mascot deliberately at the right edge. The face sits below the
    # issue badge while only the near hand reaches into the story panel.
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
    issue_font = _font(76, bold=True, condensed=True)
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

    issue_text = spec.issue
    issue_width = _text_width(draw, issue_text, issue_font)
    badge_left = max(1265, width - issue_width - 84)
    badge = ((badge_left, 28), (width - 16, 8), (width - 42, 151), (badge_left - 31, 173))
    draw.polygon(badge, fill=(*RED, 250))
    draw.text(
        (badge_left + 18, 41),
        issue_text,
        font=issue_font,
        fill=(255, 255, 255, 255),
        stroke_width=1,
        stroke_fill=(105, 0, 8),
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
    draw = ImageDraw.Draw(canvas, "RGBA")
    y = 262
    for index, headline in enumerate(spec.headlines[:6], start=1):
        number_box = (48, y + 2, 91, y + 44)
        draw.rounded_rectangle(number_box, radius=6, fill=(*accent, 255))
        number = str(index)
        number_width = _text_width(draw, number, headline_font)
        draw.text((70 - number_width / 2, y + 2), number, font=headline_font, fill=(0, 0, 0, 255))

        prefix, suffix = _headline_parts(headline, spec.labels)
        line_x = 111
        if prefix:
            draw.text((line_x, y), prefix, font=headline_font, fill=(*accent, 255))
            line_x += _text_width(draw, prefix, headline_font) + 11
        available = 970 - line_x
        suffix_lines = _wrap(draw, suffix, headline_font, max(240, available))
        if prefix and suffix_lines:
            first = suffix_lines[0]
            draw.text((line_x, y), first, font=headline_font, fill=(220, 255, 224, 255))
            remainder = " ".join(suffix_lines[1:])
            if remainder:
                draw.text((111, y + 37), remainder, font=headline_font, fill=(220, 255, 224, 255))
                y += 34
        else:
            lines = _wrap(draw, suffix, headline_font, 846)
            draw.text((111, y), lines[0] if lines else suffix, font=headline_font, fill=(220, 255, 224, 255))
            if len(lines) > 1:
                draw.text((111, y + 37), lines[1], font=headline_font, fill=(220, 255, 224, 255))
                y += 34
        y += 71
        if y > 685:
            break

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
