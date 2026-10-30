from io import BytesIO

from PIL import Image

from app.digest_cover import (
    COVER_SIZE,
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


def test_digest_cover_renders_publishable_jpeg() -> None:
    content = render_digest_cover(
        build_digest_cover_spec("Технодайджест #122", _post())
    )

    image = Image.open(BytesIO(content))
    assert image.format == "JPEG"
    assert image.size == COVER_SIZE
    assert len(content) > 50_000
