import { useState } from "react";

import type { ApiClient } from "../api";
import type { ProjectCardsSummary } from "../types";
import "./ProjectCardsRow.css";

interface ProjectCardsRowProps {
  apiClient: ApiClient;
  projectId: string;
  cards: ProjectCardsSummary;
  canWrite: boolean;
  /** 打开文件夹只在桌面端（在运行声档的 Mac 上开访达） */
  canPickFolders: boolean;
  onCopy: (text: string, done: string) => void | Promise<void>;
  /** 恢复写入、开启、补写之后重读看板 */
  onChanged: () => void | Promise<void>;
}

/** 给 Claude Code 的一句话：在项目文件夹里打开 Claude Code 时，先读这个 */
export const CLAUDE_CODE_HINT = "会议记录在 ./声档会议记录/，先读 00 索引.md；原话在 逐字稿/ 里，时间戳是录音时间。";

const STOPPED_COPY: Partial<Record<NonNullable<ProjectCardsSummary["waiting_reason"]>, string>> = {
  root_missing: "找不到项目文件夹，会议卡片先存着",
  root_in_archive: "项目文件夹在归档目录里，声档不往那里写会议卡片",
  root_shared: "这个文件夹也挂在别的项目下，会议卡片只写给先挂上的项目",
};

/** 项目页材料区的会议卡片汇总：写在哪、写了几张、几张在等、为什么在等，外加给 Claude Code 的一句话。 */
export function ProjectCardsRow({
  apiClient,
  projectId,
  cards,
  canWrite,
  canPickFolders,
  onCopy,
  onChanged,
}: ProjectCardsRowProps) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const reason = cards.waiting_reason;
  const history = cards.history ?? 0;

  const run = async (work: () => Promise<string>) => {
    setBusy(true);
    setMessage("");
    try {
      setMessage(await work());
      await onChanged();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const reveal = async () => {
    setMessage("");
    try {
      await apiClient.revealCards({ project_id: projectId });
    } catch (error) {
      // 不在 Mac 上（或文件夹还没建）时，退回复制路径
      if (cards.root) await onCopy(cards.root, "已复制卡片文件夹路径");
      else setMessage(error instanceof Error ? error.message : "打不开文件夹");
    }
  };

  const counts = [
    `已写 ${cards.written} 张`,
    cards.edited ? `其中 ${cards.edited} 张你改过，不再自动更新` : "",
    cards.missing ? `${cards.missing} 张被移走或删除` : "",
    cards.waiting
      ? `${cards.waiting} 张等待${reason === "root_offline" ? "（资料盘未连接）" : ""}`
      : "",
  ].filter(Boolean);

  let line: string;
  let action: { label: string; onClick: () => void } | null = null;
  if (reason === "disabled") {
    line = "会议卡片还没开启";
    if (canWrite) {
      action = {
        label: "开启",
        onClick: () =>
          void run(async () => {
            await apiClient.enableCards();
            return "已开启，下一轮扫描开始写会议卡片";
          }),
      };
    }
  } else if (reason === "paused") {
    line = "这个项目暂停了写会议卡片";
    if (canWrite) {
      action = {
        label: "恢复写入",
        onClick: () =>
          void run(async () => {
            const result = await apiClient.resumeProjectCards(projectId);
            return result.written ? `已恢复，补写了 ${result.written} 张会议卡片` : "已恢复写入";
          }),
      };
    }
  } else if (reason === "no_root") {
    line = `这个项目还没有文件夹，会议卡片先存着${cards.waiting ? `（${cards.waiting} 场）` : ""}，挂上文件夹后一次补写`;
  } else if (reason && STOPPED_COPY[reason]) {
    line = STOPPED_COPY[reason]!;
  } else if (cards.written === 0 && cards.waiting === 0 && cards.missing === 0) {
    line = `会议卡片会写在 ${cards.root ?? ""}/，写第一张时才建这个文件夹`;
  } else {
    line = `会议卡片写在 ${cards.root ?? ""}/ · ${counts.join(" · ")}`;
    if (cards.written > 0) {
      action = canPickFolders
        ? { label: "打开文件夹", onClick: () => void reveal() }
        : { label: "复制路径", onClick: () => void onCopy(cards.root ?? "", "已复制卡片文件夹路径") };
    }
  }

  return (
    <div className="project-cards">
      <div className="project-cards__line">
        <span>{line}</span>
        {action && (
          <button className="text-button" disabled={busy} onClick={action.onClick} type="button">
            {action.label}
          </button>
        )}
      </div>
      {history > 0 && reason !== "disabled" && (
        <div className="project-cards__line">
          <span>另有 {history} 场上线前的会没写卡片</span>
          {canWrite && (
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await apiClient.answerCardsBackfill("yes");
                  return "好的，下一轮扫描开始补写，所有项目上线前的会都会写成卡片";
                })
              }
              title="所有项目上线前的会都会补写"
              type="button"
            >
              补写历史卡片
            </button>
          )}
        </div>
      )}
      {cards.root && cards.written > 0 && (
        <div className="project-cards__hint">
          <span className="project-cards__hint-label">给 Claude Code 的一句话</span>
          <code>{CLAUDE_CODE_HINT}</code>
          <button className="text-button" onClick={() => void onCopy(CLAUDE_CODE_HINT, "已复制")} type="button">
            复制
          </button>
        </div>
      )}
      {message && (
        <p className="project-cards__message" role="status">
          {message}
        </p>
      )}
    </div>
  );
}
