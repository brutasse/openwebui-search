"""
name: WebSearch
description: Semantic web search (Exa, Tavily, Firecrawl, Parallel, TinyFish) with sticky-random provider selection and 429 failover, plus page fetch. Mirrors the OpenCode websearch/webfetch tool pair.
version: 1.0.0
author: brutasse
"""

import asyncio
import ipaddress
import json
import os
import random
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString

MAX_RESPONSE_BYTES = 256 * 1024  # hard cap on provider responses (OpenCode: 256KB)
MAX_REDIRECTS = 5
MAX_FETCH_BYTES = 5 * 1024 * 1024
DEFAULT_USER_AGENT = 'open-webui-websearch-plugin/1.0'
NO_RESULTS = 'No search results found. Please try a different query.'
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 900.0


class Valves:
    # Provider selection: 'random' (default), a provider id ('exa', 'tavily',
    # 'firecrawl', 'parallel', 'tinyfish'), or 'disabled'.
    SELECTION = 'random'
    # Results per provider call (OpenCode uses 8).
    NUM_RESULTS = 8
    # Seconds per provider request.
    TIMEOUT_SECONDS = 25
    # fetch_url: truncate extracted text beyond this many characters (0 = no limit).
    MAX_CONTENT_LENGTH = 15000
    # User-Agent header for all requests.
    USER_AGENT = DEFAULT_USER_AGENT
    # Provider API keys. All optional: every provider works keyless (anonymous),
    # exactly like OpenCode. Keys here take precedence over environment variables.
    EXA_API_KEY = ''
    TAVILY_API_KEY = ''
    FIRECRAWL_API_KEY = ''
    PARALLEL_API_KEY = ''
    TINYFISH_API_KEY = ''

    def __init__(self, **kwargs):
        # The app loads valves as Valves(**saved); unknown keys are ignored.
        for key, value in kwargs.items():
            if key in self.__class__.__dict__ and not key.startswith('_'):
                setattr(self, key, value)


# Set by the Open WebUI app at tool load time (module.Valves(**saved)).
valves = None

# Process-scoped state, mirroring OpenCode's in-memory session affinity and
# 429 cooldowns. Resets on container restart, same as OpenCode's cooldowns.
_state = {
    'sticky': None,  # provider id currently pinned under 'random' selection
    'cooldowns': {},  # provider id -> epoch seconds until usable again
}


def _valve(name, default=None):
    if valves is None:
        return default
    value = getattr(valves, name, None)
    if value in (None, ''):
        return default
    return value


def _api_key(env_var):
    return _valve(env_var) or os.environ.get(env_var, '') or None


class ProviderError(Exception):
    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class FetchError(Exception):
    pass


@dataclass
class Result:
    url: str
    title: str | None = None
    content: str | None = None
    published: float | None = None  # ms since epoch


# ---------------------------------------------------------------------------
# Shared HTTP / MCP plumbing (mirrors opencode packages/core/src/plugin/websearch/mcp.ts)
# ---------------------------------------------------------------------------


def _retry_after_seconds(value):
    """OpenCode: honor Retry-After (seconds or HTTP date), default 60s."""
    if not value:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        return max(0.0, min(float(value), MAX_COOLDOWN_SECONDS))
    except (TypeError, ValueError):
        pass
    try:
        delta = parsedate_to_datetime(str(value)) - datetime.now(timezone.utc)
        return max(0.0, min(delta.total_seconds(), MAX_COOLDOWN_SECONDS))
    except Exception:
        return DEFAULT_COOLDOWN_SECONDS


def _check_status(resp):
    if resp.status_code >= 400:
        raise ProviderError(
            f'HTTP {resp.status_code}',
            status_code=resp.status_code,
            retry_after=_retry_after_seconds(resp.headers.get('Retry-After')),
        )


def _decode_mcp_response(text, content_type):
    """MCP responses arrive as plain JSON or server-sent events."""
    if 'text/event-stream' in (content_type or ''):
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        for event in reversed(events):
            if isinstance(event, dict) and ('result' in event or 'error' in event):
                return event
        raise ProviderError('No result event in MCP stream')
    try:
        event = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderError(f'Invalid MCP response: {e}') from None
    if not isinstance(event, dict):
        raise ProviderError('Invalid MCP response: expected object')
    return event


