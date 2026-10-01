import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubPeaksFetch } from "./peaksFixtures";
import { candidateItem, EXPORT_SOURCE, JD_SOURCE, requirementItem } from "./poolFixtures";
import { PosterCard } from "./PosterCard";
import { clearPeaksCache } from "./PosterWaveform";

describe("PosterCard", () => {
  it("需求海报：座次和项目、等级、需求名、说明、三个数、出自录音和会上提出的日子", () => {
    render(<PosterCard canWrite item={requirementItem()} />);
    const poster = screen.getByRole("article", { name: "需求：京东科研仓对接" });

    const project = within(poster).getByText("医米科研用药").parentElement!;
    expect(project).toHaveTextContent("1医米科研用药");
    expect(within(project).getByText("1")).toHaveClass("poster__seat");
    expect(within(poster).getByText("P0")).toHaveClass("poster__tag--p0");
    expect(within(poster).getByRole("heading", { name: "京东科研仓对接" })).toBeInTheDocument();
    expect(within(poster).getByText(/采购单入库、销售单/)).toBeInTheDocument();
    expect(within(poster).getByText("会议").nextElementSibling).toHaveTextContent("2");
    expect(within(poster).getByText("材料").nextElementSibling).toHaveTextContent("1");
    expect(within(poster).getByRole("button", { name: /原话 00:13:45/ })).toBeInTheDocument();
    expect(within(poster).getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    // 用例钉在北京时间：-07:00 的 09-16 19:01 是 09-17 10:01；51 分钟；之后又跟进 1 场
    expect(within(poster).getByText("09-17 10:01 · 51 分钟 · 跟进 1 场")).toBeInTheDocument();
    expect(within(poster).getByText("09-17").parentElement).toHaveTextContent("09-17 会上提出");
  });

  it("点原话时间打开这场会、从那一秒开始放；点海报、查看进详情，「接下」不进详情", async () => {
    const onOpen = vi.fn();
    const onOpenMeeting = vi.fn();
    render(<PosterCard canWrite item={requirementItem()} onOpen={onOpen} onOpenMeeting={onOpenMeeting} />);

    await userEvent.click(screen.getByRole("button", { name: /原话 00:13:45/ }));
    expect(onOpenMeeting).toHaveBeenCalledWith("vm-20260916-190150-2eebb406", 825270);
    expect(onOpen).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /接下/ }));
    expect(onOpen).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "查看" }));
    await userEvent.click(screen.getByRole("heading", { name: "京东科研仓对接" }));
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("没有来源的需求不显示出自录音，底栏写建的日子", () => {
    render(<PosterCard canWrite item={requirementItem({ source: null, created_at: "2026-09-30T02:00:00+00:00" })} />);

    expect(screen.queryByText("出自录音")).not.toBeInTheDocument();
    expect(screen.getByText("09-30").parentElement).toHaveTextContent("09-30 新建");
  });

  it("候选海报：AI 候选、像已有需求，默认动作是合并时合并按钮在前", async () => {
    const onDrop = vi.fn();
    const onMerge = vi.fn();
    const onClaim = vi.fn();
    render(<PosterCard canWrite item={candidateItem()} onClaim={onClaim} onDrop={onDrop} onMerge={onMerge} />);
    const poster = screen.getByRole("article", { name: "候选：京东仓签收凭证" });

    expect(within(poster).getByText("AI 候选")).toBeInTheDocument();
    expect(within(poster).getByText("京东科研仓对接").parentElement).toHaveTextContent("像已有需求：京东科研仓对接");
    expect(within(poster).getByRole("button", { name: "合并" })).toHaveClass("is-primary");
    expect(within(poster).getByRole("button", { name: /认领/ })).not.toHaveClass("is-primary");

    await userEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    await userEvent.click(within(poster).getByRole("button", { name: "合并" }));
    await userEvent.click(within(poster).getByRole("button", { name: /认领/ }));
    expect(onDrop).toHaveBeenCalledTimes(1);
    expect(onMerge).toHaveBeenCalledTimes(1);
    expect(onClaim).toHaveBeenCalledTimes(1);
  });

  it("候选所属项目下没有可合并的需求时不给合并；未归项目的写「未归项目」、没有座次", () => {
    render(
      <PosterCard
        canWrite
        item={candidateItem({
          title: "医生资质 AI 审核规则",
          project_id: null,
          project_name: null,
          project_seat: null,
          similar_requirement: null,
          default_action: "claim",
          can_merge: false,
          source: EXPORT_SOURCE,
        })}
      />,
    );

    expect(screen.getByText("未归项目")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "合并" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /认领/ })).toHaveClass("is-primary");
  });

  it("墙上预览：按钮不响应，空的需求名和说明给占位", async () => {
    const onOpen = vi.fn();
    render(
      <PosterCard
        canWrite={false}
        item={requirementItem({ title: "", summary: "", source: null })}
        onOpen={onOpen}
        preview
      />,
    );

    expect(screen.getByRole("heading", { name: "需求名" })).toHaveClass("is-placeholder");
    expect(screen.getByText("说明")).toHaveClass("is-placeholder");
    await userEvent.click(screen.getByRole("button", { name: /接下/ }));
    expect(onOpen).not.toHaveBeenCalled();
  });
});

