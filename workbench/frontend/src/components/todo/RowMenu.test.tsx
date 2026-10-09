import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RowMenu } from "./RowMenu";

/*
 * 下面放不下就往上展开（F4）：jsdom 不排版，触发钮和菜单的位置、窗口高度在这里给定。
 * 菜单三项高 114（真浏览器里量的），和触发钮隔 5。
 */
const POP_HEIGHT = 114;

function place(triggerTop: number, viewportHeight = 900) {
  vi.spyOn(window, "innerHeight", "get").mockReturnValue(viewportHeight);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("task-menu__trigger")) return new DOMRect(1300, triggerTop, 28, 26);
    if (this.classList.contains("task-menu__pop")) return new DOMRect(1180, 0, 148, POP_HEIGHT);
    return new DOMRect();
  });
}

const ITEMS = [
  { label: "标记完成", act: vi.fn() },
  { label: "搁置", act: vi.fn() },
  { label: "并入其他需求…", act: vi.fn() },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RowMenu 往哪边展开（F4）", () => {
  it("下面地方够：照旧往下", async () => {
    place(400);
    render(<RowMenu items={ITEMS} label="更多操作" />);
    await userEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByRole("menu")).not.toHaveClass("is-up");
  });

  it("墙面最后一排、页面底部：下面放不下、上面更宽，往上展开", async () => {
    // 触发钮底边在 886，离窗口底只剩 14
    place(860);
    render(<RowMenu items={ITEMS} label="更多操作" />);
    await userEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByRole("menu")).toHaveClass("is-up");
    // 往上展开以后键盘照常：焦点在第一项
    expect(screen.getByRole("menuitem", { name: "标记完成" })).toHaveFocus();
  });

  it("下面差一点点也算放不下（菜单加空隙 119，下面只有 118）", async () => {
    place(900 - 26 - 118);
    render(<RowMenu items={ITEMS} label="更多操作" />);
    await userEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByRole("menu")).toHaveClass("is-up");
  });

  it("窗口很矮、上面比下面还窄：还是往下", async () => {
    place(20, 120);
    render(<RowMenu items={ITEMS} label="更多操作" />);
    await userEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByRole("menu")).not.toHaveClass("is-up");
  });

  it("每次打开重新量：上次在底下往上开了，滚到中间再开就往下", async () => {
    place(860);
    render(<RowMenu items={ITEMS} label="更多操作" />);
    const trigger = screen.getByRole("button", { name: "更多操作" });
    await userEvent.click(trigger);
    expect(screen.getByRole("menu")).toHaveClass("is-up");
    await userEvent.keyboard("{Escape}");

    vi.restoreAllMocks();
    place(300);
    await userEvent.click(trigger);
    expect(screen.getByRole("menu")).not.toHaveClass("is-up");
  });

  it("外层管开合（受控）时同样会量", async () => {
    place(860);
    render(<RowMenu items={ITEMS} label="更多操作" onOpenChange={vi.fn()} open />);
    expect(screen.getByRole("menu")).toHaveClass("is-up");
  });
});
