import { useCallback, useState, type SetStateAction } from "react";

/*
 * 列表页的检索条件、页签和页码：离开页面再回来要原样还在。
 * 页面组件切走时会卸载，普通 useState 跟着丢；这里把值存在模块级的 Map 里（同一次打开内即时恢复），
 * 同时写一份到 sessionStorage，刷新页面也不丢；关掉标签页才清空。
 * 只放「用户选的条件」，不放接口数据。
 */
const STORAGE_PREFIX = "meeting-workbench:view:";
const memory = new Map<string, unknown>();

function readStored<T>(key: string, initial: T): T {
  if (memory.has(key)) return memory.get(key) as T;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_PREFIX + key);
    if (raw !== null) {
      const parsed = JSON.parse(raw) as T;
      memory.set(key, parsed);
      return parsed;
    }
  } catch {
    // 存储不可用或内容损坏：用默认值。
  }
  return initial;
}

function writeStored(key: string, value: unknown) {
  memory.set(key, value);
  try {
    window.sessionStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value));
  } catch {
    // 存不下也不影响本次使用，只是刷新后回到默认。
  }
}

/** 用法同 useState，多一个全站唯一的 key，如 "tasks.activeTab"。值必须能 JSON 序列化。 */
export function usePersistentState<T>(key: string, initial: T): [T, (next: SetStateAction<T>) => void] {
  const [value, setValue] = useState<T>(() => readStored(key, initial));
  const update = useCallback(
    (next: SetStateAction<T>) => {
      setValue((previous) => {
        const resolved = typeof next === "function" ? (next as (current: T) => T)(previous) : next;
        writeStored(key, resolved);
        return resolved;
      });
    },
    [key],
  );
  return [value, update];
}

/** 测试之间清空，避免上一个用例选的条件带到下一个。 */
export function clearPersistentViewState() {
  memory.clear();
  try {
    for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(STORAGE_PREFIX)) window.sessionStorage.removeItem(key);
    }
  } catch {
    // 忽略
  }
}
