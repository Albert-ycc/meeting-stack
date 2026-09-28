import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RelatedMaterials as Payload, RelatedState } from "../../api";
import type { NoticeAction } from "../Notice";
import { RELATED_POLL_MS, RelatedMaterials, type RelatedMaterialsProps } from "./RelatedMaterials";

const KEY = "q2:3f6c0000000000000000";
const OTHER = "q2:aaaa0000000000000000";

function payload(state: Partial<RelatedState> = {}, extra: Partial<Payload> = {}): Payload {
  return {
    state: { kind: "ok", text: null, action: null, ...state },
    files: {
      [KEY]: { file_id: 812, name: "接口文档.docx", ext: "docx", root_online: true, playable: false, can_open: true, state_text: "" },
      [OTHER]: { file_id: 813, name: "报价单.xlsx", ext: "xlsx", root_online: true, playable: false, can_open: false, state_text: "" },
    },
    copies: [],
    windows: [
      {
        start_ms: 675_000,
        end_ms: 765_000,
        items: [
          { content_key: KEY, ordinal: 14, loc: "第三节", start_ms: null, text: "字段命名统一用小驼峰，驻场那部分单列", words: ["字段命名", "驻场"], at_ms: 734_500 },
        ],
      },
      {
        start_ms: 720_000,
        end_ms: 810_000,
        items: [
          { content_key: OTHER, ordinal: 2, loc: "表格甲", start_ms: null, text: "驻场费用另算", words: ["驻场"], at_ms: 750_000 },
        ],
      },
      {
        start_ms: 1_800_000,
        end_ms: 1_890_000,
        items: [{ content_key: OTHER, ordinal: 3, loc: null, start_ms: null, text: "排期表", words: ["排期表"], at_ms: 1_810_000 }],
      },
    ],
    rejected: 0,
    ...extra,
  };
}

