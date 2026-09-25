import { motion } from "framer-motion";
import type { ReactNode } from "react";

import "./motion.css";

interface ScrollRevealProps {
  children: ReactNode;
  /** 进入视口后的延迟（毫秒） */
  delay?: number;
  className?: string;
}

/**
 * 滚动到视口才浮现，每个实例只触发一次。
 *
 * 旧实现用 IntersectionObserver + threshold 0.12，对「高度超过视口的容器」
 * 永远达不到 12% 可见比例，整块内容会卡在 opacity 0 上（录音档案页 19 个元素全隐形）。
 * framer-motion 的 viewport.amount = 0 表示「露头就算」，与元素高度无关。
 */
export function ScrollReveal({ children, delay = 0, className }: ScrollRevealProps) {
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 18 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, amount: 0, margin: "0px 0px -6% 0px" }}
      transition={{ duration: 0.55, ease: [0.16, 1, 0.3, 1], delay: delay / 1000 }}
    >
      {children}
    </motion.div>
  );
}
