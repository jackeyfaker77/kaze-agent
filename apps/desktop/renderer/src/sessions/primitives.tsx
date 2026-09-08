import { useEffect, useRef, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { X } from "@phosphor-icons/react";

export function Markdown({ children }: { children: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ href, children }) => <a href={href} onClick={e => { e.preventDefault(); if (href) void window.miraDesktop.openExternal(href); }}>{children}</a> }}>{children}</ReactMarkdown></div>;
}
export function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current!; dialog.showModal(); return () => dialog.close(); }, []);
  return <dialog className="paper-modal" ref={ref} onCancel={e => { e.preventDefault(); close(); }} aria-label={title}>
    <header><h2>{title}</h2><button className="icon-button" aria-label="关闭" onClick={close}><X /></button></header>{children}
  </dialog>;
}
