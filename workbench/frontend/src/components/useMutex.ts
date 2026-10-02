import { useCallback, useRef, useState } from "react";

export interface Mutex {
  /** 手头有一次写操作在跑：按钮据此置灰 */
  busy: boolean;
  /**
   * 互斥地跑一次写操作。手头有一次在跑就把新来的丢掉：双击同帧 state 还没刷新，只有 ref 拦得住，不会连发两个写请求。
   * 抛出的错在这里接住、交给 onError，调用方不用再包 try/catch。
   */
  run: (action: () => Promise<void>) => Promise<void>;
  /**
   * 排在手头那次后面跑。提示条上的［撤销］在写操作成功后、重新取数还没回来时就弹出来了，
   * 这时点撤销不能被互斥吞掉，也不能和手头那次抢着写。
   */
  runAfterCurrent: (action: () => Promise<void>) => Promise<void>;
}

/**
 * 页面里「写操作互斥 + 撤销要等手头那次做完」：待办页、项目详情、词典页原来各写了一遍。
 * onError 收失败原因（页面自己的提示条）；用最新一次渲染传进来的，旧提示条上的闭包晚点调用也报到现在的提示条。
 * 不要在 run 的动作里再 await runAfterCurrent：它排在这个动作后面，这个动作却在等它。
 */
export function useMutex(onError: (message: string) => void): Mutex {
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  // 排队等着接手的：前一次做完时当场把互斥交给它、不先放开，别的点击插不进来；
  // 也不靠反复查 busy 来等，标志出了岔子最多是一次被丢掉，不会空转把页面卡死
  const queueRef = useRef<Array<() => void>>([]);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  // 调用时互斥已经在自己手里
  const hold = useCallback(async (action: () => Promise<void>) => {
    try {
      await action();
    } catch (error) {
      onErrorRef.current(error instanceof Error ? error.message : "操作失败，请稍后重试");
    } finally {
      const next = queueRef.current.shift();
      if (next) {
        next();
      } else {
        busyRef.current = false;
        setBusy(false);
      }
    }
  }, []);

  const run = useCallback(
    async (action: () => Promise<void>) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      await hold(action);
    },
    [hold],
  );

  const runAfterCurrent = useCallback(
    async (action: () => Promise<void>) => {
      if (busyRef.current) {
        await new Promise<void>((resolve) => queueRef.current.push(resolve));
      } else {
        busyRef.current = true;
        setBusy(true);
      }
      await hold(action);
    },
    [hold],
  );

  return { busy, run, runAfterCurrent };
}
