from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.provider import LLMProvider


@pytest.mark.asyncio
async def test_required_screen_image_cannot_be_silently_stripped():
    provider = LLMProvider(api_key="test-key", provider_name="deepseek")
    provider._create_with_retry = AsyncMock()
    try:
        with pytest.raises(ValueError, match="不支持图片输入"):
            await provider.chat(
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "describe screen"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}},
                ]}], tools=[], model="deepseek-chat", max_tokens=20,
                require_image_input=True,
            )
        provider._create_with_retry.assert_not_awaited()
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_keyless_local_provider_can_start_and_complete(monkeypatch):
    import httpx

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LLMProvider(api_key="", base_url="http://127.0.0.1:12345/v1")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "local-response", "object": "chat.completion", "created": 0,
            "model": "local-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "local reply"}}],
        })

    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await provider.chat(messages=[{"role": "user", "content": "hello"}],
                                     tools=[], model="local-model", max_tokens=20)
        assert result.content == "local reply"
        assert len(requests) == 1
        assert requests[0].url.host == "127.0.0.1"
    finally:
        await provider._client.close()


def test_provider_disables_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

    monkeypatch.setattr("agent.provider.AsyncOpenAI", FakeAsyncOpenAI)

    LLMProvider(api_key="test-key", base_url="https://example.test/v1")

    assert created["max_retries"] == 0


@pytest.mark.asyncio
async def test_provider_retries_once_after_a_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LLMProvider(api_key="test-key", max_retries=1)
    completion = object()
    create = AsyncMock(side_effect=[TimeoutError("upstream unavailable"), completion])
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    sleep = AsyncMock()
    monkeypatch.setattr("agent.provider.asyncio.sleep", sleep)

    result = await provider._create_with_retry({"model": "test-model"})

    assert result is completion
    assert create.await_count == 2
    sleep.assert_awaited_once_with(1.0)
