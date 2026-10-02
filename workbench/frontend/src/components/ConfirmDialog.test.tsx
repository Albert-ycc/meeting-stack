import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, ApiTimeoutError } from "../api";
import { useConfirm } from "./ConfirmDialog";
import { useDialogEscape, useDialogFocus } from "./useDialog";

function Harness({ onResult }: { onResult: (confirmed: boolean) => void }) {
  const [confirm, dialog] = useConfirm();
  const [count, setCount] = useState(0);
  return (
    <div>
      <button
        onClick={async () => {
          onResult(await confirm({ title: "丢弃草稿？", message: "丢弃后无法恢复。", confirmLabel: "丢弃", tone: "danger" }));
          setCount((value) => value + 1);
        }}
        type="button"
      >
        打开确认
      </button>
      <span>{count}</span>
      {dialog}
    </div>
  );
}

describe("ConfirmDialog", () => {
  it("确认返回 true，关闭后焦点回到打开它的按钮", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    const opener = screen.getByRole("button", { name: "打开确认" });
    await userEvent.click(opener);

    const dialog = screen.getByRole("alertdialog", { name: "丢弃草稿？" });
    expect(dialog).toHaveTextContent("丢弃后无法恢复。");
    expect(document.body.style.overflow).toBe("hidden");
    await userEvent.click(screen.getByRole("button", { name: "丢弃" }));

    await waitFor(() => expect(onResult).toHaveBeenCalledWith(true));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(document.body.style.overflow).toBe("");
  });

  describe("默认焦点", () => {
    function Variants({ onResult }: { onResult: (confirmed: boolean) => void }) {
      const [confirm, dialog] = useConfirm();
      return (
        <div>
          <button onClick={async () => onResult(await confirm({ title: "丢弃草稿？", confirmLabel: "丢弃", tone: "danger" }))} type="button">
            危险
          </button>
          <button
            onClick={async () =>
              onResult(await confirm({ title: "放弃修改吗？", confirmLabel: "放弃修改", cancelLabel: "继续编辑", tone: "danger" }))
            }
            type="button"
          >
            危险改了按钮字
          </button>
          <button onClick={async () => onResult(await confirm({ title: "写回会议文件夹？", confirmLabel: "写回" }))} type="button">
            普通
          </button>
          <button
            onClick={async () =>
              onResult(await confirm({ title: "移除根目录？", confirmLabel: "移除", tone: "danger", action: () => Promise.resolve() }))
            }
            type="button"
          >
            危险带动作
          </button>
          {dialog}
        </div>
      );
    }

    it("危险操作默认聚焦「取消」：弹出后直接回车不会丢弃", async () => {
      const onResult = vi.fn();
      render(<Variants onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "危险" }));

      await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toHaveFocus());
      await userEvent.keyboard("{Enter}");

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
      expect(onResult).not.toHaveBeenCalledWith(true);
    });

    it("Tab 到确认键再回车照常能用", async () => {
      const onResult = vi.fn();
      render(<Variants onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "危险" }));
      await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toHaveFocus());

      await userEvent.tab();
      expect(screen.getByRole("button", { name: "丢弃" })).toHaveFocus();
      await userEvent.keyboard("{Enter}");

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(true));
    });

    it("取消键改了字（「继续编辑」）也一样聚焦它", async () => {
      render(<Variants onResult={vi.fn()} />);
      await userEvent.click(screen.getByRole("button", { name: "危险改了按钮字" }));

      await waitFor(() => expect(screen.getByRole("button", { name: "继续编辑" })).toHaveFocus());
    });

    it("给了 action 的危险确认也一样：先落在取消上，回车只是取消", async () => {
      const onResult = vi.fn();
      render(<Variants onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "危险带动作" }));

      await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toHaveFocus());
      await userEvent.keyboard("{Enter}");

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
    });

    it("不危险的确认仍然默认聚焦确认键，回车就是确认", async () => {
      const onResult = vi.fn();
      render(<Variants onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "普通" }));

      await waitFor(() => expect(screen.getByRole("button", { name: "写回" })).toHaveFocus());
      await userEvent.keyboard("{Enter}");

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(true));
    });
  });

  describe("带 action 的确认框：写请求超时以后不再给「再执行一次」", () => {
    const TIMEOUT_TEXT = "服务没有响应，可能仍在处理，稍后刷新确认";

    function ActionHarness({ action, onResult }: { action: () => Promise<unknown>; onResult: (confirmed: boolean) => void }) {
      const [confirm, dialog] = useConfirm();
      return (
        <div>
          <button
            onClick={async () => onResult(await confirm({ title: "移除根目录？", confirmLabel: "移除", tone: "danger", action }))}
            type="button"
          >
            打开确认
          </button>
          {dialog}
        </div>
      );
    }

    it("超时：写出超时提示，确认键换成「关闭」、焦点落在上面，不能再执行；action 只跑过一次，点「关闭」按没确认处理", async () => {
      const action = vi.fn().mockRejectedValue(new ApiTimeoutError(TIMEOUT_TEXT));
      const onResult = vi.fn();
      render(<ActionHarness action={action} onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "打开确认" }));
      await userEvent.click(await screen.findByRole("button", { name: "移除" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(TIMEOUT_TEXT);
      const dialog = screen.getByRole("alertdialog");
      expect(within(dialog).queryByRole("button", { name: "移除" })).not.toBeInTheDocument();
      expect(within(dialog).queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
      // 右上角的 ✕ 也叫「关闭」：脚注里的那个按钮文字是「关闭」
      const close = within(dialog).getAllByRole("button", { name: "关闭" }).find((button) => button.textContent === "关闭")!;
      expect(close).toBeInTheDocument();
      await waitFor(() => expect(close).toHaveFocus());
      expect(action).toHaveBeenCalledTimes(1);

      await userEvent.keyboard("{Enter}");

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
      expect(onResult).toHaveBeenCalledTimes(1);
      expect(action).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });

    it("超时以后 Esc 和右上角的 ✕ 照常能关", async () => {
      const onResult = vi.fn();
      render(<ActionHarness action={() => Promise.reject(new ApiTimeoutError(TIMEOUT_TEXT))} onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "打开确认" }));
      await userEvent.click(await screen.findByRole("button", { name: "移除" }));
      await screen.findByRole("alert");

      fireEvent.keyDown(window, { key: "Escape" });

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
    });

    it("别的错误照旧：确认键还在、能再点一次重试，成功了才关并返回 true", async () => {
      const action = vi
        .fn()
        .mockRejectedValueOnce(new ApiError("这个目录正在被占用", 409))
        .mockRejectedValueOnce(new Error("断网了"))
        .mockResolvedValue({});
      const onResult = vi.fn();
      render(<ActionHarness action={action} onResult={onResult} />);
      await userEvent.click(screen.getByRole("button", { name: "打开确认" }));

      await userEvent.click(await screen.findByRole("button", { name: "移除" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("这个目录正在被占用");
      expect(screen.getByRole("button", { name: "移除" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "移除" }));
      await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("断网了"));
      expect(screen.getByRole("button", { name: "移除" })).toBeEnabled();

      await userEvent.click(screen.getByRole("button", { name: "移除" }));
      await waitFor(() => expect(onResult).toHaveBeenCalledWith(true));
      expect(action).toHaveBeenCalledTimes(3);
    });
  });

  it("Esc 取消；输入法组合中的 Esc 不算", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    await userEvent.click(screen.getByRole("button", { name: "打开确认" }));
    const dialog = screen.getByRole("alertdialog");

    fireEvent.keyDown(dialog, { key: "Escape", isComposing: true });
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
  });

  describe("盖在别的弹窗上", () => {
    function Underlay({ onEscape }: { onEscape: () => void }) {
      const ref = useRef<HTMLDivElement>(null);
      useDialogFocus(ref);
      useDialogEscape(ref, onEscape);
      return <div aria-label="下面的弹窗" ref={ref} role="dialog" />;
    }
    function Page({ onResult, onUnderEscape }: { onResult: (confirmed: boolean) => void; onUnderEscape: () => void }) {
      const [confirm, dialog] = useConfirm();
      return (
        <div>
          <Underlay onEscape={onUnderEscape} />
          <button onClick={async () => onResult(await confirm({ title: "丢弃草稿？", confirmLabel: "丢弃", tone: "danger" }))} type="button">
            打开确认
          </button>
          {dialog}
        </div>
      );
    }

    it("Esc 只取消确认框，下面的弹窗不关；确认框没了再按才轮到它", async () => {
      const onResult = vi.fn();
      const onUnderEscape = vi.fn();
      render(<Page onResult={onResult} onUnderEscape={onUnderEscape} />);
      await userEvent.click(screen.getByRole("button", { name: "打开确认" }));
      await screen.findByRole("alertdialog");

      fireEvent.keyDown(window, { key: "Escape" });
      await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
      expect(onUnderEscape).not.toHaveBeenCalled();

      fireEvent.keyDown(window, { key: "Escape" });
      expect(onUnderEscape).toHaveBeenCalledTimes(1);
    });

    it("焦点不在确认框里（落在页面上）Esc 也取消确认框", async () => {
      const onResult = vi.fn();
      render(<Page onResult={onResult} onUnderEscape={vi.fn()} />);
      await userEvent.click(screen.getByRole("button", { name: "打开确认" }));
      await screen.findByRole("alertdialog");

      fireEvent.keyDown(document.body, { key: "Escape" });

      await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
    });
  });

  it("在弹窗里按下、到背景上松开不算点背景", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    await userEvent.click(screen.getByRole("button", { name: "打开确认" }));
    const overlay = screen.getByRole("alertdialog").parentElement!;

    act(() => {
      fireEvent.mouseDown(screen.getByRole("alertdialog"));
      fireEvent.click(overlay);
    });
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();

    act(() => {
      fireEvent.mouseDown(overlay);
      fireEvent.click(overlay);
    });
    await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
  });
});
