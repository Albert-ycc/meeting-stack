import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MinutesVersion, MinutesVersionBody } from "../types";
import { MinutesVersionPreview } from "./MinutesVersionPreview";

function meta(versionNo: number, extra: Partial<MinutesVersion> = {}): MinutesVersion {
  return {
    id: `mv-${versionNo}`,
    meeting_id: "vm-1",
    version_no: versionNo,
    kind: "generated",
    published: 0,
    created_at: "2026-07-10T00:00:00Z",
    ...extra,
  };
}

function bodyOf(versionNo: number, markdown: string): MinutesVersionBody {
  return { ...meta(versionNo), markdown };
}

/** 用例自己决定哪个请求先回、哪个后回；不看信号，像一个收不到取消的慢接口 */
function pending<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function clientWith(minutesVersion: ReturnType<typeof vi.fn>) {
  return { minutesVersion } as unknown as ApiClient;
}

describe("MinutesVersionPreview", () => {
  it("选到历史版本才去取：先说正在读取，回来之后显示这一版的正文", async () => {
    const request = pending<MinutesVersionBody>();
    const minutesVersion = vi.fn().mockReturnValue(request.promise);
    render(<MinutesVersionPreview apiClient={clientWith(minutesVersion)} meetingId="vm-1" version={meta(2)} />);

    expect(screen.getByRole("status")).toHaveTextContent("正在读取这一版的内容");
    expect(minutesVersion).toHaveBeenCalledTimes(1);
    expect(minutesVersion).toHaveBeenCalledWith("vm-1", "mv-2", { signal: expect.any(AbortSignal) });

    await act(async () => request.resolve(bodyOf(2, "## 第二版的决议\n\n- 先按老办法做")));

    const region = screen.getByRole("region", { name: "所选纪要版本的内容" });
    expect(within(region).getByRole("heading", { name: "第二版的决议" })).toBeInTheDocument();
    expect(within(region).getByText("先按老办法做")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("没选历史版本时什么都不画，也不取", () => {
    const minutesVersion = vi.fn();
    const { container } = render(
      <MinutesVersionPreview apiClient={clientWith(minutesVersion)} meetingId="vm-1" version={null} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(minutesVersion).not.toHaveBeenCalled();
  });

  it("标题带版本号、类型，写回过的带「已写回」", () => {
    const minutesVersion = vi.fn().mockReturnValue(new Promise(() => {}));
    render(
      <MinutesVersionPreview
        apiClient={clientWith(minutesVersion)}
        meetingId="vm-1"
        version={meta(3, { kind: "draft", published: 1 })}
      />,
    );

    expect(screen.getByText("v3 · 人工修改 · 已写回")).toBeInTheDocument();
    expect(screen.getByText("只是预览，当前版本没动")).toBeInTheDocument();
  });

  it("列表里已经带着正文的版本直接显示，不再去取", () => {
    const minutesVersion = vi.fn();
    render(
      <MinutesVersionPreview
        apiClient={clientWith(minutesVersion)}
        meetingId="vm-1"
        version={meta(2, { markdown: "列表里就有的正文" })}
      />,
    );

    expect(screen.getByText("列表里就有的正文")).toBeInTheDocument();
    expect(minutesVersion).not.toHaveBeenCalled();
  });

  it("这一版正文是空的：说没有内容，不留一块空白", async () => {
    const minutesVersion = vi.fn().mockResolvedValue(bodyOf(2, "  \n"));
    render(<MinutesVersionPreview apiClient={clientWith(minutesVersion)} meetingId="vm-1" version={meta(2)} />);

    expect(await screen.findByText("这一版没有内容")).toBeInTheDocument();
  });

  it("取失败：写明原因和［重试］，重试成功就换成正文", async () => {
    const user = userEvent.setup();
    const minutesVersion = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("纪要版本不存在", 404))
      .mockResolvedValueOnce(bodyOf(2, "重试之后读到的正文"));
    render(<MinutesVersionPreview apiClient={clientWith(minutesVersion)} meetingId="vm-1" version={meta(2)} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败：纪要版本不存在");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByText("重试之后读到的正文")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(minutesVersion).toHaveBeenCalledTimes(2);
  });

  it("失败不是一直挂着：换到别的版本不带着上一个的错误，换回来再取", async () => {
    const minutesVersion = vi
      .fn()
      .mockRejectedValueOnce(new Error("连不上"))
      .mockResolvedValueOnce(bodyOf(3, "第三版"))
      .mockResolvedValueOnce(bodyOf(2, "第二版重新读到了"));
    const apiClient = clientWith(minutesVersion);
    const { rerender } = render(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(2)} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败：连不上");

    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(3)} />);
    expect(await screen.findByText("第三版")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(2)} />);
    expect(await screen.findByText("第二版重新读到了")).toBeInTheDocument();
  });

  it("连着切几个版本：被换下去的请求中止，迟到的回答盖不掉最后选的那一版", async () => {
    const requests = new Map<string, ReturnType<typeof pending<MinutesVersionBody>>>();
    const signals = new Map<string, AbortSignal>();
    const minutesVersion = vi.fn((_meetingId: string, versionId: string, options: { signal: AbortSignal }) => {
      requests.set(versionId, pending<MinutesVersionBody>());
      signals.set(versionId, options.signal);
      return requests.get(versionId)!.promise;
    });
    const apiClient = clientWith(minutesVersion);
    const { rerender } = render(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(2)} />);
    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(3)} />);
    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(4)} />);

    expect(minutesVersion).toHaveBeenCalledTimes(3);
    expect(signals.get("mv-2")?.aborted).toBe(true);
    expect(signals.get("mv-3")?.aborted).toBe(true);
    expect(signals.get("mv-4")?.aborted).toBe(false);
    expect(screen.getByText("v4 · AI 生成")).toBeInTheDocument();

    // 最后选的先回；两个旧的后到，一个照常回话、一个按取消的样子拒绝
    await act(async () => requests.get("mv-4")!.resolve(bodyOf(4, "第四版的正文")));
    await act(async () => requests.get("mv-2")!.resolve(bodyOf(2, "第二版的正文")));
    await act(async () => requests.get("mv-3")!.reject(new DOMException("请求已取消", "AbortError")));

    expect(screen.getByText("第四版的正文")).toBeInTheDocument();
    expect(screen.queryByText("第二版的正文")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("v4 · AI 生成")).toBeInTheDocument();
  });

  it("最后选的那一版还没回来时，不显示上一个版本的正文", async () => {
    const requests = new Map<string, ReturnType<typeof pending<MinutesVersionBody>>>();
    const minutesVersion = vi.fn((_meetingId: string, versionId: string) => {
      requests.set(versionId, pending<MinutesVersionBody>());
      return requests.get(versionId)!.promise;
    });
    const apiClient = clientWith(minutesVersion);
    const { rerender } = render(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(2)} />);
    await act(async () => requests.get("mv-2")!.resolve(bodyOf(2, "第二版的正文")));
    expect(screen.getByText("第二版的正文")).toBeInTheDocument();

    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(3)} />);

    expect(screen.queryByText("第二版的正文")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在读取这一版的内容");
  });

  it("取过的版本来回翻不重取，立刻显示", async () => {
    const minutesVersion = vi.fn((_meetingId: string, versionId: string) =>
      Promise.resolve(bodyOf(Number(versionId.slice(3)), `${versionId} 的正文`)),
    );
    const apiClient = clientWith(minutesVersion);
    const { rerender } = render(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(2)} />);
    expect(await screen.findByText("mv-2 的正文")).toBeInTheDocument();
    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(3)} />);
    expect(await screen.findByText("mv-3 的正文")).toBeInTheDocument();

    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-1" version={meta(2)} />);

    // 同步就有，不经过「正在读取」
    expect(screen.getByText("mv-2 的正文")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(minutesVersion).toHaveBeenCalledTimes(2);
  });

  it("换了一场会：同一个版本号不会拿上一场会的正文", async () => {
    const minutesVersion = vi.fn((meetingId: string) => Promise.resolve(bodyOf(2, `${meetingId} 的第二版`)));
    const apiClient = clientWith(minutesVersion);
    const { rerender } = render(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-a" version={meta(2)} />);
    expect(await screen.findByText("vm-a 的第二版")).toBeInTheDocument();

    rerender(<MinutesVersionPreview apiClient={apiClient} meetingId="vm-b" version={meta(2)} />);

    await waitFor(() => expect(screen.getByText("vm-b 的第二版")).toBeInTheDocument());
    expect(minutesVersion).toHaveBeenCalledTimes(2);
    expect(minutesVersion).toHaveBeenLastCalledWith("vm-b", "mv-2", expect.any(Object));
  });

  it("页面卸载时中止还没回来的请求", () => {
    let signal: AbortSignal | undefined;
    const minutesVersion = vi.fn((_meetingId: string, _versionId: string, options: { signal: AbortSignal }) => {
      signal = options.signal;
      return new Promise<MinutesVersionBody>(() => {});
    });
    const { unmount } = render(
      <MinutesVersionPreview apiClient={clientWith(minutesVersion)} meetingId="vm-1" version={meta(2)} />,
    );
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
  });
});
