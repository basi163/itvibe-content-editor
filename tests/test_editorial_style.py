import json
from unittest.mock import AsyncMock

import pytest

from app.llm import DeepSeekClient, QwenClient
from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision

RULES = 'Дополнение redaktor-ru. Убери декоративные противопоставления. Сохрани разные факты.'


@pytest.mark.asyncio
@pytest.mark.parametrize('client_type', [QwenClient, DeepSeekClient])
async def test_writing_runs_editor_after_generation_for_both_providers(client_type):
    client = client_type(api_key='test', base_url='https://example.com/v1',
                         triage_model='triage', writer_model='writer', max_post_chars=2000,
                         editorial_rules_provider=AsyncMock(return_value=RULES))
    raw = GeneratedPost('Новая версия', 'Это не просто обновление, а новый этап.', ['#IT', '#софт', '#новости'])
    final = {'title': 'Новая версия', 'body': 'Разработчики выпустили новую версию.', 'hashtags': ['#чужой']}
    client._completion = AsyncMock(side_effect=[raw.to_dict(), final])
    try:
        result = await client.generate(StoryCandidate('test', '', 'Версия', 'Выпущена новая версия.'),
                                       [], TriageDecision(True, 1, 'software', ''))
        assert result.body == final['body']
        assert result.hashtags == raw.hashtags
        assert client._completion.await_count == 2
        payload = json.loads(client._completion.call_args.kwargs['user'])
        assert payload['current_post'] == raw.to_dict()
        assert payload['reference_material']['candidate']['text'] == 'Выпущена новая версия.'
        assert RULES in client._completion.call_args.kwargs['system']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_style_edit_retries_link_loss_and_preserves_digest_item_without_source():
    client = QwenClient(api_key='test', base_url='https://example.com/v1',
                        triage_model='triage', writer_model='writer', max_post_chars=2000)
    post = GeneratedPost('Дайджест', '1. [[source:0|Компания выпустила версию]].\n\n'
                         '2. Исследователи изучают новый подход.', ['#IT', '#наука', '#новости'])
    lost_item = {'title': post.title, 'body': '1. [[source:0|Компания выпустила версию]].'}
    client._completion = AsyncMock(side_effect=[lost_item, post.to_dict()])
    try:
        result = await client._edit_style(post, {}, [SourceDocument('https://example.com/1', linkable=True),
                                                     SourceDocument('https://example.com/2')],
                                          editorial_rules=RULES, is_digest=True)
        assert result == post
        assert client._completion.await_count == 2
        assert json.loads(client._completion.call_args.kwargs['user'])['validation_errors']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_style_edit_cannot_silently_remove_article_link():
    client = QwenClient(api_key='test', base_url='https://example.com/v1',
                        triage_model='triage', writer_model='writer', max_post_chars=2000)
    post = GeneratedPost('Новая версия', '[[source:0|Вышла новая версия]].', ['#IT', '#софт', '#новости'])
    client._completion = AsyncMock(return_value={'title': post.title, 'body': 'Вышла новая версия.'})
    try:
        with pytest.raises(ValueError, match='Style edit failed'):
            await client._edit_style(post, {}, [SourceDocument('https://example.com/1', linkable=True)],
                                     editorial_rules=RULES)
        assert client._completion.await_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_optional_editor_skips_extra_call_without_redaktor_rules():
    client = QwenClient(api_key='test', base_url='https://example.com/v1',
                        triage_model='triage', writer_model='writer', max_post_chars=2000)
    post = GeneratedPost('Новая версия', 'Вышла новая версия.', ['#IT', '#софт', '#новости'])
    client._completion = AsyncMock()
    try:
        assert await client._edit_style(post, {}, [], editorial_rules='') is post
        client._completion.assert_not_awaited()
    finally:
        await client.close()
