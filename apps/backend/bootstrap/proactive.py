"""Session-based heartbeat, sharing the ordinary Agent and delivery services."""
import asyncio
import logging

logger = logging.getLogger(__name__)

async def run_proactive(runtime):
    config = runtime.config.proactive
    channel = config.default_channel
    chat_id = config.default_chat_id or config.session_key
    if not config.enabled or not chat_id:
        return
    key = config.session_key or (chat_id if channel == "desktop" else f"{channel}:{chat_id}")
    while True:
        await asyncio.sleep(max(60, config.interval_seconds))
        instructions = runtime.session_manager.workspace / "HEARTBEAT.md"
        if not instructions.exists() or key in runtime.loop._active_tasks:
            continue
        prompt = instructions.read_text(encoding="utf-8").strip()
        if not prompt:
            continue
        try:
            reply = await runtime.loop.process_direct(
                "执行以下主动检查。无可通知结果时只回复 NO_REPLY。\n" + prompt,
                session_key=key, channel=channel, chat_id=chat_id,
                omit_user_turn=True, skip_post_memory=True,
                disabled_tools=["message_push"],
            )
            if reply.strip() and reply.strip() != "NO_REPLY":
                await runtime.push_tool.execute(channel=channel, chat_id=chat_id, message=reply, already_persisted=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Proactive check failed for %s", key)
