import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { useConfirm } from "./ConfirmDialog";

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