async def _mcp_call(client, url, tool, arguments, extra_headers=None):
    body = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {'name': tool, 'arguments': arguments},
    }
    headers = {
        'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json',
    }
    if extra_headers:
        headers.update(extra_headers)
    resp = await client.post(url, json=body, headers=headers)
    _check_status(resp)
    text = resp.content[:MAX_RESPONSE_BYTES].decode('utf-8', errors='replace')
    event = _decode_mcp_response(text, resp.headers.get('Content-Type', ''))
    if 'error' in event:
        error = event['error']
        message = error.get('message', str(error)) if isinstance(error, dict) else str(error)
        raise ProviderError(f'MCP error: {message}', status_code=resp.status_code)
    result = event.get('result')
    if not isinstance(result, dict):
        raise ProviderError('Empty MCP result')
    return result


def _first_text(result):
    for block in result.get('content') or []:
        if isinstance(block, dict) and block.get('text'):
            return block['text']
    raise ProviderError('No text content in MCP result')


def _parse_published(value):
    if not value:
        return None
    value = str(value).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        for fmt in ('%Y-%m-%d', '%d %b %Y', '%B %d, %Y'):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


# ---------------------------------------------------------------------------
# Providers (same five as OpenCode, all with anonymous/keyless mode)
# ---------------------------------------------------------------------------

_EXA_BLOCK_RE = re.compile(r'^(URL|Title|Published|Highlights|Text):\s*(.*)$', re.IGNORECASE)


def _parse_exa(text):
    """Exa MCP returns results as '---'-separated blocks of `Key: value` lines."""
    results = []
    for block in text.split('\n\n---\n\n'):
        entry = {'url': None, 'title': None, 'published': None, 'content': []}
        in_content = False
        for line in block.splitlines():
            match = _EXA_BLOCK_RE.match(line.strip())
            if match:
                key, value = match.group(1).lower(), match.group(2).strip()
                if key == 'url':
                    entry['url'] = value
                    in_content = False
                elif key == 'title':
                    entry['title'] = value
                    in_content = False
                elif key == 'published':
                    entry['published'] = _parse_published(value)
                    in_content = False
                else:  # Highlights / Text
                    in_content = True
                    if value:
                        entry['content'].append(value)
            elif in_content:
                entry['content'].append(line)
        if entry['url']:
            results.append(
                Result(
                    url=entry['url'],
                    title=entry['title'],
                    content='\n'.join(entry['content']).strip() or None,
                    published=entry['published'],
                )
            )
    return results


async def _search_exa(client, query, num_results, key):
    url = 'https://mcp.exa.ai/mcp'
    if key:
        url = f'{url}?exaApiKey={quote(key, safe="")}'
    result = await _mcp_call(client, url, 'web_search_exa', {'query': query, 'numResults': num_results})
    # OpenCode: missing/empty text block means zero results, not an error.
    text = next((b.get('text') for b in result.get('content') or [] if isinstance(b, dict) and b.get('text')), '')
    return _parse_exa(text)


async def _search_tavily(client, query, num_results, key):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    else:
        headers['X-Tavily-Access-Mode'] = 'keyless'
    resp = await client.post(
        'https://api.tavily.com/search',
        json={
            'query': query,
            'search_depth': 'basic',
            'chunks_per_source': 3,
            'max_results': num_results,
        },
        headers=headers,
    )
    _check_status(resp)
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise ProviderError(f'Invalid Tavily response: {e}') from None
    return [
        Result(url=item['url'], title=item.get('title'), content=item.get('content'))
        for item in data.get('results', [])
        if isinstance(item, dict) and item.get('url')
    ]


async def _search_firecrawl(client, query, num_results, key):
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    result = await _mcp_call(
        client,
        'https://mcp.firecrawl.dev/v2/mcp',
        'firecrawl_search',
        {'query': query, 'limit': num_results},
        headers,
    )
    try:
        data = json.loads(_first_text(result))
    except json.JSONDecodeError as e:
        raise ProviderError(f'Invalid Firecrawl payload: {e}') from None
    web = (data.get('data') or {}).get('web') or []
    return [
        Result(url=item['url'], title=item.get('title'), content=item.get('description'))
        for item in web
        if isinstance(item, dict) and item.get('url')
    ]


async def _search_parallel(client, query, num_results, key):
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    result = await _mcp_call(
        client,
        'https://search.parallel.ai/mcp',
        'web_search',
        {'objective': query, 'search_queries': [query]},
        headers,
    )
    payload = result.get('structuredContent')
    if not isinstance(payload, dict):
        try:
            payload = json.loads(_first_text(result))
        except json.JSONDecodeError as e:
            raise ProviderError(f'Invalid Parallel payload: {e}') from None
    items = payload.get('results') or []
    return [
        Result(
            url=item['url'],
            title=item.get('title'),
            content='\n\n'.join(item.get('excerpts') or []) or None,
            published=_parse_published(item.get('publish_date')),
        )
        for item in items
        if isinstance(item, dict) and item.get('url')
    ]


