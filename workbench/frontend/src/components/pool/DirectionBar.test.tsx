import { createEvent, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DirectionBar } from "./DirectionBar";
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

/** jsdom 没有 DragEvent，拖动事件退成普通 Event、带不上 clientX：手动挂上 */
function dragAt(type: "dragOver" | "drop", element: HTMLElement, transfer: ReturnType<typeof dataTransfer>, clientX: number) {
  const event = createEvent[type](element, { dataTransfer: transfer });
  Object.defineProperty(event, "clientX", { value: clientX });
  fireEvent(element, event);
}

/** jsdom 没有布局：已排座次的项目按先后摆成一排，第 i 个占 [i*100, i*100+80)，中线在 i*100+40 */
function layOutChips() {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const index = Array.from(document.querySelectorAll("[data-seat-id]")).indexOf(this);
    const left = index * 100;
    return { left, right: left + 80, width: 80, top: 0, bottom: 30, height: 30, x: left, y: 0, toJSON: () => ({}) };
  });
}

function renderBar(overrides: Partial<Parameters<typeof DirectionBar>[0]> = {}) {
  const props = {
    canWrite: true,
    onSeatsChange: vi.fn(),
    onToggle: vi.fn(),
    projects: DIRECTION,
    selected: [],
    ...overrides,
  };
  render(<DirectionBar {...props} />);
  return props;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DirectionBar", () => {
  it("已排座次的按名次列，未排座次的收在「未排座次 +N」里；「未归项目」不上条（R03 异常与边界）", () => {
    renderBar();
    const seats = within(screen.getByRole("group", { name: "已排座次的项目" }));

    expect(seats.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "1医米科研用药2",
      "2恒瑞健康2",
      "3华夏基金会科普同行1",
    ]);
    expect(screen.getByRole("button", { name: /未排座次/ })).toHaveTextContent("+1");
    expect(screen.queryByRole("button", { name: /未归项目/ })).not.toBeInTheDocument();
    expect(screen.getByText("拖动排序，点选筛选")).toBeInTheDocument();
  });

  it("点一下是筛选（多选），选中的高亮；选中的项目收在「未排座次」里时芯片上标出来", async () => {
    const props = renderBar({ selected: [HENGRUI, CVM] });

    expect(chip("恒瑞健康")).toHaveAttribute("aria-pressed", "true");
    expect(chip("未排座次")).toHaveTextContent("已选 1");
    expect(chip("未排座次")).toHaveClass("is-selected");
    await userEvent.click(chip("医米科研用药"));
    expect(props.onToggle).toHaveBeenCalledWith(YIMI);
  });

  it("拖动排序：按指针在哪两个项目的中线之间插进去，落在项目之间的缝里也一样；松手整排保存一次", () => {
    layOutChips();
    const props = renderBar();
    const transfer = dataTransfer();
    const seats = screen.getByRole("group", { name: "已排座次的项目" });

    fireEvent.dragStart(chip("华夏基金会科普同行"), { dataTransfer: transfer });
    // 医米的左半边：插到它前面
    dragAt("dragOver", chip("医米科研用药"), transfer, 20);
    expect(screen.getByText("松手放到第 1 位")).toBeInTheDocument();
    // 医米和恒瑞之间 80～100 的缝：插在它们中间，不是排到最后
    dragAt("dragOver", seats, transfer, 90);
    expect(screen.getByText("松手放到第 2 位")).toBeInTheDocument();
    dragAt("drop", seats, transfer, 90);

    expect(props.onSeatsChange).toHaveBeenCalledTimes(1);
    expect(props.onSeatsChange).toHaveBeenCalledWith([YIMI, HUAXIA, HENGRUI]);
  });

  it("落在项目上时 drop 冒泡到整条，也只存一次", () => {
    layOutChips();
    const props = renderBar();
    const transfer = dataTransfer();

    fireEvent.dragStart(chip("华夏基金会科普同行"), { dataTransfer: transfer });
    dragAt("dragOver", chip("恒瑞健康"), transfer, 120);
    expect(screen.getByText("松手放到第 2 位")).toBeInTheDocument();
    dragAt("drop", chip("恒瑞健康"), transfer, 120);

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

  it("项目全都排了座次时，拖动中也有「未排座次」可以落，移出座次（R03-3）", () => {
    const allSeated = DIRECTION.map((project, index) => ({ ...project, seat: index + 1 }));
    const props = renderBar({ projects: allSeated });
    const transfer = dataTransfer();
    expect(screen.queryByRole("button", { name: /未排座次/ })).not.toBeInTheDocument();

    fireEvent.dragStart(chip("恒瑞健康"), { dataTransfer: transfer });
    fireEvent.dragOver(chip("未排座次"), { dataTransfer: transfer });
    fireEvent.drop(chip("未排座次"), { dataTransfer: transfer });

    expect(props.onSeatsChange).toHaveBeenCalledWith([YIMI, HUAXIA, CVM]);
  });

  it("拖起来又放回原位不保存", () => {
    layOutChips();
    const props = renderBar();
    const transfer = dataTransfer();
    // 拖起来以后它自己成了虚线占位（字隐藏，按名字找不到），先拿住这个按钮
    const yimi = chip("医米科研用药");
    fireEvent.dragStart(yimi, { dataTransfer: transfer });
    dragAt("dragOver", yimi, transfer, 40);
    expect(screen.getByText("松手放到第 1 位")).toBeInTheDocument();
    dragAt("drop", yimi, transfer, 40);
    expect(props.onSeatsChange).not.toHaveBeenCalled();
  });

  it("只读时项目不能拖，下拉里没有排入座次", async () => {
    renderBar({ canWrite: false });

    expect(chip("医米科研用药")).toHaveAttribute("draggable", "false");
    await userEvent.click(chip("未排座次"));
    expect(screen.queryByRole("button", { name: "排入座次" })).not.toBeInTheDocument();
  });
});
