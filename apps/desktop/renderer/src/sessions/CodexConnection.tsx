import { useEffect, useRef, useState } from "react";
import { ArrowSquareOut, ArrowClockwise, Trash } from "@phosphor-icons/react";
import type { ModelRegistrationFormData } from "../../../src/bridge/shared";
import { rpc } from "./api";

type CodexModel = { id: string; efforts: string[] };
type Login = { login_id: string; status: "waiting" | "connected" | "failed" | "cancelled"; user_code: string; verification_uri: string; interval: number; error?: string };

export function CodexConnection({ registration, saving, busy, removable, onSave, onRemove, close }: {
  registration: ModelRegistrationFormData;
  saving: boolean;
  busy: boolean;
  removable: boolean;
  onSave: (registration: ModelRegistrationFormData) => Promise<void>;
  onRemove: () => Promise<void>;
  close: () => void;
}) {
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [models, setModels] = useState<CodexModel[]>([]);
  const [model, setModel] = useState(registration.model);
  const [effort, setEffort] = useState(registration.effort);
  const [login, setLogin] = useState<Login | null>(null);
  const [error, setError] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const mounted = useRef(true);
  const activeLogin = useRef<Login | null>(null);
  const connectionId = registration.id;
  const selected = models.find(item => item.id === model);

  useEffect(() => {
    if (selected && effort !== "none" && !selected.efforts.includes(effort)) setEffort("none");
  }, [selected, effort]);

  async function loadModels() {
    setLoading(true); setError("");
    try {
      const result = await rpc<{ models: CodexModel[] }>("codex.models", { connection_id: connectionId });
      if (!mounted.current) return;
      setModels(result.models);
      setModel(current => result.models.some(item => item.id === current) ? current : result.models[0]?.id ?? "");
      if (!result.models.length) setError("此账号暂时没有可用模型，请确认订阅权限后重试。");
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setLoading(false); }
  }

  useEffect(() => {
    let disposed = false;
    mounted.current = true;
    void rpc<{ configured: boolean }>("codex.status", { connection_id: connectionId }).then(async result => {
      if (disposed) return;
      setConfigured(result.configured);
      if (result.configured) await loadModels();
      else setLoading(false);
    }).catch(e => { if (!disposed) { setError(String(e)); setLoading(false); } });
    return () => {
      disposed = true; mounted.current = false;
      const current = activeLogin.current;
      if (current?.status === "waiting") void rpc("codex.login.cancel", { connection_id: connectionId, login_id: current.login_id }).catch(() => undefined);
    };
    // 每个连接由父组件的 key 单独挂载。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);

  useEffect(() => {
    if (login?.status !== "waiting") return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await rpc<Login>("codex.login.status", { connection_id: connectionId, login_id: login.login_id });
        if (disposed) return;
        activeLogin.current = next;
        setLogin(next);
        if (next.status === "connected") { setConfigured(true); setEffort("none"); await loadModels(); }
        else if (next.status === "waiting") timer = setTimeout(() => void poll(), Math.max(3, next.interval) * 1000);
        else if (next.error) setError(next.error);
      } catch (e) { if (!disposed) { setError(String(e)); timer = setTimeout(() => void poll(), 5000); } }
    };
    timer = setTimeout(() => void poll(), Math.max(3, login.interval) * 1000);
    return () => { disposed = true; clearTimeout(timer); };
    // 状态轮询按 login_id 启动，避免每次返回 waiting 都重建轮询。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId, login?.login_id, login?.status]);

  async function startLogin() {
    setStarting(true); setError("");
    try {
      const next = await rpc<Login>("codex.login.start", { connection_id: connectionId });
      if (!mounted.current) {
        await rpc("codex.login.cancel", { connection_id: connectionId, login_id: next.login_id });
        return;
      }
      activeLogin.current = next; setLogin(next);
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setStarting(false); }
  }

  async function persist() {
    setError("");
    try { await onSave({ ...registration, provider: "codex", apiKey: "", baseUrl: "", model, effort }); }
    catch (e) { setError(String(e)); }
  }

  return <form className="connection-form codex-connection" onSubmit={e => { e.preventDefault(); void persist(); }}>
    <p>使用 ChatGPT 账号授权，无需填写 API Key。</p>
    <div className="codex-auth-status" role="status">{loading ? "正在读取连接…" : configured ? "已保存 ChatGPT 登录授权" : "尚未登录 ChatGPT"}</div>
    {login?.status === "waiting" ? <div className="codex-device-login">
      <p>在 OpenAI 授权页面输入以下代码：</p><code>{login.user_code}</code>
      <button type="button" onClick={() => void window.miraDesktop.openExternal("https://auth.openai.com/codex/device").catch(e => setError(String(e)))}><ArrowSquareOut />打开授权页面</button>
      <small>完成授权后自动更新。若账号提示不允许设备码登录，请在 ChatGPT 安全设置中开启。</small>
    </div> : <button type="button" className="codex-login-button" disabled={starting || loading || saving || busy} onClick={() => void startLogin()}>{starting ? "正在获取授权码…" : configured ? "重新登录 ChatGPT" : "登录 ChatGPT"}</button>}
    {configured && <>
      <label>模型<select required value={model} disabled={loading || saving} onChange={e => { setModel(e.target.value); setEffort("none"); }}>
        {!selected && <option value="">选择账号可用模型</option>}{models.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}
      </select></label>
      <button type="button" disabled={loading || saving} onClick={() => void loadModels()}><ArrowClockwise />刷新可用模型</button>
      <label>推理强度<select value={effort} disabled={saving} onChange={e => setEffort(e.target.value as ModelRegistrationFormData["effort"])}>
        <option value="none">模型默认</option>{["low", "high", "max"].filter(value => selected?.efforts.includes(value)).map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
    </>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {confirmRemove && <p>移除此连接？已有会话记录会保留。</p>}
    <footer>{removable && <button type="button" className="danger" disabled={saving} onClick={() => confirmRemove ? void onRemove().catch(e => setError(String(e))) : setConfirmRemove(true)}><Trash />{confirmRemove ? "确认移除" : "移除连接"}</button>}
      <button type="button" disabled={saving} onClick={close}>取消</button>
      <button type="submit" className="primary" disabled={saving || busy || loading || starting || login?.status === "waiting" || !configured || !selected}>{saving ? "正在保存…" : "保存连接"}</button>
    </footer>
  </form>;
}