describe("PosterCard「接下」（R02-9，S05-b）", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("点「接下」在点击的那一下同步调 onTake（复制要在手势里发起）；不进详情；复制成功按钮变「已复制」，2 秒后恢复", async () => {
    vi.useFakeTimers();
    const onTake = vi.fn().mockResolvedValue(true);
    const onOpen = vi.fn();
    render(<PosterCard canWrite item={requirementItem()} onOpen={onOpen} onTake={onTake} />);

    fireEvent.click(screen.getByRole("button", { name: /接下/ }));
    // 点击处理函数返回时 onTake 已经调过了：中间没有 await
    expect(onTake).toHaveBeenCalledTimes(1);
    expect(onTake).toHaveBeenCalledWith(expect.objectContaining({ id: "requirement-jd", kind: "requirement" }));
    expect(onOpen).not.toHaveBeenCalled();

    await act(async () => undefined);
    expect(screen.getByRole("button", { name: "已复制" })).toHaveClass("is-copied");
    act(() => {
      vi.advanceTimersByTime(1_900);
    });
    expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.getByRole("button", { name: /接下/ })).not.toHaveClass("is-copied");
    expect(screen.queryByRole("button", { name: "已复制" })).not.toBeInTheDocument();
  });

  it("复制中按钮置灰、连点只发一次；没复制成功（返回 false）按钮回到「接下」，不变成已复制", async () => {
    let finish: (copied: boolean) => void = () => undefined;
    const onTake = vi.fn(() => new Promise<boolean>((resolve) => (finish = resolve)));
    render(<PosterCard canWrite item={requirementItem()} onTake={onTake} />);

    fireEvent.click(screen.getByRole("button", { name: /接下/ }));
    const busy = screen.getByRole("button", { name: "复制中…" });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(onTake).toHaveBeenCalledTimes(1);

    await act(async () => finish(false));
    expect(screen.getByRole("button", { name: /接下/ })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /已复制/ })).not.toBeInTheDocument();
  });

  it("onTake 出了意外（reject）也不卡在「复制中」，按钮回到「接下」", async () => {
    const onTake = vi.fn().mockRejectedValue(new Error("boom"));
    render(<PosterCard canWrite item={requirementItem()} onTake={onTake} />);

    fireEvent.click(screen.getByRole("button", { name: /接下/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /接下/ })).toBeEnabled());
  });

  it("开发时的 StrictMode（挂上、卸掉、再挂上）下，复制完按钮照样变「已复制」，不卡在「复制中…」", async () => {
    const onTake = vi.fn().mockResolvedValue(true);
    render(
      <StrictMode>
        <PosterCard canWrite item={requirementItem()} onTake={onTake} />
      </StrictMode>,
    );

    fireEvent.click(screen.getByRole("button", { name: /接下/ }));
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
  });

  it("墙上预览里「接下」置灰、不调 onTake", () => {
    const onTake = vi.fn();
    render(<PosterCard canWrite={false} item={requirementItem()} onTake={onTake} preview />);

    expect(screen.getByRole("button", { name: /接下/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /接下/ }));
    expect(onTake).not.toHaveBeenCalled();
  });

  it("没有 onTake 时点「接下」什么也不发生（不再退回成打开详情）", async () => {
    const onOpen = vi.fn();
    render(<PosterCard canWrite item={requirementItem()} onOpen={onOpen} />);

    await userEvent.click(screen.getByRole("button", { name: /接下/ }));
    expect(onOpen).not.toHaveBeenCalled();
  });
});

