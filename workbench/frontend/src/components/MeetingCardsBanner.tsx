import { useEffect, useState } from "react";

import type { ApiClient } from "../api";
import type { CardsBanner } from "../types";
import { FolderIcon } from "./FolderIcon";
import { NoticeBanner, useNotice } from "./Notice";
import "./FolderSuggestionBanner.css";
import "./MeetingCardsBanner.css";

interface MeetingCardsBannerProps {
  apiClient: ApiClient;
}

/** 「/Volumes/资料盘/云图AI/声档会议记录」→「云图AI/声档会议记录/」 */
function shortDir(path: string) {
  return `${path.split("/").filter(Boolean).slice(-2).join("/")}/`;
}

/**
 * 工作台上会议卡片的一次性横幅，同一时间只出一条：
 * 先报「在 X 写入了第一张会议卡片」，再问要不要把上线前的会补写成卡片。
 */
export function MeetingCardsBanner({ apiClient }: MeetingCardsBannerProps) {
  const [banner, setBanner] = useState<CardsBanner | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const { notice, setNotice, dismissNotice } = useNotice();
  const [retireOffer, setRetireOffer] = useState(false);

  useEffect(() => {
    if (typeof apiClient.cardsBanner !== "function") return;
    let alive = true;
    apiClient
      .cardsBanner()
      .then((payload) => {
        if (alive) setBanner(payload);
      })
      .catch(() => {
        // 只是个提醒，读不到就不出现
      });
    return () => {
      alive = false;
    };
  }, [apiClient]);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    try {
      await work();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败，请稍后重试", "error");
    } finally {
      setBusy(false);
    }
  };

  const notices = banner?.notices ?? [];
  const backfill = banner?.backfill ?? null;

  const dismissNotices = () =>
    run(async () => {
      const ids = notices.map((item) => item.project_id);
      setBanner((current) => (current ? { ...current, notices: [] } : current));
      await Promise.all(ids.map((id) => apiClient.dismissCardsNotice(id)));
    });

  const answer = (value: "yes" | "no" | "later") =>
    run(async () => {
      await apiClient.answerCardsBackfill(value);
      setBanner((current) => (current ? { ...current, backfill: null } : current));
      setOpen(false);
      if (value === "yes") {
        // 带［全部撤下］，多停一会儿
        setNotice(`好的，接下来的扫描会把 ${backfill?.meetings ?? 0} 场会写成卡片`, "success", 15_000);
        setRetireOffer(true);
      } else if (value === "no") {
        setNotice("好的，先不补写；以后想补写，在项目页的材料区点一下就行");
      }
    });

  const retireAll = () =>
    run(async () => {
      const result = await apiClient.retireAllCards();
      setRetireOffer(false);
      setNotice(
        result.kept.length
          ? `已撤下 ${result.retired} 张会议卡片，${result.kept.length} 张你改过的留在原处；会议卡片已关闭`
          : `已撤下 ${result.retired} 张会议卡片，会议卡片已关闭`,
      );
    });

  if (notices.length > 0) {
    const first = notices[0];
    return (
      <div className="folder-suggestion" role="status">
        <FolderIcon className="folder-suggestion__icon" />
        <span>
          {notices.length === 1
            ? `已在 ${shortDir(first.path)} 写入第一张会议卡片，给你和 Claude Code 看`
            : `已在 ${notices.length} 个项目文件夹里建好「声档会议记录/」并写入会议卡片：${notices
                .map((item) => item.project_name)
                .join("、")}`}
        </span>
        {notices.length === 1 && (
          <button
            className="ghost-button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await apiClient.revealCards({ project_id: first.project_id });
              })
            }
            type="button"
          >
            打开文件夹
          </button>
        )}
        {notices.length === 1 && (
          <button
            className="text-button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await apiClient.pauseProjectCards(first.project_id);
                setBanner((current) => (current ? { ...current, notices: current.notices.slice(1) } : current));
                setNotice(`「${first.project_name}」不再写会议卡片，已撤下 ${result.retired} 张；想恢复在项目页点一下`);
              })
            }
            type="button"
          >
            这个项目不要写
          </button>
        )}
        <button className="text-button" disabled={busy} onClick={() => void dismissNotices()} type="button">
          知道了
        </button>
        {notice && <p className="cards-banner__notice">{notice.message}</p>}
      </div>
    );
  }

  if (backfill) {
    return (
      <div className="folder-suggestion cards-banner" role="status">
        <FolderIcon className="folder-suggestion__icon" />
        <span>
          可以把已归项目的 {backfill.meetings} 场会写成卡片，放进 {backfill.projects} 个项目文件夹的「声档会议记录/」里，方便
          Claude Code 读取。
          {backfill.ai_attributed > 0 && `其中 ${backfill.ai_attributed} 场的项目是 AI 自动归的，卡片里会标明。`}
          {backfill.no_folder_projects > 0 &&
            `另有 ${backfill.no_folder_projects} 个项目还没挂文件夹，那些会先存着。`}
        </span>
        <button className="ghost-button" disabled={busy} onClick={() => setOpen((value) => !value)} type="button">
          {open ? "收起" : "看看写到哪"}
        </button>
        <button className="ghost-button" disabled={busy} onClick={() => void answer("yes")} type="button">
          写入
        </button>
        <button className="text-button" disabled={busy} onClick={() => void answer("no")} type="button">
          先不要
        </button>
        <button className="text-button" disabled={busy} onClick={() => void answer("later")} type="button">
          稍后
        </button>
        {open && (
          <ul className="cards-banner__list">
            {backfill.top.slice(0, 5).map((item) => (
              <li key={item.project_id}>
                <strong>{item.project_name}</strong>
                <span>{item.count} 场</span>
                <span className="cards-banner__path">
                  {item.paused ? "已暂停写卡片" : item.path ? `${item.path}/` : "还没挂文件夹，先存着"}
                </span>
              </li>
            ))}
            {backfill.top.length > 5 && <li className="cards-banner__more">还有 {backfill.top.length - 5} 个项目</li>}
          </ul>
        )}
        {notice && <p className="cards-banner__notice">{notice.message}</p>}
      </div>
    );
  }

  return (
    <NoticeBanner notice={notice} onDismiss={dismissNotice}>
      {retireOffer && notice?.tone === "success" && (
        <button className="text-button action-banner__undo" disabled={busy} onClick={() => void retireAll()} type="button">
          全部撤下
        </button>
      )}
    </NoticeBanner>
  );
}
