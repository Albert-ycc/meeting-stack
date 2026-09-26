import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { MOBILE_READ_ONLY_QUERY } from "./App";
import type { ApiClient } from "./api";

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
      counts: { meetings: 0, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
    }),
    meetings: vi.fn().mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 }),
    projects: vi.fn().mockResolvedValue([]),
    tags: vi.fn().mockResolvedValue([]),
    tasks: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0, counts: {} }),
    jobs: vi.fn().mockResolvedValue({ items: [] }),
    ...overrides,
  } as unknown as ApiClient;
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => desktopMatchMedia()));
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("地址栏锚点直达", () => {
  it("冷加载带 #tasks 停在任务页，锚点不被首帧清掉", async () => {
    window.history.replaceState(null, "", "/#tasks");

    render(<App apiClient={client()} />);

    expect(await screen.findByRole("heading", { name: "任务" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#tasks");
  });

  it("冷加载带 #glossary 停在词典页", async () => {
    window.history.replaceState(null, "", "/#glossary");

    render(<App apiClient={client()} />);

    expect(await screen.findByRole("tab", { name: /术语库/ })).toBeInTheDocument();
    expect(window.location.hash).toBe("#glossary");
  });

  it("冷加载带 #requirements/<id> 停在需求详情页，锚点不被首帧清掉", async () => {
    window.history.replaceState(null, "", "/#requirements/req-1");
    const requirement = vi.fn().mockResolvedValue({
      id: "req-1",
      project_id: "project-a",
      project_name: "项目甲",
      project_color: "#376f68",
      title: "北辰仓快递配送",
      priority: "P0",
      status: "active",
      created_at: "2026-09-07T00:00:00Z",
      updated_at: "2026-09-07T00:00:00Z",
      open_task_count: 0,
      meeting_count: 0,
      latest_meeting_date: null,
      folder_count: 0,
      folders: [],
      meetings: [],
      tasks: [],
    });

    render(<App apiClient={client({ requirement })} />);

    expect(await screen.findByRole("heading", { name: "北辰仓快递配送" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#requirements/req-1");
  });

  it("冷加载带 #meetings/<id>@<秒> 打开这场会，秒数用过就从地址栏去掉", async () => {
    window.history.replaceState(null, "", "/#meetings/vm-1@754");
    const meeting = vi.fn().mockResolvedValue({
      id: "vm-1",
      title: "初审规则沟通",
      status: "completed_unreviewed",
      tags: [],
      artifacts: [],
      segments: [],
      speakers: [],
      events: [],
      transcript_versions: [],
      minutes_versions: [],
    });

    render(<App apiClient={client({ meeting, transcriptVersionSegments: vi.fn() } as Partial<ApiClient>)} />);

    expect(await screen.findByRole("heading", { name: "初审规则沟通" })).toBeInTheDocument();
    expect(meeting).toHaveBeenCalledWith("vm-1");
    expect(window.location.hash).toBe("#meetings/vm-1");
  });

  it("冷加载带 #requirements 停在需求池页", async () => {
    window.history.replaceState(null, "", "/#requirements");

    render(
      <App
        apiClient={client({
          requirements: vi.fn().mockResolvedValue({
            items: [],
            total: 0,
            limit: 10,
            offset: 0,
            counts: { active: 0, done: 0, shelved: 0, all: 0 },
          }),
        })}
      />,
    );

    expect(await screen.findByRole("heading", { name: "需求" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#requirements");
  });
});
