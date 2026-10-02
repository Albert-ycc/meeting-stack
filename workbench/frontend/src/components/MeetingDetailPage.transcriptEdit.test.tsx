import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { MeetingDetail, Segment } from "../types";
import { MeetingDetailPage, sameSegments } from "./MeetingDetailPage";

// 上百段的逐字稿编辑：脏判断不对整份逐字稿做序列化，也不改变「改了又改回去不算改过」这些口径
const COUNT = 120;
const segments: Segment[] = Array.from({ length: COUNT }, (_, index) => ({
  id: `s${index}`,
  ordinal: index,
  start_ms: index * 5_000,
  end_ms: index * 5_000 + 4_000,
  speaker_label: "SPEAKER_00",
  speaker_name: null,
  text: `第${index}句`,
}));
// 第 60 句从 05:00 起
const LABEL = "05:00 逐字稿";

function detail(): MeetingDetail {
  return {
    id: "vm-long",
    title: "超长会",
    status: "completed_unreviewed",
    tags: [],
    artifacts: [],
    segments,
    speakers: [],
    events: [],
    current_transcript_version_id: "tv-1",
    transcript_versions: [
      { id: "tv-1", meeting_id: "vm-long", version_no: 1, kind: "funasr", published: 0, created_at: "2026-07-10T00:00:00Z" },
    ],
    minutes_versions: [],
  };
}

function setup() {
  const onDirtyChange = vi.fn();
  const saveTranscript = vi.fn().mockResolvedValue({ version_id: "tv-2" });
  const onReload = vi.fn().mockResolvedValue(undefined);
  render(
    <MeetingDetailPage
      apiClient={{ asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }), saveTranscript } as unknown as ApiClient}
      initialSeekMs={0}
      isMobile={false}
      meeting={detail()}
      onBack={vi.fn()}
      onDirtyChange={onDirtyChange}
      onReload={onReload}
      projects={[]}
      tags={[]}
    />,
  );
  return { onDirtyChange, saveTranscript, onReload };
}

async function startEditing() {
  await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
  return screen.getByLabelText(LABEL) as HTMLTextAreaElement;
}

const lastDirty = (onDirtyChange: ReturnType<typeof vi.fn>) => onDirtyChange.mock.calls.at(-1)?.[0];

describe("MeetingDetailPage 逐字稿编辑的脏判断", () => {
  it("每敲一个字不再把整份逐字稿序列化（原来是对当前稿和打开时的稿各 JSON.stringify 一遍）", async () => {
    setup();
    const box = await startEditing();
    const stringify = vi.spyOn(JSON, "stringify");

    await userEvent.type(box, "一二三");

    const wholeTranscript = stringify.mock.calls.filter(([value]) => Array.isArray(value) && value.length >= COUNT);
    stringify.mockRestore();
    expect(box).toHaveValue("第60句一二三");
    expect(wholeTranscript).toHaveLength(0);
  });

  it("改了就是改过：保存草稿可点，外层收到有未保存的修改", async () => {
    const { onDirtyChange } = setup();
    const box = await startEditing();
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();

    await userEvent.type(box, "字");

    expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled();
    await waitFor(() => expect(lastDirty(onDirtyChange)).toBe(true));
  });

  it("改了又改回去不算改过：保存草稿重新置灰，外层的未保存标记撤掉", async () => {
    const { onDirtyChange } = setup();
    const box = await startEditing();
    await userEvent.type(box, "字");
    await waitFor(() => expect(lastDirty(onDirtyChange)).toBe(true));

    await userEvent.type(box, "{Backspace}");

    expect(box).toHaveValue("第60句");
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
    await waitFor(() => expect(lastDirty(onDirtyChange)).toBe(false));
  });

  it("改了两处再各自改回去，也算没改过；只改回一处还算改过", async () => {
    const { onDirtyChange } = setup();
    await startEditing();
    const first = screen.getByLabelText("00:00 逐字稿");
    const second = screen.getByLabelText(LABEL);
    await userEvent.type(first, "甲");
    await userEvent.type(second, "乙");

    await userEvent.type(first, "{Backspace}");
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled();
    await userEvent.type(second, "{Backspace}");

    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
    await waitFor(() => expect(lastDirty(onDirtyChange)).toBe(false));
  });

  it("保存之后没再动过不算改过；再敲一个字才又算改过（保存时打开时的稿被换成拷贝，内容相同）", async () => {
    const { saveTranscript, onReload } = setup();
    const box = await startEditing();
    await userEvent.type(box, "字");

    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(onReload).toHaveBeenCalled());
    expect(saveTranscript).toHaveBeenCalledTimes(1);
    expect(saveTranscript.mock.calls[0][1]).toHaveLength(COUNT);
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText(LABEL), "又");

    expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled();
  });
});

describe("sameSegments：比的字段和原来的整稿快照一致", () => {
  const base: Segment = {
    id: "s1",
    ordinal: 0,
    start_ms: 1_000,
    end_ms: 4_000,
    speaker_label: "SPEAKER_00",
    speaker_name: "甲",
    text: "原句",
  };

  it("同一个数组、内容相同的两份拷贝都算一样；长度不同不一样", () => {
    const list = [base, { ...base, id: "s2", ordinal: 1 }];
    expect(sameSegments(list, list)).toBe(true);
    expect(sameSegments(list, list.map((segment) => ({ ...segment })))).toBe(true);
    expect(sameSegments(list, list.slice(0, 1))).toBe(false);
    expect(sameSegments([], [])).toBe(true);
  });

  it.each([
    ["id", { id: "s9" }],
    ["顺序", { ordinal: 3 }],
    ["开始时间", { start_ms: 1_500 }],
    ["结束时间", { end_ms: 4_500 }],
    ["说话人标签", { speaker_label: "SPEAKER_01" }],
    ["说话人姓名", { speaker_name: "乙" }],
    ["文字", { text: "改过的句子" }],
  ] satisfies Array<[string, Partial<Segment>]>)("%s 变了就算改过", (_name, change) => {
    expect(sameSegments([base], [{ ...base, ...change }])).toBe(false);
  });

  it("说话人没有（undefined、缺这个键、null）都是一回事", () => {
    const { speaker_label: _label, speaker_name: _name, ...bare } = base;
    expect(sameSegments([{ ...bare, speaker_label: null, speaker_name: null }], [bare])).toBe(true);
    expect(sameSegments([{ ...bare, speaker_label: undefined }], [{ ...bare, speaker_label: null }])).toBe(true);
    // 有名字和没名字不一样
    expect(sameSegments([bare], [{ ...bare, speaker_name: "甲" }])).toBe(false);
  });

  it("不属于内容的字段（版本号、会议号）不算", () => {
    expect(sameSegments([base], [{ ...base, version_id: "tv-2", meeting_id: "vm-2" }])).toBe(true);
  });

  it("只在中间一行引用不同、内容相同：整份仍然一样；那一行内容变了：不一样", () => {
    const list = Array.from({ length: 50 }, (_, index) => ({ ...base, id: `s${index}`, ordinal: index }));
    const copy = [...list];
    copy[25] = { ...list[25] };
    expect(sameSegments(list, copy)).toBe(true);
    copy[25] = { ...list[25], text: "改了" };
    expect(sameSegments(list, copy)).toBe(false);
  });
});
