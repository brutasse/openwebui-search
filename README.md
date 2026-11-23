# openwebui-search

This plugin adds semantic web search to Open WebUI agent sessions. It is a
self-contained Tools plugin. You do not change the Open WebUI core. You do
not change the container image.

The plugin replicates the OpenCode `websearch` + `webfetch` tool pair:

- **`web_search(query)`** — semantic search through five providers:
  **Exa, Tavily, Firecrawl, Parallel, TinyFish** (the same set OpenCode ships).
  Sticky-random provider selection, 429 cooldown with `Retry-After`, and
  failover to the next provider (explicit selection never fails over, like
  OpenCode).
- **Anonymous-first** — every provider works without an API key, as in
  OpenCode. Exa uses its MCP endpoint without a key. Tavily sends the
  `X-Tavily-Access-Mode: keyless` header. Keys are optional. A key takes
  precedence over the anonymous mode.
- **`fetch_url(url)`** — the tool fetches a page and converts its main
  content to markdown. Links resolve to absolute URLs. Headings, lists,
  emphasis, and code blocks survive the conversion. When you disable the
  built-in web search, the built-in `fetch_url` also disappears. So the
  plugin ships its own `fetch_url`. It has an SSRF guard. The guard blocks
  private, loopback, link-local, and reserved addresses, including on
  redirects.

## Files

| File | Purpose |
|---|---|
| `plugins/websearch.py` | The tool plugin. Paste into Admin → Tools → New Tool, or provision via the script below. |
| `provision.py` | Idempotent provisioner: creates/updates the tool in the Open WebUI database, sets access grants, default valves, and attaches the tool to workspace models. |
| `tests/` | Unit tests (mocked HTTP; no live provider calls). |
| `scripts/check_owui_compat.py` | Simulates Open WebUI's exact tool-load path (frontmatter, `Valves(**saved)`, spec derivation) against the plugin. |

## Deployment (Docker container)

Tool plugins live in the Open WebUI **database**, not on disk. The source
lives in this repo. The provisioner registers the plugin against your
running container. The plugin survives image upgrades, because it shares the
data volume. Re-run the provisioner after a volume wipe. Also re-run it
after an Open WebUI upgrade that changes the plugin source.

```bash
# 1. Disable the built-in web search (removes built-in search_web + fetch_url)
#    in your docker-compose environment:
#       ENABLE_WEB_SEARCH=false

# 2. Provision the plugin (admin credentials; grant to your default group so
#    non-admin users can use it — tools do not support 'anyone' grants):
python3 provision.py \
  --base-url http://localhost:8080 \
  --email admin@example.com --password '<pw>' \
  --grant-group '<your-default-group-id>' \
  --model-id '<model-id>'
```

Then enable the tools per chat, from the search/fetch integrations menu. Or
make sure the model's tools include `websearch`. The provisioner does this
with `--model-id`.

Get an admin JWT instead of a password with the browser: DevTools →
Network → any API call → copy the `Authorization` header value → `--token`.

## Configuration (tool Valves, Admin → Tools → WebSearch)

| Valve | Default | Meaning |
|---|---|---|
| `SELECTION` | `random` | Provider: `exa` / `tavily` / `firecrawl` / `parallel` / `tinyfish` / `random` / `disabled`. |
| `NUM_RESULTS` | `8` | Results per provider call. |
| `TIMEOUT_SECONDS` | `25` | Per-request timeout. |
| `MAX_CONTENT_LENGTH` | `15000` | `fetch_url` text truncation (0 = unlimited). |
| `USER_AGENT` | `open-webui-websearch-plugin/1.0` | UA header. |
| `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, `PARALLEL_API_KEY`, `TINYFISH_API_KEY` | `''` | Optional. Empty = anonymous request. Environment variables of the same name are the fallback. |

## Behavior notes (where this mirrors OpenCode)

- With selection `random`, the plugin picks a provider and **sticks to it**
  until that provider returns 429. Then the provider enters cooldown, and
  the plugin tries another provider. Cooldown honors `Retry-After`. The
  default is 60 s. The maximum is 15 min. The state is process-scoped and
  resets on container restart, as in OpenCode, which keeps cooldowns in
  memory.
- The plugin calls MCP providers as JSON-RPC `tools/call` requests. The
  request carries `Accept: application/json, text/event-stream`. The
  plugin parses SSE `data:` lines, caps each response at 256 KB, and uses
  a 25 s timeout. These are OpenCode's values.
- `fetch_url` sends OpenCode's default `Accept` header. It prefers
  `text/markdown` and falls back to `text/html` and `*/*`. A
  `text/markdown` response passes through unchanged. An HTML page converts
  to markdown. The plugin renders links as `[text](url)` and resolves
  relative URLs against the page URL. Headings, lists, emphasis, and code
  blocks keep their structure.
- Output is OpenCode's format: `## [Title](url)` blocks with
  `Published:` and page content, joined by blank lines;
  `No search results found. Please try a different query.` on empty.
- Errors: `Web search rate limited (HTTP 429)`,
  `Web search authentication failed (HTTP 401)`, otherwise
  `Web search request failed. <details>`.
- Two OpenCode features are not portable. First, the first-use consent
  form: Open WebUI tools cannot prompt the user, so `Valves.SELECTION`
  covers that choice. Second, OpenCode persists the selection in global
  key-value storage. Here, the selection is process-scoped.

## Development

```bash
uv venv .venv && uv pip install -r requirements-dev.txt -p .venv/bin/python
.venv/bin/python -m pytest tests/ -v
```
