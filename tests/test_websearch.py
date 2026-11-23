import json
from datetime import datetime

import pytest

from conftest import set_valves, websearch
from fake_http import FakeClientFactory, FakeResponse


def mcp_response(text, content_type='application/json'):
    return FakeResponse(json_data={'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'type': 'text', 'text': text}]}})


def sse_response(text):
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'type': 'text', 'text': text}]}})
    return FakeResponse(body=f'event: message\ndata: {payload}\n\n'.encode(), content_type='text/event-stream')


EXA_TEXT = (
    'URL: https://example.com/article\n'
    'Title: Example Article\n'
    'Published: 2025-11-01\n'
    'Text:\n'
    'First paragraph of the article.\n'
    'Second line of text.\n'
    '\n\n---\n\n'
    'URL: https://example.org/page\n'
    'Title: Second Page\n'
    'Highlights:\n'
    'A highlight line.\n'
)

TAVILY_JSON = {
    'results': [
        {'url': 'https://t1.example/', 'title': 'Tavily One', 'content': 't1 body'},
        {'url': 'https://t2.example/', 'title': 'Tavily Two', 'content': 't2 body'},
    ]
}

FIRECRAWL_TEXT = json.dumps(
    {'success': True, 'data': {'web': [{'url': 'https://fc.example/', 'title': 'FC', 'description': 'fc body'}]}}
)

PARALLEL_STRUCTURED = {
    'search_id': 's1',
    'results': [
        {'url': 'https://p1.example/', 'title': 'P One', 'publish_date': '2025-06-01', 'excerpts': ['ex1', 'ex2']},
    ],
}

TINYFISH_TEXT = json.dumps(
    {'results': [{'url': 'https://tf.example/', 'title': 'TF', 'snippet': 'tf body'}]}
)


def run_search(monkeypatch, responses, **valves):
    set_valves(**valves)
    factory = FakeClientFactory(responses)
    monkeypatch.setattr(websearch.httpx, 'AsyncClient', factory)
    import asyncio

    output = asyncio.run(websearch.Tools().web_search('quantum error correction'))
    return output, factory


def deterministic_random(monkeypatch):
    monkeypatch.setattr(websearch.random, 'shuffle', lambda items: None)


# ---------------------------------------------------------------------------
# Provider request shapes
# ---------------------------------------------------------------------------


def test_exa_keyless_request_shape(monkeypatch):
    output, factory = run_search(monkeypatch, [mcp_response(EXA_TEXT)], SELECTION='exa')
    client = factory.clients[0]
    req = client.requests[0]
    assert req['url'] == 'https://mcp.exa.ai/mcp'
    assert req['json']['method'] == 'tools/call'
    assert req['json']['params'] == {'name': 'web_search_exa', 'arguments': {'query': 'quantum error correction', 'numResults': 8}}
    assert 'exaApiKey' not in req['url']
    # Parsed exa blocks
    assert '## [Example Article](https://example.com/article)' in output
    assert '## [Second Page](https://example.org/page)' in output
    assert 'First paragraph of the article.' in output
    assert 'A highlight line.' in output


def test_exa_keyed_uses_api_key_param(monkeypatch, monkeypatch_env=None):
    output, factory = run_search(monkeypatch, [mcp_response(EXA_TEXT)], SELECTION='exa', EXA_API_KEY='secret exa key')
    assert factory.clients[0].requests[0]['url'] == 'https://mcp.exa.ai/mcp?exaApiKey=secret%20exa%20key'


def test_exa_key_from_environment(monkeypatch):
    monkeypatch.setenv('EXA_API_KEY', 'env-key')
    output, factory = run_search(monkeypatch, [mcp_response(EXA_TEXT)], SELECTION='exa')
    assert factory.clients[0].requests[0]['url'] == 'https://mcp.exa.ai/mcp?exaApiKey=env-key'


def test_exa_sse_response(monkeypatch):
    output, factory = run_search(monkeypatch, [sse_response(EXA_TEXT)], SELECTION='exa')
    assert '## [Example Article](https://example.com/article)' in output


