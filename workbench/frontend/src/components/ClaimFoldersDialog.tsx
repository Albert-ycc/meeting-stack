import { useRef, useState } from "react";

import type { ApiClient } from "../api";
import { formatDate } from "../format";
import type { ClaimItem, ClaimItemResult, ClaimResult, UnclaimedFolder } from "../types";
import { FolderIcon } from "./FolderIcon";
import { NoticeBanner, useNotice } from "./Notice";
import { useBackdropDismiss, useDialogEscape, useDialogFocus } from "./useDialog";
import "./ClaimFoldersDialog.css";

interface ClaimFoldersDialogProps {
  apiClient: ApiClient;
  folders: UnclaimedFolder[];
  /** summary：这次在弹窗里处理掉的汇总一句话（含嵌套提示）；什么都没处理就是空串 */
  onClose: (summary: string) => void;
  /** 建成或挂上了文件夹之后刷新项目列表 */
  onChanged: () => void | Promise<void>;
}

interface Row extends UnclaimedFolder {
  selected: boolean;
  /** 「是另一个项目」：名字相近的那一行改成建成项目 */
  asNew: boolean;
  error?: string;
  /** 撞上已有项目：问「已有『X』，是不是它？」；exact 是完全同名，只能挂到它，不能仍然新建 */
  suggestion?: { id: string; name: string; exact: boolean } | null;
}

interface Totals {
  created: number;
  mounted: number;
  cards_written: number;
  needs_review: number;
}

const EMPTY_TOTALS: Totals = { created: 0, mounted: 0, cards_written: 0, needs_review: 0 };

/**
 * 「建了 2 个项目，挂上 1 个文件夹，补写了 6 张会议卡片；另有 5 场没认出的会提到了这些项目，已放进待你选」，
 * 是 0 的部分不说。
 */
export function claimSummary(totals: Totals): string {
  const parts = [
    totals.created > 0 ? `建了 ${totals.created} 个项目` : "",
    totals.mounted > 0 ? `挂上 ${totals.mounted} 个文件夹` : "",
    totals.cards_written > 0 ? `补写了 ${totals.cards_written} 张会议卡片` : "",
  ].filter(Boolean);
  const review =
    totals.needs_review > 0 ? `另有 ${totals.needs_review} 场没认出的会提到了这些项目，已放进待你选` : "";
  return [parts.join("，"), review].filter(Boolean).join("；");
}

function folderName(path: string) {
  return path.split("/").filter(Boolean).pop() ?? path;
}

/**
 * 挂上的文件夹和别的项目的文件夹互相嵌套时的提示（1a-41），照原文：
 * 「其中 云图AI/北辰/ 仍归项目『北辰』」（路径从挂上的文件夹名写起）；
 * 反过来它本身在别的项目的文件夹里时：「它在项目『X』的文件夹 /…/X/ 里面」。
 */
export function nestedHints(path: string, nested: ClaimItemResult["nested"]): string[] {
  const root = path.replace(/\/+$/, "");
  return (nested ?? []).map((entry) =>
    entry.path.startsWith(`${root}/`)
      ? `其中 ${folderName(root)}${entry.path.slice(root.length)}/ 仍归项目『${entry.project_name}』`
      : `它在项目『${entry.project_name}』的文件夹 ${entry.path}/ 里面`,
  );
}

/** 这一行勾上后要发的请求 */
function claimItem(row: Row): ClaimItem {
  if (!row.asNew && row.action === "mount" && row.project_id) {
    return { path: row.path, action: "mount", project_id: row.project_id };
  }
  // 建成项目：new、改成「是另一个项目」的相近行，以及你硬勾上的通用名
  return { path: row.path, action: "create" };
}

function actionText(row: Row): string {
  if (claimItem(row).action === "create") {
    if (row.kind === "generic" && !row.selected) return "看起来不是项目";
    return `建成项目『${row.name}』`;
  }
  if (row.kind === "exact_mounted") {
    const mounted = row.project_roots[0];
    return `『${row.project_name}』已挂了 ${mounted ? `…/${folderName(mounted)}` : "别的文件夹"}，再挂这个？`;
  }
  if (row.kind === "similar") return `挂到『${row.project_name}』？`;
  return `挂到『${row.project_name}』`;
}

