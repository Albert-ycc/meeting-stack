/* 关系图各个面板共用的小部件：播放按钮、分节、路径、任务状态、能撤销的一步 */
import type { ReactNode } from "react";

import type { ApiClient } from "../../api";
import { copyText } from "../../clipboard";
import { formatTime } from "../../format";
import type { MiniPlayerHandle } from "./MiniPlayer";

export const TASK_STATUS: Record<string, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
};

/**
 * 画布上能撤销的一步：带［撤销］的提示、⌘Z、残影都走这里。改归属的撤销期由服务器定（10 分钟）；
 * 关联、解除需求，搬任务、改任务由前端记下原样，同样只留 10 分钟。
 */
export type GraphNoticeUndo =
  | { kind: "project"; meetingId: string; until: string }
  | { kind: "link"; requirementId: string; meetingId: string; title: string; until: string }
  /** 解除了会和需求的关联；撤销就重新关联 */
  | { kind: "unlink"; requirementId: string; meetingId: string; title: string; until: string }
  | {
      kind: "task";
      taskId: string;
      title: string;
      /** move：搬到别的项目或需求；edit：改了标题、说明。before 是改之前的原样 */
      what: "move" | "edit";
      before: { project_id?: string | null; requirement_id?: string | null; title?: string; detail?: string };
      until: string;
    };

/** 前端自己记的撤销（关联需求、搬任务）也只留 10 分钟 */
export const LOCAL_UNDO_MS = 10 * 60_000;

export function localUndoUntil(now = Date.now()) {
  return new Date(now + LOCAL_UNDO_MS).toISOString();
}

export type NoticeFn = (message: string, undo?: GraphNoticeUndo, tone?: "success" | "warning" | "error") => void;

export function PlayButton({
  audioUrl,
  atMs,
  label,
  player,
}: {
  audioUrl: string | null | undefined;
  atMs: number | null | undefined;
  label: string;
  player: MiniPlayerHandle;
}) {
  if (atMs === null || atMs === undefined) return null;
  return (
    <button
      aria-label={`从 ${formatTime(atMs)} 播放`}
      className="graph-play"
      disabled={!audioUrl}
      onClick={() => audioUrl && player.play(audioUrl, atMs, label)}
      title={audioUrl ? undefined : "这场会没有录音文件"}
      type="button"
    >
      ▶ {formatTime(atMs)}
    </button>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="graph-panel__section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

/** 路径和［复制路径］；本机打开时再给［在访达中显示］ */
export function CopyPath({
  path,
  onNotice,
  apiClient,
  canReveal = false,
}: {
  path: string;
  onNotice: NoticeFn;
  apiClient?: ApiClient;
  canReveal?: boolean;
}) {
  return (
    <p className="graph-panel__path">
      <code>{path}</code>
      <span className="graph-panel__path-actions">
        <button
          className="text-button"
          onClick={async () => {
            try {
              await copyText(path);
              onNotice("已复制路径");
            } catch {
              onNotice("复制失败，请手动选中路径", undefined, "error");
            }
          }}
          type="button"
        >
          复制路径
        </button>
        {canReveal && apiClient && (
          <button
            className="text-button"
            onClick={async () => {
              try {
                await apiClient.revealMaterial(path);
              } catch (reason) {
                onNotice(reason instanceof Error ? reason.message : "打不开访达", undefined, "error");
              }
            }}
            type="button"
          >
            在访达中显示
          </button>
        )}
      </span>
    </p>
  );
}
