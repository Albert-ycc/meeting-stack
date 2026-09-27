import { useEffect, useState, type ReactNode } from "react";

import type { ApiClient } from "../api";
import type { NoticeTone } from "./Notice";
import type { ProjectParentStatus } from "../types";
import { pollWhileChecking } from "./checkingPoll";
import { ClaimFoldersDialog } from "./ClaimFoldersDialog";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import "./ProjectParentRow.css";

/** 设项目总文件夹时取径器的标题和说明（项目页和新建项目弹窗共用） */
export const PROJECT_PARENT_PICKER = {
  title: "选项目总文件夹",
  description: "选放项目文件夹的那一层，新项目的文件夹会建在这里面。",
};

interface ProjectParentRowProps {
  apiClient: ApiClient;
  /** 只在电脑上能改（手机上只读） */
  canEdit: boolean;
  /** 认领后刷新项目列表 */
  onProjectsChanged: () => void | Promise<void>;
  /** 结果和失败原因写在项目页的提示条上 */
  onNotice: (message: string, tone?: NoticeTone, durationMs?: number) => void;
}

function isChecking(status: ProjectParentStatus) {
  return status.state === "checking" || status.unclaimed.state === "checking";
}

/** 总文件夹本身读不了、不能用时的一句话 */
function stateNote(status: ProjectParentStatus): string {
  switch (status.state) {
    case "volume_offline":
      return "资料盘未连接，插上后再列下面的文件夹";
    case "missing":
      return "找不到这个文件夹了";
    case "unreadable":
      return "读不了这个文件夹";
    case "invalid":
    case "conflict":
      return status.reason ?? "这个位置现在用不了";
    default:
      return "";
  }
}

/**
 * 项目页标题下的一行：项目总文件夹在哪、没设时给推荐、下面还有几个文件夹没挂到项目。
 * 第一次设好（或改了位置）后，下面有还没挂的文件夹就弹出认领框。
 */
export function ProjectParentRow({ apiClient, canEdit, onProjectsChanged, onNotice }: ProjectParentRowProps) {
  const [status, setStatus] = useState<ProjectParentStatus | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pickerError, setPickerError] = useState("");
  const [claimOpen, setClaimOpen] = useState(false);
  // 刚设好 / 改了位置：等下面的文件夹列出来，有没挂的就弹认领框
  const [claimWhenReady, setClaimWhenReady] = useState(false);
  const available = typeof apiClient.projectParent === "function";
  const editable = canEdit && typeof apiClient.setProjectParent === "function";

  useEffect(() => {
    if (!available) return;
    return pollWhileChecking(() => apiClient.projectParent(), isChecking, setStatus, {
      // 只是一行说明，读不到就不出现
      onError: () => undefined,
    });
  }, [apiClient, available, reloadKey]);

  useEffect(() => {
    if (!claimWhenReady || !status || isChecking(status)) return;
    setClaimWhenReady(false);
    if (status.unclaimed.state === "ready" && status.unclaimed.folders.length > 0) setClaimOpen(true);
  }, [claimWhenReady, status]);

  const save = async (path: string, fromPicker: boolean) => {
    setBusy(true);
    setPickerError("");
    try {
      const result = await apiClient.setProjectParent(path);
      setStatus(result);
      setPickerOpen(false);
      setClaimWhenReady(true);
      // 刚设好时下面的文件夹多半还在看：重新开始轮询
      if (isChecking(result)) setReloadKey((key) => key + 1);
    } catch (error) {
      const message = error instanceof Error ? error.message : "没设成，请稍后重试";
      // 取径器还开着就在取径器里说（D27）
      if (fromPicker) setPickerError(message);
      else onNotice(message, "error");
    } finally {
      setBusy(false);
    }
  };

  const openPicker = () => {
    setPickerError("");
    setPickerOpen(true);
  };

  if (!status) return null;

  let line: ReactNode;
  if (status.path === null) {
    // 没设时手机上不说：反正改不了
    if (!editable) return null;
    const suggested = status.suggested;
    line = suggested ? (
      <>
        <span>
          还没设项目总文件夹。你挂过的 {suggested.total} 个项目文件夹里有 {suggested.count} 个在{" "}
          <span className="project-parent__path">{suggested.path}</span> 下面
        </span>
        <button className="text-button" disabled={busy} onClick={() => void save(suggested.path, false)} type="button">
          用这个
        </button>
        <button className="text-button" disabled={busy} onClick={openPicker} type="button">
          另选…
        </button>
      </>
    ) : (
      <button className="text-button" disabled={busy} onClick={openPicker} type="button">
        选项目总文件夹…
      </button>
    );
  } else {
    const note = stateNote(status);
    const unclaimed = status.unclaimed;
    line = (
      <>
        <span>
          项目总文件夹：<span className="project-parent__path">{status.path}</span>
        </span>
        {note && <span className="project-parent__note">{note}</span>}
        {editable && (
          <button className="text-button" disabled={busy} onClick={openPicker} type="button">
            改
          </button>
        )}
        {editable && unclaimed.state === "ready" && unclaimed.folders.length > 0 && (
          <span className="project-parent__unclaimed">
            还有 {unclaimed.folders.length} 个文件夹没挂到项目
            <button className="text-button" onClick={() => setClaimOpen(true)} type="button">
              看看
            </button>
          </span>
        )}
      </>
    );
  }

  return (
    <div className="project-parent">
      {line}

      {pickerOpen && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={busy}
          description={PROJECT_PARENT_PICKER.description}
          error={pickerError}
          onClose={() => setPickerOpen(false)}
          onConfirm={(path) => void save(path, true)}
          title={PROJECT_PARENT_PICKER.title}
        />
      )}

      {claimOpen && status.unclaimed.folders.length > 0 && (
        <ClaimFoldersDialog
          apiClient={apiClient}
          folders={status.unclaimed.folders}
          onChanged={onProjectsChanged}
          onClose={(summary) => {
            setClaimOpen(false);
            if (summary) onNotice(summary, "success", 12_000);
            // 认领、「不是项目」之后，下面还剩几个要重读
            setReloadKey((key) => key + 1);
          }}
        />
      )}
    </div>
  );
}