function setup(props: Partial<RelatedMaterialsProps> = {}, data: Payload = payload()) {
  const api = {
    relatedMaterials: vi.fn(async () => data),
    relatedRejected: vi.fn(async () => ({ items: [{ relation_id: 41, name: "报价单 v3.xlsx", decided_at: "2026-09-27T08:00:00Z" }] })),
    rejectRelatedMaterial: vi.fn(async () => ({
      relation: { id: 41 } as never,
      undo_until: "2099-01-01T00:00:00Z",
    })),
    undoRelation: vi.fn(async () => ({ relation: { id: 41 } as never, removed_deliverable_id: null })),
    answerRelation: vi.fn(async () => ({ relation: { id: 41 } as never, undo_until: "2099-01-01T00:00:00Z" })),
    openMaterialFile: vi.fn(async () => ({ ok: true })),
  };
  const notices: Array<{ message: string; actions?: NoticeAction[] }> = [];
  const all: RelatedMaterialsProps = {
    apiClient: api,
    meetingId: "m",
    currentMs: 740_000,
    viewMs: null,
    isMobile: false,
    canWrite: true,
    onSeek: vi.fn(),
    onOpenPreview: vi.fn(),
    onOpenProject: vi.fn(),
    onNotice: (message, _tone, actions) => notices.push({ message, actions }),
    ...props,
  };
  const view = render(<RelatedMaterials {...all} />);
  return { api, notices, props: all, view };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("RelatedMaterials", () => {
  it("位置跟 viewMs；为 null 时跟 currentMs", async () => {
    const { props, view } = setup({ viewMs: 1_820_000 });
    await screen.findByText("排期表");
    expect(screen.queryByText("接口文档.docx")).not.toBeInTheDocument();
    view.rerender(<RelatedMaterials {...props} viewMs={null} />);
    expect(screen.getByText("接口文档.docx")).toBeInTheDocument();
    expect(screen.getByText("报价单.xlsx")).toBeInTheDocument();
    expect(screen.getByText(/12:00 前后/)).toBeInTheDocument();
    expect(screen.getByText("共同词：字段命名、驻场")).toBeInTheDocument();
  });

  it("存疑 1：会议改归属（projectId 变了，meetingId 没变）要重新取相关材料，不留旧项目的数据", async () => {
    const { api, props, view } = setup({ projectId: "project-old" }, payload({}, { rejected: 3 }));
    await screen.findByText("接口文档.docx");
    expect(api.relatedMaterials).toHaveBeenCalledTimes(1);

    api.relatedMaterials.mockResolvedValueOnce(payload({}, { rejected: 0 }));
    view.rerender(<RelatedMaterials {...props} projectId="project-new" />);

    await waitFor(() => expect(api.relatedMaterials).toHaveBeenCalledTimes(2));
  });

  it("点条目打开预览并定位；▶ 跳到会上那一句", async () => {
    const { props } = setup();
    await userEvent.click(await screen.findByRole("button", { name: "预览 接口文档.docx 第三节" }));
    expect(props.onOpenPreview).toHaveBeenCalledWith({
      fileId: 812,
      passage: { contentKey: KEY, ordinal: 14, from: "related", words: ["字段命名", "驻场"] },
    });
    await userEvent.click(screen.getByRole("button", { name: "从 12:14 播放会上这段" }));
    expect(props.onSeek).toHaveBeenCalledWith(734_500);
  });

  it("［不相关］先藏，［撤销］放回", async () => {
    const { api, notices } = setup();
    const item = (await screen.findByText("接口文档.docx")).closest("li")!;
    await userEvent.click(within(item).getByRole("button", { name: "不相关" }));
    expect(api.rejectRelatedMaterial).toHaveBeenCalledWith("m", { content_key: KEY, file_id: 812 });
    expect(screen.queryByText("接口文档.docx")).not.toBeInTheDocument();
    const notice = notices.find((entry) => entry.message === "已记下：『接口文档.docx』和这场会不相关")!;
    expect(notice.actions?.[0].label).toBe("撤销");
    await act(async () => notice.actions![0].onClick());
    expect(api.undoRelation).toHaveBeenCalledWith(41);
    await waitFor(() => expect(screen.getByText("接口文档.docx")).toBeInTheDocument());
    expect(notices.some((entry) => entry.message === "已撤销")).toBe(true);
  });

  it("［用本机应用打开］只在 can_open 时有", async () => {
    const { api } = setup();
    const first = (await screen.findByText("接口文档.docx")).closest("li")!;
    const second = screen.getByText("报价单.xlsx").closest("li")!;
    expect(within(second).queryByRole("button", { name: "用本机应用打开" })).not.toBeInTheDocument();
    await userEvent.click(within(first).getByRole("button", { name: "用本机应用打开" }));
    expect(api.openMaterialFile).toHaveBeenCalledWith(812);
  });

  it("每种状态一句话，最多一个按钮", async () => {
    const states: Array<[Partial<RelatedState>, number]> = [
      [{ kind: "waiting", text: "正在找相关材料" }, 0],
      [{ kind: "stopped", text: "这场会没归项目，相关材料只在项目文件夹里找" }, 0],
      [{ kind: "stopped", text: "这个项目还没挂材料文件夹", action: { kind: "open_project", label: "去项目页", project_id: "p" } }, 1],
      [{ kind: "ok", text: "这场会没找到相关材料" }, 0],
    ];
    for (const [state, buttons] of states) {
      const { view, props } = setup({}, payload(state, { windows: [] }));
      const line = await screen.findByText(state.text!);
      expect(within(line).queryAllByRole("button")).toHaveLength(buttons);
      if (buttons) {
        await userEvent.click(within(line).getByRole("button"));
        expect(props.onOpenProject).toHaveBeenCalledWith("p");
      }
      view.unmount();
    }
  });

  it("取不到时「相关材料没取到」［重试］；没有 % 和数字（时间除外）", async () => {
    const { api } = setup({}, payload({}, { windows: [] }));
    api.relatedMaterials.mockRejectedValueOnce(new Error("boom"));
    const { view } = setup({ apiClient: api });
    const line = await screen.findByText("相关材料没取到");
    expect(within(line).getAllByRole("button")).toHaveLength(1);
    const text = (view.container.textContent ?? "").replace(/\d{1,2}:\d{2}(:\d{2})?/g, "");
    expect(text).not.toMatch(/[%\d]/);
  });

  it("这一段没有时写「这一段没找到相关材料」和别的时间小签", async () => {
    const { props } = setup({ currentMs: 3_000_000 });
    expect(await screen.findByText("这一段没找到相关材料")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "30:00" }));
    expect(props.onSeek).toHaveBeenCalledWith(1_800_000);
  });

  it("标过不相关的：［看看］，每行［改回相关］", async () => {
    const { api, notices } = setup({}, payload({}, { rejected: 1 }));
    await userEvent.click(await screen.findByRole("button", { name: "看看" }));
    await userEvent.click(await screen.findByRole("button", { name: "改回相关" }));
    expect(api.answerRelation).toHaveBeenCalledWith(41, { answer: "restore" });
    expect(notices.map((entry) => entry.message)).toContain("已改回相关：『报价单 v3.xlsx』");
  });

  it("部分客户端（没有 relatedMaterials）什么都不画", () => {
    const { view } = setup({ apiClient: {} });
    expect(view.container).toBeEmptyDOMElement();
  });

  it("手机上是收起的横条，只跟 currentMs；canWrite 为假时没有［不相关］，也没有［用本机应用打开］", async () => {
    setup({ isMobile: true, canWrite: false, viewMs: 1_820_000 });
    const bar = await screen.findByRole("button", { name: "相关材料（12:00 前后）2 份 ▸" });
    expect(screen.queryByText("接口文档.docx")).not.toBeInTheDocument();
    await userEvent.click(bar);
    expect(screen.getByText("接口文档.docx")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "不相关" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "用本机应用打开" })).not.toBeInTheDocument();
  });

  it("电脑上点［收起］只剩标题一行，重挂后仍是收起", async () => {
    const { view, props } = setup();
    await screen.findByText("接口文档.docx");
    await userEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.getByText("相关材料（12:00 前后）2 份")).toBeInTheDocument();
    expect(screen.queryByText("接口文档.docx")).not.toBeInTheDocument();
    view.unmount();
    render(<RelatedMaterials {...props} />);
    expect(await screen.findByRole("button", { name: "展开" })).toBeInTheDocument();
  });

  it("waiting 时每 15 秒重取，变成 ok 以后不再发；页面隐藏时不发", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const waiting = payload({ kind: "waiting", text: "正在找相关材料" }, { windows: [] });
    const { api } = setup({}, waiting);
    await screen.findByText("正在找相关材料");
    expect(api.relatedMaterials).toHaveBeenCalledTimes(1);
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await act(async () => vi.advanceTimersByTime(RELATED_POLL_MS + 10));
    expect(api.relatedMaterials).toHaveBeenCalledTimes(1);
    hidden.mockReturnValue(false);
    api.relatedMaterials.mockResolvedValue(payload());
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(api.relatedMaterials).toHaveBeenCalledTimes(2));
    await screen.findByText("接口文档.docx");
    await act(async () => vi.advanceTimersByTime(RELATED_POLL_MS * 3));
    expect(api.relatedMaterials).toHaveBeenCalledTimes(2);
    hidden.mockRestore();
  });
});
