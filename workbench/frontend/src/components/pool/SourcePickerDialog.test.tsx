import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import type { MeetingSummary, Segment } from "../../types";
import { CVM, HENGRUI, YIMI } from "./poolFixtures";
import { groupByDay, hasContent, QUOTE_MAX, quoteOf, SourcePickerDialog, type SourceDraft } from "./SourcePickerDialog";

/*
 * 会议和逐字稿照生产库抄（和后端 requirement_pool_world 是同一份）：会名、录音时间（-07:00）、时长、所属项目、
 * 时间锚前后的几句原话都不编。接口按录音时间由近到远给；用例的时区钉在 Asia/Shanghai，
 * 所以 09-29 19:26（-07:00）这场会在界面上是 9月30日 星期三。
 */
function meetingRow(
  id: string,
  title: string,
  recording_date: string,
  duration_ms: number,
  project: [string, string] | null,
): MeetingSummary {
  return {
    id,
    title,
    recording_date,
    duration_ms,
    status: "published",
    project_id: project?.[0] ?? null,
    project_name: project?.[1] ?? null,
    audio_artifact_id: null,
    tags: [],
  };
}

const YIMI_PROJECT: [string, string] = [YIMI, "医米科研用药"];
const HENGRUI_PROJECT: [string, string] = [HENGRUI, "恒瑞健康"];
const CVM_PROJECT: [string, string] = [CVM, "CVM 云讲堂"];

const WORLD: MeetingSummary[] = [
  meetingRow("vm-20260929-192637-f3947874", "260929 云课堂直播运营问题对齐", "2026-09-29T19:26:37-07:00", 747000, CVM_PROJECT),
  meetingRow("vm-20260928-183726-93ac203c", "EDC 系统选型与产研对接决策", "2026-09-28T18:37:26-07:00", 113475, YIMI_PROJECT),
  meetingRow("vm-20260921-192149-f9d22c68", "黑卡分享注销与积分限制口径", "2026-09-21T19:21:49-07:00", 105665, HENGRUI_PROJECT),
  meetingRow("vm-20260921-191503-d0cfb758", "恒瑞黑卡亲友积分与后台字段", "2026-09-21T19:15:03-07:00", 152885, HENGRUI_PROJECT),
  meetingRow("vm-20260921-030550-4975623f", "新患者注册流程五个问题前置", "2026-09-21T03:05:50-07:00", 78655, YIMI_PROJECT),
  meetingRow("vm-20260920-021944-87458591", "艾坦联合艾瑞卡申领与出组核对", "2026-09-20T02:19:44-07:00", 130185, [
    "project-02500313fb494b2d",
    "安心四季",
  ]),
  meetingRow("vm-20260919-235421-d359efe7", "260919 项目复制与名单一键转移", "2026-09-19T23:54:21-07:00", 276265, [
    "project-036a983d51b849f1",
    "项目复制与名单一键转移",
  ]),
  meetingRow("vm-20260916-215500-ba46d327", "口服药到店领取保留与药房配置", "2026-09-16T21:55:00-07:00", 154155, [
    "project-a9eab7580a614e5b",
    "口服药到店领取配置方案",
  ]),
  meetingRow("vm-20260916-190150-2eebb406", "260916 医米京东科研仓系统对接", "2026-09-16T19:01:50-07:00", 3033387, YIMI_PROJECT),
  meetingRow("vm-20260914-003009-1dffbbce", "华夏劳务协议签署与证据链对接", "2026-09-14T00:30:09-07:00", 3942349, [
    "project-2de1cdc1e9364813",
    "华夏基金会科普同行",
  ]),
  // 这场会没归项目
  meetingRow("vm-20260913-180145-6a743f14", "医生资质AI审核规则沟通", "2026-09-13T18:01:45-07:00", 775770, null),
  meetingRow("vm-20260910-182248-fe38a3cb", "医米赠药横跳拦截规则", "2026-09-10T18:22:48-07:00", 194525, YIMI_PROJECT),
];
const CVM_MEETING = WORLD[0];
const JD_MEETING = WORLD[8];

function segmentsOf(meetingId: string, lines: Array<[number, string, string]>): Segment[] {
  return lines.map(([start, speaker, text], ordinal) => ({
    id: `${meetingId}-${start}`,
    ordinal,
    start_ms: start,
    end_ms: start + 1500,
    speaker_label: speaker,
    speaker_name: null,
    text,
  }));
}

