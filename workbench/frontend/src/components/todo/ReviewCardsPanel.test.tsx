import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import type {
  LinkOption,
  RequirementOptionsPayload,
  ReviewCard,
  ReviewCardCandidate,
  ReviewCardTask,
  ReviewCardsPayload,
} from "../../types";
import { useMutex, type Mutex } from "../useMutex";
import { ReviewCardsPanel as BarePanel, type ReviewCardsPanelProps } from "./ReviewCardsPanel";

/** 互斥归页面（TasksPage）持有，面板拿来用：用例里用一个壳代替页面，失败原因照样走 onNotify */
let pageMutex: Mutex;
function ReviewCardsPanel(props: Omit<ReviewCardsPanelProps, "mutex">) {
  const mutex = useMutex((message) => props.onNotify(message, undefined, "error"));
  pageMutex = mutex;
  return <BarePanel {...props} mutex={mutex} />;
}

const MEETING_ID = "vm-20260929-192637-f3947874";
const PROJECT_ID = "project-592ef4b19a60442a";
const STAMP = "2026-09-30T02:27:00+00:00";

const EXPORT_OPTION: LinkOption = {
  kind: "candidate",
  id: "candidate-export",
  title: "科室会预约后台导出",
  priority: null,
  project_id: PROJECT_ID,
  project_name: "CVM 云讲堂",
  meeting_id: MEETING_ID,
  reason: "same_meeting",
};
const SIX_OPTION: LinkOption = {
  kind: "requirement",
  id: "requirement-six",
  title: "直播间运营六项修正",
  priority: "P1",
  project_id: PROJECT_ID,
  project_name: "CVM 云讲堂",
  meeting_id: null,
};

function task(overrides: Partial<ReviewCardTask>): ReviewCardTask {
  return {
    id: "task-x",
    title: "任务",
    detail: "",
    status: "pending_confirm",
    origin: "ai",
    assignee: "me",
    meeting_id: MEETING_ID,
    project_id: PROJECT_ID,
    project_name: "CVM 云讲堂",
    anchor_ms: null,
    due_date: null,
    status_changed_at: STAMP,
    created_at: STAMP,
    updated_at: STAMP,
    stall_days: 0,
    stalled: false,
    recommended: [],
    ...overrides,
  };
}

const FILL_INFO = task({ id: "task-fill", title: "催填节后三场信息", recommended: [EXPORT_OPTION] });
const WATCH = task({
  id: "task-watch",
  title: "持续盯预约场次并在群里动员报名",
  anchor_ms: 487620,
  due_date: "2026-10-03",
  recommended: [],
});
const SCRIPT = task({
  id: "task-script",
  title: "话术修改稿发执行群走默示确认",
  anchor_ms: 718230,
  recommended: [EXPORT_OPTION],
});

const CANDIDATE: ReviewCardCandidate = {
  kind: "candidate",
  id: "candidate-export",
  title: "科室会预约后台导出",
  summary: "预约审核页现在不能导出 Excel，运营要把科室会预约的名单拉出来对表。",
  status: "pending",
  priority: null,
  project_id: PROJECT_ID,
  project_name: "CVM 云讲堂",
  project_color: "#3b82f6",
  project_seat: 4,
  open_task_count: 0,
  meeting_count: 1,
  folder_count: 0,
  latest_meeting_date: "2026-09-29",
  source: {
    id: 1,
    kind: "origin",
    meeting_id: MEETING_ID,
    meeting_title: "260929 云课堂直播运营问题对齐",
    recording_date: "2026-09-29T19:26:37-07:00",
    duration_ms: 747000,
    audio_artifact_id: null,
    quote: "预约后台得能导出",
    anchor_ms: 576000,
    via_candidate_title: null,
  },
  follow_up_count: 0,
  similar_requirement: null,
  default_action: "claim",
  can_merge: true,
  created_at: STAMP,
  updated_at: STAMP,
  requirement_id: null,
  requirement_title: null,
};

const OPEN_CARD: ReviewCard = {
  meeting: {
    id: MEETING_ID,
    title: "260929 云课堂直播运营问题对齐",
    recording_date: "2026-09-29T19:26:37-07:00",
    duration_ms: 747000,
    project_id: PROJECT_ID,
    project_name: "CVM 云讲堂",
    project_color: "#3b82f6",
  },
  tasks: [FILL_INFO, WATCH, SCRIPT],
  candidates: [CANDIDATE],
  pending_task_count: 3,
  pending_candidate_count: 1,
  done: false,
};

