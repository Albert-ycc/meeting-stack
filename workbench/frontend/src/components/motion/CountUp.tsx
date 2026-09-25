import { useEffect, useRef, useState } from "react";

import "./motion.css";

interface CountUpProps {
  /** 目标值；非数字（如 "—"）原样显示，不滚动 */
  value: number | string;
  /** 滚动时长（毫秒），默认 1100 */
  duration?: number;
}

/** 数字从上一个值缓动滚到目标值（easeOutCubic），首次挂载从 0 起滚。 */
export function CountUp({ value, duration = 1100 }: CountUpProps) {
  const [display, setDisplay] = useState<number | string>(() =>
    typeof value === "number" ? 0 : value,
  );
  const fromRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (typeof value !== "number") {
      setDisplay(value);
      return;
    }
    const from = fromRef.current;
    const start = performance.now();
    const tick = (now: number) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplay(Math.round(from + (value - from) * eased));
      if (progress < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = value;
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [value, duration]);

  return <>{display}</>;
}
