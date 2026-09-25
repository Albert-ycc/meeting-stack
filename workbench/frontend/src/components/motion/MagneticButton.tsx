import { motion, useMotionValue, useSpring } from "framer-motion";
import type { ButtonHTMLAttributes, PointerEvent, ReactNode } from "react";

/** amicro 的 snappy 预设：够跟手，松手立刻归位不拖泥带水。 */
const SPRING = { stiffness: 400, damping: 28, mass: 0.8 } as const;

interface MagneticButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  /** 最大偏移像素，默认 5。工作台是高频操作界面，位移要克制。 */
  strength?: number;
}

/** 磁吸按钮：光标靠近时按钮朝光标方向轻微位移，移开归位。 */
export function MagneticButton({ children, strength = 5, ...rest }: MagneticButtonProps) {
  const x = useSpring(useMotionValue(0), SPRING);
  const y = useSpring(useMotionValue(0), SPRING);

  const onPointerMove = (event: PointerEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    x.set(((event.clientX - rect.left) / rect.width - 0.5) * strength * 2);
    y.set(((event.clientY - rect.top) / rect.height - 0.5) * strength * 2);
  };
  const reset = () => {
    x.set(0);
    y.set(0);
  };

  return (
    <motion.button
      {...(rest as Record<string, unknown>)}
      onPointerMove={onPointerMove}
      onPointerLeave={reset}
      style={{ x, y }}
      whileTap={{ scale: 0.95 }}
    >
      {children}
    </motion.button>
  );
}
