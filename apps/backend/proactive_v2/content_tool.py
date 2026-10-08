"""Expose prefetched bodies only inside the current proactive Agent task."""

from contextlib import contextmanager
from contextvars import ContextVar
import json

from agent.tools.base import Tool

_content: ContextVar[dict[str, str]] = ContextVar("proactive_content", default={})


@contextmanager
def content_scope(content_store: dict[str, str]):
    token = _content.set(dict(content_store))
    try:
        yield
    finally:
        _content.reset(token)


class ProactiveContentTool(Tool):
    name = "proactive_read_content"
    description = "读取本轮主动候选已预取的正文，item_id 使用候选列表中的完整来源 ID。"
    parameters = {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "例如 news:article-123"}
        },
        "required": ["item_id"],
    }

    async def execute(self, item_id: str = "", **kwargs) -> str:
        store = _content.get()
        if item_id not in store:
            return json.dumps({"error": "本轮没有该候选的正文缓存"}, ensure_ascii=False)
        return json.dumps(
            {"item_id": item_id, "text": store[item_id]}, ensure_ascii=False
        )
