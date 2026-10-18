from contextlib import asynccontextmanager
import json
import logging
import sys

from mcp.server import MCPServer

from . import __version__
from .config import Settings
from .poller import Poller
from .store import Store


def create_server(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    poller: Poller | None = None,
) -> MCPServer:
    store = store or Store(settings or Settings.from_env())
    poller = poller or Poller(store)

    @asynccontextmanager
    async def lifespan(_server):
        poller.start()
        try:
            yield {}
        finally:
            await poller.aclose()

    server = MCPServer(
        "kaze-calendar",
        version=__version__,
        lifespan=lifespan,
        instructions="Read-only ICS subscriptions and local todos. Use explicit timezone offsets for task times. Reminder candidates are revision-specific and expire. Validate before sending; acknowledge only after processing. The host owns all delivery. Private subscription URLs must not be published.",
    )

    def encode(value):
        return json.dumps(value, ensure_ascii=False)

    @server.tool(structured_output=False)
    def calendar_manage(
        action: str,
        name: str = "",
        url: str = "",
        source: str = "",
        timezone: str = "",
        reminder_minutes: int | None = None,
    ) -> str:
        """管理只读 ICS 订阅：list、subscribe、pause、resume、unsubscribe。

        subscribe 需要 name 和 HTTP(S)/webcal URL。timezone 为无时区时间的兜底 IANA 时区，
        默认 Asia/Shanghai；日历自身的时区优先。reminder_minutes 为定时活动的提前分钟数，
        默认15；全天活动在日历时区的当天09:00提醒。暂停/恢复/删除使用精确 source ID 或名称。
        后台刷新；本服务不会修改源日历。私有订阅地址不在列表和状态中回显。
        """
        result = store.manage(
            action,
            name=name,
            url=url,
            source=source,
            timezone=timezone,
            reminder_minutes=reminder_minutes,
        )
        if action in {"subscribe", "resume"}:
            poller.wake()
        return encode(result)

    @server.tool(structured_output=False)
    def calendar_query(
        start_at: str, end_at: str, source: str = "", limit: int = 100, offset: int = 0
    ) -> str:
        """查询缓存日程，时间必须含 UTC 偏移，范围最多93天，支持循环与单次改期。

        返回 UTC 起止时间、时区、全天标记、备注和地点；fresh=false 表示缓存过期。
        查询不联网、不消费提醒。翻页使用 offset；先 poll_calendars 可手动刷新。
        """
        return encode(
            store.calendar_query(
                start_at, end_at, source=source, limit=limit, offset=offset
            )
        )

    @server.tool(structured_output=False)
    async def poll_calendars(source: str = "", force: bool = False) -> str:
        """刷新到期 ICS 订阅，每批最多4个；force=true 忽略间隔。

        返回每源结果和 remaining。失败保留旧缓存；超过缓存有效期后暂停产生日历提醒。
        """
        return encode(await poller.poll(source, force))

    @server.tool(structured_output=False)
    def todo_create(
        title: str,
        notes: str = "",
        due_at: str | None = None,
        reminder_at: str | None = None,
    ) -> str:
        """新建本地待办。日期使用含偏移的 ISO 时间，例如 2026-10-20T15:00:00+08:00。

        有截止时间但未指定 reminder_at 时，到截止时间生成一次提醒；两者都未指定则仅保存任务。
        reminder_at 可以早于截止时间。任务保存在本地，不同步微软或谷歌账号。
        """
        return encode(
            store.todo_create(
                title, notes=notes, due_at=due_at, reminder_at=reminder_at
            )
        )

    @server.tool(structured_output=False)
    def todo_list(status: str = "open", limit: int = 100, offset: int = 0) -> str:
        """列出本地任务；status 为 open、completed、deleted 或 all。读取不改变完成或提醒状态。"""
        return encode(store.todo_list(status, limit=limit, offset=offset))

    @server.tool(structured_output=False)
    def todo_update(todo_id: str, changes: dict) -> str:
        """按精确 todo_id 修改任务；changes 支持 title、notes、due_at、reminder_at、status。

        时间必须含偏移，null 清除相应时间；status 为 open、completed、deleted。
        completed 表示完成，deleted 表示软删除，设为 open 可重新打开并重新启用提醒。
        改截止时间时，如果提醒原来跟随截止时间，会同步改动；自定义提醒保持原值。
        """
        return encode(store.todo_update(todo_id, changes))

    @server.tool(structured_output=False)
    def get_proactive_events(consumer: str = "kaze", limit: int = 100) -> str:
        """获取当前到期且未消费的日程/待办提醒；不发送、不自动 ACK。

        consumer 必须使用目标会话 session_key。event_id 是精确版本，expires_at 是提醒截止。
        日程改期、取消、任务完成/删除会令旧版本失效。每个版本每个会话只消费一次。
        """
        return encode(store.events(consumer, limit))

    @server.tool(structured_output=False)
    def validate_proactive_events(event_ids: list[str], consumer: str = "kaze") -> str:
        """发送前重查提醒版本。valid_event_ids 为仍有效的版本；unavailable_event_ids 表示暂时无法核实。

        取消、完成、改期、过期和已经消费的版本不再有效。此工具不消费、不发送。
        """
        return encode(store.validate(event_ids, consumer))

    @server.tool(structured_output=False)
    def acknowledge_events(
        event_ids: list[str],
        consumer: str = "kaze",
        reason: str = "processed",
        delivery_ref: str = "",
    ) -> str:
        """确认精确版本，reason 为 processed、read、discarded、delivered。

        delivered 要求真实平台回执 delivery_ref。发送失败不得确认；旧版本 ACK 不影响新版本。
        ACK 不会将待办标成完成；只有用户明确完成任务时才用 todo_update 修改 status。
        """
        return encode(store.acknowledge(event_ids, consumer, reason, delivery_ref))

    @server.tool(structured_output=False)
    def calendar_status() -> str:
        """查看同步时间、错误、有效期和待办数量，不回显私有订阅 URL。"""
        now = store.clock()
        return encode(
            {
                "version": __version__,
                "timezone": store.settings.timezone,
                "calendars": [
                    {**store.public_calendar(row), "fresh": store._fresh(row, now)}
                    for row in store.calendars()
                ],
                "open_todos": store.todo_list(limit=1)["total"],
            }
        )

    return server


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    create_server().run(transport="stdio")
