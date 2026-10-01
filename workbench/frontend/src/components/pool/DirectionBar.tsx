import { useEffect, useRef, useState, type DragEvent } from "react";

import type { PoolProject } from "../../types";
import "./DirectionBar.css";

interface DirectionBarProps {
  /** 已排座次的在前（按名次），其后是未排座次的（按项目最近一场会） */
  projects: PoolProject[];
  /** 选中的项目（多选） */
  selected: string[];
  canWrite: boolean;
  onToggle: (projectId: string) => void;
  /** 拖动、排入或移出后整排保存：排了座次的项目从第 1 位起的先后 */
  onSeatsChange: (projectIds: string[]) => Promise<void> | void;
}

function sameOrder(left: string[], right: string[]) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function HandleIcon() {
  return (
    <svg aria-hidden="true" className="direction-chip__handle" fill="currentColor" height="10" viewBox="0 0 6 10" width="6">
      {[0, 4, 8].map((y) => (
        <g key={y}>
          <circle cx="1" cy={y + 1} r="1" />
          <circle cx="5" cy={y + 1} r="1" />
        </g>
      ))}
    </svg>
  );
}

/**
 * 需求池顶部的「我的方向」条（R03）：项目之间的先后代表这一阶段的个人工作方向。
 * 拖动排序、点选筛选；未排座次的项目收在「未排座次 +N」里，按最近会议排，可以排入座次或拖进来；
 * 已排座次的项目拖到「未排座次」上就移出座次（全都排了座次时，拖动中也会出现这个落点）。
 * 「未归项目」不出现在条上、不能拖动排序（R03 异常与边界）。
 */
