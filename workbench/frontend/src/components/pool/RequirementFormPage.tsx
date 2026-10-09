import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, type ApiClient } from "../../api";
import { formatDurationText, formatMonthDayClock } from "../../format";
import { isComposingKeydown } from "../../keyboard";
import type {
  CandidateDetail,
  MaterialFolderStat,
  PoolItem,
  Project,
  RequirementDetail,
  RequirementPriority,
  RequirementSource,
  RequirementStatus,
  Task,
  TitleConflict,
} from "../../types";
import { MaterialFolderPickerModal } from "../MaterialFolderPickerModal";
import { REQUIREMENT_PRIORITIES, REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import { CloseTasksDialog } from "./CloseTasksDialog";
import { anchorLabel, PosterCard } from "./PosterCard";
import { PosterWaveform } from "./PosterWaveform";
import { openTasksOf } from "./requirementStatus";
import { SourcePickerDialog, type SourceDraft } from "./SourcePickerDialog";
import "./RequirementFormPage.css";

export const SUMMARY_MAX = 70;

/** 有未保存的改动时点「取消」要问的话；和会议页的离开确认同一句式，App 里拦侧栏导航时用同一句 */
export const LEAVE_FORM_CONFIRM = "当前需求仍有未保存修改。放弃这些修改并离开吗？";

export type RequirementFormResult = {
  kind: "claimed" | "merged" | "created" | "edited";
  requirement: RequirementDetail;
};

/** 从逐字稿选句进来（R01-10、S10）：来源带上，所属项目默认取会议归属 */
export interface RequirementPrefill {
  source: SourceDraft;
  projectId: string | null;
  /** 那场会是从哪个页面打开的（面包屑第一段，和会议页返回按钮一致）；不传写「录音档案」 */
  from?: string;
}

interface RequirementFormPageProps {
  apiClient: ApiClient;
  /** claim：认领候选（S02）；create：需求池里新建或逐字稿选句（S09、S10）；edit：修改需求（S11） */
  mode: "claim" | "create" | "edit";
  candidateId?: string;
  requirementId?: string;
  prefill?: RequirementPrefill;
  projects: Project[];
  canPickFolders: boolean;
  onCancel: () => void;
  onDone: (result: RequirementFormResult) => void;
  onOpenProject?: (projectId: string) => void;
  /** 候选已经被认领、合并时，提示里的［打开那条需求］ */
  onOpenRequirement?: (requirementId: string) => void;
  /** 表单有没有未保存的改动：一变就报；保存成功、取消确认放弃、页面卸载前都会先报一次「没有」。App 据此拦侧栏导航 */
  onDirtyChange?: (dirty: boolean) => void;
}

type ExistingRequirement = NonNullable<TitleConflict["existing"]>;

/** 说明按码点数，和后端的 70 字一致（一个汉字、一个常见表情各算一个；组合表情按它的码点数算） */
export function summaryLength(value: string): number {
  return [...value.trim()].length;
}

/** 和后端 clean_title 一致：去掉零宽字符这类不可见的格式字符（Unicode Cf），再去首尾空白 */
export function cleanTitle(value: string): string {
  // 和后端 clean_title 同一个规矩：去掉零宽字符，不换行空格、全角空格统一成一个普通空格，去首尾空白
  return value
    .replace(/\p{Cf}/gu, "")
    .replace(/\p{Zs}/gu, " ")
    .replace(/ {2,}/g, " ")
    .trim();
}

function recency(project: Project): number {
  const time = project.latest_meeting_date ? new Date(project.latest_meeting_date).getTime() : Number.NaN;
  return Number.isNaN(time) ? Number.NEGATIVE_INFINITY : time;
}

/** 所属项目的列法（R01-12）：「我的方向」里排了座次的按名次在前；其余按最近会议由近到远，一场会都没有的在最后 */
export function splitProjects(projects: Project[]): { seated: Project[]; others: Project[] } {
  const seated = projects.filter((project) => project.seat != null).sort((a, b) => (a.seat ?? 0) - (b.seat ?? 0));
  const others = projects
    .filter((project) => project.seat == null)
    .sort((a, b) => recency(b) - recency(a) || a.name.localeCompare(b.name, "zh-CN"));
  return { seated, others };
}

// 已完成的也能并：候选并进去后需求重新打开为进行中（D13）
const MERGEABLE = new Set(["active", "shelved", "done"]);
// 修改需求时状态只在这三态之间切换，不能改回待认领（R04-5）
const EDIT_STATUSES: RequirementStatus[] = ["active", "done", "shelved"];

function sourceKey(source: Pick<SourceDraft, "meeting_id" | "quote" | "anchor_ms"> | null | undefined): string {
  return source ? `${source.meeting_id}|${source.quote}|${source.anchor_ms ?? ""}` : "";
}

function draftOf(source: RequirementSource): SourceDraft {
  const { id: _id, kind: _kind, via_candidate_title: _via, ...draft } = source;
  return draft;
}

function samePaths(left: string[], right: string[]): boolean {
  return left.length === right.length && [...left].sort().join("\n") === [...right].sort().join("\n");
}

function SearchIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="15" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16" width="15">
      <circle cx="7" cy="7" r="4.6" />
      <path d="M10.5 10.5L14 14" />
    </svg>
  );
}

