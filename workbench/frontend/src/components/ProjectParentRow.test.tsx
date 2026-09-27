import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectParentRow } from "./ProjectParentRow";
import { ApiError, type ApiClient } from "../api";
import type { ProjectParentStatus, UnclaimedFolder } from "../types";

const PARENT = "/Volumes/资料盘/项目";

function status(overrides: Partial<ProjectParentStatus> = {}): ProjectParentStatus {
  return {
    path: PARENT,
    state: "online",
    reason: null,
    suggested: null,
    unclaimed: { state: "ready", folders: [], total: 0 },
    ...overrides,
  };
}

const FOLDERS: UnclaimedFolder[] = [
  {
    path: `${PARENT}/蓝鲸云`,
    name: "蓝鲸云",
    modified_at: "2026-09-20T08:00:00Z",
    kind: "new",
    action: "create",
    project_id: null,
    project_name: null,
    project_roots: [],
    checked: false,
  },
  {
    path: `${PARENT}/云图AI`,
    name: "云图AI",
    modified_at: "2026-09-21T08:00:00Z",
    kind: "exact",
    action: "mount",
    project_id: "p1",
    project_name: "云图AI",
    project_roots: [],
    checked: true,
  },
];

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    projectParent: vi.fn().mockResolvedValue(status()),
    setProjectParent: vi.fn().mockResolvedValue(status()),
    claimFolders: vi.fn(),
    declineFolder: vi.fn(),
    undeclineFolder: vi.fn(),
    browseMaterials: vi.fn().mockResolvedValue({
      base: "/Volumes/资料盘",
      path: "/Volumes/资料盘",
      parent: null,
      breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
      dirs: [{ name: "项目", path: PARENT }],
    }),
    ...overrides,
  } as unknown as ApiClient;
}

