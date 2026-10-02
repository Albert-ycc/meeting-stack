import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import App, { MEETING_PAGE_SIZE, MOBILE_READ_ONLY_QUERY, useMobileBreakpoint } from "./App";
import { ApiError, type ApiClient } from "./api";
import type { MeetingDetail, MeetingSummary } from "./types";

const firstMeeting: MeetingSummary = {
  id: "vm-page-1",
  title: "第一页会议",
  status: "published",
  tags: [],
};

const detail: MeetingDetail = {
  ...firstMeeting,
  title: "可编辑会议",
  artifacts: [],
  segments: [
    { id: "seg-1", ordinal: 0, start_ms: 0, end_ms: 1_000, text: "原始逐字稿" },
  ],
  speakers: [],
  events: [],
  current_transcript_version_id: "tv-1",
  transcript_versions: [
    {
      id: "tv-1",
      meeting_id: firstMeeting.id,
      version_no: 1,
      kind: "funasr",
      published: 0,
      created_at: "2026-07-10T00:00:00Z",
    },
  ],
  minutes_versions: [],
};

function desktopMatchMedia() {
  return {
    matches: false,
    media: MOBILE_READ_ONLY_QUERY,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
}

function client(overrides: Partial<ApiClient> = {}) {
  return {
    bootstrap: vi.fn().mockResolvedValue({
      csrf_token: "token",
      mobile_read_only: true,
      mobile_task_write: true,
      semantic_enabled: true,
      pending_confirm_count: 0,
    }),
    health: vi.fn().mockResolvedValue({
      status: "ok",
      services: {},
      counts: { meetings: 120, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
    }),
    meetings: vi.fn().mockResolvedValue({ items: [firstMeeting], limit: 50, offset: 0, total: 120 }),
    projects: vi.fn().mockResolvedValue([
      { id: "project-a", name: "项目甲", color: "#376f68", meeting_count: 2 },
      { id: "project-b", name: "项目乙", color: "#f0783b", meeting_count: 3 },
    ]),
    tags: vi.fn().mockResolvedValue([]),
    tasks: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 }),
    jobs: vi.fn().mockResolvedValue({ items: [] }),
    meeting: vi.fn().mockResolvedValue(detail),
    search: vi.fn().mockResolvedValue({ mode: "exact", items: [] }),
    transcriptVersionSegments: vi.fn(),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  // 视图切换会写地址栏锚点，每个用例从干净的地址冷启动。
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("matchMedia", vi.fn(() => desktopMatchMedia()));
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(() => {
  // 打开会议详情会把地址栏同步成 #meetings/<id>，不清掉的话下一个用例冷加载会直接打开那场会
  window.history.replaceState(null, "", "/");
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("mobile write protection", () => {
  it("treats a wide landscape coarse-pointer device as read-only", () => {
    const matchMedia = vi.fn((query: string) => ({
      matches: query === MOBILE_READ_ONLY_QUERY,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.stubGlobal("matchMedia", matchMedia);

    const { result } = renderHook(() => useMobileBreakpoint());

    expect(result.current).toBe(true);
    expect(matchMedia).toHaveBeenCalledWith("(max-width: 767px), (pointer: coarse)");
  });
});

describe("App refresh and navigation safety", () => {
  it("moves back to the last valid page when a silent refresh shrinks the result set", async () => {
    let shrunk = false;
    const meetings = vi.fn().mockImplementation((filters = {}) => {
      const offset = (filters as { offset?: number }).offset ?? 0;
      if (offset === 100 && shrunk) {
        return Promise.resolve({ items: [], limit: 50, offset, total: 90 });
      }
      const page = Math.floor(offset / 50) + 1;
      return Promise.resolve({
        items: [{ ...firstMeeting, id: `vm-page-${page}`, title: `第${page}页会议` }],
        limit: 50,
        offset,
        total: shrunk ? 90 : 120,
      });
    });
    render(<App apiClient={client({ meetings } as Partial<ApiClient>)} />);

    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await screen.findByText("第1页会议");
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("第2页会议");
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByText("第3页会议");

    shrunk = true;
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() =>
      expect(meetings).toHaveBeenLastCalledWith({ limit: 50, offset: 50 }),
    );
    expect(await screen.findByText("第2页会议")).toBeInTheDocument();
    expect(screen.getByText("第 2 / 2 页")).toBeInTheDocument();
  });

  it("requests fixed pages, exposes the total and resets to page one after filtering", async () => {
    const meetings = vi.fn().mockImplementation((filters = {}) => {
      const values = filters as { offset?: number; project_id?: string };
      const item = values.offset === 50
        ? { ...firstMeeting, id: "vm-page-2", title: "第二页会议" }
        : firstMeeting;
      return Promise.resolve({
        items: [item],
        limit: 50,
        offset: values.offset ?? 0,
        total: 120,
      });
    });
    render(<App apiClient={client({ meetings } as Partial<ApiClient>)} />);

    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    expect(await screen.findByText("第一页会议")).toBeInTheDocument();
    expect(meetings).toHaveBeenCalledWith({ limit: 50, offset: 0 });

    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("第二页会议")).toBeInTheDocument();
    expect(meetings).toHaveBeenLastCalledWith({ limit: 50, offset: 50 });

    await userEvent.selectOptions(screen.getByLabelText("筛选项目"), "project-a");
    await waitFor(() =>
      expect(meetings).toHaveBeenLastCalledWith({
        limit: 50,
        offset: 0,
        project_id: "project-a",
      }),
    );
  });

  it("ignores a late meeting response after a newer filter has completed", async () => {
    let resolveA!: (value: unknown) => void;
    let resolveB!: (value: unknown) => void;
    const meetings = vi.fn().mockImplementation((filters = {}) => {
      const projectId = (filters as { project_id?: string }).project_id;
      if (projectId === "project-a") return new Promise((resolve) => { resolveA = resolve; });
      if (projectId === "project-b") return new Promise((resolve) => { resolveB = resolve; });
      return Promise.resolve({ items: [firstMeeting], limit: 50, offset: 0, total: 1 });
    });
    render(<App apiClient={client({ meetings } as Partial<ApiClient>)} />);
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");

    fireEvent.change(screen.getByLabelText("筛选项目"), { target: { value: "project-a" } });
    fireEvent.change(screen.getByLabelText("筛选项目"), { target: { value: "project-b" } });
    await act(async () => {
      resolveB({
        items: [{ ...firstMeeting, id: "vm-b", title: "项目乙最新结果" }],
        limit: 50,
        offset: 0,
        total: 1,
      });
    });
    expect(screen.getByText("项目乙最新结果")).toBeInTheDocument();

    await act(async () => {
      resolveA({
        items: [{ ...firstMeeting, id: "vm-a", title: "项目甲迟到结果" }],
        limit: 50,
        offset: 0,
        total: 1,
      });
    });
    expect(screen.getByText("项目乙最新结果")).toBeInTheDocument();
    expect(screen.queryByText("项目甲迟到结果")).not.toBeInTheDocument();
  });

  it("polls active jobs every five seconds, pauses while hidden and refreshes immediately when visible", async () => {
    vi.useFakeTimers();
    const jobs = vi
      .fn()
      .mockResolvedValueOnce({
        items: [
          {
            id: "job-active",
            meeting_id: "vm-job",
            state: "transcribing",
            created_at: "2026-07-10T00:00:00Z",
            updated_at: "2026-07-10T00:00:00Z",
          },
        ],
      })
      .mockRejectedValue(new ApiError("任务台账暂时不可用", 503));
    render(<App apiClient={client({ jobs } as Partial<ApiClient>)} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "转写录音" }));
    expect(screen.getByText("vm-job")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("status")).toHaveTextContent("显示上次成功读取的任务");
    expect(jobs).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
    });
    expect(jobs).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(jobs).toHaveBeenCalledTimes(3);
  });

  it("侧栏待办角标 15 秒刷一次，一直在页面间切换也照刷，不被换页重置", async () => {
    vi.useFakeTimers();
    const tasks = vi.fn().mockResolvedValue({ items: [], total: 18, limit: 1, offset: 0 });
    render(<App apiClient={client({ tasks } as Partial<ApiClient>)} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const pendingPolls = () =>
      tasks.mock.calls.filter(([params]) => (params as { status?: string }).status === "pending_confirm").length;
    const before = pendingPolls();

    // 每 10 秒换一次页，三轮共 30 秒：至少该刷过一次
    for (const name of ["录音档案", "项目管理", "录音档案"]) {
      await act(async () => {
        vi.advanceTimersByTime(10_000);
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name }));
    }
    expect(pendingPolls()).toBeGreaterThan(before);
  });

  it("转写台账返回 5xx：工作台「进行中转写」写读取失败，不说「没有正在处理的录音」", async () => {
    const jobs = vi.fn().mockRejectedValue(new ApiError("relayctl 不存在", 503));
    render(<App apiClient={client({ jobs } as Partial<ApiClient>)} />);

    const card = (await screen.findAllByText("进行中转写"))[0].closest("button")!;
    await waitFor(() => expect(card).toHaveTextContent("转写状态读取失败"));
    expect(card).toHaveTextContent("—");
    expect(screen.queryByText("没有正在处理的录音")).not.toBeInTheDocument();
  });

  it("refreshes the visible library every fifteen seconds and immediately after returning to it", async () => {
    vi.useFakeTimers();
    const meetings = vi.fn().mockResolvedValue({
      items: [firstMeeting],
      limit: 50,
      offset: 0,
      total: 1,
    });
    render(<App apiClient={client({ meetings } as Partial<ApiClient>)} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    // 工作台的抖动图表也走 meetings 接口（自己取一次大范围做按天聚合），
    // 这里只关心资料库列表的刷新节奏，按分页尺寸把两种请求分开数。
    const libraryCalls = () =>
      meetings.mock.calls.filter(
        ([filters]) => (filters as { limit?: number } | undefined)?.limit === MEETING_PAGE_SIZE,
      ).length;
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    expect(libraryCalls()).toBe(1);

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(libraryCalls()).toBe(2);

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
    });
    expect(libraryCalls()).toBe(2);

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(libraryCalls()).toBe(3);
  });

  it("marks the health indicator unreachable after two consecutive failed polls and clears it on the next success", async () => {
    vi.useFakeTimers();
    const okPayload = {
      status: "ok" as const,
      services: {},
      counts: { meetings: 1, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
    };
    const health = vi
      .fn()
      .mockResolvedValueOnce(okPayload)
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(okPayload);
    render(<App apiClient={client({ health } as Partial<ApiClient>)} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("服务正常")).toBeInTheDocument();

    // 第一次轮询失败：网络层不可达还没确认，保留上一次的健康态。
    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("服务正常")).toBeInTheDocument();

    // 连续两次失败才判定不可达，避免单次抖动就报警。
    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("服务异常")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("服务正常")).toBeInTheDocument();
  });

  it("blocks main navigation and global search while detail edits are dirty", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const search = vi.fn().mockResolvedValue({ mode: "exact", items: [] });
    render(<App apiClient={client({ search } as Partial<ApiClient>)} />);

    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    expect(await screen.findByRole("heading", { name: "可编辑会议" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    await userEvent.type(screen.getByLabelText("00:00 逐字稿"), "本地修改");

    await userEvent.click(screen.getByRole("button", { name: "项目管理" }));
    expect(confirm).toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "可编辑会议" })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("全局检索"), "发布");
    await userEvent.click(screen.getByRole("button", { name: "检索" }));
    expect(search).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "可编辑会议" })).toBeInTheDocument();
  });

  it("does not reopen a meeting when its detail response arrives after navigation", async () => {
    let resolveDetail!: (value: MeetingDetail) => void;
    const meeting = vi.fn(
      () => new Promise<MeetingDetail>((resolve) => { resolveDetail = resolve; }),
    );
    render(<App apiClient={client({ meeting } as Partial<ApiClient>)} />);
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    expect(screen.getByText("正在读取本地档案…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "项目管理" }));

    await act(async () => {
      resolveDetail(detail);
    });
    expect(screen.getByRole("heading", { name: "项目" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "可编辑会议" })).not.toBeInTheDocument();
  });

  describe("会被新请求取代的读：新的发出去或页面卸载时中止旧的", () => {
    /** 后端还没回：像真的 fetch 一样，信号一中止就以 AbortError 拒绝 */
    const hangUntilAborted = (options?: { signal?: AbortSignal }) =>
      new Promise<never>((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => reject(new DOMException("请求已取消", "AbortError")));
      });
    const emptySearch = { mode: "exact", items: [], similar: [], expanded: [], expand_hints: [] };

    it("连着打开两场会：还没回来的上一场的详情请求被中止，也不显示读取失败", async () => {
      window.history.replaceState(null, "", "/#meetings/vm-slow");
      const signals: Record<string, AbortSignal | undefined> = {};
      const meeting = vi.fn((id: string, options?: { signal?: AbortSignal }) => {
        signals[id] = options?.signal;
        return id === "vm-slow" ? hangUntilAborted(options) : Promise.resolve({ ...detail, id, title: "第二场会" });
      });
      render(<App apiClient={client({ meeting } as unknown as Partial<ApiClient>)} />);
      await waitFor(() => expect(meeting).toHaveBeenCalledWith("vm-slow", expect.anything()));
      expect(signals["vm-slow"]?.aborted).toBe(false);

      act(() => {
        window.history.pushState(null, "", "/#meetings/vm-fast");
        window.dispatchEvent(new PopStateEvent("popstate"));
      });

      expect(await screen.findByRole("heading", { name: "第二场会" })).toBeInTheDocument();
      expect(signals["vm-slow"]?.aborted).toBe(true);
      expect(signals["vm-fast"]?.aborted).toBe(false);
      expect(screen.queryByText("会议档案读取失败")).not.toBeInTheDocument();
      expect(screen.queryByText("请求已取消")).not.toBeInTheDocument();
    });

    it("再搜一次：上一次还没回来的检索被中止，不显示「检索失败」", async () => {
      const signals: AbortSignal[] = [];
      const search = vi.fn((query: string, _scope?: string, options?: { signal?: AbortSignal }) => {
        if (options?.signal) signals.push(options.signal);
        return query === "慢的词" ? hangUntilAborted(options) : Promise.resolve(emptySearch);
      });
      render(<App apiClient={client({ search } as unknown as Partial<ApiClient>)} />);
      await screen.findByText("服务正常");
      const box = screen.getByLabelText("全局检索");
      await userEvent.type(box, "慢的词");
      await userEvent.click(screen.getByRole("button", { name: "检索" }));
      await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
      expect(signals[0].aborted).toBe(false);

      await userEvent.clear(box);
      await userEvent.type(box, "快的词");
      await userEvent.click(screen.getByRole("button", { name: "检索" }));

      await waitFor(() => expect(search).toHaveBeenCalledTimes(2));
      expect(signals[0].aborted).toBe(true);
      expect(signals[1].aborted).toBe(false);
      expect(screen.queryByText("检索失败")).not.toBeInTheDocument();
      expect(screen.queryByText("请求已取消")).not.toBeInTheDocument();
    });

    it("在读会议详情时发起检索：详情请求被中止", async () => {
      window.history.replaceState(null, "", "/#meetings/vm-slow");
      let detailSignal: AbortSignal | undefined;
      const meeting = vi.fn((_id: string, options?: { signal?: AbortSignal }) => {
        detailSignal = options?.signal;
        return hangUntilAborted(options);
      });
      const search = vi.fn().mockResolvedValue(emptySearch);
      render(<App apiClient={client({ meeting, search } as unknown as Partial<ApiClient>)} />);
      await waitFor(() => expect(meeting).toHaveBeenCalled());

      await userEvent.type(screen.getByLabelText("全局检索"), "数理协会");
      await userEvent.click(screen.getByRole("button", { name: "检索" }));

      await waitFor(() => expect(search).toHaveBeenCalled());
      expect(detailSignal?.aborted).toBe(true);
      expect(screen.queryByText("会议档案读取失败")).not.toBeInTheDocument();
    });

    it("页面卸载：还在等的详情请求中止", async () => {
      window.history.replaceState(null, "", "/#meetings/vm-slow");
      let detailSignal: AbortSignal | undefined;
      const meeting = vi.fn((_id: string, options?: { signal?: AbortSignal }) => {
        detailSignal = options?.signal;
        return hangUntilAborted(options);
      });
      const view = render(<App apiClient={client({ meeting } as unknown as Partial<ApiClient>)} />);
      await waitFor(() => expect(meeting).toHaveBeenCalled());
      expect(detailSignal?.aborted).toBe(false);

      view.unmount();

      expect(detailSignal?.aborted).toBe(true);
    });

    it("页面卸载：还在等的检索中止", async () => {
      let searchSignal: AbortSignal | undefined;
      const search = vi.fn((_query: string, _scope?: string, options?: { signal?: AbortSignal }) => {
        searchSignal = options?.signal;
        return hangUntilAborted(options);
      });
      const view = render(<App apiClient={client({ search } as unknown as Partial<ApiClient>)} />);
      await screen.findByText("服务正常");
      await userEvent.type(screen.getByLabelText("全局检索"), "数理协会");
      await userEvent.click(screen.getByRole("button", { name: "检索" }));
      await waitFor(() => expect(search).toHaveBeenCalled());
      expect(searchSignal?.aborted).toBe(false);

      view.unmount();

      expect(searchSignal?.aborted).toBe(true);
    });
  });

  it("searches all projects by default, narrows the scope on the results page and opens minutes hits on the minutes tab", async () => {
    const search = vi.fn().mockResolvedValue({
      mode: "hybrid",
      items: [
        {
          segment_id: null,
          meeting_id: "vm-page-1",
          title: "第一页会议",
          start_ms: null,
          end_ms: null,
          text: "决定成立数理协会",
          match_kind: "minutes",
          matched: "数理协会",
        },
      ],
      similar: [],
      expanded: [],
      expand_hints: [],
    });
    const meeting = vi.fn().mockResolvedValue({
      ...detail,
      current_minutes_version_id: "mv-1",
      minutes_versions: [
        { id: "mv-1", meeting_id: "vm-page-1", version_no: 1, kind: "generated", published: 0, markdown: "决定成立数理协会", created_at: "2026-07-10T00:00:00Z" },
      ],
    });
    render(<App apiClient={client({ search, meeting } as Partial<ApiClient>)} />);
    await screen.findByText("服务正常");

    await userEvent.type(screen.getByLabelText("全局检索"), "数理协会");
    await userEvent.click(screen.getByRole("button", { name: "检索" }));
    expect(search).toHaveBeenLastCalledWith("数理协会", undefined, { signal: expect.any(AbortSignal) });
    expect(screen.queryByRole("button", { name: "原句" })).toBeNull();

    fireEvent.change(await screen.findByLabelText("搜索范围"), { target: { value: "project-b" } });
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith("数理协会", "project-b", { signal: expect.any(AbortSignal) }),
    );

    await userEvent.click(await screen.findByRole("button", { name: "打开纪要" }));
    expect(await screen.findByRole("heading", { name: "可编辑会议" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /会议纪要/ })).toHaveAttribute("aria-selected", "true");
  });

  it("does not reopen search when its response arrives after navigation", async () => {
    let resolveSearch!: (value: { mode: "exact"; items: [] }) => void;
    const search = vi.fn(
      () => new Promise<{ mode: "exact"; items: [] }>((resolve) => { resolveSearch = resolve; }),
    );
    render(<App apiClient={client({ search } as Partial<ApiClient>)} />);
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await userEvent.type(screen.getByLabelText("全局检索"), "发布");
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    expect(screen.getByText("正在读取本地档案…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "项目管理" }));

    await act(async () => {
      resolveSearch({ mode: "exact", items: [] });
    });
    expect(screen.getByRole("heading", { name: "项目" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "“发布”" })).not.toBeInTheDocument();
  });

  it("locks global navigation and search while a detail save is in flight", async () => {
    let resolveSave!: (value: { version_id: string }) => void;
    const saveTranscript = vi.fn(
      () => new Promise<{ version_id: string }>((resolve) => { resolveSave = resolve; }),
    );
    render(<App apiClient={client({ saveTranscript } as Partial<ApiClient>)} />);
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });
    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    await userEvent.type(screen.getByLabelText("00:00 逐字稿"), "保存中");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    expect(screen.getByRole("button", { name: "项目管理" })).toBeDisabled();
    expect(screen.getByLabelText("全局检索")).toBeDisabled();
    expect(screen.getByRole("button", { name: "检索" })).toBeDisabled();

    await act(async () => resolveSave({ version_id: "tv-saved" }));
  });

  it("refreshes server project counts after classification is saved", async () => {
    const projects = vi
      .fn()
      .mockResolvedValueOnce([
        { id: "project-a", name: "项目甲", color: "#376f68", meeting_count: 1 },
      ])
      .mockResolvedValue([
        { id: "project-a", name: "项目甲", color: "#376f68", meeting_count: 2 },
      ]);
    const updateMeeting = vi.fn().mockResolvedValue(detail);
    render(<App apiClient={client({ projects, updateMeeting } as Partial<ApiClient>)} />);
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });

    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-a");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));
    await waitFor(() => expect(projects).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "项目管理" }));
    // 项目管理页没排座次的项目是列表行，会议数是行里「会议」下面的数字。
    const list = await screen.findByRole("list", { name: "未排座次的项目列表" });
    expect(within(list).getByText("会议").nextElementSibling).toHaveTextContent("2");
  });
});

