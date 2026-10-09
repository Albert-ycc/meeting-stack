import { useId, useRef } from "react";

import type { Task } from "../../types";
import { useBackdropDismiss, useDialogEscape, useDialogFocus } from "../useDialog";
import "../ConfirmDialog.css";
import "./PoolDialogs.css";

interface CloseTasksDialogProps {
  /** 名下没做完的待办 */
  tasks: Task[];
  /** true：一起关掉（记为已取消）；false：待办留着 */
  onDecide: (closeOpenTasks: boolean) => void;
  /** ✕、Esc、点背景：这次不改状态 */
  onCancel: () => void;
}

/**
 * 标记完成、搁置时名下还有没做完的待办（D10）：列出来，问一起关掉还是留着。
 * 用户定的默认是一起关掉，所以「一起关掉」是主按钮，打开时焦点就在它上面。样子照全站的确认弹窗。
 */
export function CloseTasksDialog({ tasks, onDecide, onCancel }: CloseTasksDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useDialogFocus(dialogRef);
  useDialogEscape(dialogRef, onCancel);
  const backdrop = useBackdropDismiss(onCancel);

  return (
    <div className="confirm-modal__overlay" {...backdrop}>
      <div aria-labelledby={titleId} aria-modal="true" className="confirm-modal__card" ref={dialogRef} role="alertdialog">
        <header className="confirm-modal__head">
          <h2 id={titleId}>还有 {tasks.length} 条待办没做完</h2>
          <button aria-label="关闭" onClick={onCancel} type="button">
            ✕
          </button>
        </header>
        <div className="confirm-modal__body">
          <ul className="close-tasks__list">
            {tasks.map((task) => (
              <li key={task.id}>{task.title}</li>
            ))}
          </ul>
          <p className="close-tasks__note">关掉＝记为已取消，待办页不再挂着</p>
        </div>
        <footer className="confirm-modal__footer">
          <button onClick={() => onDecide(false)} type="button">
            待办留着
          </button>
          <button className="confirm-modal__primary" data-autofocus onClick={() => onDecide(true)} type="button">
            一起关掉
          </button>
        </footer>
      </div>
    </div>
  );
}
