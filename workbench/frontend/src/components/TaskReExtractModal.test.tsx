import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import { TaskReExtractModal } from "./TaskReExtractModal";

function setup(reExtractTasks: ApiClient["reExtractTasks"]) {
  const onClose = vi.fn();
  const onReExtracted = vi.fn();
  render(
    <TaskReExtractModal
      apiClient={{ reExtractTasks } as unknown as ApiClient}
      meetingId="m-1"
      meetingTitle="周会"
      onClose={onClose}
      onReExtracted={onReExtracted}
      taskCount={3}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "重新生成任务清单" }));
  return { onClose, onReExtracted };
}

describe("TaskReExtractModal", () => {
  it("抽完（done）才算成功：刷新并关弹窗", async () => {
    const { onClose, onReExtracted } = setup(vi.fn().mockResolvedValue({ status: "done" }));
    await waitFor(() => expect(onReExtracted).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("没配模型（unavailable）：弹窗留着，说明原因", async () => {
    const { onClose, onReExtracted } = setup(vi.fn().mockResolvedValue({ status: "unavailable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("任务抽取未启用（缺少模型配置）");
    expect(onReExtracted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("这次没抽成（failed）：弹窗留着，说原来的任务没动，可以再点一次", async () => {
    const { onClose, onReExtracted } = setup(vi.fn().mockResolvedValue({ status: "failed" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("这次没抽成");
    expect(screen.getByRole("alert")).toHaveTextContent("原来的任务没动");
    expect(onReExtracted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "重新生成任务清单" })).toBeEnabled();
  });

  it("扫描正在抽这场会（409）：原样显示后端的说明，弹窗留着", async () => {
    const { onClose, onReExtracted } = setup(
      vi.fn().mockRejectedValue(new ApiError("这场会正在抽任务，等这一轮抽完再重新抽取", 409)),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("这场会正在抽任务，等这一轮抽完再重新抽取");
    expect(onReExtracted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "重新生成任务清单" })).toBeEnabled();
  });
});
