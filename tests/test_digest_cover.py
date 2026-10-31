from io import BytesIO

from PIL import Image

from app.digest_cover import (
    COVER_SIZE,
    PALETTES,
    _mascot,
    build_digest_cover_spec,
    digest_headlines,
    render_digest_cover,
)
from app.models import GeneratedPost


def _post() -> GeneratedPost:
    return GeneratedPost(
        "Технодайджест #122",
        (
            "1. [[source:0|Apple представила сенсорный MacBook Pro]] с OLED.\n\n"
            "2. [[source:1|NVIDIA запустила платформу для ИИ-агентов]].\n\n"
            "3. [[source:2|Xiaomi открыла код системы умного дома]]."
        ),
        ["#ИИ", "#технологии", "#гаджеты"],
    )


def test_digest_cover_spec_uses_issue_topics_and_brand_labels() -> None:
    spec = build_digest_cover_spec("Технодайджест #122", _post())

    assert spec.issue == "#122"
    assert spec.topic in {"ai", "gadgets"}
    assert spec.pose in {"thumbs", "excited"}
    assert spec.labels == ("Apple", "NVIDIA", "Xiaomi")
    assert len(spec.headlines) == 3
    assert "[[source:" not in spec.headlines[0]


def test_digest_headlines_only_uses_numbered_items() -> None:
    headlines = digest_headlines(
        "Вводный абзац.\n\n1. Первая новость.\n\n2. Вторая новость."
    )

    assert headlines == ("Первая новость.", "Вторая новость.")


def test_digest_palettes_use_matrix_green_accents() -> None:
    for accent, secondary, _base in PALETTES.values():
        assert accent[1] > accent[0]
        assert accent[1] > accent[2]
        assert secondary[1] > secondary[0]
        assert secondary[1] > secondary[2]


def test_mascot_is_scaled_to_dominate_the_right_side() -> None:
    mascot = _mascot("excited")

    assert mascot.width >= 1_100
    assert mascot.height >= 650


def test_mascot_sheet_uses_material_aware_orange_light() -> None:
    mascot = _mascot("excited")
    pixels = mascot.tobytes()

    green_highlights = sum(
        pixels[offset + 1] > pixels[offset] + 16
        and pixels[offset + 1] > pixels[offset + 2] + 10
        and pixels[offset + 1] > 34
        for offset in range(0, len(pixels), 4)
        if pixels[offset + 3]
    )
    orange_highlights = sum(
        pixels[offset] > pixels[offset + 1] * 1.45
        and pixels[offset + 1] > pixels[offset + 2] * 2
        and pixels[offset] > 80
        for offset in range(0, len(pixels), 4)
        if pixels[offset + 3]
    )

    # A few neutral anti-aliased pixels can satisfy the broad green detector,
    # but the production sheet must no longer contain a visible green rim.
    assert green_highlights < 1_000
    assert orange_highlights > 1_000


def test_digest_cover_renders_publishable_jpeg() -> None:
    content = render_digest_cover(
        build_digest_cover_spec("Технодайджест #122", _post())
    )

    image = Image.open(BytesIO(content))
    assert image.format == "JPEG"
    assert image.size == COVER_SIZE
    assert len(content) > 50_000
    # The issue number remains internal metadata but is intentionally omitted
    # from the artwork so independently sourced digests cannot look misnumbered.
    red, green, blue = image.getpixel((1500, 80))
    assert not (red > 180 and red > green * 1.8 and red > blue * 1.4)
