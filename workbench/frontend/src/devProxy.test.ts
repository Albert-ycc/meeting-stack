import { describe, expect, it } from "vitest";

import { devBackend } from "./devProxy";

describe("开发代理指向哪份后端", () => {
  it("没设 MEETING_WORKBENCH_PORT：指向开发端口，不是生产的 8765，也不吓人", () => {
    expect(devBackend({})).toEqual({ target: "http://127.0.0.1:8865", warning: null });
    expect(devBackend({ MEETING_WORKBENCH_PORT: "" }).target).toBe("http://127.0.0.1:8865");
    expect(devBackend({ MEETING_WORKBENCH_PORT: "  " }).target).toBe("http://127.0.0.1:8865");
  });

  it("设了端口就跟着走：和隔离后端用同一个环境变量起", () => {
    expect(devBackend({ MEETING_WORKBENCH_PORT: "8852" })).toEqual({ target: "http://127.0.0.1:8852", warning: null });
  });

  it("显式设成 8765（生产工作台）：照样连，但给一行警告说清楚写操作会改生产库", () => {
    const backend = devBackend({ MEETING_WORKBENCH_PORT: "8765" });
    expect(backend.target).toBe("http://127.0.0.1:8765");
    expect(backend.warning).toContain("8765");
    expect(backend.warning).toContain("生产");
    // 前导零、空白不能绕过去：URL 里 08765 就是 8765
    expect(devBackend({ MEETING_WORKBENCH_PORT: "08765" }).warning).not.toBeNull();
    expect(devBackend({ MEETING_WORKBENCH_PORT: " 8765 " }).warning).not.toBeNull();
  });

  it("端口写成别的东西：启动就报错，不带着一个连不上的代理往下跑", () => {
    for (const bad of ["abc", "87.65", "0", "70000", "-1", "8765x"]) {
      expect(() => devBackend({ MEETING_WORKBENCH_PORT: bad }), bad).toThrow("MEETING_WORKBENCH_PORT");
    }
  });
});