// 下拉里列表最高多少、列表之外（顶上搜索框、底下提醒）占多少、窗口底部固定的操作栏多高（算往上还是往下展开用）
const MENU_LIST_MAX = 320;
const MENU_CHROME_HEIGHT = 90;
const ACTION_BAR_HEIGHT = 80;
// 项目多到列表要滚动时，底下提醒一句还有更多、可以搜（macOS 的滚动条平时是藏着的）
const MENU_HINT_FROM = 8;

interface ProjectSelectProps {
  projects: Project[];
  value: string;
  disabled: boolean;
  labelledBy: string;
  onChange: (projectId: string) => void;
}

/**
 * 所属项目的下拉（S02-b）：按「我的方向」座次列在前，未排座次的在后，顶上能按名字搜。
 * 新增、认领、修改三种页用的是同一个。
 */
function ProjectSelect({ projects, value, disabled, labelledBy, onChange }: ProjectSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [place, setPlace] = useState<{ side: "below" | "above"; listMax: number }>({ side: "below", listMax: MENU_LIST_MAX });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  const { seated, others } = useMemo(() => splitProjects(projects), [projects]);
  const needle = query.trim().toLocaleLowerCase();
  const matches = (project: Project) => !needle || project.name.toLocaleLowerCase().includes(needle);
  const seatedShown = seated.filter(matches);
  const othersShown = others.filter(matches);
  const options = [...seatedShown, ...othersShown];
  const current = projects.find((project) => project.id === value) ?? null;
  const optionId = (project: Project) => `${listId}-${project.id}`;

  const openMenu = () => {
    setQuery("");
    setActive(Math.max(0, [...seated, ...others].findIndex((project) => project.id === value)));
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      // 窗口矮、下沿又有固定的操作栏：下面放不下一整页就往上展开，列表高度跟着剩下的地方走
      const below = window.innerHeight - rect.bottom - ACTION_BAR_HEIGHT - 12;
      const above = rect.top - 12;
      const side = below >= MENU_LIST_MAX - 60 || below >= above ? "below" : "above";
      setPlace({ side, listMax: Math.max(140, Math.min(MENU_LIST_MAX, (side === "below" ? below : above) - MENU_CHROME_HEIGHT)) });
    }
    setOpen(true);
  };
  const closeMenu = (refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  };
  const choose = (project: Project) => {
    onChange(project.id);
    closeMenu(true);
  };

  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  // 点到下拉外面就收起
  useEffect(() => {
    if (!open) return;
    const onMouseDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [open]);

  // 方向键移动时让高亮的那一项始终在列表里看得见
  const activeOption = options[active];
  useEffect(() => {
    if (open && activeOption) document.getElementById(`${listId}-${activeOption.id}`)?.scrollIntoView?.({ block: "nearest" });
  }, [activeOption, listId, open]);

  const renderOption = (project: Project) => {
    const index = options.indexOf(project);
    const selected = project.id === value;
    return (
      <button
        aria-selected={selected}
        className={`project-select__option ${index === active ? "is-active" : ""}`}
        id={optionId(project)}
        key={project.id}
        onClick={() => choose(project)}
        onMouseEnter={() => setActive(index)}
        role="option"
        tabIndex={-1}
        type="button"
      >
        {project.seat != null && <i className="project-select__seat">{project.seat}</i>}
        <span className="project-select__name">{project.name}</span>
        {selected && (
          <span aria-hidden="true" className="project-select__check">
            ✓
          </span>
        )}
      </button>
    );
  };

  return (
    <div
      className="project-select"
      // Tab 把焦点带出下拉就收起；焦点落到没有目标的地方（relatedTarget 为空，Safari 点按钮就是这样）不收，免得吞掉选项的点击
      onBlur={(event) => {
        if (open && event.relatedTarget && !rootRef.current?.contains(event.relatedTarget as Node)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open && !event.nativeEvent.isComposing) {
          event.stopPropagation();
          closeMenu(true);
        }
      }}
      ref={rootRef}
    >
      <button
        aria-controls={open ? listId : undefined}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-labelledby={labelledBy}
        className="project-select__trigger"
        disabled={disabled}
        onClick={() => (open ? closeMenu(false) : openMenu())}
        onKeyDown={(event) => {
          if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            openMenu();
          }
        }}
        ref={triggerRef}
        role="combobox"
        type="button"
      >
        {current ? (
          <>
            {current.seat != null && <i className="project-select__seat">{current.seat}</i>}
            <span className="project-select__name">{current.name}</span>
          </>
        ) : (
          <span className="project-select__placeholder">选择项目</span>
        )}
        <span aria-hidden="true" className="project-select__caret">
          ⌄
        </span>
      </button>

      {open && (
        <div className="project-select__menu" data-side={place.side}>
          <label className="project-select__search">
            <SearchIcon />
            <input
              aria-activedescendant={activeOption ? optionId(activeOption) : undefined}
              aria-controls={listId}
              aria-label="搜项目"
              onChange={(event) => {
                setQuery(event.target.value);
                setActive(0);
              }}
              onKeyDown={(event) => {
                if (isComposingKeydown(event)) return;
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setActive((index) => Math.min(index + 1, options.length - 1));
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setActive((index) => Math.max(index - 1, 0));
                } else if (event.key === "Enter") {
                  event.preventDefault();
                  if (activeOption) choose(activeOption);
                }
              }}
              placeholder="搜项目"
              ref={searchRef}
              value={query}
            />
          </label>
          <div aria-label="项目" className="project-select__list" id={listId} role="listbox" style={{ maxHeight: place.listMax }}>
            {seatedShown.length > 0 && (
              <div aria-label="我的方向" role="group">
                <p aria-hidden="true" className="project-select__section">
                  我的方向
                </p>
                {seatedShown.map(renderOption)}
              </div>
            )}
            {othersShown.length > 0 && (
              <div aria-label="未排座次" role="group">
                <p aria-hidden="true" className="project-select__section">
                  未排座次 · 按最近会议排
                </p>
                {othersShown.map(renderOption)}
              </div>
            )}
          </div>
          {options.length === 0 && <p className="project-select__empty">没有匹配的项目</p>}
          {!needle && options.length > MENU_HINT_FROM && (
            <p className="project-select__hint">共 {options.length} 个项目，滚动查看或输入名字搜索</p>
          )}
        </div>
      )}
    </div>
  );
}

