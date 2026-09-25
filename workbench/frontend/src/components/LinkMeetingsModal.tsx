import { useEffect, useMemo, useState } from "react";

import type { ApiClient } from "../api";
import { formatDurationText, formatMonthDayClock } from "../format";
import type { MeetingSummary, RequirementDetail } from "../types";
import "./LinkMeetingsModal.css";

interface LinkMeetingsModalProps {
  apiClient: ApiClient;
  requirementId: string;
  projectId: string;
  /** 已关联的会议 id（预勾选） */
  selectedIds: string[];
  onCancel: () => void;
  onSaved: (requirement: RequirementDetail) => void;
}

/** 关联会议弹窗（A-04-2）：列需求所属项目下的会议，勾选后一次性整体替换。 */
export function LinkMeetingsModal({
  apiClient,
  requirementId,
  projectId,
  selectedIds,
  onCancel,
  onSaved,
}: LinkMeetingsModalProps) {
  const [meetings, setMeetings] = useState<MeetingSummary[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set(selectedIds));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void apiClient
      .meetings({ project_id: projectId, limit: 400 })
      .then((payload) => {
        if (active) setMeetings(payload.items);
      })
      .catch(() => {
        if (active) setLoadError(true);
      });
    return () => {
      active = false;
    };
  }, [apiClient, projectId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel, saving]);

  const rows = useMemo(() => {
    const keyword = search.trim();
    const list = meetings ?? [];
    return keyword ? list.filter((meeting) => meeting.title.includes(keyword)) : list;
  }, [meetings, search]);

  const toggle = (meetingId: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(meetingId)) next.delete(meetingId);
      else next.add(meetingId);
      return next;
    });

  const confirm = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await apiClient.setRequirementMeetings(requirementId, [...selected]);
      onSaved(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
    }
  };

  return (
    <div className="link-meetings-modal__overlay" onClick={() => { if (!saving) onCancel(); }}>
      <div
        aria-label="关联会议"
        aria-modal="true"
        className="link-meetings-modal__card"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="link-meetings-modal__head">
          <h2>关联会议</h2>
          <button aria-label="关闭" disabled={saving} onClick={onCancel} type="button">✕</button>
        </header>
        <input
          aria-label="搜索会议"
          className="link-meetings-modal__search"
          onChange={(event) => setSearch(event.target.value)}
          placeholder="搜索会议"
          value={search}
        />
        {meetings === null ? (
          <div className="link-meetings-modal__state">{loadError ? "会议读取失败" : "加载中…"}</div>
        ) : (
          <div className="link-meetings-modal__table">
            <div className="link-meetings-modal__row link-meetings-modal__row--head">
              <span />
              <span>日期</span>
              <span>会议</span>
              <span>时长</span>
            </div>
            <div className="link-meetings-modal__body">
              {rows.length === 0 ? (
                <div className="link-meetings-modal__state">没有匹配的会议</div>
              ) : (
                rows.map((meeting) => (
                  <label className="link-meetings-modal__row" key={meeting.id}>
                    <input checked={selected.has(meeting.id)} onChange={() => toggle(meeting.id)} type="checkbox" />
                    <span>{formatMonthDayClock(meeting.recording_date)}</span>
                    <span className="link-meetings-modal__title">{meeting.title}</span>
                    <span>{formatDurationText(meeting.duration_ms)}</span>
                  </label>
                ))
              )}
            </div>
          </div>
        )}
        {error && <p className="link-meetings-modal__error" role="alert">{error}</p>}
        <footer className="link-meetings-modal__footer">
          <span>已选 {selected.size} 场</span>
          <span className="link-meetings-modal__actions">
            <button disabled={saving} onClick={onCancel} type="button">取消</button>
            <button className="link-meetings-modal__confirm" disabled={saving} onClick={() => void confirm()} type="button">
              {saving ? "保存中…" : "确定"}
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
