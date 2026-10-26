import { useEffect, useRef, useState } from "react";
import { rpc } from "./api";

type Job = { id: string; kind: string; state: string; revision: number; attempts: number;
  review_closed: number; payload: unknown; error: string | null; result: unknown };
type Detail = Job & { can_close: boolean; related_jobs: string[];
  retry: { allowed: boolean; unknown_parts: string[]; reason: string };
  parts: { part: string; state: string; payload: unknown; result: unknown }[];
  executions: { turn_id: string; session_key: string; status: string }[];
  reviews: { seq: number; created_at: string; request: { decision: string; note: string }; evidence: unknown }[] };
const labels: Record<string, string> = { pending: "等待处理", running: "处理中", done: "已完成", failed: "失败", needs_review: "待核对" };

export function TransportJobs({ sessionKey, turnId = "", onExecution }: {
  sessionKey: string; turnId?: string; onExecution: (id: string) => void;
}) {
  const [status, setStatus] = useState(turnId ? "" : "attention");
  const [rows, setRows] = useState<Job[]>([]);
  const [cursor, setCursor] = useState(0);
  const [more, setMore] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [note, setNote] = useState("");
  const [absent, setAbsent] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const requests = useRef({ generation: 0, selection: 0 });
  const lock = useRef(false);
  useEffect(() => {
    const counters = requests.current;
    const id = ++requests.current.generation;
    ++requests.current.selection;
    setDetail(null); setRows([]); setLoading(true); setError(""); setMore(false);
    void rpc<{ jobs: Job[]; next_cursor: number }>("transport.list", { session_key: sessionKey, turn_id: turnId, status, limit: 25 })
      .then(data => { if (requests.current.generation === id) { setRows(data.jobs); setCursor(data.next_cursor); setMore(data.jobs.length === 25); } })
      .catch(e => { if (requests.current.generation === id) setError(String(e)); })
      .finally(() => { if (requests.current.generation === id) setLoading(false); });
    return () => { ++counters.generation; ++counters.selection; };
  }, [sessionKey, turnId, status, refresh]);
  async function open(id: string) {
    const current = ++requests.current.selection;
    setDetail(null); setNote(""); setAbsent([]); setError("");
    try {
      const value = await rpc<Detail>("transport.get", { job_id: id });
      if (requests.current.selection === current) {
        setDetail(value); setRows(old => old.map(row => row.id === id ? value : row));
      }
    } catch (e) { if (requests.current.selection === current) setError(String(e)); }
  }
  async function moreJobs() {
    const current = requests.current.generation;
    setLoading(true);
    try {
      const data = await rpc<{ jobs: Job[]; next_cursor: number }>("transport.list", { session_key: sessionKey, turn_id: turnId, status, before: cursor, limit: 25 });
      if (requests.current.generation === current) { setRows(old => [...old, ...data.jobs]); setCursor(data.next_cursor); setMore(data.jobs.length === 25); }
    } catch (e) { if (requests.current.generation === current) setError(String(e)); }
    finally { if (requests.current.generation === current) setLoading(false); }
  }
  async function act(decision: "close" | "retry") {
    if (!detail || lock.current) return;
    const current = requests.current.generation;
    lock.current = true; setBusy(true); setError(""); setMessage("");
    try {
      await rpc("transport.review", { job_id: detail.id, expected_revision: detail.revision,
        action_id: crypto.randomUUID(), decision, note, absent_parts: decision === "retry" ? absent : [] });
      if (requests.current.generation === current) setMessage(decision === "close" ? "已结束此队列任务的处置，原状态和回执保留。" : "已重新排队，请刷新查看实际结果；这不代表已发送成功。");
    } catch (e) { if (requests.current.generation === current) setError(`${String(e)}。请刷新记录确认处置是否已保存。`); }
    finally {
      lock.current = false; setBusy(false);
      if (requests.current.generation === current) {
        // Refresh even after a lost RPC response. Never repeat the mutation automatically.
        const savedError = await rpc<Detail>("transport.get", { job_id: detail.id }).catch(() => null);
        if (savedError && requests.current.generation === current) {
          setDetail(savedError); setRows(old => old.map(row => row.id === savedError.id ? savedError : row)); setAbsent([]); setNote("");
        }
      }
    }
  }
  return <section className="execution-browser" aria-label="收发队列" aria-busy={busy || loading}>
    <header className="table-toolbar"><h2>收发队列</h2><label>状态 <select disabled={busy} value={status} onChange={e => setStatus(e.target.value)}><option value="attention">待处置</option><option value="">全部</option><option value="closed">已结束处置</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><button disabled={busy || loading} onClick={() => setRefresh(n => n + 1)}>刷新队列</button></header>
    <p>执行状态与投递状态分别保存。结束处置不会改成发送成功；重试只继续原队列任务，不会重跑已接纳的失败执行。</p>
    {turnId && <p>正在查看所选执行关联的队列任务。</p>}
    <p role="status">{message}</p>{error && <p className="inline-error" role="alert">{error}</p>}
    <div className="execution-columns"><div className="execution-list">
      {rows.map(row => <button key={row.id} disabled={busy} className="workbench-session" aria-current={detail?.id === row.id ? "true" : undefined} onClick={() => void open(row.id)}><span>{labels[row.state] ?? row.state}{row.review_closed ? " · 已结束处置" : ""}<small>{row.kind}</small><small className="mono">{row.id}</small></span></button>)}
      {!rows.length && <p>{loading ? "正在读取队列…" : "没有符合条件的队列任务，可切换为全部或刷新。"}</p>}
      {more && <button disabled={busy || loading} onClick={() => void moreJobs()}>加载更多队列任务</button>}
    </div>{detail && <article className="execution-detail" aria-label="队列详情">
      <h3>{labels[detail.state] ?? detail.state}{detail.review_closed ? " · 已结束处置" : ""}</h3><p className="mono">{detail.id}</p>
      <p>处理次数：{detail.attempts}。{detail.error && `错误：${detail.error}`}</p>
      <h4>关联执行</h4>{!detail.executions.length && <p>未找到可证明关联的执行记录。历史任务不会按文本相似度猜测关联。</p>}
      {detail.executions.map(record => <button disabled={busy} key={record.turn_id} onClick={() => onExecution(record.turn_id)}>查看执行 {record.turn_id} · {record.status}</button>)}
      {detail.related_jobs.filter(id => id !== detail.id).map(id => <button disabled={busy} key={id} onClick={() => void open(id)}>查看关联队列 {id}</button>)}
      <details><summary>原始内容与目标</summary><pre>{JSON.stringify(detail.payload, null, 2)}</pre></details>
      <details><summary>已保存结果</summary><pre>{JSON.stringify(detail.result, null, 2)}</pre></details>
      <h4>发送分段与回执</h4>{!detail.parts.length && <p>没有已记录的物理发送分段。</p>}
      {detail.parts.map(part => <details key={part.part}><summary>{part.part} · {part.state === "done" ? "已有成功回执" : part.state === "sending" ? "结果未知" : "等待发送"}</summary><pre>{JSON.stringify(part, null, 2)}</pre></details>)}
      <p>{detail.retry.reason}</p>
      {(detail.can_close || detail.retry.allowed) && <form onSubmit={e => e.preventDefault()}>
        {!!detail.retry.unknown_parts.length && <p>先检查目标聊天中的每个分段。无法确认未送达时，保留待核对或结束处置，不要重试。</p>}
        {detail.retry.unknown_parts.map(part => <label key={part}><input type="checkbox" disabled={busy || !detail.retry.allowed} checked={absent.includes(part)} onChange={e => setAbsent(old => e.target.checked ? [...old, part] : old.filter(p => p !== part))} />已核实分段 {part} 未送达</label>)}
        <label>核对依据<textarea maxLength={4000} disabled={busy} value={note} onChange={e => setNote(e.target.value)} placeholder="记录核对的账户、目标聊天、分段和结果。" /></label>
        <button disabled={busy || !detail.can_close || !note.trim()} onClick={() => void act("close")}>结束队列处置</button>
        <button disabled={busy || !detail.retry.allowed || !note.trim() || absent.length !== detail.retry.unknown_parts.length} onClick={() => void act("retry")}>确认核对并重新排队</button>
      </form>}
      <h4>处置记录</h4>{detail.reviews.map(review => <details key={review.seq}><summary>{review.request.decision === "close" ? "结束处置" : "核对后重新排队"} · {review.created_at}</summary><p>{review.request.note}</p><pre>{JSON.stringify(review.evidence, null, 2)}</pre></details>)}
    </article>}</div>
  </section>;
}
