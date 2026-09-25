import { useCallback, useEffect, useState } from "react";

import type { ApiClient } from "../api";
import { formatMonthDay } from "../format";
import type { Project, RequirementCounts, RequirementPriority, RequirementSummary } from "../types";
import { Pagination } from "./Pagination";
import { PriorityBadge, REQUIREMENT_PRIORITIES, RequirementStatusBadge } from "./RequirementBadges";
import { RequirementModal } from "./RequirementModal";
import { useToast } from "./Toast";
import "./RequirementsPage.css";

interface RequirementsPageProps {
  apiClient: ApiClient;
  canWrite: boolean;
  canPickFolders: boolean;
  projects: Project[];
  onOpenRequirement: (requirementId: string) => void;
  onOpenProject: (projectId: string) => void;
  onProjectsChanged?: () => void | Promise<void>;
}

type TabKey = "active" | "done" | "shelved" | "all";
type LoadState = "loading" | "ready" | "empty" | "error";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "active", label: "进行中" },
  { key: "done", label: "已完成" },
  { key: "shelved", label: "已搁置" },
  { key: "all", label: "全部" },
];

const PAGE_SIZE = 10;

interface Filters {
  project_id: string;
  priority: string;
  q: string;
}

const EMPTY_FILTERS: Filters = { project_id: "", priority: "", q: "" };

