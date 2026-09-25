import type { ReactNode } from "react";

import "./motion.css";

interface FadeContentProps {
  /** 变化时触发淡入动画（如当前视图 key） */
  transitionKey: string;
  children: ReactNode;
}

/**
 * 视图切换时内容淡入。transitionKey 变化即卸载重挂载，播放 fade-in。
 *
 * 这里刻意不用 AnimatePresence：mode="wait" 要等旧内容的退出动画跑完才挂新内容，
 * 一旦动画时钟不推进（后台标签页、无障碍减动效、测试环境），新内容就永远不挂载。
 * 纯 CSS 的重挂载淡入没有这个时序依赖，缓动同样能用指数曲线。
 */
export function FadeContent({ transitionKey, children }: FadeContentProps) {
  return (
    <div className="fade-content" key={transitionKey}>
      {children}
    </div>
  );
}
