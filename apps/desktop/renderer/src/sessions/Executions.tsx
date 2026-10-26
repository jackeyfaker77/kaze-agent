import { useEffect, useRef, useState } from "react";
import { rpc } from "./api";

export type Execution = {
  turn_id: string; run_id: string; session_key: string; status: string; last_seq: number;
  input: unknown; result: unknown; error_type: string | null; retry_of: string | null;
  retry_turn_id: string | null; retry: { allowed: boolean; reason: string };
  review: { decision: string; note: string } | null; can_review_no_effect: boolean;
  effects: { effect_id: string; name?: string; kind: string; certainty: string; retry_safe: boolean; arguments?: unknown; outcome?: unknown }[];
};
type Fact = { seq: number; kind: string; created_at: string; payload: unknown };
const labels: Record<string, string> = { failed: "失败", interrupted: "已中断", needs_review: "待核对", accepted: "已接纳", running: "执行中", completed: "已完成" };

export function Executions({ sessionKey, initialTurnId = "", onQueue }: { sessionKey: string; initialTurnId?: string; onQueue?: (id: string) => void }) {
  const [status, setStatus] = useState("attention");
  const [rows, setRows] = useState<Execution[]>([]);
  const [cursor, setCursor] = useState(0);
  const [more, setMore] = useState(false);
  const [detail, setDetail] = useState<Execution | null>(null);
  const [events, setEvents] = useState<Fact[]>([]);
  const [eventCursor, setEventCursor] = useState(0);
  const [moreEvents, setMoreEvents] = useState(false);
  const [note, setNote] = useState("");
  const [decision, setDecision] = useState("close");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const requests = useRef({ generation: 0, selection: 0 });
  const actionLock = useRef(false);
  useEffect(() => {
    const counters = requests.current;
    const current = ++requests.current.generation;
    ++requests.current.selection;
    setLoading(true); setError(""); setRows([]); setDetail(null); setEvents([]); setCursor(0); setMore(false);
    void rpc<{ executions: Execution[]; next_cursor: number }>("executions.list", { session_key: sessionKey, status, limit: 25 })
      .then(data => { if (requests.current.generation === current) { setRows(data.executions); setCursor(data.next_cursor); setMore(data.executions.length === 25); if (initialTurnId) void open(initialTurnId); } })
      .catch(e => { if (requests.current.generation === current) setError(String(e)); })
      .finally(() => { if (requests.current.generation === current) setLoading(false); });
    return () => { ++counters.generation; ++counters.selection; };
  }, [sessionKey, status, refresh, initialTurnId]);

  async function loadMore() {
    const current = requests.current.generation;
    setLoading(true); setError("");
    try {
      const data = await rpc<{ executions: Execution[]; next_cursor: number }>("executions.list", { session_key: sessionKey, status, before: cursor, limit: 25 });
      if (requests.current.generation === current) { setRows(old => [...old, ...data.executions]); setCursor(data.next_cursor); setMore(data.executions.length === 25); }
    } catch (e) { if (requests.current.generation === current) setError(String(e)); }
    finally { if (requests.current.generation === current) setLoading(false); }
  }
  async function open(turnId: string) {
    const current = ++requests.current.selection;
    setDetail(null); setEvents([]); setNote(""); setDecision("close");
    try {
      const [record, page] = await Promise.all([
        rpc<Execution>("execution.get", { turn_id: turnId }),
        rpc<{ events: Fact[]; next_cursor: number }>("execution.events", { turn_id: turnId, limit: 50 }),
      ]);
      if (requests.current.selection === current) {
        setDetail(record); setEvents(page.events); setEventCursor(page.next_cursor); setMoreEvents(page.events.length === 50);
        setRows(old => old.map(row => row.turn_id === record.turn_id ? record : row));
      }
    } catch (e) { if (requests.current.selection === current) setError(String(e)); }
  }
  async function loadEvents() {
    if (!detail) return;
    const current = requests.current.selection;
    setLoading(true);
    try {
      const page = await rpc<{ events: Fact[]; next_cursor: number }>("execution.events", { turn_id: detail.turn_id, after: eventCursor, limit: 50 });
      if (requests.current.selection === current) { setEvents(old => [...old, ...page.events]); setEventCursor(page.next_cursor); setMoreEvents(page.events.length === 50); }
    } catch (e) { if (requests.current.selection === current) setError(String(e)); }
    finally { setLoading(false); }
  }
  async function act(method: "execution.review" | "execution.retry") {
    if (!detail || actionLock.current) return;
    const current = requests.current.generation;
    actionLock.current = true; setBusy(true); setError(""); setMessage("");
    try {
      await rpc(method, { turn_id: detail.turn_id, expected_seq: detail.last_seq, action_id: crypto.randomUUID(), ...(method === "execution.review" ? { decision, note } : {}) });
      setMessage(method === "execution.review" ? "处置结论已保存，原执行状态和回执保留。" : "重试已返回，请查看关联执行的状态与结果。");
    } catch (e) { setError(`${String(e)}。请重新查看执行记录，确认是否已接纳。`); }
    finally { actionLock.current = false; setBusy(false); if (requests.current.generation === current) await open(detail.turn_id); }
  }
  const canReview = detail && ["failed", "interrupted", "needs_review"].includes(detail.status) && (!detail.review || detail.review.decision === "no_effect") && !detail.retry_turn_id;
  return <section className="execution-browser" aria-label="执行记录" aria-busy={busy || loading}>
    <header className="table-toolbar"><h2>执行记录</h2><label>状态 <select disabled={busy} value={status} onChange={e => setStatus(e.target.value)}><option value="attention">异常执行</option><option value="">全部</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label} · {key}</option>)}</select></label><button disabled={busy || loading} onClick={() => setRefresh(n => n + 1)}>刷新记录</button></header>
    <p>重试使用原输入和当前会话上下文创建新尝试。外部操作结果未知时，先核对目标系统；结束处置不会重发或改写回执。</p>
    <p role="status">{message}</p>{error && <p role="alert" className="inline-error">{error}</p>}
    <div className="execution-columns"><div className="execution-list">
      {rows.map(row => <button className="workbench-session" key={row.turn_id} disabled={busy} aria-current={detail?.turn_id === row.turn_id ? "true" : undefined} onClick={() => { setError(""); void open(row.turn_id); }}><span>{labels[row.status] ?? row.status} · {row.review ? "已核对" : row.retry_turn_id ? "已有重试" : "未处置"}<small>{row.session_key}</small><small className="mono">{row.turn_id}</small></span></button>)}
      {!rows.length && <p>{loading ? "正在读取执行记录…" : "没有符合条件的执行。可切换状态或刷新记录。"}</p>}
      {more && <button disabled={loading || busy} onClick={() => void loadMore()}>加载更多执行</button>}
    </div>{detail && <article className="execution-detail" aria-label="执行详情">
      <h3>{labels[detail.status] ?? detail.status} · {detail.status}</h3><p className="mono">{detail.turn_id}</p>
      {onQueue && <button disabled={busy} onClick={() => onQueue(detail.turn_id)}>查看关联收发队列</button>}
      {detail.error_type && <p>错误类型：{detail.error_type}</p>}
      {detail.retry_of && <button disabled={busy} onClick={() => void open(detail.retry_of!)}>查看原执行</button>}
      {detail.retry_turn_id && <button disabled={busy} onClick={() => void open(detail.retry_turn_id!)}>查看关联重试</button>}
      <details><summary>原始输入</summary><pre>{JSON.stringify(detail.input, null, 2)}</pre></details>
      {detail.result != null && <details><summary>已保存结果</summary><pre>{JSON.stringify(detail.result, null, 2)}</pre></details>}
      <h4>工具与外部操作</h4>{!detail.effects.length && <p>没有已记录的工具或发送操作。</p>}
      {detail.effects.map(effect => <details key={effect.effect_id}><summary>{effect.name ?? effect.kind} · {effect.certainty === "confirmed" ? "返回已记录" : "结果未知"} · {effect.retry_safe ? "可重复" : "需核对副作用"}</summary><pre>{JSON.stringify(effect, null, 2)}</pre></details>)}
      <p>{detail.retry.reason}</p><button disabled={busy || !detail.retry.allowed} onClick={() => void act("execution.retry")}>{busy ? "正在处理…" : "按原输入重试"}</button>
      {busy && <button onClick={() => void rpc("chat.cancel", { session_key: detail.session_key }).catch(e => setError(String(e)))}>停止重试</button>}
      {detail.review && <p>核对结论：{detail.review.decision === "close" ? "结束处置" : "已确认所有待核对操作未产生副作用"}。依据：{detail.review.note}</p>}
      {canReview && <form onSubmit={e => { e.preventDefault(); void act("execution.review"); }}>
        {detail.review?.decision === "no_effect" && <p>若发现新的回执或核对有误，可填写依据并结束处置，撤销尚未使用的重试许可。</p>}
        <label>处置结论<select disabled={busy} value={decision} onChange={e => setDecision(e.target.value)}><option value="close">结束处置，保留记录且不重跑</option>{detail.can_review_no_effect && <option value="no_effect">已核对所有外部操作，均未产生副作用</option>}</select></label>
        <label>核对依据<textarea required maxLength={4000} disabled={busy} value={note} onChange={e => setNote(e.target.value)} placeholder="记录检查的目标、结果或回执。确认未产生副作用后才可允许整轮重试。" /></label>
        <button disabled={busy} type="submit">保存处置结论</button>
      </form>}
      <h4>事件事实</h4>{events.map(event => <details key={event.seq}><summary>#{event.seq} {event.kind} · {event.created_at}</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>)}
      {moreEvents && <button disabled={loading || busy} onClick={() => void loadEvents()}>加载后续事件</button>}
    </article>}</div>
  </section>;
}
