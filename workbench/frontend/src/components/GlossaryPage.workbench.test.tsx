import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GlossaryPage } from "./GlossaryPage";
import { PUBLIC_GLOSSARY_KEY } from "./ProjectGlossary";
import { clearPersistentViewState } from "../viewState";
import { ApiError, type ApiClient } from "../api";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import type { GlossaryTerm, MaterialWord, MeetingSummary, Project } from "../types";

/* 词典页新行为：左栏收件箱、键盘、未保存守卫、右栏就地编辑、详情里的会议、手机单栏。 */

const meetings: MeetingSummary[] = [];
const projects: Project[] = [
  { id: "project-m", name: "医米科研用药", color: "#2a9d8f" },
  { id: "project-e", name: "MDT", color: "#8c772c" },
];
const flags = { linksEnabled: true, semanticEnabled: false, llmConfigured: false };

function term(id: string, text: string, extra: Partial<GlossaryTerm> = {}): GlossaryTerm {
  return {
    id,
    term: text,
    aliases: [],
    scope: "通用",
    category: "人名",
    confirmed: true,
    source: "manual",
    hit_count: 0,
    created_at: "2026-08-25T00:10:48Z",
    updated_at: "2026-08-25T00:10:48Z",
    ...extra,
  };
}

const inProject = { scope: "医米科研用药", project_id: "project-m", project_name: "医米科研用药", project_color: "#2a9d8f" };
const TERMS: GlossaryTerm[] = [
  term("gt-cui", "崔总", { aliases: ["C总", "CC总"] }),
  term("gt-dong", "东升"),
  term("gt-yao", "科研用药", { ...inProject, category: "术语", is_cue: true }),
  term("gt-bad", "不良反应", { ...inProject, category: "术语", aliases: ["不能反应"], also: ["ADR"] }),
  term("gt-old", "儿保科", { scope: "儿科", category: "机构" }),
];
const SCOPES = [
  { kind: "general", key: "通用", label: "公共", color: null, count: 2 },
  { kind: "project", key: "project-m", label: "医米科研用药", color: "#2a9d8f", count: 2 },
  { kind: "project", key: "project-e", label: "MDT", color: "#8c772c", count: 0 },
  { kind: "bucket", key: "儿科", label: "儿科", color: null, count: 1 },
];

function word(key: string, extra: Partial<MaterialWord> = {}): MaterialWord {
  return { key, term: key, existing_term: null, wrongs: [], files: 2, spoken: 0, heard: [], file_names: [], file_quote: null, ...extra };
}
const MEDMI_WORDS = ["审核人员", "初审通", "药房端", "历史用药", "扫码发药", "查看权限", "项目课题"].map((key) => word(key));
const MEDMI_WORDS_WITH_WRONGS = [
  word("不良反应", {
    existing_term: { id: "gt-bad", term: "不良反应" },
    wrongs: [{ text: "不能反应", meetings: 2 }, { text: "不良感应", meetings: 1 }],
    spoken: 3,
    heard: [{ meeting: { id: "m-9", title: "药物可及性业务交接", date: "2026-06-29" }, start_ms: 495000, quote: "他的不能反应发生的一个情况", audio_url: null }],
  }),
  ...MEDMI_WORDS.slice(1),
];
const INBOX = (items = MEDMI_WORDS) => ({
  total: items.length + 2,
  projects: [
    { project_id: "project-m", project_name: "医米科研用药", project_color: "#2a9d8f", items, total: items.length },
    { project_id: "project-e", project_name: "MDT", project_color: "#8c772c", items: [word("病例材料"), word("影像资料")], total: 2 },
  ],
});
const UNDO_UNTIL = () => new Date(Date.now() + 600_000).toISOString();

function client(overrides: Record<string, unknown> = {}) {
  return {
    glossaryTerms: vi.fn().mockResolvedValue(TERMS),
    glossaryScopes: vi.fn().mockResolvedValue(SCOPES),
    glossarySuggestions: vi.fn().mockResolvedValue([]),
    glossaryTermDetail: vi.fn().mockResolvedValue({ cue_meetings: [] }),
    glossaryCandidatesInbox: vi.fn().mockResolvedValue(INBOX()),
    glossaryCandidates: vi.fn().mockImplementation(async (projectId: string) => {
      const group = INBOX().projects.find((item) => item.project_id === projectId);
      return { items: group?.items ?? [], total: group?.total ?? 0 };
    }),
    acceptGlossaryCandidate: vi.fn().mockResolvedValue({ text: "已记入医米科研用药：『审核人员』", already: false, undo_until: UNDO_UNTIL() }),
    rejectGlossaryCandidate: vi.fn().mockResolvedValue({ text: "以后不再提『审核人员』", undo_until: UNDO_UNTIL() }),
    undoGlossaryCandidate: vi.fn().mockResolvedValue({ status: "pending", text: "已撤销，『审核人员』回到这里" }),
    legacyGroups: vi.fn().mockResolvedValue({ summary: null }),
    createGlossaryTerm: vi.fn().mockResolvedValue(term("gt-new", "新词")),
    updateGlossaryTerm: vi.fn().mockResolvedValue(TERMS[0]),
    deleteGlossaryTerm: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  } as unknown as ApiClient;
}

