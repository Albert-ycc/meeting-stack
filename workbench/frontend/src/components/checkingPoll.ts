/*
 * 读后台资料盘缓存的接口（项目总文件夹、同名文件夹、改名找回）在缓存还没好时返回 state: "checking"，
 * 前端隔 2 秒再问一次，直到不是 checking（照关系图 roots 的做法）。组件卸载或参数变了就停。
 */

export const CHECKING_POLL_MS = 2000;

interface PollOptions {
  /** 读失败时（只报一次，不再重试） */
  onError?: (error: unknown) => void;
}

/** 调 load；结果还在 checking 时每 2 秒再调一次。返回停止函数，放进 useEffect 的清理里。 */
export function pollWhileChecking<T>(
  load: () => Promise<T>,
  isChecking: (value: T) => boolean,
  onValue: (value: T) => void,
  { onError }: PollOptions = {},
): () => void {
  let active = true;
  let timer = 0;
  const run = () => {
    load()
      .then((value) => {
        if (!active) return;
        onValue(value);
        if (isChecking(value)) timer = window.setTimeout(run, CHECKING_POLL_MS);
      })
      .catch((error: unknown) => {
        if (active) onError?.(error);
      });
  };
  run();
  return () => {
    active = false;
    window.clearTimeout(timer);
  };
}