def test_tavily_keyless_headers_and_body(monkeypatch):
    output, factory = run_search(monkeypatch, [FakeResponse(json_data=TAVILY_JSON)], SELECTION='tavily')
    req = factory.clients[0].requests[0]
    assert req['url'] == 'https://api.tavily.com/search'
    assert req['headers']['X-Tavily-Access-Mode'] == 'keyless'
    assert 'Authorization' not in req['headers']
    assert req['json'] == {'query': 'quantum error correction', 'search_depth': 'basic', 'chunks_per_source': 3, 'max_results': 8}
    assert '## [Tavily One](https://t1.example/)' in output
    assert 't1 body' in output


def test_tavily_keyed_bearer(monkeypatch):
    output, factory = run_search(monkeypatch, [FakeResponse(json_data=TAVILY_JSON)], SELECTION='tavily', TAVILY_API_KEY='tv-key')
    assert factory.clients[0].requests[0]['headers']['Authorization'] == 'Bearer tv-key'


def test_firecrawl_nested_json(monkeypatch):
    output, factory = run_search(monkeypatch, [mcp_response(FIRECRAWL_TEXT)], SELECTION='firecrawl')
    req = factory.clients[0].requests[0]
    assert req['url'] == 'https://mcp.firecrawl.dev/v2/mcp'
    assert req['json']['params'] == {'name': 'firecrawl_search', 'arguments': {'query': 'quantum error correction', 'limit': 8}}
    assert '## [FC](https://fc.example/)' in output
    assert 'fc body' in output


def test_parallel_structured_content(monkeypatch):
    resp = FakeResponse(json_data={'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': PARALLEL_STRUCTURED}})
    output, factory = run_search(monkeypatch, [resp], SELECTION='parallel')
    req = factory.clients[0].requests[0]
    assert req['url'] == 'https://search.parallel.ai/mcp'
    assert req['json']['params']['arguments'] == {'objective': 'quantum error correction', 'search_queries': ['quantum error correction']}
    assert '## [P One](https://p1.example/)' in output
    assert 'ex1\n\nex2' in output
    assert 'Published: 2025-06-01T00:00:00+00:00' in output


def test_parallel_text_fallback(monkeypatch):
    text = mcp_response(json.dumps(PARALLEL_STRUCTURED))
    output, factory = run_search(monkeypatch, [text], SELECTION='parallel')
    assert '## [P One](https://p1.example/)' in output


def test_tinyfish_keyless(monkeypatch):
    output, factory = run_search(monkeypatch, [mcp_response(TINYFISH_TEXT)], SELECTION='tinyfish')
    req = factory.clients[0].requests[0]
    assert req['url'] == 'https://agent.tinyfish.ai/mcp'
    assert req['headers']['X-TinyFish-Access-Mode'] == 'keyless'
    assert req['json']['params'] == {'name': 'search', 'arguments': {'query': 'quantum error correction'}}
    assert '## [TF](https://tf.example/)' in output
    assert 'tf body' in output


def test_tinyfish_keyed_handshake(monkeypatch):
    init = FakeResponse(
        json_data={'jsonrpc': '2.0', 'id': 1, 'result': {}},
        headers={'Mcp-Session-Id': 'sess-123'},
    )
    notify = FakeResponse(status_code=202, body=b'')
    call = mcp_response(TINYFISH_TEXT)
    output, factory = run_search(monkeypatch, [init, notify, call], SELECTION='tinyfish', TINYFISH_API_KEY='tf-key')
    requests = factory.clients[0].requests
    assert requests[0]['json']['method'] == 'initialize'
    assert requests[0]['headers']['X-API-Key'] == 'tf-key'
    assert requests[1]['json']['method'] == 'notifications/initialized'
    assert requests[1]['headers']['Mcp-Session-Id'] == 'sess-123'
    assert requests[2]['json']['method'] == 'tools/call'
    assert requests[2]['headers']['Mcp-Session-Id'] == 'sess-123'
    assert '## [TF](https://tf.example/)' in output


