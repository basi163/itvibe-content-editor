import pytest

from app.models import GeneratedPost, SourceDocument
from app.prompts import render_writer_system_prompt
from app.renderer import render_post, validate_post

SOURCES = [
    SourceDocument(
        url="https://example.com/research",
        title="Research",
        text="Verified source",
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
