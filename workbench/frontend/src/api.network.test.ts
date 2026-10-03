import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, ApiNetworkError, ApiTimeoutError, api, isAbortError, setCsrfToken } from "./api";

const NETWORK_MESSAGE = "连不上声档服务，检查它是否在运行";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("连不上服务：fetch 抛的网络错误换成一句人话", () => {
  let warn: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    setCsrfToken("t");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
    setCsrfToken("");
  });

  /** 各家浏览器断网时 fetch 抛的都是 TypeError，只是文案不一样 */
  const BROWSER_MESSAGES = ["Failed to fetch", "NetworkError when attempting to fetch resource.", "Load failed"];

  it.each(BROWSER_MESSAGES)("读请求（%s）：抛 ApiNetworkError，状态码 0，原始错误进 console", async (message) => {
    const original = new TypeError(message);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(original));

    const error = await api.meetings().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiNetworkError);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, message: NETWORK_MESSAGE });
    expect((error as Error).message).not.toContain(message);
    expect(isAbortError(error)).toBe(false);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("[api]"), original);
  });

  it("写请求：同样换成人话；不自动重发（服务端可能已经收到）", async () => {
    const original = new TypeError("Failed to fetch");
    const fetchMock = vi.fn().mockRejectedValue(original);
    vi.stubGlobal("fetch", fetchMock);

    const error = await api.publish("vm-1").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiNetworkError);
    expect(error).toMatchObject({ status: 0, message: NETWORK_MESSAGE });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("[api]"), original);
  });

  it("不设时限的写（传录音这类，没有信号也没有计时器）也一样", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const chunk = await api.uploadChunk("upload-1", 0, "YXVkaQ==").catch((reason: unknown) => reason);
    const complete = await api.completeUpload("upload-1").catch((reason: unknown) => reason);

    expect(chunk).toBeInstanceOf(ApiNetworkError);
    expect(complete).toBeInstanceOf(ApiNetworkError);
  });

  it("关系图三个直接 fetch 的读（星图、相关线、全部项目概览）也一样", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const results = await Promise.all([
      api.graph("project-1").catch((reason: unknown) => reason),
      api.graphRelated("project-1", "28d").catch((reason: unknown) => reason),
      api.getGraphOverview("28d").catch((reason: unknown) => reason),
    ]);

    for (const error of results) {
      expect(error).toBeInstanceOf(ApiNetworkError);
      expect(error).toMatchObject({ status: 0, message: NETWORK_MESSAGE });
    }
  });

  it("响应体读到一半断了（流里报 TypeError）也算连不上", async () => {
    const original = new TypeError("network error");
    const broken = new Response(
      new ReadableStream({
        start(controller) {
          controller.error(original);
        },
      }),
      { headers: { "Content-Type": "application/json" } },
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(broken));

    const error = await api.meetings().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiNetworkError);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("[api]"), original);
  });

  it("调用方取消不受影响：照旧抛 AbortError，不是 ApiError，也不往 console 写", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
          }),
      ),
    );
    const controller = new AbortController();

    const pending = api.meeting("vm-1", { signal: controller.signal }).catch((reason: unknown) => reason);
    controller.abort();
    const error = await pending;

    expect(isAbortError(error)).toBe(true);
    expect(error).not.toBeInstanceOf(ApiError);
    expect(warn).not.toHaveBeenCalled();
  });

  it("信号已经取消、请求发都没发：照旧 AbortError", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    controller.abort();

    const error = await api.meeting("vm-1", { signal: controller.signal }).catch((reason: unknown) => reason);

    expect(isAbortError(error)).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it("我们自己的超时不受影响：照旧 ApiTimeoutError，不是 ApiNetworkError，也不往 console 写", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
          }),
      ),
    );
    let outcome: unknown = "pending";
    void api.meetings().then(
      (value) => (outcome = value),
      (error: unknown) => (outcome = error),
    );

    await vi.advanceTimersByTimeAsync(30_000);

    expect(outcome).toBeInstanceOf(ApiTimeoutError);
    expect(outcome).not.toBeInstanceOf(ApiNetworkError);
    expect(warn).not.toHaveBeenCalled();
  });

  it("服务端回了错误响应不受影响：照旧是带状态码和原话的 ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ detail: "项目不存在" }, 404)));

    const error = await api.meetings().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(ApiNetworkError);
    expect(error).toMatchObject({ status: 404, message: "项目不存在" });
    expect(warn).not.toHaveBeenCalled();
  });

  it("不是网络错误的不动：响应体不是合法 JSON 的 SyntaxError 原样往外抛", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("{半截", { status: 200, headers: { "Content-Type": "application/json" } })),
    );

    const error = await api.meetings().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(SyntaxError);
    expect(error).not.toBeInstanceOf(ApiNetworkError);
  });
});