describe("PosterCard 认领后的描边（R01-13，S02-c）", () => {
  it("highlighted 时外框带描边的 class，海报带 data-poster-id（页面靠它找到那一张）", () => {
    const { rerender } = render(<PosterCard canWrite item={requirementItem()} />);
    const poster = screen.getByRole("article", { name: "需求：京东科研仓对接" });
    expect(poster).toHaveAttribute("data-poster-id", "requirement-jd");
    expect(poster).not.toHaveClass("poster--highlight");

    rerender(<PosterCard canWrite highlighted item={requirementItem()} />);
    expect(screen.getByRole("article", { name: "需求：京东科研仓对接" })).toHaveClass("poster--highlight");
  });
});

describe("PosterCard 来源录音的波形（R02 异常：取不到就不画）", () => {
  beforeEach(() => clearPeaksCache());
  afterEach(() => vi.unstubAllGlobals());

  // 京东科研仓对接那场会：录音 id 随便给一个，峰值的时长取这场会的真实时长
  const withAudio = requirementItem({ source: { ...JD_SOURCE, audio_artifact_id: 77 } });

  it("取到真实波形就画出来；点波形打开这场会，从原话那一秒（00:13:45）开始放", async () => {
    stubPeaksFetch({ 77: JD_SOURCE.duration_ms! });
    const onOpenMeeting = vi.fn();
    const onOpen = vi.fn();
    render(<PosterCard canWrite item={withAudio} onOpen={onOpen} onOpenMeeting={onOpenMeeting} />);

    const wave = await screen.findByRole("button", { name: "260916 医米京东科研仓系统对接 的录音波形" });
    fireEvent.click(wave);
    expect(onOpenMeeting).toHaveBeenCalledWith("vm-20260916-190150-2eebb406", 825270);
    // 点波形是打开会议，不是进需求详情
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("这场会没有录音文件：不画波形，也不留占位；会议信息和原话时间锚照常", () => {
    stubPeaksFetch({});
    const { container } = render(<PosterCard canWrite item={requirementItem()} />);

    expect(container.querySelector(".poster-wave")).toBeNull();
    expect(screen.getByRole("button", { name: /原话 00:13:45/ })).toBeInTheDocument();
    expect(screen.getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
  });

  it("峰值接口失败：占位先在、取失败后撤掉，不留空位；原话时间锚和会议信息还在", async () => {
    stubPeaksFetch({ 77: "fail" });
    const { container } = render(<PosterCard canWrite item={withAudio} />);

    await waitFor(() => expect(container.querySelector(".poster-wave")).toBeNull());
    expect(screen.queryByRole("img", { name: /录音波形/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /原话 00:13:45/ })).toBeInTheDocument();
    expect(screen.getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
  });

  it("还在取的时候占位在（海报高度不跳）", () => {
    stubPeaksFetch({ 77: "hang" });
    const { container } = render(<PosterCard canWrite item={withAudio} />);

    expect(container.querySelector(".poster-wave")).not.toBeNull();
    expect(container.querySelector(".poster-wave svg")).toBeNull();
  });
});