/** 来源面板：会名、时间、原话和波形；children 是面板底下的操作或补充说明 */
function SourcePanel({ source, children }: { source: SourceDraft; children?: ReactNode }) {
  return (
    <div className="form-source">
      <div className="form-source__head">
        <strong>{source.meeting_title}</strong>
        <span>
          {formatMonthDayClock(source.recording_date)}
          {source.duration_ms ? ` · ${formatDurationText(source.duration_ms)}` : ""}
        </span>
      </div>
      {(source.anchor_ms !== null || source.quote) && (
        <p className="form-source__quote">
          {source.anchor_ms !== null && <span className="form-source__anchor">▶ {anchorLabel(source.anchor_ms)}</span>}
          {source.quote && <span>「{source.quote}」</span>}
        </p>
      )}
      <PosterWaveform
        artifactId={source.audio_artifact_id}
        bars={260}
        height={34}
        label={`${source.meeting_title} 的录音波形`}
        markers={source.anchor_ms !== null ? [{ atMs: source.anchor_ms }] : []}
      />
      {children}
    </div>
  );
}

/** 修改页来源底下的一行：合并进来的原话（R01 合并），只看不改 */
function mergedNote(merged: RequirementSource[], originMeetingId: string | null): string {
  const label = (item: RequirementSource) =>
    [
      item.via_candidate_title ? `候选「${item.via_candidate_title}」` : null,
      item.meeting_id !== originMeetingId ? item.meeting_title : null,
      item.anchor_ms !== null ? anchorLabel(item.anchor_ms) : null,
    ]
      .filter(Boolean)
      .join(" · ");
  if (merged.length === 1) {
    const [item] = merged;
    return item.via_candidate_title
      ? `另有 1 句原话合并自${label(item)}`
      : `另有 1 句原话合并进来 · ${label(item)}`;
  }
  return `另有 ${merged.length} 句原话合并进来：${merged.map(label).join("；")}`;
}

/** 表单进来时的样子：改没改过、改了哪几项，都和它比（修改时只提交改过的几项） */
interface Baseline {
  title: string;
  summary: string;
  projectId: string;
  priority: RequirementPriority;
  status: RequirementStatus;
  source: SourceDraft | null;
  folderPaths: string[];
}

