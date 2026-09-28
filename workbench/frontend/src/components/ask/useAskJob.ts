import { useEffect, useRef } from "react";

import { ApiError, type ApiClient } from "../../api";
import type { AskJob } from "../../types";

/** 每 1.5 秒问一次任务 */
export const ASK_POLL_MS = 1_500;

export type AskJobUpdate = { kind: "job"; job: AskJob } | { kind: "expired"; text: string };

/*
 * 轮询一个问答任务（4g）：每 1.5 秒一次；页面隐藏（document.hidden）时停，visibilitychange 回来立刻问一次；
 * 按序号丢掉迟到的回复；好了、停了或 404 就停。手机锁屏 30 秒再打开照样拿到回答（任务留 30 分钟）。
 */
export function useAskJob(
  apiClient: Pick<ApiClient, "askJob">,
  jobId: string | null,
  onUpdate: (update: AskJobUpdate) => void,
): void {
  const update = useRef(onUpdate);
  update.current = onUpdate;

  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let sequence = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = () => {
      if (stopped || document.hidden) return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => void poll(), ASK_POLL_MS);
    };

    const poll = async () => {
      if (stopped) return;
      timer = null;
      sequence += 1;
      const mine = sequence;
      try {
        const job = await apiClient.askJob(jobId);
        if (stopped || mine !== sequence) return;
        if (job.state === "waiting") {
          schedule();
          return;
        }
        stopped = true;
        update.current({ kind: "job", job });
      } catch (error) {
        if (stopped || mine !== sequence) return;
        if (error instanceof ApiError && error.status === 404) {
          stopped = true;
          update.current({ kind: "expired", text: error.message });
          return;
        }
        // 网络抖一下：接着问
        schedule();
      }
    };

    const onVisibility = () => {
      if (stopped) return;
      if (document.hidden) {
        if (timer !== null) clearTimeout(timer);
        timer = null;
        return;
      }
      void poll();
    };

    document.addEventListener("visibilitychange", onVisibility);
    schedule();
    return () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [apiClient, jobId]);
}