const DONE_CARD: ReviewCard = {
  meeting: {
    id: "vm-20260916-190150-2eebb406",
    title: "260916 医米京东科研仓系统对接",
    recording_date: "2026-09-16T19:01:50-07:00",
    duration_ms: 3600000,
    project_id: "project-yimi",
    project_name: "医米科研用药",
    project_color: "#10b981",
  },
  tasks: [task({ id: "task-jd", title: "确认京东仓对接窗口", status: "confirmed", requirement_title: "京东科研仓对接" })],
  candidates: [
    {
      ...CANDIDATE,
      id: "candidate-jd",
      title: "京东仓签收凭证",
      summary: "仓库签收后要回传凭证。",
      status: "claimed",
      requirement_title: "京东科研仓对接",
      source: { ...CANDIDATE.source!, meeting_id: "vm-20260916-190150-2eebb406", anchor_ms: null },
    },
  ],
  pending_task_count: 0,
  pending_candidate_count: 0,
  done: true,
};

function cards(...list: ReviewCard[]): ReviewCardsPayload {
  return {
    cards: list,
    pending_task_count: list.reduce((sum, card) => sum + card.pending_task_count, 0),
    pending_candidate_count: list.reduce((sum, card) => sum + card.pending_candidate_count, 0),
  };
}

function withTask(card: ReviewCard, taskId: string, patch: Partial<ReviewCardTask>): ReviewCard {
  const tasks = card.tasks.map((item) => (item.id === taskId ? { ...item, ...patch } : item));
  const pending = tasks.filter((item) => item.status === "pending_confirm").length;
  return { ...card, tasks, pending_task_count: pending, done: pending === 0 && card.pending_candidate_count === 0 };
}

function setup(
  overrides: Partial<Record<string, unknown>> = {},
  props: Partial<Omit<ReviewCardsPanelProps, "mutex">> = {},
  initial: ReviewCardsPayload = cards(OPEN_CARD),
) {
  const apiClient = {
    reviewCards: vi.fn().mockResolvedValue(initial),
    confirmTask: vi.fn().mockResolvedValue({}),
    rejectTask: vi.fn().mockResolvedValue({}),
    undoTaskReview: vi.fn().mockResolvedValue({ reverted: [], failed: [] }),
    confirmAllInMeeting: vi.fn().mockResolvedValue({ confirmed: [], failed: [], linked: {} }),
    dropCandidate: vi.fn().mockResolvedValue({}),
    restoreCandidate: vi.fn().mockResolvedValue({}),
    candidateMergeTargets: vi.fn().mockResolvedValue({ items: [] }),
    taskRequirementOptions: vi.fn(),
    ...overrides,
  };
  const handlers = {
    onOpenMeeting: vi.fn(),
    onClaimCandidate: vi.fn(),
    onEditTask: vi.fn(),
    onChanged: vi.fn(),
    onNotify: vi.fn(),
  };
  const view = render(
    <ReviewCardsPanel
      apiClient={apiClient as unknown as ApiClient}
      canWrite
      filters={{}}
      reloadKey={0}
      {...handlers}
      {...props}
    />,
  );
  return { apiClient, ...handlers, ...view };
}

function row(title: string) {
  return screen.getByRole("row", { name: new RegExp(title) });
}

