import { useMemo, useState, type CSSProperties, type FormEvent } from "react";

import type { ApiClient } from "../api";
import type { MeetingSummary, PoolProject, Project, Tag } from "../types";
import { BlurText } from "./motion/BlurText";
import { DirectionBar } from "./pool/DirectionBar";
import { ProjectPanel, ProjectRow } from "./projects/ProjectPanel";
import { ProjectFormModal } from "./ProjectFormModal";
import { ProjectParentRow } from "./ProjectParentRow";
import "./ProjectsPage.css";
import { NoticeBanner, useNotice } from "./Notice";
import { usePersistentState } from "../viewState";

interface ProjectsPageProps {
  apiClient: ApiClient;
  canEdit: boolean;
  meetings: MeetingSummary[];
  onCreateTag: (name: string, color: string) => Promise<void>;
  onOpenProject: (projectId: string) => void;
  /** 项目新建/编辑弹窗自己调 apiClient 保存，保存后叫父级重拉一次项目列表 */
  onProjectsChanged: () => void | Promise<void>;
  projects: Project[];
  tags: Tag[];
}

type RootFilter = "all" | "attached" | "unattached";

/** 未排座次的项目默认只列前几行，其余折起来，点开就地展开 */
const UNSEATED_FOLDED_ROWS = 8;

function meetingCount(project: Project, meetings: MeetingSummary[]): number {
  return project.meeting_count ?? meetings.filter((meeting) => meeting.project_id === project.id).length;
}

function splitBySeat(projects: Project[]) {
  const seated = projects
    .filter((project) => project.seat != null)
    .sort((a, b) => (a.seat as number) - (b.seat as number));
  const unseated = projects
    .filter((project) => project.seat == null)
    .sort((a, b) => {
      const left = a.latest_meeting_date ? Date.parse(a.latest_meeting_date) : -Infinity;
      const right = b.latest_meeting_date ? Date.parse(b.latest_meeting_date) : -Infinity;
      return right - left || a.name.localeCompare(b.name, "zh-CN");
    });
  return { seated, unseated };
}

