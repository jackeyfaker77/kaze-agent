export type Message = { id?: string; session_key?: string; seq?: number; role: string; content: string; media?: string[]; ts?: string; timestamp?: string; extra?: unknown; tool_chain?: unknown };
export type Session = { session_key: string; title: string; messages: Message[] };
export type Entry = { key: string; metadata?: { title?: string }; updated_at?: string; message_count: number };
export async function rpc<T>(method: string, payload: Record<string, unknown> = {}): Promise<T> {
  const result = await window.miraDesktop.invoke({ method, payload });
  if (result.error) throw new Error(result.error.message);
  return result.payload as T;
}
export const newDraftKey = () => `desktop:${crypto.randomUUID().replaceAll("-", "")}`;
export const entryTitle = (entry: Entry) => entry.metadata?.title || entry.key;
export function dateLabel(value?: string, time = false) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", ...(time ? { hour: "2-digit", minute: "2-digit" } : {}) });
}
