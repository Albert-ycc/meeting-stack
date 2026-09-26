import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GlossaryPage } from "./GlossaryPage";
import type { ApiClient } from "../api";
import type { GlossarySuggestion, GlossaryTerm, MeetingSummary, Project } from "../types";

const meetings: MeetingSummary[] = [
  { id: "m-1", title: "儿科门诊例会", status: "published", tags: [] },
];

const projects: Project[] = [
  { id: "project-1", name: "云图 0830 迭代", color: "#f0783b" },
];

const generalTerm: GlossaryTerm = {
  id: "term-1",
  term: "儿童生长发育",
  aliases: ["儿生发"],
  scope: "通用",
  category: "术语",
  confirmed: true,
  source: "manual",
  hit_count: 3,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

const projectTerm: GlossaryTerm = {
  id: "term-2",
  term: "生长激素",
  aliases: [],
  scope: "云图 0830 迭代",
  category: "药品",
  confirmed: true,
  source: "manual",
  hit_count: 0,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  project_id: "project-1",
  project_name: "云图 0830 迭代",
  project_color: "#f0783b",
};

const bucketTerm: GlossaryTerm = {
  ...generalTerm,
  id: "term-3",
  term: "儿保科",
  aliases: [],
  scope: "儿科",
};

const pending: GlossarySuggestion = {
  id: "sug-1",
  wrong: "儿生发",
  correct: "儿童生长发育",
  scope: "通用",
  meeting_id: "m-1",
  context: "……儿生发门诊要预约……",
  status: "pending",
  created_at: "2026-08-19T00:00:00Z",
  updated_at: "2026-08-19T00:00:00Z",
};

function client(overrides: Partial<ApiClient> = {}) {
  return {
    glossaryTerms: vi.fn().mockResolvedValue([generalTerm]),
    glossaryScopes: vi.fn().mockResolvedValue([]),
    glossarySuggestions: vi.fn().mockImplementation((status?: string) =>
      Promise.resolve(status === "pending" ? [pending] : []),
    ),
    confirmGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true }),
    rejectGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true }),
    createGlossaryTerm: vi.fn().mockResolvedValue(generalTerm),
    updateGlossaryTerm: vi.fn().mockResolvedValue(generalTerm),
    ...overrides,
  } as unknown as ApiClient;
}

