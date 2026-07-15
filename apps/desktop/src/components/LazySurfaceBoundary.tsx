import { Component, type ErrorInfo, type ReactNode } from "react";
import { pageButtonClass } from "./ui";

export interface LazySurfaceBoundaryProps {
  readonly children: ReactNode;
  readonly title: string;
  readonly detail: string;
  readonly logCode: string;
}

export class LazySurfaceBoundary extends Component<
  LazySurfaceBoundaryProps,
  { readonly failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(this.props.logCode, error, info.componentStack);
  }

  render() {
    if (!this.state.failed) {
      return this.props.children;
    }
    return (
      <section className="rounded-xl border border-danger/30 bg-dangerSoft p-5" role="alert">
        <h2 className="text-lg font-semibold text-danger">{this.props.title}</h2>
        <p className="mt-2 text-sm text-danger">{this.props.detail}</p>
        <button
          type="button"
          className={`${pageButtonClass("neutral")} mt-4`}
          onClick={() => window.location.reload()}
        >
          앱 새로고침
        </button>
      </section>
    );
  }
}
