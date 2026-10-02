import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MaterialPreviewDrawer } from "./MaterialPreview";
import { TaskDrawer } from "./TaskDrawer";
import type { ApiClient, RelationQuestion } from "../api";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import type { TaskDetail, TaskStatus } from "../types";

function makeTask(status: TaskStatus): TaskDetail {
  return {
    id: "t1",
    title: "确认样品发放口径",
    detail: "",
    status,
    origin: "ai",
    assignee: "me",
    meeting_id: "m1",
    project_id: null,
    anchor_ms: null,
    anchor_quote: null,
    status_changed_at: "2026-08-20T02:00:00Z",
    created_at: "2026-08-20T02:00:00Z",
    updated_at: "2026-08-20T02:00:00Z",
    meeting_title: "样品项目周会",
    project_name: null,
    project_color: null,
    stall_days: 0,
    stall_since: null,
    stalled: false,
    events: [],
    deliverables: [],
  };
}

function renderDrawer(status: TaskStatus, overrides: Partial<ApiClient> = {}) {
  const apiClient = {
    task: vi.fn().mockResolvedValue(makeTask(status)),
    confirmTask: vi.fn().mockResolvedValue(makeTask("confirmed")),
    setTaskStatus: vi.fn().mockResolvedValue(makeTask("done")),
    updateTask: vi.fn().mockResolvedValue(makeTask(status)),
    ...overrides,
  } as unknown as ApiClient;
  render(
    <TaskDrawer
      apiClient={apiClient}
      canWrite
      onChanged={vi.fn()}
      onClose={vi.fn()}
      onOpenMeeting={vi.fn()}
      taskId="t1"
    />,
  );
  return apiClient;
}

describe("TaskDrawer 页脚主操作按状态切换", () => {
  it("待确认任务主按钮是「确认」而非「标记完成」", async () => {
    const apiClient = renderDrawer("pending_confirm");

    expect(await screen.findByRole("button", { name: "确认" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "标记完成" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确认" }));
    expect(apiClient.confirmTask).toHaveBeenCalledWith("t1", {});
  });

  it("进行中任务主按钮是「标记完成」", async () => {
    const apiClient = renderDrawer("in_progress");

    expect(await screen.findByRole("button", { name: "标记完成" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "标记完成" }));
    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t1", "done");
  });

  it("已取消任务（closed）的主操作与取消按钮被禁用", async () => {
    renderDrawer("cancelled");

    expect(await screen.findByRole("button", { name: "转给 AI 执行" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "标记完成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消任务" })).toBeDisabled();
  });

  it("父级写进行中时禁用所有页脚写按钮", async () => {
    const apiClient = {
      task: vi.fn().mockResolvedValue(makeTask("in_progress")),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        parentBusy
        taskId="t1"
      />,
    );

    expect(await screen.findByRole("button", { name: "标记完成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "转给 AI 执行" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消任务" })).toBeDisabled();
  });
});

describe("TaskDrawer 所属需求", () => {
  it("挂了需求时显示优先级胶囊与可点链接，点击跳转", async () => {
    const onOpenRequirement = vi.fn();
    const apiClient = {
      task: vi.fn().mockResolvedValue({
        ...makeTask("in_progress"),
        requirement_id: "req-1",
        requirement_title: "北辰仓快递配送",
        requirement_priority: "P0",
        requirement_status: "active",
      }),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        taskId="t1"
      />,
    );

    expect(await screen.findByText("P0")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "北辰仓快递配送" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-1");
  });

  it("没挂需求时不显示所属需求行", async () => {
    renderDrawer("in_progress");
    await screen.findByText("确认样品发放口径");
    expect(screen.queryByText("P0")).not.toBeInTheDocument();
  });

  it("没传 onOpenRequirement 时需求名不是可点按钮", async () => {
    const apiClient = {
      task: vi.fn().mockResolvedValue({
        ...makeTask("in_progress"),
        requirement_id: "req-1",
        requirement_title: "北辰仓快递配送",
        requirement_priority: "P0",
        requirement_status: "active",
      }),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        taskId="t1"
      />,
    );

    expect(await screen.findByText("北辰仓快递配送")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "北辰仓快递配送" })).not.toBeInTheDocument();
  });
});

describe("TaskDrawer 文件交付物（3g）", () => {
  const base = { task_id: "t1", title: "", note: "", created_at: "2026-09-27T02:00:00Z" };

  function renderWithDeliverables(deliverables: TaskDetail["deliverables"], onOpenPreview?: (fileId: number) => void) {
    const apiClient = {
      task: vi.fn().mockResolvedValue({ ...makeTask("in_progress"), deliverables }),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenPreview={onOpenPreview}
        taskId="t1"
      />,
    );
  }

  it("有 file_id 时是按钮，点了打开预览抽屉；找不到时标「找不到这个文件了」", async () => {
    const onOpenPreview = vi.fn();
    renderWithDeliverables(
      [
        { ...base, id: 1, kind: "file", url: "/Volumes/资料盘/云图AI/交付/定稿.pdf", file_id: 42, name: "定稿.pdf", gone: false },
        { ...base, id: 2, kind: "file", url: "/Volumes/资料盘/云图AI/旧稿.docx", file_id: 43, name: "旧稿.docx", gone: true },
        { ...base, id: 3, kind: "link", url: "https://example.com/doc", title: "在线文档" },
      ],
      onOpenPreview,
    );

    await userEvent.click(await screen.findByRole("button", { name: "定稿.pdf" }));
    expect(onOpenPreview).toHaveBeenCalledWith(42);
    expect(screen.getByText("找不到这个文件了")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "在线文档" })).toHaveAttribute("href", "https://example.com/doc");
    expect(screen.queryByRole("link", { name: /定稿/ })).not.toBeInTheDocument();
  });

  it("没有 file_id（旧数据）时显示路径和［复制路径］，不做成打不开的链接", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const path = "/Volumes/资料盘/云图AI/交付/定稿.pdf";
    renderWithDeliverables([{ ...base, id: 1, kind: "file", url: path, file_id: null }], vi.fn());

    expect(await screen.findByText(path)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "复制路径" }));
    expect(writeText).toHaveBeenCalledWith(path);
    expect(await screen.findByText("路径已复制")).toBeInTheDocument();
  });
});