# ---------------------------------------------------------------------------
# Selection, stickiness, failover, cooldowns
# ---------------------------------------------------------------------------


def test_random_selection_is_sticky(monkeypatch):
    deterministic_random(monkeypatch)
    first = mcp_response(EXA_TEXT)
    second = mcp_response(EXA_TEXT)
    output, factory = None, None

    set_valves(SELECTION='random')
    factory1 = FakeClientFactory([first])
    monkeypatch.setattr(websearch.httpx, 'AsyncClient', factory1)
    import asyncio

    asyncio.run(websearch.Tools().web_search('q'))
    assert websearch._state['sticky'] == 'exa'  # first in deterministic order

    factory2 = FakeClientFactory([second])
    monkeypatch.setattr(websearch.httpx, 'AsyncClient', factory2)
    asyncio.run(websearch.Tools().web_search('q'))
    assert factory2.clients[0].requests[0]['url'] == 'https://mcp.exa.ai/mcp'  # still sticky on exa
    assert websearch._state['sticky'] == 'exa'


def test_random_failover_on_429(monkeypatch):
    deterministic_random(monkeypatch)
    exa_429 = FakeResponse(status_code=429, body=b'', headers={'Retry-After': '30'})
    tavily_ok = FakeResponse(json_data=TAVILY_JSON)
    output, factory = run_search(monkeypatch, [exa_429, tavily_ok], SELECTION='random')
    assert '## [Tavily One](https://t1.example/)' in output
    # exa cooled down for ~30s; sticky moved to tavily
    assert 'exa' in websearch._state['cooldowns']
    import time

    cooldown_until = websearch._state['cooldowns']['exa']
    assert time.time() < cooldown_until <= time.time() + 31
    assert websearch._state['sticky'] == 'tavily'

    # Next call skips exa entirely (it is cooling down and tavily is available).
    output2, factory2 = run_search(monkeypatch, [FakeResponse(json_data=TAVILY_JSON)], SELECTION='random')
    assert all('mcp.exa.ai' not in r['url'] for r in factory2.clients[0].requests)


def test_explicit_provider_does_not_failover(monkeypatch):
    exa_429 = FakeResponse(status_code=429, body=b'', headers={'Retry-After': '30'})
    tavily_ok = FakeResponse(json_data=TAVILY_JSON)
    output, factory = run_search(monkeypatch, [exa_429, tavily_ok], SELECTION='exa')
    assert output == 'Web search rate limited (HTTP 429)'
    assert len(factory.clients) == 1  # only exa was tried
    # cooldown recorded, but sticky must not have changed
    assert websearch._state['sticky'] is None


def test_explicit_401(monkeypatch):
    output, factory = run_search(monkeypatch, [FakeResponse(status_code=401, body=b'')], SELECTION='exa')
    assert output == 'Web search authentication failed (HTTP 401)'


def test_all_providers_fail(monkeypatch):
    deterministic_random(monkeypatch)
    responses = [FakeResponse(status_code=500, body=b'') for _ in range(PROVIDER_COUNT)]
    output, factory = run_search(monkeypatch, responses, SELECTION='random')
    assert output.startswith('Web search request failed. All web search providers failed.')
    assert 'Exa: HTTP 500' in output
    assert 'TinyFish: HTTP 500' in output
    assert len(factory.clients) == PROVIDER_COUNT


PROVIDER_COUNT = len(websearch.PROVIDERS)


def test_disabled_selection(monkeypatch):
    output, factory = run_search(monkeypatch, [], SELECTION='disabled')
    assert 'Web search is disabled' in output
    assert not factory.clients


def test_unknown_selection(monkeypatch):
    output, factory = run_search(monkeypatch, [], SELECTION='bogus')
    assert 'no providers' in output
    assert not factory.clients


def test_retry_after_http_date_header(monkeypatch):
    import time
    from email.utils import format_datetime
    from datetime import timedelta, timezone

    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
    exa_429 = FakeResponse(status_code=429, body=b'', headers={'Retry-After': future})
    tavily_ok = FakeResponse(json_data=TAVILY_JSON)
    run_search(monkeypatch, [exa_429, tavily_ok], SELECTION='exa')
    cooldown_until = websearch._state['cooldowns']['exa']
    assert 115 < cooldown_until - time.time() <= 121


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