// 云课堂那场会 05:30 到 09:42 的十句原话和时间
const CVM_LINES: Array<[number, string, string]> = [
  [330820, "SPEAKER_01", "它其实要读白介素十七，"],
  [335530, "SPEAKER_01", "就是 IL 杠十七 a 是一个错误的读法，"],
  [387880, "SPEAKER_01", "要把它的比例稍微缩小一点，"],
  [389820, "SPEAKER_01", "比如说搜到百分之九十五，"],
  [391400, "SPEAKER_01", "然后确保所有的画面都可以呈现出来。"],
  [526090, "SPEAKER_01", "然后可能前面四列需要去把它填充满一百场。"],
  [568390, "SPEAKER_03", "预约审核查看。"],
  [576900, "SPEAKER_01", "那我有办法导出 excel 吗？"],
  [581000, "SPEAKER_01", "是没有办法，"],
  [582060, "SPEAKER_01", "我看到导出是一个 OKOK。"],
];
const CVM_SEGMENTS = segmentsOf(CVM_MEETING.id, CVM_LINES);

function meetingsApi(items: MeetingSummary[], pageSize = Infinity) {
  return vi.fn(async (filters: { limit?: number; offset?: number } = {}) => {
    const offset = filters.offset ?? 0;
    const size = Math.min(filters.limit ?? 100, pageSize);
    return { items: items.slice(offset, offset + size), total: items.length, limit: size, offset };
  });
}

function api(overrides: Partial<ApiClient> = {}) {
  return {
    meetings: meetingsApi(WORLD),
    meeting: vi.fn(async (id: string) => ({ id, title: "", segments: id === CVM_MEETING.id ? CVM_SEGMENTS : [] })),
    ...overrides,
  } as unknown as ApiClient & { meetings: ReturnType<typeof meetingsApi>; meeting: ReturnType<typeof vi.fn> };
}

function renderPicker(client: ApiClient, props: Partial<Parameters<typeof SourcePickerDialog>[0]> = {}) {
  const handlers = { onClose: vi.fn(), onPicked: vi.fn<(source: SourceDraft) => void>() };
  render(<SourcePickerDialog apiClient={client} projectId={null} projectName={null} {...handlers} {...props} />);
  return handlers;
}

async function openCvm() {
  const dialog = await screen.findByRole("dialog", { name: "选来源" });
  await userEvent.click(await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));
  await within(dialog).findByRole("list", { name: "逐字稿" });
  return dialog;
}

function lineButton(dialog: HTMLElement, text: RegExp | string) {
  return within(dialog).getByRole("button", { name: text });
}

/** jsdom 的 PointerEvent 带得上 button、pointerType、clientY，可以直接发 */
function pointer(type: "pointerDown" | "pointerMove" | "pointerUp", element: Element, init: PointerEventInit = {}) {
  fireEvent[type](element, { button: 0, pointerType: "mouse", clientX: 40, clientY: 100, ...init });
}

// 机器忙的时候取数加渲染会超过默认的 1 秒
configure({ asyncUtilTimeout: 4000 });

