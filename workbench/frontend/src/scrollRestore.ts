/*
 * 浏览器前进后退时的整页滚动位置（审核 P2：列表回得到原位）。
 *
 * 浏览器自己会在 popstate 之后按它给那一条历史记下的位置滚一次，可声档大多数页面是回来以后现取数据的：
 * 那一刻页面还只有「正在读取」，不够高，位置被截断（待办回到 238、需求池回到 0）。所以：
 * - 每一条历史自己记着离开时滚到哪（history.state.scroll）：滚动停下来记一次，应用内跳走之前再记一次；
 * - 回到一条记过位置的历史时，这一次不让浏览器恢复，等页面长到够高再滚过去；内容分几批到、上面插进东西，
 *   就跟着再放回去，直到页面不再变高（或用户自己动了滚轮、键盘、鼠标）为止。
 */

type HistoryWithScroll = { scroll?: number } | null;

const SETTLE_MS = 600; // 页面这么久不再变高，就算数据到齐了
const GIVE_UP_MS = 5000; // 再慢也不等了：能滚多少滚多少
const RECORD_DELAY_MS = 150;
// 用户自己动了就不再替他放回去
const USER_EVENTS = ["wheel", "touchstart", "keydown", "mousedown"] as const;

let recordTimer: number | undefined;
let cancelRestore: (() => void) | null = null;

function writeScroll(y: number) {
  try {
    history.replaceState({ ...((history.state as Record<string, unknown> | null) ?? {}), scroll: y }, "");
  } catch {
    // Safari 短时间内 replaceState 太多会抛错：少记一次不要紧
  }
}

/** 应用内跳走之前：把当前这一条历史的位置记下（不等滚动停下来的那次） */
export function recordScrollNow() {
  window.clearTimeout(recordTimer);
  writeScroll(Math.round(document.documentElement.scrollTop));
}

/** 滚动停下来记一次。浏览器前进后退时这条已经换成了新的历史，没记上的那次要丢掉，不然记到别人头上 */
export function startScrollRecorder(): () => void {
  const onScroll = () => {
    window.clearTimeout(recordTimer);
    recordTimer = window.setTimeout(() => writeScroll(Math.round(document.documentElement.scrollTop)), RECORD_DELAY_MS);
  };
  const onPopState = () => window.clearTimeout(recordTimer);
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("popstate", onPopState, true);
  return () => {
    window.clearTimeout(recordTimer);
    window.removeEventListener("scroll", onScroll);
    window.removeEventListener("popstate", onPopState, true);
  };
}

/** 停掉正在等的恢复：应用内跳去别处、换了检索时 */
export function cancelScrollRestore() {
  cancelRestore?.();
}

/** popstate 里调：落到的这一条记过位置的话，接管这一次恢复 */
export function restoreScrollFromHistory() {
  cancelScrollRestore();
  const target = (history.state as HistoryWithScroll)?.scroll;
  if (typeof target !== "number" || target <= 0) return;
  history.scrollRestoration = "manual";
  window.setTimeout(() => {
    history.scrollRestoration = "auto";
  }, 0);

  const root = document.documentElement;
  const startedAt = performance.now();
  let lastHeight = -1;
  let changedAt = startedAt;
  let reached = false;
  let frame = 0;

  const stop = () => {
    window.cancelAnimationFrame(frame);
    for (const type of USER_EVENTS) window.removeEventListener(type, stop, true);
    if (cancelRestore === stop) cancelRestore = null;
  };
  const tick = () => {
    const now = performance.now();
    const height = root.scrollHeight;
    if (height !== lastHeight) {
      lastHeight = height;
      changedAt = now;
    }
    if (height - root.clientHeight >= target) {
      reached = true;
      if (Math.round(root.scrollTop) !== target) root.scrollTop = target;
    }
    if (now - startedAt > GIVE_UP_MS) {
      if (!reached) root.scrollTop = target;
      stop();
      return;
    }
    if (reached && now - changedAt > SETTLE_MS) {
      stop();
      return;
    }
    frame = window.requestAnimationFrame(tick);
  };
  for (const type of USER_EVENTS) window.addEventListener(type, stop, true);
  cancelRestore = stop;
  frame = window.requestAnimationFrame(tick);
}
