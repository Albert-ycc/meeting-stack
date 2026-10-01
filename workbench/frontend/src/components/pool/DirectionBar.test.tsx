import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DirectionBar, UNASSIGNED } from "./DirectionBar";
import { CVM, DIRECTION, HENGRUI, HUAXIA, YIMI } from "./poolFixtures";

function chip(name: string) {
  return screen.getByRole("button", { name: new RegExp(name) });
}

function dataTransfer() {
  const store: Record<string, string> = {};
  return {
    setData: (key: string, value: string) => {
      store[key] = value;
    },
    getData: (key: string) => store[key],
    effectAllowed: "",
    dropEffect: "",
  };
}

function renderBar(overrides: Partial<Parameters<typeof DirectionBar>[0]> = {}) {
  const props = {
    canWrite: true,
    onSeatsChange: vi.fn(),
    onToggle: vi.fn(),
    projects: DIRECTION,
    selected: [],
    unassignedCount: 1,
    ...overrides,
  };
  render(<DirectionBar {...props} />);
  return props;
}

describe("DirectionBar", () => {
  it("已排座次的按名次列，未排座次的收在「未排座次 +N」里，未归项目单独一个", () => {
    renderBar();
    const seats = within(screen.getByRole("group", { name: "已排座次的项目" }));

    expect(seats.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "1医米科研用药2",
      "2恒瑞健康2",
      "3华夏基金会科普同行1",
    ]);
    expect(screen.getByRole("button", { name: /未排座次/ })).toHaveTextContent("+1");
    expect(screen.getByRole("button", { name: /未归项目/ })).toHaveTextContent("1");
    expect(screen.getByText("拖动排序，点选筛选")).toBeInTheDocument();
  });

  it("点一下是筛选（多选），选中的高亮；未归项目也能点", async () => {
    const props = renderBar({ selected: [HENGRUI] });

    expect(chip("恒瑞健康")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(chip("医米科研用药"));
    await userEvent.click(chip("未归项目"));
    expect(props.onToggle).toHaveBeenNthCalledWith(1, YIMI);
    expect(props.onToggle).toHaveBeenNthCalledWith(2, UNASSIGNED);
  });

  it("拖动排序：放在别的项目上就插到它前后，松手整排保存", () => {
    const props = renderBar();
    const transfer = dataTransfer();

    fireEvent.dragStart(chip("华夏基金会科普同行"), { dataTransfer: transfer });
    // jsdom 里没有布局，指针算在目标的右半边：插到医米科研用药后面
    fireEvent.dragOver(chip("医米科研用药"), { dataTransfer: transfer, clientX: 1 });
    expect(screen.getByText("松手放到第 2 位")).toBeInTheDocument();
    fireEvent.drop(chip("医米科研用药"), { dataTransfer: transfer });

    // 落在项目上的 drop 会冒泡到整条：只能存一次
    expect(props.onSeatsChange).toHaveBeenCalledTimes(1);
    expect(props.onSeatsChange).toHaveBeenCalledWith([YIMI, HUAXIA, HENGRUI]);
  });

  it("拖到「未排座次」上就移出座次；从下拉里排入座次排到最后", async () => {
    const props = renderBar();
    const transfer = dataTransfer();

    fireEvent.dragStart(chip("恒瑞健康"), { dataTransfer: transfer });
    fireEvent.dragOver(chip("未排座次"), { dataTransfer: transfer });
    expect(screen.getByText("松手移出座次")).toBeInTheDocument();
    fireEvent.drop(chip("未排座次"), { dataTransfer: transfer });
    expect(props.onSeatsChange).toHaveBeenLastCalledWith([YIMI, HUAXIA]);

    await userEvent.click(chip("未排座次"));
    const menu = screen.getByRole("dialog", { name: "未排座次的项目" });
    expect(within(menu).getByText("未排座次的项目，按最近会议排")).toBeInTheDocument();
    await userEvent.click(within(menu).getByRole("button", { name: "排入座次" }));
    expect(props.onSeatsChange).toHaveBeenLastCalledWith([YIMI, HENGRUI, HUAXIA, CVM]);
  });

  it("拖起来又放回原位不保存", () => {
    const props = renderBar();
    const transfer = dataTransfer();
    // 拖起来以后它自己成了虚线占位（字隐藏，按名字找不到），先拿住这个按钮
    const yimi = chip("医米科研用药");
    fireEvent.dragStart(yimi, { dataTransfer: transfer });
    fireEvent.dragOver(yimi, { dataTransfer: transfer });
    expect(screen.getByText("松手放到第 1 位")).toBeInTheDocument();
    fireEvent.drop(yimi, { dataTransfer: transfer });
    expect(props.onSeatsChange).not.toHaveBeenCalled();
  });

  it("只读时项目不能拖，下拉里没有排入座次", async () => {
    renderBar({ canWrite: false });

    expect(chip("医米科研用药")).toHaveAttribute("draggable", "false");
    await userEvent.click(chip("未排座次"));
    expect(screen.queryByRole("button", { name: "排入座次" })).not.toBeInTheDocument();
  });
});
