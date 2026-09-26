import { useState } from "react";

import type { ApiClient } from "../api";
import type { GlossarySuggestion, GlossaryTarget, Project } from "../types";
import { GlossaryTargetButton, targetName } from "./GlossaryTargetButton";
import "./MinutesCorrectionsBar.css";

interface MinutesCorrectionsBarProps {
  apiClient: ApiClient;
  /** 这次保存纪要捕获到的错字更正（含已直接记入的） */
  corrections: GlossarySuggestion[];
  projects: Project[];
  onChanged?: () => void;
  onClose: () => void;
}

type RowState =
  | { status: "pending"; short: boolean }
  | { status: "confirmed"; label: string; auto: boolean }
  | { status: "rejected" };

function initialState(item: GlossarySuggestion): RowState {
  if (item.status === "confirmed") {
    return {
      status: "confirmed",
      label: `已自动记入『${item.correct}』（${targetName(item.existing_term_project_name)}）`,
      auto: Boolean(item.auto_recorded),
    };
  }
  return { status: "pending", short: false };
}

function pairText(wrong: string, correct: string) {
  return `${wrong}→${correct}`;
}

/**
 * 保存纪要后编辑器下方的提示条：「2 处像是错字更正：树立协会→数理协会、岳总→月总［记入 云图AI ▾］［不是错字］」。
 * 什么都不点，建议就留在词典的待确认里，不当作驳回。
 */
export function MinutesCorrectionsBar({
  apiClient,
  corrections,
  projects,
  onChanged,
  onClose,
}: MinutesCorrectionsBarProps) {
  const [rows, setRows] = useState<Record<string, RowState>>(() =>
    Object.fromEntries(corrections.map((item) => [item.id, initialState(item)])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const setRow = (id: string, state: RowState) => setRows((current) => ({ ...current, [id]: state }));

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
      onChanged?.();
    }
  };

  const pending = corrections.filter((item) => rows[item.id]?.status === "pending");
  const handled = corrections.filter((item) => rows[item.id]?.status !== "pending");
  const lead = pending[0];
  const noProject = pending.length > 0 && pending.every((item) => !item.target_project_id);

  const confirmAll = (target: GlossaryTarget) =>
    void run(async () => {
      for (const item of pending) {
        const state = rows[item.id];
        const short = state?.status === "pending" && state.short;
        const result = await apiClient.confirmGlossarySuggestion(item.id, { target, short });
        const where = targetName(result.term?.project_name);
        setRow(item.id, {
          status: "confirmed",
          label: result.created
            ? `已记入 ${where}`
            : `已加到『${result.correct}』（${where}）`,
          auto: false,
        });
      }
    });

  const reject = (item: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.rejectGlossarySuggestion(item.id);
      setRow(item.id, { status: "rejected" });
    });

  const undo = (item: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.undoGlossarySuggestion(item.id);
      setRow(item.id, { status: "pending", short: false });
    });

  const restore = (item: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.restoreGlossarySuggestion(item.id);
      setRow(item.id, { status: "pending", short: false });
    });

  if (corrections.length === 0) return null;

  return (
    <section aria-label="错字更正" className="minutes-corrections" role="status">
      {pending.length > 0 && (
        <p className="minutes-corrections__lead">
          {pending.length} 处像是错字更正
          {noProject && "。这场会还没定项目，定了以后再记，也可以先记入公共"}
        </p>
      )}
      <ul className="minutes-corrections__list">
        {pending.map((item) => {
          const state = rows[item.id];
          const short = state?.status === "pending" && state.short;
          return (
            <li key={item.id}>
              <span className="minutes-corrections__pair">
                <s>{short ? item.alt_wrong : item.wrong}</s> → <strong>{short ? item.alt_correct : item.correct}</strong>
              </span>
              {item.alt_wrong && item.alt_correct && (
                <label className="minutes-corrections__short">
                  <input
                    checked={short}
                    disabled={busy}
                    onChange={(event) => setRow(item.id, { status: "pending", short: event.target.checked })}
                    type="checkbox"
                  />
                  只记 2 字
                </label>
              )}
              {item.existing_term_id && (
                <span className="minutes-corrections__note">
                  词典里已有『{item.correct}』（{targetName(item.existing_term_project_name)}），会加到那条
                </span>
              )}
              <button className="text-button text-button--muted" disabled={busy} onClick={() => reject(item)} type="button">
                不是错字
              </button>
            </li>
          );
        })}
        {handled.map((item) => {
          const state = rows[item.id];
          return (
            <li className="minutes-corrections__done" key={item.id}>
              <span className="minutes-corrections__pair">
                {pairText(item.confirmed_wrong || item.wrong, item.correct)}
              </span>
              {state?.status === "confirmed" && (
                <>
                  <span>{state.label}</span>
                  <button className="text-button" disabled={busy} onClick={() => undo(item)} type="button">
                    撤销
                  </button>
                </>
              )}
              {state?.status === "rejected" && (
                <>
                  <span>已标成不是错字</span>
                  <button className="text-button" disabled={busy} onClick={() => restore(item)} type="button">
                    恢复
                  </button>
                </>
              )}
            </li>
          );
        })}
      </ul>
      <div className="minutes-corrections__actions">
        {lead && (
          <GlossaryTargetButton
            defaultProjectId={lead.target_project_id}
            defaultProjectName={lead.target_project_name}
            disabled={busy}
            onConfirm={(target) => confirmAll(target)}
            projects={projects}
          />
        )}
        <button className="text-button text-button--muted" disabled={busy} onClick={onClose} type="button">
          {pending.length > 0 ? "稍后在词典里处理" : "收起"}
        </button>
      </div>
      {error && <p className="minutes-corrections__error">{error}</p>}
    </section>
  );
}
