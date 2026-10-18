import httpx
import pytest

from conftest import event, ics
from kaze_calendar.poller import Poller


async def test_sync_conditional_get_and_malformed_response_dont_advance_cache(
    store, calendar
):
    requests = []
    responses = [
        httpx.Response(304),
        httpx.Response(200, content=b"broken", headers={"etag": '"v2"'}),
        httpx.Response(200, content=ics(event()), headers={"etag": '"v3"'}),
    ]

    def handle(request):
        requests.append(request)
        return responses.pop(0)

    poller = Poller(store, transport=httpx.MockTransport(handle))
    assert (await poller.poll(force=True))["ok"]
    assert requests[0].headers["If-None-Match"] == '"v1"'
    assert not (await poller.poll(force=True))["ok"]
    current = store.calendars(calendar["id"])[0]
    assert current["body"] == calendar["body"] and current["etag"] == '"v1"'
    assert current["last_error"].startswith("invalid_calendar:")
    assert (await poller.poll(force=True))["ok"]
    assert requests[2].headers["If-None-Match"] == '"v1"'
    assert store.calendars(calendar["id"])[0]["etag"] == '"v3"'


async def test_one_failed_calendar_does_not_hide_another_or_todos(store):
    for name in ("good", "bad"):
        store.manage("subscribe", name=name, url=f"https://example.test/{name}.ics")

    def handle(request):
        return (
            httpx.Response(200, content=ics(event()))
            if "good" in request.url.path
            else httpx.Response(503)
        )

    result = await Poller(store, transport=httpx.MockTransport(handle)).poll(force=True)
    assert sorted(row["ok"] for row in result["results"]) == [False, True]
    store.todo_create("本地任务", reminder_at="2026-10-16T08:45:00Z")
    assert {row["type"] for row in store.events("session1")} == {"todo", "calendar"}
    assert "https://" not in str(result)


async def test_background_refresh_runs_and_stops_cleanly(store):
    store.manage("subscribe", name="source", url="https://example.test/a.ics")
    poller = Poller(
        store,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=ics(event()))
        ),
    )
    poller.start()
    try:
        import asyncio

        for _ in range(100):
            if store.calendars()[0]["last_success_at"]:
                break
            await asyncio.sleep(0.01)
        assert store.calendars()[0]["last_success_at"]
    finally:
        await poller.aclose()


async def test_poll_batch_rotates_and_reports_remaining(store):
    for i in range(6):
        store.manage(
            "subscribe", name=f"source-{i}", url=f"https://example.test/{i}.ics"
        )
    poller = Poller(
        store,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=ics(event()))
        ),
    )
    assert (await poller.poll())["remaining"] == 2
    assert (await poller.poll())["remaining"] == 0
    assert all(row["last_success_at"] for row in store.calendars())


async def test_malformed_calendars_do_not_starve_later_batches(store):
    for name in ("bad1", "bad2", "bad3", "bad4", "good"):
        store.manage("subscribe", name=name, url=f"https://example.test/{name}.ics")
    requests = []

    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            content=ics(
                event(
                    extra=""
                    if request.url.path == "/good.ics"
                    else "DTSTART:20261016T100000Z"
                )
            ),
        )

    poller = Poller(store, transport=httpx.MockTransport(handle))
    first = await poller.poll()
    assert not first["ok"] and first["remaining"] == 1
    assert len(first["results"]) == 4
    assert all(not result["ok"] for result in first["results"])
    assert (await poller.poll())["ok"]
    assert requests == ["/bad1.ics", "/bad2.ics", "/bad3.ics", "/bad4.ics", "/good.ics"]
    for row in store.calendars():
        assert row["last_checked_at"]
        if row["name"] == "good":
            assert row["last_success_at"] and row["body"]
        else:
            assert row["last_error"] == "invalid_calendar:ValueError"
            assert not row["body"]
    store.todo_create("本地任务", reminder_at="2026-10-16T08:45:00Z")
    assert {row["type"] for row in store.events("session1")} == {"todo", "calendar"}


@pytest.mark.parametrize("error_type", [AttributeError, RuntimeError])
async def test_unexpected_parser_failure_is_isolated_and_preserves_cache(
    store,
    calendar,
    monkeypatch,
    error_type,
):
    from kaze_calendar.parsing import occurrences

    store.manage("subscribe", name="healthy", url="https://example.test/good.ics")

    def parse(body, source_id, *args):
        if source_id == calendar["id"]:
            raise error_type("private-url-token")
        return occurrences(body, source_id, *args)

    monkeypatch.setattr("kaze_calendar.poller.occurrences", parse)
    poller = Poller(
        store,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=ics(event()), headers={"etag": '"v2"'}
            )
        ),
    )
    result = await poller.poll(force=True)
    assert sorted(row["ok"] for row in result["results"]) == [False, True]
    current = store.calendars(calendar["id"])[0]
    assert current["body"] == calendar["body"]
    assert current["etag"] == calendar["etag"]
    assert current["last_success_at"] == calendar["last_success_at"]
    assert current["last_error"] == f"invalid_calendar:{error_type.__name__}"
    assert "private-url-token" not in str(result)


async def test_poll_cancellation_is_not_swallowed(store, calendar, monkeypatch):
    import asyncio

    def cancel(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr("kaze_calendar.poller.occurrences", cancel)
    poller = Poller(
        store,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=ics(event()))
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await poller.poll(force=True)
    assert store.calendars(calendar["id"])[0]["last_error"] == ""
