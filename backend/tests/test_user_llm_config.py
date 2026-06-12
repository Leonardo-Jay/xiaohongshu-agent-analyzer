import asyncio
import json

import pytest

from app.tools import llm as llm_tools
from app.tools.llm import (
    AnthropicCompatibleChatAdapter,
    LLMConfigError,
    LongcatChatAdapter,
    OpenAICompatibleChatAdapter,
    create_llm,
    normalize_user_llm_config,
    sanitize_llm_error,
)


def run_async(coro):
    return asyncio.run(coro)


def test_create_llm_without_request_config_uses_env_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "longcat")
    monkeypatch.setenv("LONGCAT_API_KEY", "env-longcat-key")
    monkeypatch.setenv("LONGCAT_MODEL", "LongCat-2.0-Preview")

    adapter = create_llm()

    assert isinstance(adapter, LongcatChatAdapter)
    assert adapter.api_key == "env-longcat-key"
    assert adapter.model == "LongCat-2.0-Preview"


def test_builtin_provider_whitelist_and_env_key(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "env-longcat-key")

    cfg = normalize_user_llm_config({
        "mode": "builtin",
        "provider": "longcat",
        "model": "LongCat-2.0-Preview",
    })

    assert cfg["provider"] == "longcat"
    assert cfg["api_key"] == "env-longcat-key"
    assert cfg["api_key_source"] == "env:LONGCAT_API_KEY"

    with pytest.raises(LLMConfigError, match="不支持所选模型"):
        normalize_user_llm_config({
            "mode": "builtin",
            "provider": "longcat",
            "model": "not-allowed",
        })


def test_custom_config_validation_and_private_url_guard(monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_LLM_ENDPOINTS", raising=False)

    cfg = normalize_user_llm_config({
        "mode": "custom",
        "api_type": "openai-compatible",
        "api_url": "https://example.com/v1",
        "model": "my-model",
        "api_key": "secret-key",
    })

    assert cfg["api_url"] == "https://example.com/v1"
    assert cfg["api_key_source"] == "request"

    with pytest.raises(LLMConfigError, match="HTTPS"):
        normalize_user_llm_config({
            "mode": "custom",
            "api_type": "openai-compatible",
            "api_url": "http://localhost:11434/v1",
            "model": "local-model",
            "api_key": "secret-key",
        })

    with pytest.raises(LLMConfigError, match="API Key"):
        normalize_user_llm_config({
            "mode": "custom",
            "api_type": "openai-compatible",
            "api_url": "https://example.com/v1",
            "model": "my-model",
            "api_key": "",
        })


def test_sanitize_llm_error_redacts_api_key_and_headers():
    message = "Authorization: Bearer secret-key x-api-key: secret-key api_key=secret-key"

    sanitized = sanitize_llm_error(message, ["secret-key"])

    assert "secret-key" not in sanitized
    assert "[REDACTED]" in sanitized


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeOpenAIClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        return _FakeResponse({
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_posts",
                            "arguments": "{\"keyword\":\"iPhone\"}",
                        },
                    }],
                },
            }],
        })

    def stream(self, method, url, headers=None, json=None):
        return _FakeStreamResponse([
            'data: {"choices":[{"delta":{"content":"你"}}]}',
            'data: {"choices":[{"delta":{"content":"好"}}]}',
            "data: [DONE]",
        ])


def test_openai_compatible_adapter_tool_calls_and_stream(monkeypatch):
    monkeypatch.setattr(llm_tools.httpx, "AsyncClient", _FakeOpenAIClient)
    adapter = OpenAICompatibleChatAdapter(
        api_url="https://example.com/v1",
        api_key="secret-key",
        model="my-model",
    )

    response = run_async(adapter.ainvoke("prompt", tools=[{"type": "function", "function": {"name": "search_posts"}}]))
    chunks = run_async(_collect_stream(adapter.astream("prompt")))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "search_posts"
    assert response.tool_calls[0].arguments == {"keyword": "iPhone"}
    assert chunks == "你好"


class _FakeAnthropicClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        return _FakeResponse({
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "需要工具"},
                {"type": "tool_use", "id": "toolu_1", "name": "search_posts", "input": {"keyword": "iPhone"}},
            ],
        })

    def stream(self, method, url, headers=None, json=None):
        return _FakeStreamResponse([
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}',
        ])


async def _collect_stream(stream):
    buffer = ""
    async for chunk in stream:
        buffer += chunk
    return buffer


def test_anthropic_compatible_adapter_tool_use_tool_result_and_stream(monkeypatch):
    monkeypatch.setattr(llm_tools.httpx, "AsyncClient", _FakeAnthropicClient)
    adapter = AnthropicCompatibleChatAdapter(
        api_url="https://anthropic.example.com/v1/messages",
        api_key="secret-key",
        model="claude-test",
    )

    response = run_async(adapter.ainvoke([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "search_posts", "arguments": "{\"keyword\":\"iPhone\"}"},
        }]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "{\"status\":\"ok\"}"},
    ], tools=[{"type": "function", "function": {"name": "search_posts", "parameters": {"type": "object"}}}]))
    chunks = run_async(_collect_stream(adapter.astream("prompt")))

    assert response.finish_reason == "tool_calls"
    assert response.content == "需要工具"
    assert response.tool_calls[0].arguments == {"keyword": "iPhone"}
    assert chunks == "你好"


def test_request_level_llm_instances_are_isolated():
    cfg_a = {
        "mode": "custom",
        "api_type": "openai-compatible",
        "provider": "custom",
        "api_url": "https://a.example.com/v1",
        "model": "model-a",
        "api_key": "secret-a",
    }
    cfg_b = {
        "mode": "custom",
        "api_type": "openai-compatible",
        "provider": "custom",
        "api_url": "https://b.example.com/v1",
        "model": "model-b",
        "api_key": "secret-b",
    }

    adapter_a = create_llm(llm_config=cfg_a)
    adapter_b = create_llm(llm_config=cfg_b)

    assert adapter_a.model == "model-a"
    assert adapter_b.model == "model-b"
    assert adapter_a.api_key == "secret-a"
    assert adapter_b.api_key == "secret-b"


def test_analysis_request_accepts_valid_llm_config():
    from app.api.v1.routes_analysis import AnalysisRequestV2

    req = AnalysisRequestV2(
        query="iPhone",
        llm_config={
            "mode": "builtin",
            "provider": "longcat",
            "model": "LongCat-2.0-Preview",
        },
    )

    assert req.llm_config.mode == "builtin"
    assert req.llm_config.provider == "longcat"
