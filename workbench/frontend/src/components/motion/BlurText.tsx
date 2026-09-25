import "./motion.css";

interface BlurTextProps {
  /** 要展示的文本 */
  text: string;
  className?: string;
}

/** 标题整体从 blur(8px) 渐入清晰，轻微上移。挂载时播放一次。 */
export function BlurText({ text, className }: BlurTextProps) {
  return (
    <span className={`blur-text ${className ?? ""}`} aria-label={text}>
      {text}
    </span>
  );
}
