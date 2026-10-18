import pytest

from app.models import GeneratedPost, SourceDocument
from app.prompts import render_writer_system_prompt
from app.renderer import (
    SIGNATURE_TEXT,
    SIGNATURE_URL,
    normalize_generated_post,
    render_post,
    validate_post,
)

SOURCES = [
    SourceDocument(
        url="https://example.com/research",
        title="Research",
        text="Verified source",
        linkable=True,
    )
]


def test_render_valid_post_with_contextual_link() -> None:
    post = GeneratedPost(
        title="Новая архитектура ускоряет локальные модели",
        body=(
            "Разработчики описали технологию в "
            "[[source:0|официальной публикации]]. Она снижает требования к памяти."
        ),
        hashtags=["#ИИ", "#чипы", "#технологии"],
    )

    rendered = render_post(post, SOURCES, 2000)

    assert rendered.html.startswith("<b>Новая архитектура")
    assert rendered.rich_html.startswith("<p><b>Новая архитектура")
    assert "<p>Разработчики описали" in rendered.rich_html
    assert '<a href="https://example.com/research">официальной публикации</a>' in rendered.html
    assert rendered.html.endswith(
        f'<a href="{SIGNATURE_URL}">{SIGNATURE_TEXT}</a>'
    )
    assert f'<p><a href="{SIGNATURE_URL}">{SIGNATURE_TEXT}</a></p>' in rendered.rich_html
    assert rendered.visible_text.endswith(SIGNATURE_TEXT)
    assert "https://" not in rendered.visible_text


def test_renderer_adds_exactly_one_blank_line_between_paragraphs() -> None:
    post = GeneratedPost(
        title="Заголовок",
        body="Первый абзац.\nВторой абзац.\n\n\nТретий абзац.",
        hashtags=["#ИИ", "#наука", "#технологии"],
    )

    rendered = render_post(post, SOURCES, 2000)

    assert "Первый абзац.\n\nВторой абзац.\n\nТретий абзац." in rendered.html
    assert "<p>Первый абзац.</p><p><br></p><p>Второй абзац.</p>" in rendered.rich_html
    assert "<h2>" not in rendered.rich_html


def test_signature_is_separated_from_hashtags_by_blank_line() -> None:
    post = GeneratedPost(
        title="Заголовок",
        body="Основной текст.",
        hashtags=["#ИИ", "#наука", "#технологии"],
    )

    rendered = render_post(post, SOURCES, 2000)

    assert (
        '#ИИ #наука #технологии\n\n'
        f'<a href="{SIGNATURE_URL}">{SIGNATURE_TEXT}</a>'
    ) in rendered.html
    assert (
        "<p>#ИИ #наука #технологии</p><p><br></p>"
        f'<p><a href="{SIGNATURE_URL}">{SIGNATURE_TEXT}</a></p>'
    ) in rendered.rich_html


def test_local_normalizer_repairs_formal_model_errors() -> None:
    post = GeneratedPost(
        title="Новость: важное обновление 🚀",
        body=(
            "И вот тут начинается самое важное: подробности — здесь — и — там — снова. "
            "Ссылка https://example.com. "
            "[[source:0|Первый источник]] [[source:1|Второй источник]] "
            "[[source:2|Третий источник]] [[source:3|Четвертый источник]]"
        ),
        hashtags=["ИИ", "#ИИ", "#open-source", "🚀"],
    )

    normalized = normalize_generated_post(post, 2000)

    assert validate_post(normalized, 2000) == []
    assert normalized.title == "Новость, важное обновление"
    assert len(normalized.hashtags) == 3
    assert normalized.body.count("[[source:") == 3
    assert "https://" not in normalized.body


def test_local_normalizer_trims_post_for_signature_aware_limit() -> None:
    post = GeneratedPost(
        title="Короткий заголовок",
        body="Очень длинный текст " * 80,
        hashtags=["#ИИ", "#наука", "#технологии"],
    )

    normalized = normalize_generated_post(post, 300)

    assert validate_post(normalized, 300) == []
    assert render_post(normalized, [], 300).char_count <= 300


def test_local_normalizer_removes_malformed_source_markers() -> None:
    post = GeneratedPost(
        title="Заголовок",
        body=(
            "Факт из источника [[source,0]]. "
            "Верная ссылка [[source:0|официальная публикация]]."
        ),
        hashtags=["#ИИ", "#наука", "#технологии"],
    )

    assert any("маркер" in error for error in validate_post(post, 2000))

    normalized = normalize_generated_post(post, 2000)

    assert "[[source,0]]" not in normalized.body
    assert "источника ." not in normalized.body
    assert "[[source:0|официальная публикация]]" in normalized.body
    assert validate_post(normalized, 2000) == []


def test_local_normalizer_removes_meta_commentary_lead_ins() -> None:
    post = GeneratedPost(
        title="Заголовок",
        body=(
            "Этот шаг показывает, что экосистема Linux меняется.\n\n"
            "Мы наблюдаем классический пример того, как технология меняет рынок."
        ),
        hashtags=["#Linux", "#технологии", "#новости"],
    )

    errors = validate_post(post, 2000)
    assert any("этот шаг показывает" in error.casefold() for error in errors)

    normalized = normalize_generated_post(post, 2000)

    assert normalized.body.startswith("Экосистема Linux меняется.")
    assert "\n\nТехнология меняет рынок." in normalized.body
    assert validate_post(normalized, 2000) == []


def test_writer_prompt_renders_limit_without_consuming_json_braces() -> None:
    prompt = render_writer_system_prompt(2000)

    assert "{max_chars}" not in prompt
    assert '"title": ""' in prompt


def test_renderer_does_not_link_non_primary_source() -> None:
    sources = [SourceDocument(url="https://www.solidot.org/story?sid=1", linkable=False)]
    post = GeneratedPost(
        "Заголовок",
        "Подробности описаны в [[source:0|материале]].",
        ["#ИИ", "#наука", "#технологии"],
    )

    rendered = render_post(post, sources, 2000)

    assert "solidot" not in rendered.html.casefold()
    assert rendered.html.count("<a href=") == 1
    assert f'<a href="{SIGNATURE_URL}">{SIGNATURE_TEXT}</a>' in rendered.html
    assert "в материале" in rendered.visible_text


def test_validation_rejects_visible_url() -> None:
    post = GeneratedPost(
        "Заголовок",
        "Ссылка https://www.solidot.org/story?sid=1",
        ["#ИИ", "#наука", "#технологии"],
    )

    assert any("URL" in error for error in validate_post(post, 2000))


@pytest.mark.parametrize(
    "post,error",
    [
        (
            GeneratedPost("Заголовок: ошибка", "Текст", ["#ИИ", "#чипы", "#наука"]),
            "двоеточие",
        ),
        (
            GeneratedPost("Заголовок", "И вот тут начинается самое важное", ["#ИИ", "#чипы", "#наука"]),
            "Запрещенный шаблон",
        ),
        (
            GeneratedPost("Заголовок", "Текст 🚀", ["#ИИ", "#чипы", "#наука"]),
            "эмодзи",
        ),
    ],
)
def test_validation_rejects_style_violations(post: GeneratedPost, error: str) -> None:
    errors = validate_post(post, 2000)
    assert any(error.casefold() in item.casefold() for item in errors)
