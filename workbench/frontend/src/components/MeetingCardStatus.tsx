import { useState } from "react";

import type { ApiClient } from "../api";
import { cardStatusCopy } from "../cardCopy";
import type { MeetingCard } from "../types";
import { CopyFolderPathButton } from "./CopyFolderPathButton";
import "./MeetingCardStatus.css";

interface MeetingCardStatusProps {
  apiClient: ApiClient;
  card: MeetingCard;
  meetingId: string;
  projectId: string | null;
  projectName: string | null;
  /** 操作之后的新状态（重写、重新生成直接带回；恢复、开启之后重读一次） */
  onCardChange: (card: MeetingCard) => void;
  /** 去项目页挂文件夹、处理找不到的文件夹；没有项目或没传时不出按钮 */
  onOpenProject?: (projectId: string) => void;
}

const CATEGORY_LABEL: Record<MeetingCard["category"], string> = {
  ok: "已写入",
  waiting: "等待中",
  stopped: "已停止",
};

/**
 * 会议页标题区的「项目卡片」状态条：一句话说清卡片在哪、在等什么、为什么停了，最多一个按钮。
 * 手机上照样能点（卡片按钮都是改声档自己的状态，不改纪要）。
 */
export function MeetingCardStatus({
  apiClient,
  card,
  meetingId,
  projectId,
  projectName,
  onCardChange,
  onOpenProject,
}: MeetingCardStatusProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const copy = cardStatusCopy(card, projectName);

  const run = async (work: () => Promise<MeetingCard>) => {
    setBusy(true);
    setError("");
    try {
      onCardChange(await work());
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const act = () => {
    switch (copy.action) {
      case "rewrite":
      case "regenerate": {
        const action = copy.action;
        return run(() => apiClient.meetingCardAction(meetingId, action));
      }
      case "resume":
        if (!projectId) return;
        return run(async () => {
          await apiClient.resumeProjectCards(projectId);
          return apiClient.meetingCard(meetingId);
        });
      case "enable":
        return run(async () => {
          await apiClient.enableCards();
          return apiClient.meetingCard(meetingId);
        });
      case "open_project":
        if (projectId) onOpenProject?.(projectId);
        return;
      default:
        return;
    }
  };

  const showButton =
    copy.action && copy.action !== "copy" && (copy.action !== "open_project" || (projectId && onOpenProject));

  return (
    <div className={`card-status card-status--${card.category}`} role="status">
      <span className="card-status__label">项目卡片</span>
      <span className="card-status__tone">{CATEGORY_LABEL[card.category]}</span>
      <span className="card-status__text" title={card.path ?? undefined}>
        {copy.text}
      </span>
      {copy.action === "copy" && card.path && (
        <CopyFolderPathButton className="card-status__copy" label={copy.actionLabel} path={card.path} withLabel />
      )}
      {showButton && (
        <button className="text-button card-status__action" disabled={busy} onClick={() => void act()} type="button">
          {copy.actionLabel}
        </button>
      )}
      {error && (
        <span className="card-status__error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
