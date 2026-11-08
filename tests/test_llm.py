"""Summary path through the real openai SDK, with only the HTTP transport stubbed.

Swapping the transport (not the client class) means the SDK's request building,
retry logic and response parsing still run, so an incompatible ``openai`` release
fails here. No API key and no network are needed.
"""

from __future__ import annotations

import asyncio
import functools
import json

import httpx
import openai
import pytest
from conftest import ALL_NEWS_MODULES, load_module

token_usage = load_module("_token_usage")


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


@pytest.fixture
def llm(monkeypatch, tmp_path):
    """Configure env + token log and route AsyncOpenAI through a MockTransport."""
    log = tmp_path / "tokens.jsonl"
    monkeypatch.setattr(token_usage, "_TOKEN_LOG_PATH", str(log))
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "stub-model")

    class Ctl:
        requests: list[httpx.Request] = []
        log_path = log

        def install(self, module, handler):
            def wrapped(request: httpx.Request) -> httpx.Response:
                self.requests.append(request)
                return handler(request)

            http_client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
            monkeypatch.setattr(
                module,
                "AsyncOpenAI",
                functools.partial(openai.AsyncOpenAI, http_client=http_client),
            )

    ctl = Ctl()
    ctl.requests = []
    return ctl


@pytest.mark.parametrize("name", ALL_NEWS_MODULES)
def test_summary_success_logs_token_usage(llm, name: str) -> None:
    mod = load_module(name)
    llm.install(mod, lambda r: httpx.Response(200, json=_completion("  A concise summary.  ")))

    summary = asyncio.run(mod.get_summary("x" * 9000, "Title"))

    assert summary == "A concise summary."
    assert len(llm.requests) == 1
    req = llm.requests[0]
    assert req.url.path == "/v1/chat/completions"
    body = json.loads(req.content)
    assert body["model"] == "stub-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert len(body["messages"][1]["content"]) < 4200  # input truncated to 4000 chars
    assert req.headers["authorization"] == "Bearer not-needed"  # no key configured

    records = [json.loads(line) for line in llm.log_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["prompt_tokens"] == 11 and records[0]["completion_tokens"] == 7
    assert records[0]["model"] == "stub-model"
    assert records[0]["call_type"].endswith("_summary")


@pytest.mark.parametrize("name", ALL_NEWS_MODULES)
def test_summary_skipped_without_base_url(monkeypatch, name: str) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    mod = load_module(name)

    def fail(*a, **k):
        raise AssertionError("AsyncOpenAI must not be constructed without LLM_BASE_URL")

    monkeypatch.setattr(mod, "AsyncOpenAI", fail)
    assert asyncio.run(mod.get_summary("body", "Title")) == ""


@pytest.mark.parametrize("name", ALL_NEWS_MODULES)
def test_summary_api_error_degrades_to_empty_string(llm, name: str) -> None:
    mod = load_module(name)
    llm.install(mod, lambda r: httpx.Response(400, json={"error": {"message": "bad request"}}))
    assert asyncio.run(mod.get_summary("body", "Title")) == ""
    assert not llm.log_path.exists()


def test_openai_retries_transient_5xx(llm, monkeypatch) -> None:
    mod = load_module("agc_news")
    statuses = iter([500, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if next(statuses) == 500:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=_completion("ok"))

    # skip the SDK's real backoff sleep
    monkeypatch.setattr(
        openai.AsyncOpenAI, "_calculate_retry_timeout", lambda *a, **k: 0, raising=False
    )
    llm.install(mod, handler)
    assert asyncio.run(mod.get_summary("body", "Title")) == "ok"
    assert len(llm.requests) == 2


def test_generate_summaries_fills_every_item(llm) -> None:
    mod = load_module("agc_news")
    llm.install(mod, lambda r: httpx.Response(200, json=_completion("sum")))
    items = [{"title": "a", "content_text": "x"}, {"title": "b", "content_text": ""}]
    out = asyncio.run(mod.generate_summaries(items))
    assert [i["summary"] for i in out] == ["sum", "sum"]


def test_token_log_failure_does_not_break_summary(llm, monkeypatch, tmp_path) -> None:
    mod = load_module("agc_news")
    # a path whose parent is a file: makedirs/open raise OSError, which must be swallowed
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(token_usage, "_TOKEN_LOG_PATH", str(blocker / "sub" / "log.jsonl"))
    llm.install(mod, lambda r: httpx.Response(200, json=_completion("fine")))
    assert asyncio.run(mod.get_summary("body", "Title")) == "fine"
