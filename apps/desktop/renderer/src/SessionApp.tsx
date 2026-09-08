import { useEffect, useRef, useState } from "react";
import { BookOpen, CaretDown, ChatCircleDots, Gauge, GearSix, MagnifyingGlass, Minus, MoonStars, PaperPlaneRight, PawPrint, PencilSimple, Plus, SidebarSimple, SlidersHorizontal, Square, Sun, Trash, X } from "@phosphor-icons/react";
import { SettingsPage } from "./settings/SettingsPage";
import { settingsSections, type SettingsSectionId } from "./settings/SettingsSidebar";
import type { SettingsFormData } from "../../src/bridge/shared";
import { dateLabel, entryTitle, rpc } from "./sessions/api";
import { useConversation } from "./sessions/useConversation";
import { Markdown, Modal } from "./sessions/primitives";
import { Workbench } from "./sessions/Workbench";
import { Knowledge } from "./sessions/Knowledge";
import { Models } from "./sessions/Models";
import "./session.css";

type View = "chat" | "workbench" | "knowledge" | "models" | "settings";
type PetState = { packages: { id: string; display_name: string }[]; selected_package_id: string };
const pages = [{ id: "chat", label: "对话", icon: ChatCircleDots }, { id: "workbench", label: "工作台", icon: Gauge }, { id: "knowledge", label: "知识与运行", icon: BookOpen }, { id: "models", label: "模型", icon: SlidersHorizontal }] as const;

