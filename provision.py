#!/usr/bin/env python3
"""Idempotently provision the WebSearch tools plugin against an Open WebUI instance.

The plugin source lives in this repo; the *live* copy lives in the Open WebUI
database. This script is the bridge: create or update the tool, ensure access
grants, optionally set default valves, and attach the tool to workspace models.

Usage:
    python3 provision.py --base-url http://localhost:8080 \
        --email admin@example.com --password <pw> \
        --grant-group <group-id> \
        --model-id <model-id>

Auth is via --token (JWT from the browser, or "Sign In" response) or
--email/--password (signed in automatically).
"""

import argparse
import asyncio
import json
import sys

import httpx

DEFAULT_BASE_URL = 'http://localhost:8080'
TOOL_ID = 'websearch'
TOOL_NAME = 'WebSearch'
TOOL_DESCRIPTION = (
    'Semantic web search (Exa, Tavily, Firecrawl, Parallel, TinyFish) with '
    'sticky-random provider selection and 429 failover, plus page fetch.'
)


class ProvisionError(Exception):
    pass


def _check(resp, action):
    if resp.status_code >= 400:
        detail = ''
        try:
            detail = resp.json().get('detail', '')
        except Exception:
            detail = resp.text[:200]
        raise ProvisionError(f'{action} failed: HTTP {resp.status_code} {detail}')
    return resp


