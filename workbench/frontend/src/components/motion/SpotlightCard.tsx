import { useRef, type MouseEvent, type ReactNode } from "react";

import "./motion.css";

interface SpotlightCardProps {
  children: ReactNode;
  className?: string;
}

/** 卡片追光：鼠标移动时高光跟随光标（适用于深色卡片）。 */
export function SpotlightCard({ children, className }: SpotlightCardProps) {
  const ref = useRef<HTMLDivElement>(null);

  const onMouseMove = (event: MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${event.clientX - rect.left}px`);
    el.style.setProperty("--my", `${event.clientY - rect.top}px`);
  };

  return (
    <div ref={ref} className={`spotlight-card ${className ?? ""}`} onMouseMove={onMouseMove}>
      <span className="spotlight-card__glow" aria-hidden="true" />
      {children}
    </div>
  );
}
