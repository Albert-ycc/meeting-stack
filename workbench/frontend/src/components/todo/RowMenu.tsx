import { useState, type KeyboardEvent } from "react";

export interface RowMenuItem {
  label: string;
  /** 取消任务这类不好撤回的动作标红 */
  danger?: boolean;
  act: () => void;
}

/** 行内「⋯」更多操作：Esc 收起并把焦点还给触发钮，上下键在菜单项间移动。样式在 TasksPage.css 的 .task-menu。 */
interface RowMenuProps {
  items: RowMenuItem[];
  disabled?: boolean;
  /** 触发钮的可访问名带上任务名，读屏不会听到一串「更多操作」 */
  label?: string;
  /** 外层要让页面同一时刻只开一个弹层时，由外层管开合；不传就自己管 */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export function RowMenu({ items, disabled, label = "更多操作", open: controlledOpen, onOpenChange }: RowMenuProps) {
  const [ownOpen, setOwnOpen] = useState(false);
  const open = controlledOpen ?? ownOpen;
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
            className="task-menu__pop"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={onKeyDown}
            ref={(element) => {
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