function renderRow(apiClient: ApiClient, canEdit = true) {
  const onNotice = vi.fn();
  const onProjectsChanged = vi.fn();
  const view = render(
    <ProjectParentRow apiClient={apiClient} canEdit={canEdit} onNotice={onNotice} onProjectsChanged={onProjectsChanged} />,
  );
  return { ...view, onNotice, onProjectsChanged };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ProjectParentRow 几种状态", () => {
  it("设好了：写路径和［改］；下面有没挂的写「还有 N 个」［看看］打开认领框", async () => {
    const apiClient = makeClient({
      projectParent: vi.fn().mockResolvedValue(status({ unclaimed: { state: "ready", folders: FOLDERS, total: 2 } })),
    } as Partial<ApiClient>);
    renderRow(apiClient);

    expect(await screen.findByText(PARENT)).toBeInTheDocument();
    expect(screen.getByText(/项目总文件夹：/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "改" })).toBeInTheDocument();
    expect(screen.getByText(/还有 2 个文件夹没挂到项目/)).toBeInTheDocument();
    // 不是刚设好的，不自动弹
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "看看" }));
    expect(screen.getByRole("dialog", { name: "这下面有 2 个文件夹还没挂到项目" })).toBeInTheDocument();
  });

  it("没设、有推荐：写实数，［用这个］设成推荐位置，下面有没挂的就弹认领框", async () => {
    const setProjectParent = vi
      .fn()
      .mockResolvedValue(status({ unclaimed: { state: "ready", folders: FOLDERS, total: 2 } }));
    const apiClient = makeClient({
      projectParent: vi
        .fn()
        .mockResolvedValue(
          status({ path: null, state: null, suggested: { path: PARENT, count: 10, total: 12 }, unclaimed: { state: "unset", folders: [], total: 0 } }),
        ),
      setProjectParent,
    } as Partial<ApiClient>);
    renderRow(apiClient);

    const line = await screen.findByText(/还没设项目总文件夹/);
    expect(line).toHaveTextContent(`还没设项目总文件夹。你挂过的 12 个项目文件夹里有 10 个在 ${PARENT} 下面`);
    expect(screen.getByRole("button", { name: "另选…" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "用这个" }));
    expect(setProjectParent).toHaveBeenCalledWith(PARENT);
    expect(await screen.findByRole("dialog", { name: "这下面有 2 个文件夹还没挂到项目" })).toBeInTheDocument();
  });

  it("一个都没挂过：只有［选项目总文件夹…］，在取径器里选，失败原因就地显示", async () => {
    const setProjectParent = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("不能用整个磁盘、/Volumes 或用户主目录，请选放项目文件夹的那一层", 400))
      .mockResolvedValueOnce(status());
    const apiClient = makeClient({
      projectParent: vi.fn().mockResolvedValue(status({ path: null, state: null, unclaimed: { state: "unset", folders: [], total: 0 } })),
      setProjectParent,
    } as Partial<ApiClient>);
    const { container } = renderRow(apiClient);

    const button = await screen.findByRole("button", { name: "选项目总文件夹…" });
    expect(container.querySelector(".project-parent")).toHaveTextContent(/^选项目总文件夹…$/);
    await userEvent.click(button);

    const picker = screen.getByRole("dialog", { name: "选项目总文件夹" });
    await userEvent.click(await within(picker).findByRole("button", { name: "选当前文件夹" }));
    await userEvent.click(within(picker).getByRole("button", { name: "确定" }));
    expect(setProjectParent).toHaveBeenCalledWith("/Volumes/资料盘");
    expect(await within(picker).findByRole("alert")).toHaveTextContent("不能用整个磁盘");

    await userEvent.click(within(picker).getByText("项目"));
    await userEvent.click(within(picker).getByRole("button", { name: "确定" }));
    expect(setProjectParent).toHaveBeenLastCalledWith(PARENT);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(PARENT)).toBeInTheDocument();
  });

  it("盘没插、在根目录里面、位置不能用：各一句话", async () => {
    const projectParent = vi
      .fn()
      .mockResolvedValueOnce(status({ state: "volume_offline", unclaimed: { state: "volume_offline", folders: [], total: 0 } }))
      .mockResolvedValueOnce(
        status({
          state: "conflict",
          reason: "这个文件夹在项目「云图AI」的材料文件夹里面",
          unclaimed: { state: "conflict", folders: [], total: 0 },
        }),
      );
    const first = renderRow(makeClient({ projectParent } as Partial<ApiClient>));
    expect(await screen.findByText("资料盘未连接，插上后再列下面的文件夹")).toBeInTheDocument();
    first.unmount();

    renderRow(makeClient({ projectParent } as Partial<ApiClient>));
    expect(await screen.findByText("这个文件夹在项目「云图AI」的材料文件夹里面")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "改" })).toBeInTheDocument();
  });

  it("手机上只读：设好了只写路径，没设就不出现", async () => {
    const withParent = makeClient({
      projectParent: vi.fn().mockResolvedValue(status({ unclaimed: { state: "ready", folders: FOLDERS, total: 2 } })),
    } as Partial<ApiClient>);
    const first = renderRow(withParent, false);
    expect(await screen.findByText(PARENT)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText(/还有 2 个/)).not.toBeInTheDocument();
    first.unmount();

    const unset = makeClient({
      projectParent: vi.fn().mockResolvedValue(status({ path: null, state: null, suggested: { path: PARENT, count: 1, total: 1 } })),
    } as Partial<ApiClient>);
    const { container } = renderRow(unset, false);
    await waitFor(() => expect(unset.projectParent).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("旧 client 没有这个接口时什么都不出", () => {
    const { container } = renderRow({} as ApiClient);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ProjectParentRow 轮询", () => {
  it("下面的文件夹还在看时每 2 秒再问一次，列出来后弹认领框", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const checking = status({ state: "checking", unclaimed: { state: "checking", folders: [], total: 0 } });
    const projectParent = vi
      .fn()
      .mockResolvedValueOnce(status({ path: null, state: null, suggested: { path: PARENT, count: 2, total: 3 } }))
      .mockResolvedValueOnce(checking)
      .mockResolvedValueOnce(checking)
      .mockResolvedValue(status({ unclaimed: { state: "ready", folders: FOLDERS, total: 2 } }));
    const setProjectParent = vi.fn().mockResolvedValue(checking);
    renderRow(makeClient({ projectParent, setProjectParent } as Partial<ApiClient>));

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(await screen.findByRole("button", { name: "用这个" }));
    await waitFor(() => expect(projectParent).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(projectParent).toHaveBeenCalledTimes(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(projectParent).toHaveBeenCalledTimes(4);
    expect(await screen.findByRole("dialog", { name: "这下面有 2 个文件夹还没挂到项目" })).toBeInTheDocument();

    // 不再是 checking 就不再问
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(projectParent).toHaveBeenCalledTimes(4);
  });

  it("认领完关掉后把汇总交给项目页、重读还剩几个", async () => {
    const projectParent = vi
      .fn()
      .mockResolvedValueOnce(status({ unclaimed: { state: "ready", folders: FOLDERS, total: 2 } }))
      .mockResolvedValue(status({ unclaimed: { state: "ready", folders: [FOLDERS[0]], total: 1 } }));
    const claimFolders = vi.fn().mockResolvedValue({
      items: [{ path: `${PARENT}/云图AI`, action: "mount", ok: true, project_id: "p1", project_name: "云图AI" }],
      created: 0,
      mounted: 1,
      cards_written: 3,
      needs_review: 0,
    });
    const { onNotice, onProjectsChanged } = renderRow(makeClient({ projectParent, claimFolders } as Partial<ApiClient>));

    await userEvent.click(await screen.findByRole("button", { name: "看看" }));
    await userEvent.click(screen.getByRole("button", { name: "处理勾选的 1 个" }));

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("挂上 1 个文件夹，补写了 3 张会议卡片", "success", 12_000));
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(await screen.findByText(/还有 1 个文件夹没挂到项目/)).toBeInTheDocument();
  });
});
