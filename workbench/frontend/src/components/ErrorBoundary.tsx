import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** 变了就撤掉错误态（换视图、换会议）；不用 key，免得每次换都把内容区卸掉重建 */
  resetKey: string;
  /** ［重新载入］：撤掉错误态之前让外面重取这一页的数据 */
  onReset?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/** 内容区里任何一处渲染抛错，只把内容区换成错误态，侧栏和顶栏还在，不整页白屏 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("内容区渲染出错", error, info.componentStack);
  }

  componentDidUpdate(previous: ErrorBoundaryProps) {
    if (this.state.error && previous.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  private reload = () => {
    this.props.onReset?.();
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="async-state async-state--error content-error" role="alert">
        <span className="state-mark">!</span>
        <p>这一页出错了，侧栏和别的页面不受影响。</p>
        {error.message && <small>{error.message}</small>}
        <button className="ghost-button" onClick={this.reload} type="button">
          重新载入
        </button>
      </div>
    );
  }
}
