import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GlossaryTermModal } from "./GlossaryTermModal";
import type { ApiClient } from "../api";
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

  it("归属「其他范围」：提交自定义 scope，project_id 为 null", async () => {
    const apiClient = client();
    render(
      <GlossaryTermModal apiClient={apiClient} onClose={vi.fn()} onSaved={vi.fn()} projects={projects} />,
    );

    fireEvent.change(screen.getByPlaceholderText("权威写法，例如：儿童生长发育"), {
      target: { value: "儿保科" },
    });
    fireEvent.click(screen.getByRole("button", { name: "其他范围" }));
    fireEvent.change(screen.getByPlaceholderText("新范围名称，例如：儿科"), {
      target: { value: "儿科" },
    });
    fireEvent.click(screen.getByRole("button", { name: "加入词典" }));

    await waitFor(() =>
      expect(apiClient.createGlossaryTerm).toHaveBeenCalledWith(
        expect.objectContaining({ project_id: null, scope: "儿科" }),
      ),
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

    expect(screen.getByRole("button", { name: "其他范围" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(apiClient.updateGlossaryTerm).toHaveBeenCalledWith(
        "term-2",
        expect.objectContaining({ project_id: null, scope: "儿科" }),
      ),
    );
  });
});
