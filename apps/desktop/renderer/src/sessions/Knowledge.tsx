import { useEffect, useState } from "react";
import { ArrowsClockwise, BookOpen, CaretRight, CheckCircle, Copy, HardDrives, Timer } from "@phosphor-icons/react";
import { rpc } from "./api";
import { Markdown } from "./primitives";

type Catalog = { documents: { id: string; name: string; description: string }[]; skills: { name: string; source: string }[]; tools: { name: string; description: string; source_type: string; source_name: string }[]; servers: string };
type Job = { id: string; name?: string; message?: string; prompt?: string; trigger: string; enabled?: boolean; session_key: string; fire_at?: string; cron_expr?: string; interval_seconds?: number };
export function Knowledge() {
  const [tab, setTab] = useState("documents");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [tasks, setTasks] = useState<Job[]>([]);
  const [doc, setDoc] = useState("MEMORY.md");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [docLoading, setDocLoading] = useState(true);
  const [error, setError] = useState("");
  const [synced, setSynced] = useState("");
  const [revision, setRevision] = useState(0);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let disposed = false; setLoading(true); setError("");
    void Promise.all([rpc<Catalog>("runtime.catalog"), rpc<{ tasks: Job[] }>("tasks.list")]).then(([next, jobs]) => {
      if (!disposed) { setCatalog(next); setTasks(jobs.tasks); setSynced(new Date().toLocaleTimeString("zh-CN")); }
    }).catch(e => { if (!disposed) setError(String(e)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [revision]);
  useEffect(() => {
    let disposed = false; setContent(""); setCopied(false); setDocLoading(true);
    void rpc<{ content: string }>("document.get", { id: doc }).then(result => { if (!disposed) setContent(result.content); }).catch(e => { if (!disposed) setError(String(e)); }).finally(() => { if (!disposed) setDocLoading(false); });
    return () => { disposed = true; };
  }, [doc, revision]);
  const mcp = catalog?.tools.filter(t => t.source_type === "mcp") ?? [];
  return <section className="knowledge-page">
    <header className="knowledge-heading"><div><h1>知识与运行</h1><p>当前电脑的运行目录 · 所有会话共享全局记忆</p></div><div className="sync-controls"><span role="status"><CheckCircle />{loading ? "正在同步…" : synced ? `已同步 · ${synced}` : "尚未同步"}</span><button className="pill" disabled={loading} onClick={() => setRevision(n => n + 1)}><ArrowsClockwise />刷新</button></div></header>
    {error && <p role="alert" className="inline-error">{error}</p>}
    <div className="runtime-stats"><div><BookOpen /><strong>{catalog?.documents.length ?? "—"}</strong><span>核心文档</span></div><div><HardDrives /><strong>{catalog ? mcp.length : "—"}</strong><span>MCP 工具 · {catalog?.skills.length ?? "—"} Skills</span></div><div><Timer /><strong>{tasks.filter(t => t.enabled !== false).length}</strong><span>已启用定时任务</span></div></div>
    <nav className="knowledge-tabs" aria-label="知识与运行分类">{[{ id: "documents", label: "文档", icon: BookOpen }, { id: "tools", label: "MCP 与 Skills", icon: HardDrives }, { id: "tasks", label: "定时任务", icon: Timer }].map(t => <button key={t.id} className="pill" aria-current={tab === t.id ? "page" : undefined} onClick={() => setTab(t.id)}><t.icon />{t.label}</button>)}</nav>
    {tab === "documents" ? <div className="document-browser"><aside><header><h2>文档</h2><p>全局共享的固定运行目录</p></header><nav aria-label="全局文档">{catalog?.documents.map(d => <button key={d.id} aria-current={doc === d.id ? "page" : undefined} onClick={() => setDoc(d.id)}><BookOpen /><span className="truncate">{d.name}<small className="truncate">{d.description} · memory/{d.id}</small></span><CaretRight /></button>)}</nav></aside><section className="document-content"><header><div><h2>{catalog?.documents.find(d => d.id === doc)?.name ?? "长期记忆"}</h2><p>只读 · memory/{doc}</p></div><button className="pill" onClick={() => void navigator.clipboard.writeText(`memory/${doc}`).then(() => setCopied(true)).catch(e => setError(String(e)))}><Copy />{copied ? "已复制" : "复制标识"}</button></header><div className="document-text">{content ? <Markdown>{content}</Markdown> : <p className="muted">{docLoading ? "正在读取文档…" : "此文档暂无内容"}</p>}</div></section></div>
    : tab === "tools" ? <div className="runtime-list"><h2>MCP 服务</h2><Markdown>{catalog?.servers ?? "正在读取…"}</Markdown>{mcp.map(t => <article key={t.name}><HardDrives /><div><h3>{t.name}</h3><p>{t.description}</p><small>{t.source_name}</small></div></article>)}<h2>Skills <small>{catalog?.skills.length ?? 0}</small></h2>{catalog?.skills.map(s => <article key={s.name}><BookOpen /><div><h3>{s.name}</h3><small>{s.source === "workspace" ? "工作区技能" : "内置技能"}</small></div></article>)}{catalog?.skills.length === 0 && <p className="muted">暂无可用技能</p>}</div>
    : <div className="runtime-list"><h2>定时任务</h2>{tasks.length ? tasks.map(t => <article key={t.id}><Timer /><div><h3>{t.name || t.message || t.prompt || t.id}</h3><p>{t.trigger} · {t.fire_at || t.cron_expr || (t.interval_seconds ? `每 ${t.interval_seconds} 秒` : "")}</p><small className="mono">{t.session_key}</small></div><span className="status-badge">{t.enabled === false ? "已暂停" : "已启用"}</span></article>) : <div className="quiet-empty"><Timer size={30} /><h3>暂无定时任务</h3><p>在对话中告诉 Agent 需要提醒或定期执行的事项。</p></div>}</div>}
  </section>;
}
