import { useEffect, useRef, useState } from "react";
import { newDraftKey, rpc, type Entry, type Session } from "./api";
import { recoverAttachments, recoverDraft } from "./recoverDraft";

/** A draft has an address for pet voice, but is never saved by navigation. */
export function useConversation() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const [error, setError] = useState("");
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
  const fail = (value: unknown) => setError(value instanceof Error ? value.message : String(value));
  async function refresh() {
    const result = await rpc<{ sessions: Entry[] }>("sessions.list");
    if (alive.current) setEntries(result.sessions);
  }
  async function selectCurrent() {
    await rpc(saved.current ? "session.get" : "session.draft", { session_key: key.current });
  }
  useEffect(() => {
    alive.current = true;
    void (async () => { await rpc("health"); await selectCurrent(); await refresh(); if (alive.current) setReady(true); })().catch(fail);
    const off = window.miraDesktop.onEvent(event => {
      if (event.payload.session_key !== key.current) return;
      if (event.method === "chat.done" || event.method === "message.pushed") {
        const next = event.payload.session as Session | undefined;
        if (next) { saved.current = true; setSession(next); }
        setDelta(""); setTool("");
        void refresh().catch(fail);
      }
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
  }, []);
  const textSending = useRef(false);
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
    if (!ready || locked.current || navigating.current || (!draft.trim() && !attachments.length)) return;
    locked.current = true; textSending.current = true; setBusy(true); setError("");
    const content = draft, media = attachments, currentKey = key.current, previous = session;
    setDraft(""); setAttachments([]); setDelta("");
    setSession({ session_key: currentKey, title: previous?.title || content.slice(0, 60) || "附件会话", messages: [...(previous?.messages ?? []), { role: "user", content, media }] });
    try {
      const result = await rpc<{ session: Session }>("chat.send", { session_key: currentKey, content, media });
      saved.current = result.session.messages.length > 0; setSession(saved.current ? result.session : null);
      await refresh().catch(fail);
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      setError(`发送失败，输入和附件已恢复。${reason}`);
      setDraft(current => recoverDraft(current, content));
      setAttachments(current => recoverAttachments(current, media));
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
  return { entries, session, draft, setDraft, attachments, setAttachments, error, setError, ready, busy, loading, delta, tool, fail, refresh, selectCurrent, open, startNew, send, rename, remove,
    cancel: () => rpc("chat.cancel", { session_key: key.current }) };
}
