import { useEffect, useRef, type MouseEvent as ReactMouseEvent, type RefObject } from "react";

/*
 * 弹窗与抽屉的共用行为。全站约定：
 * - 打开时焦点移进弹窗（优先带 autoFocus 的控件，其次第一个可聚焦元素）；Tab、Shift+Tab 全由弹窗自己在可聚焦元素里
 *   按文档顺序循环，不靠浏览器原生的 Tab（Safari 默认 Tab 不停在按钮上，靠原生的话焦点一下就跑出弹窗）；
 *   只有日期这类浏览器在里面自己分格走 Tab 的输入框，在格子里的那几下还是交给浏览器；
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

/** 日期、时间这类输入框，浏览器自己在里面的几个小格子（年、月、日）之间走 Tab，走完才出输入框，没有接口能知道走到哪一格 */
const MULTI_STOP_INPUT = new Set(["date", "time", "datetime-local", "month", "week"]);

function isMultiStopInput(element: Element | null): boolean {
  return element instanceof HTMLInputElement && MULTI_STOP_INPUT.has(element.type);
}

/**
 * 弹窗里的 Tab 停靠点：可聚焦元素按文档顺序。单选组和浏览器一样只算一个点：选中的那个，
 * 没有选中的就取头一个（Shift+Tab 进来取最后一个）。
 */
function tabStops(root: HTMLElement, backward: boolean): HTMLElement[] {
  const all = focusableIn(root);
  const groups = new Map<string, HTMLInputElement[]>();
  const groupKey = (radio: HTMLInputElement) => `${radio.form ? "form" : ""}:${radio.name}`;
  for (const element of all) {
    if (element instanceof HTMLInputElement && element.type === "radio" && element.name) {
      const key = groupKey(element);
      groups.set(key, [...(groups.get(key) ?? []), element]);
    }
  }
  return all.filter((element) => {
    if (!(element instanceof HTMLInputElement) || element.type !== "radio" || !element.name) return true;
    const group = groups.get(groupKey(element)) ?? [];
    return element === (group.find((radio) => radio.checked) ?? (backward ? group[group.length - 1] : group[0]));
  });
}

/** 按下 Tab（backward 为 Shift+Tab）后，焦点该去 stops 里的第几个；到头了绕回另一头 */
function tabDestination(root: HTMLElement, stops: HTMLElement[], backward: boolean): number {
  const active = document.activeElement;
  const last = stops.length - 1;
  const at = stops.findIndex((stop) => stop === active);
  if (at >= 0) return (at + (backward ? -1 : 1) + stops.length) % stops.length;
  if (active && active !== root && root.contains(active)) {
    // 焦点在弹窗里一个不算停靠点的元素上（tabindex=-1 的行）：从它在页面里的位置往后（往前）找最近的
    let found = -1;
    if (backward) {
      for (let i = last; i >= 0 && found < 0; i -= 1) {
        if (active.compareDocumentPosition(stops[i]) & Node.DOCUMENT_POSITION_PRECEDING) found = i;
      }
    } else {
      found = stops.findIndex((stop) => active.compareDocumentPosition(stop) & Node.DOCUMENT_POSITION_FOLLOWING);
    }
    if (found >= 0) return found;
  }
  return backward ? last : 0;
}

// 浏览器里 Tab 进这些输入框会把里面的字全选（以前点过、光标停在中间也一样），.focus() 只会还原上次的光标
const SELECT_ON_TAB = new Set(["text", "search", "url", "tel", "password", "email", "number"]);

/** 从 start 起往前（往后）找第一个聚焦得了的停靠点：个别元素聚焦不了（禁用的 fieldset 里、display:none 的），焦点没动就试下一个，不卡在原地 */
function focusFrom(stops: HTMLElement[], start: number, backward: boolean) {
  const before = document.activeElement;
  for (let step = 0; step < stops.length; step += 1) {
    const target = stops[(((start + (backward ? -step : step)) % stops.length) + stops.length) % stops.length];
    target.focus();
    if (document.activeElement !== before) {
      if (target instanceof HTMLInputElement && SELECT_ON_TAB.has(target.type)) target.select();
      return;
    }
  }
}

/**
 * 日期输入框交给浏览器在年月日几格之间走 Tab 时，把走出去以后该落的那个停靠点临时挂上 tabindex=0（下个 tick 摘掉）：
 * Safari 默认 Tab 不停在按钮上，没有它，走完最后一格焦点就越过弹窗里的按钮、跑出页面；带了 tabindex 的元素，
 * 各家浏览器的 Tab 都会停。Chromium、Firefox 本来就停，多挂一下没有影响。
 */
function lendTabStop(target: HTMLElement) {
  if (target.hasAttribute("tabindex")) return;
  target.setAttribute("tabindex", "0");
  window.setTimeout(() => target.removeAttribute("tabindex"), 0);
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
      // Ctrl/Cmd+Tab 是切标签页；别人已经拦下的 Tab（自己管焦点的控件）和输入法组合中的 Tab 也不管
      if (event.key !== "Tab" || event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey) return;
      if (dialogStack[dialogStack.length - 1] !== root) return;
      const stops = tabStops(root, event.shiftKey);
      if (stops.length === 0) {
        event.preventDefault();
        return;
      }
      const active = document.activeElement;
      const at = stops.findIndex((stop) => stop === active);
      const destination = tabDestination(root, stops, event.shiftKey);
      // 日期这类输入框里有好几格，没有接口能知道走到哪一格，下面两种情形交给浏览器：
      // 1. 焦点就在日期框里：它还没走完格子，由浏览器走；它正好是弹窗里第一个 / 最后一个时才由我们接管，不让焦点从弹窗漏出去
      // 2. 倒着走、前一个正好是日期框（没有绕回）：浏览器落在它的最后一格，.focus() 落的是第一格
      const leaveToBrowser =
        (isMultiStopInput(active) && at !== (event.shiftKey ? 0 : stops.length - 1)) ||
        (event.shiftKey && at >= 0 && destination < at && isMultiStopInput(stops[destination]));
      if (leaveToBrowser) {
        if (!isMultiStopInput(stops[destination])) lendTabStop(stops[destination]);
        return;
      }
      event.preventDefault();
      focusFrom(stops, destination, event.shiftKey);
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
