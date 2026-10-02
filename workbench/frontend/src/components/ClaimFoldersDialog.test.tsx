import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ClaimFoldersDialog, claimSummary, nestedHints } from "./ClaimFoldersDialog";
import type { ApiClient } from "../api";
import type { ClaimResult, UnclaimedFolder } from "../types";

const PARENT = "/Volumes/资料盘/项目";

function folder(name: string, overrides: Partial<UnclaimedFolder> = {}): UnclaimedFolder {
  return {
    path: `${PARENT}/${name}`,
    name,
    modified_at: "2026-09-20T08:00:00Z",
    kind: "new",
    action: "create",
    project_id: null,
    project_name: null,
    project_roots: [],
    checked: false,
    ...overrides,
  };
}

const FOLDERS: UnclaimedFolder[] = [
  folder("资料", { kind: "generic", action: null }),
  folder("云图AI", { kind: "exact", action: "mount", project_id: "p1", project_name: "云图AI", checked: true }),
  folder("北辰", {
    kind: "exact_mounted",
    action: "mount",
    project_id: "p2",
    project_name: "北辰",
    project_roots: ["/Volumes/资料盘/旧项目/北辰"],
  }),
  folder("数据看板2026", { kind: "similar", action: "mount", project_id: "p3", project_name: "数据看板" }),
  folder("蓝鲸云"),
  folder("海豚"),
];

function result(overrides: Partial<ClaimResult> = {}): ClaimResult {
  return { items: [], created: 0, mounted: 0, cards_written: 0, needs_review: 0, ...overrides };
}

function renderDialog(apiOverrides: Partial<ApiClient> = {}, folders = FOLDERS) {
  const apiClient = {
    claimFolders: vi.fn().mockResolvedValue(result()),
    declineFolder: vi.fn().mockResolvedValue({ ok: true }),
    undeclineFolder: vi.fn().mockResolvedValue({ ok: true }),
    ...apiOverrides,
  } as unknown as ApiClient;
  const onClose = vi.fn();
  const onChanged = vi.fn();
  render(<ClaimFoldersDialog apiClient={apiClient} folders={folders} onChanged={onChanged} onClose={onClose} />);
  return { apiClient, onClose, onChanged };
}

function row(name: string) {
  return screen.getByRole("checkbox", { name }).closest("li") as HTMLElement;
}

describe("ClaimFoldersDialog 默认动作", () => {
  it("每行写默认动作，默认勾选照后端，通用名排最后", () => {
    renderDialog();

    expect(screen.getByRole("dialog", { name: "这下面有 6 个文件夹还没挂到项目" })).toBeInTheDocument();
    expect(within(row("云图AI")).getByText("挂到『云图AI』")).toBeInTheDocument();
    expect(within(row("北辰")).getByText("『北辰』已挂了 …/北辰，再挂这个？")).toBeInTheDocument();
    expect(within(row("数据看板2026")).getByText("挂到『数据看板』？")).toBeInTheDocument();
    expect(within(row("蓝鲸云")).getByText("建成项目『蓝鲸云』")).toBeInTheDocument();
    expect(within(row("资料")).getByText("看起来不是项目")).toBeInTheDocument();

    expect(screen.getByRole("checkbox", { name: "云图AI" })).toBeChecked();
    for (const name of ["北辰", "数据看板2026", "蓝鲸云", "海豚", "资料"]) {
      expect(screen.getByRole("checkbox", { name })).not.toBeChecked();
    }
    const names = screen.getAllByRole("checkbox").map((box) => box.getAttribute("aria-label"));
    expect(names[names.length - 1]).toBe("资料");
    expect(screen.getByRole("button", { name: "处理勾选的 1 个" })).toBeEnabled();
  });

  it("［全选「建成项目」］只勾建成项目的行；［是另一个项目］把相近的那行改成建成项目", async () => {
    renderDialog();

    await userEvent.click(screen.getByRole("button", { name: "全选「建成项目」" }));
    expect(screen.getByRole("checkbox", { name: "蓝鲸云" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "海豚" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "资料" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "数据看板2026" })).not.toBeChecked();

    await userEvent.click(within(row("数据看板2026")).getByRole("button", { name: "是另一个项目" }));
    expect(within(row("数据看板2026")).getByText("建成项目『数据看板2026』")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "数据看板2026" })).toBeChecked();
    expect(screen.getByRole("button", { name: "处理勾选的 4 个" })).toBeInTheDocument();
  });
});

