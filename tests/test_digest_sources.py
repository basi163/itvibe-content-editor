import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import EditorBot
from app.digest_sources import approved_source, resolve_digest_sources, usable_source_url
from app.llm import DeepSeekClient, FallbackLLMClient, QwenClient, _digest_validation_errors
from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.pipeline import ContentPipeline

CANDIDATE = StoryCandidate("telegram_web", "https://t.me/news/1", "Digest", "1. First\n2. Second")
EVIDENCE = "Today we announced a new processor with improved performance."


def selection(index=0, page=0, **changes):
    return {"index": index, "page_index": page, "kind": "company", "language": "en",
            "is_primary": True, "supports_item": True, "evidence": EVIDENCE, **changes}


@pytest.mark.parametrize("url", [
    "https://www.ithome.com/0/123/456.htm", "https://www.nbd.com.cn/articles/1.html",
    "https://www.stcn.com/article/1", "https://weibo.com/123/456",
    "https://passport.weibo.com/login", "https://t.me/news/1", "https://apple.com/",
])
def test_reposts_social_pages_and_homepages_are_not_sources(url):
    assert not usable_source_url(url)


@pytest.mark.parametrize(("changes", "error", "expected"), [
    ({}, None, True),
    ({"is_primary": False}, None, False),
    ({"supports_item": False}, None, False),
    ({"evidence": "An invented quote that the page does not contain"}, None, False),
    ({"evidence": "Today"}, None, False),
    ({"kind": "aggregator"}, None, False),
    ({"language": "zh", "kind": "original_reporting"}, None, False),
    ({"language": "zh", "kind": "company"}, None, True),
    ({"language": "zh", "kind": "government"}, None, True),
    ({"kind": "original_reporting"}, None, True),
    ({}, "HTTP 404", False),
])
def test_approval_requires_actual_primary_page_evidence(changes, error, expected):
    document = SourceDocument("https://company.example/news/release", text=EVIDENCE, error=error)
    assert approved_source(selection(**changes), document) is expected


def test_chinese_repost_cannot_be_approved_as_english():
    document = SourceDocument("https://media.example/story", text=EVIDENCE + "科技新闻" * 60)
    assert not approved_source(selection(kind="original_reporting"), document)


@pytest.mark.asyncio
async def test_resolution_keeps_item_indices_prefers_english_and_ignores_bad_matches():
    seeds = [SourceDocument("https://www.ithome.com/story", text=EVIDENCE, linkable=True),
             SourceDocument("https://company.example/other", text=EVIDENCE, linkable=True)]
    documents = {
        "https://company.example/zh/release": SourceDocument(
            "https://company.example/zh/release", text=EVIDENCE + "科技新闻" * 60),
        "https://company.example/en/release": SourceDocument(
            "https://company.example/en/release", text=EVIDENCE),
    }
    extractor = SimpleNamespace(fetch_document=AsyncMock(side_effect=lambda url: documents[url]))
    llm = SimpleNamespace(
        discover_digest_sources=AsyncMock(return_value={"items": [
            {"index": 0, "urls": list(documents)},
        ]}),
        review_digest_sources=AsyncMock(return_value={"selections": [
            selection(page=0, language="zh"), selection(page=1),
            selection(index=1, supports_item=False),
            selection(index=1, page=1),  # page 1 belongs to another item, never borrow it
            selection(index=-1),
        ]}),
    )
    resolved, audit = await resolve_digest_sources(CANDIDATE, seeds, extractor, llm)
    assert resolved[0].url == "https://company.example/en/release"
    assert resolved[0].linkable
    assert resolved[1].url == seeds[1].url and not resolved[1].linkable
    assert audit["unresolved"] == [1]
    assert all(seed.linkable for seed in seeds)  # no mutation of retained context
    assert extractor.fetch_document.await_count == 2


@pytest.mark.asyncio
async def test_search_failure_never_restores_unverified_seed_links():
    llm = SimpleNamespace(discover_digest_sources=AsyncMock(side_effect=RuntimeError("offline")))
    resolved, audit = await resolve_digest_sources(
        CANDIDATE, [SourceDocument("https://example.com/story", linkable=True)], None, llm,
    )
    assert not resolved[0].linkable
    assert audit["unresolved"] == [0]
    assert audit["lookup_error"] == "RuntimeError"


