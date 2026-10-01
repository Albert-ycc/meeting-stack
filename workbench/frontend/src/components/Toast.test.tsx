import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useToast } from "./Toast";

type ShowOptions = Parameters<ReturnType<typeof useToast>["showToast"]>[1];

/** 用例里的页面：一个按钮弹一条提示，toastNode 渲染在页面里，和真实页面的用法一样 */
function Page({
  durationMs,
  message = "已复制",
  options,
  onReady,
}: {
  durationMs?: number;
  message?: string;
  options?: ShowOptions;
  onReady?: (showToast: ReturnType<typeof useToast>["showToast"]) => void;
}) {
  const { toastNode, showToast } = useToast(durationMs);
  onReady?.(showToast);
  return (
    <div>
      <button onClick={() => showToast(message, options)} type="button">
        弹提示
      </button>
      {toastNode}
    </div>
  );
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useToast", () => {
  it("只说一句结果：没有撤销，默认停 2.4 秒；hook 可以改默认时长", () => {
    const { unmount } = render(<Page />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));

    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("已复制");
    expect(within(toast).queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
    advance(2399);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(2);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    unmount();

    render(<Page durationMs={3000} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));
    advance(2900);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(200);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("带 onUndo：右边有「撤销」，停 10 秒，不跟 hook 的默认时长走", () => {
    render(<Page durationMs={2400} options={{ onUndo: vi.fn() }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));

    expect(within(screen.getByRole("status")).getByRole("button", { name: "撤销" })).toBeInTheDocument();
    advance(9_900);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(200);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("单条自带的 durationMs 优先：不带撤销的可以停更久，带撤销的也可以更短", () => {
    const { rerender } = render(<Page options={{ durationMs: 6_000 }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));
    advance(5_900);
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(200);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    rerender(<Page options={{ onUndo: vi.fn(), durationMs: 1_000 }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));
    advance(1_100);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("点「撤销」：提示先收起，再调 onUndo；onUndo 里弹的结果提示不会被收起动作抹掉", () => {
    let show: ReturnType<typeof useToast>["showToast"] = () => undefined;
    const onUndo = vi.fn(() => show("已撤销合并"));
    render(<Page message="已合并到「京东科研仓对接」" onReady={(fn) => (show = fn)} options={{ onUndo }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));

    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));

    expect(onUndo).toHaveBeenCalledTimes(1);
    // 页面上留下的是 onUndo 自己弹的那一条（不带撤销），原来那条已经收起了
    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("已撤销合并");
    expect(toast).not.toHaveTextContent("已合并到");
    expect(within(toast).queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
    // 它自己的 2.4 秒到了照常收起
    advance(2_500);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("撤销是异步的：条在点下去的当下就收起，不等它做完；原来那条的 10 秒计时也一并取消", async () => {
    let finish: () => void = () => undefined;
    const onUndo = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    render(<Page options={{ onUndo }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));

    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(onUndo).toHaveBeenCalledTimes(1);

    // 过了原来的 10 秒也没有别的动静
    advance(11_000);
    await act(async () => finish());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it("新提示顶掉旧提示并重新计时，旧提示的计时不会把新的提前收掉", () => {
    render(<Page />);
    const button = screen.getByRole("button", { name: "弹提示" });
    fireEvent.click(button);
    advance(2_000);
    fireEvent.click(button);
    advance(1_000); // 离第一次已经 3 秒，第一次的 2.4 秒早过了
    expect(screen.getByRole("status")).toBeInTheDocument();
    advance(1_500);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("提示条能点：不挡鼠标事件（原来是 pointer-events: none，撤销点不到）", () => {
    render(<Page options={{ onUndo: vi.fn() }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));

    const toast = screen.getByRole("status");
    expect(getComputedStyle(toast).pointerEvents).not.toBe("none");
    expect(getComputedStyle(within(toast).getByRole("button", { name: "撤销" })).pointerEvents).not.toBe("none");
  });

  it("页面卸载后计时器清掉，不会在卸载后改状态", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { unmount } = render(<Page options={{ onUndo: vi.fn() }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));
    unmount();
    advance(15_000);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });
  it("tone: error：换成叹号和 alert，读屏立即播报；默认停 10 秒，不跟 hook 的 2.4 秒走", () => {
    render(<Page message="确认失败：任务已经是「已完成」，不能改成「已确认」，刷新后再看" options={{ tone: "error" }} />);
    fireEvent.click(screen.getByRole("button", { name: "弹提示" }));

    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass("app-toast--error");
    expect(alert).toHaveTextContent("确认失败");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    advance(9_900);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    advance(200);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