function mount(apiClient: ApiClient, props: Record<string, unknown> = {}) {
  return render(
    <LinksFlagsContext.Provider value={flags}>
      <GlossaryPage apiClient={apiClient} canWrite meetings={meetings} projects={projects} {...props} />
    </LinksFlagsContext.Provider>,
  );
}

const termList = () => screen.getByRole("listbox", { name: "词条" });
const candidateList = () => screen.getByRole("listbox", { name: "待认词" });
const rail = () => screen.getByRole("navigation", { name: "范围" });
const selectedRow = (list: HTMLElement) => list.querySelector('[role="option"][aria-selected="true"]');
const press = (key: string, init: KeyboardEventInit = {}) => fireEvent.keyDown(document.body, { key, ...init });
async function ready() {
  await within(await screen.findByRole("listbox", { name: "词条" })).findByText("崔总");
}
async function openInbox(firstWord = "审核人员") {
  fireEvent.click(await screen.findByRole("button", { name: /待认词/ }));
  await within(await screen.findByRole("listbox", { name: "待认词" })).findByText(firstWord);
}

describe("词典页 · 左栏", () => {
  it("等你处理：待认词和待确认带数字；术语库：全部、公共、有词项目、旧分组", async () => {
    mount(client());
    await ready();

    const nav = within(rail());
    expect(nav.getByRole("button", { name: /待认词/ })).toHaveTextContent("9");
    expect(nav.getByRole("button", { name: /待确认/ })).toHaveTextContent("0");
    expect(nav.getByRole("button", { name: /^全部/ })).toHaveTextContent("5");
    expect(nav.getByRole("button", { name: /^公共/ })).toHaveTextContent("2");
    expect(nav.getByRole("button", { name: /医米科研用药/ })).toHaveTextContent("2");
    // 旧分组桶照常作为范围可进
    fireEvent.click(nav.getByRole("button", { name: /儿科/ }));
    expect(within(termList()).getByText("儿保科")).toBeTruthy();
    expect(within(termList()).queryByText("崔总")).toBeNull();
  });

  it("没有词的项目收进折叠区，展开后有待认词的标「待认 N」", async () => {
    mount(client());
    await ready();

    const nav = within(rail());
    expect(nav.queryByRole("button", { name: /^MDT/ })).toBeNull();
    const fold = nav.getByRole("button", { name: /还没有词的项目/ });
    expect(fold).toHaveTextContent("1");
    fireEvent.click(fold);
    const mdt = nav.getByRole("button", { name: /^MDT/ });
    expect(mdt).toHaveTextContent("待认 2");
    fireEvent.click(mdt);
    expect(await screen.findByText("『MDT』还没有项目词。")).toBeTruthy();
    expect(screen.getByText("项目词只在这个项目的会里用来纠错和识别项目。")).toBeTruthy();
    // 空项目里有待认词，给一个去看的入口
    fireEvent.click(screen.getByRole("button", { name: /看看材料里找到的 2 个词/ }));
    expect(await within(candidateList()).findByText("病例材料")).toBeTruthy();
  });

  it("旧分组整理提示做成左栏底部小卡：查看、撤销、知道了", async () => {
    const summary = {
      event_id: 1, at: "2026-09-26T00:00:00Z", undone: false,
      groups: [{ scope: "互联网医院", count: 16, project_id: null, project_name: null }],
    };
    const apiClient = client({
      legacyGroups: vi.fn().mockResolvedValue({ summary }),
      dismissLegacyGroups: vi.fn().mockResolvedValue({ ok: true }),
    });
    mount(apiClient);
    await ready();

    const card = await within(rail()).findByText("升级时已自动整理 1 个旧分组");
    expect(card).toBeTruthy();
    fireEvent.click(within(rail()).getByRole("button", { name: "查看" }));
    expect(within(rail()).getByText("「互联网医院」16 条 → 公共")).toBeTruthy();
    fireEvent.click(within(rail()).getByRole("button", { name: "知道了" }));
    await waitFor(() => expect(apiClient.dismissLegacyGroups).toHaveBeenCalled());
  });

  it("initialProjectId 是公共时预选「公共」", async () => {
    mount(client(), { initialProjectId: PUBLIC_GLOSSARY_KEY });
    await ready();
    expect(within(rail()).getByRole("button", { name: /^公共/ })).toHaveAttribute("aria-current", "true");
    expect(within(termList()).queryByText("科研用药")).toBeNull();
  });

  it("范围、搜索词、选中的词条切走再回来保留（不写进地址栏）", async () => {
    const apiClient = client();
    const first = mount(apiClient);
    await ready();
    fireEvent.click(within(rail()).getByRole("button", { name: /医米科研用药/ }));
    fireEvent.click(within(termList()).getByText("不良反应"));
    first.unmount();

    mount(apiClient);
    await within(await screen.findByRole("listbox", { name: "词条" })).findByText("不良反应");
    expect(within(rail()).getByRole("button", { name: /医米科研用药/ })).toHaveAttribute("aria-current", "true");
    expect(selectedRow(termList())).toHaveTextContent("不良反应");
    expect(window.location.hash).not.toContain("gt-bad");
  });
});

