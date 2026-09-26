import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NOTICE_AUTO_HIDE_MS, NoticeBanner, useNotice, type NoticeTone } from "./Notice";

function Harness() {
  const { notice, setNotice, dismissNotice } = useNotice();
  const show = (message: string, tone?: NoticeTone) => () => setNotice(message, tone);
  return (
    <div>
      <button onClick={show("已保存")} type="button">成功</button>
      <button onClick={show("请再次保存", "warning")} type="button">提醒</button>
      <button onClick={show("保存失败：磁盘已满", "error")} type="button">失败</button>
      <NoticeBanner notice={notice} onDismiss={dismissNotice} />
    </div>
  );
}

describe("操作提示条", () => {
  afterEach(() => vi.useRealTimers());

  it("成功提示几秒后自动收起", () => {
    vi.useFakeTimers();
    render(<Harness />);
    act(() => screen.getByRole("button", { name: "成功" }).click());

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("已保存");
    expect(banner).toHaveClass("action-banner--success");

    act(() => vi.advanceTimersByTime(NOTICE_AUTO_HIDE_MS + 10));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("失败提示用红色 alert，不会自己消失，点 ✕ 才收起", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Harness />);
    act(() => screen.getByRole("button", { name: "失败" }).click());

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("保存失败：磁盘已满");
    expect(banner).toHaveClass("action-banner--error");

    act(() => vi.advanceTimersByTime(NOTICE_AUTO_HIDE_MS * 4));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "关闭提示" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("提醒不自动收起；新提示替换旧提示并重新计时", () => {
    vi.useFakeTimers();
    render(<Harness />);
    act(() => screen.getByRole("button", { name: "提醒" }).click());
    expect(screen.getByRole("status")).toHaveClass("action-banner--warning");
    act(() => vi.advanceTimersByTime(NOTICE_AUTO_HIDE_MS * 2));
    expect(screen.getByRole("status")).toHaveTextContent("请再次保存");

    act(() => screen.getByRole("button", { name: "成功" }).click());
    expect(screen.getByRole("status")).toHaveTextContent("已保存");
    act(() => vi.advanceTimersByTime(NOTICE_AUTO_HIDE_MS + 10));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