describe("ClaimFoldersDialog 不是项目", () => {
  it("点［不是项目］那一行消失，提示里能撤销", async () => {
    const { apiClient } = renderDialog();

    await userEvent.click(within(row("海豚")).getByRole("button", { name: "不是项目" }));
    expect(apiClient.declineFolder).toHaveBeenCalledWith(`${PARENT}/海豚`);
    expect(screen.queryByRole("checkbox", { name: "海豚" })).not.toBeInTheDocument();
    expect(await screen.findByText("『海豚』不算项目，以后不再列出")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "这下面有 5 个文件夹还没挂到项目" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(apiClient.undeclineFolder).toHaveBeenCalledWith(`${PARENT}/海豚`);
    expect(await screen.findByRole("checkbox", { name: "海豚" })).toBeInTheDocument();
    expect(screen.queryByText("『海豚』不算项目，以后不再列出")).not.toBeInTheDocument();
  });
});

describe("ClaimFoldersDialog 处理勾选的", () => {
  it("一次发整批，都成了就关，汇总一句话交给项目页，并刷新项目列表", async () => {
    const claimFolders = vi.fn().mockResolvedValue(
      result({
        items: [
          {
            path: `${PARENT}/云图AI`,
            action: "mount",
            ok: true,
            project_id: "p1",
            project_name: "云图AI",
            nested: [{ path: `${PARENT}/云图AI/北辰仓储`, project_id: "p9", project_name: "北辰仓储" }],
          },
          { path: `${PARENT}/蓝鲸云`, action: "create", ok: true, project_id: "p10", project_name: "蓝鲸云" },
          { path: `${PARENT}/海豚`, action: "create", ok: true, project_id: "p11", project_name: "海豚" },
        ],
        created: 2,
        mounted: 1,
        cards_written: 6,
        needs_review: 5,
      }),
    );
    const { onClose, onChanged } = renderDialog({ claimFolders } as Partial<ApiClient>);

    await userEvent.click(screen.getByRole("button", { name: "全选「建成项目」" }));
    await userEvent.click(screen.getByRole("button", { name: "处理勾选的 3 个" }));

    expect(claimFolders).toHaveBeenCalledTimes(1);
    expect(claimFolders).toHaveBeenCalledWith([
      { path: `${PARENT}/云图AI`, action: "mount", project_id: "p1" },
      { path: `${PARENT}/蓝鲸云`, action: "create" },
      { path: `${PARENT}/海豚`, action: "create" },
    ]);
    await waitFor(() =>
      expect(onClose).toHaveBeenCalledWith(
        "建了 2 个项目，挂上 1 个文件夹，补写了 6 张会议卡片；另有 5 场没认出的会提到了这些项目，已放进待你选。" +
          "其中 云图AI/北辰仓储/ 仍归项目『北辰仓储』",
      ),
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it("近似重名的那一行就地问「是不是它」，［挂到它］只把这一行再发一次", async () => {
    const claimFolders = vi
      .fn()
      .mockResolvedValueOnce(
        result({
          items: [
            { path: `${PARENT}/云图AI`, action: "mount", ok: true, project_id: "p1", project_name: "云图AI" },
            {
              path: `${PARENT}/蓝鲸云`,
              action: "create",
              ok: false,
              error: "已有「蓝鲸云平台」，是不是它？",
              suggestion: { id: "p5", project_id: "p5", name: "蓝鲸云平台" },
            },
          ],
          mounted: 1,
        }),
      )
      .mockResolvedValueOnce(
        result({
          items: [{ path: `${PARENT}/蓝鲸云`, action: "mount", ok: true, project_id: "p5", project_name: "蓝鲸云平台" }],
          mounted: 1,
          cards_written: 2,
        }),
      );
    const { onClose } = renderDialog({ claimFolders } as Partial<ApiClient>);

    await userEvent.click(screen.getByRole("checkbox", { name: "蓝鲸云" }));
    await userEvent.click(screen.getByRole("button", { name: "处理勾选的 2 个" }));

    const question = await within(row("蓝鲸云")).findByRole("alert");
    expect(question).toHaveTextContent("已有『蓝鲸云平台』，是不是它？");
    expect(within(question).getByRole("button", { name: "仍然新建" })).toBeInTheDocument();
    // 成了的那一行拿掉，弹窗留着，先说已经做了什么
    expect(screen.queryByRole("checkbox", { name: "云图AI" })).not.toBeInTheDocument();
    expect(screen.getByText("挂上 1 个文件夹")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.click(within(question).getByRole("button", { name: "挂到它" }));
    expect(claimFolders).toHaveBeenLastCalledWith([{ path: `${PARENT}/蓝鲸云`, action: "mount", project_id: "p5" }]);
    await waitFor(() => expect(onClose).toHaveBeenCalledWith("挂上 2 个文件夹，补写了 2 张会议卡片"));
  });

  it("［仍然新建］带 force 再发；完全同名时只有［挂到它］", async () => {
    const claimFolders = vi
      .fn()
      .mockResolvedValueOnce(
        result({
          items: [
            {
              path: `${PARENT}/蓝鲸云`,
              action: "create",
              ok: false,
              error: "已有「蓝鲸云平台」，是不是它？",
              suggestion: { id: "p5", project_id: "p5", name: "蓝鲸云平台" },
            },
            {
              path: `${PARENT}/海豚`,
              action: "create",
              ok: false,
              error: "已有「海豚」，是不是它？",
              suggestion: { id: "p6", project_id: "p6", name: "海豚", exact: true },
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        result({
          items: [{ path: `${PARENT}/蓝鲸云`, action: "create", ok: true, project_id: "p12", project_name: "蓝鲸云" }],
          created: 1,
        }),
      );
    const { onClose } = renderDialog({ claimFolders } as Partial<ApiClient>);

    await userEvent.click(screen.getByRole("checkbox", { name: "云图AI" }));
    await userEvent.click(screen.getByRole("button", { name: "全选「建成项目」" }));
    await userEvent.click(screen.getByRole("button", { name: "处理勾选的 2 个" }));

    const exact = await within(row("海豚")).findByRole("alert");
    expect(exact).toHaveTextContent("已有『海豚』，是不是它？");
    expect(within(exact).getByRole("button", { name: "挂到它" })).toBeInTheDocument();
    expect(within(exact).queryByRole("button", { name: "仍然新建" })).not.toBeInTheDocument();

    await userEvent.click(within(row("蓝鲸云")).getByRole("button", { name: "仍然新建" }));
    expect(claimFolders).toHaveBeenLastCalledWith([{ path: `${PARENT}/蓝鲸云`, action: "create", force: true }]);
    // 海豚那一行还没解决，弹窗不关
    expect(await screen.findByText("建了 1 个项目")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("［以后再说］什么都没处理时不留汇总", async () => {
    const { onClose } = renderDialog();
    await userEvent.click(screen.getByRole("button", { name: "以后再说" }));
    expect(onClose).toHaveBeenCalledWith("");
  });
});

describe("ClaimFoldersDialog 按 Esc", () => {
  it("焦点在不在弹窗里都关；输入法组合中的 Esc 不关", () => {
    const { onClose } = renderDialog();

    fireEvent.keyDown(document.body, { key: "Escape", isComposing: true });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("正在处理时不关：等这一批发完", async () => {
    const claimFolders = vi.fn().mockReturnValue(new Promise(() => undefined));
    const { onClose } = renderDialog({ claimFolders } as Partial<ApiClient>);
    await userEvent.click(screen.getByRole("button", { name: "全选「建成项目」" }));
    await userEvent.click(screen.getByRole("button", { name: "处理勾选的 3 个" }));
    expect(claimFolders).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("认领结果的说法", () => {
  it("是 0 的部分不说", () => {
    expect(claimSummary({ created: 2, mounted: 1, cards_written: 6, needs_review: 5 })).toBe(
      "建了 2 个项目，挂上 1 个文件夹，补写了 6 张会议卡片；另有 5 场没认出的会提到了这些项目，已放进待你选",
    );
    expect(claimSummary({ created: 0, mounted: 3, cards_written: 0, needs_review: 0 })).toBe("挂上 3 个文件夹");
    expect(claimSummary({ created: 1, mounted: 0, cards_written: 0, needs_review: 2 })).toBe(
      "建了 1 个项目；另有 2 场没认出的会提到了这些项目，已放进待你选",
    );
  });

  it("嵌套提示照原文：从挂上的文件夹名写起", () => {
    expect(
      nestedHints("/Volumes/资料盘/项目/云图AI", [
        { path: "/Volumes/资料盘/项目/云图AI/北辰/资料", project_id: "p2", project_name: "北辰" },
      ]),
    ).toEqual(["其中 云图AI/北辰/资料/ 仍归项目『北辰』"]);
  });
});
