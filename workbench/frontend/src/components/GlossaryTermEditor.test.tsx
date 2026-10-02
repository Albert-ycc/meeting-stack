import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GlossaryTermEditor } from "./GlossaryTermEditor";
import { ApiError, type ApiClient } from "../api";
import type { GlossaryTerm, Project } from "../types";

/*
 * 右栏编辑表单的行为，由原来的编辑弹窗（GlossaryTermModal）测试搬来：
 * 输入框改用 aria-label（正确写法 / 添加错写 / 添加也叫），归属三选一改成 radio，
 * 保存结果从 onSaved(message) 改成 onSaved({ message, termId, owner })。
 */

const projects: Project[] = [
  { id: "project-1", name: "云图 0830 迭代", color: "#f0783b" },
  { id: "project-2", name: "ACME 白名单", color: "#3ecf8e" },
];

function client(overrides: Partial<ApiClient> = {}) {
  return {
    createGlossaryTerm: vi.fn().mockResolvedValue({ id: "new-1" }),
    updateGlossaryTerm: vi.fn().mockResolvedValue({}),
    deleteGlossaryTerm: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  } as unknown as ApiClient;
}

const projectTerm: GlossaryTerm = {
  id: "term-1",
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

function renderEditor(apiClient: ApiClient, props: Partial<React.ComponentProps<typeof GlossaryTermEditor>> = {}) {
  const handlers = { onSaved: vi.fn(), onCancelNew: vi.fn(), onDirtyChange: vi.fn() };
  render(
    <GlossaryTermEditor apiClient={apiClient} canWrite projects={projects} term={null} {...handlers} {...props} />,
  );
  return handlers;
}

describe("GlossaryTermEditor", () => {
  it("默认归属「通用」：提交 project_id=null、scope=通用", async () => {
    const apiClient = client();
    const { onSaved } = renderEditor(apiClient);

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "儿童生长发育" } });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));

    await waitFor(() =>
      expect(apiClient.createGlossaryTerm).toHaveBeenCalledWith(
        expect.objectContaining({ term: "儿童生长发育", project_id: null, scope: "通用" }),
      ),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onSaved.mock.calls[0][0]).toMatchObject({ termId: "new-1", message: "已加入词典：『儿童生长发育』" });
  });

  it("错写和也叫分两栏提交；新词条只能选公共或项目", async () => {
    const apiClient = client();
    renderEditor(apiClient);

    expect(screen.queryByRole("radio", { name: "其他范围" })).toBeNull();
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "病例报告表" } });
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "病历报告表" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    fireEvent.change(screen.getByLabelText("添加也叫"), { target: { value: "CRF" } });
    fireEvent.keyDown(screen.getByLabelText("添加也叫"), { key: "Enter" });
    fireEvent.click(screen.getByRole("radio", { name: "项目" }));
    fireEvent.change(screen.getByLabelText("选择项目"), { target: { value: "project-1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /用来识别项目/ }));
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));

    await waitFor(() =>
      expect(apiClient.createGlossaryTerm).toHaveBeenCalledWith(
        expect.objectContaining({
          term: "病例报告表",
          aliases: ["病历报告表"],
          also: ["CRF"],
          project_id: "project-1",
          is_cue: false,
        }),
      ),
    );
  });

  it("重名时就地给出选项：在别的项目里可改成公共词并合并，也可加到那条", async () => {
    const conflict = {
      term_id: "gt-crf",
      term: "CRF",
      project_id: "project-1",
      project_name: "云图 0830 迭代",
      aliases: ["CFR"],
      also: [],
    };
    const apiClient = client({
      createGlossaryTerm: vi.fn().mockRejectedValue(new ApiError("「CRF」已在 云图 0830 迭代 项目", 409, { conflict })),
      mergeGlossaryTerm: vi.fn().mockResolvedValue({}),
    });
    const { onSaved } = renderEditor(apiClient);

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "CRF" } });
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "CRV" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));

    expect(await screen.findByText("『CRF』已在 云图 0830 迭代 项目（错写：CFR）")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加入词典" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "加到 云图 0830 迭代 那条" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "改成公共词并合并错写" }));
    await waitFor(() =>
      expect(apiClient.mergeGlossaryTerm).toHaveBeenCalledWith("gt-crf", {
        aliases: ["CRV"],
        also: [],
        make_public: true,
      }),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onSaved.mock.calls[0][0]).toMatchObject({
      message: "已把『CRF』改成公共词，并合并了错写",
      termId: "gt-crf",
      owner: { project_id: null, scope: "通用" },
    });
  });

  it("公共词典里已有时只给「把新错写加到那条」", async () => {
    const conflict = { term_id: "gt-1", term: "随访", project_id: null, project_name: null, aliases: ["随方", "随仿"], also: [] };
    const apiClient = client({
      createGlossaryTerm: vi.fn().mockRejectedValue(new ApiError("「随访」已在 公共 词典", 409, { conflict })),
      mergeGlossaryTerm: vi.fn().mockResolvedValue({}),
    });
    renderEditor(apiClient);

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "随访" } });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));
    expect(await screen.findByText("『随访』已在 公共 词典（错写：随方、随仿）")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "改成公共词并合并错写" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "把新错写加到那条" }));
    await waitFor(() =>
      expect(apiClient.mergeGlossaryTerm).toHaveBeenCalledWith("gt-1", { aliases: [], also: [], make_public: false }),
    );
  });

  it("编辑时重名：两个合并按钮都附「（删掉这条）」，合并后原来那条删掉", async () => {
    const conflict = { term_id: "gt-9", term: "CRF", project_id: "project-2", project_name: "ACME 白名单", aliases: [], also: [] };
    const apiClient = client({
      updateGlossaryTerm: vi.fn().mockRejectedValue(new ApiError("重名", 409, { conflict })),
      mergeGlossaryTerm: vi.fn().mockResolvedValue({}),
    });
    const { onSaved } = renderEditor(apiClient, { term: projectTerm });

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "CRF" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(await screen.findByRole("button", { name: "改成公共词并合并错写（删掉这条）" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加到 ACME 白名单 那条（删掉这条）" }));
    await waitFor(() => expect(apiClient.deleteGlossaryTerm).toHaveBeenCalledWith("term-1"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("归属「项目」未选具体项目时禁用提交", () => {
    renderEditor(client());

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "生长曲线" } });
    fireEvent.click(screen.getByRole("radio", { name: "项目" }));

    expect(screen.getByRole("button", { name: "加入词典" })).toBeDisabled();
  });

  it("编辑已挂项目的术语：归属回填为「项目」并预选中原项目", () => {
    renderEditor(client(), { term: projectTerm });

    expect(screen.getByRole("radio", { name: "项目" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText("选择项目")).toHaveValue("project-1");
  });

  it("错写输入框：中文输入法组合态下按 Enter 不提前提交（新增术语回归）", () => {
    renderEditor(client());
    const aliasInput = screen.getByLabelText("添加错写");

    // 组合态：模拟用户还在敲拼音候选字时按下 Enter 确认候选——不应该被当成提交。
    fireEvent.change(aliasInput, { target: { value: "erbaoke" } });
    fireEvent.keyDown(aliasInput, { key: "Enter", isComposing: true, keyCode: 229 });
    expect(screen.queryByText("erbaoke")).toBeNull();
    expect(aliasInput).toHaveValue("erbaoke");

    // 真正打完字后再按 Enter：正常提交为 chip，并清空草稿。
    fireEvent.change(aliasInput, { target: { value: "儿保科" } });
    fireEvent.keyDown(aliasInput, { key: "Enter" });
    expect(screen.getByText("儿保科")).toBeTruthy();
    expect(aliasInput).toHaveValue("");
  });

  it("正确写法框：输入法组合态下的回车不提交，正常回车提交", async () => {
    const apiClient = client();
    renderEditor(apiClient);
    const title = screen.getByLabelText("正确写法");

    fireEvent.change(title, { target: { value: "sui" } });
    fireEvent.keyDown(title, { key: "Enter", isComposing: true, keyCode: 229 });
    expect(apiClient.createGlossaryTerm).not.toHaveBeenCalled();

    fireEvent.change(title, { target: { value: "随访" } });
    fireEvent.keyDown(title, { key: "Enter" });
    await waitFor(() => expect(apiClient.createGlossaryTerm).toHaveBeenCalledTimes(1));
  });

  it("编辑自定义范围的术语：提交更新时带回原 scope，不带 project_id", async () => {
    const term: GlossaryTerm = {
      id: "term-2",
      term: "儿保科",
      aliases: [],
      scope: "儿科",
      category: "其他",
      confirmed: true,
      source: "manual",
      hit_count: 1,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    };
    const apiClient = client();
    renderEditor(apiClient, { term });

    expect(screen.getByRole("radio", { name: "旧分组「儿科」" })).toHaveAttribute("aria-checked", "true");
    // 没改任何东西时保存是灰的，改完才亮
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("分类"), { target: { value: "机构" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(apiClient.updateGlossaryTerm).toHaveBeenCalledWith(
        "term-2",
        expect.objectContaining({ project_id: null, scope: "儿科", category: "机构" }),
      ),
    );
  });

  it("校验：正确写法须含中文或字母；错写 2–8 字；重复的不加", () => {
    renderEditor(client());

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "12345" } });
    expect(screen.getByText("1–40 字，须包含中文或字母", { selector: "p" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "加入词典" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "随访" } });
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "错" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    expect(screen.getByText("每条 2–8 字")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "随方" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    fireEvent.change(screen.getByLabelText("添加错写"), { target: { value: "随方" } });
    fireEvent.keyDown(screen.getByLabelText("添加错写"), { key: "Enter" });
    expect(screen.getByText("已经有这一条了")).toBeTruthy();
  });

  it("编辑时没改动，名称框里回车不再保存一遍", async () => {
    const apiClient = client();
    renderEditor(apiClient, { term: projectTerm });
    fireEvent.keyDown(screen.getByLabelText("正确写法"), { key: "Enter" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(apiClient.updateGlossaryTerm).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "生长激素片" } });
    fireEvent.keyDown(screen.getByLabelText("正确写法"), { key: "Enter" });
    await waitFor(() =>
      expect(apiClient.updateGlossaryTerm).toHaveBeenCalledWith("term-1", expect.objectContaining({ term: "生长激素片" })),
    );
  });

  it("Esc 放弃修改：编辑时恢复原样，新增时通知页面取消；保存中不响应", async () => {
    let resolveSave: (value: unknown) => void = () => undefined;
    const apiClient = client({
      updateGlossaryTerm: vi.fn().mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; })),
    });
    const { onCancelNew } = renderEditor(apiClient, { term: projectTerm });
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "生长激素 2" } });
    expect(screen.getByText("有未保存的修改")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("button", { name: "保存中…" })).toBeDisabled();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByLabelText("正确写法")).toHaveValue("生长激素 2");
    resolveSave({});
    await waitFor(() => expect(screen.queryByRole("button", { name: "保存中…" })).toBeNull());

    // 页面收到 onSaved 会把新词条传回来；这里 term 属性没变，放弃修改回到属性里的样子
    fireEvent.change(screen.getByLabelText("正确写法"), { target: { value: "又改了" } });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByLabelText("正确写法")).toHaveValue("生长激素");
    expect(onCancelNew).not.toHaveBeenCalled();
  });

  it("新增时 Esc 取消；输入法组合中的 Esc 不算", () => {
    const { onCancelNew } = renderEditor(client());
    fireEvent.keyDown(document, { key: "Escape", isComposing: true, keyCode: 229 });
    expect(onCancelNew).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancelNew).toHaveBeenCalledTimes(1);
  });

  it("只读：不出保存、删除和添加输入框", () => {
    renderEditor(client(), { canWrite: false, term: projectTerm, onDelete: vi.fn() });
    expect(screen.queryByRole("button", { name: "保存修改" })).toBeNull();
    expect(screen.queryByRole("button", { name: "删除这条" })).toBeNull();
    expect(screen.queryByLabelText("添加错写")).toBeNull();
    expect(screen.getByLabelText("正确写法")).toHaveAttribute("readonly");
  });

  it("「在哪些会上被提到」：有数据列出会议名、日期、次数和时间点，没有时给诚实的空状态", async () => {
    const onOpenMeeting = vi.fn();
    const apiClient = client({
      glossaryTermDetail: vi.fn().mockResolvedValue({
        cue_meetings: [
          { meeting_id: "m-1", title: "药物可及性业务交接", date: "2026-06-29", count: 2, anchors_ms: [495000], only_cue: false, origin: null },
          { meeting_id: "m-2", title: "只用来识别项目的会", date: "2026-08-31", count: 1, anchors_ms: [], only_cue: true, origin: "ai" },
        ],
      }),
    });
    renderEditor(apiClient, { term: projectTerm, onOpenMeeting });

    expect(await screen.findByText("药物可及性业务交接")).toBeTruthy();
    expect(screen.getByText("提到 2 次")).toBeTruthy();
    expect(screen.getByText("2026年06月29日")).toBeTruthy();
    expect(screen.getByText(/这场会里只用来识别项目，没有逐句时间点/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "00:08:15" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-1", 495000);
  });

  it("「在哪些会上被提到」没有记录时写「还没有记录到」", async () => {
    const apiClient = client({ glossaryTermDetail: vi.fn().mockResolvedValue({ cue_meetings: [] }) });
    renderEditor(apiClient, { term: projectTerm });
    expect(await screen.findByText("还没有记录到。之后的新会议里提到它会记在这里。")).toBeTruthy();
  });
});
