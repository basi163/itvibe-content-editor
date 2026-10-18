import pytest

from app.models import GeneratedPost, SourceDocument
from app.prompts import render_writer_system_prompt
from app.renderer import SIGNATURE_TEXT, SIGNATURE_URL, render_post, validate_post

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
