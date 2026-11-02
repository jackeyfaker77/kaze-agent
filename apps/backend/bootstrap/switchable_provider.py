"""Stable provider reference shared by chat, tools and background services."""
from agent.provider import LLMProvider


class DefaultModel(str):
    """A default binding, distinct from an explicitly pinned model with the same name."""


class SwitchableProvider(LLMProvider):
    def __init__(self, provider, default_model):
        self.current = provider
        self.model = default_model
        self.active_calls = 0
        self._journal = None

    def __getattr__(self, name):
        return getattr(self.current, name)

    @property
    def execution_journal(self):
        return self._journal

    @execution_journal.setter
    def execution_journal(self, journal):
        self._journal = journal
        self.current.execution_journal = journal

    def replace(self, provider, model):
        if self.active_calls:
            raise ValueError("模型请求正在进行，请稍后切换")
        provider.execution_journal = self._journal
        self.current, self.model = provider, model

    async def chat(self, messages, tools, model, max_tokens, tool_choice="auto",
                   extra_body=None, disable_thinking=False, payload_snapshot_enabled=None,
                   on_content_delta=None, require_image_input=False):
        provider = self.current
        selected = self.model if isinstance(model, DefaultModel) else model
        self.active_calls += 1
        try:
            # Recording remains on the concrete provider and sees the actual model.
            return await provider.chat(messages=messages, tools=tools, model=selected, max_tokens=max_tokens,
                tool_choice=tool_choice, extra_body=extra_body, disable_thinking=disable_thinking,
                payload_snapshot_enabled=payload_snapshot_enabled, on_content_delta=on_content_delta,
                require_image_input=require_image_input)
        finally:
            self.active_calls -= 1
