import asyncio
import pytest

from tests.backend.bootstrap.test_remote_host import remote, authenticate, rpc  # noqa: F401
from tests.backend.test_continuation import Write, seed, continuation_provider


@pytest.mark.asyncio
async def test_two_authenticated_clients_confirm_one_boundary_once(remote):
    service = remote.server.service
    tool = Write()
    service.runtime.tools.register(tool)
    turn, _ = await seed(service, tool)
    first, second = await authenticate(remote.url), await authenticate(remote.url)
    provider, wire = await continuation_provider(service)
    try:
        plans = await asyncio.gather(rpc(first, 'execution.plan', {'turn_id': turn}),
                                    rpc(second, 'execution.plan', {'turn_id': turn}))
        assert plans[0]['payload'] == plans[1]['payload']
        assert plans[0]['payload']['allowed'], plans
        payload = {'turn_id': turn, 'plan_digest': plans[0]['payload']['plan_digest'], 'confirmed': True}
        results = await asyncio.gather(rpc(first, 'execution.continue', {**payload, 'action_id': 'one'}),
                                      rpc(second, 'execution.continue', {**payload, 'action_id': 'two'}))
        assert all(not r.get('error') for r in results), results
        assert {r['payload']['created'] for r in results} == {True, False}
        assert len({r['payload']['execution']['turn_id'] for r in results}) == 1
        assert tool.calls == ['one'] and len(wire) == 1
        await first.close()
        first = await authenticate(remote.url)
        repeated = await rpc(first, 'execution.continue', {**payload, 'action_id': 'reconnect'})
        assert not repeated.get('error') and not repeated['payload']['created']
        assert len(wire) == 1
    finally:
        await first.close()
        await second.close()
        await provider._client.close()
