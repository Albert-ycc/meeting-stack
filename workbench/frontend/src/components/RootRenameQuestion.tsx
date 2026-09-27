import { useEffect, useState } from "react";

import type { ApiClient } from "../api";
import type { MaterialRoot, MaterialRootRepoint, RenameCandidate, RenameCandidatesPayload } from "../types";
import { pollWhileChecking } from "./checkingPoll";

/**
 * 根目录换了位置后一起跟着改的：「，嵌在里面的『北辰』文件夹一起改了，3 个需求文件夹一起改了，已补写 2 张会议卡片」。
 * 旧后端只带 cards_written。项目页和关系图的文件夹面板共用。
 */
export function movedNote(result: Partial<MaterialRootRepoint> | undefined): string {
  if (!result) return "";
  const names = [...new Set((result.moved_roots ?? []).map((item) => item.project_name))];
  return [
    names.length ? `，嵌在里面的${names.map((name) => `『${name}』`).join("")}文件夹一起改了` : "",
    result.moved_folders ? `，${result.moved_folders} 个需求文件夹一起改了` : "",
    result.cards_written ? `，已补写 ${result.cards_written} 张会议卡片` : "",
  ].join("");
}

interface RootRenameQuestionProps {
  apiClient: ApiClient;
  projectId: string;
  /** 状态是「找不到」（盘在、文件夹没了）的根目录 */
  root: MaterialRoot;
  /** 能挂/换文件夹（能写这个项目，并且在电脑上） */
  canManage: boolean;
  /** ［是它］成功之后：说一起改了哪些，再重读项目 */
  onRepointed: (result: MaterialRootRepoint) => void | Promise<void>;
  /** 重新选…：打开取径器 */
  onReselect: () => void;
}

/**
 * 项目页根目录那一行「找不到」时：从后台缓存里找它是不是改了名。
 * 有默认候选时问「是不是改名成了『X』？（证据）」［是它］［不是］，后面一个文字链接「重新选…」；
 * 前两名一样强时逐个列出、各带［是它］；没有候选（或还在看）时保留原来的说法和［重新选…］。
 */
export function RootRenameQuestion({ apiClient, projectId, root, canManage, onRepointed, onReselect }: RootRenameQuestionProps) {
  const [payload, setPayload] = useState<RenameCandidatesPayload | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const enabled = canManage && typeof apiClient.renameCandidates === "function";

  useEffect(() => {
    if (!enabled) return;
    return pollWhileChecking(
      () => apiClient.renameCandidates(projectId, root.id),
      (value) => value.state === "checking",
      setPayload,
      // 找不到候选就按原来的样子问「重新选…」
      { onError: () => setPayload(null) },
    );
  }, [apiClient, enabled, projectId, root.id, reloadKey]);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const confirm = (candidate: RenameCandidate) =>
    run(async () => {
      const result = await apiClient.repointProjectMaterialRoot(projectId, root.id, candidate.path);
      await onRepointed(result);
    });

  const decline = (candidate: RenameCandidate) =>
    run(async () => {
      await apiClient.declineRenameCandidate(projectId, root.id, candidate.path);
      // 拒掉一个之后，剩下的候选可能换一个默认
      setReloadKey((key) => key + 1);
    });

  const candidates = payload?.state === "ready" ? payload.candidates : [];
  if (candidates.length === 0) {
    return (
      <>
        <span className="material-root-row__missing">找不到该目录</span>
        {canManage && (
          <span className="material-root-row__ops">
            <button onClick={onReselect} type="button">
              重新选…
            </button>
          </span>
        )}
      </>
    );
  }

  const chosen = candidates.find((candidate) => candidate.path === payload?.default_path) ?? null;
  const reselect = (
    <button className="material-root-row__link" disabled={busy} onClick={onReselect} type="button">
      重新选…
    </button>
  );

  return (
    <div aria-label="是不是改了名" className="material-root-row__rename" role="group">
      {chosen ? (
        <p>
          <span>
            找不到这个文件夹了。是不是改名成了『{chosen.name}』？（{chosen.evidence.text}）
          </span>
          <button className="ghost-button" disabled={busy} onClick={() => void confirm(chosen)} type="button">
            是它
          </button>
          <button className="text-button" disabled={busy} onClick={() => void decline(chosen)} type="button">
            不是
          </button>
          {reselect}
        </p>
      ) : (
        <>
          <p>
            <span>找不到这个文件夹了。是不是改名成了下面哪一个？</span>
            {reselect}
          </p>
          <ul>
            {candidates.map((candidate) => (
              <li key={candidate.path}>
                <span title={candidate.path}>
                  『{candidate.name}』（{candidate.evidence.text}）
                </span>
                <button className="ghost-button" disabled={busy} onClick={() => void confirm(candidate)} type="button">
                  是它
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {error && (
        <span className="material-root-row__rename-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
