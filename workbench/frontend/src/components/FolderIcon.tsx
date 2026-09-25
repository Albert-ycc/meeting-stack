interface FolderIconProps {
  className?: string;
}

/**
 * 线性文件夹图标。材料根目录列表行、取径器行、移除确认弹窗共四处用到同一个图标，
 * 抽出来避免各处各画一个占位方块（之前那个占位块太像空 checkbox，容易被看成可勾选）。
 */
export function FolderIcon({ className }: FolderIconProps) {
  return (
    <svg aria-hidden="true" className={className} fill="none" viewBox="0 0 16 16">
      <path
        d="M2 4.5c0-.552.448-1 1-1h3.4l1 1.3H13c.552 0 1 .448 1 1V12c0 .552-.448 1-1 1H3c-.552 0-1-.448-1-1V4.5Z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.3"
      />
    </svg>
  );
}