export function SessionApp() {
  const chat = useConversation();
  const [view, setView] = useState<View>("chat");
  const [theme, setTheme] = useState(() => localStorage.getItem("hasaki-theme") === "dark" ? "dark" : "paper");
  const [search, setSearch] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [section, setSection] = useState<SettingsSectionId>("voice");
  const [settings, setSettings] = useState<SettingsFormData | null>(null);
  const [saving, setSaving] = useState(false);
  const saveLock = useRef(false);
  const [modal, setModal] = useState<"rename" | "delete" | "pet" | null>(null);
  const [name, setName] = useState("");
  const [modalError, setModalError] = useState("");
  const [modalBusy, setModalBusy] = useState(false);
  const [pets, setPets] = useState<PetState>({ packages: [], selected_package_id: "" });
  const input = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { localStorage.setItem("hasaki-theme", theme); }, [theme]);
  useEffect(() => { void window.miraDesktop.readSettings().then(s => setSettings(s.formData)).catch(chat.fail); }, [view]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [chat.session, chat.delta, view]);
  useEffect(() => { if (input.current) { input.current.style.height = "auto"; input.current.style.height = `${Math.min(input.current.scrollHeight, 140)}px`; } }, [chat.draft, view]);
  async function saveSettings(next: SettingsFormData) {
    if (saveLock.current || chat.busy) throw new Error("请等待当前操作完成");
    saveLock.current = true; setSaving(true);
    try {
      // Read again to preserve edits made in the separate advanced settings surface.
      const latest = await window.miraDesktop.readSettings();
      const value = { ...latest.formData, models: next.models };
      const result = await window.miraDesktop.saveSettings(value);
      if (!result.ok || !result.health.ok) throw new Error(result.health.message || "服务连接失败");
      setSettings(value); await chat.selectCurrent();
    } finally { saveLock.current = false; setSaving(false); }
  }
  async function showPet() {
    setModalError(""); setModal("pet"); setModalBusy(true);
    try { setPets(await rpc<PetState>("pet.get")); } catch (e) { setModalError(String(e)); }
    finally { setModalBusy(false); }
  }
  async function modalAction(action: () => Promise<unknown>, close = false) {
    if (modalBusy) return;
    setModalBusy(true); setModalError("");
    try { await action(); if (close) setModal(null); } catch (e) { setModalError(String(e)); }
    finally { setModalBusy(false); }
  }
  const blocked = chat.busy || chat.loading || saving;
  const registrations = settings?.models.registrations ?? [];
  const visibleEntries = chat.entries.filter(e => entryTitle(e).toLowerCase().includes(search.toLowerCase()));
  function navigate(next: View) { setView(next); setRailOpen(false); }
  return <div className="session-app" data-theme={theme}>
    <header className="product-bar">
      <button className="brand" onClick={() => navigate("chat")} aria-label="Hasaki 首页"><PawPrint weight="fill" size={23} /><span>Hasaki</span></button>
      <nav aria-label="主导航">{pages.map(page => <button key={page.id} aria-current={view === page.id ? "page" : undefined} onClick={() => navigate(page.id)}><page.icon size={18} /><span>{page.label}</span></button>)}</nav>
      <div className="titlebar-space" />
      <button className="icon-button" title="桌宠" aria-label="桌宠" onClick={() => void showPet()}><PawPrint /></button>
      <button className="icon-button" title="设置" aria-label="设置" disabled={blocked} onClick={() => navigate("settings")} aria-current={view === "settings" ? "page" : undefined}><GearSix /></button>
      <button className="icon-button" title={theme === "paper" ? "切换墨纸主题" : "切换纸感主题"} aria-label={theme === "paper" ? "切换墨纸主题" : "切换纸感主题"} onClick={() => setTheme(theme === "paper" ? "dark" : "paper")}>{theme === "paper" ? <MoonStars /> : <Sun />}</button>
      <div className="window-actions"><button className="icon-button" aria-label="最小化" onClick={() => void window.miraDesktop.windowControl("minimize")}><Minus /></button><button className="icon-button" aria-label="最大化或还原" onClick={() => void window.miraDesktop.windowControl("toggleMaximize")}><Square size={13} /></button><button className="icon-button close-window" aria-label="关闭窗口" onClick={() => void window.miraDesktop.windowControl("close")}><X /></button></div>
    </header>
    {chat.error && <div className="app-error" role="alert"><span>{chat.error}</span><button className="icon-button" aria-label="关闭错误提示" onClick={() => chat.setError("")}><X /></button></div>}
    <main className="app-surface">
      {view === "workbench" ? <Workbench entries={chat.entries} /> : view === "knowledge" ? <Knowledge /> : view === "models" ? <Models settings={settings} save={saveSettings} saving={saving} busy={chat.busy || chat.loading} /> : view === "settings" ? <div className="settings-layout"><nav className="settings-rail" aria-label="设置分类">{settingsSections.filter(s => s.id !== "models").map(s => <button key={s.id} aria-current={section === s.id ? "page" : undefined} onClick={() => setSection(s.id)}>{s.label}</button>)}</nav><div className="settings-container"><SettingsPage bridgeReady={chat.ready} section={section} /></div></div> : <div className={`chat-layout ${railOpen ? "rail-open" : ""}`}>
        {railOpen && <button className="rail-scrim" aria-label="收起会话列表" onClick={() => setRailOpen(false)} />}
        <aside className="chat-rail" aria-label="会话列表"><div className="rail-toolbar"><button className="new-chat" disabled={!chat.ready || blocked} onClick={() => { void chat.startNew(); setRailOpen(false); }}><Plus size={19} />新会话</button><label className="search-field"><MagnifyingGlass size={16} /><input aria-label="搜索会话" placeholder="搜索会话" value={search} onChange={e => setSearch(e.target.value)} /></label></div><nav className="conversation-list" aria-label="历史会话">{visibleEntries.map(entry => <button key={entry.key} disabled={blocked} aria-current={chat.session?.session_key === entry.key ? "page" : undefined} onClick={() => { void chat.open(entry.key); setRailOpen(false); }}><span className="conversation-title"><span className="truncate">{entryTitle(entry)}</span><time>{dateLabel(entry.updated_at)}</time></span><small>{entry.message_count} 条消息</small></button>)}{!visibleEntries.length && <p className="rail-empty">{search ? "没有找到会话" : "发送第一条消息后，会话会出现在这里。"}</p>}</nav><div className="rail-footer"><button onClick={() => void showPet()}><PawPrint size={19} />桌宠</button><span className="bridge-state" role="status">{chat.ready ? "本地服务已连接" : "正在连接…"}</span></div></aside>
        <section className="chat-content" aria-label="对话"><div className="chat-context"><button className="icon-button rail-toggle" aria-label="展开会话列表" aria-expanded={railOpen} onClick={() => setRailOpen(!railOpen)}><SidebarSimple /></button>{chat.session && <><span className="truncate">{chat.session.title}</span><div className="context-actions"><button className="icon-button" aria-label="重命名会话" title="重命名会话" disabled={blocked} onClick={() => { setName(chat.session!.title); setModalError(""); setModal("rename"); }}><PencilSimple /></button><button className="icon-button" aria-label="删除会话" title="删除会话" disabled={blocked} onClick={() => { setModalError(""); setModal("delete"); }}><Trash /></button></div></>}</div>
          <div className={`message-scroll ${!chat.session ? "is-empty" : ""}`} aria-label="消息记录" aria-busy={chat.loading}>{!chat.session ? <div className="chat-welcome"><h1>布置下一件事</h1><p>在下方输入；模型与附件都在同一条输入条里</p></div> : <div className="message-column">{chat.session.messages.filter(m => m.role === "user" || m.role === "assistant").map((message, i) => <article className={`chat-message ${message.role}`} key={message.id ?? i}><small>{message.role === "user" ? "你" : "Hasaki"}</small><Markdown>{message.content}</Markdown>{message.media?.map((path, index) => <p className="attachment-name" key={index}>附件：{path.split(/[\\/]/).at(-1)}</p>)}</article>)}{(chat.busy || chat.delta) && <article className="chat-message assistant"><small>Hasaki</small><Markdown>{chat.delta || "正在思考…"}</Markdown></article>}<div ref={bottom} /></div>}</div>
          <div className="composer-area"><div className="composer-status" role="status">{chat.tool ? `正在使用 ${chat.tool}` : saving ? "正在切换模型…" : ""}</div>{chat.attachments.length > 0 && <div className="attachment-list">{chat.attachments.map((path, i) => <span key={`${path}-${i}`} title={path}>{path.split(/[\\/]/).at(-1)}<button className="icon-button" aria-label={`移除附件 ${path.split(/[\\/]/).at(-1)}`} onClick={() => chat.setAttachments(items => items.filter((_, index) => index !== i))}><X size={13} /></button></span>)}</div>}<form className="composer" onSubmit={e => { e.preventDefault(); if (!saving) void chat.send(); }}><textarea ref={input} rows={1} aria-label="消息" value={chat.draft} disabled={!chat.ready || saving} onChange={e => chat.setDraft(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!saving) void chat.send(); } }} placeholder="继续布置任务…" /><div className="composer-tools">{registrations.length ? <label className="composer-model" title="全局默认模型"><SlidersHorizontal size={15} /><select aria-label="对话模型（全局默认）" disabled={blocked} value={registrations[0]?.id} onChange={e => { const first = registrations.find(r => r.id === e.target.value); if (settings && first) void saveSettings({ ...settings, models: { registrations: [first, ...registrations.filter(r => r.id !== first.id)] } }).catch(chat.fail); }}>{registrations.map(r => <option key={r.id} value={r.id}>{r.model}</option>)}</select><CaretDown size={12} /></label> : <button className="model-empty" type="button" onClick={() => navigate("models")}>配置模型</button>}<button className="icon-button" aria-label="添加附件" title="添加附件" type="button" disabled={blocked || !chat.ready} onClick={() => void window.miraDesktop.pickChatAttachments({ multiple: true }).then(paths => chat.setAttachments(value => [...new Set([...value, ...paths])])).catch(chat.fail)}><Plus size={19} /></button>{chat.busy ? <button className="send-button" type="button" aria-label="停止回复" onClick={() => void chat.cancel().catch(chat.fail)}><Square weight="fill" size={14} /></button> : <button className="send-button" type="submit" aria-label="发送消息" disabled={!chat.ready || blocked || (!chat.draft.trim() && !chat.attachments.length)}><PaperPlaneRight size={19} /></button>}</div></form></div>
        </section></div>}
    </main>
    {modal && <Modal title={modal === "rename" ? "重命名会话" : modal === "delete" ? "删除会话" : "桌宠"} close={() => { if (!modalBusy) setModal(null); }}>
      {modal === "rename" ? <form className="connection-form" onSubmit={e => { e.preventDefault(); void modalAction(() => chat.rename(name.trim()), true); }}><label>会话名称<input autoFocus required value={name} onChange={e => setName(e.target.value)} /></label><footer><button type="button" onClick={() => setModal(null)}>取消</button><button type="submit" className="primary" disabled={modalBusy || !name.trim()}>保存</button></footer></form> : modal === "delete" ? <div className="modal-content"><p>删除“{chat.session?.title}”及其消息记录？全局记忆会保留。</p><footer><button onClick={() => setModal(null)}>取消</button><button className="danger" disabled={modalBusy} onClick={() => void modalAction(chat.remove, true)}>确认删除</button></footer></div> : <div className="modal-content pet-controls"><p>在桌面陪伴你，支持语音和打开当前对话。</p><label>桌宠包<select value={pets.selected_package_id} disabled={modalBusy} onChange={e => void modalAction(async () => { setPets(await rpc<PetState>("pet.select", { package_id: e.target.value })); await window.miraDesktop.syncPet(true); })}><option value="" disabled>选择桌宠包</option>{pets.packages.map(p => <option key={p.id} value={p.id}>{p.display_name}</option>)}</select></label><button disabled={modalBusy} onClick={() => void modalAction(async () => { const path = await window.miraDesktop.pickPetPackage(); if (path) { setPets(await rpc<PetState>("pet.import", { path })); await window.miraDesktop.syncPet(true); } })}>导入桌宠 ZIP 包</button><footer><button disabled={modalBusy} onClick={() => void modalAction(() => window.miraDesktop.syncPet(false))}>隐藏桌宠</button><button className="primary" disabled={modalBusy || !pets.selected_package_id} onClick={() => void modalAction(() => window.miraDesktop.syncPet(true))}>显示桌宠</button></footer></div>}{modalError && <p role="alert" className="inline-error">{modalError}</p>}
    </Modal>}
  </div>;
}