describe("词典页 · 列表与搜索", () => {
  it("错写在列表第二列，放不下的写「+N」，也叫接在后面；分类贴在名字后，归属看分组头", async () => {
    const many = term("gt-many", "数理协会", { aliases: ["树立协会", "术立协会", "数立协会", "输理协会"], also: ["数理"] });
    mount(client({ glossaryTerms: vi.fn().mockResolvedValue([...TERMS, many]) }));
    await ready();

    const row = within(termList()).getByText("数理协会").closest('[role="option"]') as HTMLElement;
    expect(row).toHaveTextContent("树立协会");
    expect(row).toHaveTextContent("+2");
    expect(row).toHaveTextContent("也叫 数理");
    // 量不到宽度时按字数估：前两块显示，后两块收进「+2」
    const chips = [...row.querySelectorAll(".gw-w__chip")];
    expect(chips.map((chip) => chip.classList.contains("gw-w__chip--spare"))).toEqual([false, false, true, true]);
    const cui = within(termList()).getByText("崔总").closest('[role="option"]') as HTMLElement;
    expect(cui).toHaveTextContent("C总");
    expect(cui).toHaveTextContent("CC总");
    expect(cui.querySelector(".gw-t")).toHaveTextContent("崔总人名");
    expect(cui).not.toHaveTextContent("公共");
    expect(within(termList()).getByRole("group", { name: "公共" })).toContainElement(cui);
  });

  it.each([true, false])("项目里每行只有名字、错写、删除位三格，没有识别列（canWrite=%s）", async (canWrite) => {
    // 手机样式按「第 3 格是删除钮或只读占位」把它藏掉，行里再加格子要一起改那条规则
    mount(client(), { canWrite });
    await ready();
    fireEvent.click(within(rail()).getByRole("button", { name: /医米科研用药/ }));

    const list = termList();
    const head = list.querySelector(".gw-colh") as HTMLElement;
    expect(head).toHaveTextContent("正确写法 · 分类");
    expect(head).toHaveTextContent("← 错写（会被改正）");
    expect(head).not.toHaveTextContent("识别");
    const rows = within(list).getAllByRole("option");
    expect(rows).toHaveLength(2);
    for (const row of rows) expect(row.children).toHaveLength(3);
  });

  it("配了错写的排在本组前面：默认选中和 J/K 都按排好的顺序走", async () => {
    mount(client());
    await ready();
    fireEvent.click(within(rail()).getByRole("button", { name: /医米科研用药/ }));

    // 数据里「科研用药」在前，但它没有错写
    const names = () => within(termList()).getAllByRole("option").map((row) => row.querySelector(".gw-t__name")?.textContent);
    expect(names()).toEqual(["不良反应", "科研用药"]);
    expect(selectedRow(termList())).toHaveTextContent("不良反应");
    press("j");
    expect(selectedRow(termList())).toHaveTextContent("科研用药");
  });

  it("给词补上错写保存后，它挪到组前面、仍然选中并滚进可视范围", async () => {
    const zhang = term("gt-zhang", "张三");
    const scrolled = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled(this.querySelector(".gw-t__name")?.textContent);
    };
    try {
      const apiClient = client({
        glossaryTerms: vi
          .fn()
          .mockResolvedValueOnce([...TERMS, zhang])
          .mockResolvedValue([...TERMS, { ...zhang, aliases: ["章三"] }]),
        updateGlossaryTerm: vi.fn().mockResolvedValue({ ...zhang, aliases: ["章三"] }),
      });
      mount(apiClient);
      await ready();
      const publicNames = () =>
        within(within(termList()).getByRole("group", { name: "公共" }))
          .getAllByRole("option")
          .map((row) => row.querySelector(".gw-t__name")?.textContent);
      expect(publicNames()).toEqual(["崔总", "东升", "张三"]);

      fireEvent.click(within(termList()).getByText("张三"));
      scrolled.mockClear();
      fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "章三" } });
      fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
      fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

      await waitFor(() => expect(publicNames()).toEqual(["崔总", "张三", "东升"]));
      const row = selectedRow(termList()) as HTMLElement;
      expect(row).toHaveTextContent("张三");
      // 错写变了要按新内容重新估放几块：新补的这块显示出来，不是收进「+1」
      expect(row.querySelector(".gw-w__chip")).not.toHaveClass("gw-w__chip--spare");
      expect(row.querySelector(".gw-w__more")).toBeNull();
      await waitFor(() => expect(scrolled).toHaveBeenLastCalledWith("张三"));
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("删掉别的词不去拽列表：选中项不动时不滚动", async () => {
    const scrolled = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrolled;
    try {
      const apiClient = client({
        glossaryTerms: vi.fn().mockResolvedValueOnce(TERMS).mockResolvedValue(TERMS.filter((item) => item.id !== "gt-cui")),
      });
      mount(apiClient);
      await ready();
      fireEvent.click(within(termList()).getByText("东升"));
      scrolled.mockClear();

      fireEvent.click(within(termList()).getByRole("button", { name: "删除『崔总』" }));
      fireEvent.click(await screen.findByRole("button", { name: "删除" }));
      await waitFor(() => expect(within(termList()).queryByText("崔总")).toBeNull());
      expect(selectedRow(termList())).toHaveTextContent("东升");
      expect(scrolled).not.toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("删掉选中的词，落到排好顺序里的下一条", async () => {
    // 数据顺序 A B C D，显示成 A C B D（带错写的在前）
    const four = [
      term("gt-a", "甲一", { aliases: ["甲乙"] }),
      term("gt-b", "乙二"),
      term("gt-c", "丙三", { aliases: ["饼三"] }),
      term("gt-d", "丁四"),
    ];
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValueOnce(four).mockResolvedValue(four.filter((item) => item.id !== "gt-c")),
      glossaryScopes: vi.fn().mockResolvedValue([{ kind: "general", key: "通用", label: "公共", color: null, count: 4 }]),
    });
    mount(apiClient);
    await within(await screen.findByRole("listbox", { name: "词条" })).findByText("丙三");
    fireEvent.click(within(termList()).getByText("丙三"));

    fireEvent.click(within(termList()).getByRole("button", { name: "删除『丙三』" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() => expect(apiClient.deleteGlossaryTerm).toHaveBeenCalledWith("gt-c"));
    await waitFor(() => expect(selectedRow(termList())).toHaveTextContent("乙二"));
  });

  it("搜索命中错写也能找到，命中的字标出来；搜不到时给「把『X』加进词典」", async () => {
    mount(client());
    await ready();

    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "c总" } });
    const list = termList();
    expect(within(list).getByText("崔总")).toBeTruthy();
    expect(within(list).queryByText("东升")).toBeNull();
    expect(list.querySelector("mark")).toHaveTextContent("C总");

    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "adr" } });
    expect(within(termList()).getByText("不良反应")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "泰邦" } });
    expect(screen.getByText("没找到「泰邦」")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "把「泰邦」加进词典" }));
    expect(screen.getByLabelText("正确写法")).toHaveValue("泰邦");
  });

  it("每个分组头有「＋ 在这里新增」，项目里点它预选该项目", async () => {
    mount(client());
    await ready();

    const heads = screen.getAllByRole("button", { name: "＋ 在这里新增" });
    expect(heads.length).toBeGreaterThanOrEqual(3);
    const projectHead = within(termList()).getByText("医米科研用药", { selector: "strong" }).parentElement as HTMLElement;
    fireEvent.click(within(projectHead).getByRole("button", { name: "＋ 在这里新增" }));
    expect(screen.getByLabelText("选择项目")).toHaveValue("project-m");
  });

  it("加载中给骨架，读取失败给重试，词典空给新增入口", async () => {
    const glossaryTerms = vi.fn().mockRejectedValueOnce(new Error("boom")).mockResolvedValue(TERMS);
    const first = mount(client({ glossaryTerms }));
    expect(screen.getByLabelText("正在载入词典")).toBeTruthy();
    expect(await screen.findByText("词典没载入成功")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await within(await screen.findByRole("listbox", { name: "词条" })).findByText("崔总");
    expect(glossaryTerms).toHaveBeenCalledTimes(2);
    first.unmount();

    mount(client({ glossaryTerms: vi.fn().mockResolvedValue([]) }));
    expect(await screen.findByText("词典还是空的。")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /新增术语/ }).length).toBeGreaterThan(0);
  });
});

