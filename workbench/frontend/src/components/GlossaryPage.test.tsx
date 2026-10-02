import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GlossaryPage } from "./GlossaryPage";
import { ApiError, type ApiClient } from "../api";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import type { GlossarySuggestion, GlossaryTerm, MeetingSummary, Project } from "../types";

/*
 * 词典页（分栏工作台）。这个文件是原来卡片式词典页测试的改写：
 * 芯片条改成左栏范围按钮（role=button，aria-current 表示选中），卡片改成中栏列表行（role=option），
 * 「新增术语」是列表头上的按钮，编辑不再有弹窗而是右栏就地编辑，
 * 项目的待认词从列表上方的一整块改成「从材料里找到的词」页签。
 * 新行为（收件箱、键盘、未保存守卫、右栏编辑、详情里的会议）见 GlossaryPage.workbench.test.tsx。
 */

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

const list = () => screen.getByRole("listbox", { name: "词条" });
const flags = { linksEnabled: true, semanticEnabled: false, llmConfigured: false };

describe("GlossaryPage", () => {
  it("渲染术语行：术语、错写、分类；命中次数没有写入方，不显示", async () => {
    render(<GlossaryPage apiClient={client()} canWrite meetings={meetings} projects={projects} />);

    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("儿童生长发育")).toBeTruthy();
    expect(within(list()).getByText("儿生发")).toBeTruthy();
    expect(within(list()).getByText("术语")).toBeTruthy();
    expect(screen.queryByText(/命中 \d+/)).toBeNull();
  });

  it("默认「全部」按分组渲染：公共、项目、旧分组桶各成一节", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm, bucketTerm]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("儿童生长发育")).toBeTruthy();
    expect(within(list()).getByText("生长激素")).toBeTruthy();
    expect(within(list()).getByText("儿保科")).toBeTruthy();
    // 分组小节标题：公共 / 项目名 / 桶名，各自都出现过（左栏和分组头都会渲染这些文字）
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

    await within(await screen.findByRole("listbox", { name: "词条" })).findByText("儿童生长发育");
    // 「全部」分组标题只应出现一次「通用」，且计数按本地已加载术语现算为 1（不是远端的陈旧值 2）。
    const groupHeads = screen.getAllByText("公共").filter((node) => node.tagName === "STRONG");
    expect(groupHeads).toHaveLength(1);
    expect(groupHeads[0].nextSibling?.textContent).toBe("1");
  });

  it("点击左栏的项目范围只留下这个项目的术语", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm, bucketTerm]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("儿童生长发育")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /云图 0830 迭代/ }));

    expect(within(list()).queryByText("儿童生长发育")).toBeNull();
    expect(within(list()).queryByText("儿保科")).toBeNull();
    expect(within(list()).getByText("生长激素")).toBeTruthy();
  });

  it("搜索框按术语与错写即时过滤，不打接口", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm]),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("生长激素")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "生长激素" } });

    expect(within(list()).queryByText("儿童生长发育")).toBeNull();
    expect(within(list()).getByText("生长激素")).toBeTruthy();
    expect(apiClient.glossaryTerms).toHaveBeenCalledTimes(1);
  });

  it("initialProjectId 预选中对应的项目范围", async () => {
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

    await waitFor(() => expect(within(list()).getByText("生长激素")).toBeTruthy());
    expect(within(list()).queryByText("儿童生长发育")).toBeNull();
    expect(screen.getByRole("button", { name: /云图 0830 迭代/ })).toHaveAttribute("aria-current", "true");
  });

  it("4h：选中项目时取这个项目的待认词，做成「从材料里找到的词」页签；切换范围时旧请求不串进来", async () => {
    const word = {
      key: "驻场服务", term: "驻场服务", existing_term: null, wrongs: [], files: 15, spoken: 3,
      heard: [], file_names: [], file_quote: null,
    };
    let resolveFirst: (value: unknown) => void = () => undefined;
    const glossaryCandidates = vi
      .fn()
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValue({ items: [word], total: 1 });
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm]),
      glossaryCandidates,
      acceptGlossaryCandidate: vi.fn(),
      rejectGlossaryCandidate: vi.fn(),
      undoGlossaryCandidate: vi.fn(),
    } as unknown as Partial<ApiClient>);
    render(
      <LinksFlagsContext.Provider value={flags}>
        <GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />
      </LinksFlagsContext.Provider>,
    );
    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("儿童生长发育")).toBeTruthy();
    expect(glossaryCandidates).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /云图 0830 迭代/ }));
    fireEvent.click(screen.getByRole("button", { name: /^全部/ }));
    fireEvent.click(screen.getByRole("button", { name: /云图 0830 迭代/ }));

    const materialTab = await screen.findByRole("tab", { name: /从材料里找到的词/ });
    expect(glossaryCandidates).toHaveBeenCalledWith("project-1");
    resolveFirst({ items: [{ ...word, key: "旧的", term: "旧请求的词" }], total: 1 });
    fireEvent.click(materialTab);
    expect(await within(screen.getByRole("listbox", { name: "待认词" })).findByText("驻场服务")).toBeTruthy();
    expect(screen.queryByText("旧请求的词")).toBeNull();
    // 词条页签还在，回去能看到这个项目的词
    fireEvent.click(screen.getByRole("tab", { name: /^词条/ }));
    expect(within(list()).getByText("生长激素")).toBeTruthy();
  });

  it("4h：回答后这一行留在列表里并带撤销，撤销的词回到待认", async () => {
    const word = {
      key: "驻场服务", term: "驻场服务", existing_term: null, wrongs: [], files: 15, spoken: 3,
      heard: [], file_names: [], file_quote: null,
    };
    const rejectGlossaryCandidate = vi.fn().mockResolvedValue({
      text: "以后不再提『驻场服务』", undo_until: new Date(Date.now() + 600_000).toISOString(),
    });
    const undoGlossaryCandidate = vi.fn().mockResolvedValue({ status: "pending", text: "已撤销，『驻场服务』回到这里" });
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm]),
      glossaryCandidates: vi.fn().mockResolvedValue({ items: [word], total: 1 }),
      acceptGlossaryCandidate: vi.fn(),
      rejectGlossaryCandidate,
      undoGlossaryCandidate,
    } as unknown as Partial<ApiClient>);
    render(
      <LinksFlagsContext.Provider value={flags}>
        <GlossaryPage apiClient={apiClient} canWrite initialProjectId="project-1" meetings={meetings} projects={projects} />
      </LinksFlagsContext.Provider>,
    );
    fireEvent.click(await screen.findByRole("tab", { name: /从材料里找到的词/ }));
    const candidates = screen.getByRole("listbox", { name: "待认词" });
    expect(within(candidates).getAllByRole("option")).toHaveLength(1);

    fireEvent.click(within(candidates).getByRole("button", { name: "不是：驻场服务" }));
    expect(await screen.findByText("以后不再提『驻场服务』", { selector: ".action-banner__text" })).toBeTruthy();
    expect(within(candidates).getAllByRole("option")).toHaveLength(1);
    expect(within(candidates).getByText("不是")).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "撤销" })[0]);
    await waitFor(() => expect(undoGlossaryCandidate).toHaveBeenCalledWith("project-1", { key: "驻场服务" }));
    expect(await screen.findByText("已撤销，『驻场服务』回到这里", { selector: ".action-banner__text" })).toBeTruthy();
    await waitFor(() => expect(within(candidates).getByRole("button", { name: "不是：驻场服务" })).toBeTruthy());
  });

  it("4h：候选词接口是旧后台（404 Not Found）时整块静默不出", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValue([generalTerm, projectTerm]),
      glossaryCandidates: vi.fn().mockRejectedValue(new ApiError("Not Found", 404, { detail: "Not Found" })),
      glossaryCandidatesInbox: vi.fn().mockRejectedValue(new ApiError("Not Found", 404, { detail: "Not Found" })),
      acceptGlossaryCandidate: vi.fn(),
      rejectGlossaryCandidate: vi.fn(),
      undoGlossaryCandidate: vi.fn(),
    } as unknown as Partial<ApiClient>);
    render(
      <LinksFlagsContext.Provider value={flags}>
        <GlossaryPage apiClient={apiClient} canWrite initialProjectId="project-1" meetings={meetings} projects={projects} />
      </LinksFlagsContext.Provider>,
    );
    await waitFor(() => expect(within(list()).getByText("生长激素")).toBeTruthy());
    await waitFor(() => expect(apiClient.glossaryCandidates).toHaveBeenCalled());
    expect(screen.queryByText(/材料里找到的词/)).toBeNull();
    expect(screen.queryByRole("button", { name: /待认词/ })).toBeNull();
    expect(screen.queryByText(/后台还是旧版本/)).toBeNull();
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

  it("待确认：记入后重新取数还没回来时点提示上的［撤销］，等取数完了照样撤销，不被吞掉", async () => {
    let release: () => void = () => undefined;
    const hanging = new Promise<void>((resolve) => {
      release = resolve;
    });
    let confirmed = false;
    const apiClient = client({
      glossaryTerms: vi.fn().mockImplementation(async () => {
        if (confirmed) await hanging; // 记入之后的重载卡住
        return [generalTerm];
      }),
      glossarySuggestions: vi.fn().mockImplementation(async (status?: string) => {
        if (confirmed) await hanging;
        return status === "pending" ? [pending] : [];
      }),
      confirmGlossarySuggestion: vi.fn().mockImplementation(async () => {
        confirmed = true;
        return { ok: true, created: true, wrong: "儿生发", correct: "儿童生长发育", term: { project_name: null } };
      }),
      undoGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true, suggestion: null }),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    fireEvent.click(await screen.findByRole("button", { name: "记入 公共" }));
    await screen.findByText(/已记入 公共/);
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(apiClient.undoGlossarySuggestion).not.toHaveBeenCalled();

    release();
    await waitFor(() => expect(apiClient.undoGlossarySuggestion).toHaveBeenCalledWith("sug-1"));
    expect(await screen.findByText(/已撤销/)).toBeTruthy();
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

    fireEvent.click(screen.getByRole("tab", { name: /^已驳回/ }));
    fireEvent.click(await screen.findByRole("button", { name: "恢复" }));
    await waitFor(() => expect(apiClient.restoreGlossarySuggestion).toHaveBeenCalledWith("sug-9"));
  });

  it("删掉列表里仅剩的一条（选中着）后，不再去取这条的详情", async () => {
    // 刷新列表的请求还没回来时，旧列表里只剩刚删的这条，选中项不能落回它身上，否则详情接口回 404
    const reloads: Array<(value: GlossaryTerm[]) => void> = [];
    const glossaryTerms = vi
      .fn()
      .mockResolvedValueOnce([generalTerm, projectTerm])
      .mockImplementation(() => new Promise<GlossaryTerm[]>((resolve) => reloads.push(resolve)));
    const glossaryTermDetail = vi.fn().mockResolvedValue({ term_id: "x", meetings: [] });
    const apiClient = client({
      glossaryTerms,
      glossaryTermDetail,
      deleteGlossaryTerm: vi.fn().mockResolvedValue({ ok: true }),
    });
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("生长激素")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "生长激素" } });
    await waitFor(() => expect(glossaryTermDetail).toHaveBeenCalledWith("term-2"));
    const before = glossaryTermDetail.mock.calls.filter(([id]) => id === "term-2").length;

    fireEvent.click(within(list()).getByRole("button", { name: "删除『生长激素』" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() => expect(apiClient.deleteGlossaryTerm).toHaveBeenCalledWith("term-2"));
    await waitFor(() => expect(reloads.length).toBeGreaterThan(0));
    reloads.forEach((resolve) => resolve([generalTerm]));

    expect(await screen.findByText(/已删除「生长激素」/)).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("button", { name: "删除『生长激素』" })).toBeNull());
    expect(glossaryTermDetail.mock.calls.filter(([id]) => id === "term-2").length).toBe(before);
  });

  it("只读模式（canWrite=false）不展示编辑、新增与删除操作", async () => {
    render(<GlossaryPage apiClient={client()} canWrite={false} meetings={meetings} projects={projects} />);

    expect(await within(await screen.findByRole("listbox", { name: "词条" })).findByText("儿童生长发育")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /新增术语/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^删除/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "保存修改" })).toBeNull();
    expect(screen.getByLabelText("正确写法")).toHaveAttribute("readonly");
  });

  it("新增术语：归属选「项目」时提交 project_id，不带 scope", async () => {
    const apiClient = client();
    render(<GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} />);

    fireEvent.click(await screen.findByRole("button", { name: /新增术语/ }));
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "生长曲线" } });
    fireEvent.click(screen.getByRole("radio", { name: "项目" }));
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