export function ProjectsPage({
  apiClient,
  canEdit,
  meetings,
  onCreateTag,
  onOpenProject,
  onProjectsChanged,
  projects,
  tags,
}: ProjectsPageProps) {
  const [nameDraft, setNameDraft] = usePersistentState("projects.nameDraft", "");
  const [rootDraft, setRootDraft] = usePersistentState<RootFilter>("projects.rootDraft", "all");
  const [appliedName, setAppliedName] = usePersistentState("projects.appliedName", "");
  const [appliedRoot, setAppliedRoot] = usePersistentState<RootFilter>("projects.appliedRoot", "all");

  const [tagName, setTagName] = useState("");
  const [tagColor, setTagColor] = useState("#f0783b");
  const [tagBusy, setTagBusy] = useState(false);
  const { notice, setNotice, dismissNotice } = useNotice();

  const [formModal, setFormModal] = useState<{ mode: "create" | "edit"; project: Project | null } | null>(null);

  // 「我的方向」条点选的项目；记在页面状态里，离开再回来还在
  const [pickedIds, setPickedIds] = usePersistentState<string[]>("projects.directionIds", [], { valid: Array.isArray });
  const [unseatedOpen, setUnseatedOpen] = useState(false);

  // 已排座次的按座次，未排的按最近一场会由近到远（没有会议的排最后）：方向条和下面的两段用同一个顺序
  const { seated, unseated } = useMemo(() => splitBySeat(projects), [projects]);
  const directionProjects: PoolProject[] = useMemo(
    () =>
      [...seated, ...unseated].map((project) => ({
        id: project.id,
        name: project.name,
        color: project.color,
        seat: project.seat ?? null,
        latest_meeting_date: project.latest_meeting_date ?? null,
        count: project.open_task_count ?? 0,
      })),
    [seated, unseated],
  );

  const matches = useMemo(() => {
    const keyword = appliedName.trim().toLowerCase();
    const picked = pickedIds.filter((id) => projects.some((project) => project.id === id));
    return (project: Project) => {
      if (keyword && !project.name.toLowerCase().includes(keyword)) return false;
      const attached = (project.material_roots ?? []).length > 0;
      if (appliedRoot === "attached" && !attached) return false;
      if (appliedRoot === "unattached" && attached) return false;
      return picked.length === 0 || picked.includes(project.id);
    };
  }, [projects, appliedName, appliedRoot, pickedIds]);
  const seatedShown = seated.filter(matches);
  const unseatedShown = unseated.filter(matches);
  const unseatedRows = unseatedOpen ? unseatedShown : unseatedShown.slice(0, UNSEATED_FOLDED_ROWS);

  const toggleProject = (projectId: string) =>
    setPickedIds((current) => (current.includes(projectId) ? current.filter((id) => id !== projectId) : [...current, projectId]));

  const saveSeats = async (projectIds: string[]) => {
    try {
      await apiClient.saveProjectSeats(projectIds);
      setNotice("座次已保存");
    } catch {
      // 多半是项目在别处改过（删掉、合并、另一个窗口排过座次）：下面重新取一遍，用新的一排再拖
      setNotice("座次没保存：项目有变化，已刷新，请再拖一次", "warning");
    }
    await onProjectsChanged();
  };

  const runQuery = (event: FormEvent) => {
    event.preventDefault();
    setAppliedName(nameDraft);
    setAppliedRoot(rootDraft);
  };

  const resetQuery = () => {
    setNameDraft("");
    setRootDraft("all");
    setAppliedName("");
    setAppliedRoot("all");
  };

  const createTag = async (event: FormEvent) => {
    event.preventDefault();
    if (!tagName.trim()) return;
    const requestName = tagName.trim();
    const requestColor = tagColor;
    setTagBusy(true);
    setNotice("");
    try {
      await onCreateTag(requestName, requestColor);
      setTagName((current) => (current.trim() === requestName ? "" : current));
      setNotice("标签已创建");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "标签创建失败", "error");
    } finally {
      setTagBusy(false);
    }
  };

  return (
    <section className="projects-page page-content">
      <header className="page-heading projects-page__heading">
        <div>
          <span className="eyebrow">PROJECTS / 项目管理</span>
          <h1><BlurText text="项目" /></h1>
          <p>按「我的方向」排座次，每个项目里的需求、任务和录音都从这里进。</p>
          <ProjectParentRow
            apiClient={apiClient}
            canEdit={canEdit}
            onNotice={setNotice}
            onProjectsChanged={onProjectsChanged}
          />
        </div>
        <div className="projects-page__actions">
          <div className="projects-count">
            <strong>{projects.length}</strong>
            <span>个项目<br />在跟进</span>
          </div>
          {canEdit && (
            <>
              {/* 全部项目概览只在电脑上有（手机上 #graph 退回项目列表），路由在 App 里 */}
              <a className="projects-graph-link" href="#graph">
                全部项目图
              </a>
              <button
                className="projects-create"
                onClick={() => setFormModal({ mode: "create", project: null })}
                type="button"
              >
                ＋ 新建项目
              </button>
            </>
          )}
        </div>
      </header>

      <DirectionBar
        canWrite={canEdit}
        onSeatsChange={saveSeats}
        onToggle={toggleProject}
        projects={directionProjects}
        selected={pickedIds}
      />

      <form className="projects-query" onSubmit={runQuery}>
        <label className="projects-query__field">
          <span>项目名称</span>
          <input
            onChange={(event) => setNameDraft(event.target.value)}
            placeholder="输入项目名称"
            value={nameDraft}
          />
        </label>
        <label className="projects-query__field">
          <span>材料根目录</span>
          <select onChange={(event) => setRootDraft(event.target.value as RootFilter)} value={rootDraft}>
            <option value="all">全部</option>
            <option value="attached">已挂</option>
            <option value="unattached">未挂</option>
          </select>
        </label>
        <span className="projects-query__buttons">
          <button className="projects-query__search" type="submit">
            查询
          </button>
          <button className="projects-query__reset" onClick={resetQuery} type="button">
            重置
          </button>
        </span>
      </form>

      <NoticeBanner notice={notice} onDismiss={dismissNotice} />

      {seatedShown.length === 0 && unseatedShown.length === 0 && <p className="projects-empty">没有符合条件的项目</p>}

      {seatedShown.length > 0 && (
        <div className="project-panels">
          {seatedShown.map((project) => (
            <ProjectPanel
              canEdit={canEdit}
              key={project.id}
              meetingTotal={meetingCount(project, meetings)}
              onEdit={(target) => setFormModal({ mode: "edit", project: target })}
              onOpen={onOpenProject}
              project={project}
            />
          ))}
        </div>
      )}

      {unseatedShown.length > 0 && (
        <section className="project-unseated">
          <h2 className="project-unseated__title">
            未排座次 <span>按最近会议排</span>
          </h2>
          <ul aria-label="未排座次的项目列表" className="project-rows">
            {unseatedRows.map((project) => (
              <ProjectRow
                canEdit={canEdit}
                key={project.id}
                meetingTotal={meetingCount(project, meetings)}
                onEdit={(target) => setFormModal({ mode: "edit", project: target })}
                onOpen={onOpenProject}
                project={project}
              />
            ))}
          </ul>
          {unseatedShown.length > UNSEATED_FOLDED_ROWS && (
            <button
              aria-expanded={unseatedOpen}
              className="project-more"
              onClick={() => setUnseatedOpen((open) => !open)}
              type="button"
            >
              {unseatedOpen ? "收起" : `还有 ${unseatedShown.length - UNSEATED_FOLDED_ROWS} 个项目`} {unseatedOpen ? "▴" : "▾"}
            </button>
          )}
        </section>
      )}

      {canEdit && (
        <div className="classification-create desktop-only">
          <form className="classification-form" onSubmit={(event) => void createTag(event)}>
            <div><span className="eyebrow">NEW TAG</span><h2>新建标签</h2></div>
            <label>
              <span>标签名称</span>
              <input aria-label="标签名称" onChange={(event) => setTagName(event.target.value)} placeholder="例如：待跟进" value={tagName} />
            </label>
            <label className="color-field">
              <span>识别色</span>
              <input aria-label="标签颜色" onChange={(event) => setTagColor(event.target.value)} type="color" value={tagColor} />
            </label>
            <button className="primary-button" disabled={tagBusy || !tagName.trim()} type="submit">新建标签</button>
          </form>
        </div>
      )}

      <div className="tag-catalogue">
        <span className="eyebrow">TAG INDEX</span>
        <h2>标签目录</h2>
        <div>
          {tags.map((tag) => (
            <span key={tag.id} style={{ "--tag-color": tag.color } as CSSProperties}>
              {tag.name}
            </span>
          ))}
          {tags.length === 0 && <p className="muted">尚未创建标签。</p>}
        </div>
      </div>

      {formModal && (
        <ProjectFormModal
          apiClient={apiClient}
          canPickFolders={canEdit}
          mode={formModal.mode}
          onClose={() => setFormModal(null)}
          onSaved={(saved) => {
            const wasCreate = formModal.mode === "create";
            setFormModal(null);
            void onProjectsChanged();
            // 新建成功直接进详情（A-01-8）；编辑留在列表原地刷新
            if (wasCreate) onOpenProject(saved.id);
          }}
          onUseExisting={onOpenProject}
          projects={projects}
          onMerged={() => {
            setFormModal(null);
            void onProjectsChanged();
          }}
          onDeleted={() => {
            setFormModal(null);
            void onProjectsChanged();
          }}
          project={formModal.project}
        />
      )}
    </section>
  );
}
