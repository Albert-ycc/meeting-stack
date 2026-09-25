import { useMemo, useState, type CSSProperties, type FormEvent } from "react";

import type { ApiClient } from "../api";
import type { MeetingSummary, Project, Tag } from "../types";
import { BlurText } from "./motion/BlurText";
import { ProjectFormModal } from "./ProjectFormModal";
import "./ProjectsPage.css";

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

function meetingCount(project: Project, meetings: MeetingSummary[]): number {
  return project.meeting_count ?? meetings.filter((meeting) => meeting.project_id === project.id).length;
}

function rootsLabel(project: Project): { text: string; attached: boolean } {
  const roots = project.material_roots ?? [];
  if (roots.length === 0) return { text: "未挂", attached: false };
  if (roots.length === 1) return { text: roots[0].path, attached: true };
  return { text: `${roots[0].path}（等 ${roots.length} 个）`, attached: true };
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
  const [nameDraft, setNameDraft] = useState("");
  const [rootDraft, setRootDraft] = useState<RootFilter>("all");
  const [appliedName, setAppliedName] = useState("");
  const [appliedRoot, setAppliedRoot] = useState<RootFilter>("all");

  const [tagName, setTagName] = useState("");
  const [tagColor, setTagColor] = useState("#f0783b");
  const [tagBusy, setTagBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const [formModal, setFormModal] = useState<{ mode: "create" | "edit"; project: Project | null } | null>(null);

  const rows = useMemo(() => {
    const keyword = appliedName.trim().toLowerCase();
    const filtered = projects.filter((project) => {
      if (keyword && !project.name.toLowerCase().includes(keyword)) return false;
      const attached = (project.material_roots ?? []).length > 0;
      if (appliedRoot === "attached" && !attached) return false;
      if (appliedRoot === "unattached" && attached) return false;
      return true;
    });
    return [...filtered].sort((a, b) => meetingCount(b, meetings) - meetingCount(a, meetings));
  }, [projects, meetings, appliedName, appliedRoot]);

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
      setNotice(error instanceof Error ? error.message : "标签创建失败");
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
        </div>
        {canEdit && (
          <button
            className="projects-create"
            onClick={() => setFormModal({ mode: "create", project: null })}
            type="button"
          >
            ＋ 新建项目
          </button>
        )}
      </header>

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

      {notice && <div className="action-banner" role="status">{notice}</div>}

      <div className="projects-table-card">
        <table className="projects-table">
          <thead>
            <tr>
              <th>项目名称</th>
              <th>材料根目录</th>
              <th>进行中需求</th>
              <th>全部需求</th>
              <th>会议</th>
              <th>未完成任务</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((project) => {
              const roots = rootsLabel(project);
              return (
                <tr key={project.id}>
                  <td>
                    <button className="projects-table__name" onClick={() => onOpenProject(project.id)} type="button">
                      <i style={{ background: project.color }} />
                      {project.name}
                    </button>
                  </td>
                  <td className={roots.attached ? "projects-table__root" : "projects-table__root is-empty"}>
                    {roots.text}
                  </td>
                  <td>{project.requirement_counts?.active ?? 0}</td>
                  <td>{project.requirement_counts?.all ?? 0}</td>
                  <td>{meetingCount(project, meetings)}</td>
                  <td>{project.open_task_count ?? 0}</td>
                  <td>
                    <span className="projects-table__ops">
                      <button className="text-button" onClick={() => onOpenProject(project.id)} type="button">
                        查看
                      </button>
                      {canEdit && (
                        <button
                          className="text-button"
                          onClick={() => setFormModal({ mode: "edit", project })}
                          type="button"
                        >
                          编辑
                        </button>
                      )}
                    </span>
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td className="projects-table__empty" colSpan={7}>
                  没有符合条件的项目
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <p className="projects-table__count">共 {rows.length} 条</p>
      </div>

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
          project={formModal.project}
        />
      )}
    </section>
  );
}