describe("词典页 · 右栏就地编辑", () => {
  it("选中一条，右栏就是它的详情；改完保存、提示条出现、列表重读", async () => {
    const apiClient = client();
    mount(apiClient);
    await ready();

    expect(selectedRow(termList())).toHaveTextContent("崔总");
    expect(screen.getByLabelText("正确写法")).toHaveValue("崔总");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();

    fireEvent.click(within(termList()).getByText("东升"));
    expect(screen.getByLabelText("正确写法")).toHaveValue("东升");
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "冬升" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(apiClient.updateGlossaryTerm).toHaveBeenCalledWith("gt-dong", expect.objectContaining({ aliases: ["冬升"] })),
    );
    expect(await screen.findByText("已保存『东升』")).toBeTruthy();
    await waitFor(() => expect(apiClient.glossaryTerms).toHaveBeenCalledTimes(2));
  });

  it("新增：保存后选中新词条，提示条说加入了词典", async () => {
    const apiClient = client({
      glossaryTerms: vi.fn().mockResolvedValueOnce(TERMS).mockResolvedValue([...TERMS, term("gt-new", "新词")]),
    });
    mount(apiClient);
    await ready();

    press("c");
    expect(screen.getByLabelText("正确写法")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "新词" } });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));
    expect(await screen.findByText("已加入词典：『新词』")).toBeTruthy();
    await waitFor(() => expect(selectedRow(termList())).toHaveTextContent("新词"));
  });

  it("重名冲突：加到那条，提示条说明并选中那条", async () => {
    const conflict = { term_id: "gt-cui", term: "崔总", project_id: null, project_name: null, aliases: ["C总"], also: [] };
    const apiClient = client({
      createGlossaryTerm: vi.fn().mockRejectedValue(new ApiError("重名", 409, { conflict })),
      mergeGlossaryTerm: vi.fn().mockResolvedValue({}),
    });
    mount(apiClient);
    await ready();

    press("c");
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "崔总" } });
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "崔总们" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));
    fireEvent.click(await screen.findByRole("button", { name: "把新错写加到那条" }));

    expect(await screen.findByText("已加到 公共 的『崔总』")).toBeTruthy();
    await waitFor(() => expect(selectedRow(termList())).toHaveTextContent("崔总"));
  });

  it("删除要二次确认；取消不删，确认才删，并说明无法撤销", async () => {
    const apiClient = client();
    mount(apiClient);
    await ready();

    fireEvent.click(within(termList()).getByRole("button", { name: "删除『东升』" }));
    expect(await screen.findByText("删除后无法撤销。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(apiClient.deleteGlossaryTerm).not.toHaveBeenCalled();

    fireEvent.click(within(termList()).getByRole("button", { name: "删除『东升』" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() => expect(apiClient.deleteGlossaryTerm).toHaveBeenCalledWith("gt-dong"));
    expect(await screen.findByText("已删除「东升」")).toBeTruthy();
  });

  it("右栏右上角的垃圾桶也能删", async () => {
    const apiClient = client();
    mount(apiClient);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "删除这条" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    await waitFor(() => expect(apiClient.deleteGlossaryTerm).toHaveBeenCalledWith("gt-cui"));
  });

  it("「在哪些会上被提到」在右栏里显示，时间点可点开那场会", async () => {
    const onOpenMeeting = vi.fn();
    const apiClient = client({
      glossaryTermDetail: vi.fn().mockResolvedValue({
        cue_meetings: [{ meeting_id: "m-5", title: "医米纪百思重复入组状态互通", date: "2026-08-31", count: 3, anchors_ms: [61000], only_cue: false, origin: null }],
      }),
    });
    mount(apiClient, { onOpenMeeting });
    await ready();
    expect(await screen.findByText("医米纪百思重复入组状态互通")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "00:01:01" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-5", 61000);
  });
});

describe("词典页 · 未保存守卫", () => {
  async function dirtyOnFirst() {
    const apiClient = client();
    mount(apiClient);
    await ready();
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "崔总改" } });
    expect(screen.getByText("有未保存的修改")).toBeTruthy();
    return apiClient;
  }

  it("切换词条先确认：继续编辑留在原地，放弃修改才换", async () => {
    await dirtyOnFirst();

    fireEvent.click(within(termList()).getByText("东升"));
    expect(await screen.findByText("这条还没保存，要放弃修改吗？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByLabelText("正确写法")).toHaveValue("崔总改");

    fireEvent.click(within(termList()).getByText("东升"));
    fireEvent.click(await screen.findByRole("button", { name: "放弃修改" }));
    await waitFor(() => expect(screen.getByLabelText("正确写法")).toHaveValue("东升"));
  });

  it("切换范围、切到收件箱、切到待确认页签也先确认", async () => {
    await dirtyOnFirst();

    fireEvent.click(within(rail()).getByRole("button", { name: /医米科研用药/ }));
    expect(await screen.findByText("这条还没保存，要放弃修改吗？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(within(rail()).getByRole("button", { name: /^全部/ })).toHaveAttribute("aria-current", "true");

    fireEvent.click(within(rail()).getByRole("button", { name: /待认词/ }));
    expect(await screen.findByText("这条还没保存，要放弃修改吗？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));

    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    expect(await screen.findByText("这条还没保存，要放弃修改吗？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "放弃修改" }));
    expect(await screen.findByRole("tab", { name: /^已驳回/ })).toBeTruthy();
  });

  it("没有改动时不问；Esc 放弃修改", async () => {
    mount(client());
    await ready();

    fireEvent.click(within(termList()).getByText("东升"));
    expect(screen.queryByText("这条还没保存，要放弃修改吗？")).toBeNull();
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "东升改" } });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByLabelText("正确写法")).toHaveValue("东升");
  });
});

describe("词典页 · 键盘", () => {
  it("J/K 在列表里上下移动选中，右栏跟着换", async () => {
    mount(client());
    await ready();

    expect(selectedRow(termList())).toHaveTextContent("崔总");
    press("j");
    await waitFor(() => expect(selectedRow(termList())).toHaveTextContent("东升"));
    expect(screen.getByLabelText("正确写法")).toHaveValue("东升");
    press("k");
    await waitFor(() => expect(selectedRow(termList())).toHaveTextContent("崔总"));
  });

  it("/ 聚焦搜索，C 新增；在输入框里和输入法组合中一律不触发", async () => {
    mount(client());
    await ready();

    press("/");
    expect(screen.getByLabelText("搜索术语")).toHaveFocus();
    // 输入框里敲 j、c 是在打字
    fireEvent.keyDown(screen.getByLabelText("搜索术语"), { key: "j" });
    fireEvent.keyDown(screen.getByLabelText("搜索术语"), { key: "c" });
    expect(selectedRow(termList())).toHaveTextContent("崔总");
    expect(screen.getByLabelText("正确写法")).toHaveValue("崔总");
    // 右栏的输入框同理
    fireEvent.keyDown(screen.getByLabelText("正确写法"), { key: "j" });
    expect(selectedRow(termList())).toHaveTextContent("崔总");
    // 输入法组合中
    press("j", { isComposing: true, keyCode: 229 });
    press("c", { isComposing: true, keyCode: 229 });
    expect(selectedRow(termList())).toHaveTextContent("崔总");
    expect(screen.getByLabelText("正确写法")).toHaveValue("崔总");

    // Esc 清掉搜索词并离开输入框
    fireEvent.change(screen.getByLabelText("搜索术语"), { target: { value: "东" } });
    fireEvent.keyDown(screen.getByLabelText("搜索术语"), { key: "Escape" });
    expect(screen.getByLabelText("搜索术语")).toHaveValue("");

    (document.activeElement as HTMLElement).blur();
    press("c");
    expect(screen.getByLabelText("正确写法")).toHaveValue("");
    expect(screen.getByText("回车加入词典")).toBeTruthy();
  });

  it("确认框开着时不响应快捷键", async () => {
    mount(client());
    await ready();
    fireEvent.click(within(termList()).getByRole("button", { name: "删除『东升』" }));
    await screen.findByText("删除后无法撤销。");
    press("j");
    expect(selectedRow(termList())).toHaveTextContent("崔总");
  });

  it("只读时 C 不新增", async () => {
    mount(client(), { canWrite: false });
    await ready();
    press("c");
    expect(screen.getByLabelText("正确写法")).toHaveValue("崔总");
  });
});

describe("词典页 · 待认词收件箱", () => {
  it("按项目分组，先给 5 个，其余「还有 N 个」；点进项目去它的「从材料里找到的词」", async () => {
    mount(client());
    await ready();
    await openInbox();

    const list = candidateList();
    expect(within(list).getAllByRole("option")).toHaveLength(5 + 2);
    expect(within(list).getByText("医米科研用药")).toBeTruthy();
    expect(within(list).getByText("MDT")).toBeTruthy();
    fireEvent.click(within(list).getByRole("button", { name: "还有 2 个" }));
    expect(within(list).getAllByRole("option")).toHaveLength(7 + 2);
    expect(screen.getByText("记入后会随纪要生成交给 AI 纠错，没记入的词只留在声档里。", { selector: ".gw-note" })).toBeTruthy();

    fireEvent.click(within(list).getAllByRole("button", { name: "进入项目 ›" })[1]);
    expect(await within(candidateList()).findByText("病例材料")).toBeTruthy();
    expect(within(rail()).getByRole("button", { name: /^MDT/ })).toHaveAttribute("aria-current", "true");
  });

  it("详情：会上可能听成了（可去掉）、会上原话与时间点、材料来源、底注", async () => {
    const onOpenMeeting = vi.fn();
    const apiClient = client({
      glossaryCandidatesInbox: vi.fn().mockResolvedValue(INBOX(MEDMI_WORDS_WITH_WRONGS)),
      glossaryTerms: vi.fn().mockResolvedValue(TERMS),
    });
    mount(apiClient, { onOpenMeeting });
    await ready();
    await openInbox("不良反应");

    const detail = screen.getByRole("complementary", { name: "候选词详情" });
    expect(within(detail).getByRole("heading", { name: "不良反应" })).toBeTruthy();
    expect(within(detail).getByText(/词典里已有『不良反应』（医米科研用药）/)).toBeTruthy();
    expect(within(detail).getByText("『不能反应』")).toBeTruthy();
    expect(within(detail).getByText("会上说过 3 次")).toBeTruthy();
    expect(within(detail).getByText("药物可及性业务交接 · 6月29日")).toBeTruthy();
    expect(detail.querySelector("q mark")).toHaveTextContent("不能反应");
    fireEvent.click(within(detail).getByRole("button", { name: "00:08:15" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-9", 495000);
    expect(within(detail).getByText("在 2 个文件里", { exact: false })).toBeTruthy();

    // 全去掉后「记到已有词」置灰
    fireEvent.click(within(detail).getByRole("button", { name: "不是听错：不能反应" }));
    fireEvent.click(within(detail).getByRole("button", { name: "不是听错：不良感应" }));
    expect(within(detail).getByRole("button", { name: /记到『不良反应』/ })).toBeDisabled();
  });

  it("记入：带着去掉的写法调接口，提示条带撤销，自动跳到下一个", async () => {
    const apiClient = client({ glossaryCandidatesInbox: vi.fn().mockResolvedValue(INBOX(MEDMI_WORDS_WITH_WRONGS)) });
    mount(apiClient);
    await ready();
    await openInbox("不良反应");

    const detail = screen.getByRole("complementary", { name: "候选词详情" });
    fireEvent.click(within(detail).getByRole("button", { name: "不是听错：不良感应" }));
    fireEvent.click(within(detail).getByRole("button", { name: /记到『不良反应』/ }));

    await waitFor(() =>
      expect(apiClient.acceptGlossaryCandidate).toHaveBeenCalledWith("project-m", { key: "不良反应", not_wrong: ["不良感应"] }),
    );
    expect(await screen.findByText("已记入医米科研用药：『审核人员』", { selector: ".action-banner__text" })).toBeTruthy();
    await waitFor(() => expect(selectedRow(candidateList())).toHaveTextContent("初审通"));
    // 那一行留在列表里，标已记入
    expect(within(candidateList()).getByText("已记到那条")).toBeTruthy();
    // 词条和范围计数重读
    await waitFor(() => expect(apiClient.glossaryTerms).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getAllByRole("button", { name: "撤销" })[0]);
    await waitFor(() => expect(apiClient.undoGlossaryCandidate).toHaveBeenCalledWith("project-m", { key: "不良反应" }));
    await waitFor(() => expect(within(candidateList()).queryByText("已记到那条")).toBeNull());
  });

  it("键盘 1 记入、2 不是，处理完自动跳下一条；已答过的不再响应", async () => {
    const apiClient = client();
    mount(apiClient);
    await ready();
    await openInbox();

    expect(selectedRow(candidateList())).toHaveTextContent("审核人员");
    press("1");
    await waitFor(() => expect(apiClient.acceptGlossaryCandidate).toHaveBeenCalledWith("project-m", { key: "审核人员", not_wrong: [] }));
    expect(selectedRow(candidateList())).toHaveTextContent("初审通");
    press("2");
    await waitFor(() => expect(apiClient.rejectGlossaryCandidate).toHaveBeenCalledWith("project-m", { key: "初审通" }));
    expect(selectedRow(candidateList())).toHaveTextContent("药房端");
    press("j");
    expect(selectedRow(candidateList())).toHaveTextContent("历史用药");
    press("k");
    press("k");
    expect(selectedRow(candidateList())).toHaveTextContent("初审通");
    press("1");
    expect(apiClient.acceptGlossaryCandidate).toHaveBeenCalledTimes(1);
  });

  it("失败的回答给红色提示条，不自动收起，这一行仍待认", async () => {
    const apiClient = client({ acceptGlossaryCandidate: vi.fn().mockRejectedValue(new ApiError("服务忙", 500, { detail: "服务忙" })) });
    mount(apiClient);
    await ready();
    await openInbox();

    press("1");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("服务忙");
    expect(within(candidateList()).queryByText("已记入")).toBeNull();
  });

  it("搜索候选词，命中听错的写法也算", async () => {
    mount(client({ glossaryCandidatesInbox: vi.fn().mockResolvedValue(INBOX(MEDMI_WORDS_WITH_WRONGS)) }));
    await ready();
    await openInbox("不良反应");
    fireEvent.change(screen.getByLabelText("搜索候选词"), { target: { value: "不良感应" } });
    expect(within(candidateList()).getAllByRole("option")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("搜索候选词"), { target: { value: "没有这个" } });
    expect(screen.getByText("没找到这个词")).toBeTruthy();
  });

  it("没有待认词时写清楚；只读时只看不能答", async () => {
    const empty = mount(client({ glossaryCandidatesInbox: vi.fn().mockResolvedValue({ projects: [], total: 0 }) }));
    await ready();
    fireEvent.click(await screen.findByRole("button", { name: /待认词/ }));
    expect(await screen.findByText("没有等你认的词了")).toBeTruthy();
    empty.unmount();
    clearPersistentViewState();

    mount(client(), { canWrite: false });
    await ready();
    await openInbox();
    expect(screen.queryByRole("button", { name: /^记入/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^不是/ })).toBeNull();
  });

  it("收件箱接口不在时，左栏没有待认词，其余照常", async () => {
    mount(client({ glossaryCandidatesInbox: vi.fn().mockRejectedValue(new ApiError("Not Found", 404, {})) }));
    await ready();
    expect(within(rail()).queryByRole("button", { name: /待认词/ })).toBeNull();
    expect(within(rail()).getByRole("button", { name: /待确认/ })).toBeTruthy();
    fireEvent.click(within(rail()).getByRole("button", { name: /^公共/ }));
    expect(within(termList()).getByText("崔总")).toBeTruthy();
  });
});

describe("词典页 · 待确认", () => {
  const sug = (id: string, status: "pending" | "confirmed" | "rejected") => ({
    id, wrong: "C总", correct: "崔总", scope: "通用", meeting_id: null, context: null, status,
    created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z",
  });

  it("三个状态页签带数字；空状态照文案；左栏待确认与页头页签同步", async () => {
    const apiClient = client({
      glossarySuggestions: vi.fn().mockImplementation(async (status: string) => (status === "confirmed" ? [sug("s2", "confirmed")] : [])),
    });
    mount(apiClient);
    await ready();

    fireEvent.click(within(rail()).getByRole("button", { name: /待确认/ }));
    expect(await screen.findByText("这里还没有待确认建议。")).toBeTruthy();
    expect(screen.getByText("编辑纪要时的错字更正会出现在这里等你处理。")).toBeTruthy();
    const statusTabs = within(screen.getByRole("tablist", { name: "建议状态" }));
    expect(statusTabs.getByRole("tab", { name: /^待确认 0/ })).toBeTruthy();
    expect(statusTabs.getByRole("tab", { name: /^已确认 1/ })).toBeTruthy();
    expect(statusTabs.getByRole("tab", { name: /^已驳回 0/ })).toBeTruthy();
    fireEvent.click(statusTabs.getByRole("tab", { name: /^已确认/ }));
    expect(await screen.findByRole("button", { name: "撤销" })).toBeTruthy();
    expect(within(rail()).getByRole("button", { name: /待确认/ })).toHaveAttribute("aria-current", "true");
  });

  it("页签选择刷新不丢：卸载再挂上还在已确认", async () => {
    const apiClient = client({ glossarySuggestions: vi.fn().mockResolvedValue([]) });
    const first = mount(apiClient);
    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    fireEvent.click(await screen.findByRole("tab", { name: /^已驳回/ }));
    first.unmount();

    mount(apiClient);
    expect(await screen.findByRole("tab", { name: /^已驳回/ })).toHaveAttribute("aria-selected", "true");
  });

  it("读取失败给重试", async () => {
    let failing = true;
    const glossarySuggestions = vi.fn().mockImplementation(() => (failing ? Promise.reject(new Error("x")) : Promise.resolve([])));
    mount(client({ glossarySuggestions }));
    fireEvent.click(screen.getByRole("tab", { name: /待确认/ }));
    expect(await screen.findByText("待确认建议读取失败")).toBeTruthy();
    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("这里还没有待确认建议。")).toBeTruthy();
  });
});

describe("词典页 · 手机单栏", () => {
  it("逐级进入：列表 → 详情 → 回列表 → 范围 → 选范围回到列表", async () => {
    const { container } = mount(client());
    await ready();
    const wb = container.querySelector(".gw-wb") as HTMLElement;
    expect(wb.dataset.m).toBe("list");

    fireEvent.click(within(termList()).getByText("东升"));
    expect(wb.dataset.m).toBe("detail");
    // 这些按钮只在手机宽度下显示，jsdom 里样式把它们藏了（藏起来的元素没有可访问名称），按文字找
    fireEvent.click(screen.getByText("列表", { selector: "button" }));
    expect(wb.dataset.m).toBe("list");

    fireEvent.click(screen.getByText(/^等你处理/, { selector: "button" }));
    expect(wb.dataset.m).toBe("rail");
    fireEvent.click(within(rail()).getByRole("button", { name: /^公共/ }));
    expect(wb.dataset.m).toBe("list");
    fireEvent.click(screen.getByText("范围", { selector: "button" }));
    expect(wb.dataset.m).toBe("rail");
  });

  it("手机上点新增直接进详情", async () => {
    const { container } = mount(client());
    await ready();
    fireEvent.click(screen.getAllByRole("button", { name: /新增术语/ })[0]);
    expect((container.querySelector(".gw-wb") as HTMLElement).dataset.m).toBe("detail");
  });
});
