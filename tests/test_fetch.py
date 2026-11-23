from conftest import load_tool, websearch
from fake_http import FakeClientFactory, FakeResponse
import asyncio


def fetch(url, responses, **valves):
    """Run Tools.fetch_url with a fake httpx client; returns (output, factory)."""
    tool = load_tool(**valves)
    factory = FakeClientFactory(responses)
    websearch.httpx.AsyncClient = factory
    output = asyncio.run(tool.fetch_url(url))
    return output, factory


HTML = (
    '<html><head><title>My Page</title><style>body{color:red}</style></head>'
    '<body><script>var secret = 1;</script><main>'
    '<p>Hello world</p><p>Second paragraph</p>'
    '</main></body></html>'
)


def test_fetch_accept_header_is_opencode_markdown_default():
    output, factory = fetch('https://example.com/plain', [FakeResponse(body=b'x', content_type='text/plain')])
    assert (
        factory.clients[0].requests[0]['headers']['Accept']
        == 'text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1'
    )


def test_fetch_markdown_content_type_passes_through():
    md = '# Heading\n\nsome **markdown** body'
    output, factory = fetch('https://example.com/doc', [FakeResponse(body=md.encode(), content_type='text/markdown')])
    assert output == md  # raw pass-through: no title heading, no re-encode


def test_fetch_html_extraction():
    output, factory = fetch('https://example.com/page', [FakeResponse(body=HTML.encode(), content_type='text/html')])
    assert output.startswith('# My Page')
    assert 'Hello world' in output
    assert 'Second paragraph' in output
    assert 'var secret' not in output  # script stripped
    assert 'color:red' not in output  # style stripped
    assert factory.clients[0].requests[0]['headers']['User-Agent'] == 'open-webui-websearch-plugin/1.0'


MD_HTML = (
    '<html><head><title>MD Page</title>'
    '<script>var secret = 1;</script><style>p{}</style></head>'
    '<body><main>'
    '<h1>Top</h1>'
    '<p>Para with <a href="/docs">Docs</a> and <a href="c.html">local</a> and '
    '<a href="https://ext.example/x">ext</a> plus <b><i>strong em</i></b> and '
    '<a>plain anchor</a> and <code>x = 1</code>.</p>'
    '<pre><code class="language-python">def f():\n    return 1</code></pre>'
    '<pre><code>```inner```</code></pre>'
    '<ul><li>alpha</li><li>beta<ul><li>beta-child</li></ul></li></ul>'
    '<ol><li>first</li><li>second</li></ol>'
    '<table><tr><td>A1</td><td>B1</td></tr><tr><td>A2</td><th>B2</th></tr></table>'
    '<img src="img.png" alt="pic">'
    '<h3>Sub</h3>'
    '</main></body></html>'
)

BASE = 'https://example.com/a/b'


def md_output():
    output, factory = fetch(BASE, [FakeResponse(body=MD_HTML.encode(), content_type='text/html')])
    return output


def test_fetch_md_links_resolved():
    output = md_output()
    assert '[Docs](https://example.com/docs)' in output  # root-relative
    assert '[local](https://example.com/a/c.html)' in output  # path-relative
    assert '[ext](https://ext.example/x)' in output  # absolute kept
    assert 'plain anchor' in output  # no href -> text only


def test_fetch_md_headings():
    output = md_output()
    assert '# Top' in output
    assert '### Sub' in output


def test_fetch_md_emphasis_and_inline_code():
    output = md_output()
    assert '***strong em***' in output
    assert '`x = 1`' in output


def test_fetch_md_code_block_language():
    output = md_output()
    assert '```python\ndef f():\n    return 1\n```' in output


def test_fetch_md_code_block_backtick_fence():
    output = md_output()
    assert '````\n```inner```\n````' in output


def test_fetch_md_lists():
    output = md_output()
    assert '- alpha' in output
    assert '- beta' in output
    assert '  - beta-child' in output  # nested, indented
    assert '1. first' in output  # ordered numbered
    assert '2. second' in output


def test_fetch_md_table_rows():
    output = md_output()
    assert 'A1 | B1' in output
    assert 'A2 | B2' in output


def test_fetch_md_image():
    output = md_output()
    assert '![pic](https://example.com/a/img.png)' in output


def test_fetch_blocks_loopback():
    output, factory = fetch('http://127.0.0.1/admin', [])
    assert 'non-public' in output
    assert not factory.clients[0].requests  # blocked before any request


def test_fetch_blocks_private_range():
    output, factory = fetch('http://10.1.2.3/x', [])
    assert 'non-public' in output
    assert not factory.clients[0].requests


def test_fetch_blocks_non_http_scheme():
    output, factory = fetch('ftp://example.com/file', [])
    assert 'Unsupported URL scheme' in output


def test_fetch_blocks_redirect_to_link_local():
    redirect = FakeResponse(status_code=302, body=b'', headers={'Location': 'http://169.254.169.254/latest/meta-data/'})
    output, factory = fetch('https://example.com/ok', [redirect])
    assert 'non-public' in output
    assert len(factory.clients[0].requests) == 1  # first hop only, redirect never followed


def test_fetch_json_pretty():
    output, factory = fetch('https://api.example.com/data', [FakeResponse(body=b'{"b": 1, "a": [1, 2]}', content_type='application/json')])
    assert '"a": [' in output
    assert '"b": 1' in output


def test_fetch_truncation():
    long_body = 'x' * 10000
    output, factory = fetch('https://example.com/long', [FakeResponse(body=long_body.encode(), content_type='text/plain')],
                            MAX_CONTENT_LENGTH=100)
    assert len(output) < 200
    assert '[Content truncated...]' in output


def test_fetch_unsupported_content_type():
    output, factory = fetch('https://example.com/file.pdf', [FakeResponse(body=b'%PDF-1.4', content_type='application/pdf')])
    assert 'Unsupported content type' in output


def test_fetch_http_error():
    output, factory = fetch('https://example.com/missing', [FakeResponse(status_code=404, body=b'nope', content_type='text/plain')])
    assert 'HTTP 404' in output


def test_fetch_text_plain_no_title():
    output, factory = fetch('https://example.com/plain', [FakeResponse(body=b'plain body text', content_type='text/plain')])
    assert output == 'plain body text'
