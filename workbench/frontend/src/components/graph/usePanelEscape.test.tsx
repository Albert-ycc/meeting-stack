import { act, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePanelEscape } from "./usePanelEscape";

function Host({ onEscape, children }: { onEscape: (() => void) | null; children?: ReactNode }) {
  usePanelEscape(onEscape);
  return (
    <div>
      <input aria-label="输入框" />
      <textarea aria-label="文本域" />
      <button type="button">按钮</button>
      <span onKeyDown={(event) => event.preventDefault()}>
        <button type="button">自己处理 Esc 的按钮</button>
      </span>
      {children}
    </div>
  );
}

const esc = (target: Element | Document = document.body, init: KeyboardEventInit = {}) =>
  fireEvent.keyDown(target, { key: "Escape", ...init });

afterEach(() => {
  vi.restoreAllMocks();
});

describe("usePanelEscape", () => {
  it("没有面板（null）时不听；面板开了才听，收起后又不听", () => {
    const onEscape = vi.fn();
    const view = render(<Host onEscape={null} />);
    esc();
    expect(onEscape).not.toHaveBeenCalled();

    view.rerender(<Host onEscape={onEscape} />);
    esc();
    expect(onEscape).toHaveBeenCalledTimes(1);

    view.rerender(<Host onEscape={null} />);
    esc();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("焦点在 body 上、在按钮上，Esc 都关面板（只调一次）", () => {
    const onEscape = vi.fn();
    render(<Host onEscape={onEscape} />);
    expect(document.activeElement).toBe(document.body);

    esc(document.body);
    expect(onEscape).toHaveBeenCalledTimes(1);
    esc(screen.getByRole("button", { name: "按钮" }));
    expect(onEscape).toHaveBeenCalledTimes(2);
  });

  it("别的键不算", () => {
    const onEscape = vi.fn();
    render(<Host onEscape={onEscape} />);
    fireEvent.keyDown(document.body, { key: "Enter" });
    fireEvent.keyDown(document.body, { key: "Tab" });
    expect(onEscape).not.toHaveBeenCalled();
  });

  it("输入框、文本域里的 Esc 是它们自己的事，不关面板", () => {
    const onEscape = vi.fn();
    render(<Host onEscape={onEscape} />);
    esc(screen.getByRole("textbox", { name: "输入框" }));
    esc(screen.getByRole("textbox", { name: "文本域" }));
    expect(onEscape).not.toHaveBeenCalled();
  });

  it("输入法组合中的 Esc 只取消候选词（isComposing、Safari 里组合结束那一下的 keyCode 229）", () => {
    const onEscape = vi.fn();
    render(<Host onEscape={onEscape} />);
    esc(document.body, { isComposing: true });
    esc(document.body, { keyCode: 229 });
    expect(onEscape).not.toHaveBeenCalled();
  });

  it("焦点所在的控件已经处理了这次 Esc（preventDefault）：不再关面板", () => {
    const onEscape = vi.fn();
    render(<Host onEscape={onEscape} />);
    esc(screen.getByRole("button", { name: "自己处理 Esc 的按钮" }));
    expect(onEscape).not.toHaveBeenCalled();
  });

  it("面板上盖着弹窗或浮层时 Esc 归它们；盖着的东西没了才轮到面板", () => {
    const onEscape = vi.fn();
    const view = render(
      <Host onEscape={onEscape}>
        <div aria-label="取径器" role="dialog" />
      </Host>,
    );
    esc();
    expect(onEscape).not.toHaveBeenCalled();

    view.rerender(
      <Host onEscape={onEscape}>
        <div aria-label="确认" role="alertdialog" />
      </Host>,
    );
    esc();
    expect(onEscape).not.toHaveBeenCalled();

    view.rerender(<Host onEscape={onEscape} />);
    esc();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("同一次按键里弹窗已经被它自己的监听关掉了（它的监听排在面板前面、关完立刻重画）：面板不跟着关", () => {
    function Dialog({ onClosed }: { onClosed: () => void }) {
      const [open, setOpen] = useState(true);
      useEffect(() => {
        const onKeyDown = (event: KeyboardEvent) => {
          if (event.key !== "Escape") return;
          flushSync(() => setOpen(false));
          onClosed();
        };
        window.addEventListener("keydown", onKeyDown);
        return () => window.removeEventListener("keydown", onKeyDown);
      }, [onClosed]);
      return open ? <div aria-label="先开的弹窗" role="dialog" /> : null;
    }
    const onEscape = vi.fn();
    const onClosed = vi.fn();
    // 弹窗先挂上（监听先登记），面板后开
    function Page({ panel }: { panel: boolean }) {
      return (
        <>
          <Dialog onClosed={onClosed} />
          <Host onEscape={panel ? onEscape : null} />
        </>
      );
    }
    const view = render(<Page panel={false} />);
    view.rerender(<Page panel />);
    expect(screen.getByRole("dialog", { name: "先开的弹窗" })).toBeInTheDocument();

    act(() => {
      esc();
    });

    expect(onClosed).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onEscape).not.toHaveBeenCalled();

    // 弹窗没了，下一次 Esc 关面板
    act(() => {
      esc();
    });
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("每次渲染换成最新的回调，不重复登记监听；卸载后不再听", () => {
    const first = vi.fn();
    const second = vi.fn();
    const add = vi.spyOn(window, "addEventListener");
    const view = render(<Host onEscape={first} />);
    const registered = add.mock.calls.filter(([type]) => type === "keydown").length;

    view.rerender(<Host onEscape={second} />);
    expect(add.mock.calls.filter(([type]) => type === "keydown").length).toBe(registered);

    esc();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);

    view.unmount();
    esc();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
