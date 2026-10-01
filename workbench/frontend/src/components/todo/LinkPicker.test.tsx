import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import type { LinkOption, RequirementOptionsPayload } from "../../types";
import { LinkPicker } from "./LinkPicker";

// CVM 云讲堂 09-29「260929 云课堂直播运营问题对齐」：库里的需求名、候选名（口径书 T02）
const SIX: LinkOption = {
  kind: "requirement",
  id: "requirement-six",
  title: "直播间运营六项修正",
  priority: "P1",
  project_id: "project-592ef4b19a60442a",
  project_name: "CVM 云讲堂",
  meeting_id: null,
};
const EXPORT: LinkOption = {
  kind: "candidate",
  id: "candidate-export",
  title: "科室会预约后台导出",
  priority: null,
  project_id: "project-592ef4b19a60442a",
  project_name: "CVM 云讲堂",
  meeting_id: "vm-20260929-192637-f3947874",
};

function payload(overrides: Partial<RequirementOptionsPayload> = {}): RequirementOptionsPayload {
  return {
    task_id: "task-c0e4a87bea824645bdaf2d1fdd48d731",
    can_link_candidates: true,
    recommended: [{ ...EXPORT, reason: "same_meeting" }],
    default: { ...EXPORT, reason: "same_meeting" },
    options: [SIX, EXPORT],
    current: null,
    ...overrides,
  };
}

function setup(taskRequirementOptions = vi.fn().mockResolvedValue(payload())) {
  const onPick = vi.fn();
  const onClose = vi.fn();
  const onLoaded = vi.fn();
  render(
    <LinkPicker
      apiClient={{ taskRequirementOptions } as unknown as ApiClient}
      onClose={onClose}
      onLoaded={onLoaded}
      onPick={onPick}
      projectName="CVM 云讲堂"
      taskId="task-c0e4a87bea824645bdaf2d1fdd48d731"
    />,
  );
  return { onPick, onClose, onLoaded, taskRequirementOptions };
}

describe("LinkPicker", () => {
  it("推荐在前、范围里其余的在后，底部是不挂需求", async () => {
    const { onPick, onLoaded } = setup();

    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "候选科室会预约后台导出同场会",
      "直播间运营六项修正P1",
    ]);
    expect(screen.getByText("CVM 云讲堂 · 进行中的需求和待认领候选")).toBeInTheDocument();
    expect(onLoaded).toHaveBeenCalledWith(payload());

    await userEvent.click(options[1]);
    expect(onPick).toHaveBeenLastCalledWith(SIX);
    await userEvent.click(screen.getByRole("button", { name: "不挂需求" }));
    expect(onPick).toHaveBeenLastCalledWith(null);
  });

  it("搜索时按名字向后端查，不再单列推荐；Esc 收起", async () => {
    const taskRequirementOptions = vi
      .fn()
      .mockResolvedValueOnce(payload())
      .mockResolvedValue(payload({ options: [EXPORT] }));
    const { onClose } = setup(taskRequirementOptions);
    await screen.findAllByRole("option");

    await userEvent.type(screen.getByRole("textbox", { name: "搜索需求" }), "导出");

    await waitFor(() =>
      expect(taskRequirementOptions).toHaveBeenLastCalledWith("task-c0e4a87bea824645bdaf2d1fdd48d731", "导出"),
    );
    await waitFor(() => expect(screen.queryByText("推荐")).not.toBeInTheDocument());
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["候选科室会预约后台导出"]);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("Esc 收起后焦点回到打开它的按钮；Tab 离开就收起", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "挂到需求";
    document.body.appendChild(trigger);
    trigger.focus();
    const { onClose } = setup();
    await screen.findAllByRole("option");

    await userEvent.keyboard("{Escape}");
    expect(document.activeElement).toBe(trigger);

    const outside = document.createElement("button");
    document.body.appendChild(outside);
    screen.getByRole("textbox", { name: "搜索需求" }).focus();
    onClose.mockClear();
    outside.focus();
    expect(onClose).toHaveBeenCalled();
    trigger.remove();
    outside.remove();
  });

  it("已确认任务只列进行中需求；没有可挂的给出提示", async () => {
    setup(vi.fn().mockResolvedValue(payload({ can_link_candidates: false, recommended: [], default: null, options: [] })));

    expect(await screen.findByText("这里还没有可挂的需求")).toBeInTheDocument();
    expect(screen.getByText("CVM 云讲堂 · 进行中的需求")).toBeInTheDocument();
  });
});
