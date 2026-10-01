import { useCallback, useState, type SetStateAction } from "react";

/*
 * 列表页的检索条件、页签和页码：离开页面再回来要原样还在。
 * 页面组件切走时会卸载，普通 useState 跟着丢；这里把值存在模块级的 Map 里（同一次打开内即时恢复），
 * 同时写一份到 sessionStorage，刷新页面也不丢；关掉标签页才清空。
 * 要「记在本机」的（需求池的筛选，关掉浏览器再开也还在）传 { local: true }，改写 localStorage。
 * 只放「用户选的条件」，不放接口数据。
 */
const STORAGE_PREFIX = "meeting-workbench:view:";
const memory = new Map<string, unknown>();

interface PersistOptions {
  /** 写 localStorage：关掉标签页、重开浏览器也保留 */
  local?: boolean;
}

function storageFor(options: PersistOptions): Storage {
  return options.local ? window.localStorage : window.sessionStorage;
}

function readStored<T>(key: string, initial: T, options: PersistOptions): T {
  if (memory.has(key)) return memory.get(key) as T;
  try {
    const raw = storageFor(options).getItem(STORAGE_PREFIX + key);
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

function writeStored(key: string, value: unknown, options: PersistOptions) {
  memory.set(key, value);
  try {
    storageFor(options).setItem(STORAGE_PREFIX + key, JSON.stringify(value));
  } catch {
    // 存不下也不影响本次使用，只是刷新后回到默认。
  }
}

/** 用法同 useState，多一个全站唯一的 key，如 "tasks.activeTab"。值必须能 JSON 序列化。 */
export function usePersistentState<T>(
  key: string,
  initial: T,
  options: PersistOptions = {},
): [T, (next: SetStateAction<T>) => void] {
  const local = Boolean(options.local);
  const [value, setValue] = useState<T>(() => readStored(key, initial, { local }));
  const update = useCallback(
    (next: SetStateAction<T>) => {
      setValue((previous) => {
        const resolved = typeof next === "function" ? (next as (current: T) => T)(previous) : next;
        writeStored(key, resolved, { local });
        return resolved;
      });
    },
    [key, local],
  );
  return [value, update];
}

/** 在页面外改一个记着的值（页面下次挂载时读到）：比如认领完回需求池，要落在「进行中」页签。 */
export function writePersistentState(key: string, value: unknown, options: PersistOptions = {}) {
  writeStored(key, value, { local: Boolean(options.local) });
}

/** 测试之间清空，避免上一个用例选的条件带到下一个。 */
export function clearPersistentViewState() {
  memory.clear();
  for (const storage of [window.sessionStorage, window.localStorage]) {
    try {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        const key = storage.key(index);
        if (key?.startsWith(STORAGE_PREFIX)) storage.removeItem(key);
      }
    } catch {
      // 忽略
    }
  }
}