export function RequirementsPage({
  apiClient,
  canWrite,
  canPickFolders,
  projects,
  onOpenRequirement,
  onOpenProject,
  onProjectsChanged,
}: RequirementsPageProps) {
  const { toastNode, showToast } = useToast();
  const [activeTab, setActiveTab] = useState<TabKey>("active");
  const [draftFilters, setDraftFilters] = useState<Filters>(EMPTY_FILTERS);
  const [appliedFilters, setAppliedFilters] = useState<Filters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<RequirementSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<RequirementCounts>({ active: 0, done: 0, shelved: 0, all: 0 });
  const [state, setState] = useState<LoadState>("loading");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<RequirementSummary | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    try {
      const payload = await apiClient.requirements({
        project_id: appliedFilters.project_id || undefined,
        priority: appliedFilters.priority || undefined,
        q: appliedFilters.q || undefined,
        status: activeTab === "all" ? undefined : activeTab,
        limit: PAGE_SIZE,
        offset,
      });
      // 当前页空了（本页最后一条改了状态被筛掉）就退到最后一个有内容的页。
      if (payload.items.length === 0 && offset > 0 && payload.total > 0) {
        setOffset(Math.max(0, Math.ceil(payload.total / PAGE_SIZE) - 1) * PAGE_SIZE);
        return;
      }
      setItems(payload.items);
      setTotal(payload.total);
      setCounts(payload.counts);
      setState(payload.items.length ? "ready" : "empty");
    } catch {
      setState("error");
    }
  }, [apiClient, activeTab, appliedFilters, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const switchTab = (key: TabKey) => {
    if (key === activeTab) return;
    setActiveTab(key);
    setOffset(0);
  };

  const applyFilters = () => {
    setAppliedFilters(draftFilters);
    setOffset(0);
  };

  const resetFilters = () => {
    setDraftFilters(EMPTY_FILTERS);
    setAppliedFilters(EMPTY_FILTERS);
    setOffset(0);
  };

  const afterSaved = async (message: string) => {
    setCreating(false);
    setEditing(null);
    showToast(message);
    await load();
    await onProjectsChanged?.();
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <section className="page-content requirements-page">
      {toastNode}
      <header className="page-heading">
        <div>
          <span className="eyebrow">REQUIREMENTS / 需求清单</span>
          <h1>需求</h1>
          <p>手上在做的需求按优先级排列，点开可以看到关联的会议和材料。</p>
        </div>
        {canWrite && (
          <button className="requirements-create" onClick={() => setCreating(true)} type="button">
            ＋ 新建需求
          </button>
        )}
      </header>

      <form
        className="requirements-query"
        onSubmit={(event) => {
          event.preventDefault();
          applyFilters();
        }}
      >
        <label>
          <span>所属项目</span>
          <select
            onChange={(event) => setDraftFilters((current) => ({ ...current, project_id: event.target.value }))}
            value={draftFilters.project_id}
          >
            <option value="">全部项目</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>优先级</span>
          <select
            onChange={(event) => setDraftFilters((current) => ({ ...current, priority: event.target.value }))}
            value={draftFilters.priority}
          >
            <option value="">全部</option>
            {REQUIREMENT_PRIORITIES.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          <span>需求名称</span>
          <input
            onChange={(event) => setDraftFilters((current) => ({ ...current, q: event.target.value }))}
            placeholder="输入需求名称"
            value={draftFilters.q}
          />
        </label>
        <div className="requirements-query__actions">
          <button className="requirements-query__submit" type="submit">查询</button>
          <button className="requirements-query__reset" onClick={resetFilters} type="button">重置</button>
        </div>
      </form>

      <div className="requirements-table">
        <div className="requirements-tabs-bar">
          <div className="requirements-tabs" role="tablist">
            {TABS.map((tab) => (
              <button
                aria-selected={activeTab === tab.key}
                key={tab.key}
                onClick={() => switchTab(tab.key)}
                role="tab"
                type="button"
              >
                {tab.label}
                <span>{counts[tab.key]}</span>
              </button>
            ))}
          </div>
        </div>

        {state === "loading" && <div className="requirements-state">正在读取需求…</div>}
        {state === "error" && <div className="requirements-state requirements-state--error" role="alert">需求读取失败</div>}
        {(state === "ready" || state === "empty") && (
          <>
          <div className="requirements-row requirements-row--head">
            <span>需求名称</span>
            <span>所属项目</span>
            <span>优先级</span>
            <span>状态</span>
            <span>未完成任务</span>
            <span>关联会议</span>
            <span>最近会议</span>
            <span>材料文件夹</span>
            <span>操作</span>
          </div>
          {state === "empty" ? (
            <div className="requirements-empty">
              <span aria-hidden="true" className="requirements-empty__icon">
                <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 15 15">
                  <path d="M3.2 13V2" />
                  <path d="M3.2 2.6c1.3-.8 2.5-.8 3.8 0s2.5.8 3.8 0v5.6c-1.3.8-2.5.8-3.8 0s-2.5-.8-3.8 0" />
                </svg>
              </span>
              <p>还没有需求</p>
              {canWrite && (
                <button onClick={() => setCreating(true)} type="button">＋ 新建需求</button>
              )}
            </div>
          ) : (
            items.map((item) => (
              <div className="requirements-row" key={item.id}>
                <span className="requirements-row__title">{item.title}</span>
                <button
                  className="requirements-row__project"
                  onClick={() => onOpenProject(item.project_id)}
                  type="button"
                >
                  <i style={{ background: item.project_color }} />
                  {item.project_name}
                </button>
                <span><PriorityBadge priority={item.priority} /></span>
                <span><RequirementStatusBadge status={item.status} /></span>
                <span>{item.open_task_count}</span>
                <span>{item.meeting_count}</span>
                <span>{formatMonthDay(item.latest_meeting_date)}</span>
                <span>{item.folder_count}</span>
                <span className="requirements-row__actions">
                  <button onClick={() => onOpenRequirement(item.id)} type="button">查看</button>
                  {canWrite && (
                    <button onClick={() => setEditing(item)} type="button">编辑</button>
                  )}
                </span>
              </div>
            ))
          )}
          </>
        )}
      </div>

      {state === "ready" && total > 0 && (
        <div className="requirements-pagination">
          <span>共 {total} 条</span>
          <Pagination
            onChange={(page) => setOffset(page * PAGE_SIZE)}
            page={Math.floor(offset / PAGE_SIZE)}
            pageCount={totalPages}
          />
        </div>
      )}

      {creating && (
        <RequirementModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          mode="create"
          onClose={() => setCreating(false)}
          onOpenProject={onOpenProject}
          onSaved={() => void afterSaved("需求已创建")}
          projects={projects}
        />
      )}
      {editing && (
        <RequirementModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          mode="edit"
          onClose={() => setEditing(null)}
          onOpenProject={onOpenProject}
          onSaved={() => void afterSaved("已保存")}
          projects={projects}
          requirement={editing}
        />
      )}
    </section>
  );
}
