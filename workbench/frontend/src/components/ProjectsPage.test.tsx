import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectsPage } from "./ProjectsPage";

describe("ProjectsPage classification controls", () => {
  it("creates a project and tag from the desktop ledger", async () => {
    const onCreateProject = vi.fn().mockResolvedValue(undefined);
    const onCreateTag = vi.fn().mockResolvedValue(undefined);
    render(
      <ProjectsPage
        canEdit
        meetings={[]}
        onCreateProject={onCreateProject}
        onCreateTag={onCreateTag}
        onOpenProject={vi.fn()}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.type(screen.getByLabelText("项目名称"), "蓝鲸云");
    await userEvent.click(screen.getByRole("button", { name: "新建项目" }));
    expect(onCreateProject).toHaveBeenCalledWith("蓝鲸云", expect.stringMatching(/^#/));

    await userEvent.type(screen.getByLabelText("标签名称"), "需复盘");
    await userEvent.click(screen.getByRole("button", { name: "新建标签" }));
    expect(onCreateTag).toHaveBeenCalledWith("需复盘", expect.stringMatching(/^#/));
  });

  it("does not render classification writes on mobile", () => {
    render(
      <ProjectsPage
        canEdit={false}
        meetings={[]}
        onCreateProject={vi.fn()}
        onCreateTag={vi.fn()}
        onOpenProject={vi.fn()}
        projects={[]}
        tags={[]}
      />,
    );

    expect(screen.queryByRole("button", { name: "新建项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建标签" })).not.toBeInTheDocument();
  });

  it("does not erase a newer project name when an older create request completes", async () => {
    let resolveCreate!: () => void;
    const onCreateProject = vi.fn().mockReturnValue(new Promise<void>((resolve) => { resolveCreate = resolve; }));
    render(<ProjectsPage canEdit meetings={[]} onCreateProject={onCreateProject} onCreateTag={vi.fn()} onOpenProject={vi.fn()} projects={[]} tags={[]} />);
    const input = screen.getByLabelText("项目名称");
    await userEvent.type(input, "旧项目");
    await userEvent.click(screen.getByRole("button", { name: "新建项目" }));
    await userEvent.clear(input);
    await userEvent.type(input, "下一个项目");
    resolveCreate();
    expect(await screen.findByText("项目已创建")).toBeInTheDocument();
    expect(input).toHaveValue("下一个项目");
  });

  it("uses the server meeting count instead of the current meeting page", () => {
    render(
      <ProjectsPage
        canEdit={false}
        meetings={[]}
        onCreateProject={vi.fn()}
        onCreateTag={vi.fn()}
        onOpenProject={vi.fn()}
        projects={[{ id: "project-1", name: "长期项目", color: "#376f68", meeting_count: 57 }]}
        tags={[]}
      />,
    );

    expect(screen.getByText("57 场会议")).toBeInTheDocument();
  });
});