// 日期标题里「今年不带年份」要看当前年份：钉住日期，别让用例的结果随跑它的那天变
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-01T12:00:00+08:00") });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("选来源会议（第一步）", () => {
  it("默认列所选项目的会，按本机日期分组：组标题带星期，每行是会名加项目标签", async () => {
    const client = api();
    renderPicker(client, { projectId: CVM, projectName: "CVM 云讲堂" });
    const dialog = await screen.findByRole("dialog", { name: "选来源" });

    expect(within(dialog).getByRole("heading", { name: "选择来源会议" })).toBeInTheDocument();
    await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ });
    expect(client.meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: CVM, limit: 30, offset: 0 });

    expect(within(dialog).getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "9月30日 星期三",
      "9月29日 星期二",
      "9月22日 星期二",
      "9月21日 星期一",
      "9月20日 星期日",
      "9月17日 星期四",
      "9月14日 星期一",
      "9月11日 星期五",
    ]);
    const day = within(dialog).getByRole("region", { name: "9月22日 星期二" });
    expect(within(day).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "黑卡分享注销与积分限制口径恒瑞健康",
      "恒瑞黑卡亲友积分与后台字段恒瑞健康",
    ]);
    // 没归项目的会标「未归项目」
    expect(within(within(dialog).getByRole("region", { name: "9月14日 星期一" })).getAllByRole("button")[1]).toHaveTextContent(
      "医生资质AI审核规则沟通未归项目",
    );
  });

  it("搜索框写「搜会议标题或原话」，停 300ms 再查，带上搜索词", async () => {
    const client = api();
    renderPicker(client);
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ });
    expect(client.meetings).toHaveBeenCalledTimes(1);

    const search = within(dialog).getByRole("textbox", { name: "搜会议" });
    expect(search).toHaveAttribute("placeholder", "搜会议标题或原话");
    expect(search).toHaveFocus();
    await userEvent.type(search, "导出");
    expect(client.meetings).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(client.meetings).toHaveBeenCalledTimes(2));
    expect(client.meetings).toHaveBeenLastCalledWith({ q: "导出", project_id: undefined, limit: 30, offset: 0 });
  });

  it("没选项目时列全部的会，也没有「全部项目」的切换", async () => {
    const client = api();
    renderPicker(client);
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ });

    expect(client.meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: undefined, limit: 30, offset: 0 });
    expect(within(dialog).queryByRole("combobox", { name: "会议范围" })).not.toBeInTheDocument();
  });

  it("有所选项目时可以在本项目和全部项目之间切换，切换后从第一页重新取", async () => {
    const client = api();
    renderPicker(client, { projectId: YIMI, projectName: "医米科研用药" });
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ });

    const scope = within(dialog).getByRole("combobox", { name: "会议范围" });
    expect(within(scope).getAllByRole("option").map((option) => option.textContent)).toEqual(["医米科研用药", "全部项目"]);
    expect(scope).toHaveValue("project");
    await userEvent.selectOptions(scope, "all");

    await waitFor(() =>
      expect(client.meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: undefined, limit: 30, offset: 0 }),
    );
    await userEvent.selectOptions(scope, "project");
    await waitFor(() => expect(client.meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: YIMI, limit: 30, offset: 0 }));
  });

  it("往下滚自动加载更早的会：从已列出的条数接着取，到底以后不再请求", async () => {
    // 一页只给 8 场：后端说总共 12 场，列表要接着往下要
    const meetings = meetingsApi(WORLD, 8);
    renderPicker(api({ meetings } as Partial<ApiClient>));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await within(dialog).findByRole("button", { name: /口服药到店领取保留与药房配置/ });
    expect(within(dialog).queryByRole("button", { name: /医米赠药横跳拦截规则/ })).not.toBeInTheDocument();
    expect(within(dialog).getByText("更早的会议往下滚动或搜索")).toBeInTheDocument();

    const list = dialog.querySelector(".source-picker__meetings") as HTMLElement;
    // 离底边还远：不取
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(list, "clientHeight", { configurable: true, value: 400 });
    list.scrollTop = 100;
    fireEvent.scroll(list);
    expect(meetings).toHaveBeenCalledTimes(1);

    // 滚到离底边不到 160px：取更早的一页
    list.scrollTop = 1500;
    fireEvent.scroll(list);
    await within(dialog).findByRole("button", { name: /医米赠药横跳拦截规则/ });
    expect(meetings).toHaveBeenCalledTimes(2);
    expect(meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: undefined, limit: 30, offset: 8 });
    // 同一天的会接在同一组里，不重复出组标题
    expect(within(dialog).getAllByRole("heading", { level: 3 }).filter((h) => h.textContent === "9月14日 星期一")).toHaveLength(1);
    expect(within(dialog).queryByText("更早的会议往下滚动或搜索")).not.toBeInTheDocument();

    fireEvent.scroll(list);
    fireEvent.scroll(list);
    expect(meetings).toHaveBeenCalledTimes(2);
  });

  it("一页没把列表撑出滚动条就没有滚动可触发：自己接着取，直到撑满或到底", async () => {
    // jsdom 没有布局：让列表的内容（300）一直比框（400）矮，相当于每页都撑不满
    const clientHeight = Object.getOwnPropertyDescriptor(Element.prototype, "clientHeight");
    const scrollHeight = Object.getOwnPropertyDescriptor(Element.prototype, "scrollHeight");
    Object.defineProperty(Element.prototype, "clientHeight", { configurable: true, get: () => 400 });
    Object.defineProperty(Element.prototype, "scrollHeight", { configurable: true, get: () => 300 });
    try {
      const meetings = meetingsApi(WORLD, 5);
      renderPicker(api({ meetings } as Partial<ApiClient>));
      const dialog = await screen.findByRole("dialog", { name: "选来源" });

      await within(dialog).findByRole("button", { name: /医米赠药横跳拦截规则/ });
      expect(meetings.mock.calls.map(([filters]) => filters?.offset)).toEqual([0, 5, 10]);
    } finally {
      if (clientHeight) Object.defineProperty(Element.prototype, "clientHeight", clientHeight);
      if (scrollHeight) Object.defineProperty(Element.prototype, "scrollHeight", scrollHeight);
    }
  });

  it("取更早一页失败：停在列表底下给重试，滚动不再重发，重试成功后接着往下", async () => {
    const meetings = meetingsApi(WORLD, 8);
    const flaky = vi.fn(async (filters: { limit?: number; offset?: number } = {}) => {
      if ((filters.offset ?? 0) > 0 && flaky.mock.calls.length === 2) throw new Error("会议读取失败：网络断了");
      return meetings(filters);
    });
    renderPicker(api({ meetings: flaky } as Partial<ApiClient>));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await within(dialog).findByRole("button", { name: /口服药到店领取保留与药房配置/ });
    const list = dialog.querySelector(".source-picker__meetings") as HTMLElement;

    fireEvent.scroll(list);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("会议读取失败：网络断了");
    fireEvent.scroll(list);
    fireEvent.scroll(list);
    expect(flaky).toHaveBeenCalledTimes(2);

    await userEvent.click(within(dialog).getByRole("button", { name: "重试" }));
    await within(dialog).findByRole("button", { name: /医米赠药横跳拦截规则/ });
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("换了搜索词：还在路上的旧搜索结果作废，不会盖掉新的", async () => {
    let releaseOld: (value: unknown) => void = () => undefined;
    const meetings = vi.fn((filters: { q?: string } = {}) =>
      filters.q
        ? Promise.resolve({ items: [WORLD[1]], total: 1, limit: 30, offset: 0 })
        : new Promise((resolve) => {
            releaseOld = resolve;
          }),
    );
    renderPicker(api({ meetings } as unknown as Partial<ApiClient>));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });

    await userEvent.type(within(dialog).getByRole("textbox", { name: "搜会议" }), "EDC");
    await within(dialog).findByRole("button", { name: /EDC 系统选型与产研对接决策/ });
    // 第一次（没有搜索词）的请求这时才回来
    releaseOld({ items: WORLD, total: WORLD.length, limit: 30, offset: 0 });
    await Promise.resolve();
    // 列表还是搜出来的那一场，没有被晚到的整张列表盖掉
    const days = within(dialog).getAllByRole("region");
    expect(days).toHaveLength(1);
    expect(within(days[0]).getAllByRole("button")).toHaveLength(1);
  });

  it("读取失败写在弹层里，没有会议时说一句", async () => {
    renderPicker(api({ meetings: vi.fn().mockRejectedValue(new Error("会议读取失败：库连不上")) } as Partial<ApiClient>));
    expect(await screen.findByText("会议读取失败：库连不上")).toBeInTheDocument();
  });

  it("没有找到会议时说一句", async () => {
    renderPicker(api({ meetings: meetingsApi([]) } as Partial<ApiClient>));
    expect(await screen.findByText("没有找到会议")).toBeInTheDocument();
  });

  it("从第二步换一场会回来：列表不重新取，停在离开时的位置", async () => {
    const client = api();
    renderPicker(client);
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ });
    const list = dialog.querySelector(".source-picker__meetings") as HTMLElement;
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(list, "clientHeight", { configurable: true, value: 400 });
    list.scrollTop = 320;
    fireEvent.scroll(list);

    await userEvent.click(within(dialog).getByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));
    await within(dialog).findByRole("list", { name: "逐字稿" });
    await userEvent.click(within(dialog).getByRole("button", { name: "← 换一场会" }));

    expect(within(dialog).getByRole("heading", { name: "选择来源会议" })).toBeInTheDocument();
    expect((dialog.querySelector(".source-picker__meetings") as HTMLElement).scrollTop).toBe(320);
    expect(client.meetings).toHaveBeenCalledTimes(1);
  });
});

