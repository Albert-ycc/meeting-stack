import type { ReactNode } from "react";

/** 词典工作台用到的小图标，15px 网格、跟随文字颜色 */
function Icon({ children, width = 1.5, size = 15 }: { children: ReactNode; width?: number; size?: number }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={width}
      viewBox="0 0 15 15"
      width={size}
    >
      {children}
    </svg>
  );
}

export const IconTray = () => (
  <Icon>
    <path d="M1.5 8.5 3.2 2.8a1 1 0 0 1 1-.8h6.6a1 1 0 0 1 1 .8l1.7 5.7v3.3a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1Z" />
    <path d="M1.5 8.5h3.4l.9 1.6h3.4l.9-1.6h3.4" />
  </Icon>
);
export const IconCheck = () => (
  <Icon>
    <circle cx="7.5" cy="7.5" r="6" />
    <path d="m4.8 7.6 1.9 1.9 3.6-3.8" />
  </Icon>
);
export const IconAll = () => (
  <Icon>
    <rect height="5" rx="1" width="5" x="1.5" y="1.5" />
    <rect height="5" rx="1" width="5" x="8.5" y="1.5" />
    <rect height="5" rx="1" width="5" x="1.5" y="8.5" />
    <rect height="5" rx="1" width="5" x="8.5" y="8.5" />
  </Icon>
);
export const IconGlobe = () => (
  <Icon>
    <circle cx="7.5" cy="7.5" r="6" />
    <path d="M1.5 7.5h12M7.5 1.5c1.8 2 1.8 10 0 12M7.5 1.5c-1.8 2-1.8 10 0 12" />
  </Icon>
);
export const IconChevron = ({ size = 12 }: { size?: number }) => (
  <Icon size={size} width={1.6}>
    <path d="m5.5 3 4.5 4.5L5.5 12" />
  </Icon>
);
export const IconBack = () => (
  <Icon size={13} width={1.8}>
    <path d="M9.5 3 5 7.5 9.5 12" />
  </Icon>
);
export const IconTrash = ({ size = 13 }: { size?: number }) => (
  <Icon size={size}>
    <path d="M2.5 4h10M6 4V2.5h3V4M3.8 4l.7 9h6l.7-9" />
  </Icon>
);
export const IconX = () => (
  <Icon size={10} width={1.8}>
    <path d="m3 3 9 9M12 3l-9 9" />
  </Icon>
);
export const IconTarget = () => (
  <Icon size={14}>
    <circle cx="7.5" cy="7.5" r="5.8" />
    <circle cx="7.5" cy="7.5" r="2.4" />
  </Icon>
);
export const IconPlus = () => (
  <Icon size={13} width={1.8}>
    <path d="M7.5 2v11M2 7.5h11" />
  </Icon>
);
export const IconCaret = () => (
  <Icon size={13} width={1.8}>
    <path d="m3.5 5.5 4 4 4-4" />
  </Icon>
);
export const IconInbox = () => (
  <Icon size={22} width={1.2}>
    <path d="M1.5 8.5 3.2 2.8a1 1 0 0 1 1-.8h6.6a1 1 0 0 1 1 .8l1.7 5.7v3.3a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1Z" />
    <path d="M1.5 8.5h3.4l.9 1.6h3.4l.9-1.6h3.4" />
  </Icon>
);
export function IconSearch() {
  return (
    <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 14 14" width="14">
      <circle cx="6" cy="6" r="4.6" />
      <path d="M9.7 9.7 13 13" strokeLinecap="round" />
    </svg>
  );
}
export function IconPlay() {
  return (
    <svg aria-hidden="true" height="9" viewBox="0 0 10 10" width="9">
      <path d="M2 1.2v7.6L8.6 5Z" fill="currentColor" />
    </svg>
  );
}

/** 搜索命中的那一段标出来（不分大小写，只标第一处） */
export function Highlight({ text, needle }: { text: string; needle: string }) {
  if (!needle) return <>{text}</>;
  const at = text.toLowerCase().indexOf(needle);
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark className="gw-hl">{text.slice(at, at + needle.length)}</mark>
      {text.slice(at + needle.length)}
    </>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd>{children}</kbd>;
}
