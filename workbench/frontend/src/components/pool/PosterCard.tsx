import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";

import { formatBeijingMonthDay, formatDurationText, formatMonthDay, formatMonthDayClock, formatTime } from "../../format";
import type { PoolItem, RequirementStatus } from "../../types";
import { REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import { RowMenu, type RowMenuItem } from "../todo/RowMenu";
import { PosterWaveform } from "./PosterWaveform";
import "./PosterCard.css";

interface PosterCardProps {
  item: PoolItem;
  canWrite: boolean;
  /** 新增、认领页右侧的「墙上预览」：只看样子，按钮不响应 */
  preview?: boolean;
  /** 点海报：需求进详情，候选进认领页（R02-5） */
  onOpen?: (item: PoolItem) => void;
  /** 点波形或原话时间：打开这场会，从这一秒开始放（R02-3） */
  onOpenMeeting?: (meetingId: string, atMs: number) => void;
  onClaim?: (item: PoolItem) => void;
  onMerge?: (item: PoolItem) => void;
  onDrop?: (item: PoolItem) => void;
  /**
   * 点「接下」：把这条需求的背景复制出去，不改需求状态（R02-9）。要在点击的手势里同步发起复制，
   * 所以这里只管调用；复制成功返回 true，按钮这才变「已复制」2 秒。
   */
  onTake?: (item: PoolItem) => Promise<boolean>;
  /** 需求票根「⋯」里的标记完成、搁置、重新打开（D1），以及「待办都清了」那一行的［标记完成］（D14） */
  onSetStatus?: (item: PoolItem, status: RequirementStatus) => void;
  /** 需求票根「⋯」里的「并入其他需求…」（D1） */
  onMergeInto?: (item: PoolItem) => void;
  /** 认领、新建后落位的那张：描边 3 秒（R01-13） */
  highlighted?: boolean;
}

/** 「已复制」在按钮上停多久 */
const TAKE_FEEDBACK_MS = 2000;

export function anchorLabel(milliseconds: number): string {
  return formatTime(milliseconds, true);
}

/**
 * 海报底栏的日子：已完成、已搁置的需求写状态变更那天（D12，北京日历）；
 * 其余出自哪场会就写那场会的日子，没有来源写建的日子
 */
export function posterDateLabel(item: PoolItem): { day: string; text: string } {
  if (item.kind === "requirement" && (item.status === "done" || item.status === "shelved")) {
    return {
      day: formatBeijingMonthDay(item.status_changed_at || item.updated_at),
      text: item.status === "done" ? "完成" : "搁置",
    };
  }
  if (item.source) return { day: formatMonthDay(item.source.recording_date), text: "会上提出" };
  return { day: formatMonthDay(item.created_at), text: "新建" };
}

function LinkIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16" width="12">
      <path d="M6.6 9.4l2.8-2.8" />
      <path d="M7.4 4.6l1.1-1.1a2.8 2.8 0 014 4l-1.1 1.1" />
      <path d="M8.6 11.4l-1.1 1.1a2.8 2.8 0 01-4-4l1.1-1.1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" viewBox="0 0 16 16" width="12">
      <path d="M3.2 8.6l3 3 6.6-7.2" />
    </svg>
  );
}

function stop(event: MouseEvent) {
  event.stopPropagation();
}

