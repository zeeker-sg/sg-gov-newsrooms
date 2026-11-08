"""Shared fixtures for the offline test suite.

Every test runs behind a network guard: a real socket-level HTTP request from
httpx raises immediately, so "tests never hit the network" is enforced rather
than assumed. ``httpx.MockTransport`` is a separate class and stays usable.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# zeeker's builder appends resources/ to sys.path; sibling imports such as
# ``from _isomer import ...`` rely on it.
if str(RESOURCES_DIR) not in sys.path:
    sys.path.append(str(RESOURCES_DIR))

# Resources whose fetch_data is exercised end to end with fixtures.
ALL_NEWS_MODULES = [
    "mlaw_news",
    "judiciary_news",
    "agc_news",
    "ipos_news",
    "ccs_news",
    "acra_news",
    "mom_news",
    "pdpc_news",
]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*args, **kwargs):
        raise AssertionError("test attempted a real network request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must pass with no credentials configured."""
    for var in ("OPENAI_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)


def load_module(name: str) -> ModuleType:
    return importlib.import_module(name)


def read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text("utf-8")


def read_fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


@pytest.fixture
def mock_client() -> Callable[..., httpx.Client]:
    """Build a real ``httpx.Client`` whose transport is a MockTransport."""

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    return _make


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch):
    """Make a resource's ``httpx.Client(...)`` use a MockTransport handler.

    ``fetch_data`` builds its own client, so we swap the factory on the module's
    ``httpx`` reference. Real httpx request/response/redirect machinery still runs.
    """
    real_client = httpx.Client

    def _patch(module: ModuleType, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def factory(*args, **kwargs):
            return real_client(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
                headers=kwargs.get("headers"),
                timeout=kwargs.get("timeout"),
            )

        monkeypatch.setattr(module.httpx, "Client", factory)
        monkeypatch.setattr(module, "polite_sleep", lambda: None)

    return _patch
