import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { MOBILE_READ_ONLY_QUERY, useMobileBreakpoint } from "./App";
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
      semantic_enabled: true,
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
    jobs: vi.fn().mockResolvedValue({ items: [] }),
    meeting: vi.fn().mockResolvedValue(detail),
    search: vi.fn().mockResolvedValue({ mode: "exact", items: [] }),
    transcriptVersionSegments: vi.fn(),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => desktopMatchMedia()));
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(() => {
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

    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
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

    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
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
    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));

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
    fireEvent.click(screen.getByRole("button", { name: "任务" }));
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
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    expect(meetings).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(meetings).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
    });
    expect(meetings).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(meetings).toHaveBeenCalledTimes(3);
  });

  it("blocks main navigation and global search while detail edits are dirty", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const search = vi.fn().mockResolvedValue({ mode: "exact", items: [] });
    render(<App apiClient={client({ search } as Partial<ApiClient>)} />);

    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    expect(await screen.findByRole("heading", { name: "可编辑会议" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    await userEvent.type(screen.getByLabelText("00:00 逐字稿"), "本地修改");

    await userEvent.click(screen.getByRole("button", { name: "项目" }));
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
    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    expect(screen.getByText("正在读取本地档案…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "项目" }));

    await act(async () => {
      resolveDetail(detail);
    });
    expect(screen.getByRole("heading", { name: "项目与档案标签" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "可编辑会议" })).not.toBeInTheDocument();
  });

  it("does not reopen search when its response arrives after navigation", async () => {
    let resolveSearch!: (value: { mode: "exact"; items: [] }) => void;
    const search = vi.fn(
      () => new Promise<{ mode: "exact"; items: [] }>((resolve) => { resolveSearch = resolve; }),
    );
    render(<App apiClient={client({ search } as Partial<ApiClient>)} />);
    await screen.findByText("会议录音档案");
    await userEvent.type(screen.getByLabelText("全局检索"), "发布");
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    expect(screen.getByText("正在读取本地档案…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "项目" }));

    await act(async () => {
      resolveSearch({ mode: "exact", items: [] });
    });
    expect(screen.getByRole("heading", { name: "项目与档案标签" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "“发布”" })).not.toBeInTheDocument();
  });

  it("locks global navigation and search while a detail save is in flight", async () => {
    let resolveSave!: (value: { version_id: string }) => void;
    const saveTranscript = vi.fn(
      () => new Promise<{ version_id: string }>((resolve) => { resolveSave = resolve; }),
    );
    render(<App apiClient={client({ saveTranscript } as Partial<ApiClient>)} />);
    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });
    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    await userEvent.type(screen.getByLabelText("00:00 逐字稿"), "保存中");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    expect(screen.getByRole("button", { name: "项目" })).toBeDisabled();
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
    await screen.findByText("会议录音档案");
    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    await userEvent.click(await screen.findByRole("button", { name: /第一页会议/ }));
    await screen.findByRole("heading", { name: "可编辑会议" });

    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-a");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));
    await waitFor(() => expect(projects).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "项目" }));
    expect(await screen.findByText("2 场会议")).toBeInTheDocument();
  });
});
