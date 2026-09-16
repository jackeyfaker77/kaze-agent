import { useEffect, useState } from "react";
import { CaretLeft, CaretRight, MagnifyingGlass, X } from "@phosphor-icons/react";
import { dateLabel, entryTitle, rpc, type Entry, type Message } from "./api";
import { Markdown } from "./primitives";
import { ToolChain } from "./ToolChain";
import { KazeAvatar } from "../shared/KazeAvatar";

export function Workbench({ entries }: { entries: Entry[] }) {
  const [selected, setSelected] = useState("");
  const [sessionQuery, setSessionQuery] = useState("");
  const [source, setSource] = useState("");
  const [query, setQuery] = useState("");
  const [role, setRole] = useState("");
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<Message[]>([]);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<Message | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError(""); setDetail(null);
    const timer = setTimeout(() => { void rpc<{ messages: Message[]; total: number }>("messages.list", { session_key: selected, q: query, role, page }).then(result => {
      if (!disposed) { setRows(result.messages); setTotal(result.total); }
    }).catch(e => { if (!disposed) setError(String(e)); }).finally(() => { if (!disposed) setLoading(false); }); }, 150);
    return () => { disposed = true; clearTimeout(timer); };
  }, [selected, query, role, page]);
  const sources = [...new Set(entries.map(e => e.key.split(":")[0]))];
  return <div className="workbench">
    <aside className="workbench-rail">
      <div className="workbench-brand"><KazeAvatar size={36} /><div>Kaze<small>Dashboard</small></div></div>
      <div className="rail-section-label">Sessions <span>{entries.length}</span></div>
      <div className="workbench-filters"><label className="search-field"><MagnifyingGlass /><input aria-label="搜索工作台会话" placeholder="搜索会话" value={sessionQuery} onChange={e => setSessionQuery(e.target.value)} /></label><select aria-label="会话来源" value={source} onChange={e => setSource(e.target.value)}><option value="">全部来源</option>{sources.map(s => <option key={s}>{s}</option>)}</select></div>
      <nav aria-label="工作台会话"><button className="workbench-session" aria-current={!selected ? "page" : undefined} onClick={() => { setSelected(""); setPage(1); }}>全部会话 <span>{entries.length}</span></button>{entries.filter(e => (!source || e.key.split(":")[0] === source) && `${entryTitle(e)} ${e.key}`.toLowerCase().includes(sessionQuery.toLowerCase())).map(entry => <button key={entry.key} className="workbench-session" aria-current={selected === entry.key ? "page" : undefined} onClick={() => { setSelected(entry.key); setPage(1); }}><span className="truncate">{entryTitle(entry)}<small>{entry.key.split(":")[0]} · {dateLabel(entry.updated_at)}</small></span><span>{entry.message_count}</span></button>)}</nav>
    </aside>
    <section className="workbench-body" aria-label="消息浏览器">
      <div className="table-toolbar"><label className="search-field"><MagnifyingGlass /><input aria-label="搜索消息内容" placeholder="搜索消息内容" value={query} onChange={e => { setQuery(e.target.value); setPage(1); }} /></label><select aria-label="消息类型" value={role} onChange={e => { setRole(e.target.value); setPage(1); }}><option value="">全部 role</option>{["user", "assistant", "tool", "system"].map(r => <option key={r}>{r}</option>)}</select></div>
      {error && <p role="alert" className="inline-error">{error}</p>}
      <div className="table-scroll" aria-busy={loading}><table><thead><tr><th>Session Key</th><th>Seq</th><th>Content</th><th>Timestamp ↓</th><th>Role</th></tr></thead><tbody>{rows.map((row, i) => <tr key={row.id ?? i} data-selected={detail?.id === row.id && !!detail}><td><span className="mono truncate" title={row.session_key}>{row.session_key}</span></td><td>#{row.seq ?? i}</td><td><button className="table-content" onClick={() => setDetail(row)}>{row.content || "（无文本内容）"}</button></td><td className="mono">{dateLabel(row.ts ?? row.timestamp, true)}</td><td><span className={`role-badge ${row.role}`}>{row.role}</span></td></tr>)}</tbody></table>{!rows.length && <p className="empty-table">{loading ? "正在读取消息…" : "没有符合条件的消息"}</p>}</div>
      <footer className="table-footer"><span role="status">共 {total} 条</span><div><button className="icon-button" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => setPage(p => p - 1)}><CaretLeft /></button>{page} / {Math.max(1, Math.ceil(total / 50))}<button className="icon-button" aria-label="下一页" disabled={page * 50 >= total || loading} onClick={() => setPage(p => p + 1)}><CaretRight /></button></div></footer>
    </section>
    {detail && <aside className="message-detail" aria-label="消息详情"><header><div><h2>消息详情</h2><small className="mono">{detail.session_key} · #{detail.seq}</small></div><button className="icon-button" aria-label="关闭消息详情" onClick={() => setDetail(null)}><X /></button></header><dl><div><dt>role</dt><dd><span className={`role-badge ${detail.role}`}>{detail.role}</span></dd></div><div><dt>time</dt><dd className="mono">{detail.ts ?? detail.timestamp ?? "—"}</dd></div><div><dt>id</dt><dd className="mono">{detail.id}</dd></div></dl><small>Content</small><Markdown>{detail.content}</Markdown>{detail.media?.map(path => <p key={path}>附件：{path.split(/[\\/]/).at(-1)}</p>)}<ToolChain key={detail.id ?? `${detail.session_key}:${detail.seq}`} value={detail.tool_chain} />{detail.extra != null && <><small>Extra</small><pre>{JSON.stringify(detail.extra, null, 2)}</pre></>}</aside>}
  </div>;
}
