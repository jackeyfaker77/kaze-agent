import { useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { SessionApp } from "./SessionApp";
import { DesktopErrorBoundary } from "./diagnostics/DesktopErrorBoundary";
import "./styles.css";

function Application({ ready }: { ready: () => void }) {
  useLayoutEffect(ready, [ready]);
  return <DesktopErrorBoundary><SessionApp /></DesktopErrorBoundary>;
}

export function mount(ready: () => void): void {
  createRoot(document.getElementById("root")!).render(<Application ready={ready} />);
}
