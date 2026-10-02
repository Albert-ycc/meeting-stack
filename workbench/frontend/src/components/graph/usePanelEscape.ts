import { useEffect, useRef } from "react";

/**
 * 节点面板开着时，Esc 在页面上哪里按都关面板（onEscape 为 null 表示没有面板开着，不监听）。
 *
 * 面板容器、画布上的 onKeyDown 只收得到焦点落在它们里面时的 Esc。点线（SVG 的 path 不能聚焦）、
 * Safari 里点节点（按钮不拿焦点）、点面板里的字，焦点都落在 body 上，Esc 谁也收不到，面板就按不掉。所以挂在 window 上听。
 *
 * 让路的几种情形，Esc 归别人：
 * - 输入框里（清搜索、收下拉是它们自己的事）；输入法组合输入中；焦点所在的控件已经处理了（preventDefault）；
 * - 面板上盖着弹窗或浮层（取径器、确认框、预览抽屉、图例）：一次 Esc 只关最上面一层，再按一次才轮到面板。
 *   弹窗开没开要在这次按键被任何人处理之前看：弹窗自己的 Esc 监听可能排在这里前面，先把弹窗关了、
 *   React 在两个监听之间重画完，再看就是「没有弹窗」，面板会被连带关掉。
 */
export function usePanelEscape(onEscape: (() => void) | null) {
  const latest = useRef(onEscape);
  latest.current = onEscape;
  const open = onEscape !== null;
  useEffect(() => {
    if (!open) return;
    let covered = false;
    const onCapture = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      covered = document.querySelector('[role="alertdialog"], [role="dialog"]') !== null;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
      if (event.defaultPrevented || covered) return;
      if ((event.target as Element | null)?.closest?.("select, input, textarea")) return;
      latest.current?.();
    };
    window.addEventListener("keydown", onCapture, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onCapture, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);
}
