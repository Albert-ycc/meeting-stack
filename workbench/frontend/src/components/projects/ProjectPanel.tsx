import type { MouseEvent } from "react";

import { formatDurationText } from "../../format";
import type { Project } from "../../types";
import { MeetingRhythm } from "./MeetingRhythm";
import { projectFacts } from "./projectFacts";
import "./ProjectPanel.css";

interface ProjectItemProps {
  project: Project;
  /** 服务端的会议数；旧后端没有时由页面按已载入的会议数兜底 */
  meetingTotal: number;
  /** 只在电脑上能改（手机上只读） */
  canEdit: boolean;
  onOpen: (projectId: string) => void;
  onEdit: (project: Project) => void;
}

function StaleNote({ text }: { text: string }) {
  return (
    <span className="project-stale">
      <svg aria-hidden="true" fill="none" height="11" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 12 12" width="11">
        <circle cx="6" cy="6" r="4.6" />
        <path d="M6 3.4V6l1.8 1.1" strokeLinecap="round" />
      </svg>
      {text}
    </span>
  );
}

function RequirementLines({ project, meetingTotal }: Pick<ProjectItemProps, "project" | "meetingTotal">) {
  const facts = projectFacts(project, meetingTotal);
  if (facts.titles.length === 0) return <p className="project-reqs__none">没有进行中的需求</p>;
  return (
    <ul className="project-reqs__list">
      {facts.titles.map((title) => (
        <li key={title} title={title}>
          {title}
        </li>
      ))}
    </ul>
  );
}

function EditButton({ project, onEdit }: Pick<ProjectItemProps, "project" | "onEdit">) {
  return (
    <button
      aria-label={`编辑${project.name}`}
      className="project-edit"
      onClick={(event: MouseEvent) => {
        event.stopPropagation();
        onEdit(project);
      }}
      type="button"
    >
      编辑
    </button>
  );
}

/** 面板和行都是整块可点：名字、「进入」只是键盘够得着的入口，点击冒泡到外层统一打开 */
function EnterButton({ project }: { project: Project }) {
  return (
    <button aria-label={`进入${project.name}`} className="project-enter" type="button">
      进入 <span aria-hidden="true">→</span>
    </button>
  );
}

/** 已排座次的项目：同尺寸大面板 */
export function ProjectPanel({ project, meetingTotal, canEdit, onOpen, onEdit }: ProjectItemProps) {
  const facts = projectFacts(project, meetingTotal);
  return (
    <article className="project-panel" onClick={() => onOpen(project.id)}>
      <header className="project-panel__head">
        {project.seat != null && <span className="project-panel__seat">{project.seat}</span>}
        <h2>
          <button className="project-panel__name" title={project.name} type="button">
            {project.name}
          </button>
        </h2>
        {facts.pending > 0 && <span className="project-tag">待认领 {facts.pending}</span>}
      </header>
      <div className="project-panel__body">
        <div className="project-reqs">
          <span className="project-label">进行中需求 {facts.requirementCount}</span>
          <RequirementLines meetingTotal={meetingTotal} project={project} />
        </div>
        <div className="project-panel__side">
          <MeetingRhythm weeks={project.weekly_meetings} />
          <div className="project-latest">
            <span className="project-label">
              最近一场会
              {facts.staleText && <StaleNote text={facts.staleText} />}
            </span>
            {facts.latestTitle || facts.latestDate ? (
              <p className="project-latest__line">
                <span title={facts.latestTitle ?? undefined}>{facts.latestTitle}</span>
                <time>{facts.latestDate}</time>
              </p>
            ) : (
              <p className="project-latest__none">还没有会议</p>
            )}
          </div>
        </div>
      </div>
      <footer className="project-panel__foot">
        <p className="project-stats">
          <span>需求 {facts.requirementCount}</span>
          <span>待办 {facts.openTasks}</span>
          <span>会议 {facts.meetings}</span>
          <span>录音 {facts.recordingMs > 0 ? formatDurationText(facts.recordingMs) : "—"}</span>
        </p>
        {canEdit && <EditButton onEdit={onEdit} project={project} />}
        <EnterButton project={project} />
      </footer>
    </article>
  );
}

/** 未排座次的项目：整宽列表行，同一行里把节奏、需求、数字和最近一场会排开 */
export function ProjectRow({ project, meetingTotal, canEdit, onOpen, onEdit }: ProjectItemProps) {
  const facts = projectFacts(project, meetingTotal);
  return (
    <li className="project-row" onClick={() => onOpen(project.id)}>
      <div className="project-row__name">
        <span className="project-row__title">
          <i style={{ background: project.color }} />
          <button className="project-panel__name" title={project.name} type="button">
            {project.name}
          </button>
          {facts.pending > 0 && <span className="project-tag">待认领 {facts.pending}</span>}
        </span>
        {facts.staleText && <StaleNote text={facts.staleText} />}
      </div>
      <MeetingRhythm weeks={project.weekly_meetings} />
      <div className="project-reqs">
        <RequirementLines meetingTotal={meetingTotal} project={project} />
      </div>
      <dl className="project-row__nums">
        <div>
          <dt>需求</dt>
          <dd>{facts.requirementCount}</dd>
        </div>
        <div>
          <dt>待办</dt>
          <dd>{facts.openTasks}</dd>
        </div>
        <div>
          <dt>会议</dt>
          <dd>{facts.meetings}</dd>
        </div>
      </dl>
      <div className="project-latest">
        {facts.latestTitle || facts.latestDate ? (
          <>
            <p className="project-latest__line">
              <span title={facts.latestTitle ?? undefined}>{facts.latestTitle}</span>
            </p>
            <p className="project-latest__meta">
              {facts.latestDate} 最近一场{facts.recordingMs > 0 ? ` · 录音 ${formatDurationText(facts.recordingMs)}` : ""}
            </p>
          </>
        ) : (
          <p className="project-latest__none">还没有会议</p>
        )}
      </div>
      <div className="project-row__ops">
        {canEdit && <EditButton onEdit={onEdit} project={project} />}
        <EnterButton project={project} />
      </div>
    </li>
  );
}