describe("选原话（第二步）", () => {
  it("进到第二步：焦点落在标题上，会议信息和说明照原型写，逐字稿带时间和说话人", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    expect(within(dialog).getByRole("heading", { name: "选原话" })).toHaveFocus();
    expect(within(dialog).getByText("260929 云课堂直播运营问题对齐 · 09-30 10:26 · 12 分钟 · CVM 云讲堂")).toBeInTheDocument();
    expect(within(dialog).getByText("点一句当原话，或按住拖过几句一起选")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "← 换一场会" })).toBeInTheDocument();
    expect(within(dialog).getByText("还没挑原话")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确定" })).toBeDisabled();
    // 05:30 起不到一小时，时间是 mm:ss；SPEAKER_01 界面上叫「说话人 2」
    expect(lineButton(dialog, /预约审核查看/)).toHaveTextContent("09:28说话人 4预约审核查看。");
  });

  it("点一句选这一句：已选 1 句，时间锚是这一句的开始，确定带出原话和时间锚", async () => {
    const handlers = renderPicker(api());
    const dialog = await openCvm();

    await userEvent.click(lineButton(dialog, /那我有办法导出 excel 吗/));
    expect(lineButton(dialog, /那我有办法导出 excel 吗/)).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByText("已选 1 句")).toBeInTheDocument();
    expect(within(dialog).getByText("▶ 00:09:36")).toBeInTheDocument();
    expect(within(dialog).getByText("时间锚取第一句的开始时间")).toBeInTheDocument();
    expect(within(dialog).getByText("「那我有办法导出 excel 吗？」")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "确定" }));
    expect(handlers.onPicked).toHaveBeenCalledWith({
      meeting_id: CVM_MEETING.id,
      meeting_title: "260929 云课堂直播运营问题对齐",
      recording_date: "2026-09-29T19:26:37-07:00",
      duration_ms: 747000,
      audio_artifact_id: null,
      quote: "那我有办法导出 excel 吗？",
      anchor_ms: 576900,
    });
  });

  it("先点一句再按住 Shift 点另一句，连着选中间的几句，倒着点也一样", async () => {
    const handlers = renderPicker(api());
    const dialog = await openCvm();

    await userEvent.click(lineButton(dialog, /那我有办法导出 excel 吗/));
    fireEvent.click(lineButton(dialog, /是没有办法/), { shiftKey: true });
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();
    expect(within(dialog).getByText("「那我有办法导出 excel 吗？是没有办法，」")).toBeInTheDocument();

    // 起点不动，往上连：从「是没有办法」那一边换到「预约审核查看」
    await userEvent.click(lineButton(dialog, /我看到导出是一个 OKOK/));
    fireEvent.click(lineButton(dialog, /预约审核查看/), { shiftKey: true });
    expect(within(dialog).getByText("已选 4 句")).toBeInTheDocument();
    expect(within(dialog).getByText("▶ 00:09:28")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "确定" }));
    expect(handlers.onPicked).toHaveBeenCalledWith(
      expect.objectContaining({
        quote: "预约审核查看。那我有办法导出 excel 吗？是没有办法，我看到导出是一个 OKOK。",
        anchor_ms: 568390,
      }),
    );
  });

  it("按住鼠标拖过几句，选中连着的几句；松手后补发的单击不把选区缩回一句", async () => {
    const handlers = renderPicker(api());
    const dialog = await openCvm();
    const first = lineButton(dialog, /那我有办法导出 excel 吗/);
    const last = lineButton(dialog, /我看到导出是一个 OKOK/);

    pointer("pointerDown", first);
    expect(within(dialog).getByText("还没挑原话")).toBeInTheDocument();
    pointer("pointerMove", lineButton(dialog, /是没有办法/));
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();
    pointer("pointerMove", last);
    expect(within(dialog).getByText("已选 3 句")).toBeInTheDocument();
    pointer("pointerUp", last);
    // 浏览器松手后补发的 click，目标恰好是这一行
    fireEvent.click(last, { detail: 1 });

    expect(within(dialog).getByText("已选 3 句")).toBeInTheDocument();
    expect(within(dialog).getByText("▶ 00:09:36")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "确定" }));
    expect(handlers.onPicked).toHaveBeenCalledWith(
      expect.objectContaining({ quote: "那我有办法导出 excel 吗？是没有办法，我看到导出是一个 OKOK。", anchor_ms: 576900 }),
    );
  });

  it("拖选完接着 Shift+点击：连选照常生效，不被拖选留下的「吞掉补发单击」吃掉", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    // 先拖选 预约审核查看 → 那我有办法导出 excel 吗；松手后浏览器在起点这一行补发 click，被吞掉
    pointer("pointerDown", lineButton(dialog, /预约审核查看/));
    pointer("pointerMove", lineButton(dialog, /那我有办法导出 excel 吗/));
    pointer("pointerUp", lineButton(dialog, /那我有办法导出 excel 吗/));
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();

    // 紧接着 Shift+点「我看到导出是一个 OKOK」：起点还是拖选的起点，连成 4 句
    const last = lineButton(dialog, /我看到导出是一个 OKOK/);
    pointer("pointerDown", last, { shiftKey: true });
    pointer("pointerUp", last, { shiftKey: true });
    fireEvent.click(last, { detail: 1, shiftKey: true });
    expect(within(dialog).getByText("已选 4 句")).toBeInTheDocument();
  });

  it("往上拖也一样；松手以后指针再怎么动，选区都不变", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    pointer("pointerDown", lineButton(dialog, /我看到导出是一个 OKOK/));
    pointer("pointerMove", lineButton(dialog, /预约审核查看/));
    pointer("pointerUp", lineButton(dialog, /预约审核查看/));
    expect(within(dialog).getByText("已选 4 句")).toBeInTheDocument();
    expect(within(dialog).getByText("▶ 00:09:28")).toBeInTheDocument();

    pointer("pointerMove", lineButton(dialog, /它其实要读白介素十七/));
    expect(within(dialog).getByText("已选 4 句")).toBeInTheDocument();
  });

  it("只是点一下（没有拖）照常选一句；触屏不起拖选，留给滚动；右键、Shift 点击也不起拖选", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    pointer("pointerDown", lineButton(dialog, /预约审核查看/), { pointerType: "touch" });
    pointer("pointerMove", lineButton(dialog, /那我有办法导出 excel 吗/), { pointerType: "touch" });
    expect(within(dialog).getByText("还没挑原话")).toBeInTheDocument();
    pointer("pointerUp", lineButton(dialog, /那我有办法导出 excel 吗/), { pointerType: "touch" });

    pointer("pointerDown", lineButton(dialog, /预约审核查看/), { button: 2 });
    pointer("pointerMove", lineButton(dialog, /那我有办法导出 excel 吗/));
    expect(within(dialog).getByText("还没挑原话")).toBeInTheDocument();
    pointer("pointerUp", lineButton(dialog, /那我有办法导出 excel 吗/));

    await userEvent.click(lineButton(dialog, /预约审核查看/));
    expect(within(dialog).getByText("已选 1 句")).toBeInTheDocument();
  });

  it("键盘：回车选一句，Shift+回车连选（浏览器发来的 click 没有鼠标点击数）", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    fireEvent.click(lineButton(dialog, /预约审核查看/));
    fireEvent.click(lineButton(dialog, /是没有办法/), { shiftKey: true });
    expect(within(dialog).getByText("已选 3 句")).toBeInTheDocument();
  });

  it("用查找过滤后连选，只取看得见的几句：高亮的和进原话的是同一批（审查 M4）", async () => {
    const handlers = renderPicker(api());
    const dialog = await openCvm();

    // 含「要」的只有三句：1 它其实要读…、3 要把它的比例…、6 …需要去把它填充满…，中间隔着没显示的句子
    await userEvent.type(within(dialog).getByRole("textbox", { name: "在逐字稿中查找" }), "要");
    expect(within(dialog).getAllByRole("button", { name: /^\d\d:\d\d/ })).toHaveLength(3);

    await userEvent.click(lineButton(dialog, /它其实要读白介素十七/));
    fireEvent.click(lineButton(dialog, /然后可能前面四列需要去把它填充满一百场/), { shiftKey: true });

    expect(within(dialog).getByText("已选 3 句")).toBeInTheDocument();
    expect(within(dialog).getAllByRole("button", { pressed: true })).toHaveLength(3);
    await userEvent.click(within(dialog).getByRole("button", { name: "确定" }));
    expect(handlers.onPicked).toHaveBeenCalledWith(
      expect.objectContaining({
        quote: "它其实要读白介素十七，要把它的比例稍微缩小一点，然后可能前面四列需要去把它填充满一百场。",
        anchor_ms: 330820,
      }),
    );
  });

  it("用查找过滤后拖选，同样只取看得见的几句", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    await userEvent.type(within(dialog).getByRole("textbox", { name: "在逐字稿中查找" }), "要");
    pointer("pointerDown", lineButton(dialog, /它其实要读白介素十七/));
    pointer("pointerMove", lineButton(dialog, /然后可能前面四列需要去把它填充满一百场/));
    pointer("pointerUp", lineButton(dialog, /然后可能前面四列需要去把它填充满一百场/));

    expect(within(dialog).getByText("已选 3 句")).toBeInTheDocument();
    expect(within(dialog).getByText("「它其实要读白介素十七，要把它的比例稍微缩小一点，然后可能前面四列需要去把它填充满一百场。」")).toBeInTheDocument();
  });

  it("查找词变了：被过滤掉的已选句子既不高亮也不进原话，清掉查找词它们又回来", async () => {
    renderPicker(api());
    const dialog = await openCvm();

    await userEvent.click(lineButton(dialog, /那我有办法导出 excel 吗/));
    fireEvent.click(lineButton(dialog, /是没有办法/), { shiftKey: true });
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();

    const find = within(dialog).getByRole("textbox", { name: "在逐字稿中查找" });
    await userEvent.type(find, "没有办法");
    expect(within(dialog).getByText("已选 1 句")).toBeInTheDocument();
    expect(within(dialog).getByText("「是没有办法，」")).toBeInTheDocument();
    expect(within(dialog).getByText("▶ 00:09:41")).toBeInTheDocument();

    await userEvent.clear(find);
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();
  });

  it("查找不到时说一句；也能按说话人查找", async () => {
    renderPicker(api());
    const dialog = await openCvm();
    const find = within(dialog).getByRole("textbox", { name: "在逐字稿中查找" });

    await userEvent.type(find, "说话人 4");
    expect(within(dialog).getAllByRole("button", { name: /^\d\d:\d\d/ })).toHaveLength(1);
    await userEvent.clear(find);
    await userEvent.type(find, "没有这句话");
    expect(within(dialog).getByText("没有句子含「没有这句话」")).toBeInTheDocument();
  });

  it("选中的只有标点或空白：确定置灰，给一句提示；改选有字的就好了", async () => {
    // 真实逐字稿里没有纯标点的句子，这里造一句边界：其余各句都是云课堂那场会的原话
    const withComma = [
      ...CVM_SEGMENTS,
      { ...CVM_SEGMENTS[9], id: "boundary-comma", ordinal: 10, start_ms: 583000, text: "，" },
      { ...CVM_SEGMENTS[9], id: "boundary-blank", ordinal: 11, start_ms: 584000, text: "  " },
    ];
    renderPicker(
      api({ meeting: vi.fn(async (id: string) => ({ id, title: "", segments: withComma })) } as unknown as Partial<ApiClient>),
    );
    const dialog = await openCvm();

    await userEvent.click(lineButton(dialog, /^09:43/));
    expect(within(dialog).getByText("选中的只有标点或空白，再选一句有字的")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确定" })).toBeDisabled();

    fireEvent.click(lineButton(dialog, /^09:44/), { shiftKey: true });
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确定" })).toBeDisabled();

    // 连上一句有字的就行了
    fireEvent.click(lineButton(dialog, /我看到导出是一个 OKOK/), { shiftKey: true });
    expect(within(dialog).getByRole("button", { name: "确定" })).toBeEnabled();
  });

  it("原话超过 1000 字：确定置灰，提示少选几句", async () => {
    // 把这场会的十句原话反复拼，凑出超过 1000 字的一大段
    const long = Array.from({ length: 8 }, (_, round) =>
      CVM_SEGMENTS.map((segment) => ({ ...segment, id: `${segment.id}-${round}`, start_ms: segment.start_ms + round * 600000 })),
    ).flat();
    renderPicker(
      api({ meeting: vi.fn(async (id: string) => ({ id, title: "", segments: long })) } as unknown as Partial<ApiClient>),
    );
    const dialog = await openCvm();
    const rows = within(dialog).getAllByRole("button", { name: /^\d\d:\d\d/ });

    await userEvent.click(rows[0]);
    fireEvent.click(rows[rows.length - 1], { shiftKey: true });
    expect(within(dialog).getByText(`原话最多 ${QUOTE_MAX} 字，少选几句`)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "确定" })).toBeDisabled();
  });

  it("只关联这场会：不带原话和时间锚", async () => {
    const handlers = renderPicker(api());
    const dialog = await openCvm();

    await userEvent.click(within(dialog).getByRole("button", { name: "只关联这场会" }));
    expect(handlers.onPicked).toHaveBeenCalledWith(expect.objectContaining({ meeting_id: CVM_MEETING.id, quote: "", anchor_ms: null }));
  });

  it("这场会还没有逐字稿：说一句，仍然可以只关联这场会", async () => {
    const handlers = renderPicker(api());
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await userEvent.click(await within(dialog).findByRole("button", { name: /260916 医米京东科研仓系统对接/ }));

    expect(await within(dialog).findByText("这场会还没有逐字稿，只能关联这场会")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "只关联这场会" }));
    expect(handlers.onPicked).toHaveBeenCalledWith(expect.objectContaining({ meeting_id: JD_MEETING.id, quote: "" }));
  });

  it("逐字稿读不出来时把原因写在弹层里", async () => {
    renderPicker(api({ meeting: vi.fn().mockRejectedValue(new Error("逐字稿读取失败：文件不在了")) } as Partial<ApiClient>));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    await userEvent.click(await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));

    expect(await within(dialog).findByText("逐字稿读取失败：文件不在了")).toBeInTheDocument();
  });

  it("「← 换一场会」回到第一步；Esc 和 ✕ 关掉整个弹层", async () => {
    const handlers = renderPicker(api());
    const dialog = await openCvm();

    await userEvent.click(within(dialog).getByRole("button", { name: "← 换一场会" }));
    expect(within(dialog).getByRole("heading", { name: "选择来源会议" })).toBeInTheDocument();
    await userEvent.click(await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));
    await within(dialog).findByRole("list", { name: "逐字稿" });

    await userEvent.keyboard("{Escape}");
    expect(handlers.onClose).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(handlers.onClose).toHaveBeenCalledTimes(2);
  });
});

