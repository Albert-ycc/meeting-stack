import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import { ApiError, type ApiClient } from "../api";
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

  it("可以直接选当前打开的这个文件夹", async () => {
    const browseMaterials = vi
      .fn()
      .mockResolvedValueOnce(payloadAt("/Volumes/资料盘", null, ["项目"]))
      .mockResolvedValueOnce(payloadAt("/Volumes/资料盘/项目", "/Volumes/资料盘", ["云图AI", "北辰"]));
    const onConfirm = vi.fn();
    render(
      <MaterialRootPickerModal
        apiClient={makeClient({ browseMaterials } as Partial<ApiClient>)}
        confirmLabel="设为项目总文件夹"
        description="选放项目文件夹的那一层"
        onClose={vi.fn()}
        onConfirm={onConfirm}
        title="选项目总文件夹"
      />,
    );

    expect(screen.getByRole("dialog", { name: "选项目总文件夹" })).toBeInTheDocument();
    expect(screen.getByText("选放项目文件夹的那一层")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "进入 项目" }));
    await screen.findByText("云图AI");
    expect(screen.getByRole("button", { name: "设为项目总文件夹" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "选当前文件夹" }));
    expect(screen.getByRole("button", { name: "选当前文件夹" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "设为项目总文件夹" }));
    expect(onConfirm).toHaveBeenCalledWith("/Volumes/资料盘/项目");
  });

  it("资料盘没插时照原文显示服务端的原因", async () => {
    const browseMaterials = vi
      .fn()
      .mockRejectedValue(new ApiError("资料盘未连接，插上后再选", 409, { detail: "资料盘未连接，插上后再选" }));
    render(
      <MaterialRootPickerModal
        apiClient={makeClient({ browseMaterials } as Partial<ApiClient>)}
        onClose={vi.fn()}
        onConfirm={vi.fn()}
      />,
    );

    expect(await screen.findByText("资料盘未连接，插上后再选")).toBeInTheDocument();
    expect(screen.queryByText("目录读取失败")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "选当前文件夹" })).not.toBeInTheDocument();
  });

  it("底部的文字选项交给上层处理", async () => {
    const onSelect = vi.fn();
    render(
      <MaterialRootPickerModal
        apiClient={makeClient()}
        extraOption={{ label: "不建了，以后自己挂文件夹", onSelect }}
        onClose={vi.fn()}
        onConfirm={vi.fn()}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "不建了，以后自己挂文件夹" }));
    expect(onSelect).toHaveBeenCalled();
  });

  it("按对话框约定：Esc 和点背景都关，点弹窗里面不关", async () => {
    const onClose = vi.fn();
    render(<MaterialRootPickerModal apiClient={makeClient()} onClose={onClose} onConfirm={vi.fn()} />);

    await userEvent.click(await screen.findByText("蓝鲸云"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape", isComposing: true });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    const overlay = screen.getByRole("dialog").parentElement!;
    await userEvent.click(overlay);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("取径器盖在别的容器里面（关系图的节点面板）：焦点在取径器里按 Esc，外面容器自己的 Esc 处理不会被触发", async () => {
    const onClose = vi.fn();
    const onAncestorKeyDown = vi.fn();
    render(
      <div onKeyDown={onAncestorKeyDown}>
        <MaterialRootPickerModal apiClient={makeClient()} onClose={onClose} onConfirm={vi.fn()} />
      </div>,
    );
    await userEvent.click(await screen.findByText("蓝鲸云"));

    // 对照：别的键外面的容器看得到
    await userEvent.keyboard("{ArrowDown}");
    expect(onAncestorKeyDown).toHaveBeenCalledWith(expect.objectContaining({ key: "ArrowDown" }));

    await userEvent.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onAncestorKeyDown).not.toHaveBeenCalledWith(expect.objectContaining({ key: "Escape" }));
  });

  it("焦点不在取径器里（点「›」进下一级后，被点的那个按钮被换掉了）：Esc 照样关", async () => {
    const onClose = vi.fn();
    render(<MaterialRootPickerModal apiClient={makeClient()} onClose={onClose} onConfirm={vi.fn()} />);
    await screen.findByText("蓝鲸云");

    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("busy 为 true 时确定按钮禁用并显示处理中，避免重复提交", async () => {
    render(<MaterialRootPickerModal apiClient={makeClient()} busy onClose={vi.fn()} onConfirm={vi.fn()} />);

    await userEvent.click(await screen.findByText("蓝鲸云"));
    expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
  });
  it("进下一级还没回来就点「返回上一级」：慢回来的那一级不盖掉后点的", async () => {
    let resolveDeeper!: (value: MaterialBrowsePayload) => void;
    const browseMaterials = vi
      .fn()
      .mockResolvedValueOnce(payloadAt("/Volumes/资料盘/蓝鲸云", "/Volumes/资料盘", ["星河随访系统"]))
      .mockReturnValueOnce(new Promise<MaterialBrowsePayload>((resolve) => (resolveDeeper = resolve)))
      .mockResolvedValueOnce(payloadAt("/Volumes/资料盘", null, ["蓝鲸云", "黄金"]));
    const apiClient = makeClient({ browseMaterials } as Partial<ApiClient>);
    render(<MaterialRootPickerModal apiClient={apiClient} onClose={vi.fn()} onConfirm={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "进入 星河随访系统" }));
    await userEvent.click(screen.getByRole("button", { name: "‹ 返回上一级" }));
    expect(await screen.findByText("黄金")).toBeInTheDocument();

    await act(async () => {
      resolveDeeper(payloadAt("/Volumes/资料盘/蓝鲸云/星河随访系统", "/Volumes/资料盘/蓝鲸云", ["随访表单"]));
    });

    expect(screen.getByText("黄金")).toBeInTheDocument();
    expect(screen.queryByText("随访表单")).toBeNull();
  });
});
