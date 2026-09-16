const callLabels: Record<string, string> = {
  call_id: "调用 ID",
  name: "工具名称",
  status: "执行状态",
  arguments: "调用参数",
  final_arguments: "实际执行参数",
  result: "返回结果（保存的记录）",
  pre_hook_trace: "执行前处理记录",
  post_hook_trace: "执行后处理记录",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function TextValue({ value }: { value: unknown }) {
  const text = value == null ? "未记录" : typeof value === "string" ? value || "（空文本）" : String(value);
  if (text.length > 600) {
    return <details className="record-long-text">
      <summary>展开完整内容 · {text.length} 字符</summary>
      <pre>{text}</pre>
    </details>;
  }
  return <pre>{text}</pre>;
}

/** 按原始字段展示嵌套数据，不把工具返回的文本作为 HTML 执行。 */
function RecordValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || typeof value !== "object") return <TextValue value={value} />;
  if (depth >= 8) return <TextValue value={JSON.stringify(value, null, 2)} />;
  const entries = Object.entries(value);
  if (!entries.length) return <pre>{Array.isArray(value) ? "[]" : "{}"}</pre>;
  return <details className="record-tree" open={depth === 0}>
    <summary>{Array.isArray(value) ? "列表" : "字段"} · {entries.length} 项</summary>
    <dl>{entries.map(([key, item]) => <div key={key}>
      <dt>{Array.isArray(value) ? `第 ${Number(key) + 1} 项` : key}</dt>
      <dd><RecordValue value={item} depth={depth + 1} /></dd>
    </div>)}</dl>
  </details>;
}

function ToolCall({ value, index }: { value: unknown; index: number }) {
  if (!isRecord(value)) return <RecordValue value={value} />;
  return <details className="tool-call">
    <summary>
      <span className="mono">{typeof value.name === "string" && value.name ? value.name : `调用 ${index + 1}`}</span>
      {typeof value.status === "string" && <span className="tool-call-status">{value.status}</span>}
    </summary>
    <dl>{Object.entries(value).map(([key, item]) => <div key={key}>
      <dt>{callLabels[key] ?? key}</dt>
      <dd><RecordValue value={item} /></dd>
    </div>)}</dl>
  </details>;
}

/** 保留工具链中的轮次、调用顺序及额外字段，只读取已保存的记录。 */
export function ToolChain({ value }: { value: unknown }) {
  const empty = value == null || (Array.isArray(value) && value.length === 0);
  return <section className="tool-chain" aria-label="工具执行记录">
    <h3>Tool Chain <span>工具执行记录</span></h3>
    {empty ? <p className="muted">这条消息没有保存工具执行记录。</p> : <>
      <p className="tool-chain-note">按执行轮次查看调用参数和结果。结果可能仅保存了预览。</p>
      {Array.isArray(value) ? value.map((group, index) => {
        if (!isRecord(group)) return <RecordValue key={index} value={group} />;
        const calls = Array.isArray(group.calls) ? group.calls : null;
        return <details className="tool-round" key={index} open={index === 0}>
          <summary>第 {index + 1} 轮{calls && <span> · {calls.length} 次调用</span>}</summary>
          {calls && <div className="tool-calls">{calls.map((call, callIndex) => <ToolCall key={callIndex} value={call} index={callIndex} />)}</div>}
          <dl>{Object.entries(group).filter(([key]) => key !== "calls" || !calls).map(([key, item]) => <div key={key}>
            <dt>{key === "text" ? "轮次文本" : key === "reasoning_content" ? "模型返回的推理文本" : key}</dt>
            <dd><RecordValue value={item} /></dd>
          </div>)}</dl>
        </details>;
      }) : <RecordValue value={value} />}
    </>}
  </section>;
}
