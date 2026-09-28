import { useEffect, useMemo, useState } from "react";

import { ApiError, type ApiClient } from "../../api";

/*
 * 列表里的小签「3 场会提到」（4d）：一个列表只发一次批量请求（最多 200 个 id），结果按模块缓存，
 * 版本（宿主的 reloadKey 这类）变了清掉；接口回 404（旧后台）时不显示小签。
 */

const BATCH = 200;
const cache = new Map<number, number>();
let cacheVersion: string | null = null;
let unsupported = false;

/** 测试之间清空 */
export function clearMentionedCounts() {
  cache.clear();
  cacheVersion = null;
  unsupported = false;
}

export function useMentionedCounts(
  apiClient: Partial<Pick<ApiClient, "mentionedCounts">>,
  fileIds: Array<number | null | undefined>,
  version = "",
): Map<number, number> | null {
  const ids = useMemo(
    () => Array.from(new Set(fileIds.filter((id): id is number => typeof id === "number"))).sort((a, b) => a - b),
    // 按内容比，不按数组身份
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fileIds.join(",")],
  );
  const key = ids.join(",");
  const [tick, setTick] = useState(0);
  const [disabled, setDisabled] = useState(unsupported);

  useEffect(() => {
    if (cacheVersion !== version) {
      cache.clear();
      cacheVersion = version;
    }
    if (unsupported || typeof apiClient.mentionedCounts !== "function") return;
    const missing = ids.filter((id) => !cache.has(id)).slice(0, BATCH);
    if (!missing.length) return;
    let cancelled = false;
    apiClient
      .mentionedCounts(missing)
      .then((payload) => {
        for (const id of missing) cache.set(id, Number(payload.counts[String(id)] ?? 0));
        if (!cancelled) setTick((value) => value + 1);
      })
      .catch((reason) => {
        if (reason instanceof ApiError && reason.status === 404) {
          unsupported = true;
          if (!cancelled) setDisabled(true);
        }
      });
    return () => {
      cancelled = true;
    };
    // tick 只用来重画
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, key, version]);

  return useMemo(() => {
    if (disabled || typeof apiClient.mentionedCounts !== "function") return null;
    const result = new Map<number, number>();
    for (const id of ids) {
      const count = cache.get(id);
      if (count !== undefined) result.set(id, count);
    }
    return result;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [disabled, key, tick, apiClient]);
}
