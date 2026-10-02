import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, ApiTimeoutError, api } from "./api";

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

describe("api.graph：带 etag 的星图读取", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("不带 etag：不发 If-None-Match；200 返回整张图和响应头里的 etag", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ project: { id: "p" } }, 200, { ETag: 'W/"g-1"' }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.graph("p", "28d", "m:a");

    expect(result).toEqual({ graph: { project: { id: "p" } }, etag: 'W/"g-1"' });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/graph/projects/p?window=28d&focus=m%3Aa");
    expect(init.credentials).toBe("same-origin");
    expect(init.headers).not.toHaveProperty("If-None-Match");
  });

  it("带 etag：原样放进 If-None-Match；304 返回 graph: null，etag 取响应头的", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 304, headers: { ETag: 'W/"g-1"' } }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.graph("p", undefined, undefined, 'W/"g-1"');

    expect(result).toEqual({ graph: null, etag: 'W/"g-1"' });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/graph/projects/p");
    expect(init.headers).toMatchObject({ "If-None-Match": 'W/"g-1"' });
  });

  it("304 的响应头里没有 etag（中间有代理吃掉了）：沿用发出去的那个", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 304 })));

    await expect(api.graph("p", "7d", undefined, 'W/"g-7d"')).resolves.toEqual({ graph: null, etag: 'W/"g-7d"' });
  });

  it("带着 etag 但数据变了：200，换上新的整张图和新的 etag", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ project: { id: "p" }, meetings: [] }, 200, { ETag: 'W/"g-2"' })));

    const result = await api.graph("p", undefined, undefined, 'W/"g-1"');

    expect(result.graph).toMatchObject({ meetings: [] });
    expect(result.etag).toBe('W/"g-2"');
  });

  it("响应里没有 ETag（旧后台）：etag 是 null，下次就不带 If-None-Match", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ project: { id: "p" } })));

    await expect(api.graph("p")).resolves.toEqual({ graph: { project: { id: "p" } }, etag: null });
  });

  it("出错照旧抛 ApiError，文案取后端的 detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ detail: "项目不存在" }, 404)));

    const error = await api.graph("gone").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, message: "项目不存在" });
  });

  describe("读请求的 30 秒上限", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    it("30 秒没有响应：抛 ApiTimeoutError，29 秒时还在等", async () => {
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
      void api.graph("p", undefined, undefined, 'W/"g-1"').then(
        (value) => (outcome = value),
        (error: unknown) => (outcome = error),
      );

      await vi.advanceTimersByTimeAsync(29_999);
      expect(outcome).toBe("pending");
      await vi.advanceTimersByTimeAsync(1);

      expect(outcome).toBeInstanceOf(ApiTimeoutError);
    });
  });
});
