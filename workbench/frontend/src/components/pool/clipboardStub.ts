/*
 * 用例里的剪贴板替身。jsdom 没有 navigator.clipboard，也没有 ClipboardItem。
 * 注意不要用 userEvent.setup()：它会给 navigator 装一个只读的 clipboard，后面的用例就换不了；直接调 userEvent.click 或 fireEvent。
 */
import { vi } from "vitest";

/** 只记下传进来的内容（值可以是 Promise<Blob>），和浏览器的 ClipboardItem 一样 */
export class FakeClipboardItem {
  readonly items: Record<string, Promise<Blob> | Blob>;
  constructor(items: Record<string, Promise<Blob> | Blob>) {
    this.items = items;
  }
}

export interface ClipboardStubs {
  write: ReturnType<typeof vi.fn>;
  writeText: ReturnType<typeof vi.fn>;
}

/** 装上 navigator.clipboard（write 和 writeText）和 ClipboardItem；withItem 为 false 时模拟没有 ClipboardItem 的旧浏览器 */
export function installClipboard(overrides: Partial<ClipboardStubs> = {}, withItem = true): ClipboardStubs {
  const stubs: ClipboardStubs = {
    write: vi.fn().mockResolvedValue(undefined),
    writeText: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  Object.defineProperty(navigator, "clipboard", { value: stubs, configurable: true, writable: true });
  if (withItem) vi.stubGlobal("ClipboardItem", FakeClipboardItem);
  else vi.stubGlobal("ClipboardItem", undefined);
  return stubs;
}

export function uninstallClipboard() {
  Reflect.deleteProperty(navigator, "clipboard");
  vi.unstubAllGlobals();
}

/** 读出 write() 收到的那个 ClipboardItem 里的文本（等它的 Promise 兑现） */
export async function writtenText(write: ClipboardStubs["write"], call = 0): Promise<string> {
  const [items] = write.mock.calls[call] as [FakeClipboardItem[]];
  const blob = await items[0].items["text/plain"];
  return await blob.text();
}