def test_writer_allows_missing_primary_without_shifting_link_to_another_item():
    post = GeneratedPost("Новости технологий", "1. Первая новость без ссылки.\n\n"
                         "2. [[source:1|Вторая новость]] со ссылкой.", ["#IT", "#ИИ", "#новости"])
    assert _digest_validation_errors(post, 2000, 2, [1]) == []
    post.body = "1. [[source:1|Первая новость]] с чужой ссылкой.\n\n2. Вторая новость."
    assert any("номер" in error for error in _digest_validation_errors(post, 2000, 2, [1]))
    post.body = "1. [[source:0|Первая новость]] с запрещённой ссылкой.\n\n2. Вторая новость."
    assert any("разрешённых" in error for error in _digest_validation_errors(post, 2000, 2, [1]))
    post.body = "1. [[source:0|Первая новость]] со ссылкой."
    assert any("все пункты" in error for error in _digest_validation_errors(post, 2000, 2, [0]))


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", [QwenClient, DeepSeekClient])
async def test_discovery_uses_provider_web_search(client_type):
    client = client_type(api_key="test", base_url="https://example.com/v1",
                         triage_model="triage", writer_model="writer", max_post_chars=2000)
    await client.close()
    chat = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"items": []}'))]))
    responses = AsyncMock(return_value=SimpleNamespace(output_text='{"items": []}', status="completed"))
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=chat)),
                                    responses=SimpleNamespace(create=responses))
    assert await client.discover_digest_sources(CANDIDATE, []) == {"items": []}
    if client_type is QwenClient:
        assert chat.call_args.kwargs["extra_body"]["enable_search"] is True
    else:
        assert responses.call_args.kwargs["tools"] == [{"type": "web_search"}]


@pytest.mark.asyncio
async def test_lookup_and_review_use_fallback_provider():
    primary = SimpleNamespace(discover_digest_sources=AsyncMock(side_effect=RuntimeError("offline")))
    fallback = SimpleNamespace(discover_digest_sources=AsyncMock(return_value={"items": []}),
                               review_digest_sources=AsyncMock(return_value={"selections": []}))
    client = FallbackLLMClient(primary, fallback)
    assert await client.discover_digest_sources(CANDIDATE, []) == {"items": []}
    assert await client.review_digest_sources(CANDIDATE, []) == {"selections": []}


@pytest.mark.asyncio
async def test_regeneration_migrates_old_digest_sources_atomically(monkeypatch):
    old = SourceDocument("https://www.nbd.com.cn/articles/1.html", linkable=True)
    new = SourceDocument("https://company.example/release", text=EVIDENCE, linkable=True)
    draft = {"material": {"candidate": CANDIDATE.to_dict(), "sources": [old.to_dict()]},
             "triage": TriageDecision(True, 1.0, "digest", "").to_dict()}
    repository = SimpleNamespace(get_draft=AsyncMock(return_value=draft), update_draft=AsyncMock())
    llm = SimpleNamespace(generate_digest=AsyncMock(return_value=GeneratedPost(
        "Новые технологии", "1. [[source:0|Компания представила процессор]].", ["#IT", "#ИИ", "#новости"])))
    resolver = AsyncMock(return_value=([new], {"policy_version": 1, "unresolved": []}))
    monkeypatch.setattr("app.pipeline.resolve_digest_sources", resolver)
    pipeline = ContentPipeline(settings=SimpleNamespace(max_post_chars=2000),
                               repository=repository, extractor=None, llm=llm)
    assert await pipeline.regenerate(10)
    updated = repository.update_draft.call_args.kwargs
    assert json.loads(updated["sources_json"])[0]["url"] == new.url
    material = json.loads(updated["material_json"])
    assert material["digest_seed_sources"][0]["url"] == old.url
    assert material["digest_source_review"]["unresolved"] == []
    repository.update_draft.reset_mock()
    llm.generate_digest.side_effect = RuntimeError("writer offline")
    with pytest.raises(RuntimeError):
        await pipeline.regenerate(10)
    repository.update_draft.assert_not_awaited()


def test_editor_shows_unresolved_source_count_without_blocking_controls():
    keyboard = EditorBot._editor_keyboard({
        "id": 12, "status": "draft", "material": {"digest_source_review": {"unresolved": [1, 3]}},
    })
    assert keyboard.inline_keyboard[0][0].text == "⚠️ Без первоисточника — 2"
    assert keyboard.inline_keyboard[0][0].callback_data == "sources:12"
    assert any(button.text == "Опубликовать" for row in keyboard.inline_keyboard for button in row)