describe("quoteOf、hasContent、groupByDay", () => {
  it("原话取选中的文字，时间锚取第一句有字的开头", () => {
    expect(quoteOf(CVM_SEGMENTS.slice(7, 9))).toEqual({ quote: "那我有办法导出 excel 吗？是没有办法，", anchor_ms: 576900 });
    expect(quoteOf([{ ...CVM_SEGMENTS[6], text: " " }, CVM_SEGMENTS[7]])).toEqual({ quote: "那我有办法导出 excel 吗？", anchor_ms: 576900 });
  });

  it("去掉标点、空白和看不见的格式字符以后还剩字，才算有内容", () => {
    expect(hasContent("，")).toBe(false);
    expect(hasContent("。 …… ！？")).toBe(false);
    expect(hasContent("  \u200b\u3000")).toBe(false);
    expect(hasContent("，嗯。")).toBe(true);
    expect(hasContent("OKOK")).toBe(true);
  });

  it("按本机日期分组：09-29 19:26（-07:00）是上海的 9月30日；不是今年的带上年份", () => {
    const groups = groupByDay(WORLD.slice(0, 3), 2026);
    expect(groups.map((group) => [group.title, group.meetings.length])).toEqual([
      ["9月30日 星期三", 1],
      ["9月29日 星期二", 1],
      ["9月22日 星期二", 1],
    ]);
    expect(groupByDay(WORLD.slice(0, 1), 2027)[0].title).toBe("2026年9月30日 星期三");
  });

  it("没有录音时间的会按创建时间分组，两样都没有就归到「日期待补」", () => {
    const orphan = { ...WORLD[0], id: "x", recording_date: null, created_at: undefined };
    expect(groupByDay([orphan], 2026)[0].title).toBe("日期待补");
    expect(groupByDay([{ ...orphan, created_at: "2026-09-29T19:26:37-07:00" }], 2026)[0].title).toBe("9月30日 星期三");
  });
});

