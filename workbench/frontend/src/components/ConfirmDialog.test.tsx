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
