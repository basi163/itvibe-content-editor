import pytest

from app.models import GeneratedPost, SourceDocument
from app.prompts import render_writer_system_prompt
from app.renderer import render_post, validate_post

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
    assert '<a href="https://example.com/research">официальной публикации</a>' in rendered.html
    assert "https://" not in rendered.visible_text


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
    assert "<a href=" not in rendered.html
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