describe("浏览历史与返回", () => {
  it("打开会议写入 #meetings/<id>，返回按钮回到打开前的录音档案", async () => {
    render(<App apiClient={client()} />);
    fireEvent.click(await screen.findByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await waitFor(() => expect(window.location.hash).toBe("#library"));

    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });
    expect(window.location.hash).toBe("#meetings/vm-page-1");

    await userEvent.click(screen.getByRole("button", { name: "← 返回录音档案" }));
    await screen.findByText("会议录音档案");
    expect(screen.queryByRole("heading", { name: "可编辑会议" })).not.toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#library"));
  });

  it("浏览器后退关掉会议详情，停在原来的视图", async () => {
    render(<App apiClient={client()} />);
    fireEvent.click(await screen.findByRole("button", { name: "录音档案" }));
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });

    act(() => window.history.back());

    await screen.findByText("会议录音档案");
    expect(screen.queryByRole("heading", { name: "可编辑会议" })).not.toBeInTheDocument();
  });

  // 真浏览器的后退：地址先回到上一条，依次派发 popstate、hashchange。rendered：两个事件之间 React 有没有渲染完
  function browserBack(hash: string, rendered = true) {
    const popstate = () => {
      window.history.replaceState({ app: true }, "", `/${hash}`);
      window.dispatchEvent(new PopStateEvent("popstate", { state: { app: true } }));
    };
    const hashchange = () => window.dispatchEvent(new HashChangeEvent("hashchange"));
    if (rendered) {
      act(popstate);
      act(hashchange);
    } else {
      act(() => {
        popstate();
        hashchange();
      });
    }
  }

  it("从检索结果打开会议，浏览器后退（popstate 后紧跟 hashchange）：检索结果还在", async () => {
    const search = vi.fn().mockResolvedValue({
      mode: "exact",
      items: [
        {
          segment_id: null,
          meeting_id: "vm-page-1",
          title: "第一页会议",
          start_ms: null,
          end_ms: null,
          text: "决定成立数理协会",
          match_kind: "minutes",
          matched: "数理协会",
        },
      ],
    });
    render(<App apiClient={client({ search } as Partial<ApiClient>)} />);
    fireEvent.click(await screen.findByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    await userEvent.type(screen.getByLabelText("全局检索"), "数理协会");
    await userEvent.click(screen.getByRole("button", { name: "检索" }));
    await userEvent.click(await screen.findByRole("button", { name: "打开纪要" }));
    await screen.findByRole("heading", { name: "可编辑会议" });
    expect(screen.getByRole("button", { name: "← 返回检索结果" })).toBeInTheDocument();

    browserBack("#library");

    expect(screen.queryByRole("heading", { name: "可编辑会议" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "“数理协会”" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开纪要" })).toBeInTheDocument();
  });

  it("会议有没保存的修改时浏览器后退：只问一次，两个事件之间没来得及渲染也不再问一遍", async () => {
    render(<App apiClient={client()} />);
    fireEvent.click(await screen.findByRole("button", { name: "录音档案" }));
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });
    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    await userEvent.type(screen.getByLabelText("00:00 逐字稿"), "改了");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    browserBack("#library", false);

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByText("会议录音档案")).toBeInTheDocument();
  });

  it("点侧栏换视图从顶上看起；浏览器后退不动滚动位置（交给浏览器恢复）", async () => {
    // jsdom 不排版，scrollTop 换成能记值的属性
    let scrollTop = 0;
    Object.defineProperty(document.documentElement, "scrollTop", {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => {
        scrollTop = value;
      },
    });
    onTestFinished(() => {
      delete (document.documentElement as { scrollTop?: number }).scrollTop;
    });
    render(<App apiClient={client()} />);
    fireEvent.click(await screen.findByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    scrollTop = 900;

    fireEvent.click(screen.getByRole("button", { name: "项目管理" }));
    await screen.findByRole("heading", { name: "项目" });
    expect(scrollTop).toBe(0);

    scrollTop = 400;
    act(() => window.history.back());
    await screen.findByText("会议录音档案");
    expect(scrollTop).toBe(400);
  });

  it("冷加载带 #meetings/<id> 直接打开那场会", async () => {
    window.history.replaceState(null, "", "/#meetings/vm-page-1");
    const meeting = vi.fn().mockResolvedValue(detail);
    render(<App apiClient={client({ meeting } as Partial<ApiClient>)} />);

    await screen.findByRole("heading", { name: "可编辑会议" });
    expect(meeting).toHaveBeenCalledWith("vm-page-1", { signal: expect.any(AbortSignal) });
    // 冷加载直达没有上一条可退，返回按钮就地关掉详情，回到默认的工作台。
    await userEvent.click(screen.getByRole("button", { name: "← 返回工作台" }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "可编辑会议" })).not.toBeInTheDocument());
    expect(window.location.hash).toBe("");
  });
});