describe("TaskDrawer 在问的产出（4e）", () => {
  const PRODUCED: RelationQuestion = {
    relation_id: 61,
    kind: "produced",
    text: "会后 3 天新增在『能耗看板/』",
    ask: "是任务『写一版方案』的交付物吗？",
    task: { id: "t1", title: "写一版方案", status: "in_progress" },
    file: { id: 930, name: "能耗看板方案.key", folder: "能耗看板/" },
    words: ["能耗看板"],
    answers: ["yes", "no"],
  };
  const FLAGS = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

  function renderWithSuggestions(
    {
      canWrite = true,
      flags = true,
      overrides = {},
    }: { canWrite?: boolean; flags?: boolean; overrides?: Record<string, unknown> } = {},
  ) {
    const deliverable = {
      id: 17, task_id: "t1", kind: "file", url: "/材料/能耗看板/能耗看板方案.key", title: "能耗看板方案.key", note: "",
      created_at: "2026-09-27T02:00:00Z", file_id: 930, name: "能耗看板方案.key", gone: false,
    };
    const task = vi
      .fn()
      .mockResolvedValueOnce({ ...makeTask("in_progress"), suggestions: [PRODUCED] })
      .mockResolvedValue({ ...makeTask("in_progress"), suggestions: [], deliverables: [deliverable] });
    const apiClient = {
      task,
      answerRelation: vi.fn().mockResolvedValue({ relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString(), deliverable_id: 17 }),
      undoRelation: vi.fn().mockResolvedValue({ relation: {}, removed_deliverable_id: 17 }),
      ...overrides,
    } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
    const onOpenPreview = vi.fn();
    const onChanged = vi.fn();
    const drawer = (
      <TaskDrawer
        apiClient={apiClient}
        canWrite={canWrite}
        onChanged={onChanged}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenPreview={onOpenPreview}
        taskId="t1"
      />
    );
    render(flags ? <LinksFlagsContext.Provider value={FLAGS}>{drawer}</LinksFlagsContext.Provider> : drawer);
    return { apiClient, onOpenPreview, onChanged };
  }

  it("问题在交付物列表上面：标题、文件名和证据，［是］以后重取，交付物出现，提示带［撤销］", async () => {
    const { apiClient, onOpenPreview, onChanged } = renderWithSuggestions();
    const block = await screen.findByRole("group", { name: PRODUCED.text });
    expect(within(block).getByText("是这条任务的交付物吗？")).toBeInTheDocument();
    expect(within(block).getByText(/会后 3 天新增在『能耗看板\/』/)).toBeInTheDocument();
    // 列表为空时下面照旧写「还没有登记交付物」，问题块在它上面
    const empty = screen.getByText("还没有登记交付物");
    expect(block.compareDocumentPosition(empty) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(within(block).getByRole("button", { name: "能耗看板方案.key" }));
    expect(onOpenPreview).toHaveBeenCalledWith(930);

    await userEvent.click(within(block).getByRole("button", { name: "是" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(61, { answer: "yes" });
    // 前端不另发登记交付物的请求；回答以后重取任务
    expect(apiClient.addDeliverable).toBeUndefined();
    await waitFor(() => expect(apiClient.task).toHaveBeenCalledTimes(2));
    expect(onChanged).toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "能耗看板方案.key" })).toHaveClass("task-drawer__deliverable-file");
    // 收成的一行和提示都带［撤销］
    expect(screen.getAllByText("已登记为『写一版方案』的交付物").length).toBeGreaterThan(0);
    const undoButtons = screen.getAllByRole("button", { name: "撤销" });
    await userEvent.click(undoButtons[0]);
    expect(apiClient.undoRelation).toHaveBeenCalledWith(61);
  });

  it("canWrite 为假只显示问题、不给按钮；旧后台（没有 linksFlags、没有 answerRelation）不画", async () => {
    const view = renderWithSuggestions({ canWrite: false });
    const block = await screen.findByRole("group", { name: PRODUCED.text });
    expect(within(block).queryByRole("button", { name: "是" })).toBeNull();
    expect(within(block).queryByRole("button", { name: "不是" })).toBeNull();
    cleanup();

    renderWithSuggestions({ flags: false });
    expect(await screen.findByText("还没有登记交付物")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: PRODUCED.text })).toBeNull();
    cleanup();

    renderWithSuggestions({ overrides: { answerRelation: undefined } });
    expect(await screen.findByText("还没有登记交付物")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: PRODUCED.text })).toBeNull();
    expect(view).toBeTruthy();
  });
});

