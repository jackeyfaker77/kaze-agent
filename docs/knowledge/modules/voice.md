---
title: 语音交互
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/desktop_bridge/voice/voice_handler.py
  - apps/backend/desktop_bridge/voice/voice_service.py
  - apps/backend/desktop_bridge/voice/voice_assets.py
  - apps/backend/desktop_bridge/session_service.py
  - apps/backend/agent/voice_config.py
  - apps/desktop/src/voice/controller.ts
  - apps/desktop/src/voice/bridgeEvents.ts
  - apps/desktop/src/voice/playback.ts
  - apps/desktop/src/voice/recorder.ts
related:
  - desktop-and-bridge.md
  - identity-and-configuration.md
  - ../flows/passive-turn.md
---

# 语音交互

语音输入与回复使用全局 voice 配置，并关联当前会话和 voice_turn_id。Electron 管理录音、热键、播放与交互状态，后端 VoiceService 负责识别、合成和声音资产接口。

## 输入

录音模块获取音频，经 voice.transcribe 调用后端。voice handler 在线程中执行识别并返回文本与指标；桌面再将文本作为当前会话的 chat.send 输入，进入普通 Agent 回合。

热键与会话输入使用同一交互控制器。请求 ID、session_key 和 voice_turn_id 将识别、模型回复、播放及取消关联起来。

## 回复合成

DesktopBridgeService 在生成完整回复、发布 chat.done 后调用 _speak_reply。它先发布 voice.reply.started；语音和 TTS 已启用且 voice_id 可用时，调用 VoiceService 合成完整正文，再发出 sequence=0 的 voice.tts.audio 和 voice.tts.finished。

未启用合成时直接结束语音阶段；合成失败发出 voice.tts.error 并结束。前端按 turn 关联处理音频、播放和界面状态。

## 取消与资产

voice.turn.cancel 处理当前语音回合相关的聊天任务和 TTS；预览合成使用独立的 voice.synthesize.cancel 请求。Electron 控制器同时停止当前录音或播放，避免已结束 turn 的事件继续驱动界面。

voice assets 管理克隆声音与删除相关状态，voice handler 提供资产操作接口。全局 voice.tts.voice_id 决定回复采用的声音。

## 修改影响

检查识别完成后的会话归属、聊天取消、合成任务、音频事件和播放状态。TTS 的实际触发时点是完整文本回复之后；修改为其他粒度时，需要同步后端事件、前端排序和取消处理。
