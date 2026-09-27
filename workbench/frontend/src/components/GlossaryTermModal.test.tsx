import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GlossaryTermModal } from "./GlossaryTermModal";
import { ApiError, type ApiClient } from "../api";
import type { GlossaryTerm, Project } from "../types";

const projects: Project[] = [
  { id: "project-1", name: "云图 0830 迭代", color: "#f0783b" },
  { id: "project-2", name: "ACME 白名单", color: "#3ecf8e" },
];

function client(overrides: Partial<ApiClient> = {}) {
  return {
    createGlossaryTerm: vi.fn().mockResolvedValue({}),
    updateGlossaryTerm: vi.fn().mockResolvedValue({}),
    ...overrides,
  } as unknown as ApiClient;
}

describe("GlossaryTermModal", () => {
  it("默认归属「通用」：提交 project_id=null、scope=通用", async () => {
    const apiClient = client();
    const onSaved = vi.fn();
    render(
      <GlossaryTermModal apiClient={apiClient} onClose={vi.fn()} onSaved={onSaved} projects={projects} />,
    );

    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), {
      target: { value: "儿童生长发育" },
    });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));

    await waitFor(() =>
      expect(apiClient.createGlossaryTerm).toHaveBeenCalledWith(
        expect.objectContaining({ term: "儿童生长发育", project_id: null, scope: "通用" }),
      ),
    );
    expect(onSaved).toHaveBeenCalled();
  });

  it("错写和也叫分两栏提交；新词条只能选公共或项目", async () => {
    const apiClient = client();
    render(
      <GlossaryTermModal apiClient={apiClient} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} />,
    );

    expect(screen.queryByRole("button", { name: "其他范围" })).toBeNull();
    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), {
      target: { value: "病例报告表" },
    });
    fireEvent.change(screen.getByLabelText("别名内容"), { target: { value: "病历报告表" } });
    fireEvent.keyDown(screen.getByLabelText("别名内容"), { key: "Enter" });
    fireEvent.change(screen.getByLabelText("也叫内容"), { target: { value: "CRF" } });
    fireEvent.keyDown(screen.getByLabelText("也叫内容"), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "项目" }));
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
    const onSaved = vi.fn();
    render(
      <GlossaryTermModal apiClient={apiClient} onClose={vi.fn()} onSaved={onSaved} projects={projects} />,
    );

    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), { target: { value: "CRF" } });
    fireEvent.change(screen.getByLabelText("别名内容"), { target: { value: "CRV" } });
    fireEvent.keyDown(screen.getByLabelText("别名内容"), { key: "Enter" });
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
    expect(onSaved).toHaveBeenCalledWith("已把『CRF』改成公共词，并合并了错写");
  });

  it("公共词典里已有时只给「把新错写加到那条」", async () => {
    const conflict = { term_id: "gt-1", term: "随访", project_id: null, project_name: null, aliases: ["随方", "随仿"], also: [] };
    const apiClient = client({
      createGlossaryTerm: vi.fn().mockRejectedValue(new ApiError("「随访」已在 公共 词典", 409, { conflict })),
      mergeGlossaryTerm: vi.fn().mockResolvedValue({}),
    });
    render(<GlossaryTermModal apiClient={apiClient} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} />);

    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), { target: { value: "随访" } });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));
    expect(await screen.findByText("『随访』已在 公共 词典（错写：随方、随仿）")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "改成公共词并合并错写" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "把新错写加到那条" }));
    await waitFor(() =>
      expect(apiClient.mergeGlossaryTerm).toHaveBeenCalledWith("gt-1", { aliases: [], also: [], make_public: false }),
    );
  });

  it("归属「项目」未选具体项目时禁用提交", () => {
    render(
      <GlossaryTermModal apiClient={client()} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} />,
    );

    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), {
      target: { value: "生长曲线" },
    });
    fireEvent.click(screen.getByRole("button", { name: "项目" }));

    expect(screen.getByRole("button", { name: "加入词典" })).toBeDisabled();
  });

  it("编辑已挂项目的术语：归属回填为「项目」并预选中原项目", () => {
    const term: GlossaryTerm = {
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
    render(
      <GlossaryTermModal apiClient={client()} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} term={term} />,
    );

    expect(screen.getByRole("button", { name: "项目" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("选择项目")).toHaveValue("project-1");
  });

  it("别名输入框：中文输入法组合态下按 Enter 不提前提交别名（新增术语回归）", () => {
    render(
      <GlossaryTermModal apiClient={client()} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} />,
    );
    const aliasInput = screen.getByLabelText("别名内容");

    // 组合态：模拟用户还在敲拼音候选字时按下 Enter 确认候选——不应该被当成提交别名。
    fireEvent.change(aliasInput, { target: { value: "erbaoke" } });
    fireEvent.keyDown(aliasInput, { key: "Enter", isComposing: true, keyCode: 229 });
    expect(screen.queryByText("erbaoke")).toBeNull();
    expect(aliasInput).toHaveValue("erbaoke");

    // 真正打完字后再按 Enter：正常提交为别名 chip，并清空草稿。
    fireEvent.change(aliasInput, { target: { value: "儿保科" } });
    fireEvent.keyDown(aliasInput, { key: "Enter" });
    expect(screen.getByText("儿保科")).toBeTruthy();
    expect(aliasInput).toHaveValue("");
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
    render(
      <GlossaryTermModal apiClient={apiClient} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} term={term} />,
    );

    expect(screen.getByRole("button", { name: "旧分组「儿科」" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(apiClient.updateGlossaryTerm).toHaveBeenCalledWith(
        "term-2",
        expect.objectContaining({ project_id: null, scope: "儿科" }),
      ),
    );
  });
});
