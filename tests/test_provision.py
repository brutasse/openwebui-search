import httpx
import pytest

from conftest import ROOT, provision

PLUGIN_CONTENT = (ROOT / 'plugins' / 'websearch.py').read_text()

OLD_TOOL = {'id': 'websearch', 'name': 'WebSearch'}


def make_env(db):
    """Build a MockTransport-backed client + call log around a mutable db dict."""
    import json as _json

    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        body = _json.loads(request.content) if request.content else {}
        request_json = body

        if request.method == 'GET' and path == '/api/v1/tools/list':
            return httpx.Response(200, json=db['tools'])
        if request.method == 'GET' and path == '/api/v1/tools/export':
            return httpx.Response(200, json=db['exports'])
        if request.method == 'POST' and path == '/api/v1/tools/create':
            body = request_json
            tool = {
                'id': body['id'],
                'name': body['name'],
                'meta': body['meta'],
                'access_grants': body.get('access_grants') or [],
                'is_active': True,
            }
            db['tools'].append(tool)
            db['exports'].append({'id': tool['id'], 'content': body['content'], 'access_grants': tool['access_grants']})
            return httpx.Response(200, json=tool)
        if request.method == 'POST' and path == '/api/v1/tools/id/websearch/update':
            body = request_json
            for t in db['exports']:
                if t['id'] == body['id']:
                    t['content'] = body['content']
            return httpx.Response(200, json={'id': body['id'], 'name': body['name'], 'content': body['content']})
        if request.method == 'POST' and path == '/api/v1/tools/id/websearch/access/update':
            body = request_json
            for t in db['tools']:
                if t['id'] == 'websearch':
                    t['access_grants'] = body['access_grants']
            return httpx.Response(200, json={'id': 'websearch', 'access_grants': body['access_grants']})
        if request.method == 'GET' and path == '/api/v1/tools/id/websearch/valves':
            return httpx.Response(200, json=db.get('valves') or {})
        if request.method == 'POST' and path == '/api/v1/tools/id/websearch/valves/update':
            db['valves'] = request_json['valves']
            return httpx.Response(200, json=db['valves'])
        if request.method == 'GET' and path == '/api/v1/models/model':
            model_id = request.url.params['id']
            if model_id not in db['models']:
                return httpx.Response(404, json={'detail': 'not found'})
            return httpx.Response(200, json=db['models'][model_id])
        if request.method == 'POST' and path == '/api/v1/models/model/update':
            body = request_json
            db['models'][body['id']] = body
            return httpx.Response(200, json=body)
        if request.method == 'POST' and path == '/api/v1/auths/signin':
            return httpx.Response(200, json={'token': 'jwt-123'})
        return httpx.Response(404, json={'detail': f'unhandled {request.method} {path}'})

    client = httpx.AsyncClient(base_url='http://owui.test', transport=httpx.MockTransport(handler))
    return client, calls


def run(client, grant_groups=(), grant_users=(), model_ids=(), valves=()):
    provisioner = provision.Provisioner(client, PLUGIN_CONTENT)
    import asyncio

    asyncio.run(provisioner.run(list(grant_groups), list(grant_users), list(model_ids), list(valves)))
    return provisioner


def req_json(request):
    import json

    return json.loads(request.content)


def test_creates_tool_with_grants():
    db = {'tools': [], 'exports': [], 'models': {}}
    client, calls = make_env(db)
    run(client, grant_groups=['group-1'])
    create = next(r for r in calls if r.url.path == '/api/v1/tools/create')
    body = req_json(create)
    assert body['id'] == 'websearch'
    assert body['content'] == PLUGIN_CONTENT
    assert body['meta']['description']
    assert {'principal_type': 'group', 'principal_id': 'group-1', 'permission': 'read'} in body['access_grants']
    # grants were supplied at create time, so no separate access update is needed
    assert not [r for r in calls if r.url.path.endswith('/access/update')]


def test_updates_content_when_stale():
    db = {
        'tools': [dict(OLD_TOOL)],
        'exports': [{'id': 'websearch', 'content': 'stale old content', 'access_grants': []}],
        'models': {},
    }
    client, calls = make_env(db)
    run(client)
    update = next(r for r in calls if r.url.path == '/api/v1/tools/id/websearch/update')
    assert req_json(update)['content'] == PLUGIN_CONTENT
    assert not [r for r in calls if r.url.path == '/api/v1/tools/create']


def test_noop_when_content_matches():
    db = {
        'tools': [dict(OLD_TOOL)],
        'exports': [{'id': 'websearch', 'content': PLUGIN_CONTENT, 'access_grants': []}],
        'models': {},
    }
    client, calls = make_env(db)
    actions = run(client).actions
    assert not [r for r in calls if r.url.path == '/api/v1/tools/create']
    assert not [r for r in calls if r.url.path.endswith('/update')]
    assert any('up to date' in a for a in actions)


