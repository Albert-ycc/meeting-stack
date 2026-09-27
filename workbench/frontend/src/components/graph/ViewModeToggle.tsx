import type { ProjectViewMode } from "./graphPrefs";
import "./ViewModeToggle.css";

/** 项目详情页标题行右侧的［关系图｜清单］ */
export function ViewModeToggle({ mode, onChange }: { mode: ProjectViewMode; onChange: (mode: ProjectViewMode) => void }) {
  return (
    <span aria-label="项目视图" className="view-mode-toggle" role="group">
      <button aria-pressed={mode === "graph"} onClick={() => onChange("graph")} type="button">
        关系图
      </button>
      <button aria-pressed={mode === "list"} onClick={() => onChange("list")} type="button">
        清单
      </button>
    </span>
  );
}
