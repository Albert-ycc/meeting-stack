import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import type { ApiClient } from "../api";
import type { MaterialBrowsePayload } from "../types";

function payloadAt(path: string, parent: string | null, dirs: string[]): MaterialBrowsePayload {
  return {
    base: "/Volumes/资料盘",
    path,
    parent,
    breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
    dirs: dirs.map((name) => ({ name, path: `${path}/${name}` })),
  };
}

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    browseMaterials: vi.fn().mockResolvedValue(payloadAt("/Volumes/资料盘", null, ["蓝鲸云", "黄金"])),
    ...overrides,
  } as unknown as ApiClient;
}

describe("MaterialRootPickerModal", () => {
  it("点行选中后确定按钮才可用，回传选中的路径", async () => {
    const apiClient = makeClient();
    const onConfirm = vi.fn();
    render(<MaterialRootPickerModal apiClient={apiClient} onClose={vi.fn()} onConfirm={onConfirm} />);

    expect(await screen.findByText("蓝鲸云")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确定" })).toBeDisabled();

    await userEvent.click(screen.getByText("蓝鲸云"));
    expect(screen.getByRole("button", { name: "确定" })).toBeEnabled();
    expect(screen.getByText("/Volumes/资料盘/蓝鲸云")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确定" }));
    expect(onConfirm).toHaveBeenCalledWith("/Volumes/资料盘/蓝鲸云");
  });

  it("点行尾「›」进入下一级，不算选中", async () => {
    const browseMaterials = vi
      .fn()
      .mockResolvedValueOnce(payloadAt("/Volumes/资料盘", null, ["蓝鲸云"]))
      .mockResolvedValueOnce(
        payloadAt("/Volumes/资料盘/蓝鲸云", "/Volumes/资料盘", ["星河随访系统"]),
      );
    const apiClient = makeClient({ browseMaterials } as Partial<ApiClient>);
    render(<MaterialRootPickerModal apiClient={apiClient} onClose={vi.fn()} onConfirm={vi.fn()} />);

    await screen.findByText("蓝鲸云");
    await userEvent.click(screen.getByRole("button", { name: "进入 蓝鲸云" }));

    expect(await screen.findByText("星河随访系统")).toBeInTheDocument();
    expect(browseMaterials).toHaveBeenLastCalledWith("/Volumes/资料盘/蓝鲸云");
    expect(screen.getByRole("button", { name: "确定" })).toBeDisabled();
  });

  it("返回上一级按父路径重新加载", async () => {
    const browseMaterials = vi
      .fn()
      .mockResolvedValueOnce(
        payloadAt("/Volumes/资料盘/蓝鲸云", "/Volumes/资料盘", ["星河随访系统"]),
      )
      .mockResolvedValueOnce(payloadAt("/Volumes/资料盘", null, ["蓝鲸云"]));
    const apiClient = makeClient({ browseMaterials } as Partial<ApiClient>);
    render(<MaterialRootPickerModal apiClient={apiClient} onClose={vi.fn()} onConfirm={vi.fn()} />);

    await screen.findByText("星河随访系统");
    await userEvent.click(screen.getByRole("button", { name: "‹ 返回上一级" }));

    expect(await screen.findByText("蓝鲸云")).toBeInTheDocument();
    expect(browseMaterials).toHaveBeenLastCalledWith("/Volumes/资料盘");
  });

  it("目录读取失败时显示错误提示", async () => {
    const apiClient = makeClient({ browseMaterials: vi.fn().mockRejectedValue(new Error("boom")) } as Partial<ApiClient>);
    render(<MaterialRootPickerModal apiClient={apiClient} onClose={vi.fn()} onConfirm={vi.fn()} />);

    expect(await screen.findByText("目录读取失败")).toBeInTheDocument();
  });

  it("点取消关闭弹窗", async () => {
    const onClose = vi.fn();
    render(<MaterialRootPickerModal apiClient={makeClient()} onClose={onClose} onConfirm={vi.fn()} />);

    await screen.findByText("蓝鲸云");
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("D27：上层挂根目录失败时就地显示后端原因，弹窗不关", async () => {
    render(
      <MaterialRootPickerModal
        apiClient={makeClient()}
        error="不能选择隐藏目录"
        onClose={vi.fn()}
        onConfirm={vi.fn()}
      />,
    );

    await screen.findByText("蓝鲸云");
    expect(screen.getByRole("alert")).toHaveTextContent("不能选择隐藏目录");
    // 弹窗本身还在（没有因为报错被卸载）
    expect(screen.getByRole("dialog", { name: "添加材料根目录" })).toBeInTheDocument();
  });

  it("busy 为 true 时确定按钮禁用并显示处理中，避免重复提交", async () => {
    render(<MaterialRootPickerModal apiClient={makeClient()} busy onClose={vi.fn()} onConfirm={vi.fn()} />);

    await userEvent.click(await screen.findByText("蓝鲸云"));
    expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
  });
});
