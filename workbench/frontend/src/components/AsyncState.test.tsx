import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AsyncState } from "./AsyncState";

describe("AsyncState", () => {
  it("renders loading, error and empty states explicitly", () => {
    const { rerender } = render(<AsyncState state="loading" />);
    expect(screen.getByText("正在读取本地档案…")).toBeInTheDocument();

    rerender(<AsyncState state="error" message="外置盘不可用" />);
    expect(screen.getByRole("alert")).toHaveTextContent("外置盘不可用");

    rerender(<AsyncState state="empty" />);
    expect(screen.getByText("这里还没有可显示的记录")).toBeInTheDocument();
  });
});
