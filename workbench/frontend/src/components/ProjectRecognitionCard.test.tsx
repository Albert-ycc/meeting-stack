import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectRecognitionCard } from "./ProjectRecognitionCard";
import type { ApiClient } from "../api";
import type { ProjectRecognitionProfile } from "../types";

const PROFILE: ProjectRecognitionProfile = {
  also_names: [
    { name: "云图", source: "manual" },
    { name: "云图用药", source: "former" },
  ],
  folder_names: ["云图科研用药"],
  cue_terms: { total: 12, cue: 5 },
  auto_30d: 9,
  corrected_30d: 1,
};

function renderCard(overrides: Partial<ApiClient> = {}, canWrite = true) {
  const updateProject = vi.fn().mockResolvedValue({});
  const onChanged = vi.fn();
  const apiClient = { updateProject, ...overrides } as unknown as ApiClient;
  render(
    <ProjectRecognitionCard
      apiClient={apiClient}
      canWrite={canWrite}
      onChanged={onChanged}
      profile={PROFILE}
      projectId="p1"
      projectName="云图科研用药"
    />,
  );
  return { updateProject: (overrides.updateProject ?? updateProject) as ReturnType<typeof vi.fn>, onChanged };
}

describe("ProjectRecognitionCard", () => {
  it("列出名称、也叫（曾用名带小字）、文件夹名、项目词和最近 30 天归属", () => {
    renderCard();

    const card = screen.getByRole("region", { name: "系统怎么认出这个项目" });
    expect(card).toHaveTextContent("云图科研用药");
    expect(screen.getByText("曾用名")).toBeInTheDocument();
    expect(screen.getByText("自动算作叫法")).toBeInTheDocument();
    expect(card).toHaveTextContent("12 条，其中 5 条参与识别");
    expect(card).toHaveTextContent("自动归入 9 场、你改走 1 场");
  });

  it("添加叫法：整份叫法列表一起提交，成功后通知刷新", async () => {
    const { updateProject, onChanged } = renderCard();

    await userEvent.click(screen.getByRole("button", { name: "＋ 添加叫法" }));
    await userEvent.type(screen.getByRole("textbox", { name: "新的叫法" }), "云图EDC{Enter}");

    expect(updateProject).toHaveBeenCalledWith("p1", { also_names: ["云图", "云图用药", "云图EDC"] });
    expect(onChanged).toHaveBeenCalled();
  });

  it("纯数字或太短就地提示，不提交；两个字的给黄色提醒", async () => {
    const { updateProject } = renderCard();

    await userEvent.click(screen.getByRole("button", { name: "＋ 添加叫法" }));
    const input = screen.getByRole("textbox", { name: "新的叫法" });
    await userEvent.type(input, "2026");
    expect(screen.getByRole("alert")).toHaveTextContent("叫法要 2–20 个字，不能是纯数字");
    expect(screen.getByRole("button", { name: "添加" })).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "云景");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/两个字的叫法容易撞车/)).toBeInTheDocument();
    expect(updateProject).not.toHaveBeenCalled();
  });

  it("后端拦下（撞别的项目）时显示原因", async () => {
    const updateProject = vi.fn().mockRejectedValue(new Error("「数据」已经是「数据中台」的叫法，一个叫法只能指向一个项目"));
    renderCard({ updateProject } as Partial<ApiClient>);

    await userEvent.click(screen.getByRole("button", { name: "＋ 添加叫法" }));
    await userEvent.type(screen.getByRole("textbox", { name: "新的叫法" }), "数据{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("一个叫法只能指向一个项目");
    expect(screen.getByRole("textbox", { name: "新的叫法" })).toHaveValue("数据");
  });

  it("删掉一个叫法", async () => {
    const { updateProject } = renderCard();

    await userEvent.click(screen.getByRole("button", { name: "删掉叫法 云图" }));

    expect(updateProject).toHaveBeenCalledWith("p1", { also_names: ["云图用药"] });
  });

  it("没有写权限时只读", () => {
    renderCard({}, false);

    expect(screen.queryByRole("button", { name: "＋ 添加叫法" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删掉叫法 云图" })).not.toBeInTheDocument();
  });
});