describe("TaskDrawer 上再盖登记交付物的弹窗", () => {
  it("Esc 只关弹窗，任务抽屉留着；弹窗关了再按才关抽屉", async () => {
    const onCloseTask = vi.fn();
    const apiClient = { task: vi.fn().mockResolvedValue(makeTask("confirmed")) } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={onCloseTask}
        onOpenMeeting={vi.fn()}
        taskId="t1"
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: /登记交付物/ }));
    const modal = await screen.findByRole("dialog", { name: /交付物/ });

    fireEvent.keyDown(window, { key: "Escape" });
    expect(modal).not.toBeInTheDocument();
    expect(onCloseTask).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "任务详情" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCloseTask).toHaveBeenCalledTimes(1);
  });
});

describe("TaskDrawer 上再盖一层抽屉", () => {
  it("材料预览盖在任务抽屉上：Esc 只关最上面的预览，预览关了再按才关任务抽屉", async () => {
    const onCloseTask = vi.fn();
    const apiClient = {
      task: vi.fn().mockResolvedValue(makeTask("confirmed")),
      getMaterialPreview: vi.fn().mockReturnValue(new Promise(() => undefined)),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={onCloseTask}
        onOpenMeeting={vi.fn()}
        taskId="t1"
      />,
    );
    await screen.findByRole("dialog", { name: "任务详情" });
    const onClosePreview = vi.fn();
    const preview = render(
      <MaterialPreviewDrawer
        apiClient={apiClient}
        canReveal
        fileId={7}
        isMobile={false}
        onClose={onClosePreview}
        onOpenInGraph={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenTask={vi.fn()}
      />,
    );
    await screen.findByRole("dialog", { name: "材料预览" });

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClosePreview).toHaveBeenCalledTimes(1);
    expect(onCloseTask).not.toHaveBeenCalled();

    preview.unmount();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCloseTask).toHaveBeenCalledTimes(1);
  });
});
