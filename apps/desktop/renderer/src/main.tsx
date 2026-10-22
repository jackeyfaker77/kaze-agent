import { registerRendererGlobalDiagnostics } from "./diagnostics/rendererGlobalDiagnostics";
import { startDesktopRenderer } from "./startup";

registerRendererGlobalDiagnostics();
void startDesktopRenderer(() => import("./mountSessionApp"));