export function DirectionBar({
  projects,
  selected,
  canWrite,
  onToggle,
  onSeatsChange,
}: DirectionBarProps) {
  const seated = projects.filter((project) => project.seat !== null);
  const unseated = projects.filter((project) => project.seat === null);
  const seatedIds = seated.map((project) => project.id);
  const [dragId, setDragId] = useState<string | null>(null);
  // 拖着的项目放下后在已排座次里的位置（不算它自己）
  const [insertAt, setInsertAt] = useState<number | null>(null);
  const [overUnseat, setOverUnseat] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const seatsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  const draggable = canWrite && !saving;
  const rest = dragId ? seatedIds.filter((id) => id !== dragId) : seatedIds;

  const save = async (next: string[]) => {
    if (sameOrder(next, seatedIds)) return;
    setSaving(true);
    try {
      await onSeatsChange(next);
    } finally {
      setSaving(false);
    }
  };

  const reset = () => {
    setDragId(null);
    setInsertAt(null);
    setOverUnseat(false);
  };

  const startDrag = (event: DragEvent, projectId: string) => {
    if (!draggable) return;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", projectId);
    setDragId(projectId);
  };

  // 指针在条上的哪儿就插到哪儿：数一数中线在指针左边的项目（不算拖着的那个）。
  // 落在项目上、项目之间的缝里、条尾的空白处都一样算，不会因为落在缝里就排到最后
  const overBar = (event: DragEvent) => {
    if (!dragId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setOverUnseat(false);
    const chips = Array.from(seatsRef.current?.querySelectorAll<HTMLElement>("[data-seat-id]") ?? []).filter(
      (chip) => chip.dataset.seatId !== dragId,
    );
    setInsertAt(
      chips.filter((chip) => {
        const rect = chip.getBoundingClientRect();
        return rect.left + rect.width / 2 < event.clientX;
      }).length,
    );
  };

  const dropOnBar = (event: DragEvent) => {
    if (!dragId) return;
    event.preventDefault();
    const target = insertAt ?? rest.length;
    const next = [...rest];
    next.splice(target, 0, dragId);
    reset();
    void save(next);
  };

  const overUnseatZone = (event: DragEvent) => {
    if (!dragId || !seatedIds.includes(dragId)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "move";
    setInsertAt(null);
    setOverUnseat(true);
  };

  const dropOnUnseat = (event: DragEvent) => {
    if (!dragId || !seatedIds.includes(dragId)) return;
    event.preventDefault();
    event.stopPropagation();
    const next = seatedIds.filter((id) => id !== dragId);
    reset();
    void save(next);
  };

  // 插入线画在完整列表（含拖着的那个的虚线框）里的位置
  const lineBefore = (() => {
    if (!dragId || insertAt === null || overUnseat) return null;
    const draggedIndex = seatedIds.indexOf(dragId);
    if (draggedIndex === -1) return insertAt;
    return insertAt < draggedIndex ? insertAt : insertAt + 1;
  })();

  const hint = overUnseat
    ? "松手移出座次"
    : dragId && insertAt !== null
      ? `松手放到第 ${insertAt + 1} 位`
      : "拖动排序，点选筛选";
  const draggingSeated = dragId !== null && seatedIds.includes(dragId);
  // 选中的项目里有收在「未排座次」里的：芯片上标出来，不然墙被筛了却看不出是谁筛的
  const pickedUnseated = unseated.filter((project) => selected.includes(project.id)).length;

  return (
    <div className={`direction-bar ${dragId ? "is-dragging" : ""}`}>
      <span className="direction-bar__label">我的方向</span>
      <div
        className="direction-bar__seats"
        data-testid="direction-seats"
        onDragOver={overBar}
        onDrop={dropOnBar}
        ref={seatsRef}
        role="group"
        aria-label="已排座次的项目"
      >
        {seated.map((project, index) => (
          <span className="direction-bar__slot" key={project.id}>
            {lineBefore === index && <span aria-hidden="true" className="direction-bar__line" />}
            <button
              aria-pressed={selected.includes(project.id)}
              className={`direction-chip ${selected.includes(project.id) ? "is-selected" : ""} ${
                dragId === project.id ? "is-placeholder" : ""
              }`}
              data-seat-id={project.id}
              draggable={draggable}
              onClick={() => onToggle(project.id)}
              onDragEnd={reset}
              onDragStart={(event) => startDrag(event, project.id)}
              title={canWrite ? "拖动调整座次，点一下只看这个项目" : undefined}
              type="button"
            >
              {canWrite && <HandleIcon />}
              <span className="direction-chip__seat">{project.seat}</span>
              <span className="direction-chip__name" title={project.name}>
                {project.name}
              </span>
              <span className="direction-chip__count">{project.count}</span>
            </button>
          </span>
        ))}
        {lineBefore === seated.length && <span aria-hidden="true" className="direction-bar__line" />}
      </div>

      {(unseated.length > 0 || draggingSeated) && (
        <div className="direction-bar__more" ref={moreRef}>
          <button
            aria-expanded={menuOpen}
            aria-haspopup="true"
            className={`direction-chip direction-chip--more ${overUnseat ? "is-drop-target" : ""} ${
              pickedUnseated ? "is-selected" : ""
            }`}
            disabled={unseated.length === 0 && !draggingSeated}
            onClick={() => unseated.length > 0 && setMenuOpen((open) => !open)}
            onDragLeave={() => setOverUnseat(false)}
            onDragOver={overUnseatZone}
            onDrop={dropOnUnseat}
            type="button"
          >
            未排座次
            {unseated.length > 0 && <span className="direction-chip__count">+{unseated.length}</span>}
            {pickedUnseated > 0 && <span className="direction-chip__picked">已选 {pickedUnseated}</span>}
            {unseated.length > 0 && (
              <span aria-hidden="true" className="direction-chip__caret">
                ▾
              </span>
            )}
          </button>
          {menuOpen && unseated.length > 0 && (
            <div className="direction-menu" role="dialog" aria-label="未排座次的项目">
              <p className="direction-menu__title">未排座次的项目，按最近会议排</p>
              <ul>
                {unseated.map((project) => (
                  <li
                    className={selected.includes(project.id) ? "is-selected" : ""}
                    draggable={draggable}
                    key={project.id}
                    onDragEnd={reset}
                    onDragStart={(event) => startDrag(event, project.id)}
                  >
                    {canWrite && <HandleIcon />}
                    <button
                      aria-pressed={selected.includes(project.id)}
                      className="direction-menu__name"
                      onClick={() => onToggle(project.id)}
                      type="button"
                    >
                      {project.name}
                      <span className="direction-chip__count">{project.count}</span>
                    </button>
                    {canWrite && (
                      <button
                        className="direction-menu__seat"
                        disabled={saving}
                        onClick={() => void save([...seatedIds, project.id])}
                        type="button"
                      >
                        排入座次
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <span aria-live="polite" className="direction-bar__hint">
        {saving ? "正在保存座次…" : hint}
      </span>
    </div>
  );
}
