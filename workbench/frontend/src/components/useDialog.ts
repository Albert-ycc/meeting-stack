import { useEffect, useRef, type MouseEvent as ReactMouseEvent, type RefObject } from "react";

/*
 * 弹窗与抽屉的共用行为。全站约定：
 * - 打开时焦点移进弹窗（优先带 autoFocus 的控件，其次第一个可聚焦元素），Tab 在弹窗内循环；
 * - 关闭后焦点回到打开它的那个按钮；
 * - 弹窗开着时页面本身不滚动；
 * - Esc 关闭（useDialogEscape）：弹窗叠着弹窗时一次只关最上面那层；中文输入法组合输入中的 Esc 只取消候选词，不关弹窗；
 * - 有输入内容的表单弹窗点背景不关，只能 ✕ / 取消 / Esc 关，避免误触丢掉填了一半的内容；
 *   只做选择的弹窗（选文件夹、关联会议/任务）点背景关，用 useBackdropDismiss 判定。
 */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// 嵌套弹窗（表单里再开取径器）只让最上层那个接管 Tab。
const dialogStack: HTMLElement[] = [];
let scrollLocks = 0;
let previousOverflow = "";

// 带 autoFocus 的弹窗（新建任务的标题框等）挂上的那一刻焦点就进了弹窗，比下面记打开者的 effect 早，
// 那时 activeElement 已经是弹窗里的控件。焦点移进来之前在谁身上，看最近一次 focusin 的 relatedTarget
let focusedBefore: HTMLElement | null = null;
if (typeof document !== "undefined") {
  document.addEventListener(
    "focusin",
    (event) => {
      focusedBefore = event.relatedTarget instanceof HTMLElement ? event.relatedTarget : null;
    },
    true,
  );
}

function focusableIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.closest("[inert]") && element.getAttribute("aria-hidden") !== "true",
  );
}

export function useDialogFocus(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    let opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (opener && root.contains(opener)) opener = focusedBefore && !root.contains(focusedBefore) ? focusedBefore : null;
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

/** 这个弹窗是不是最上面那层：抽屉上再盖抽屉时，Esc 只该关最上面的，各层自己的 Esc 监听先问一句 */
export function isTopDialog(root: HTMLElement | null): boolean {
  return root !== null && dialogStack[dialogStack.length - 1] === root;
}

/**
 * 按 Esc 关弹窗，全站统一从这里走，弹窗自己不再写 Esc 监听、也不用 stopPropagation 或「子弹窗开着」的标志防着：
 * 只有最上面那一层（isTopDialog）响应，弹窗里再开弹窗、抽屉上再盖弹窗，一次 Esc 只关一层。
 * ref 要和 useDialogFocus 用的是同一个。onEscape 每次渲染换成最新的，里面自己判断现在能不能关（比如保存中不关）。
 *
 * 每个弹窗只在挂载时登记一次监听，挂在 window 的冒泡阶段：下层弹窗的监听总是排在上层前面，
 * 上层关掉、从栈里退出的那一刻下层已经问过「我是不是最上层」，不会同一次按键里连下层一起关；
 * 元素上、document 上的监听都排在 window 前面，先看到这次 Esc，不会撞上弹窗已经消失了的页面
 * （GlossaryTermEditor 靠看确认框还在不在来避让，就靠这个顺序）。
 */
export function useDialogEscape(ref: RefObject<HTMLElement | null>, onEscape: () => void) {
  const latest = useRef(onEscape);
  latest.current = onEscape;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // keyCode 229：Safari 里组合输入结束那一下 isComposing 已经是 false
      if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
      if (isTopDialog(ref.current)) latest.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [ref]);
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