describe("GlossaryPage", () => {
  it("渲染术语卡片：术语、别名、分类；命中次数没有写入方，不显示", async () => {
    render(<GlossaryPage apiClient={client()} canWrite meetings={meetings} projects={projects} />);

    expect(await screen.findByText("儿童生长发育")).toBeTruthy();
    expect(screen.getByText("儿生发")).toBeTruthy();
    expect(screen.getByText("术语")).toBeTruthy();
    expect(screen.queryByText(/命中 \d+/)).toBeNull();
  });

  it("默认「全部」按分组渲染：公共、项目、旧分组桶各成一节", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm, bucketTerm]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await screen.findByText("儿童生长发育")).toBeTruthy();
    expect(screen.getByText("生长激素")).toBeTruthy();
    expect(screen.getByText("儿保科")).toBeTruthy();
    // 分组小节标题：公共 / 项目名 / 桶名，各自都出现过（chip 和分组标题都会渲染这些文字）
    expect(screen.getAllByText("公共").length).toBeGreaterThan(0);
    expect(screen.getAllByText("云图 0830 迭代").length).toBeGreaterThan(0);
    expect(screen.getAllByText("儿科").length).toBeGreaterThan(0);
  });

  it("远端 scopes 的通用 key 是「通用」而不是本地兜底的 general：不裂出两个通用分组", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm]),
      // 真实后端 /api/glossary/scopes 就是这个形状：kind=general 时 key 是 label 本身。
      glossaryScopes: vi.fn().mockResolvedValue([
        { kind: "general", key: "通用", label: "公共", color: null, count: 2 },
      ]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    await screen.findByText("儿童生长发育");
    // 「全部」分组标题只应出现一次「通用」，且计数按本地已加载术语现算为 1（不是远端的陈旧值 2）。
    const groupHeads = screen.getAllByText("公共").filter((node) => node.tagName === "STRONG");
    expect(groupHeads).toHaveLength(1);
    expect(groupHeads[0].nextSibling?.textContent).toBe("1");
  });

  it("点击项目 chip 只留下这个项目的术语", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm, bucketTerm]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await screen.findByText("儿童生长发育")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: /云图 0830 迭代/ }));

    expect(screen.queryByText("儿童生长发育")).toBeNull();
    expect(screen.queryByText("儿保科")).toBeNull();
    expect(screen.getByText("生长激素")).toBeTruthy();
  });

  it("搜索框按术语与别名即时过滤，不打接口", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await screen.findByText("生长激素")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "生长激素" } });

    expect(screen.queryByText("儿童生长发育")).toBeNull();
    expect(screen.getByText("生长激素")).toBeTruthy();
    expect(apiClient.glossaryTerms).toHaveBeenCalledTimes(1);
  });

  it("initialProjectId 预选中对应的项目 chip", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm]),
    });
    render(
      <GlossaryPage
        apiClient={apiClient}
        canWrite
        initialProjectId="project-1"
        meetings={meetings}
        projects={projects}
      />,
    );

    await waitFor(() => expect(screen.getByText("生长激素")).toBeTruthy());
    expect(screen.queryByText("儿童生长发育")).toBeNull();
  });

  it("切换待确认页签：显示 wrong→correct 与来源会议", async () => {
    render(<GlossaryPage apiClient={client()} canWrite meetings={meetings} projects={projects} />);

    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));

    expect(await screen.findByText("儿童生长发育")).toBeTruthy();
    expect(screen.getByText("儿生发")).toBeTruthy();
    expect(screen.getByText(/儿科门诊例会/)).toBeTruthy();
  });

  it("确认建议：调用确认接口并刷新侧栏角标", async () => {
    const onPendingChange = vi.fn();
    const apiClient = client();
    render(
      <GlossaryPage
        apiClient={apiClient}
        canWrite
        meetings={meetings}
        onPendingChange={onPendingChange}
        projects={projects}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    expect(await screen.findByText(/儿科门诊例会/)).toBeTruthy();

    // 会议没归项目：默认记公共
    fireEvent.click(screen.getByRole("button", { name: "记入 公共" }));

    await waitFor(() =>
      expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("sug-1", { target: "public", short: false }),
    );
    expect(onPendingChange).toHaveBeenCalled();
  });

  it("待确认：记入会议所在项目或 ▾ 改记别处，勾「只记 2 字」，记完可撤销", async () => {
    const item: GlossarySuggestion = {
      ...pending,
      wrong: "树立协会",
      correct: "数理协会",
      alt_wrong: "树立",
      alt_correct: "数理",
      meeting_title: "云图周会",
      target_project_id: "project-1",
      target_project_name: "云图 0830 迭代",
    };
    const apiClient = client({
      glossarySuggestions: vi.fn().mockImplementation((status?: string) =>
        Promise.resolve(status === "pending" ? [item] : []),
      ),
      confirmGlossarySuggestion: vi.fn().mockResolvedValue({
        ok: true,
        created: true,
        wrong: "树立",
        correct: "数理",
        term: { project_name: "云图 0830 迭代" },
        suggestion: null,
      }),
      undoGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true, suggestion: null }),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    expect(await screen.findByText(/来自 云图周会 · 会议现在在 云图 0830 迭代/)).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "只记 2 字" }));
    expect(screen.getByText("树立")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "改记到别处" }));
    expect(screen.getByRole("menuitem", { name: "记入 公共" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "记入 云图 0830 迭代" }));
    await waitFor(() =>
      expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("sug-1", { target: "auto", short: true }),
    );
    expect(await screen.findByText("已记入 云图 0830 迭代：树立 → 数理")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(apiClient.undoGlossarySuggestion).toHaveBeenCalledWith("sug-1"));
  });

  it("已驳回的可以恢复；正确写法已是词条时只能加到那条", async () => {
    const rejected: GlossarySuggestion = { ...pending, id: "sug-9", status: "rejected" };
    const existing: GlossarySuggestion = {
      ...pending,
      existing_term_id: "term-1",
      existing_term_project_name: null,
    };
    const apiClient = client({
      glossarySuggestions: vi.fn().mockImplementation((status?: string) =>
        Promise.resolve(status === "rejected" ? [rejected] : status === "pending" ? [existing] : []),
      ),
      restoreGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true, suggestion: null }),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    expect(await screen.findByText("词典里已有『儿童生长发育』（公共），会加到那条")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加到那条" }));
    await waitFor(() =>
      expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("sug-1", { target: "auto", short: false }),
    );

    fireEvent.click(screen.getByRole("tab", { name: "已驳回" }));
    fireEvent.click(await screen.findByRole("button", { name: "恢复" }));
    await waitFor(() => expect(apiClient.restoreGlossarySuggestion).toHaveBeenCalledWith("sug-9"));
  });

  it("只读模式（canWrite=false）不展示编辑与新增操作", async () => {
    render(<GlossaryPage apiClient={client()} canWrite={false} meetings={meetings} projects={projects} />);

    expect(await screen.findByText("儿童生长发育")).toBeTruthy();
    expect(screen.queryByText("＋ 新增术语")).toBeNull();
    expect(screen.queryByText("编辑")).toBeNull();
    expect(screen.queryByText("删除")).toBeNull();
  });

  it("新增术语：归属选「项目」时提交 project_id，不带 scope", async () => {
    const apiClient = client();
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    fireEvent.click(await screen.findByText("＋ 新增术语"));
    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), {
      target: { value: "生长曲线" },
    });
    fireEvent.click(screen.getByRole("button", { name: "项目" }));
    fireEvent.change(screen.getByLabelText("选择项目"), { target: { value: "project-1" } });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));

    await waitFor(() =>
      expect(apiClient.createGlossaryTerm).toHaveBeenCalledWith(
        expect.objectContaining({ term: "生长曲线", project_id: "project-1" }),
      ),
    );
    const payload = (apiClient.createGlossaryTerm as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.scope).toBeUndefined();
  });
});
