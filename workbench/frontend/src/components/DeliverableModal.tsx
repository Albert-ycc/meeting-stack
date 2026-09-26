import { useEffect, useMemo, useRef, useState } from "react";

import { type ApiClient } from "../api";
import type { DeliverableKind } from "../types";
import "./DeliverableModal.css";
import { useDialogFocus } from "./useDialog";

interface DeliverableModalProps {
  apiClient: ApiClient;
  taskId: string;
  taskTitle: string;
  onClose: () => void;
  onSaved: () => void;
}

const KIND_OPTIONS: Array<{ kind: DeliverableKind; label: string }> = [
  { kind: "figma", label: "Figma" },
  { kind: "lark", label: "飞书文档" },
  { kind: "file", label: "本地文件" },
  { kind: "link", label: "其他链接" },
];

const COMMON_FILE_EXT =
  /\.(pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|webp|svg|mp3|wav|mp4|mov|zip|txt|md|csv|key|numbers|pages|fig)$/i;

interface Recognition {
  kind: DeliverableKind;
  title: string;
}

function recognizeUrl(value: string): Recognition | null {
  const t = value.trim();
  if (!t) return null;
  if (/figma\.com/i.test(t)) return { kind: "figma", title: "Figma 原型" };
  if (/feishu|larksuite/i.test(t)) return { kind: "lark", title: "飞书文档" };
  if (/^[/~.]/.test(t) || COMMON_FILE_EXT.test(t)) {
    const cleaned = t.replace(/[/\\]+$/, "");
    const base = cleaned.slice(Math.max(cleaned.lastIndexOf("/"), cleaned.lastIndexOf("\\")) + 1);
    return { kind: "file", title: base || "本地文件" };
  }
  return { kind: "link", title: "其他链接" };
}

export function DeliverableModal({
  apiClient,
  taskId,
  taskTitle,
  onClose,
  onSaved,
}: DeliverableModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [kind, setKind] = useState<DeliverableKind | null>(null);
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [markDone, setMarkDone] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const savingRef = useRef(false);

  const recognition = useMemo(() => recognizeUrl(url), [url]);

  // 输入链接后自动识别类型并回填分段选择，用户仍可手动改。
  useEffect(() => {
    if (recognition) setKind(recognition.kind);
  }, [recognition]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing && !savingRef.current) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const canSave = url.trim().length > 0 && !saving;

  const save = async () => {
    const value = url.trim();
    if (!value || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      await apiClient.addDeliverable(taskId, {
        kind: kind ?? recognition?.kind ?? "link",
        url: value,
        title: recognition?.title ?? "其他链接",
        ...(note.trim() ? { note: note.trim() } : {}),
        mark_done: markDone,
      });
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
      savingRef.current = false;
    }
  };

  return (
    <div className="deliverable-overlay">
      <div
        aria-labelledby="deliverable-modal-title"
        aria-modal="true"
        className="deliverable-card"
        ref={dialogRef}
        role="dialog"
      >
        <div className="deliverable-card__head">
          <h2 id="deliverable-modal-title">登记交付物</h2>
          <p>任务：{taskTitle}</p>
        </div>

        <div className="deliverable-field">
          <span className="deliverable-field__label">类型</span>
          <div aria-label="交付物类型" className="deliverable-kind" role="radiogroup">
            {KIND_OPTIONS.map((option) => (
              <button
                aria-checked={kind === option.kind}
                className={
                  kind === option.kind
                    ? "deliverable-kind__option is-selected"
                    : "deliverable-kind__option"
                }
                key={option.kind}
                onClick={() => setKind(option.kind)}
                role="radio"
                type="button"
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="deliverable-field">
          <label className="deliverable-field__label" htmlFor="deliverable-url">
            链接
          </label>
          <input
            autoFocus
            className="deliverable-input"
            disabled={saving}
            id="deliverable-url"
            onChange={(event) => setUrl(event.target.value)}
            placeholder="粘贴交付物链接或本地文件路径"
            spellCheck={false}
            value={url}
          />
          {recognition && (
            <p className="deliverable-hint">已识别为「{recognition.title}」</p>
          )}
        </div>

        <div className="deliverable-field">
          <label className="deliverable-field__label" htmlFor="deliverable-note">
            实现方式（选填）
          </label>
          <textarea
            className="deliverable-note"
            disabled={saving}
            id="deliverable-note"
            onChange={(event) => setNote(event.target.value)}
            placeholder="补充实现方式或说明"
            rows={3}
            value={note}
          />
        </div>

        <label className="deliverable-check">
          <input
            checked={markDone}
            disabled={saving}
            onChange={(event) => setMarkDone(event.target.checked)}
            type="checkbox"
          />
          <span>同时把任务标记为已完成</span>
        </label>

        {error && <div className="deliverable-error" role="alert">{error}</div>}

        <div className="deliverable-footer">
          <button
            className="deliverable-cancel"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            取消
          </button>
          <button
            className="deliverable-save"
            disabled={!canSave}
            onClick={() => void save()}
            type="button"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
