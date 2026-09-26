import { useEffect, useRef, type MouseEvent as ReactMouseEvent, type RefObject } from "react";

/*
 * 弹窗与抽屉的共用行为。全站约定：
 * - 打开时焦点移进弹窗（优先带 autoFocus 的控件，其次第一个可聚焦元素），Tab 在弹窗内循环；
 * - 关闭后焦点回到打开它的那个按钮；
 * - 弹窗开着时页面本身不滚动；
 * - Esc 关闭，但中文输入法组合输入中的 Esc 只取消候选词，不关弹窗（判 event.isComposing）；
 * - 有输入内容的表单弹窗点背景不关，只能 ✕ / 取消 / Esc 关，避免误触丢掉填了一半的内容；
 *   只做选择的弹窗（选文件夹、关联会议/任务）点背景关，用 useBackdropDismiss 判定。
 */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// 嵌套弹窗（表单里再开取径器）只让最上层那个接管 Tab。
const dialogStack: HTMLElement[] = [];
let scrollLocks = 0;
let previousOverflow = "";

function focusableIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.closest("[inert]") && element.getAttribute("aria-hidden") !== "true",
  );
}

export function useDialogFocus(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogStack.push(root);

    if (scrollLocks === 0) {
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    scrollLocks += 1;

    // autoFocus 在挂载时已经生效的话就不抢；否则把焦点放进弹窗，键盘用户不用从页面顶部 Tab 过来。
    const frame = window.requestAnimationFrame(() => {
      if (root.contains(document.activeElement)) return;
      const preferred = root.querySelector<HTMLElement>("[autofocus], [data-autofocus]");
      const target = preferred ?? focusableIn(root)[0];
      if (target) target.focus();
      else {
        if (!root.hasAttribute("tabindex")) root.setAttribute("tabindex", "-1");
        root.focus();
      }
    });

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || dialogStack[dialogStack.length - 1] !== root) return;
      const items = focusableIn(root);
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !root.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !root.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);

    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKeyDown);
      const index = dialogStack.lastIndexOf(root);
      if (index >= 0) dialogStack.splice(index, 1);
      scrollLocks = Math.max(0, scrollLocks - 1);
      if (scrollLocks === 0) document.body.style.overflow = previousOverflow;
      // 打开者还在页面上才还焦点（比如删除后那一行已经没了，就别硬塞）。
      if (opener && opener.isConnected) opener.focus();
    };
    // 弹窗挂载一次只绑定一次；ref 对象本身是稳定的。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

/**
 * 选择类弹窗的背景点击关闭：只有按下和松开都落在背景本身上才算。
 * 否则在输入框里拖选文字、松手时滑到了背景上，也会触发 overlay 的 click 把弹窗关掉。
 */
export function useBackdropDismiss(onDismiss: () => void, disabled = false) {
  const pressedOnBackdrop = useRef(false);
  return {
    onMouseDown: (event: ReactMouseEvent<HTMLElement>) => {
      pressedOnBackdrop.current = event.target === event.currentTarget;
    },
    onClick: (event: ReactMouseEvent<HTMLElement>) => {
      const shouldDismiss = pressedOnBackdrop.current && event.target === event.currentTarget;
      pressedOnBackdrop.current = false;
      if (shouldDismiss && !disabled) onDismiss();
    },
  };
}
