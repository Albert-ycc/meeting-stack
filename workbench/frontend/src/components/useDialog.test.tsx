import { fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { describe, expect, it, vi } from "vitest";

import { isTopDialog, useDialogEscape, useDialogFocus } from "./useDialog";

function Dialog({ name, onEscape, children }: { name: string; onEscape: () => void; children?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useDialogFocus(ref);
  useDialogEscape(ref, onEscape);
  return (
    <div aria-label={name} ref={ref} role="dialog">
      {children}
    </div>
  );
}

const esc = (target: Window | Document | Element = window, init: KeyboardEventInit = {}) =>
  fireEvent.keyDown(target, { key: "Escape", ...init });

/**
 * 里层弹窗由另一个组件开着（自己的状态），作为外层弹窗的子节点，外层弹窗并不知道它开没开。
 * 里层的 Esc 里用 flushSync 当场把自己卸掉：浏览器在两个监听之间会把 React 的渲染刷出来，
 * 这里用它模拟「上层刚退出栈、下层的监听才轮到」；这一下只渲染里层，外层不会跟着重新渲染、顺手换掉自己的监听。
 */
function InnerHost({ onEscape }: { onEscape: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        开里层
      </button>
      {open && (
        <Dialog
          name="里层"
          onEscape={() => {
            onEscape();
            flushSync(() => setOpen(false));
          }}
        />
      )}
    </>
  );
}

function Stack({ outerEscape, innerEscape, tick = 0 }: { outerEscape: (tick: number) => void; innerEscape: () => void; tick?: number }) {
  return (
    // 每次渲染都换一个新的内联函数
    <Dialog name="外层" onEscape={() => outerEscape(tick)}>
      <InnerHost onEscape={innerEscape} />
    </Dialog>
  );
}

describe("useDialogEscape", () => {
  it("单个弹窗：Esc 调一次 onEscape", () => {
    const onEscape = vi.fn();
    render(<Dialog name="a" onEscape={onEscape} />);

    esc();

    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("叠了两层：只有最上面那层响应；它关掉以后再按，才轮到下面那层", () => {
    const outerEscape = vi.fn();
    const innerEscape = vi.fn();
    render(<Stack innerEscape={innerEscape} outerEscape={outerEscape} />);
    fireEvent.click(screen.getByRole("button", { name: "开里层" }));
    expect(screen.getByRole("dialog", { name: "里层" })).toBeInTheDocument();

    esc();
    expect(innerEscape).toHaveBeenCalledTimes(1);
    expect(outerEscape).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "里层" })).not.toBeInTheDocument();

    esc();
    expect(outerEscape).toHaveBeenCalledTimes(1);
    expect(innerEscape).toHaveBeenCalledTimes(1);
  });

  it("外层每次渲染都传新的 onEscape：不会因此重新登记、排到里层后面，同一次按键里连外层一起关", () => {
    const outerEscape = vi.fn();
    const innerEscape = vi.fn();
    const view = render(<Stack innerEscape={innerEscape} outerEscape={outerEscape} tick={0} />);
    fireEvent.click(screen.getByRole("button", { name: "开里层" }));
    // 里层开着的时候外层又渲染了几遍
    view.rerender(<Stack innerEscape={innerEscape} outerEscape={outerEscape} tick={1} />);
    view.rerender(<Stack innerEscape={innerEscape} outerEscape={outerEscape} tick={2} />);

    esc();
    expect(innerEscape).toHaveBeenCalledTimes(1);
    expect(outerEscape).not.toHaveBeenCalled();

    // 之后轮到外层时调的是它最新一次渲染传进来的
    esc();
    expect(outerEscape).toHaveBeenCalledTimes(1);
    expect(outerEscape).toHaveBeenLastCalledWith(2);
  });

  it("输入法组合中的 Esc 只取消候选词，不关弹窗（isComposing、Safari 的 keyCode 229）", () => {
    const onEscape = vi.fn();
    render(<Dialog name="a" onEscape={onEscape} />);

    esc(window, { isComposing: true });
    esc(window, { keyCode: 229 });
    expect(onEscape).not.toHaveBeenCalled();

    esc();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("别的键不管", () => {
    const onEscape = vi.fn();
    render(<Dialog name="a" onEscape={onEscape} />);

    fireEvent.keyDown(window, { key: "Enter" });
    fireEvent.keyDown(window, { key: "Tab" });

    expect(onEscape).not.toHaveBeenCalled();
  });

  it("焦点在页面上哪里都行：事件从任意元素冒上来、直接落在 document 上都算", () => {
    const onEscape = vi.fn();
    render(
      <>
        <button type="button">页面上的按钮</button>
        <Dialog name="a" onEscape={onEscape} />
      </>,
    );

    esc(screen.getByRole("button", { name: "页面上的按钮" }));
    esc(document);
    esc(document.body);

    expect(onEscape).toHaveBeenCalledTimes(3);
  });

  it("弹窗里的下拉自己拦下了 Esc（React 里 stopPropagation）：先收下拉，弹窗不关；下一次才关", () => {
    const onEscape = vi.fn();
    render(
      <Dialog name="a" onEscape={onEscape}>
        <button
          onKeyDown={(event) => {
            if (event.key === "Escape") event.stopPropagation();
          }}
          type="button"
        >
          下拉
        </button>
      </Dialog>,
    );

    esc(screen.getByRole("button", { name: "下拉" }));
    expect(onEscape).not.toHaveBeenCalled();

    esc();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("卸载以后不再响应，也不再算栈里的弹窗", () => {
    const onEscape = vi.fn();
    let root: HTMLElement | null = null;
    function Probe() {
      const ref = useRef<HTMLDivElement>(null);
      useDialogFocus(ref);
      useDialogEscape(ref, onEscape);
      return (
        <div
          ref={(node) => {
            ref.current = node;
            root = node;
          }}
          role="dialog"
        />
      );
    }
    const view = render(<Probe />);
    expect(isTopDialog(root)).toBe(true);

    view.unmount();
    esc();

    expect(onEscape).not.toHaveBeenCalled();
    expect(isTopDialog(null)).toBe(false);
  });
});
