import { useId, useState, type ReactNode } from "react";
import { Brain, CaretRight, CheckCircle, CircleNotch, Terminal, WarningCircle } from "@phosphor-icons/react";
import { Markdown } from "./primitives";
import { record, splitMessageParts, type MessagePart } from "./conversationDisplay";

function Disclosure({ label, children, running = false, thinking = false, className = "" }: {
  label: ReactNode; children: ReactNode; running?: boolean; thinking?: boolean; className?: string;
}) {
  const id = useId();
  const [choice, setChoice] = useState<boolean | null>(null);
  const [visited, setVisited] = useState(false);
  const open = choice ?? running;
  if (open && !visited) setVisited(true);
  const base = thinking ? "chat-thinking-content" : "chat-tool-call-content";
  return <div className={`message-disclosure ${className}`}>
    <button type="button" className="message-disclosure-toggle" aria-expanded={open} aria-controls={id}
      onClick={() => setChoice(!open)}>
      <CaretRight size={14} className={open ? "message-chevron expanded" : "message-chevron"} aria-hidden="true" />
      {label}
    </button>
    <div id={id} className={`${base}${open ? ` ${base}-expanded` : ""}`} aria-hidden={!open} inert={!open}>
      <div><div className="message-disclosure-body">{(open || visited) && children}</div></div>
    </div>
  </div>;
}

const statuses: Record<string, string> = {
  running: "运行中", success: "已完成", error: "失败", denied: "已拒绝", blocked: "已阻止",
  cancelled: "已取消", canceled: "已取消", interrupted: "已中断", unknown: "结果待核对", needs_review: "待核对",
};
function stringify(value: unknown): string {
  if (value === undefined) return "未记录";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
function ToolValue({ label, value }: { label: string; value: unknown }) {
  const display = stringify(value);
  return <div className="message-tool-field"><span>{label}</span>{display.length > 1200
    ? <Disclosure label={<span>展开内容 · {display.length} 字符</span>}><pre>{display}</pre></Disclosure>
    : <pre>{display || "（空内容）"}</pre>}</div>;
}
function ToolBlock({ part }: { part: Extract<MessagePart, { kind: "tool" }> }) {
  const args = record(part.args);
  const summary = [args.command, args.path, args.query, args.task_id].find(value => typeof value === "string") as string | undefined;
  const running = part.status === "running";
  const Icon = running ? CircleNotch : part.status === "success" ? CheckCircle : WarningCircle;
  return <Disclosure className="message-tool" label={<>
    <Terminal size={16} aria-hidden="true" /><span className="message-tool-name">{part.name}</span>
    {summary && <span className="message-tool-summary">{summary.replace(/\s+/g, " ").slice(0, 100)}</span>}
    <span className="message-tool-status" data-status={part.status}><Icon size={14} className={running ? "chat-tool-running" : ""} aria-hidden="true" />{statuses[part.status] ?? part.status}</span>
  </>}>
    <ToolValue label="调用参数" value={part.args} />
    {part.finalArgs !== undefined && <ToolValue label="实际执行参数" value={part.finalArgs} />}
    {part.result !== undefined && <ToolValue label="返回结果（保存的预览）" value={part.result} />}
  </Disclosure>;
}

/** One renderer for committed round facts and temporary stream parts. No execution side effects. */
export function MessageParts({ parts, running = false, awaitingHistory = false, interrupted = false, durationMs }: {
  parts: MessagePart[]; running?: boolean; awaitingHistory?: boolean; interrupted?: boolean; durationMs?: number;
}) {
  const { process, answer } = splitMessageParts(parts);
  const hasProcess = process.length > 0;
  const label = running ? "正在处理" : awaitingHistory ? "等待历史同步" : interrupted ? "已中断 · 执行过程" : "执行过程";
  return <div className="message-parts">
    {hasProcess && <Disclosure className="message-process" running={running} label={<>
      {running && <CircleNotch size={15} className="chat-tool-running" aria-hidden="true" />}
      <span>{label}</span>
      {durationMs !== undefined && <span className="message-thinking-duration">思考用时 {(durationMs / 1000).toFixed(1)} 秒</span>}
    </>}>
      {process.map(part => part.kind === "thinking" ? <Disclosure key={part.id} thinking className="message-thinking"
        label={<><Brain size={16} aria-hidden="true" /><span className={!part.done && running ? "chat-thinking-label-streaming" : ""}>思考过程</span></>}>
        <Markdown>{part.text}</Markdown>
      </Disclosure> : part.kind === "tool" ? <ToolBlock key={part.id} part={part} /> : <div key={part.id} className="message-commentary"><Markdown>{part.text}</Markdown></div>)}
    </Disclosure>}
    {answer && <div className="message-answer"><Markdown>{answer.text}</Markdown>{running && <span className="chat-stream-cursor" aria-hidden="true" />}</div>}
    {awaitingHistory && <p className="message-awaiting">回复已暂停，等待已保存的历史接管；未确认的工具结果请在工作台核对。</p>}
    {interrupted && !hasProcess && <p className="message-awaiting">回复已中断</p>}
    {running && !parts.length && <p className="message-waiting"><CircleNotch size={15} className="chat-tool-running" aria-hidden="true" />正在等待回复…</p>}
  </div>;
}
