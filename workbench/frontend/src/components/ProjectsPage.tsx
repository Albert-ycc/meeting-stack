import { useState, type FormEvent } from "react";

import type { MeetingSummary, Project, Tag } from "../types";

interface ProjectsPageProps {
  canEdit: boolean;
  meetings: MeetingSummary[];
  onCreateProject: (name: string, color: string) => Promise<void>;
  onCreateTag: (name: string, color: string) => Promise<void>;
  onOpenProject: (projectId: string) => void;
  projects: Project[];
  tags: Tag[];
}

export function ProjectsPage({
  canEdit,
  meetings,
  onCreateProject,
  onCreateTag,
  onOpenProject,
  projects,
  tags,
}: ProjectsPageProps) {
  const [projectName, setProjectName] = useState("");
  const [projectColor, setProjectColor] = useState("#2c8d83");
  const [tagName, setTagName] = useState("");
  const [tagColor, setTagColor] = useState("#f0783b");
  const [busy, setBusy] = useState<"project" | "tag" | null>(null);
  const [notice, setNotice] = useState("");

  const createProject = async (event: FormEvent) => {
    event.preventDefault();
    if (!projectName.trim()) return;
    const requestName = projectName.trim();
    const requestColor = projectColor;
    setBusy("project");
    setNotice("");
    try {
      await onCreateProject(requestName, requestColor);
      setProjectName((current) => current.trim() === requestName ? "" : current);
      setNotice("项目已创建");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "项目创建失败");
    } finally {
      setBusy(null);
    }
  };

  const createTag = async (event: FormEvent) => {
    event.preventDefault();
    if (!tagName.trim()) return;
    const requestName = tagName.trim();
    const requestColor = tagColor;
    setBusy("tag");
    setNotice("");
    try {
      await onCreateTag(requestName, requestColor);
      setTagName((current) => current.trim() === requestName ? "" : current);
      setNotice("标签已创建");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "标签创建失败");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="projects-page page-content">
      <header className="page-heading">
        <div>
          <span className="eyebrow">COLLECTIONS / 项目</span>
          <h1>项目与档案标签</h1>
          <p>项目决定主归属，标签保留跨项目的检索线索。</p>
        </div>
      </header>
      {canEdit && (
        <div className="classification-create desktop-only">
          <form className="classification-form" onSubmit={(event) => void createProject(event)}>
            <div><span className="eyebrow">NEW COLLECTION</span><h2>新建项目</h2></div>
            <label>
              <span>项目名称</span>
              <input aria-label="项目名称" onChange={(event) => setProjectName(event.target.value)} placeholder="例如：星河随访" value={projectName} />
            </label>
            <label className="color-field">
              <span>识别色</span>
              <input aria-label="项目颜色" onChange={(event) => setProjectColor(event.target.value)} type="color" value={projectColor} />
            </label>
            <button className="primary-button" disabled={busy !== null || !projectName.trim()} type="submit">新建项目</button>
          </form>
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
            <button className="primary-button" disabled={busy !== null || !tagName.trim()} type="submit">新建标签</button>
          </form>
        </div>
      )}
      {notice && <div className="action-banner" role="status">{notice}</div>}
      <div className="project-ledger">
        {projects.map((project) => {
          const count =
            project.meeting_count ??
            meetings.filter((meeting) => meeting.project_id === project.id).length;
          return (
            <button key={project.id} onClick={() => onOpenProject(project.id)} type="button">
              <i style={{ background: project.color }} />
              <span>
                <strong>{project.name}</strong>
                <small>{count} 场会议</small>
              </span>
              <em>打开 →</em>
            </button>
          );
        })}
        {projects.length === 0 && <p className="muted">尚未创建项目。</p>}
      </div>
      <div className="tag-catalogue">
        <span className="eyebrow">TAG INDEX</span>
        <h2>标签目录</h2>
        <div>
          {tags.map((tag) => (
            <span key={tag.id} style={{ "--tag-color": tag.color } as React.CSSProperties}>
              {tag.name}
            </span>
          ))}
          {tags.length === 0 && <p className="muted">尚未创建标签。</p>}
        </div>
      </div>
    </section>
  );
}