/**
 * 新增、认领、修改需求的二级页（R04-1：不是弹窗；保存或放弃后回到进入前的页面）。
 * 左边表单、右边「墙上预览」，底部一条固定的操作栏。认领时需求名和说明由 AI 预填，
 * 来源出自会议纪要、不可改；需求名或所属项目一变，停 300ms 就查同项目有没有同名需求（R04-9），
 * 撞名时红框加红字、不能保存，认领页另给「改为合并到那一条」（R01 异常）。
 * 新增、修改时来源可以选、换、清空（R04-3、R04-5）；修改时状态在进行中、已完成、已搁置之间切换，
 * 只提交改过的几项，一处都没改时保存置灰。
 */
export function RequirementFormPage({
  apiClient,
  mode,
  candidateId,
  requirementId,
  prefill,
  projects,
  canPickFolders,
  onCancel,
  onDone,
  onOpenProject,
  onOpenRequirement,
  onDirtyChange,
}: RequirementFormPageProps) {
  const claiming = mode === "claim";
  const editing = mode === "edit";
  const [candidate, setCandidate] = useState<CandidateDetail | null>(null);
  const [requirement, setRequirement] = useState<RequirementDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [projectId, setProjectId] = useState(prefill?.projectId ?? "");
  const [priority, setPriority] = useState<RequirementPriority>("P2");
  const [status, setStatus] = useState<RequirementStatus>("active");
  // 新增、修改时的来源（认领时来源取候选的、不可改）
  const [source, setSource] = useState<SourceDraft | null>(prefill?.source ?? null);
  const [pickingSource, setPickingSource] = useState(false);
  const [folders, setFolders] = useState<MaterialFolderStat[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  // 改成已完成、已搁置时名下还有没做完的待办：先问一起关掉还是留着（D10），答了才保存
  const [askingTasks, setAskingTasks] = useState<Task[] | null>(null);
  // 同项目里的同名需求：边填边查的结果，或保存时后端 409 带回来的；只对查它时的需求名和项目有效
  const [found, setFound] = useState<{ title: string; projectId: string; existing: ExistingRequirement | null } | null>(null);
  // 保存时 409 是后端刚给的结论，比任何更早发出去、还在路上的查重结果都新：每撞一次名就加一，旧的查重回来也不再盖掉它
  const conflictEpoch = useRef(0);
  // 换项目前各个项目下选好的材料文件夹：换走再换回来，原来的文件夹还在
  const folderStash = useRef(new Map<string, MaterialFolderStat[]>());
  const titleRef = useRef<HTMLInputElement>(null);
  const conflictRef = useRef<HTMLDivElement>(null);
  const focusedOnce = useRef(false);
  const dirtyCallback = useRef(onDirtyChange);
  const projectLabelId = useId();
  const conflictId = useId();

  // 从滚动过的需求池点进来时页面停在半截：二级页从页头开始看
  useEffect(() => {
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, []);

  useEffect(() => {
    if (!claiming || !candidateId) return;
    let active = true;
    void apiClient
      .requirementCandidate(candidateId)
      .then((detail) => {
        if (!active) return;
        setCandidate(detail);
        setTitle(detail.title);
        setSummary(detail.summary);
        setProjectId(detail.project_id ?? "");
      })
      .catch((err: unknown) => {
        if (active) setLoadError(err instanceof Error ? err.message : "候选读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, candidateId, claiming]);

  useEffect(() => {
    if (!editing || !requirementId) return;
    let active = true;
    void apiClient
      .requirement(requirementId)
      .then((detail) => {
        if (!active) return;
        setRequirement(detail);
        setTitle(detail.title);
        setSummary(detail.summary ?? "");
        setProjectId(detail.project_id);
        setPriority(detail.priority);
        setStatus(detail.status);
        setFolders(detail.folders);
        setSource(detail.source ? draftOf(detail.source) : null);
      })
      .catch((err: unknown) => {
        if (active) setLoadError(err instanceof Error ? err.message : "需求读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, editing, requirementId]);

  const project = projects.find((item) => item.id === projectId) ?? null;
  const chars = summaryLength(summary);
  const cleanedTitle = cleanTitle(title);
  const handled = claiming && candidate !== null && candidate.status !== "pending";
  const ready = claiming ? candidate !== null : editing ? requirement !== null : true;

  // 换所属项目：原来那个项目根目录下的文件夹不再合法（D6），先收起来；换回去时原样还给
  const changeProject = (next: string) => {
    if (next === projectId) return;
    folderStash.current.set(projectId, folders);
    setFolders(folderStash.current.get(next) ?? []);
    setProjectId(next);
  };

  const baseline: Baseline | null = claiming
    ? candidate && {
        title: cleanTitle(candidate.title),
        summary: candidate.summary,
        projectId: candidate.project_id ?? "",
        priority: "P2",
        status: "active",
        source: null,
        folderPaths: [],
      }
    : editing
      ? requirement && {
          title: cleanTitle(requirement.title),
          summary: requirement.summary ?? "",
          projectId: requirement.project_id,
          priority: requirement.priority,
          status: requirement.status,
          source: requirement.source ? draftOf(requirement.source) : null,
          folderPaths: requirement.folders.map((folder) => folder.path),
        }
      : {
          title: "",
          summary: "",
          projectId: prefill?.projectId ?? "",
          priority: "P2",
          status: "active",
          source: prefill?.source ?? null,
          folderPaths: [],
        };
  const changed = baseline && {
    title: cleanedTitle !== baseline.title,
    summary: summary.trim() !== baseline.summary.trim(),
    project: projectId !== baseline.projectId,
    priority: priority !== baseline.priority,
    status: status !== baseline.status,
    source: sourceKey(source) !== sourceKey(baseline.source),
    folders: !samePaths(
      folders.map((folder) => folder.path),
      baseline.folderPaths,
    ),
  };
  const dirty = changed !== null && Object.values(changed).some(Boolean);

  // 需求名或所属项目变了，停 300ms 查同项目有没有同名需求；修改时没动过这两项就不必查，传本需求 id 不和自己比
  const checkable =
    ready && !handled && cleanedTitle !== "" && projectId !== "" && !(editing && changed && !changed.title && !changed.project);
  useEffect(() => {
    if (!checkable) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void (async () => {
        const epoch = conflictEpoch.current;
        try {
          const { existing } = await apiClient.requirementTitleCheck(projectId, cleanedTitle, editing ? requirementId : undefined);
          if (active && epoch === conflictEpoch.current) setFound({ title: cleanedTitle, projectId, existing });
        } catch {
          // 查重本身失败不拦保存：保存时后端的唯一约束照样会拦，并带回撞上的那一条
        }
      })();
    }, 300);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [apiClient, checkable, cleanedTitle, editing, projectId, requirementId]);

  const conflict =
    found?.existing && found.title === cleanedTitle && found.projectId === projectId ? found.existing : null;

  // 撞名的提示在需求名底下：窗口矮、页面滚过时滚到看得见的地方
  useEffect(() => {
    if (conflict) conflictRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [conflict]);

  // 进页面时焦点放在需求名上；认领、修改要等数据读回来、输入框能用了再放
  useEffect(() => {
    if (focusedOnce.current || !ready || handled) return;
    focusedOnce.current = true;
    titleRef.current?.focus();
  }, [ready, handled]);

  // 有改动时刷新、关页面要拦；侧栏导航由 App 根据 onDirtyChange 拦
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    dirtyCallback.current = onDirtyChange;
  }, [onDirtyChange]);
  useEffect(() => {
    dirtyCallback.current?.(dirty);
  }, [dirty]);
  useEffect(() => () => dirtyCallback.current?.(false), []);

  const canSave =
    ready &&
    !handled &&
    !saving &&
    cleanedTitle !== "" &&
    projectId !== "" &&
    chars <= SUMMARY_MAX &&
    !conflict &&
    (!editing || dirty);

  // 墙上预览：认领看候选的来源和数，修改看需求的，新增看表单上选的来源
  const previewSource: RequirementSource | null = claiming
    ? (candidate?.source ?? null)
    : source && { ...source, id: 0, kind: "origin", via_candidate_title: null };
  const meetingIds = new Set(
    [...(requirement?.meetings.map((meeting) => meeting.id) ?? []), source?.meeting_id].filter(Boolean),
  );
  const preview: PoolItem = {
    kind: "requirement",
    id: "preview",
    title: cleanedTitle,
    summary: summary.trim(),
    status: editing ? status : "active",
    // 预览里改了状态：底栏写今天完成、搁置（D12），不写上一次改状态的日子
    status_changed_at: editing && changed?.status ? new Date().toISOString() : (requirement?.status_changed_at ?? null),
    priority,
    project_id: projectId || null,
    project_name: project?.name ?? null,
    project_color: project?.color ?? null,
    project_seat: project?.seat ?? null,
    open_task_count: candidate?.open_task_count ?? requirement?.open_task_count ?? 0,
    meeting_count: candidate?.meeting_count ?? meetingIds.size,
    folder_count: folders.length,
    latest_meeting_date: candidate?.latest_meeting_date ?? previewSource?.recording_date ?? null,
    source: previewSource,
    follow_up_count: candidate?.follow_up_count ?? Math.max(meetingIds.size - 1, 0),
    similar_requirement: null,
    default_action: null,
    can_merge: false,
    created_at: candidate?.created_at ?? requirement?.created_at ?? new Date().toISOString(),
    updated_at: candidate?.updated_at ?? requirement?.updated_at ?? new Date().toISOString(),
  };

  const finish = (result: RequirementFormResult) => {
    // 保存成功是正常离开，先报「没有未保存的改动」，App 接着跳转时不会再拦
    dirtyCallback.current?.(false);
    onDone(result);
  };

  const cancel = () => {
    if (dirty && !window.confirm(LEAVE_FORM_CONFIRM)) return;
    dirtyCallback.current?.(false);
    onCancel();
  };

  const submit = () => {
    if (!canSave) return;
    if (editing && changed?.status && status !== "active") {
      const open = openTasksOf(requirement?.tasks ?? []);
      if (open.length > 0) {
        setAskingTasks(open);
        return;
      }
    }
    void save();
  };

  /** closeOpenTasks：问过待办怎么办才带（D10）；没问过不带，老调用不变 */
  const save = async (closeOpenTasks?: boolean) => {
    if (!canSave) return;
    setSaving(true);
    setError("");
    const folderPaths = folders.map((folder) => folder.path);
    const sourceInput = source && { meeting_id: source.meeting_id, quote: source.quote, anchor_ms: source.anchor_ms };
    try {
      if (claiming && candidateId) {
        finish({
          kind: "claimed",
          requirement: await apiClient.claimCandidate(candidateId, {
            title: cleanedTitle,
            summary: summary.trim(),
            project_id: projectId,
            priority,
            folder_paths: canPickFolders ? folderPaths : [],
          }),
        });
      } else if (editing && requirementId && changed) {
        // 只提交改过的几项：两个标签页改同一条需求时，后保存的不会把先保存的改回去；
        // 换了项目一定要带材料文件夹（后端要求同批重选），没换就只在文件夹动过时才带；
        // 来源动过才传（换会、换一句、清空，R04-5），没动不碰合并进来的原话
        finish({
          kind: "edited",
          requirement: await apiClient.updateRequirement(requirementId, {
            ...(changed.title ? { title: cleanedTitle } : {}),
            ...(changed.summary ? { summary: summary.trim() } : {}),
            ...(changed.project ? { project_id: projectId } : {}),
            ...(changed.priority ? { priority } : {}),
            ...(changed.status ? { status } : {}),
            ...(changed.project || (canPickFolders && changed.folders) ? { folder_paths: folderPaths } : {}),
            ...(changed.source ? { source: sourceInput } : {}),
            ...(closeOpenTasks !== undefined ? { close_open_tasks: closeOpenTasks } : {}),
          }),
        });
      } else {
        finish({
          kind: "created",
          requirement: await apiClient.createRequirement({
            title: cleanedTitle,
            summary: summary.trim(),
            project_id: projectId,
            priority,
            folder_paths: canPickFolders ? folderPaths : [],
            ...(sourceInput ? { source: sourceInput } : {}),
          }),
        });
      }
    } catch (err) {
      const data = err instanceof ApiError ? (err.data as TitleConflict | null) : null;
      if (err instanceof ApiError && err.status === 409 && data?.existing) {
        // 保存时才发现撞名（边填边查没查到，或别处刚建了同名的）：同样标在需求名底下
        conflictEpoch.current += 1;
        setFound({ title: cleanedTitle, projectId, existing: data.existing });
      } else if (claiming && err instanceof ApiError && err.status === 404) {
        setError("这条候选已经不在了（纪要重新抽取时换掉了），回需求池看看新的候选");
      } else {
        setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
        // 候选在别处已经认领、合并或丢掉：重读一遍，页上显示它的状态，表单不能再提交
        if (claiming && candidateId && err instanceof ApiError && err.status === 409) {
          void apiClient
            .requirementCandidate(candidateId)
            .then(setCandidate)
            .catch(() => undefined);
        }
      }
      setSaving(false);
    }
  };

  const mergeIntoExisting = async () => {
    if (!candidateId || !conflict) return;
    setSaving(true);
    setError("");
    try {
      finish({ kind: "merged", requirement: await apiClient.mergeCandidate(candidateId, conflict.id, projectId) });
    } catch (err) {
      setError(err instanceof Error ? err.message : "合并失败，请稍后重试");
      setSaving(false);
    }
  };

  const candidateSource = candidate?.source ?? null;
  const extraSources = (candidate?.sources ?? []).filter((item) => item.id !== candidateSource?.id);
  const mergedSources = (requirement?.sources ?? []).filter((item) => item.kind === "merged");
  const heading = claiming ? "认领候选" : editing ? "修改需求" : "新增需求";
  const lead = claiming
    ? "AI 从会议纪要里抽出来的，改好再认领，认领后挂上「进行中」的墙。"
    : editing
      ? "改完保存，墙上的海报会跟着变。"
      : prefill
        ? "来源已从逐字稿带入，补上需求名就能建。"
        : "从会上听到的一句话开始，或者直接写下要做的事。";

  return (
    <section className="page-content form-page">
      <header className="form-page__head">
        <p className="form-page__crumb">
          <span>
            {prefill
              ? `${prefill.from ?? "录音档案"} / ${prefill.source.meeting_title} /`
              : editing && requirement
                ? `需求池 / ${requirement.title} /`
                : "需求池 /"}
          </span>{" "}
          {heading}
        </p>
        <div className="form-page__title">
          <h1>{heading}</h1>
          <p>{lead}</p>
        </div>
      </header>

      {loadError && (
        <p className="form-page__alert" role="alert">
          {loadError}
        </p>
      )}
      {handled && candidate && (
        <p className="form-page__alert" role="alert">
          这条候选已经{candidate.status === "claimed" ? "认领" : candidate.status === "merged" ? "合并" : "丢掉"}了。
          {candidate.requirement_id && onOpenRequirement && (
            <button onClick={() => onOpenRequirement(candidate.requirement_id!)} type="button">
              打开那条需求
            </button>
          )}
        </p>
      )}

      <div className="form-page__body">
        <form
          className="form-card"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label className="form-field">
            <span className="form-field__label">
              需求名
              {claiming && <em className="form-field__ai">AI 预填</em>}
            </span>
            <input
              aria-describedby={conflict ? conflictId : undefined}
              aria-invalid={conflict ? true : undefined}
              className={conflict ? "is-invalid" : undefined}
              disabled={saving || !ready || handled}
              maxLength={200}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="给这条需求起个名字"
              ref={titleRef}
              value={title}
            />
          </label>

          {conflict && (
            <div className="form-conflict" id={conflictId} ref={conflictRef} role="alert">
              <p>
                <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16" width="14">
                  <circle cx="8" cy="8" r="6.2" />
                  <path d="M8 4.8v3.8M8 11.2v.1" />
                </svg>
                {project?.name ?? "这个项目"}里已经有一条叫「{conflict.title}」的需求
              </p>
              {claiming && (
                <div className="form-conflict__actions">
                  <button onClick={() => titleRef.current?.focus()} type="button">
                    改个名字
                  </button>
                  {MERGEABLE.has(conflict.status) ? (
                    <button className="is-primary" disabled={saving} onClick={() => void mergeIntoExisting()} type="button">
                      改为合并到这条需求
                    </button>
                  ) : (
                    <span>它已完成，不能合并，请改个名字</span>
                  )}
                </div>
              )}
            </div>
          )}

          <label className="form-field">
            <span className="form-field__label">
              说明
              {claiming ? <em className="form-field__ai">AI 预填</em> : <em className="form-field__optional">选填</em>}
            </span>
            <span className="form-field__textarea">
              <textarea
                disabled={saving || !ready || handled}
                onChange={(event) => setSummary(event.target.value)}
                placeholder="一两句话说清要做什么，40～70 字"
                rows={3}
                value={summary}
              />
              <span className={`form-field__counter ${chars > SUMMARY_MAX ? "is-over" : ""}`}>
                {chars} / {SUMMARY_MAX}
              </span>
            </span>
          </label>

          <div className="form-field__row">
            <div className="form-field">
              <span className="form-field__label form-field__label--split" id={projectLabelId}>
                所属项目
                {prefill && <small>随会议归属，可改</small>}
              </span>
              <ProjectSelect
                disabled={saving || !ready || handled}
                labelledBy={projectLabelId}
                onChange={changeProject}
                projects={projects}
                value={projectId}
              />
            </div>
            <div className="form-field">
              <span className="form-field__label">优先级</span>
              <div aria-label="优先级" className="form-segmented" role="group">
                {REQUIREMENT_PRIORITIES.map((value) => (
                  <button
                    aria-pressed={priority === value}
                    disabled={saving || !ready || handled}
                    key={value}
                    onClick={() => setPriority(value)}
                    type="button"
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
            {editing && (
              <div className="form-field">
                <span className="form-field__label">状态</span>
                <div aria-label="状态" className="form-segmented" role="group">
                  {EDIT_STATUSES.map((value) => (
                    <button
                      aria-pressed={status === value}
                      disabled={saving || !ready}
                      key={value}
                      onClick={() => setStatus(value)}
                      type="button"
                    >
                      {REQUIREMENT_STATUS_LABELS[value]}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          {claiming && (
            <div className="form-field">
              <span className="form-field__label form-field__label--split">
                来源
                <small>出自会议纪要，不可改</small>
              </span>
              {candidateSource ? (
                <SourcePanel source={candidateSource}>
                  {extraSources.length > 0 && (
                    <p className="form-source__more">另有 {extraSources.length} 句原话出自其他会议</p>
                  )}
                </SourcePanel>
              ) : (
                <p className="form-source form-source--empty">
                  {loadError
                    ? "读不到这条候选"
                    : !candidate
                      ? "正在读取…"
                      : handled
                        ? "来源已经跟着候选挂到需求上了"
                        : "这条候选没有来源"}
                </p>
              )}
            </div>
          )}

          {!claiming && (
            <div className="form-field">
              <span className="form-field__label form-field__label--split">
                <span>
                  来源 <em className="form-field__optional">选填</em>
                </span>
                <small>{prefill ? "来自逐字稿选句" : "选定的会同时加进关联会议"}</small>
              </span>
              {source ? (
                <SourcePanel source={source}>
                  <div className="form-source__actions">
                    <button disabled={saving || !ready} onClick={() => setPickingSource(true)} type="button">
                      换一个
                    </button>
                    <button disabled={saving || !ready} onClick={() => setSource(null)} type="button">
                      清空来源
                    </button>
                  </div>
                </SourcePanel>
              ) : (
                <button
                  className="form-folders__pick"
                  disabled={saving || !ready}
                  onClick={() => setPickingSource(true)}
                  type="button"
                >
                  ＋ 选来源会议，再挑会上原话
                </button>
              )}
              {mergedSources.length > 0 && (
                <p className="form-source__more">{mergedNote(mergedSources, source?.meeting_id ?? null)}</p>
              )}
            </div>
          )}

          {canPickFolders && (
            <div className="form-field">
              <span className="form-field__label">
                材料文件夹 <em className="form-field__optional">选填</em>
              </span>
              {folders.length > 0 && (
                <ul className="form-folders">
                  {folders.map((folder) => (
                    <li key={folder.path}>
                      <strong>{folder.name}</strong>
                      <span>{folder.path}</span>
                      <button
                        disabled={saving}
                        onClick={() => setFolders((current) => current.filter((item) => item.path !== folder.path))}
                        type="button"
                      >
                        移除
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button
                className="form-folders__pick"
                disabled={saving || !project || handled}
                onClick={() => setPickerOpen(true)}
                type="button"
              >
                ＋ {folders.length > 0 ? "再选一个文件夹" : "选择文件夹"}
              </button>
            </div>
          )}
        </form>

        <aside className="form-preview">
          <p className="form-preview__label">墙上预览</p>
          <PosterCard canWrite={false} item={preview} preview />
        </aside>
      </div>

      <footer className="form-actions">
        {/* 失败原因写在固定的操作栏里：表单长、窗口矮的时候也看得见 */}
        {error && (
          <p className="form-actions__error" role="alert">
            {error}
          </p>
        )}
        <button className="form-actions__cancel" disabled={saving} onClick={cancel} type="button">
          取消
        </button>
        <button className="form-actions__submit" disabled={!canSave} onClick={submit} type="button">
          {saving ? "保存中…" : claiming ? "认领" : editing ? "保存" : "创建"}
        </button>
      </footer>

      {askingTasks && (
        <CloseTasksDialog
          onCancel={() => setAskingTasks(null)}
          onDecide={(closeOpenTasks) => {
            setAskingTasks(null);
            void save(closeOpenTasks);
          }}
          tasks={askingTasks}
        />
      )}

      {pickingSource && (
        <SourcePickerDialog
          apiClient={apiClient}
          onClose={() => setPickingSource(false)}
          onPicked={(picked) => {
            setSource(picked);
            setPickingSource(false);
          }}
          projectId={projectId || null}
          projectName={project?.name ?? null}
        />
      )}

      {pickerOpen && project && (
        <MaterialFolderPickerModal
          apiClient={apiClient}
          onCancel={() => setPickerOpen(false)}
          onConfirm={(picked) => {
            setFolders(picked);
            setPickerOpen(false);
          }}
          onOpenProject={(pid) => {
            setPickerOpen(false);
            onOpenProject?.(pid);
          }}
          projectId={project.id}
          projectName={project.name}
          selectedFolders={folders}
        />
      )}
    </section>
  );
}
