import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

// ---------------------------------------------------------------- Tab 由弹窗自己接管

function Modal({ name = "弹窗", children }: { name?: string; children?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useDialogFocus(ref);
  return (
    <div aria-label={name} ref={ref} role="dialog">
      {children}
    </div>
  );
}

/** 只派发 keydown：不会像浏览器那样跟着移动焦点，所以焦点动没动全看弹窗自己 */
const tab = (target: Element, init: KeyboardEventInit = {}) => fireEvent.keyDown(target, { key: "Tab", ...init });

const button = (name: string) => screen.getByRole("button", { name });

describe("useDialogFocus 接管弹窗里的 Tab", () => {
  it("落在中间的按钮上按 Tab / Shift+Tab：不靠浏览器原生的 Tab，焦点由弹窗移到下一个 / 上一个（Safari 默认 Tab 不停在按钮上）", () => {
    render(
      <Modal>
        <button type="button">一</button>
        <button type="button">二</button>
        <button type="button">三</button>
      </Modal>,
    );
    button("二").focus();

    expect(tab(button("二"))).toBe(false); // 默认动作被拦下了：交给浏览器就走偏了
    expect(button("三")).toHaveFocus();

    button("二").focus();
    expect(tab(button("二"), { shiftKey: true })).toBe(false);
    expect(button("一")).toHaveFocus();
  });

  it("首尾循环：最后一个再按 Tab 回到第一个，第一个按 Shift+Tab 到最后一个", () => {
    render(
      <Modal>
        <button type="button">一</button>
        <button type="button">二</button>
      </Modal>,
    );
    button("二").focus();
    tab(button("二"));
    expect(button("一")).toHaveFocus();

    tab(button("一"), { shiftKey: true });
    expect(button("二")).toHaveFocus();
  });

  it("输入框、文本域、下拉、复选框、按钮按文档顺序走一整圈，回到起点", () => {
    render(
      <Modal>
        <input aria-label="名称" type="text" />
        <textarea aria-label="描述" />
        <select aria-label="项目">
          <option>甲</option>
        </select>
        <input aria-label="含已完成" type="checkbox" />
        <a href="#x">链接</a>
        <button type="button">确定</button>
      </Modal>,
    );
    const order = ["名称", "描述", "项目", "含已完成", "链接", "确定"];
    const labelOf = (el: Element | null) => el?.getAttribute("aria-label") ?? el?.textContent;
    (screen.getByLabelText("名称") as HTMLElement).focus();

    const seen: Array<string | null | undefined> = [labelOf(document.activeElement)];
    for (let i = 0; i < order.length; i += 1) {
      tab(document.activeElement!);
      seen.push(labelOf(document.activeElement));
    }
    expect(seen).toEqual([...order, "名称"]);

    // 反着走一圈
    const back: Array<string | null | undefined> = [];
    for (let i = 0; i < order.length; i += 1) {
      tab(document.activeElement!, { shiftKey: true });
      back.push(labelOf(document.activeElement));
    }
    expect(back).toEqual(["确定", "链接", "含已完成", "项目", "描述", "名称"]);
  });

  it("单选组和浏览器一样只算一个停靠点：选中的那个；没有选中的，Tab 进来是头一个、Shift+Tab 进来是最后一个", async () => {
    render(
      <Modal>
        <button type="button">前</button>
        <input aria-label="甲" name="g" type="radio" />
        <input aria-label="乙" defaultChecked name="g" type="radio" />
        <input aria-label="丙" name="g" type="radio" />
        <input aria-label="空一" name="empty" type="radio" />
        <input aria-label="空二" name="empty" type="radio" />
        <button type="button">后</button>
      </Modal>,
    );
    button("前").focus();
    tab(button("前"));
    expect(screen.getByLabelText("乙")).toHaveFocus(); // 选中的那个，不是甲
    tab(document.activeElement!);
    expect(screen.getByLabelText("空一")).toHaveFocus(); // 没选中的组：Tab 进来是头一个
    tab(document.activeElement!);
    expect(button("后")).toHaveFocus(); // 空一后面不再停空二

    tab(button("后"), { shiftKey: true });
    expect(screen.getByLabelText("空二")).toHaveFocus(); // Shift+Tab 进来是最后一个
    tab(document.activeElement!, { shiftKey: true });
    expect(screen.getByLabelText("乙")).toHaveFocus();
    tab(document.activeElement!, { shiftKey: true });
    expect(button("前")).toHaveFocus();
    await userEvent.tab(); // 真实的 Tab 序列（user-event 按浏览器规则走）也是同一个停靠点
    expect(screen.getByLabelText("乙")).toHaveFocus();
  });

  it("禁用的、被 inert 的、没法聚焦的（禁用的 fieldset 里）跳过，不卡在原地", () => {
    render(
      <Modal>
        <button type="button">一</button>
        <button disabled type="button">禁用</button>
        <fieldset disabled>
          <button type="button">在禁用的 fieldset 里</button>
        </fieldset>
        <div inert>
          <button type="button">inert 里</button>
        </div>
        <button type="button">二</button>
      </Modal>,
    );
    button("一").focus();

    tab(button("一"));
    expect(button("二")).toHaveFocus();
    tab(button("二"), { shiftKey: true });
    expect(button("一")).toHaveFocus();
  });

  it("焦点在弹窗外：Tab 进第一个，Shift+Tab 进最后一个", () => {
    render(
      <>
        <button type="button">页面上的</button>
        <Modal>
          <button type="button">一</button>
          <button type="button">二</button>
        </Modal>
      </>,
    );
    button("页面上的").focus();
    tab(button("页面上的"));
    expect(button("一")).toHaveFocus();

    button("页面上的").focus();
    tab(button("页面上的"), { shiftKey: true });
    expect(button("二")).toHaveFocus();
  });

  it("焦点在弹窗里一个不算停靠点的元素上（tabindex=-1 的行、弹窗卡片本身）：从它的位置往后 / 往前找最近的", () => {
    render(
      <Modal>
        <button type="button">一</button>
        <div role="option" tabIndex={-1}>
          一行
        </div>
        <button type="button">二</button>
      </Modal>,
    );
    const row = screen.getByRole("option");
    row.focus();
    tab(row);
    expect(button("二")).toHaveFocus();

    row.focus();
    tab(row, { shiftKey: true });
    expect(button("一")).toHaveFocus();

    // 卡片自己（tabindex=-1）拿着焦点：Tab 进第一个，Shift+Tab 进最后一个
    const dialog = screen.getByRole("dialog");
    dialog.tabIndex = -1;
    dialog.focus();
    tab(dialog);
    expect(button("一")).toHaveFocus();
    dialog.focus();
    tab(dialog, { shiftKey: true });
    expect(button("二")).toHaveFocus();
  });

  it("日期输入框：浏览器自己在年月日几格之间走 Tab，不拦；它正好是第一个 / 最后一个时才接管，焦点不漏出弹窗", () => {
    render(
      <Modal>
        <button type="button">前</button>
        <input aria-label="截止" type="date" />
        <button type="button">后</button>
        <input aria-label="结束" type="date" />
      </Modal>,
    );
    const middle = screen.getByLabelText("截止");
    middle.focus();
    expect(tab(middle)).toBe(true); // 没被拦：交给浏览器在小格子里走，走出去也是按文档顺序的下一个
    expect(tab(middle, { shiftKey: true })).toBe(true);

    // 最后一个是日期框：往后走要回到第一个，不能让焦点漏出弹窗
    const last = screen.getByLabelText("结束");
    last.focus();
    expect(tab(last)).toBe(false);
    expect(button("前")).toHaveFocus();
  });

  it("日期输入框交给浏览器走时，走出去后该落的那个停靠点临时挂上 tabindex=0（Safari 默认 Tab 不停在按钮上），下个 tick 摘掉", async () => {
    render(
      <Modal>
        <button type="button">前</button>
        <input aria-label="截止" type="date" />
        <button type="button">后</button>
      </Modal>,
    );
    const date = screen.getByLabelText("截止");
    date.focus();

    tab(date);
    expect(button("后")).toHaveAttribute("tabindex", "0");
    expect(button("前")).not.toHaveAttribute("tabindex");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(button("后")).not.toHaveAttribute("tabindex");

    tab(date, { shiftKey: true });
    expect(button("前")).toHaveAttribute("tabindex", "0");
    expect(button("后")).not.toHaveAttribute("tabindex");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(button("前")).not.toHaveAttribute("tabindex");

  });

  it("日期输入框后面那个停靠点本来就带着 tabindex：不动它，也不摘掉", async () => {
    render(
      <Modal>
        <input aria-label="截止" type="date" />
        <button tabIndex={0} type="button">
          自己带着的
        </button>
        <button type="button">尾</button>
      </Modal>,
    );
    const date = screen.getByLabelText("截止");
    date.focus();

    tab(date);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(button("自己带着的")).toHaveAttribute("tabindex", "0");
  });

  it("倒着走、前一个正好是日期框：交给浏览器（它落在日期框的最后一格，.focus() 落的是第一格）；绕回来的那一下仍由弹窗接管", () => {
    render(
      <Modal>
        <input aria-label="开始" type="date" />
        <button type="button">中</button>
        <input aria-label="结束" type="date" />
        <button type="button">尾</button>
      </Modal>,
    );
    button("尾").focus();
    expect(tab(button("尾"), { shiftKey: true })).toBe(true); // 前一个是日期框「结束」：不拦
    expect(button("尾")).toHaveFocus();
    expect(screen.getByLabelText("结束")).not.toHaveAttribute("tabindex"); // 日期框本来就是浏览器的停靠点，不用借

    // 往后走进日期框没有这个问题：浏览器和 .focus() 都落在第一格，由弹窗来
    button("中").focus();
    expect(tab(button("中"))).toBe(false);
    expect(screen.getByLabelText("结束")).toHaveFocus();

    // 第一个就是日期框时，Shift+Tab 要绕回最后一个：不能交给浏览器，不然焦点漏出弹窗
    const first = screen.getByLabelText("开始");
    first.focus();
    expect(tab(first, { shiftKey: true })).toBe(false);
    expect(button("尾")).toHaveFocus();
  });

  it("Tab 进文本框和浏览器一样把字选中（以前点过、光标停在中间也一样）；文本域不全选，还原上次的光标", () => {
    render(
      <Modal>
        <button type="button">前</button>
        <input aria-label="名称" defaultValue="已有的名称" type="text" />
        <textarea aria-label="描述" defaultValue="已有的多行文字" />
        <input aria-label="数量" defaultValue="42" type="number" />
        <input aria-label="含已完成" type="checkbox" />
      </Modal>,
    );
    const name = screen.getByLabelText("名称") as HTMLInputElement;
    const text = screen.getByLabelText("描述") as HTMLTextAreaElement;
    name.focus();
    name.setSelectionRange(2, 2);
    text.focus();
    text.setSelectionRange(4, 4);
    button("前").focus();

    tab(button("前"));
    expect(name).toHaveFocus();
    expect([name.selectionStart, name.selectionEnd]).toEqual([0, 5]);

    tab(name);
    expect(text).toHaveFocus();
    expect([text.selectionStart, text.selectionEnd]).toEqual([4, 4]);

    tab(text);
    expect(screen.getByLabelText("数量")).toHaveFocus();
    tab(screen.getByLabelText("数量"));
    expect(screen.getByLabelText("含已完成")).toHaveFocus(); // 复选框不用选字，也不报错
  });

  it("只有一个可聚焦的：Tab 停在它身上；一个都没有：焦点哪也不去", () => {
    const view = render(
      <Modal>
        <button type="button">唯一</button>
      </Modal>,
    );
    button("唯一").focus();
    tab(button("唯一"));
    expect(button("唯一")).toHaveFocus();
    view.unmount();

    render(<Modal>没有按钮</Modal>);
    const dialog = screen.getByRole("dialog");
    dialog.tabIndex = -1;
    dialog.focus();
    expect(tab(dialog)).toBe(false);
    expect(dialog).toHaveFocus();
  });

  it("别人已经拦下的 Tab、Ctrl/Cmd+Tab、输入法组合中的 Tab 都不管", () => {
    render(
      <Modal>
        <button type="button">一</button>
        <button
          onKeyDown={(event) => {
            if (event.key === "Tab") event.preventDefault();
          }}
          type="button"
        >
          自己管 Tab
        </button>
        <button type="button">三</button>
      </Modal>,
    );
    button("自己管 Tab").focus();
    tab(button("自己管 Tab"));
    expect(button("自己管 Tab")).toHaveFocus();

    button("一").focus();
    expect(tab(button("一"), { ctrlKey: true })).toBe(true);
    expect(tab(button("一"), { metaKey: true })).toBe(true);
    expect(tab(button("一"), { isComposing: true })).toBe(true);
    expect(button("一")).toHaveFocus();
  });

  it("叠了两层（上层是下层开着之后才开的）：Tab 只在最上面那层里转，下面那层不抢", () => {
    function Host() {
      const [open, setOpen] = useState(false);
      return (
        <Modal name="下层">
          <button type="button">下一</button>
          <button onClick={() => setOpen(true)} type="button">
            开上层
          </button>
          {open && (
            <Modal name="上层">
              <button type="button">上一</button>
              <button type="button">上二</button>
            </Modal>
          )}
        </Modal>
      );
    }
    render(<Host />);
    fireEvent.click(button("开上层"));
    button("上二").focus();

    tab(button("上二"));
    expect(button("上一")).toHaveFocus();
    tab(button("上一"), { shiftKey: true });
    expect(button("上二")).toHaveFocus();
  });
});
