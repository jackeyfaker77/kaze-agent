"""Atomic model-only configuration updates owned by the resident Host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from agent.config_models import Config
from bootstrap.providers import build_providers
from bootstrap.switchable_provider import DefaultModel, SwitchableProvider


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_models(content: str, registrations: list) -> str:
    if not isinstance(registrations, list) or not registrations or len(registrations) > 100:
        raise ValueError("至少需要一个模型注册，最多 100 个")
    if re.search(r'^\s*\[llm\.registrations[.\]]', content, re.M):
        raise ValueError("模型配置含嵌套表，请先整理配置格式")
    fields = {"id": "id", "provider": "provider", "base_url": "baseUrl", "api_key": "apiKey", "model": "model", "effort": "effort"}
    blocks = []
    for item in registrations:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in fields.values()):
            raise ValueError("模型注册格式无效")
        blocks.append('[[llm.registrations]]\n' + '\n'.join(
            f'{key} = {json.dumps(item[value] if key == "api_key" else item[value].strip(), ensure_ascii=False)}'
            for key, value in fields.items()) + '\n')
    pattern = r'^\[\[llm\.registrations\]\][^\r\n]*\r?\n[\s\S]*?(?=^\s*\[|\Z)'
    first = True
    def substitute(_):
        nonlocal first
        if not first:
            return ''
        first = False
        return '\n'.join(blocks) + '\n'
    result, count = re.subn(pattern, substitute, content, flags=re.M)
    if not count:
        raise ValueError("没有找到模型注册表，未修改配置")
    return result


class HostModelSettings:
    def __init__(self, runtime, path: Path):
        self.runtime = runtime
        self.path = path.resolve()
        self.revision = revision(self.path.read_bytes())

    def save(self, payload):
        router = self.runtime.provider
        if not isinstance(router, SwitchableProvider):
            raise ValueError("Host 不支持在线模型切换，请更新 Host")
        if router.active_calls or self.runtime.loop.active_turn_states:
            raise ValueError("Host 有正在进行的对话或模型请求，请等待完成后再切换")
        original = self.path.read_bytes()
        if payload.get("expected_revision") != self.revision or revision(original) != self.revision:
            raise ValueError("模型配置已变化，请重新加载后再保存")
        candidate = self.path.with_name(f'.{self.path.stem}.{uuid4().hex}.candidate.toml')
        try:
            content = replace_models(original.decode('utf-8-sig'), payload.get('registrations'))
            with candidate.open('xb') as stream:
                stream.write(content.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            config = Config.load(candidate)
            provider, _, _ = build_providers(config, self.runtime.session_manager.workspace)
            # No await between admission, persistence and pointer swap: no turn can
            # begin midway, and all existing consumers retain the same router.
            provider.execution_journal = router.execution_journal
            if self.path.read_bytes() != original:
                raise ValueError("配置已被其他程序修改，请重新加载后再保存")
            self.path.with_suffix('.toml.before-model-switch.bak').write_bytes(original)
            os.replace(candidate, self.path)
            router.replace(provider, config.model)
            # Previously constructed default bindings also follow the router;
            # explicit background model names remain pinned, even when equal.
            self.runtime.config.model = DefaultModel(config.model)
            for field in ('model_registrations', 'provider', 'api_key', 'base_url', 'extra_body'):
                setattr(self.runtime.config, field, getattr(config, field))
            self.revision = revision(content.encode('utf-8'))
            return {"ok": True, "health": {"ok": True, "message": "模型已切换，Host 持续运行"}, "revision": self.revision}
        finally:
            candidate.unlink(missing_ok=True)