describe("内容区出错不白屏", () => {
  it("一场会的详情形状不对、渲染时抛错：侧栏和检索框还在，内容区给［重新载入］，换视图后恢复", async () => {
    window.history.replaceState(null, "", "/#meetings/vm-page-1");
    // 打到别的接口拿回来的形状（没有 segments 等字段），会议页渲染时抛错
    const meeting = vi
      .fn()
      .mockResolvedValueOnce({ state: "synced", reason: null })
      .mockResolvedValueOnce({ state: "synced", reason: null })
      .mockResolvedValue(detail);
    const silence = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<App apiClient={client({ meeting } as Partial<ApiClient>)} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("这一页出错了");
    expect(screen.getByRole("button", { name: "录音档案" })).toBeInTheDocument();
    expect(screen.getByLabelText("全局检索")).toBeInTheDocument();

    // ［重新载入］重取这场会；还是坏的就仍然停在错误态
    await userEvent.click(within(alert).getByRole("button", { name: "重新载入" }));
    await waitFor(() => expect(meeting).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("alert")).toHaveTextContent("这一页出错了");

    // 换视图：错误态撤掉，正常显示
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    expect(await screen.findByText("会议录音档案")).toBeInTheDocument();
    expect(screen.queryByText("这一页出错了")).not.toBeInTheDocument();

    // 再打开这场会（接口这回正常）：会议页正常出来
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    expect(await screen.findByRole("heading", { name: "可编辑会议" })).toBeInTheDocument();
    silence.mockRestore();
  });
});

