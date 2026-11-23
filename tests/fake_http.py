import json


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b'', json_data=None, content_type='application/json'):
        self.status_code = status_code
        self.headers = {'Content-Type': content_type}
        if headers:
            self.headers.update(headers)
        if json_data is not None:
            self.content = json.dumps(json_data).encode()
        else:
            self.content = body
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.content.decode())


class FakeClient:
    """Stands in for httpx.AsyncClient: records requests, replays queued responses.

    The response queue is shared (not copied) across all clients created by one
    factory: attempts consume it in order, which is how the plugin works — each
    provider attempt gets a fresh client.
    """

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _next(self, method, url, **extra):
        self.requests.append({'method': method, 'url': url, **extra})
        if not self.responses:
            raise AssertionError(f'unexpected extra request: {method} {url}')
        item = self.responses.pop(0)
        return item(self) if callable(item) else item

    async def post(self, url, json=None, headers=None, **kwargs):
        return self._next('POST', url, json=json, headers=headers or {})

    async def get(self, url, headers=None, **kwargs):
        return self._next('GET', url, headers=headers or {})


class FakeClientFactory:
    """monkeypatch replacement for httpx.AsyncClient; exposes created clients."""

    def __init__(self, responses_per_client):
        self.responses_per_client = responses_per_client
        self.clients = []

    def __call__(self, *args, **kwargs):
        client = FakeClient(self.responses_per_client)
        self.clients.append(client)
        return client