/**
 * 项目总文件夹下还没挂到项目的一级文件夹：逐行给默认动作，勾上的整批发一次认领请求。
 * 撞上已有项目的那一行就地问「是不是它」，只把这一行再发一次；「不是项目」的行拿掉，提示里能撤销。
 */
export function ClaimFoldersDialog({ apiClient, folders, onClose, onChanged }: ClaimFoldersDialogProps) {
  const [rows, setRows] = useState<Row[]>(() =>
    // 通用名排最后（后端已经排好，这里再保一次）
    [...folders]
      .sort((a, b) => Number(a.kind === "generic") - Number(b.kind === "generic"))
      .map((folder) => ({ ...folder, selected: folder.checked, asNew: false })),
  );
  const [busy, setBusy] = useState(false);
  const [totals, setTotals] = useState<Totals>(EMPTY_TOTALS);
  const [hints, setHints] = useState<string[]>([]);
  const [declined, setDeclined] = useState<{ row: Row; index: number } | null>(null);
  const { notice, setNotice, dismissNotice } = useNotice();
  const cardRef = useRef<HTMLDivElement>(null);
  useDialogFocus(cardRef);

  const summaryText = (current: Totals, currentHints: string[]) =>
    [claimSummary(current), ...currentHints].filter(Boolean).join("。");
  const close = () => {
    if (!busy) onClose(summaryText(totals, hints));
  };
  useDialogEscape(cardRef, close);
  const backdrop = useBackdropDismiss(close, busy);

  const update = (path: string, change: Partial<Row>) =>
    setRows((current) => current.map((row) => (row.path === path ? { ...row, ...change } : row)));

  const submit = async (items: ClaimItem[]) => {
    if (items.length === 0 || busy) return;
    setBusy(true);
    setNotice("");
    setDeclined(null);
    let result: ClaimResult;
    try {
      result = await apiClient.claimFolders(items);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "没处理成，请稍后重试", "error");
      setBusy(false);
      return;
    }
    const byPath = new Map(result.items.map((item) => [item.path, item]));
    const nextRows = rows.flatMap((row) => {
      const item = byPath.get(row.path);
      if (!item) return [row];
      if (item.ok) return [];
      const suggested = item.suggestion ? item.suggestion.id ?? item.suggestion.project_id : undefined;
      return [
        {
          ...row,
          error: item.error ?? "没处理成，请稍后重试",
          suggestion:
            item.suggestion && suggested
              ? { id: suggested, name: item.suggestion.name, exact: item.suggestion.exact === true }
              : null,
        },
      ];
    });
    const nextTotals: Totals = {
      created: totals.created + result.created,
      mounted: totals.mounted + result.mounted,
      cards_written: totals.cards_written + result.cards_written,
      needs_review: totals.needs_review + result.needs_review,
    };
    const nextHints = [
      ...hints,
      ...result.items.filter((item) => item.ok).flatMap((item) => nestedHints(item.path, item.nested)),
    ];
    setRows(nextRows);
    setTotals(nextTotals);
    setHints(nextHints);
    if (result.created + result.mounted > 0) await onChanged();
    setBusy(false);
    // 都处理好了就关，汇总交给项目页的提示；有没处理成的就留在弹窗里就地说
    if (!nextRows.some((row) => row.error)) onClose(summaryText(nextTotals, nextHints));
  };

  const decline = async (row: Row) => {
    const index = rows.findIndex((entry) => entry.path === row.path);
    setRows((current) => current.filter((entry) => entry.path !== row.path));
    try {
      await apiClient.declineFolder(row.path);
      setDeclined({ row, index });
      setNotice(`『${row.name}』不算项目，以后不再列出`, "success", 10_000);
    } catch (error) {
      setRows((current) => [...current.slice(0, index), row, ...current.slice(index)]);
      setDeclined(null);
      setNotice(error instanceof Error ? error.message : "没记上，请稍后重试", "error");
    }
  };

  const undoDecline = async () => {
    if (!declined) return;
    const { row, index } = declined;
    try {
      await apiClient.undeclineFolder(row.path);
      setRows((current) => [...current.slice(0, index), row, ...current.slice(index)]);
      setDeclined(null);
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "没撤销成，请稍后重试", "error");
    }
  };

  const selectedRows = rows.filter((row) => row.selected);
  const createRows = rows.filter((row) => row.kind !== "generic" && claimItem(row).action === "create");

  return (
    <div className="claim-dialog__overlay" {...backdrop}>
      <div
        aria-labelledby="claim-dialog-title"
        aria-modal="true"
        className="claim-dialog__card"
        // 点到行这类不能聚焦的地方时焦点落在弹窗上，Tab 接着从弹窗里走
        tabIndex={-1}
        ref={cardRef}
        role="dialog"
      >
        <header className="claim-dialog__head">
          <h2 id="claim-dialog-title">这下面有 {rows.length} 个文件夹还没挂到项目</h2>
          <button aria-label="关闭" disabled={busy} onClick={close} type="button">
            ✕
          </button>
        </header>

        <NoticeBanner className="claim-dialog__notice" notice={notice} onDismiss={dismissNotice}>
          {declined && notice?.tone === "success" && (
            <button className="text-button action-banner__undo" onClick={() => void undoDecline()} type="button">
              撤销
            </button>
          )}
        </NoticeBanner>

        {(totals.created > 0 || totals.mounted > 0 || hints.length > 0) && (
          <p className="claim-dialog__summary" role="status">
            {summaryText(totals, hints)}
          </p>
        )}

        {createRows.length > 0 && (
          <div className="claim-dialog__tools">
            <button
              className="text-button text-button--accent"
              disabled={busy}
              onClick={() =>
                setRows((current) =>
                  current.map((row) =>
                    row.kind !== "generic" && claimItem(row).action === "create" ? { ...row, selected: true } : row,
                  ),
                )
              }
              type="button"
            >
              全选「建成项目」
            </button>
          </div>
        )}

        <ul className="claim-dialog__list">
          {rows.map((row) => (
            <li className={`claim-dialog__row ${row.kind === "generic" ? "is-generic" : ""}`} key={row.path}>
              <input
                aria-label={row.name}
                checked={row.selected}
                disabled={busy}
                onChange={(event) => update(row.path, { selected: event.target.checked })}
                type="checkbox"
              />
              <span className="claim-dialog__text">
                <span className="claim-dialog__name">
                  <FolderIcon className="claim-dialog__icon" />
                  <strong title={row.path}>{row.name}</strong>
                  <span className="claim-dialog__date">{formatDate(row.modified_at)}</span>
                </span>
                <span className="claim-dialog__action">
                  {actionText(row)}
                  {row.kind === "similar" && !row.asNew && (
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() => update(row.path, { asNew: true, selected: true })}
                      type="button"
                    >
                      是另一个项目
                    </button>
                  )}
                </span>
                {row.error &&
                  (row.suggestion ? (
                    <span className="claim-dialog__question" role="alert">
                      已有『{row.suggestion.name}』，是不是它？
                      <button
                        className="ghost-button"
                        disabled={busy}
                        onClick={() =>
                          void submit([{ path: row.path, action: "mount", project_id: row.suggestion!.id }])
                        }
                        type="button"
                      >
                        挂到它
                      </button>
                      {!row.suggestion.exact && (
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => void submit([{ path: row.path, action: "create", force: true }])}
                          type="button"
                        >
                          仍然新建
                        </button>
                      )}
                    </span>
                  ) : (
                    <span className="claim-dialog__error" role="alert">
                      {row.error}
                    </span>
                  ))}
              </span>
              <button className="text-button claim-dialog__decline" disabled={busy} onClick={() => void decline(row)} type="button">
                不是项目
              </button>
            </li>
          ))}
        </ul>

        <footer className="claim-dialog__footer">
          <button className="ghost-button" disabled={busy} onClick={close} type="button">
            以后再说
          </button>
          <button
            className="claim-dialog__submit"
            disabled={busy || selectedRows.length === 0}
            onClick={() => void submit(selectedRows.map(claimItem))}
            type="button"
          >
            {busy ? "处理中…" : `处理勾选的 ${selectedRows.length} 个`}
          </button>
        </footer>
      </div>
    </div>
  );
}
