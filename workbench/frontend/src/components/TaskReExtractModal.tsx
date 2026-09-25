import { useCallback, useEffect, useRef, useState } from "react";

import { type ApiClient } from "../api";
import "./TaskReExtractModal.css";

interface TaskReExtractModalProps {
  apiClient: ApiClient;
  meetingId: string;
  meetingTitle: string;
  taskCount: number;
  onClose: () => void;
  onReExtracted: () => void;
}

export function TaskReExtractModal({
  apiClient,
  meetingId,
  meetingTitle,
  taskCount,
  onClose,
  onReExtracted,
}: TaskReExtractModalProps) {
  const [supplement, setSupplement] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, submitting]);

  const focusTextarea = useCallback(() => {
    textareaRef.current?.focus();
  }, []);

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const result = await apiClient.reExtractTasks(meetingId, supplement);
      if (result.status === "unavailable") {
        setError("任务抽取未启用（缺少模型配置）");
        setSubmitting(false);
        return;
      }
      onReExtracted();
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重新生成失败，请稍后重试");
      setSubmitting(false);
    }
  };

  return (
    <div
      className="re-extract-modal__overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <div
        aria-labelledby="re-extract-modal-title"
        aria-modal="true"
        className="re-extract-modal"
        role="dialog"
      >
        <header className="re-extract-modal__head">
          <div>
            <span className="eyebrow">REFINE TASKS</span>
            <h2 id="re-extract-modal-title">补充上下文，重新生成这批任务</h2>
            <em className="re-extract-modal__count">{taskCount} 条待确认任务</em>
          </div>
          <button
            aria-label="关闭"
            className="re-extract-modal__close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            ×
          </button>
        </header>

        <p className="re-extract-modal__subtitle">
          把要参考的材料或口头说明交给 AI，它会结合本场纪要重新整理任务清单。
        </p>

        <div className="re-extract-modal__body">
          <label className="re-extract-modal__field">
            <span className="re-extract-modal__label">补充说明</span>
            <textarea
              autoFocus
              disabled={submitting}
              onChange={(event) => setSupplement(event.target.value)}
              placeholder="项目经理的分工清单在下面的链接里，相关任务以他的分工为准，重复的合并掉。"
              ref={textareaRef}
              rows={5}
              value={supplement}
            />
          </label>

          <div className="re-extract-modal__materials">
            <span className="re-extract-modal__label">引用材料</span>
            <ul className="re-extract-modal__material-list">
              <li className="re-extract-modal__material">
                <span className="re-extract-modal__material-dot" aria-hidden="true" />
                <span className="re-extract-modal__material-name">
                  本场纪要 · {meetingTitle}
                </span>
                <em>已自动带入</em>
              </li>
              <li>
                <button
                  className="re-extract-modal__add"
                  disabled={submitting}
                  onClick={focusTextarea}
                  type="button"
                >
                  ＋ 添加链接、文档或一段话
                </button>
              </li>
            </ul>
          </div>
        </div>

        <footer className="re-extract-modal__foot">
          {error && (
            <p className="re-extract-modal__error" role="alert">
              {error}
            </p>
          )}
          <div className="re-extract-modal__foot-row">
            <p className="re-extract-modal__hint">
              重新生成后会回到待确认列表，本轮修订记录保留在每条任务的讨论轨迹里。
            </p>
            <div className="re-extract-modal__actions">
              <button
                className="re-extract-modal__cancel"
                disabled={submitting}
                onClick={onClose}
                type="button"
              >
                取消
              </button>
              <button
                className="re-extract-modal__submit"
                disabled={submitting}
                onClick={() => void submit()}
                type="button"
              >
                {submitting ? "正在重新生成…" : "重新生成任务清单"}
              </button>
            </div>
          </div>
        </footer>
      </div>
    </div>
  );
}
