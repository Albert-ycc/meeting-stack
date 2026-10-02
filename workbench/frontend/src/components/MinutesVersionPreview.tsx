import { useEffect, useState } from "react";

import { isAbortError, type ApiClient } from "../api";
import { versionKindLabel } from "../format";
import type { MinutesVersion } from "../types";
import "./MinutesVersionPreview.css";
import { SafeMarkdown } from "./SafeMarkdown";

interface MinutesVersionPreviewProps {
  apiClient: ApiClient;
  meetingId: string;
  /** 下拉里选中的、不是当前的那一版；没选（或选的就是当前版本）时是 null，什么都不画 */
  version: MinutesVersion | null;
}

/**
 * 回滚纪要之前看看所选历史版本写的什么。会议详情里历史版本没有正文，选到哪一版才去取哪一版；
 * 版本入库后不会改，取过的留在本页里，来回翻不重取。连着切几个版本时，被换下去的请求先中止，
 * 迟到的回答也盖不掉后选的那一版。
 */
export function MinutesVersionPreview({ apiClient, meetingId, version }: MinutesVersionPreviewProps) {
  // 取回来的正文，键里带会议号：同一个页面换了一场会也不会串
  const [bodies, setBodies] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<{ key: string; message: string } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const versionId = version?.id ?? null;
  const key = `${meetingId}/${versionId}`;
  // 列表里已经带着正文就直接用；取过的也直接用，不用等一帧「正在读取」
  const known = version ? (version.markdown ?? bodies[key]) : undefined;
  const needsFetch = versionId !== null && known === undefined;

  useEffect(() => {
    setFailed(null);
    if (!needsFetch || versionId === null) return;
    const controller = new AbortController();
    apiClient
      .minutesVersion(meetingId, versionId, { signal: controller.signal })
      .then((payload) => {
        if (!controller.signal.aborted) setBodies((current) => ({ ...current, [key]: payload.markdown }));
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setFailed({ key, message: error instanceof Error ? error.message : "请稍后重试" });
      });
    return () => controller.abort();
  }, [apiClient, key, meetingId, needsFetch, versionId, attempt]);

  if (!version) return null;
  const title = `v${version.version_no} · ${versionKindLabel(version.kind)}${version.published ? " · 已写回" : ""}`;

  return (
    <section aria-label="所选纪要版本的内容" className="minutes-version-preview">
      <header>
        <strong>{title}</strong>
        <small>只是预览，当前版本没动</small>
      </header>
      {known !== undefined ? (
        known.trim() ? (
          <div className="minutes-version-preview__body markdown-safe">
            <SafeMarkdown>{known}</SafeMarkdown>
          </div>
        ) : (
          <p className="minutes-version-preview__note">这一版没有内容</p>
        )
      ) : failed?.key === key ? (
        <div className="minutes-version-preview__failure" role="alert">
          <span>读取失败：{failed.message}</span>
          <button className="minutes-version-preview__retry" onClick={() => setAttempt((count) => count + 1)} type="button">
            重试
          </button>
        </div>
      ) : (
        <p className="minutes-version-preview__note" role="status">正在读取这一版的内容…</p>
      )}
    </section>
  );
}