async def _mcp_call_tinyfish_keyed(client, url, query, key):
    """Keyed TinyFish requires the MCP initialize handshake for a session id."""
    headers = {
        'X-API-Key': key,
        'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json',
    }
    init_resp = await client.post(
        url,
        json={
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2025-06-18',
                'capabilities': {},
                'clientInfo': {'name': 'open-webui', 'version': '1.0.0'},
            },
        },
        headers=headers,
    )
    _check_status(init_resp)
    session_id = init_resp.headers.get('Mcp-Session-Id')
    if session_id:
        headers['Mcp-Session-Id'] = session_id
        await client.post(url, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=headers)
    return await _mcp_call(client, url, 'search', {'query': query}, headers)


async def _search_tinyfish(client, query, num_results, key):
    url = 'https://agent.tinyfish.ai/mcp'
    if key:
        result = await _mcp_call_tinyfish_keyed(client, url, query, key)
    else:
        result = await _mcp_call(client, url, 'search', {'query': query}, {'X-TinyFish-Access-Mode': 'keyless'})
    try:
        data = json.loads(_first_text(result))
    except json.JSONDecodeError as e:
        raise ProviderError(f'Invalid TinyFish payload: {e}') from None
    return [
        Result(url=item['url'], title=item.get('title'), content=item.get('snippet'))
        for item in data.get('results', [])
        if isinstance(item, dict) and item.get('url')
    ]


PROVIDERS = {
    'exa': {'name': 'Exa', 'env': 'EXA_API_KEY', 'search': _search_exa},
    'tavily': {'name': 'Tavily', 'env': 'TAVILY_API_KEY', 'search': _search_tavily},
    'firecrawl': {'name': 'Firecrawl', 'env': 'FIRECRAWL_API_KEY', 'search': _search_firecrawl},
    'parallel': {'name': 'Parallel', 'env': 'PARALLEL_API_KEY', 'search': _search_parallel},
    'tinyfish': {'name': 'TinyFish', 'env': 'TINYFISH_API_KEY', 'search': _search_tinyfish},
}


# ---------------------------------------------------------------------------
# Selection, cooldown, failover (OpenCode semantics)
# ---------------------------------------------------------------------------


def _selection():
    return (_valve('SELECTION', 'random') or 'random').strip().lower()


def _on_cooldown(provider_id):
    return _state['cooldowns'].get(provider_id, 0.0) > time.time()


def _set_cooldown(provider_id, retry_after):
    _state['cooldowns'][provider_id] = time.time() + _retry_after_seconds(retry_after)


def _candidate_order():
    selection = _selection()
    if selection == 'random':
        order = list(PROVIDERS)
        random.shuffle(order)
        sticky = _state['sticky']
        if sticky in order:
            order.remove(sticky)
            order.insert(0, sticky)
        return order
    if selection in PROVIDERS:
        return [selection]
    return []


def _timeout_seconds():
    try:
        return float(_valve('TIMEOUT_SECONDS', 25) or 25)
    except (TypeError, ValueError):
        return 25.0


def _num_results():
    try:
        return max(1, int(_valve('NUM_RESULTS', 8) or 8))
    except (TypeError, ValueError):
        return 8


async def _run_search(query):
    """Search via the selected provider(s). Returns (results, provider_id)."""
    selection = _selection()
    order = _candidate_order()
    if not order:
        raise ProviderError(f'Web search is disabled or has no providers (SELECTION={selection!r})')

    user_agent = _valve('USER_AGENT', DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT
    failures = []
    for provider_id in order:
        # Skip a cooling-down provider only while another provider is available.
        if selection == 'random' and _on_cooldown(provider_id) and any(
            not _on_cooldown(p) for p in order
        ):
            continue
        spec = PROVIDERS[provider_id]
        key = _api_key(spec['env'])
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_timeout_seconds(), connect=10),
                headers={'User-Agent': user_agent},
                follow_redirects=False,
            ) as client:
                results = await spec['search'](client, query, _num_results(), key)
            if selection == 'random':
                _state['sticky'] = provider_id
            return results, provider_id
        except ProviderError as e:
            if e.status_code == 429:
                _set_cooldown(provider_id, e.retry_after)
            failures.append(f'{spec["name"]}: {e}')
            if selection != 'random':
                raise
    raise ProviderError('All web search providers failed. ' + '; '.join(failures))