export function PosterCard({
  item,
  canWrite,
  preview = false,
  onOpen,
  onOpenMeeting,
  onClaim,
  onMerge,
  onDrop,
  onTake,
  onSetStatus,
  onMergeInto,
  highlighted = false,
}: PosterCardProps) {
  const candidate = item.kind === "candidate";
  const stamped = !candidate && (item.status === "done" || item.status === "shelved");
  const source = item.source;
  const date = posterDateLabel(item);
  const open = () => {
    if (!preview) onOpen?.(item);
  };
  const [takeState, setTakeState] = useState<"idle" | "busy" | "copied">("idle");
  const resetTimer = useRef<number | null>(null);
  const mounted = useRef(true);
  // StrictMode 开发时会先卸载再挂一次：挂上时要把 mounted 重新置回 true，不然复制完按钮卡在「复制中…」
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    };
  }, []);
  const take = () => {
    if (preview || !onTake || takeState === "busy") return;
    setTakeState("busy");
    // onTake 在这一行同步调用：复制要在点击的手势里发起
    void onTake(item).then(
      (copied) => {
        if (!mounted.current) return;
        if (!copied) {
          setTakeState("idle");
          return;
        }
        setTakeState("copied");
        if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
        resetTimer.current = window.setTimeout(() => setTakeState("idle"), TAKE_FEEDBACK_MS);
      },
      () => {
        if (mounted.current) setTakeState("idle");
      },
    );
  };
  const openSource = () => {
    if (preview || !source || !onOpenMeeting) return;
    onOpenMeeting(source.meeting_id, source.anchor_ms ?? 0);
  };
  const meta = source
    ? [
        formatMonthDayClock(source.recording_date),
        source.duration_ms ? formatDurationText(source.duration_ms) : null,
        item.follow_up_count > 0 ? `跟进 ${item.follow_up_count} 场` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "";
  const mergeFirst = candidate && item.default_action === "merge" && item.can_merge;

  // 「⋯」菜单（D1）：进行中的给标记完成、搁置，已完成、已搁置的给重新打开；都能并入其他需求
  const writable = canWrite && !preview && !candidate;
  const menu: RowMenuItem[] = [];
  if (writable && onSetStatus) {
    if (item.status === "active") {
      menu.push({ label: "标记完成", act: () => onSetStatus(item, "done") });
      menu.push({ label: "搁置", act: () => onSetStatus(item, "shelved") });
    } else {
      menu.push({ label: "重新打开", act: () => onSetStatus(item, "active") });
    }
  }
  if (writable && onMergeInto) menu.push({ label: "并入其他需求…", act: () => onMergeInto(item) });
  const [menuOpen, setMenuOpen] = useState(false);
  // 待办都清了（名下有过待办、没做完的为 0）还挂在进行中：问一句完成了吗（D14）
  const allCleared =
    writable && onSetStatus !== undefined && item.status === "active" && (item.task_count ?? 0) >= 1 && item.open_task_count === 0;

  return (
    <article
      aria-label={`${candidate ? "候选" : "需求"}：${item.title}`}
      className={`poster ${candidate ? "poster--candidate" : ""} ${preview ? "poster--preview" : ""} ${
        highlighted ? "poster--highlight" : ""
      } ${menuOpen ? "is-menu-open" : ""}`}
      data-poster-id={item.id}
      data-status={item.status}
      onClick={open}
      onKeyDown={(event: KeyboardEvent) => {
        if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          open();
        }
      }}
      tabIndex={preview ? -1 : 0}
    >
      {/* 封面：提出它的那场会的真实波形（R02-3）；没有来源时换成一条静音线，同一排的标题照样对齐 */}
      {source ? (
        <section className="poster__cover">
          <div className="poster__cover-top">
            <p className="poster__meeting">{source.meeting_title}</p>
            {source.anchor_ms !== null && (
              <button
                className="poster__anchor"
                onClick={(event) => {
                  stop(event);
                  openSource();
                }}
                tabIndex={preview ? -1 : 0}
                type="button"
              >
                <span aria-hidden="true" className="poster__anchor-play" />
                原话 {anchorLabel(source.anchor_ms)}
              </button>
            )}
          </div>
          <PosterWaveform
            artifactId={source.audio_artifact_id}
            height={30}
            label={`${source.meeting_title} 的录音波形`}
            markers={source.anchor_ms !== null ? [{ atMs: source.anchor_ms }] : []}
            onActivate={preview || !onOpenMeeting ? undefined : openSource}
          />
          <p className="poster__meeting-meta">{meta}</p>
        </section>
      ) : (
        <div className="poster__cover is-silent">
          <span aria-hidden="true" className="poster__silence" />
          <span className="poster__silence-label">没有来源录音</span>
        </div>
      )}

      <div className="poster__body">
        <header className="poster__head">
          <span className={`poster__project ${item.project_id ? "" : "is-unassigned"}`}>
            {item.project_seat !== null && <span className="poster__seat">{item.project_seat}</span>}
            <span className="poster__project-name">{item.project_name ?? "未归项目"}</span>
          </span>
          {candidate ? (
            <span className="poster__tag poster__tag--ai">AI 候选</span>
          ) : (
            item.priority && (
              <span className={`poster__tag poster__tag--${item.priority.toLowerCase()}`}>{item.priority}</span>
            )
          )}
        </header>

        {stamped ? (
          // 已完成、已搁置在需求名后面盖章（D12），和详情页头部一个意思
          <div className="poster__title-row">
            <h3 className={`poster__title ${item.title ? "" : "is-placeholder"}`}>{item.title || "需求名"}</h3>
            <span className={`poster__stamp poster__stamp--${item.status}`}>
              {REQUIREMENT_STATUS_LABELS[item.status as RequirementStatus]}
            </span>
          </div>
        ) : (
          <h3 className={`poster__title ${item.title ? "" : "is-placeholder"}`}>{item.title || "需求名"}</h3>
        )}
        {(item.summary || preview) && (
          <p
            className={`poster__summary ${item.summary ? "" : "is-placeholder"} ${
              candidate && item.similar_requirement ? "is-short" : ""
            }`}
          >
            {item.summary || "说明"}
          </p>
        )}
        {candidate && item.similar_requirement && (
          <p className="poster__similar">
            <LinkIcon />
            像已有需求：<b>{item.similar_requirement.title}</b>
          </p>
        )}
      </div>

      {/* 票根：虚线撕口下面是数字和操作 */}
      <div aria-hidden="true" className="poster__perf" />
      <div className="poster__stub">
        <dl className="poster__stats">
          {(
            [
              ["待办", item.open_task_count],
              ["会议", item.meeting_count],
              ["材料", item.folder_count],
            ] as const
          ).map(([label, count]) => (
            <div className={count ? "" : "is-zero"} key={label}>
              <dt>{label}</dt>
              <dd>{count}</dd>
            </div>
          ))}
        </dl>

        {allCleared && (
          <p className="poster__nudge" onClick={stop}>
            <span>待办都清了，完成了吗？</span>
            <button className="poster__nudge-action" onClick={() => onSetStatus?.(item, "done")} type="button">
              标记完成
            </button>
          </p>
        )}

        <footer className="poster__foot">
          <span className="poster__date">
            <b>{date.day}</b> {date.text}
          </span>
          {candidate ? (
            canWrite && (
              <span className="poster__actions" onClick={stop}>
                <button className="poster__text-action" disabled={preview} onClick={() => onDrop?.(item)} type="button">
                  丢掉
                </button>
                {item.can_merge && (
                  <button
                    className={`poster__pill ${mergeFirst ? "is-primary" : ""}`}
                    disabled={preview}
                    onClick={() => onMerge?.(item)}
                    type="button"
                  >
                    合并
                  </button>
                )}
                <button
                  className={`poster__pill ${mergeFirst ? "" : "is-primary"}`}
                  disabled={preview}
                  onClick={() => onClaim?.(item)}
                  type="button"
                >
                  认领 <span aria-hidden="true">→</span>
                </button>
              </span>
            )
          ) : (
            <span className="poster__actions" onClick={stop}>
              <button className="poster__text-action" disabled={preview} onClick={open} type="button">
                查看
              </button>
              <button
                className={`poster__pill ${takeState === "copied" ? "is-copied" : ""}`}
                disabled={preview || takeState === "busy"}
                onClick={take}
                type="button"
              >
                {takeState === "copied" ? (
                  <>
                    <CheckIcon /> 已复制
                  </>
                ) : takeState === "busy" ? (
                  "复制中…"
                ) : (
                  <>
                    接下 <span aria-hidden="true">→</span>
                  </>
                )}
              </button>
              {menu.length > 0 && (
                <RowMenu items={menu} label={`更多操作：${item.title}`} onOpenChange={setMenuOpen} open={menuOpen} />
              )}
            </span>
          )}
        </footer>
      </div>
    </article>
  );
}
