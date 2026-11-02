import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.config_models import Config
from bootstrap.switchable_provider import DefaultModel, SwitchableProvider
from desktop_bridge import host_models


FIRST = {"id": "00000000-0000-4000-a000-000000000001", "provider": "openai", "model": "first", "apiKey": "offline", "baseUrl": "", "effort": "none"}
SECOND = {**FIRST, "id": "00000000-0000-4000-a000-000000000002", "model": "second"}


@pytest.fixture
def models(tmp_path, monkeypatch):
    content = '[[llm.registrations]]\nid = "placeholder"\nmodel = "placeholder"\n[plugins.test]\nvalue = ["x", "y"] # kept\n'
    path = tmp_path / 'config.toml'
    path.write_text(host_models.replace_models(content, [FIRST, SECOND]), encoding='utf-8')
    async def chat(**kwargs):
        return kwargs['model']
    def build(config, workspace):
        return SimpleNamespace(chat=chat, tag=config.model), None, None
    monkeypatch.setattr(host_models, 'build_providers', build)
    config = Config.load(path)
    router = SwitchableProvider(build(config, tmp_path)[0], config.model)
    router.execution_journal = object()
    runtime = SimpleNamespace(provider=router, config=config, loop=SimpleNamespace(active_turn_states={}),
                              session_manager=SimpleNamespace(workspace=tmp_path))
    return host_models.HostModelSettings(runtime, path)


@pytest.mark.asyncio
async def test_switch_preserves_shared_provider_and_actual_next_call(models):
    router = models.runtime.provider
    journal = router.execution_journal
    before = models.path.read_text(encoding='utf-8')
    assert await router.chat([], [], DefaultModel('first'), 100) == 'first'
    result = models.save({'expected_revision': models.revision, 'registrations': [SECOND, FIRST]})
    assert result['ok']
    assert models.runtime.provider is router
    assert router.current.execution_journal is journal
    assert await router.chat([], [], DefaultModel('first'), 100) == 'second'
    assert await router.chat([], [], 'explicit-background-model', 100) == 'explicit-background-model'
    assert await router.chat([], [], 'first', 100) == 'first'  # explicitly pinned, not a default binding
    assert Config.load(models.path).model == 'second'
    assert models.path.read_text(encoding='utf-8').split('[plugins.test]')[1] == before.split('[plugins.test]')[1]
    assert models.path.with_suffix('.toml.before-model-switch.bak').read_text(encoding='utf-8') == before
    models.save({'expected_revision': models.revision, 'registrations': [FIRST, SECOND]})
    assert await router.chat([], [], DefaultModel('first'), 100) == 'first'


@pytest.mark.asyncio
async def test_active_model_call_and_active_turn_block_save(models):
    started, release = asyncio.Event(), asyncio.Event()
    async def chat(**kwargs):
        started.set()
        await release.wait()
        return kwargs['model']
    models.runtime.provider.current.chat = chat
    original = models.path.read_bytes()
    pending = asyncio.create_task(models.runtime.provider.chat([], [], DefaultModel('first'), 100))
    await started.wait()
    try:
        with pytest.raises(ValueError, match='正在进行'):
            models.save({'expected_revision': models.revision, 'registrations': [SECOND]})
        assert models.path.read_bytes() == original
    finally:
        release.set()
        assert await pending == 'first'
    models.runtime.loop.active_turn_states['desktop:x'] = object()
    with pytest.raises(ValueError, match='正在进行'):
        models.save({'expected_revision': models.revision, 'registrations': [SECOND]})


def test_invalid_or_stale_save_never_changes_running_model_or_config(models, monkeypatch):
    original, provider, rev = models.path.read_bytes(), models.runtime.provider.current, models.revision
    for payload in [{'expected_revision': 'stale', 'registrations': [SECOND]},
                    {'expected_revision': rev, 'registrations': [SECOND, SECOND]},
                    {'expected_revision': rev, 'registrations': []}]:
        with pytest.raises(ValueError):
            models.save(payload)
        assert models.path.read_bytes() == original
        assert models.runtime.provider.current is provider
    def fail_replace(*_):
        raise PermissionError('locked')
    monkeypatch.setattr(host_models.os, 'replace', fail_replace)
    with pytest.raises(PermissionError):
        models.save({'expected_revision': rev, 'registrations': [SECOND]})
    assert models.path.read_bytes() == original
    assert models.runtime.provider.current is provider
    assert models.revision == rev


def test_external_config_edit_conflicts_and_secret_text_stays_in_string(models):
    updated = {**SECOND, 'apiKey': 'quote"\n[channels.telegram]\ntoken="changed"'}
    rendered = host_models.replace_models(models.path.read_text(encoding='utf-8'), [updated])
    import tomllib
    assert tomllib.loads(rendered)['llm']['registrations'][0]['api_key'] == updated['apiKey']
    models.path.write_bytes(models.path.read_bytes() + b'\n# external edit\n')
    with pytest.raises(ValueError, match='已变化'):
        models.save({'expected_revision': models.revision, 'registrations': [SECOND]})