def _format_results(results):
    """OpenCode's tool output format: '## [Title](url)' blocks, joined by blank lines."""
    if not results:
        return NO_RESULTS
    blocks = []
    for r in results:
        title = ' '.join((r.title or r.url).split())
        block = f'## [{title}]({r.url})'
        if r.published:
            published = datetime.fromtimestamp(r.published / 1000, tz=timezone.utc)
            block += f'\nPublished: {published.isoformat()}'
        if r.content:
            block += f'\n\n{r.content}'
        blocks.append(block)
    return '\n\n'.join(blocks)


# ---------------------------------------------------------------------------
# fetch_url (self-contained; the built-in is removed with ENABLE_WEB_SEARCH=false)
# ---------------------------------------------------------------------------


def _validate_url(url):
    """Block non-http(s) schemes and non-public addresses (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise FetchError(f'Unsupported URL scheme: {parsed.scheme!r}')
    host = parsed.hostname
    if not host:
        raise FetchError('URL has no host')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        addrinfo = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError(f'Could not resolve host: {host}') from None
    for _family, _type, _proto, _canon, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise FetchError(f'Blocked request to non-public address: {host}')


_MD_SKIP = {'script', 'style', 'noscript', 'svg', 'template', 'iframe', 'object', 'embed', 'head'}
_MD_BLOCK = {'address', 'article', 'aside', 'blockquote', 'details', 'div', 'dl', 'dt', 'dd',
             'fieldset', 'figcaption', 'figure', 'footer', 'form', 'header', 'main', 'nav',
             'p', 'section', 'summary', 'tr', 'td', 'th'}
_MD_ESCAPES = re.compile(r'([\\`*_\[\]])')


def _collapse(text):
    return re.sub(r'\s+', ' ', text).strip()


def _md_pre(node):
    code = node.get_text().strip('\n')
    lang = ''
    code_el = node.find('code')
    if code_el is not None:
        match = re.search(r'(?:language|lang)-([\w+.-]+)', ' '.join(code_el.get('class') or []))
        if match:
            lang = match.group(1)
    fence = ('````' if '```' in code else '```') + lang
    return f'{fence}\n{code}\n{fence}'


def _md_list_item(li, base_url, number, ordered):
    """Render one <li> as markdown lines: its own content plus nested lists."""
    content = []
    nested = []
    for child in li.children:
        if getattr(child, 'name', None) in ('ul', 'ol'):
            nested.append(child)
        else:
            content.append(_node_md(child, base_url))
    text = _collapse(''.join(content))
    marker = f'{number}.' if ordered else '-'
    lines = [f'{marker} {text}'] if text else []
    for sub in nested:
        sub_md = _node_md(sub, base_url)
        if sub_md:
            lines.extend(f'  {line}' for line in sub_md.splitlines() if line.strip())
    return lines


def _node_md(node, base_url):
    """Render one bs4 node as markdown text (pragmatic converter, not byte-faithful)."""
    if isinstance(node, NavigableString):
        return _MD_ESCAPES.sub(r'\\\1', _collapse(str(node)))
    name = (node.name or '').lower()
    if name in _MD_SKIP:
        return ''
    if name == 'pre':
        return _md_pre(node)
    if name == 'code':
        text = _collapse(node.get_text())
        if not text:
            return ''
        fence = '``' if '`' in text else '`'
        return f'{fence}{text}{fence}'
    if name == 'br':
        return ''
    if name == 'hr':
        return '---'
    if name == 'img':
        src = (node.get('src') or '').strip()
        alt = _collapse(node.get('alt') or '')
        if src:
            url = urljoin(base_url, src.replace(' ', '%20'))
            return f'![{_MD_ESCAPES.sub(r"\\\1", alt)}]({url})' if alt else url
        return _MD_ESCAPES.sub(r'\\\1', alt)
    if name == 'a':
        href = (node.get('href') or '').strip()
        text = _collapse(''.join(_node_md(child, base_url) for child in node.children))
        if not href or href.lower().startswith('javascript:'):
            return text
        url = urljoin(base_url, href.replace(' ', '%20'))
        if not text:
            return url.replace(' ', '%20')
        return f'[{text}]({url})'
    if name in ('strong', 'b'):
        text = _collapse(''.join(_node_md(child, base_url) for child in node.children))
        return f'**{text}**' if text else ''
    if name in ('em', 'i'):
        text = _collapse(''.join(_node_md(child, base_url) for child in node.children))
        return f'*{text}*' if text else ''
    if re.fullmatch(r'h[1-6]', name):
        text = _collapse(''.join(_node_md(child, base_url) for child in node.children))
        return f'{"#" * int(name[1])} {text}' if text else ''
    if name in ('ul', 'ol'):
        lines = []
        number = 1
        for child in node.children:
            if getattr(child, 'name', None) != 'li':
                continue
            lines.extend(_md_list_item(child, base_url, number, name == 'ol'))
            number += 1
        return '\n'.join(lines)
    if name == 'table':
        rows = []
        for tr in node.find_all('tr'):
            cells = [
                _collapse(''.join(_node_md(c, base_url) for c in cell.children))
                for cell in tr.find_all(['td', 'th'], recursive=False)
            ]
            row = ' | '.join(c for c in cells if c)
            if row:
                rows.append(row)
        return '\n'.join(rows)
    if name in _MD_BLOCK:
        parts = [_node_md(child, base_url) for child in node.children]
        return '\n\n'.join(part.strip() for part in parts if part.strip())
    return ''.join(_node_md(child, base_url) for child in node.children)


def _extract_html(html, base_url):
    soup = BeautifulSoup(html, 'lxml')
    title = soup.title.get_text(strip=True) if soup.title else ''
    root = soup.find('main') or soup.find('article') or soup.body or soup
    text = _node_md(root, base_url)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return title, text


def _decode_response(resp, url):
    content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    raw = resp.content.decode('utf-8', errors='replace')
    if content_type == 'text/html':
        return _extract_html(raw, url)
    if content_type == 'application/json' or content_type.endswith('+json'):
        try:
            raw = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
        return None, raw
    if (
        content_type.startswith('text/')
        or content_type in ('application/xml', 'application/javascript', 'application/x-javascript')
        or content_type.endswith(('+xml', '+json'))
    ):
        return None, raw
    raise FetchError(f'Unsupported content type: {content_type or "unknown"}')


def _finalize(title, body):
    max_length = _valve('MAX_CONTENT_LENGTH', 15000)
    try:
        max_length = int(max_length) if max_length is not None else 0
    except (TypeError, ValueError):
        max_length = 0
    if max_length > 0 and len(body) > max_length:
        body = body[:max_length] + '\n\n[Content truncated...]'
    if title:
        return f'# {title}\n\n{body}' if body else f'# {title}'
    return body


async def _fetch_url(url):
    user_agent = _valve('USER_AGENT', DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT
    headers = {'User-Agent': user_agent, 'Accept': 'text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1'}
    current = url
    async with httpx.AsyncClient(timeout=httpx.Timeout(_timeout_seconds(), connect=10), follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS):
            await asyncio.to_thread(_validate_url, current)
            resp = await client.get(current, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get('Location')
                if not location:
                    raise FetchError(f'Redirect without Location (HTTP {resp.status_code})')
                current = urljoin(current, location)
                continue
            if resp.status_code >= 400:
                raise FetchError(f'HTTP {resp.status_code}')
            if len(resp.content) > MAX_FETCH_BYTES:
                raise FetchError('Response too large')
            title, body = _decode_response(resp, current)
            return _finalize(title, body)
    raise FetchError('Too many redirects')


# ---------------------------------------------------------------------------
# Tools (spec = public methods: name + type hints + docstring)
# ---------------------------------------------------------------------------


class Tools:
    async def web_search(self, query: str) -> str:
        """
        Search the web using the user's selected search integration. Use this for current
        information beyond knowledge cutoff.

        :param query: The search query to search for
        :return: Markdown sections with title, URL, publication date and page content for each result.
        """
        try:
            results, _provider = await _run_search(query)
        except ProviderError as e:
            if e.status_code == 429:
                return 'Web search rate limited (HTTP 429)'
            if e.status_code == 401:
                return 'Web search authentication failed (HTTP 401)'
            return f'Web search request failed. {e}'
        return _format_results(results)

    async def fetch_url(self, url: str) -> str:
        """
        Fetch a web page and convert its main content to markdown. Links are resolved
        to absolute URLs; headings, lists, emphasis and code blocks are preserved.
        Non-HTML content (JSON, plain text, markdown) is returned as-is.

        :param url: The URL to fetch content from
        :return: The page content as markdown, with the page title as the first heading
        """
        try:
            return await _fetch_url(url)
        except FetchError as e:
            return json.dumps({'error': str(e)})
        except Exception as e:
            return json.dumps({'error': f'Fetch failed: {e}'})


# Mirror OpenCode: the tool description carries the current year.
Tools.web_search.__doc__ = (
    'Search the web using the user\'s selected search integration. Use this for current '
    'information beyond knowledge cutoff.\n\n'
    'The current year is {year}. Use this year when searching for recent information or '
    'current events.\n\n'
    ':param query: The search query to search for\n'
    ':return: Markdown sections with title, URL, publication date and page content for each result.'
).format(year=datetime.now().year)
