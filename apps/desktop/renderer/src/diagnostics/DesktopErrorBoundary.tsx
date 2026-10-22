import React from "react";

type DesktopErrorBoundaryProps = {
  children: React.ReactNode;
};

type DesktopErrorBoundaryState = {
  hasError: boolean;
};

function reportBoundaryError(error: Error, componentStack: string): void {
  try { window.miraDesktop?.reportRendererDiagnostic({
    kind: "error-boundary",
    message: error.message,
    stack: error.stack,
    componentStack,
  }); } catch { /* Keep the recovery surface visible even if the bridge is unavailable. */ }
}

/** Catches renderer tree failures so desktop crashes degrade into a visible fallback instead of a blank window. */
export class DesktopErrorBoundary extends React.Component<
  DesktopErrorBoundaryProps,
  DesktopErrorBoundaryState
> {
  state: DesktopErrorBoundaryState = {
    hasError: false,
  };

  static getDerivedStateFromError(): DesktopErrorBoundaryState {
    return {
      hasError: true,
    };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    reportBoundaryError(error, info.componentStack ?? "");
  }

  render(): React.ReactNode {
    if (!this.state.hasError) {
      return this.props.children;
    }
    return (
      <div className="grid h-screen place-items-center bg-[var(--app-bg)] px-6 text-center text-[#3E434B]">
        <div className="max-w-[320px] rounded-[20px] border border-[rgba(31,41,55,0.08)] bg-white/86 px-6 py-7 shadow-[0_18px_48px_rgba(31,41,55,0.08)]">
          <div className="text-sm font-semibold">界面暂时不可用</div>
          <div className="mt-2 text-[13px] leading-6 text-[#667085]">
            可以重新加载工作区后再试。
          </div>
          <button type="button" className="mt-4 rounded-lg border px-4 py-2" onClick={() => window.location.reload()}>重新加载</button>
        </div>
      </div>
    );
  }
}
