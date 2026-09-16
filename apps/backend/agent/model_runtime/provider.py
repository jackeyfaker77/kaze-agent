"""把 Codex Responses 接入 Hasaki 现有 Agent 的模型接口。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.provider import ContextLengthError, LLMProvider, LLMResponse, ToolCall
from agent.model_runtime.auth.codex import CODEX_API_BASE, CodexAuthDriver
from agent.model_runtime.auth.store import workspace_store
from agent.model_runtime.catalog.codex import CodexModelCatalog
from agent.model_runtime.errors import ContextWindowError, ModelRuntimeError
from agent.model_runtime.transports.responses import CodexResponsesTransport
from agent.model_runtime.types import ModelRequest


class CodexProvider(LLMProvider):
    def __init__(self, *, workspace: Path, registration) -> None:
        if registration.base_url and registration.base_url.rstrip("/") != CODEX_API_BASE:
            raise ValueError("Codex 订阅连接使用固定的 ChatGPT 服务地址")
        self.auth = CodexAuthDriver(workspace_store(workspace), registration.id)
        self.registration = registration
        self._transports: dict[str, CodexResponsesTransport] = {}
        self._capabilities = {}
        self._catalog_lock = asyncio.Lock()

    async def chat(self, messages, tools, model, max_tokens, tool_choice="auto",
                   extra_body=None, disable_thinking=False, payload_snapshot_enabled=None,
                   on_content_delta=None) -> LLMResponse:
        try:
            async with self._catalog_lock:
                if model not in self._transports:
                    models = await CodexModelCatalog(self.auth).list_models()
                    selected = next((item for item in models if item.slug == model), None)
                    if selected is None:
                        raise ValueError("此账号的可用模型中没有当前型号，请重新选择 Codex 模型")
                    caps = selected.capabilities
                    self._capabilities[model] = caps
                    self._transports[model] = CodexResponsesTransport(
                        self.auth, runtime_id=self.registration.id,
                        use_responses_lite=caps.use_responses_lite,
                        supports_parallel_tool_calls=caps.supports_parallel_tool_calls,
                        reasoning_summary="auto" if caps.supports_reasoning_summaries else "none",
                    )
            caps = self._capabilities[model]
            effort = (extra_body or {}).get("reasoning_effort", self.registration.effort)
            effort = caps.default_reasoning_effort if effort == "none" else effort
            if effort and caps.supported_reasoning_efforts and effort not in caps.supported_reasoning_efforts:
                raise ValueError("当前 Codex 模型不支持所选推理强度，请在模型连接中调整")
            response = await self._transports[model].send(ModelRequest(
                messages=messages, tools=tools, model=model, max_output_tokens=max_tokens,
                tool_choice=tool_choice, reasoning_effort=effort, on_delta=on_content_delta,
            ))
        except ContextWindowError as exc:
            raise ContextLengthError(str(exc)) from exc
        except (ModelRuntimeError, ValueError):
            raise
        except Exception as exc:
            # 不向聊天或日志回传可能含请求头的底层异常。
            raise RuntimeError("Codex 请求失败，请检查网络或重新登录") from exc
        usage = response.usage
        total = ((usage.input_tokens or 0) + (usage.output_tokens or 0)) if usage else None
        return LLMResponse(
            content=response.content,
            tool_calls=[ToolCall(id=call.id, name=call.name, arguments=call.arguments) for call in response.tool_calls],
            thinking=response.thinking, provider_fields=response.provider_fields,
            cache_prompt_tokens=response.cache_prompt_tokens, cache_hit_tokens=response.cache_hit_tokens,
            total_tokens=total,
        )
