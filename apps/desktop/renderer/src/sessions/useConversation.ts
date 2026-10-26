import { useCallback, useEffect, useRef, useState } from "react";
import { BridgeRpcError, newDraftKey, rpc, type Entry, type Session } from "./api";
import { recoverAttachments, recoverDraft } from "./recoverDraft";
import { handleCommittedEvent } from "./committedEvent";

/** A draft has an address for pet voice, but is never saved by navigation. */
export function useConversation() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"local" | "remote" | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [delta, setDelta] = useState("");
  const [tool, setTool] = useState("");
  const key = useRef(newDraftKey());
  const saved = useRef(false);
  const locked = useRef(false);
  const navigating = useRef(false);
  const alive = useRef(true);
  const textSending = useRef(false);
  const pendingSends = useRef(new Map<string, { id: string }>());
  const fail = useCallback((value: unknown) => setError(value instanceof Error ? value.message : String(value)), []);
  async function refresh() {
    const result = await rpc<{ sessions: Entry[] }>("sessions.list");
    if (alive.current) setEntries(result.sessions);
  }
  async function selectCurrent() {
    await rpc(saved.current ? "session.get" : "session.draft", { session_key: key.current });
    if (alive.current) setReady(true);
  }
  useEffect(() => {
    alive.current = true;
    void (async () => { const status = await window.miraDesktop.bridgeStatus(); if (alive.current) setMode(status.mode ?? "local"); await rpc("health"); await selectCurrent(); await refresh(); if (alive.current) setReady(true); })().catch(fail);
    const resync = async () => {
      const selectedKey = key.current;
      await refresh();
      const result = await rpc<Session>("session.get", { session_key: selectedKey });
      if (alive.current && key.current === selectedKey) { saved.current = result.messages.length > 0; setSession(saved.current ? result : null); setDelta(""); setTool(""); setReady(true); }
    };
    const off = window.miraDesktop.onEvent(event => {
      if (event.method === "bridge.exit") { setReady(false); return; }
      if (event.method === "bridge.connected" || event.method === "sessions.changed") {
        void resync().catch(fail); return;
      }
      if (handleCommittedEvent(event, key.current, () => { void refresh().catch(fail); }, next => {
        if (next) { saved.current = true; setSession(next); }
        setDelta(""); setTool("");
      })) return;
      if (event.payload.session_key !== key.current) return;
      if (event.method === "chat.delta") setDelta(value => value + String(event.payload.content_delta ?? ""));
      if (event.method === "chat.tool.started") setTool(String(event.payload.tool_name ?? ""));
      if (event.method === "chat.tool.completed") setTool("");
    });
    const offVoice = window.miraDesktop.onVoiceState(payload => {
      const running = ["transcribing", "sending", "waiting_reply", "speaking_prepare", "speaking", "finish_current_sentence_then_idle"].includes(payload.status);
      // Text sends own their lock until the RPC completes.
      if (!textSending.current) { locked.current = running; setBusy(running); }
    });
    return () => { alive.current = false; off(); offVoice(); };
  }, [fail]);
  async function open(nextKey: string) {
    if (locked.current || navigating.current || (saved.current && key.current === nextKey)) return;
    navigating.current = true; setLoading(true);
    try {
      const next = await rpc<Session>("session.get", { session_key: nextKey });
      key.current = nextKey; saved.current = true; setSession(next);
      setDraft(""); setAttachments([]); setDelta(""); setError("");
    } catch (e) { fail(e); }
    finally { navigating.current = false; setLoading(false); }
  }
  async function startNew(force = false) {
    if (locked.current || navigating.current || (!saved.current && !force)) return;
    navigating.current = true; setLoading(true);
    try {
      const nextKey = newDraftKey();
      await rpc("session.draft", { session_key: nextKey });
      key.current = nextKey; saved.current = false; setSession(null);
      setDraft(""); setAttachments([]); setDelta(""); setError("");
    } catch (e) { fail(e); }
    finally { navigating.current = false; setLoading(false); }
  }
  async function send() {
    // [消息链路 1/6] 检查 Electron↔Python 通信是否就绪，并阻止重复发送、导航冲突和空消息。
    if (!ready || locked.current || navigating.current || (!draft.trim() && !attachments.length)) return;
    locked.current = true; textSending.current = true; setBusy(true); setError("");
    const content = draft, media = attachments, currentKey = key.current, previous = session;
    const signature = JSON.stringify([currentKey, content, media]);
    const attempt = pendingSends.current.get(signature) ?? { id: crypto.randomUUID() };
    pendingSends.current.set(signature, attempt);
    setDraft(""); setAttachments([]); setDelta("");
    // Optimistic update：后端返回前先把用户消息写入本地 Session，让界面立即响应。
    setSession({ session_key: currentKey, title: previous?.title || content.slice(0, 60) || "附件会话", messages: [...(previous?.messages ?? []), { role: "user", content, media }] });
    try {
      // 下一步：调用同目录 api.ts 的 rpc()，把 session_key、content、media 发给 Electron 主进程。
      const result = await rpc<{ session: Session }>("chat.send", {
        session_key: currentKey, content, media, execution_request_id: attempt.id,
      });
      if (pendingSends.current.get(signature) === attempt) pendingSends.current.delete(signature);
      saved.current = result.session.messages.length > 0; setSession(saved.current ? result.session : null);
      await refresh().catch(fail);
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      const uncertain = !(e instanceof BridgeRpcError) || ["remote_outcome_unknown", "bridge_exit", "bridge_timeout", "bridge_write_failed", "internal_error", "execution_needs_review", "execution_unavailable", "execution_outcome_unknown"].includes(e.code);
      const turn = e instanceof BridgeRpcError && typeof e.details?.turn_id === "string" ? ` 执行：${e.details.turn_id}` : "";
      setError(uncertain ? `${reason}${turn} 请在工作台核对原执行；请求标识：${attempt.id}。` : `发送失败，输入和附件已恢复。${reason}`);
      if (!uncertain) {
        pendingSends.current.delete(signature);
        setDraft(current => recoverDraft(current, content));
        setAttachments(current => recoverAttachments(current, media));
      }
      try {
        const actual = await rpc<Session>("session.get", { session_key: currentKey });
        saved.current = actual.messages.length > 0; setSession(saved.current ? actual : null); await refresh();
      } catch { setSession(previous); }
    } finally { locked.current = false; textSending.current = false; setBusy(false); setDelta(""); setTool(""); }
  }
  async function rename(title: string) {
    setSession(await rpc<Session>("session.rename", { session_key: key.current, title })); await refresh();
  }
  async function remove() {
    await rpc("session.delete", { session_key: key.current }); await startNew(true); await refresh();
  }
  return { mode, entries, session, draft, setDraft, attachments, setAttachments, error, setError, ready, busy, loading, delta, tool, fail, refresh, selectCurrent, open, startNew, send, rename, remove,
    cancel: () => rpc("chat.cancel", { session_key: key.current }) };
}
