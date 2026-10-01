import type { ApiClient } from "../../../api";
import type { Task } from "../../../types";
import { TaskRow, type RowPopover, type TaskActions } from "./TaskRow";
import { PANEL_VISIBLE, panelHeading } from "./workModel";

interface TaskPanelProps extends TaskActions {
  apiClient: ApiClient;
  requirementTitle: string;
  tasks: Task[];
  canWrite: boolean;
  busy: boolean;
  expanded: boolean;
  onToggleExpanded: () => void;
  popover: { id: string; kind: Exclude<RowPopover, null> } | null;
  onPopover: (next: { id: string; kind: Exclude<RowPopover, null> } | null) => void;
  onCreate: () => void;
}

/** 一条进行中需求的任务面板：已确认、进行中、已完成，未完成的在前；最多 6 行，其余折成「还有 N 项」 */
export function TaskPanel({
  apiClient,
  requirementTitle,
  tasks,
  canWrite,
  busy,
  expanded,
  onToggleExpanded,
  popover,
  onPopover,
  onCreate,
  ...actions
}: TaskPanelProps) {
  const heading = panelHeading(tasks);
  const folded = tasks.length > PANEL_VISIBLE;
  const shown = folded && !expanded ? tasks.slice(0, PANEL_VISIBLE) : tasks;

  return (
    <section aria-label={`「${requirementTitle}」的任务`} className="work-panel">
      <h3 className="work-panel__head">
        任务 {heading.total}
        {heading.note && <span>· {heading.note}</span>}
      </h3>
      {tasks.length === 0 ? (
        <p className="work-panel__empty">这条需求下还没有任务</p>
      ) : (
        <>
          <div aria-hidden="true" className="work-cols work-cols--panel">
            <span />
            <span>任务</span>
            <span>负责人</span>
            <span>截止</span>
            <span>来源</span>
            <span className="work-cols__ops">操作</span>
          </div>
          <ul className="work-panel__list">
            {shown.map((task) => (
              <TaskRow
                {...actions}
                apiClient={apiClient}
                busy={busy}
                canWrite={canWrite}
                key={task.id}
                onPopover={(kind) => onPopover(kind ? { id: task.id, kind } : null)}
                popover={popover?.id === task.id ? popover.kind : null}
                task={task}
                variant="panel"
              />
            ))}
          </ul>
          {folded && (
            <button className="work-panel__more" onClick={onToggleExpanded} type="button">
              {expanded ? "收起" : `还有 ${tasks.length - PANEL_VISIBLE} 项`}
            </button>
          )}
        </>
      )}
      {canWrite && (
        <footer className="work-panel__foot">
          <button className="work-panel__new" onClick={onCreate} type="button">
            ＋ 新建任务
          </button>
        </footer>
      )}
    </section>
  );
}