describe("手工导入录音", () => {
  it("上传中切到别的页面，刷新、关标签页照样先问一句；传完就不再拦", async () => {
    let releaseChunk!: () => void;
    const chunkGate = new Promise<void>((resolve) => {
      releaseChunk = resolve;
    });
    const apiClient = client({
      startUpload: vi.fn().mockResolvedValue({ upload_id: "upload-1", chunk_bytes: 8, chunk_count: 1 }),
      uploadChunk: vi.fn(async () => {
        await chunkGate;
        return {};
      }),
      completeUpload: vi.fn().mockResolvedValue({ path: "/tmp/a.m4a", size_bytes: 5, status: "queued", job_id: "job-9" }),
    } as unknown as Partial<ApiClient>);
    const unloadPrevented = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    render(<App apiClient={apiClient} />);
    fireEvent.click(await screen.findByRole("button", { name: "转写录音" }));
    await userEvent.upload(
      await screen.findByLabelText("选择录音文件"),
      new File(["audio"], "a.m4a", { type: "audio/mp4" }),
    );
    await waitFor(() => expect(apiClient.uploadChunk).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    expect(unloadPrevented()).toBe(true);

    // 回到转写录音页：还看得到在传，不能再开一个
    fireEvent.click(screen.getByRole("button", { name: "转写录音" }));
    expect(await screen.findByRole("button", { name: /上传中/ })).toBeDisabled();

    await act(async () => {
      releaseChunk();
      await chunkGate;
    });
    await waitFor(() => expect(apiClient.completeUpload).toHaveBeenCalledWith("upload-1"));
    await waitFor(() => expect(unloadPrevented()).toBe(false));
  });

  it("分块发到一半点［取消上传］：中止这一块、不再发后面的、清掉服务端会话，不弹失败，回到能重新导入的样子", async () => {
    const signals: Array<AbortSignal | undefined> = [];
    const apiClient = client({
      startUpload: vi.fn().mockResolvedValue({ upload_id: "upload-1", chunk_bytes: 3, chunk_count: 3 }),
      uploadChunk: vi.fn((_uploadId: string, index: number, _content: string, signal?: AbortSignal) => {
        signals.push(signal);
        if (index === 0) return Promise.resolve({});
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("请求已取消", "AbortError")), { once: true });
        });
      }),
      cancelUpload: vi.fn().mockResolvedValue({ ok: true, upload_id: "upload-1" }),
      completeUpload: vi.fn(),
    } as unknown as Partial<ApiClient>);
    const unloadPrevented = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    render(<App apiClient={apiClient} />);
    fireEvent.click(await screen.findByRole("button", { name: "转写录音" }));
    await userEvent.upload(
      await screen.findByLabelText("选择录音文件"),
      new File(["abcdefg"], "a.m4a", { type: "audio/mp4" }),
    );
    await waitFor(() => expect(apiClient.uploadChunk).toHaveBeenCalledTimes(2)); // 第 1 块在路上
    expect(unloadPrevented()).toBe(true);

    await userEvent.click(await screen.findByRole("button", { name: "取消上传" }));

    await waitFor(() => expect(apiClient.cancelUpload).toHaveBeenCalledWith("upload-1"));
    expect(await screen.findByText("已取消上传")).toBeInTheDocument();
    expect(signals[1]?.aborted).toBe(true);
    expect(apiClient.uploadChunk).toHaveBeenCalledTimes(2);
    expect(apiClient.completeUpload).not.toHaveBeenCalled();
    expect(screen.queryByText(/上传失败|请求已取消|aborted/i)).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "＋ 手工导入录音" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /取消上传|正在取消/ })).not.toBeInTheDocument();
    await waitFor(() => expect(unloadPrevented()).toBe(false));
  });
});

