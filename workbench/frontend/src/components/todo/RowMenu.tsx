import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

export interface RowMenuItem {
  label: string;
  /** 取消任务这类不好撤回的动作标红 */
  danger?: boolean;
  act: () => void;
}

/**
 * 行内「⋯」更多操作：Esc 收起并把焦点还给触发钮，上下键在菜单项间移动。样式在 TasksPage.css 的 .task-menu。
 * 默认往下展开；下面放不下、上面地方更大时往上展开（墙面最后一排、待办页底部那几行）。
 */
interface RowMenuProps {
  items: RowMenuItem[];
  disabled?: boolean;
  /** 触发钮的可访问名带上任务名，读屏不会听到一串「更多操作」 */
  label?: string;
  /** 外层要让页面同一时刻只开一个弹层时，由外层管开合；不传就自己管 */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

/** 菜单和触发钮之间的空隙，和 .task-menu__pop 的 top: calc(100% + 5px) 一致 */
const POP_GAP = 5;

export function RowMenu({ items, disabled, label = "更多操作", open: controlledOpen, onOpenChange }: RowMenuProps) {
  const [ownOpen, setOwnOpen] = useState(false);
  const open = controlledOpen ?? ownOpen;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [upward, setUpward] = useState(false);

  // 菜单挂上、还没画出来之前量一次：触发钮下面到窗口底的地方放不下菜单，且上面更宽裕，就翻到上面
  useLayoutEffect(() => {
    if (!open || !triggerRef.current || !popRef.current) return;
    const trigger = triggerRef.current.getBoundingClientRect();
    const height = popRef.current.getBoundingClientRect().height + POP_GAP;
    const below = window.innerHeight - trigger.bottom;
    setUpward(below < height && trigger.top > below);
  }, [open]);
  const setOpen = (next: boolean) => {
    setOwnOpen(next);
    onOpenChange?.(next);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const entries = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    const index = entries.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape") {
      event.stopPropagation();
      const trigger = event.currentTarget.parentElement?.querySelector<HTMLElement>(".task-menu__trigger");
      setOpen(false);
      trigger?.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      entries[(index + step + entries.length) % entries.length]?.focus();
    }
  };

  return (
    <span className="task-menu">
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={label}
        className="task-menu__trigger"
        ref={triggerRef}
        disabled={disabled}
        onClick={(event) => {
          event.stopPropagation();
          setOpen(!open);
        }}
        type="button"
      >
        ⋯
      </button>
      {open && (
        <>
          {/* 透明遮罩：点菜单外任何地方收起 */}
          <div className="task-menu__scrim" onClick={() => setOpen(false)} />
          <div
            className={`task-menu__pop${upward ? " is-up" : ""}`}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={onKeyDown}
            ref={(element) => {
              popRef.current = element;
              // 菜单一打开焦点就落到第一项，键盘用户可以直接上下选。
              if (element && !element.contains(document.activeElement)) {
                element.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
              }
            }}
            role="menu"
          >
            {items.map((item) => (
              <button
                className={item.danger ? "is-danger" : undefined}
                key={item.label}
                onClick={(event) => {
                  event.stopPropagation();
                  setOpen(false);
                  item.act();
                }}
                role="menuitem"
                type="button"
              >
                {item.label}
              </button>
            ))}
          </div>
        </>
      )}
    </span>
  );
}