def test_merges_access_grants():
    db = {
        'tools': [{'id': 'websearch', 'name': 'WebSearch',
                   'access_grants': [{'principal_type': 'group', 'principal_id': 'g1', 'permission': 'read'}]}],
        'exports': [{'id': 'websearch', 'content': PLUGIN_CONTENT, 'access_grants': []}],
        'models': {},
    }
    client, calls = make_env(db)
    run(client, grant_users=['user-2'], grant_groups=['g2'])
    access = next(r for r in calls if r.url.path.endswith('/access/update'))
    granted = req_json(access)['access_grants']
    assert {'principal_type': 'group', 'principal_id': 'g1', 'permission': 'read'} in granted
    assert {'principal_type': 'user', 'principal_id': 'user-2', 'permission': 'read'} in granted
    assert {'principal_type': 'group', 'principal_id': 'g2', 'permission': 'read'} in granted
    assert len(granted) == 3


def test_no_duplicate_grant_updates():
    db = {
        'tools': [{'id': 'websearch', 'name': 'WebSearch',
                   'access_grants': [{'principal_type': 'group', 'principal_id': 'g1', 'permission': 'read'}]}],
        'exports': [{'id': 'websearch', 'content': PLUGIN_CONTENT, 'access_grants': []}],
        'models': {},
    }
    client, calls = make_env(db)
    run(client, grant_groups=['g1'])
    assert not [r for r in calls if r.url.path.endswith('/access/update')]


def test_attaches_to_models_and_preserves_existing_tools():
    db = {
        'tools': [],
        'exports': [],
        'models': {'m1': {'id': 'm1', 'name': 'M1', 'base_model_id': 'base-1',
                          'meta': {'toolIds': ['other-tool']}, 'params': {'temperature': 0.2}}},
    }
    client, calls = make_env(db)
    run(client, model_ids=['m1'])
    update = next(r for r in calls if r.url.path == '/api/v1/models/model/update')
    body = req_json(update)
    assert body['meta']['toolIds'] == ['other-tool', 'websearch']
    assert body['name'] == 'M1'
    assert body['params'] == {'temperature': 0.2}
    assert body['base_model_id'] == 'base-1'


def test_skips_model_when_already_attached():
    db = {
        'tools': [],
        'exports': [],
        'models': {'m1': {'id': 'm1', 'name': 'M1', 'base_model_id': None,
                          'meta': {'toolIds': ['websearch']}, 'params': {}}},
    }
    client, calls = make_env(db)
    actions = run(client, model_ids=['m1']).actions
    assert not [r for r in calls if r.url.path == '/api/v1/models/model/update']
    assert any('already has' in a for a in actions)


def test_valves_merge_with_existing():
    db = {
        'tools': [dict(OLD_TOOL)],
        'exports': [{'id': 'websearch', 'content': PLUGIN_CONTENT, 'access_grants': []}],
        'models': {},
        'valves': {'SELECTION': 'tavily', 'NUM_RESULTS': 5},
    }
    client, calls = make_env(db)
    run(client, valves=['SELECTION=exa'])
    update = next(r for r in calls if r.url.path.endswith('/valves/update'))
    assert req_json(update)['valves'] == {'SELECTION': 'exa', 'NUM_RESULTS': 5}


def test_signin_sets_authorization_header():
    db = {'tools': [], 'exports': [], 'models': {}}
    client, calls = make_env(db)
    provisioner = provision.Provisioner(client, PLUGIN_CONTENT)
    import asyncio

    asyncio.run(provisioner._signin('admin@example.com', 'pw'))
    assert client.headers['Authorization'] == 'Bearer jwt-123'
    assert calls[0].url.path == '/api/v1/auths/signin'


def test_main_requires_credentials():
    import asyncio

    exit_code = asyncio.run(provision.main(['--base-url', 'http://owui.test']))
    assert exit_code == 2


def test_parse_args_repeatable_flags():
    args = provision.parse_args(
        ['--base-url', 'http://x', '--token', 't',
         '--grant-group', 'g1', '--grant-group', 'g2',
         '--grant-user', 'u1',
         '--model-id', 'm1',
         '--valve', 'SELECTION=exa', '--valve', 'NUM_RESULTS=5']
    )
    assert args.grant_group == ['g1', 'g2']
    assert args.grant_user == ['u1']
    assert args.model_id == ['m1']
    assert args.valve == ['SELECTION=exa', 'NUM_RESULTS=5']
