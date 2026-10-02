import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useMutex } from "./useMutex";

/** 一个手动放行的异步动作：模拟还在路上的写请求 */
function pending() {
  let finish: () => void = () => undefined;
  let fail: (error: unknown) => void = () => undefined;
  const promise = new Promise<void>((resolve, reject) => {
    finish = resolve;
    fail = reject;
  });
  return { promise, finish: () => finish(), fail: (error: unknown) => fail(error) };
}

const flush = () => act(async () => undefined);

describe("useMutex", () => {
  it("run：跑完整个动作，期间 busy 为真，结束后放开", async () => {
    const { result } = renderHook(() => useMutex(vi.fn()));
    const gate = pending();

    let done: Promise<void> = Promise.resolve();
    act(() => {
      done = result.current.run(() => gate.promise);
    });
    expect(result.current.busy).toBe(true);

    await act(async () => {
      gate.finish();
      await done;
    });
    expect(result.current.busy).toBe(false);
  });

  it("手头有一次在跑，新来的 run 直接丢掉，不发第二个写请求（双击同帧也一样）", async () => {
    const { result } = renderHook(() => useMutex(vi.fn()));
    const gate = pending();
    const first = vi.fn(() => gate.promise);
    const second = vi.fn(async () => undefined);

    act(() => {
      void result.current.run(first);
      void result.current.run(second); // 同一帧里连点第二下，state 还没刷新，只有 ref 拦得住
    });
    await flush();

    expect(first).toHaveBeenCalledTimes(1);
    expect(second).not.toHaveBeenCalled();

    await act(async () => gate.finish());
    // 放开以后能再跑
    const third = vi.fn(async () => undefined);
    await act(async () => result.current.run(third));
    expect(third).toHaveBeenCalledTimes(1);
  });

  it("动作抛错：原因交给 onError，互斥放开，下一次照常跑", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() => useMutex(onError));

    await act(async () =>
      result.current.run(async () => {
        throw new Error("需求「京东科研仓对接」已搁置，只能挂到进行中的需求");
      }),
    );

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith("需求「京东科研仓对接」已搁置，只能挂到进行中的需求");
    expect(result.current.busy).toBe(false);

    const next = vi.fn(async () => undefined);
    await act(async () => result.current.run(next));
    expect(next).toHaveBeenCalledTimes(1);
  });

  it("抛出来的不是 Error（字符串、对象）：用兜底文案，不把 [object Object] 摆上屏", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() => useMutex(onError));

    await act(async () =>
      result.current.run(async () => {
        throw { detail: "x" };
      }),
    );

    expect(onError).toHaveBeenCalledWith("操作失败，请稍后重试");
  });

  it("onError 用最新一次渲染传进来的：旧提示条上的闭包晚点调 run，报错也进现在的提示条", async () => {
    const stale = vi.fn();
    const fresh = vi.fn();
    const { result, rerender } = renderHook(({ onError }) => useMutex(onError), { initialProps: { onError: stale } });
    const run = result.current.run; // 旧闭包拿着的

    rerender({ onError: fresh });
    await act(async () =>
      run(async () => {
        throw new Error("没成");
      }),
    );

    expect(fresh).toHaveBeenCalledWith("没成");
    expect(stale).not.toHaveBeenCalled();
  });

  it("run 和 runAfterCurrent 的引用不随渲染、也不随 busy 变：放进旧闭包、依赖数组都安全", async () => {
    const { result, rerender } = renderHook(() => useMutex(vi.fn()));
    const { run, runAfterCurrent } = result.current;
    const gate = pending();

    rerender();
    act(() => {
      void result.current.run(() => gate.promise);
    });
    expect(result.current.busy).toBe(true); // 在 busy 为真的这一帧渲染过
    expect(result.current.run).toBe(run);
    expect(result.current.runAfterCurrent).toBe(runAfterCurrent);

    await act(async () => gate.finish());
    expect(result.current.busy).toBe(false);
    expect(result.current.run).toBe(run);
    expect(result.current.runAfterCurrent).toBe(runAfterCurrent);
  });

  describe("runAfterCurrent", () => {
    it("没人在跑：当场跑", async () => {
      const { result } = renderHook(() => useMutex(vi.fn()));
      const action = vi.fn(async () => undefined);

      await act(async () => result.current.runAfterCurrent(action));

      expect(action).toHaveBeenCalledTimes(1);
    });

    it("手头有一次在跑：等它做完再跑，不被互斥吞掉（提示条上的［撤销］比写操作的重新取数先出来）", async () => {
      const { result } = renderHook(() => useMutex(vi.fn()));
      const gate = pending();
      const order: string[] = [];

      act(() => {
        void result.current.run(async () => {
          await gate.promise;
          order.push("写操作做完");
        });
      });
      let undone: Promise<void> = Promise.resolve();
      act(() => {
        undone = result.current.runAfterCurrent(async () => {
          order.push("撤销");
        });
      });
      await flush();
      expect(order).toEqual([]); // 还在等

      await act(async () => {
        gate.finish();
        await undone;
      });
      expect(order).toEqual(["写操作做完", "撤销"]);
      expect(result.current.busy).toBe(false);
    });

    it("一起在等的不止一个：按先后一个一个接手，前一个做完才轮到下一个，谁也不被丢掉", async () => {
      const { result } = renderHook(() => useMutex(vi.fn()));
      const first = pending();
      const jia = pending();
      const order: string[] = [];

      act(() => {
        void result.current.run(() => first.promise);
      });
      let both: Promise<unknown> = Promise.resolve();
      act(() => {
        both = Promise.all([
          result.current.runAfterCurrent(async () => {
            order.push("甲开始");
            await jia.promise;
            order.push("甲做完");
          }),
          result.current.runAfterCurrent(async () => void order.push("乙")),
        ]);
      });

      await act(async () => first.finish());
      expect(order).toEqual(["甲开始"]); // 乙还得等甲

      await act(async () => {
        jia.finish();
        await both;
      });
      expect(order).toEqual(["甲开始", "甲做完", "乙"]);
      expect(result.current.busy).toBe(false);
    });

    it("接手以后、做完之前，别的写操作照样进不来：互斥在交接时没有空出来", async () => {
      const { result } = renderHook(() => useMutex(vi.fn()));
      const first = pending();
      const undo = pending();
      const sneaked = vi.fn(async () => undefined);

      act(() => {
        void result.current.run(() => first.promise);
      });
      let undone: Promise<void> = Promise.resolve();
      act(() => {
        undone = result.current.runAfterCurrent(() => undo.promise);
      });

      await act(async () => first.finish());
      expect(result.current.busy).toBe(true); // 撤销接手了，没有中间空出一帧
      await act(async () => result.current.run(sneaked)); // 这时候点别的写操作
      expect(sneaked).not.toHaveBeenCalled();

      await act(async () => {
        undo.finish();
        await undone;
      });
      expect(result.current.busy).toBe(false);
    });

    it("等着的这次失败了：原因照样交给 onError", async () => {
      const onError = vi.fn();
      const { result } = renderHook(() => useMutex(onError));
      const gate = pending();

      act(() => {
        void result.current.run(() => gate.promise);
      });
      let undone: Promise<void> = Promise.resolve();
      act(() => {
        undone = result.current.runAfterCurrent(async () => {
          throw new Error("已经过了 10 分钟，没法撤销了");
        });
      });
      await act(async () => {
        gate.finish();
        await undone;
      });

      expect(onError).toHaveBeenCalledWith("已经过了 10 分钟，没法撤销了");
    });

    it("手头那次失败了也一样放开：等着的照样跑", async () => {
      const onError = vi.fn();
      const { result } = renderHook(() => useMutex(onError));
      const gate = pending();
      const action = vi.fn(async () => undefined);

      act(() => {
        void result.current.run(() => gate.promise);
      });
      let undone: Promise<void> = Promise.resolve();
      act(() => {
        undone = result.current.runAfterCurrent(action);
      });
      await act(async () => {
        gate.fail(new Error("写失败"));
        await undone;
      });

      expect(onError).toHaveBeenCalledWith("写失败");
      expect(action).toHaveBeenCalledTimes(1);
    });
  });
});