class Provisioner:
    def __init__(self, client, plugin_content, tool_id=TOOL_ID, tool_name=TOOL_NAME,
                 tool_description=TOOL_DESCRIPTION):
        self.client = client
        self.plugin_content = plugin_content
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.tool_description = tool_description
        self.actions = []

    async def _signin(self, email, password):
        resp = _check(
            await self.client.post('/api/v1/auths/signin', json={'email': email, 'password': password}),
            'sign in',
        )
        token = resp.json().get('token')
        if not token:
            raise ProvisionError('sign in succeeded but no token returned')
        self.client.headers['Authorization'] = f'Bearer {token}'
        self.actions.append(f'signed in as {email}')

    async def _list_tools(self):
        resp = _check(await self.client.get('/api/v1/tools/list'), 'list tools')
        return resp.json()

    async def _export_tools(self):
        resp = _check(await self.client.get('/api/v1/tools/export'), 'export tools')
        return resp.json()

    async def _upsert_tool(self, grants):
        listing = await self._list_tools()
        existing = next((t for t in listing if t.get('id') == self.tool_id or t.get('name') == self.tool_name), None)

        meta = {'description': self.tool_description}
        if existing is None:
            body = {
                'id': self.tool_id,
                'name': self.tool_name,
                'content': self.plugin_content,
                'meta': meta,
                'access_grants': grants,
            }
            resp = _check(await self.client.post('/api/v1/tools/create', json=body), 'create tool')
            tool = resp.json()
            self.actions.append(f'created tool {self.tool_id} ({self.tool_name})')
            return tool

        exported = {t.get('id'): t for t in await self._export_tools()}
        stored = exported.get(existing['id']) or {}
        if stored.get('content') != self.plugin_content:
            body = {'id': self.tool_id, 'name': self.tool_name, 'content': self.plugin_content, 'meta': meta}
            resp = _check(
                await self.client.post(f'/api/v1/tools/id/{self.tool_id}/update', json=body),
                'update tool',
            )
            self.actions.append(f'updated tool content ({self.tool_id})')
        else:
            self.actions.append(f'tool content up to date ({self.tool_id})')
        return stored or existing

    async def _ensure_grants(self, tool, requested_grants):
        current = [
            {'principal_type': g['principal_type'], 'principal_id': g['principal_id'], 'permission': g['permission']}
            for g in (tool.get('access_grants') or [])
        ]
        merged = list(current)
        for grant in requested_grants:
            key = (grant['principal_type'], grant['principal_id'], grant['permission'])
            if all((g['principal_type'], g['principal_id'], g['permission']) != key for g in merged):
                merged.append(grant)
        if len(merged) == len(current):
            return
        resp = _check(
            await self.client.post(
                f'/api/v1/tools/id/{self.tool_id}/access/update',
                json={'access_grants': merged},
            ),
            'update tool access',
        )
        self.actions.append(f'granted access: ' + ', '.join(
            f"{g['principal_type']}:{g['principal_id']} ({g['permission']})"
            for g in merged[len(current):]
        ))

    async def _set_valves(self, valve_pairs):
        if not valve_pairs:
            return
        resp = _check(await self.client.get(f'/api/v1/tools/id/{self.tool_id}/valves'), 'get valves')
        current = resp.json() or {}
        merged = dict(current)
        for pair in valve_pairs:
            key, _, value = pair.partition('=')
            if not key:
                raise ProvisionError(f'invalid --valve value: {pair!r} (expected KEY=VALUE)')
            merged[key] = value
        _check(
            await self.client.post(f'/api/v1/tools/id/{self.tool_id}/valves/update', json={'valves': merged}),
            'update valves',
        )
        self.actions.append(f'set valves: {", ".join(p.split("=")[0] for p in valve_pairs)}')

    async def _attach_to_models(self, model_ids):
        for model_id in model_ids:
            resp = _check(
                await self.client.get('/api/v1/models/model', params={'id': model_id}),
                f'get model {model_id}',
            )
            model = resp.json()
            meta = dict(model.get('meta') or {})
            tool_ids = meta.get('toolIds') or []
            if self.tool_id in tool_ids:
                self.actions.append(f'model {model_id} already has {self.tool_id}')
                continue
            meta['toolIds'] = [*tool_ids, self.tool_id]
            body = {
                'id': model['id'],
                'base_model_id': model.get('base_model_id'),
                'name': model['name'],
                'meta': meta,
                'params': model.get('params') or {},
            }
            _check(
                await self.client.post('/api/v1/models/model/update', json=body),
                f'update model {model_id}',
            )
            self.actions.append(f'attached {self.tool_id} to model {model_id}')

    async def run(self, grant_groups, grant_users, model_ids, valve_pairs):
        grants = []
        for group_id in grant_groups:
            grants.append({'principal_type': 'group', 'principal_id': group_id, 'permission': 'read'})
        for user_id in grant_users:
            grants.append({'principal_type': 'user', 'principal_id': user_id, 'permission': 'read'})

        tool = await self._upsert_tool(grants)
        # Refresh grants from the live record (create response may not echo them).
        if grants:
            listing = await self._list_tools()
            live = next((t for t in listing if t.get('id') == self.tool_id), tool)
            await self._ensure_grants(live, grants)
        else:
            current = (tool.get('access_grants') or [])
            if not current:
                self.actions.append('WARNING: no access grants on tool; non-admin users will not see it '
                                    '(re-run with --grant-group/--grant-user)')
        await self._set_valves(valve_pairs)
        await self._attach_to_models(model_ids)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL, help='Open WebUI base URL')
    parser.add_argument('--token', help='admin JWT (alternative to --email/--password)')
    parser.add_argument('--email', help='admin email (signed in automatically)')
    parser.add_argument('--password', help='admin password')
    parser.add_argument('--plugin-file', default='plugins/websearch.py', help='path to the plugin source')
    parser.add_argument('--tool-id', default=TOOL_ID)
    parser.add_argument('--tool-name', default=TOOL_NAME)
    parser.add_argument('--grant-group', action='append', default=[], metavar='GROUP_ID',
                        help='grant read access to a group (repeatable)')
    parser.add_argument('--grant-user', action='append', default=[], metavar='USER_ID',
                        help='grant read access to a user (repeatable)')
    parser.add_argument('--model-id', action='append', default=[], metavar='MODEL_ID',
                        help='attach the tool to a workspace model (repeatable)')
    parser.add_argument('--valve', action='append', default=[], metavar='KEY=VALUE',
                        help='set a default valve value (repeatable), e.g. SELECTION=exa')
    return parser.parse_args(argv)


async def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.token and not (args.email and args.password):
        print('error: provide --token or --email/--password', file=sys.stderr)
        return 2

    plugin_file = args.plugin_file
    try:
        with open(plugin_file, 'r', encoding='utf-8') as f:
            plugin_content = f.read()
    except OSError as e:
        print(f'error: cannot read {plugin_file}: {e}', file=sys.stderr)
        return 2

    headers = {'Authorization': f'Bearer {args.token}'} if args.token else {}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=30) as client:
        provisioner = Provisioner(
            client,
            plugin_content,
            tool_id=args.tool_id,
            tool_name=args.tool_name,
        )
        if not args.token:
            await provisioner._signin(args.email, args.password)
        try:
            await provisioner.run(args.grant_group, args.grant_user, args.model_id, args.valve)
        except (ProvisionError, httpx.HTTPError) as e:
            print(f'error: {e}', file=sys.stderr)
            return 1
    for action in provisioner.actions:
        print(f'  {action}')
    print(f'done. Tool "{provisioner.tool_name}" ({provisioner.tool_id}) is provisioned '
          f'at {args.base_url}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