describe("ReviewCardsPanel", () => {
  afterEach(() => vi.useRealTimers());

  it("卡头、候选段和任务段按口径渲染，计数是还没处理的条数", async () => {
    const { onOpenMeeting } = setup();

    const card = await screen.findByRole("region", { name: "260929 云课堂直播运营问题对齐" });
    // 测试环境钉了 Asia/Shanghai，-07:00 的 19:26 在这里是次日 10:26
    expect(within(card).getByText("09-30 10:26 · 12 分钟 · CVM 云讲堂")).toBeInTheDocument();
    expect(within(card).getByRole("heading", { name: "需求候选 1" })).toBeInTheDocument();
    expect(within(card).getByRole("heading", { name: "待确认任务 3" })).toBeInTheDocument();
    expect(within(card).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "任务",
      "负责人",
      "截止",
      "原话",
      "挂到需求",
      "操作",
    ]);

    expect(within(card).getByText("AI 候选")).toBeInTheDocument();
    expect(within(card).getByText(CANDIDATE.summary)).toBeInTheDocument();
    expect(within(row("持续盯预约场次")).getByText("10-03")).toBeInTheDocument();
    expect(within(row("催填节后三场信息")).getByText("截止未定")).toBeInTheDocument();
    expect(within(row("催填节后三场信息")).getByText("—")).toBeInTheDocument();

    await userEvent.click(within(card).getByRole("button", { name: "打开会议 →" }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID);
    await userEvent.click(within(card).getByRole("button", { name: "播放「科室会预约后台导出」的原话" }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 576000);
    expect(within(card).getByText("原话 00:09:36")).toBeInTheDocument();
    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /播放/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 718230);
  });

  it("没有卡片时给空态", async () => {
    setup({ reviewCards: vi.fn().mockResolvedValue(cards()) });
    expect(
      await screen.findByText("没有要审的会。会议纪要生成后，AI 抽出的任务和需求候选会按会议列在这里。"),
    ).toBeInTheDocument();
  });

  it("挂到需求默认选中第一条推荐，没推荐显示不挂需求", async () => {
    setup();
    await screen.findByRole("region", { name: /260929/ });

    const recommended = within(row("话术修改稿")).getByRole("button", { name: "挂到需求：科室会预约后台导出" });
    expect(recommended).toHaveTextContent("候选");
    expect(recommended).toHaveTextContent("推荐");
    const none = within(row("持续盯预约场次")).getByRole("button", { name: "挂到需求：不挂需求" });
    expect(none).not.toHaveTextContent("推荐");
  });

  it("不改选直接确认：按推荐的候选带 candidate_id，提示带撤销，撤销调 undoTaskReview", async () => {
    const confirmed = withTask(OPEN_CARD, "task-script", { status: "confirmed", candidate_title: "科室会预约后台导出" });
    const reviewCards = vi.fn().mockResolvedValueOnce(cards(OPEN_CARD)).mockResolvedValue(cards(confirmed));
    const { apiClient, onNotify, onChanged } = setup({ reviewCards });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ }));

    expect(apiClient.confirmTask).toHaveBeenCalledWith("task-script", { candidate_id: "candidate-export" });
    await waitFor(() => expect(onNotify).toHaveBeenCalled());
    expect(onNotify).toHaveBeenCalledWith("已确认，挂到「科室会预约后台导出」", expect.any(Function));
    expect(onChanged).toHaveBeenCalled();

    // 确认后行就地置灰、留在卡里，写「已确认 ✓」和挂上的候选名；段标题计数同步变少
    const handled = row("话术修改稿");
    expect(within(handled).getByText("已确认 ✓")).toBeInTheDocument();
    expect(within(handled).getByText("候选：科室会预约后台导出")).toBeInTheDocument();
    expect(within(handled).queryByRole("button", { name: /^确认「/ })).toBeNull();
    expect(screen.getByRole("heading", { name: "待确认任务 2" })).toBeInTheDocument();

    const undo = onNotify.mock.calls[0][1] as () => Promise<void>;
    await undo();
    expect(apiClient.undoTaskReview).toHaveBeenCalledWith(["task-script"]);
    expect(onNotify).toHaveBeenLastCalledWith("已撤销确认");
  });

  it("改选成需求后确认带 requirement_id；改选不挂需求带两个 null", async () => {
    const taskRequirementOptions = vi.fn().mockImplementation(
      async (taskId: string): Promise<RequirementOptionsPayload> => ({
        task_id: taskId,
        can_link_candidates: true,
        recommended: [EXPORT_OPTION],
        default: EXPORT_OPTION,
        options: [SIX_OPTION, EXPORT_OPTION],
        current: null,
      }),
    );
    const { apiClient, onNotify } = setup({ taskRequirementOptions });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /挂到需求/ }));
    await userEvent.click(await screen.findByRole("option", { name: /直播间运营六项修正/ }));
    const trigger = within(row("话术修改稿")).getByRole("button", { name: "挂到需求：直播间运营六项修正" });
    expect(trigger).not.toHaveTextContent("推荐");
    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ }));
    expect(apiClient.confirmTask).toHaveBeenLastCalledWith("task-script", { requirement_id: "requirement-six" });
    await waitFor(() => expect(onNotify).toHaveBeenCalledWith("已确认，挂到「直播间运营六项修正」", expect.any(Function)));

    await userEvent.click(within(row("催填节后三场信息")).getByRole("button", { name: /挂到需求/ }));
    await userEvent.click(await screen.findByRole("button", { name: "不挂需求" }));
    await userEvent.click(within(row("催填节后三场信息")).getByRole("button", { name: /^确认「/ }));
    expect(apiClient.confirmTask).toHaveBeenLastCalledWith("task-fill", { requirement_id: null, candidate_id: null });
  });

  describe("别处改了挂接，卡上旧的选择作废", () => {
    const JD_OPTION: LinkOption = {
      kind: "requirement",
      id: "requirement-jd",
      title: "京东科研仓对接",
      priority: "P1",
      project_id: PROJECT_ID,
      project_name: "CVM 云讲堂",
      meeting_id: null,
      reason: "current",
    };
    const NONE_PICKER = {
      task_id: "task-script",
      can_link_candidates: true,
      recommended: [EXPORT_OPTION],
      default: EXPORT_OPTION,
      options: [SIX_OPTION, EXPORT_OPTION],
      current: null,
    };
    // 修改弹窗里把这条挂到京东并保存：库里 requirement_id 已是京东，后端推荐把当前挂着的排第一
    const savedInModal = cards(
      withTask(OPEN_CARD, "task-script", { requirement_id: "requirement-jd", recommended: [JD_OPTION] }),
    );

    async function pickNoneThenModalSaves(extra: Partial<Record<string, unknown>> = {}) {
      const reviewCards = vi.fn().mockResolvedValue(cards(OPEN_CARD));
      const view = setup({ reviewCards, taskRequirementOptions: vi.fn().mockResolvedValue(NONE_PICKER), ...extra });
      await screen.findByRole("region", { name: /260929/ });
      await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /挂到需求/ }));
      await userEvent.click(await screen.findByRole("button", { name: "不挂需求" }));
      expect(within(row("话术修改稿")).getByRole("button", { name: "挂到需求：不挂需求" })).toBeInTheDocument();

      reviewCards.mockResolvedValue(savedInModal);
      view.rerender(
        <ReviewCardsPanel
          apiClient={view.apiClient as unknown as ApiClient}
          canWrite
          filters={{}}
          onChanged={vi.fn()}
          onClaimCandidate={vi.fn()}
          onEditTask={vi.fn()}
          onNotify={view.onNotify}
          onOpenMeeting={vi.fn()}
          reloadKey={1}
        />,
      );
      return view;
    }

    it("卡上先选不挂需求、弹窗里挂到京东并保存后，显示并确认的都是京东，不会把它清掉", async () => {
      const { apiClient } = await pickNoneThenModalSaves();

      const trigger = await within(row("话术修改稿")).findByRole("button", { name: "挂到需求：京东科研仓对接" });
      expect(trigger).toHaveTextContent("推荐");
      await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ }));
      expect(apiClient.confirmTask).toHaveBeenCalledWith("task-script", { requirement_id: "requirement-jd" });
    });

    it("全部确认也不把作废的旧选择当成手选：没有逐条确认，挂接交给批量", async () => {
      const confirmAllInMeeting = vi.fn().mockResolvedValue({
        confirmed: ["task-fill", "task-watch", "task-script"],
        failed: [],
        linked: { "task-fill": EXPORT_OPTION, "task-watch": null, "task-script": JD_OPTION },
      });
      const { apiClient, onNotify } = await pickNoneThenModalSaves({ confirmAllInMeeting });
      await within(row("话术修改稿")).findByRole("button", { name: "挂到需求：京东科研仓对接" });

      await userEvent.click(screen.getByRole("button", { name: "全部确认" }));
      await waitFor(() => expect(onNotify).toHaveBeenCalled());
      expect(apiClient.confirmTask).not.toHaveBeenCalled();
      expect(confirmAllInMeeting).toHaveBeenCalledWith(MEETING_ID);
    });

    it("挂接没被别处动过时，卡上的选择照旧有效", async () => {
      const reviewCards = vi.fn().mockResolvedValue(cards(OPEN_CARD));
      const { apiClient, rerender } = setup({
        reviewCards,
        taskRequirementOptions: vi.fn().mockResolvedValue(NONE_PICKER),
      });
      await screen.findByRole("region", { name: /260929/ });
      await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /挂到需求/ }));
      await userEvent.click(await screen.findByRole("button", { name: "不挂需求" }));
      // 重新取数回来，任务挂接没变
      rerender(
        <ReviewCardsPanel
          apiClient={apiClient as unknown as ApiClient}
          canWrite
          filters={{}}
          onChanged={vi.fn()}
          onClaimCandidate={vi.fn()}
          onEditTask={vi.fn()}
          onNotify={vi.fn()}
          onOpenMeeting={vi.fn()}
          reloadKey={1}
        />,
      );
      await waitFor(() => expect(reviewCards).toHaveBeenCalledTimes(2));
      await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ }));
      expect(apiClient.confirmTask).toHaveBeenCalledWith("task-script", { requirement_id: null, candidate_id: null });
    });
  });

  it("驳回后行就地显示已驳回，撤销调 undoTaskReview；修改交给页面", async () => {
    // 驳回时间是 STAMP（02:27），钉在 2 分钟后，还在 10 分钟撤销窗口内
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-09-30T02:29:00+00:00") });
    const rejected = withTask(OPEN_CARD, "task-watch", { status: "cancelled" });
    const reviewCards = vi.fn().mockResolvedValueOnce(cards(OPEN_CARD)).mockResolvedValue(cards(rejected));
    const { apiClient, onEditTask, onChanged, onNotify } = setup({ reviewCards });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("持续盯预约场次")).getByRole("button", { name: /^修改「/ }));
    expect(onEditTask).toHaveBeenCalledWith(expect.objectContaining({ id: "task-watch" }));

    await userEvent.click(within(row("持续盯预约场次")).getByRole("button", { name: /^驳回「/ }));
    expect(apiClient.rejectTask).toHaveBeenCalledWith("task-watch");
    expect(await within(row("持续盯预约场次")).findByText("已驳回")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "待确认任务 2" })).toBeInTheDocument();

    await userEvent.click(within(row("持续盯预约场次")).getByRole("button", { name: /撤销/ }));
    expect(apiClient.undoTaskReview).toHaveBeenCalledWith(["task-watch"]);
    await waitFor(() => expect(onNotify).toHaveBeenLastCalledWith("已撤销驳回"));
  });

  it("已驳回的行过了 10 分钟撤销窗口就只剩「已驳回」，窗口内才有撤销", async () => {
    const rejected = withTask(OPEN_CARD, "task-watch", { status: "cancelled" });
    try {
      vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-09-30T02:37:00+00:00") }); // 刚好 10 分钟
      const first = setup({}, {}, cards(rejected));
      await screen.findByRole("region", { name: /260929/ });
      expect(within(row("持续盯预约场次")).getByText("已驳回")).toBeInTheDocument();
      expect(within(row("持续盯预约场次")).getByRole("button", { name: /^撤销驳回/ })).toBeInTheDocument();
      first.unmount();

      vi.setSystemTime(new Date("2026-09-30T02:37:01+00:00")); // 超过 1 秒
      setup({}, {}, cards(rejected));
      await screen.findByRole("region", { name: /260929/ });
      expect(within(row("持续盯预约场次")).getByText("已驳回")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^撤销驳回/ })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("全部确认：调接口，成功提示带撤销、撤销用 confirmed，并给「已撤销」", async () => {
    const confirmAllInMeeting = vi.fn().mockResolvedValue({
      confirmed: ["task-fill", "task-watch", "task-script"],
      failed: [],
      linked: { "task-fill": EXPORT_OPTION, "task-watch": null, "task-script": EXPORT_OPTION },
    });
    const { apiClient, onNotify, onChanged } = setup({ confirmAllInMeeting });
    await screen.findByRole("region", { name: /260929/ });
    expect(screen.getByText("剩下的待确认任务按推荐一并确认，10 分钟内可撤销")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "全部确认" }));

    expect(confirmAllInMeeting).toHaveBeenCalledWith(MEETING_ID);
    await waitFor(() => expect(onNotify).toHaveBeenCalledTimes(1));
    expect(onNotify.mock.calls[0][0]).toBe("已确认 3 条任务，其中 2 条挂到需求");
    expect(onNotify.mock.calls[0][2]).toBeUndefined();
    expect(onChanged).toHaveBeenCalled();
    await (onNotify.mock.calls[0][1] as () => Promise<void>)();
    expect(apiClient.undoTaskReview).toHaveBeenCalledWith(["task-fill", "task-watch", "task-script"]);
    expect(onNotify).toHaveBeenLastCalledWith("已撤销 3 条确认");
  });

  it("全部确认部分失败：合成一条提示，写明确认几条、没确认上的原因，带撤销，tone 是 error", async () => {
    const confirmAllInMeeting = vi.fn().mockResolvedValue({
      confirmed: ["task-fill", "task-script"],
      failed: [{ task_id: "task-watch", error: "任务状态已变化" }],
      linked: { "task-fill": EXPORT_OPTION, "task-script": EXPORT_OPTION },
    });
    const { apiClient, onNotify } = setup({ confirmAllInMeeting });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(screen.getByRole("button", { name: "全部确认" }));

    await waitFor(() => expect(onNotify).toHaveBeenCalledTimes(1));
    expect(onNotify).toHaveBeenCalledWith(
      "已确认 2 条任务，其中 2 条挂到需求；1 条没确认上：任务状态已变化",
      expect.any(Function),
      "error",
    );
    await (onNotify.mock.calls[0][1] as () => Promise<void>)();
    expect(apiClient.undoTaskReview).toHaveBeenCalledWith(["task-fill", "task-script"]);
  });

  it("全部确认不无视卡上改过的挂接：改选过的先逐条按他选的确认，其余交给批量，撤销覆盖全部", async () => {
    const taskRequirementOptions = vi.fn().mockResolvedValue({
      task_id: "task-script",
      can_link_candidates: true,
      recommended: [EXPORT_OPTION],
      default: EXPORT_OPTION,
      options: [SIX_OPTION, EXPORT_OPTION],
      current: null,
    });
    const confirmAllInMeeting = vi.fn().mockResolvedValue({
      confirmed: ["task-fill", "task-watch"],
      failed: [],
      linked: { "task-fill": EXPORT_OPTION, "task-watch": null },
    });
    const { apiClient, onNotify } = setup({ taskRequirementOptions, confirmAllInMeeting });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /挂到需求/ }));
    await userEvent.click(await screen.findByRole("option", { name: /直播间运营六项修正/ }));
    await userEvent.click(screen.getByRole("button", { name: "全部确认" }));

    await waitFor(() => expect(onNotify).toHaveBeenCalledTimes(1));
    expect(apiClient.confirmTask).toHaveBeenCalledTimes(1);
    expect(apiClient.confirmTask).toHaveBeenCalledWith("task-script", { requirement_id: "requirement-six" });
    expect(confirmAllInMeeting).toHaveBeenCalledWith(MEETING_ID);
    expect(onNotify.mock.calls[0][0]).toBe("已确认 3 条任务，其中 2 条挂到需求");
    await (onNotify.mock.calls[0][1] as () => Promise<void>)();
    expect(apiClient.undoTaskReview).toHaveBeenCalledWith(["task-script", "task-fill", "task-watch"]);
  });

  it("全部确认时手选的那条没确认上：不再批量，免得被按推荐挂上", async () => {
    const taskRequirementOptions = vi.fn().mockResolvedValue({
      task_id: "task-script",
      can_link_candidates: true,
      recommended: [EXPORT_OPTION],
      default: EXPORT_OPTION,
      options: [SIX_OPTION, EXPORT_OPTION],
      current: null,
    });
    const confirmTask = vi.fn().mockRejectedValue(new Error("需求已归档"));
    const { apiClient, onNotify } = setup({ taskRequirementOptions, confirmTask });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /挂到需求/ }));
    await userEvent.click(await screen.findByRole("option", { name: /直播间运营六项修正/ }));
    await userEvent.click(screen.getByRole("button", { name: "全部确认" }));

    await waitFor(() => expect(onNotify).toHaveBeenCalledWith("1 条没确认上：需求已归档", undefined, "error"));
    expect(apiClient.confirmAllInMeeting).not.toHaveBeenCalled();
  });

  it("同一帧连点两下确认只发一次请求", async () => {
    let release: () => void = () => {};
    const confirmTask = vi.fn().mockReturnValue(new Promise<void>((resolve) => (release = resolve)));
    setup({ confirmTask });
    await screen.findByRole("region", { name: /260929/ });

    const button = within(row("话术修改稿")).getByRole("button", { name: /^确认「/ });
    act(() => {
      button.click();
      button.click();
    });
    expect(confirmTask).toHaveBeenCalledTimes(1);
    release();
  });

  it("刚确认的行 10 分钟内行内也带撤销，过了窗口只剩已确认", async () => {
    const confirmed = withTask(OPEN_CARD, "task-script", { status: "confirmed", candidate_title: "科室会预约后台导出" });
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-09-30T02:29:00+00:00") });
    const { apiClient, onNotify, unmount } = setup({}, {}, cards(confirmed));
    await screen.findByRole("region", { name: /260929/ });
    const handled = row("话术修改稿");
    expect(within(handled).getByText("已确认 ✓")).toBeInTheDocument();
    await userEvent.click(within(handled).getByRole("button", { name: /^撤销确认「/ }));
    expect(apiClient.undoTaskReview).toHaveBeenCalledWith(["task-script"]);
    await waitFor(() => expect(onNotify).toHaveBeenLastCalledWith("已撤销确认"));
    unmount();

    vi.setSystemTime(new Date("2026-09-30T02:37:01+00:00"));
    setup({}, {}, cards(confirmed));
    await screen.findByRole("region", { name: /260929/ });
    expect(within(row("话术修改稿")).getByText("已确认 ✓")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^撤销确认「/ })).toBeNull();
  });

  it("没撤成时按错误提示，不报已撤销", async () => {
    const confirmed = withTask(OPEN_CARD, "task-script", { status: "confirmed" });
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-09-30T02:29:00+00:00") });
    const undoTaskReview = vi
      .fn()
      .mockResolvedValue({ reverted: [], failed: [{ task_id: "task-script", error: "已过撤销时间" }] });
    const { onNotify } = setup({ undoTaskReview }, {}, cards(confirmed));
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^撤销确认「/ }));
    await waitFor(() => expect(onNotify).toHaveBeenCalledWith("撤销失败：已过撤销时间", undefined, "error"));
  });

  it("这场会没有待确认任务时全部确认禁用", async () => {
    const onlyCandidate: ReviewCard = {
      ...OPEN_CARD,
      tasks: [],
      pending_task_count: 0,
    };
    setup({}, {}, cards(onlyCandidate));
    expect(await screen.findByRole("button", { name: "全部确认" })).toBeDisabled();
  });

  it("丢掉候选：提示带撤销，撤销调 restoreCandidate", async () => {
    const dropped = {
      ...OPEN_CARD,
      candidates: [{ ...CANDIDATE, status: "dropped" as const }],
      pending_candidate_count: 0,
    };
    const reviewCards = vi.fn().mockResolvedValueOnce(cards(OPEN_CARD)).mockResolvedValue(cards(dropped));
    const { apiClient, onNotify } = setup({ reviewCards });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(screen.getByRole("button", { name: /^丢掉「/ }));
    expect(apiClient.dropCandidate).toHaveBeenCalledWith("candidate-export");
    await waitFor(() => expect(onNotify).toHaveBeenCalledWith("已丢掉「科室会预约后台导出」", expect.any(Function)));
    expect(await screen.findByText("已丢掉")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^认领「/ })).toBeNull();

    await (onNotify.mock.calls[0][1] as () => Promise<void>)();
    expect(apiClient.restoreCandidate).toHaveBeenCalledWith("candidate-export");
    expect(onNotify).toHaveBeenLastCalledWith("已撤销丢掉「科室会预约后台导出」");
  });

  it("认领交给页面；can_merge 为 false 时不显示合并", async () => {
    const { onClaimCandidate, unmount } = setup();
    await screen.findByRole("region", { name: /260929/ });
    expect(screen.getByRole("button", { name: /^合并「/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^认领「/ }));
    expect(onClaimCandidate).toHaveBeenCalledWith("candidate-export");
    unmount();

    setup({}, {}, cards({ ...OPEN_CARD, candidates: [{ ...CANDIDATE, can_merge: false }] }));
    await screen.findByRole("region", { name: /260929/ });
    expect(screen.queryByRole("button", { name: /^合并「/ })).toBeNull();
  });

  it("合并：弹窗里选了需求合并成功后提示合并到哪条并重新取数", async () => {
    const mergeCandidate = vi.fn().mockResolvedValue({ id: "requirement-six", title: "直播间运营六项修正" });
    const candidateMergeTargets = vi.fn().mockResolvedValue({
      items: [
        {
          id: "requirement-six",
          title: "直播间运营六项修正",
          status: "active",
          priority: "P1",
          recommended: true,
          meeting_title: null,
          recording_date: null,
        },
      ],
    });
    const { apiClient, onNotify } = setup({ mergeCandidate, candidateMergeTargets });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(screen.getByRole("button", { name: /^合并「/ }));
    const dialog = await screen.findByRole("dialog", { name: "合并到已有需求" });
    await userEvent.click(await within(dialog).findByRole("radio"));
    await userEvent.click(within(dialog).getByRole("button", { name: "合并" }));

    expect(mergeCandidate).toHaveBeenCalledWith("candidate-export", "requirement-six", undefined);
    await waitFor(() => expect(onNotify).toHaveBeenCalledWith("已合并到「直播间运营六项修正」"));
    expect(screen.queryByRole("dialog", { name: "合并到已有需求" })).toBeNull();
    expect(apiClient.reviewCards.mock.calls.length).toBeGreaterThan(1);
  });

  it("合并进已完成的需求：提示里加一句它已重新打开（D13）", async () => {
    const mergeCandidate = vi
      .fn()
      .mockResolvedValue({ id: "requirement-six", title: "直播间运营六项修正", status: "active", reopened: true });
    const candidateMergeTargets = vi.fn().mockResolvedValue({
      items: [
        {
          id: "requirement-six",
          title: "直播间运营六项修正",
          status: "done",
          priority: "P1",
          recommended: true,
          meeting_title: null,
          recording_date: null,
        },
      ],
    });
    const { onNotify } = setup({ mergeCandidate, candidateMergeTargets });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(screen.getByRole("button", { name: /^合并「/ }));
    const dialog = await screen.findByRole("dialog", { name: "合并到已有需求" });
    expect(await within(dialog).findByText("已完成")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("radio"));
    await userEvent.click(within(dialog).getByRole("button", { name: "合并" }));

    await waitFor(() =>
      expect(onNotify).toHaveBeenCalledWith("已合并到「直播间运营六项修正」；「直播间运营六项修正」已重新打开"),
    );
  });

  it("已处理的候选置灰，写明认领到哪、合并到哪", async () => {
    const merged = { ...CANDIDATE, id: "candidate-m", title: "另一条", status: "merged" as const, requirement_title: "老需求" };
    setup(
      {},
      {},
      cards({
        ...OPEN_CARD,
        candidates: [{ ...CANDIDATE, status: "claimed", requirement_title: "科室会导出需求" }, merged],
        pending_candidate_count: 0,
      }),
    );
    await screen.findByRole("region", { name: /260929/ });
    expect(screen.getByText("已认领为「科室会导出需求」")).toBeInTheDocument();
    expect(screen.getByText("已合并到「老需求」")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^认领「/ })).toBeNull();
  });

  it("处理完的卡排在后面、整张置灰并收成一行，展开后能看里面的内容", async () => {
    setup({}, {}, cards(OPEN_CARD, DONE_CARD));

    const collapsed = await screen.findByRole("region", { name: "260916 医米京东科研仓系统对接" });
    expect(collapsed).toHaveClass("is-done", "is-collapsed");
    expect(within(collapsed).getByText("需求候选 1 · 任务 1")).toBeInTheDocument();
    expect(screen.queryByText("已认领为「京东科研仓对接」")).toBeNull();
    // 置灰的卡在后
    const regions = screen.getAllByRole("region").map((item) => item.getAttribute("aria-label"));
    expect(regions).toEqual(["260929 云课堂直播运营问题对齐", "260916 医米京东科研仓系统对接"]);

    await userEvent.click(within(collapsed).getByRole("button", { name: /展开/ }));
    expect(screen.getByText("已认领为「京东科研仓对接」")).toBeInTheDocument();
    expect(screen.getByText("已确认 ✓")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.queryByText("已认领为「京东科研仓对接」")).toBeNull();
  });

  it("canWrite 为 false 时不出任何写按钮", async () => {
    setup({}, { canWrite: false });
    await screen.findByRole("region", { name: /260929/ });

    for (const name of [/^确认「/, /^驳回「/, "全部确认", /^丢掉「/, /^合并「/, /^认领「/, /^撤销/]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.queryByRole("button", { name: /^修改「/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /挂到需求/ })).toBeNull();
    // 读的入口还在
    expect(screen.getByRole("button", { name: "打开会议 →" })).toBeInTheDocument();
    expect(within(row("话术修改稿")).getByText("科室会预约后台导出")).toBeInTheDocument();
  });

  it("reloadKey 变化时重新取数，筛选带给后端", async () => {
    const { apiClient, rerender } = setup({}, { filters: { project_id: PROJECT_ID, meeting_date_from: "2026-09-01" } });
    await screen.findByRole("region", { name: /260929/ });
    expect(apiClient.reviewCards).toHaveBeenCalledTimes(1);
    expect(apiClient.reviewCards).toHaveBeenCalledWith({
      project_id: PROJECT_ID,
      meeting_date_from: "2026-09-01",
      meeting_date_to: undefined,
    });

    rerender(
      <ReviewCardsPanel
        apiClient={apiClient as unknown as ApiClient}
        canWrite
        filters={{ project_id: PROJECT_ID, meeting_date_from: "2026-09-01" }}
        onChanged={vi.fn()}
        onClaimCandidate={vi.fn()}
        onEditTask={vi.fn()}
        onNotify={vi.fn()}
        onOpenMeeting={vi.fn()}
        reloadKey={1}
      />,
    );
    await waitFor(() => expect(apiClient.reviewCards).toHaveBeenCalledTimes(2));
  });

  it("页面可见时每 20 秒轮询一次", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { apiClient } = setup();
      await screen.findByRole("region", { name: /260929/ });
      expect(apiClient.reviewCards).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(apiClient.reviewCards).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("互斥是页面给的：页面那边有写操作（比如提示条上的［撤销］）在跑，卡上的按钮置灰，也进不去", async () => {
    const { apiClient } = setup();
    await screen.findByRole("region", { name: /260929/ });
    let finish: () => void = () => undefined;
    act(() => {
      void pageMutex.run(
        () =>
          new Promise<void>((resolve) => {
            finish = resolve;
          }),
      );
    });

    const confirm = within(row("话术修改稿")).getByRole("button", { name: /^确认「/ });
    expect(confirm).toBeDisabled();
    expect(screen.getByRole("button", { name: "全部确认" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^丢掉「/ })).toBeDisabled();
    await userEvent.click(confirm);
    expect(apiClient.confirmTask).not.toHaveBeenCalled();

    await act(async () => finish());
    await waitFor(() => expect(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ })).toBeEnabled());
  });

  it("卡上的写操作在跑，页面的互斥也被占着：提示条上的［撤销］排在它后面", async () => {
    let release: () => void = () => undefined;
    const confirmTask = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    setup({ confirmTask });
    await screen.findByRole("region", { name: /260929/ });
    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ }));
    await waitFor(() => expect(confirmTask).toHaveBeenCalledTimes(1));
    expect(pageMutex.busy).toBe(true);

    const undo = vi.fn(async () => undefined);
    let queued: Promise<void> = Promise.resolve();
    act(() => {
      queued = pageMutex.runAfterCurrent(undo);
    });
    expect(undo).not.toHaveBeenCalled();

    release();
    await act(async () => {
      await queued;
    });
    expect(undo).toHaveBeenCalledTimes(1);
  });

  it("写操作失败时用提示条报原因，不动列表", async () => {
    const confirmTask = vi.fn().mockRejectedValue(new Error("任务已被处理"));
    const { apiClient, onNotify } = setup({ confirmTask });
    await screen.findByRole("region", { name: /260929/ });

    await userEvent.click(within(row("话术修改稿")).getByRole("button", { name: /^确认「/ }));
    await waitFor(() => expect(onNotify).toHaveBeenCalledWith("确认失败：任务已被处理", undefined, "error"));
    // 失败后立刻按最新状态重新取数（行可能已被别处改了）
    await waitFor(() => expect(apiClient.reviewCards.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByRole("heading", { name: "待确认任务 3" })).toBeInTheDocument();
  });
});
