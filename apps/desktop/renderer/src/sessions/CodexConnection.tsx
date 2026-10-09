import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowSquareOut, ArrowClockwise, Trash } from "@phosphor-icons/react";
import type { ModelRegistrationFormData } from "../../../src/bridge/shared";
import { rpc } from "./api";
import { openCodexLoginPage, type CodexLogin } from "./codexBrowserLogin";

type CodexModel = { id: string; efforts: string[] };

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
  const [login, setLogin] = useState<CodexLogin | null>(null);
  const [error, setError] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const mounted = useRef(true);
  const activeLogin = useRef<CodexLogin | null>(null);
  const connectionId = registration.id;
  const selected = models.find(item => item.id === model);

  useEffect(() => {
    if (selected && effort !== "none" && !selected.efforts.includes(effort)) setEffort("none");
  }, [selected, effort]);

  const loadModels = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const result = await rpc<{ models: CodexModel[] }>("codex.models", { connection_id: connectionId });
      if (!mounted.current) return;
      setModels(result.models);
      setModel(current => result.models.some(item => item.id === current) ? current : result.models[0]?.id ?? "");
      if (!result.models.length) setError("此账号暂时没有可用模型，请确认订阅权限后重试。");
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setLoading(false); }
  }, [connectionId]);

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
  }, [connectionId, loadModels]);

  const loginId = login?.login_id;
  const loginStatus = login?.status;
  const loginInterval = login?.interval ?? 1;
  useEffect(() => {
    if (loginStatus !== "waiting" || !loginId) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await rpc<CodexLogin>("codex.login.status", { connection_id: connectionId, login_id: loginId });
        if (disposed) return;
        activeLogin.current = next;
        setLogin(next);
        if (next.status === "connected") { setConfigured(true); setEffort("none"); await loadModels(); }
        else if (next.status === "waiting") timer = setTimeout(() => void poll(), Math.max(1, next.interval) * 1000);
        else if (next.error) setError(next.error);
      } catch (e) { if (!disposed) { setError(String(e)); timer = setTimeout(() => void poll(), 5000); } }
    };
    timer = setTimeout(() => void poll(), Math.max(1, loginInterval) * 1000);
    return () => { disposed = true; clearTimeout(timer); };
    // 状态轮询按 login_id 启动，避免每次返回 waiting 都重建轮询。
  }, [connectionId, loginId, loginStatus, loginInterval, loadModels]);

  async function startLogin() {
    setStarting(true); setError("");
    let next: CodexLogin | null = null;
    try {
      next = await rpc<CodexLogin>("codex.login.start", { connection_id: connectionId });
      if (!mounted.current) {
        await rpc("codex.login.cancel", { connection_id: connectionId, login_id: next.login_id });
        return;
      }
      activeLogin.current = next; setLogin(next);
      await openCodexLoginPage(next.authorization_url, url => window.miraDesktop.openExternal(url));
    } catch (e) {
      if (next) {
        await rpc("codex.login.cancel", { connection_id: connectionId, login_id: next.login_id }).catch(() => undefined);
        if (mounted.current) { activeLogin.current = null; setLogin(null); }
      }
      if (mounted.current) setError(String(e));
    }
    finally { if (mounted.current) setStarting(false); }
  }

  async function cancelLogin() {
    const current = activeLogin.current;
    if (!current || current.status !== "waiting") return;
    setStarting(true); setError("");
    try {
      const next = await rpc<CodexLogin>("codex.login.cancel", { connection_id: connectionId, login_id: current.login_id });
      if (!mounted.current) return;
      activeLogin.current = next; setLogin(next);
      if (next.status === "connected") { setConfigured(true); setEffort("none"); await loadModels(); }
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setStarting(false); }
  }

  async function persist() {
    setError("");
    try { await onSave({ ...registration, provider: "codex", apiKey: "", baseUrl: "", model, effort }); }
    catch (e) { setError(String(e)); }
  }

  return <form className="connection-form codex-connection" onSubmit={e => { e.preventDefault(); void persist(); }}>
    <p>使用 ChatGPT 账号在浏览器中登录，授权后自动连接。</p>
    <div className="codex-auth-status" role="status">{loading ? "正在读取连接…" : configured ? "已保存 ChatGPT 登录授权" : "尚未登录 ChatGPT"}</div>
    {login?.status === "waiting" ? <div className="codex-browser-login" role="status">
      <p>请在浏览器中完成 ChatGPT 登录和授权。</p>
      <button type="button" disabled={starting} onClick={() => void openCodexLoginPage(login.authorization_url, url => window.miraDesktop.openExternal(url)).catch(e => setError(String(e)))}><ArrowSquareOut />重新打开登录页面</button>
      <button type="button" disabled={starting} onClick={() => void cancelLogin()}>取消登录</button>
      <small>完成授权后，Kaze 会自动更新连接状态。</small>
    </div> : <button type="button" className="codex-login-button" disabled={starting || loading || saving || busy} onClick={() => void startLogin()}>{starting ? "正在打开浏览器…" : configured ? "重新登录 ChatGPT" : "登录 ChatGPT"}</button>}
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
