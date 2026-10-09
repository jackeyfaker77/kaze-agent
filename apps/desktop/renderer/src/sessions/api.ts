export type Message = { id?: string; session_key?: string; seq?: number; role: string; content: string; media?: string[]; ts?: string; timestamp?: string; extra?: unknown; tool_chain?: unknown };
export type Session = { session_key: string; title: string; messages: Message[] };
export type Entry = { key: string; metadata?: { title?: string }; updated_at?: string; message_count: number };
export async function rpc<T>(method: string, payload: Record<string, unknown> = {}): Promise<T> {
  // [消息链路 2/6] miraDesktop.invoke 经 preload 进入 Electron 主进程的
  // apps/desktop/src/bridge/ipc.ts → ipcMain.handle("desktop:invoke")。
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
