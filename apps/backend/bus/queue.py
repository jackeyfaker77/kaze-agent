import asyncio
import logging
from dataclasses import asdict
from datetime import datetime
from contextvars import ContextVar
from collections.abc import Awaitable, Callable

from bus.durable import TransportStore, Outbox
from bus.transport_review import inbound_account
from bus.events import InboundItem, InboundMessage, OutboundMessage, SpawnCompletionItem
from bus.internal_events import SpawnCompletionEvent

from bus.chat_lane import ChatLane
from core.execution.contracts import ExecutionUnavailable

inbound_job: ContextVar[str | None] = ContextVar('inbound_job', default=None)
spawn_delivery_account: ContextVar[tuple[str, str | None] | None] = ContextVar('spawn_delivery_account', default=None)
logger = logging.getLogger(__name__)


class MessageBus:
    """agent 与各 channel 之间的异步消息总线"""

    def __init__(self, chat_lane: ChatLane | None = None) -> None:
        self._inbound: asyncio.Queue[InboundItem] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._subscribers: dict[
            str, list[Callable[[OutboundMessage], Awaitable[None]]]
        ] = {}
        self._running = False
        self.chat_lane = chat_lane or ChatLane()
        self.transport: TransportStore | None = None
        self.outbox: Outbox | None = None
        self.journal = None

    def enable_durability(self, workspace, journal):
        self.transport = TransportStore(workspace)
        self.outbox = Outbox(self.transport, self.chat_lane)
        self.journal = journal

    async def publish_inbound(self, msg: InboundItem) -> None:
        """channel → agent"""
        key = msg.metadata.get('_transport_id')
        if self.transport is not None and key and isinstance(msg, InboundMessage):
            payload = asdict(msg)
            payload['timestamp'] = msg.timestamp.isoformat()
            self.transport.put(key, 'inbound', payload)
            return
        await self.chat_lane.mark_passive_pending(msg.channel, msg.chat_id)
        await self._inbound.put(msg)

    async def consume_inbound(self) -> InboundItem:
        """阻塞直到有消息可消费"""
        while True:
            if self.journal is not None:
                completion = self.journal.claim_spawn_completion(accounts=self.outbox.accounts if self.outbox else {})
                if completion is not None:
                    token = completion.pop('claim_token')
                    completion['event'] = SpawnCompletionEvent(**completion['event'])
                    completion['timestamp'] = datetime.fromisoformat(completion['timestamp'])
                    completion['metadata'] = {**completion.get('metadata', {}), '_spawn_claim': token}
                    msg = SpawnCompletionItem(**completion)
                    await self.chat_lane.mark_passive_pending(msg.channel, msg.chat_id)
                    return msg
            if self.transport is not None:
                rows = self.transport.pending('inbound')
                if rows:
                    row = rows[0]
                    account = inbound_account(row)
                    if account is not None and (self.outbox is None or account != self.outbox.accounts.get(row['payload'].get('channel'))):
                        self.transport.state(row['id'], 'needs_review', error='Channel account changed')
                        continue
                    payload = dict(row['payload'])
                    payload['timestamp'] = datetime.fromisoformat(payload['timestamp'])
                    msg = InboundMessage(**payload)
                    self.transport.attempt(row['id'])
                    self.transport.active.add(row['id'])
                    await self.chat_lane.mark_passive_pending(msg.channel, msg.chat_id)
                    return msg
            try:
                return await asyncio.wait_for(self._inbound.get(), .1)
            except TimeoutError:
                pass

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """agent → channel"""
        if self.outbox is not None and msg.channel not in {'desktop', 'cli'}:
            binding = spawn_delivery_account.get()
            expected = binding[1] if binding is not None and binding[0] == msg.channel else msg.metadata.get('_spawn_account')
            if binding is not None or '_spawn_account' in msg.metadata:
                self._validate_spawn_account(msg.channel, expected)
            key = inbound_job.get() or msg.metadata.get('_transport_id')
            payload = asdict(msg)
            payload['media'] = [self.transport.snapshot_file(p) if not p.startswith(('http://', 'https://')) else p for p in msg.media]
            self.outbox.enqueue('bus:'+msg.channel, payload, 'reply:'+key if key else None)
            return
        await self.chat_lane.mark_passive_send_pending(msg.channel, msg.chat_id)
        await self._outbound.put(msg)

    async def complete_inbound(self, msg: InboundItem) -> None:
        if isinstance(msg, SpawnCompletionItem) and self.journal is not None and msg.metadata.get('_spawn_claim'):
            handled = bool(msg.metadata.get('_spawn_handled'))
            if handled:
                try:
                    self._ensure_spawn_reply(msg)
                except ExecutionUnavailable:
                    handled = False  # Account changed while processing; leave unacknowledged.
            self.journal.settle_spawn_completion(msg.event.job_id, msg.metadata['_spawn_claim'], handled=handled)
        key = msg.metadata.get('_transport_id')
        if self.transport is not None and key:
            record = self.journal.find(msg.session_key, msg.channel, msg.metadata['execution_request_id']) if self.journal else None
            state = 'done' if record and record['status'] == 'completed' else 'needs_review' if record else 'pending'
            if state == 'done' and self.transport.db.execute(
                'SELECT 1 FROM jobs WHERE id=?', ('reply:'+key,),
            ).fetchone() is None:
                state = 'pending'  # Completed execution still needs its durable reply intent.
            if record:
                self.transport.link_execution(key, record['turn_id'])
            if state == 'pending' and self.transport.get(key)['attempts'] >= self.transport.get(key)['attempt_limit']:
                state = 'failed'
            self.transport.state(key, state, delay=2)
            self.transport.active.discard(key)
        await self.chat_lane.mark_passive_done(msg.channel, msg.chat_id)

    def _validate_spawn_account(self, channel, expected):
        if channel not in {'desktop', 'cli'} and (
                not expected or self.outbox is None or expected != self.outbox.accounts.get(channel)):
            raise ExecutionUnavailable('后台结果的原渠道账号不可用，请核对渠道配置')

    def validate_spawn_account(self, msg: SpawnCompletionItem):
        self._validate_spawn_account(msg.channel, msg.metadata.get('_spawn_account'))

    def _ensure_spawn_reply(self, msg: SpawnCompletionItem) -> None:
        """Consumer receipt and channel delivery are separate durable responsibilities."""
        if self.transport is None or self.outbox is None or msg.channel in {'desktop', 'cli'}:
            return
        self.validate_spawn_account(msg)
        receipt = self.journal.spawn_completion(msg.event.job_id)
        if not receipt or not receipt['consumer_turn_id']:
            return
        record = self.journal.get(receipt['consumer_turn_id'])
        if record['status'] in {'accepted', 'running'}:
            return
        reply_key = 'reply:spawn:' + msg.event.job_id
        if self.transport.db.execute('SELECT 1 FROM jobs WHERE id=?', (reply_key,)).fetchone():
            return  # Includes unknown physical sends; never replace their payload.
        payload = record['result'] if record['status'] == 'completed' else None
        if payload is None:
            payload = asdict(OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                content=f"后台结果已接纳，但后续执行未完成。请核对执行 {record['turn_id']}，不会自动重跑。",
                metadata={'session_key_override': msg.session_key, 'execution_notice': record['turn_id'],
                          'spawn_completion_notice': True}))
        else:
            payload = {**payload, 'media': [self.transport.snapshot_file(p)
                       if not p.startswith(('http://', 'https://')) else p for p in payload.get('media', [])]}
        self.outbox.enqueue('bus:' + msg.channel, payload, reply_key)
        self.transport.link_execution(reply_key, record['turn_id'])


    def subscribe_outbound(
        self,
        channel: str,
        callback: Callable[[OutboundMessage], Awaitable[None]],
        *, durable_parts: bool = False,
    ) -> None:
        """订阅某 channel 的出站消息"""
        self._subscribers.setdefault(channel, []).append(callback)
        if self.outbox is not None:
            async def send(payload):
                for index, cb in enumerate(self._subscribers.get(channel, [])):
                    if durable_parts:
                        await cb(OutboundMessage(**payload))
                    else:
                        await self.outbox.part(str(index), payload, lambda: cb(OutboundMessage(**payload)))
                return {}
            self.outbox.handlers['bus:'+channel] = send

    def unsubscribe_outbound(
        self, channel: str, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        subscribers = self._subscribers.get(channel, [])
        if callback in subscribers:
            subscribers.remove(callback)
        if not subscribers:
            self._subscribers.pop(channel, None)

    async def dispatch_outbound(self) -> None:
        """后台任务：将出站消息分发给对应 channel 的订阅者。

        发送失败时退避 2s 重试一次；仍失败则向用户发送降级错误通知，不静默丢弃。
        """
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._outbound.get(), timeout=1.0)
                await self.chat_lane.run_passive(
                    msg.channel, msg.chat_id, lambda: self._send_outbound(msg)
                )
            except asyncio.TimeoutError:
                continue

    async def _send_outbound(self, msg: OutboundMessage) -> None:
        for cb in self._subscribers.get(msg.channel, []):
            try:
                await cb(msg)
            except Exception as first_err:
                logger.warning(
                    f"分发消息到 {msg.channel} 首次失败，2s 后重试: {first_err}"
                )
                await asyncio.sleep(2)
                try:
                    await cb(msg)
                except Exception as second_err:
                    logger.error(
                        f"分发消息到 {msg.channel} 重试仍失败，发送降级通知: {second_err}"
                    )
                    fallback = OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="（消息发送失败，请稍后重试）",
                        metadata=dict(msg.metadata or {}),
                    )
                    try:
                        await cb(fallback)
                    except Exception:
                        logger.error(
                            f"降级通知也失败，消息彻底丢失  channel={msg.channel} "
                            f"chat_id={msg.chat_id}"
                        )

    def stop(self) -> None:
        self._running = False

    @property
    def inbound_size(self) -> int:
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self._outbound.qsize()
