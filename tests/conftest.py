import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


websearch = _load('owui_websearch', ROOT / 'plugins' / 'websearch.py')
provision = _load('owui_provision', ROOT / 'provision.py')


@pytest.fixture(autouse=True)
def clean_plugin_state():
    websearch.valves = None
    websearch._state.update({'sticky': None, 'cooldowns': {}})
    original_client = websearch.httpx.AsyncClient
    yield
    websearch.valves = None
    websearch._state.update({'sticky': None, 'cooldowns': {}})
    websearch.httpx.AsyncClient = original_client


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Stub hostname resolution; IP literals still resolve for real (no DNS needed)."""
    import ipaddress

    import socket

    from conftest import websearch as ws

    real_getaddrinfo = ws.socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Hostname: answer with a fixed public IP without touching the resolver.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(ws.socket, 'getaddrinfo', fake_getaddrinfo)


def set_valves(**kwargs):
    websearch.valves = websearch.Valves(**kwargs)