describe("全局检索框和输入法", () => {
  it("输入法选字的回车不触发表单提交，正常回车照常提交", async () => {
    render(<App apiClient={client()} />);
    const input = await screen.findByLabelText("全局检索");
    fireEvent.change(input, { target: { value: "sui" } });

    // 浏览器对组合中的回车也派发 keydown；拦掉它的默认动作，表单就不会被隐式提交
    expect(fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 })).toBe(false);
    expect(fireEvent.keyDown(input, { key: "Enter", keyCode: 229 })).toBe(false);
    expect(fireEvent.keyDown(input, { key: "Enter", keyCode: 13 })).toBe(true);
  });
});

describe("列表页检索条件", () => {
  it("待办查过的条件，去别的页面再回来还在", async () => {
    const todo = vi.fn().mockResolvedValue({
      today: "2026-09-30",
      week_end: "2026-10-04",
      total: 0,
      groups: [],
      counts: {},
      project_counts: {},
      projects: [],
    });
    render(<App apiClient={client({ todo } as Partial<ApiClient>)} />);

    fireEvent.click(await screen.findByRole("button", { name: "待办" }));
    await userEvent.type(await screen.findByPlaceholderText("输入任务名称"), "周报");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() => expect(todo).toHaveBeenLastCalledWith(expect.objectContaining({ q: "周报" })));

    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "待办" }));

    expect(await screen.findByPlaceholderText("输入任务名称")).toHaveValue("周报");
    await waitFor(() => expect(todo).toHaveBeenLastCalledWith(expect.objectContaining({ q: "周报" })));
  });
});