def test_output_format_matches_opencode(monkeypatch):
    exa_text = (
        'URL: https://example.com/a\n'
        'Title:  Spaced   Title \n'
        'Published: 2025-01-02T03:04:05Z\n'
        'Text:\n'
        'body here'
    )
    output, factory = run_search(monkeypatch, [mcp_response(exa_text)], SELECTION='exa')
    expected = (
        '## [Spaced Title](https://example.com/a)\n'
        'Published: 2025-01-02T03:04:05+00:00\n'
        '\n'
        'body here'
    )
    assert output == expected


def test_no_results_message(monkeypatch):
    empty = mcp_response('')
    output, factory = run_search(monkeypatch, [empty], SELECTION='exa')
    assert output == 'No search results found. Please try a different query.'


def test_num_results_valve(monkeypatch):
    output, factory = run_search(monkeypatch, [mcp_response(EXA_TEXT)], SELECTION='exa', NUM_RESULTS=3)
    assert factory.clients[0].requests[0]['json']['params']['arguments']['numResults'] == 3


def test_docstring_carries_current_year():
    doc = websearch.Tools.web_search.__doc__
    assert str(datetime.now().year) in doc
    assert ':param query:' in doc


# ---------------------------------------------------------------------------
# Valves schema (the admin UI renders Valves.schema(), exactly as
# GET /api/v1/tools/id/{id}/valves/spec calls it)
# ---------------------------------------------------------------------------


def test_valves_schema_covers_all_valves_with_types_and_defaults():
    schema = websearch.Valves.schema()
    props = schema['properties']
    assert set(props) == {
        'SELECTION', 'NUM_RESULTS', 'TIMEOUT_SECONDS', 'MAX_CONTENT_LENGTH', 'USER_AGENT',
        'EXA_API_KEY', 'TAVILY_API_KEY', 'FIRECRAWL_API_KEY', 'PARALLEL_API_KEY', 'TINYFISH_API_KEY',
    }
    assert props['SELECTION']['type'] == 'string' and props['SELECTION']['default'] == 'random'
    assert props['NUM_RESULTS']['type'] == 'integer' and props['NUM_RESULTS']['default'] == 8
    assert props['TIMEOUT_SECONDS']['default'] == 25
    assert props['MAX_CONTENT_LENGTH']['default'] == 15000
    assert props['USER_AGENT']['default'] == websearch.DEFAULT_USER_AGENT
    # no required fields: every valve has a default, so an empty stored
    # valves dict yields a fully working tool
    assert not schema.get('required')


def test_valves_schema_ui_conventions():
    props = websearch.Valves.schema()['properties']
    # SELECTION renders as a dropdown with the provider choices
    assert props['SELECTION']['input'] == {
        'type': 'select',
        'options': ['random', 'exa', 'tavily', 'firecrawl', 'parallel', 'tinyfish', 'disabled'],
    }
    # API keys render as sensitive inputs
    for key in ('EXA_API_KEY', 'TAVILY_API_KEY', 'FIRECRAWL_API_KEY', 'PARALLEL_API_KEY', 'TINYFISH_API_KEY'):
        assert props[key]['input'] == {'type': 'password'}


def test_valves_construction_defaults_and_unknown_keys():
    v = websearch.Valves()
    assert v.SELECTION == 'random' and v.NUM_RESULTS == 8
    # The app loads valves as Valves(**saved); unknown stored keys must be
    # ignored (pydantic v2 extra='ignore' is the default)
    v2 = websearch.Valves(**{'BOGUS_KEY': 1})
    assert v2.SELECTION == 'random' and v2.NUM_RESULTS == 8
    v3 = websearch.Valves(**{'SELECTION': 'exa', 'NUM_RESULTS': 3})
    assert v3.SELECTION == 'exa' and v3.NUM_RESULTS == 3
