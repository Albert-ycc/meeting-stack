import { useEffect, useRef, useState, type ReactNode } from "react";

import { similarProjectFrom, type ApiClient } from "../api";
import { formatTime } from "../format";
import type {
  NameAsRequirementResult,
  NameCandidate,
  NameCandidatesPayload,
  NameDecisionResult,
  NameHint,
  Project,
  SimilarProjectSuggestion,
} from "../types";
import { pollWhileChecking } from "./checkingPoll";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import type { NoticeAction, NoticeTone } from "./Notice";
import { PROJECT_COLORS } from "./ProjectFormModal";
import { PROJECT_PARENT_PICKER } from "./ProjectParentRow";
import { SimilarProjectQuestion } from "./SimilarProjectQuestion";
import "./NewNamePrompt.css";

/** 文件夹缓存还没好时［建成项目］最多等多久；过了就按手上的信息照常建 */
export const CHECKING_WAIT_MS = 15_000;

/** 结果提示：undoUntil 是改归属的撤销（归属条原有的）；actions 是这条提示自带的［撤销］［打开需求］ */
export type PromptNotice = (message: string, undoUntil?: string, tone?: NoticeTone, actions?: NoticeAction[]) => void;

export interface NewNamePromptProps {
  apiClient: ApiClient;
  meetingId: string;
  hint: NameHint;
  projects: Project[];
  /** 手机上只读：不显示会动磁盘和建东西的按钮 */
  readOnly?: boolean;
  /** 归属条锁住（右侧有未保存的归属修改）或正在做别的操作 */
  disabled?: boolean;
  /** 锁住的原因，写在标题后面 */
  lockNote?: ReactNode;
  /** 嵌在别的归属条里（自动归属、刚改过、待你选）：自带一圈底色 */
  nested?: boolean;
  /** 待你选里展开的：给［收起］ */
  onCollapse?: () => void;
  /** 在这场会的录音里跳到某处 */
  onSeek?: (ms: number) => void;
  /** 播放同名的另一场会；不给就不显示那几场的 ▶ */
  onPlayMeeting?: (meetingId: string, ms: number) => void;
  /** ［选已有项目］的下拉（归属条的，只改这场会） */
  pickExisting?: ReactNode;
  /** ［用它］：这场会和同名的另几场会一起改到已有项目，归属条自己出提示 */
  onAssign: (projectId: string, otherMeetingIds: string[]) => Promise<void> | void;
  /** 建成、撤销以后读回这场会，更新归属条 */
  onReload: () => Promise<void>;
  /** ［不是新项目］［不算新需求］以后就地清掉提示 */
  onDismissed: () => void;
  onNotice: PromptNotice;
  onProjectsChanged?: () => Promise<void> | void;
  onOpenRequirement?: (requirementId: string) => void;
}

type LoadState = "loading" | "ready" | "error" | "unavailable";

/** 建成项目时文件夹怎么处理（名字改了以后前端自己算） */
type ProjectFolderPlan =
  | { mode: "mount"; path: string }
  | { mode: "create"; parent: string; target: string; offline: boolean; suggested: boolean }
  | { mode: "blocked"; parent: string; reason: string }
  | { mode: "none" };

// 和后端 sanitize_folder_name 一样：exFAT 不认的字符换成 -，去掉结尾的点和空格
// eslint-disable-next-line no-control-regex
const EXFAT_ILLEGAL = /[\\/:*?"<>|\x00-\x1f]/g;

function folderName(name: string) {
  return name.trim().replace(EXFAT_ILLEGAL, "-").replace(/[. ]+$/, "");
}

function joinPath(parent: string, name: string) {
  return `${parent.replace(/\/+$/, "")}/${name}`;
}

/** 「云图AI/数据看板/」：需求文件夹只能是根目录的直接子文件夹，写根目录名和它 */
function shortFolder(path: string) {
  const parts = path.split("/").filter(Boolean);
  return `${parts.slice(-2).join("/")}/`;
}

/** 候选小标签上的证据；会上说过几次放最后，后面跟 ▶ */
function evidenceLabels(candidate: NameCandidate): string[] {
  const labels: string[] = [];
  if (candidate.folder_path) labels.push("磁盘上有这个文件夹");
  if (candidate.ai) labels.push("AI 起的名字");
  if (candidate.similar_folder_path && !candidate.folder_path) labels.push("名字相近的文件夹");
  if (candidate.spoken) labels.push(`会上说过 ${candidate.spoken.count} 次`);
  return labels;
}

/** 会上说到的地方：第一次在前，后面是其余的锚点 */
function spokenMarks(candidate: NameCandidate): number[] {
  if (!candidate.spoken) return [];
  const { first_ms: first, anchors_ms: anchors } = candidate.spoken;
  return [first, ...anchors.filter((ms) => ms !== first)];
}

/** 提示里的［撤销］只能点一次：请求发出去后再点不再重复发 */
function once(action: () => Promise<void>): () => void {
  let used = false;
  return () => {
    if (used) return;
    used = true;
    void action();
  };
}

function errorText(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

/**
 * 「像是新项目 / 新需求」提示（2b）。会议页的归属条和关系图的会议面板都用它。
 * 点开时取候选名（只查库和文件夹缓存）；名字可以改，文件夹怎么处理跟着名字算。
 */
export function NewNamePrompt({
  apiClient,
  meetingId,
  hint,
  projects,
  readOnly = false,
  disabled = false,
  lockNote,
  nested = false,
  onCollapse,
  onSeek,
  onPlayMeeting,
  pickExisting,
  onAssign,
  onReload,
  onDismissed,
  onNotice,
  onProjectsChanged,
  onOpenRequirement,
}: NewNamePromptProps) {
  const canLoad = typeof apiClient.nameCandidates === "function";
  const [payload, setPayload] = useState<NameCandidatesPayload | null>(null);
  const [loadState, setLoadState] = useState<LoadState>(canLoad ? "loading" : "unavailable");
  const [loadError, setLoadError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  // 等了 15 秒缓存还没好：不再等，［建成项目］照常能点
  const [waitedOut, setWaitedOut] = useState(false);
  const waitedOutRef = useRef(false);
  const [name, setName] = useState(hint.name);
  // 名字你改过（点了候选或手输）：重取候选时不再覆盖
  const touchedRef = useRef(false);
  // 点了「改成挂上这个文件夹」的相近文件夹
  const [mountSimilar, setMountSimilar] = useState<string | null>(null);
  const [showMeetings, setShowMeetings] = useState(false);
  // ［建成需求］建在哪个项目（没归项目时）；null 表示还没动过，用排第一的推荐项目
  const [requirementProject, setRequirementProject] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [suggestion, setSuggestion] = useState<SimilarProjectSuggestion | null>(null);
  // 每个会上叫法的 ▶ 已经放到第几处了：再点跳到下一处
  const [spokenStep, setSpokenStep] = useState<Record<string, number>>({});
  // 设项目总文件夹：取径器、请求中、失败原因
  const [pickerOpen, setPickerOpen] = useState(false);
  const [parentBusy, setParentBusy] = useState(false);
  const [pickerError, setPickerError] = useState("");
  const [parentError, setParentError] = useState("");

  // 取候选；文件夹缓存还没好（folders_state: checking）时每 2 秒再取，最多等 15 秒。
  useEffect(() => {
    if (!canLoad) return;
    waitedOutRef.current = false;
    setWaitedOut(false);
    setLoadError("");
    const stop = pollWhileChecking(
      () => apiClient.nameCandidates(meetingId),
      // 等满 15 秒就不再追着问；已经发出去的那次照常用
      (value) => value.folders_state === "checking" && !waitedOutRef.current,
      (value) => {
        setPayload(value);
        setLoadState("ready");
        if (!touchedRef.current && value.candidates[0]) setName(value.candidates[0].name);
      },
      {
        onError: (error) => {
          setLoadError(errorText(error, "读取失败"));
          setLoadState("error");
        },
      },
    );
    const timer = window.setTimeout(() => {
      waitedOutRef.current = true;
      setWaitedOut(true);
    }, CHECKING_WAIT_MS);
    return () => {
      stop();
      window.clearTimeout(timer);
    };
  }, [apiClient, canLoad, meetingId, reloadKey]);

  const ready = loadState === "ready" || loadState === "unavailable";
  const candidates = payload?.candidates ?? [];
  const meetings = payload?.meetings ?? [];
  const others = meetings.filter((meeting) => meeting.id !== meetingId);
  const meetingIds = [meetingId, ...others.map((meeting) => meeting.id)];
  const trimmed = name.trim();
  const picked = candidates.find((candidate) => candidate.name === trimmed) ?? null;
  const similarMounted =
    mountSimilar && picked?.similar_folder_path === mountSimilar ? mountSimilar : null;
  const checking = payload?.folders_state === "checking" && !waitedOut;
  const defaultAction =
    payload?.default_action ?? (hint.kind === "requirement" ? "create_requirement" : "create_project");
  const projectName = payload?.project?.name ?? (hint.kind === "requirement" ? hint.project_name : "");
  const title = hint.kind === "requirement" ? `像是『${projectName}』里的一个新需求` : "像是一个新项目";
  const locked = disabled || busy;
  const canSetProjectParent = typeof apiClient.setProjectParent === "function";

  // 项目提示时［建成需求］的项目下拉：接口排好的（推荐的在前）；取不到候选时列全部项目
  const requirementProjects = payload
    ? payload.requirement_projects
    : projects.map((project) => ({ id: project.id, name: project.name, color: project.color, suggested: false }));
  const chosenProjectId =
    requirementProject ?? requirementProjects.find((project) => project.suggested)?.id ?? "";

  // 需求提示：选中的名字是根目录下的同名子文件夹（或点了「改成挂上这个文件夹」）时一起挂成需求文件夹
  const requirementFolder =
    hint.kind === "requirement" ? (picked?.folder_path ?? similarMounted) : null;

  const projectFolderPlan = (): ProjectFolderPlan | null => {
    if (!payload) return null;
    // 需求提示的文件夹候选是项目根目录下的子文件夹，不拿来挂成新项目的文件夹
    if (hint.kind === "project" && picked?.folder_path) return { mode: "mount", path: picked.folder_path };
    if (hint.kind === "project" && similarMounted) return { mode: "mount", path: similarMounted };
    const parent = payload.create_parent;
    if (!parent) return { mode: "none" };
    if (payload.create_parent_state === "missing") {
      return { mode: "blocked", parent, reason: `找不到 ${parent}，这次先不建文件夹` };
    }
    if (payload.create_parent_state === "unreadable") {
      return { mode: "blocked", parent, reason: `读不了 ${parent}，这次先不建文件夹` };
    }
    return {
      mode: "create",
      parent,
      target: joinPath(parent, folderName(trimmed || hint.name)),
      offline: payload.create_parent_state === "volume_offline",
      suggested: payload.create_parent_source === "suggested",
    };
  };
  const plan = projectFolderPlan();

  const run = async (work: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await work();
    } catch (error) {
      onNotice(errorText(error, "操作失败"), undefined, "error");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const choose = (candidate: NameCandidate) => {
    touchedRef.current = true;
    setName(candidate.name);
    setMountSimilar(null);
    setSuggestion(null);
  };

  const playSpoken = (candidate: NameCandidate) => {
    const marks = spokenMarks(candidate);
    const step = spokenStep[candidate.name] ?? 0;
    onSeek?.(marks[step % marks.length]);
    setSpokenStep((current) => ({ ...current, [candidate.name]: step + 1 }));
  };

  // —— 撤销：提示可能已经收起（会的归属变了），这里只靠上层传进来的回调 ——

  const undoSpoken = (projectId: string, projectTitle: string, spoken: { name: string; event_id: number }) =>
    once(async () => {
      try {
        await apiClient.undoSpokenAlsoName(projectId, spoken.event_id);
        onNotice(`已撤销：以后会上说『${spoken.name}』不再认成『${projectTitle}』`);
        await onProjectsChanged?.();
      } catch (error) {
        onNotice(errorText(error, "撤销失败"), undefined, "error");
      }
    });

  const undoRequirement = (result: NameAsRequirementResult) =>
    once(async () => {
      try {
        const undone = await apiClient.undoNameAsRequirement(meetingId);
        const title = result.requirement_title;
        const what = undone.requirement_deleted
          ? `需求『${title}』删掉了`
          : result.existing
            ? `会不再关联需求『${title}』`
            : `会不再关联需求『${title}』（需求里后来又有了别的东西，留着）`;
        const restored = undone.meetings_restored ? `，${undone.meetings_restored} 场会改回了原来的归属` : "";
        onNotice(`已撤销：${what}${restored}`);
        await onReload();
        await onProjectsChanged?.();
      } catch (error) {
        onNotice(errorText(error, "撤销失败"), undefined, "error");
      }
    });

  const undoDecision = (result: NameDecisionResult) =>
    once(async () => {
      try {
        await apiClient.undoNameDecision(result.event_id);
        onNotice(`已撤销：『${result.name}』还会当成${result.kind === "requirement" ? "新需求" : "新项目"}提示`);
        await onReload();
      } catch (error) {
        onNotice(errorText(error, "撤销失败"), undefined, "error");
      }
    });

  // —— 主按钮 ——

  const buildProject = (force = false) =>
    run(async () => {
      const folder =
        plan?.mode === "mount"
          ? { mode: "mount" as const, path: plan.path }
          : plan?.mode === "create"
            ? { mode: "create" as const, path: plan.parent, name: trimmed }
            : undefined;
      let created: Project;
      try {
        created = await apiClient.createProjectWith({
          name: trimmed,
          color: PROJECT_COLORS[projects.length % PROJECT_COLORS.length],
          source_name: hint.name,
          meeting_ids: meetingIds,
          ...(folder ? { folder } : {}),
          ...(force ? { force: true } : {}),
        });
      } catch (error) {
        const similar = similarProjectFrom(error);
        if (!similar) throw error;
        setSuggestion(similar);
        return;
      }
      setSuggestion(null);
      const assigned = created.meetings_assigned ?? meetingIds.length;
      let message = `已建成项目『${created.name}』，${assigned} 场会归进去了`;
      if (created.folder_pending) {
        message += `；资料盘没连接，插上后自动建 ${created.folder_pending.path}`;
      } else if (folder?.mode === "mount") {
        message += `，挂上了 ${folder.path}`;
      } else if (folder?.mode === "create") {
        const path = created.material_roots?.[0]?.path ?? (plan?.mode === "create" ? plan.target : folder.path);
        message += `，新建并挂上了 ${path}`;
      }
      const spoken = created.spoken_added;
      if (spoken) {
        message += `。以后会上说『${spoken.name}』也会认成『${created.name}』（记进了它的也叫）`;
      }
      onNotice(
        message,
        undefined,
        "success",
        spoken ? [{ label: "撤销", onClick: undoSpoken(created.id, created.name, spoken) }] : undefined,
      );
      await onReload();
      await onProjectsChanged?.();
    });

  const buildRequirement = () =>
    run(async () => {
      const projectId = hint.kind === "project" ? chosenProjectId : undefined;
      if (hint.kind === "project" && !projectId) return;
      const result = await apiClient.nameAsRequirement(meetingId, {
        title: trimmed,
        ...(projectId ? { project_id: projectId } : {}),
        ...(requirementFolder ? { folder_path: requirementFolder } : {}),
      });
      const head = result.existing
        ? `『${result.project_name}』里已有需求『${result.requirement_title}』，${result.meetings_linked} 场会已关联过去`
        : `已在『${result.project_name}』建好需求『${result.requirement_title}』（${result.priority}），${result.meetings_linked} 场会已关联`;
      const actions: NoticeAction[] = [];
      if (onOpenRequirement) {
        actions.push({ label: "打开需求", onClick: () => onOpenRequirement(result.requirement_id) });
      }
      actions.push({ label: "撤销", onClick: undoRequirement(result) });
      onNotice(
        result.folder_error ? `${head}。需求文件夹没挂上：${result.folder_error}` : head,
        undefined,
        result.folder_error ? "warning" : "success",
        actions,
      );
      await onReload();
      await onProjectsChanged?.();
    });

  const ignore = () =>
    run(async () => {
      const result = await apiClient.ignoreProjectName(
        hint.name,
        hint.kind === "requirement"
          ? { kind: "requirement", project_id: hint.project_id, meeting_id: meetingId }
          : { kind: "project", meeting_id: meetingId },
      );
      onDismissed();
      onNotice(
        `以后不再把『${result.name}』当成${hint.kind === "requirement" ? "新需求" : "新项目"}提示`,
        undefined,
        "success",
        [{ label: "撤销", onClick: undoDecision(result) }],
      );
    });

  // —— 设项目总文件夹（推荐位置设成总文件夹，或就地选一个），设好后重取候选 ——

  const saveProjectParent = async (path: string, fromPicker: boolean) => {
    setParentBusy(true);
    setPickerError("");
    setParentError("");
    try {
      await apiClient.setProjectParent(path);
      if (fromPicker) setPickerOpen(false);
      setReloadKey((key) => key + 1);
    } catch (error) {
      const message = errorText(error, "没设成，请稍后重试");
      if (fromPicker) setPickerError(message);
      else setParentError(message);
    } finally {
      setParentBusy(false);
    }
  };

  const openPicker = () => {
    setPickerError("");
    setPickerOpen(true);
  };

  // —— 画 ——

  const head = (
    <div className="name-prompt__head">
      <strong>{title}</strong>
      {others.length > 0 && (
        <span className="name-prompt__others">
          （另有 {others.length} 场会也像是它
          <button
            aria-expanded={showMeetings}
            className="text-button name-prompt__look"
            onClick={() => setShowMeetings((value) => !value)}
            type="button"
          >
            看看
          </button>
          ）
        </span>
      )}
      {onCollapse && (
        <button className="text-button name-prompt__collapse" onClick={onCollapse} type="button">
          收起
        </button>
      )}
      {lockNote}
    </div>
  );

  const meetingList = showMeetings && others.length > 0 && (
    <ul aria-label="同名的会" className="name-prompt__meetings">
      {others.map((meeting) => (
        <li key={meeting.id}>
          <span className="name-prompt__meeting-title">{meeting.title}</span>
          <span className="name-prompt__muted">{meeting.date.slice(0, 10)}</span>
          {meeting.said_ms !== null && onPlayMeeting && (
            <button
              aria-label={`从 ${formatTime(meeting.said_ms)} 播放「${meeting.title}」`}
              className="name-prompt__play"
              onClick={() => onPlayMeeting(meeting.id, meeting.said_ms as number)}
              type="button"
            >
              ▶
            </button>
          )}
        </li>
      ))}
    </ul>
  );

  if (readOnly) {
    return (
      <div aria-label={title} className={`name-prompt${nested ? " name-prompt--nested" : ""}`} role="group">
        {head}
        {meetingList}
        <p className="name-prompt__readonly">
          <strong>『{trimmed || hint.name}』</strong>
          <span className="name-prompt__muted">在电脑上打开可以建项目并建文件夹</span>
        </p>
        {hint.kind === "project" && pickExisting && <div className="name-prompt__secondary">{pickExisting}</div>}
      </div>
    );
  }

  const chips = candidates.length > 0 && (
    <div aria-label="候选名" className="name-prompt__chips" role="group">
      {candidates.map((candidate) => {
        const selected = candidate.name === trimmed;
        const why = evidenceLabels(candidate).join(" · ");
        const marks = spokenMarks(candidate);
        const next = marks.length ? marks[(spokenStep[candidate.name] ?? 0) % marks.length] : 0;
        const canMountSimilar =
          selected && candidate.similar_folder_path && !candidate.folder_path && mountSimilar !== candidate.similar_folder_path;
        return (
          <span className="name-prompt__chip-wrap" key={candidate.name}>
            <span className={`name-prompt__chip${selected ? " is-selected" : ""}`}>
              <button
                aria-label={why ? `${candidate.name} · ${why}` : candidate.name}
                aria-pressed={selected}
                className="name-prompt__chip-fill"
                disabled={locked}
                onClick={() => choose(candidate)}
                type="button"
              >
                {candidate.name}
                {why && <span className="name-prompt__chip-why"> · {why}</span>}
              </button>
              {candidate.spoken && onSeek && (
                <button
                  aria-label={`从 ${formatTime(next)} 播放「${candidate.name}」`}
                  className="name-prompt__play"
                  onClick={() => playSpoken(candidate)}
                  title={marks.length > 1 ? `说到的地方：${marks.map((ms) => formatTime(ms)).join("、")}，再点跳到下一处` : undefined}
                  type="button"
                >
                  ▶
                </button>
              )}
            </span>
            {canMountSimilar && (
              <button
                className="text-button name-prompt__link"
                disabled={locked}
                onClick={() => setMountSimilar(candidate.similar_folder_path)}
                type="button"
              >
                改成挂上这个文件夹
              </button>
            )}
          </span>
        );
      })}
    </div>
  );

  // ［建成项目］下面那行小字：文件夹会怎么处理，都写完整路径
  const projectFolderNote = (): ReactNode => {
    if (checking) {
      return (
        <p className="name-prompt__note" role="status">
          正在看磁盘上有没有同名文件夹…
        </p>
      );
    }
    if (!plan) return null;
    const pickParent = canSetProjectParent && (
      <button className="text-button name-prompt__link" disabled={locked || parentBusy} onClick={openPicker} type="button">
        选项目总文件夹…
      </button>
    );
    switch (plan.mode) {
      case "mount":
        return <p className="name-prompt__note">会挂上 {plan.path}</p>;
      case "create":
        return (
          <>
            <p className="name-prompt__note">
              {plan.offline ? `资料盘没连接：项目照常建，插上后自动建 ${plan.target}` : `会新建 ${plan.target}`}
            </p>
            {plan.suggested && (
              <p className="name-prompt__note name-prompt__muted">
                这是你多数项目文件夹所在的位置
                {canSetProjectParent && (
                  <button
                    className="text-button name-prompt__link"
                    disabled={locked || parentBusy}
                    onClick={() => void saveProjectParent(plan.parent, false)}
                    type="button"
                  >
                    设为项目总文件夹
                  </button>
                )}
              </p>
            )}
          </>
        );
      case "blocked":
        return (
          <p className="name-prompt__note">
            {plan.reason}
            {pickParent}
          </p>
        );
      default:
        return (
          <p className="name-prompt__note">
            还没有可参照的项目文件夹，这次先不建文件夹
            {pickParent}
          </p>
        );
    }
  };

  const mainClass = (solid: boolean) => (solid ? "name-prompt__main name-prompt__main--solid" : "name-prompt__main");

  const projectBlock = (
    <div className="name-prompt__action" key="project">
      <button
        className={mainClass(defaultAction === "create_project")}
        disabled={locked || !ready || checking || !trimmed}
        onClick={() => void buildProject()}
        type="button"
      >
        建成项目
      </button>
      <div className="name-prompt__notes">
        {projectFolderNote()}
        {parentError && (
          <p className="name-prompt__note name-prompt__error" role="alert">
            {parentError}
          </p>
        )}
      </div>
    </div>
  );

  const showRequirement = hint.kind === "requirement" || !ready || requirementProjects.length > 0;
  const requirementBlock = showRequirement && (
    <div className="name-prompt__action" key="requirement">
      <button
        className={mainClass(defaultAction === "create_requirement")}
        disabled={locked || !ready || !trimmed || (hint.kind === "project" && !chosenProjectId)}
        onClick={() => void buildRequirement()}
        type="button"
      >
        建成需求
      </button>
      <div className="name-prompt__notes">
        {hint.kind === "project" ? (
          <select
            aria-label="建在哪个项目"
            className="name-prompt__pick"
            disabled={locked || !ready}
            onChange={(event) => setRequirementProject(event.target.value)}
            value={chosenProjectId}
          >
            <option value="">建在哪个项目…</option>
            {requirementProjects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        ) : (
          requirementFolder && <p className="name-prompt__note">会把 {shortFolder(requirementFolder)} 挂成需求文件夹</p>
        )}
      </div>
    </div>
  );

  return (
    <div aria-label={title} className={`name-prompt${nested ? " name-prompt--nested" : ""}`} role="group">
      {head}
      {meetingList}
      <input
        aria-label="名字"
        className="name-prompt__input"
        disabled={locked}
        onChange={(event) => {
          touchedRef.current = true;
          setName(event.target.value);
          setSuggestion(null);
        }}
        value={name}
      />
      {chips}
      {loadState === "error" && (
        <p className="name-prompt__note name-prompt__error" role="alert">
          读不到候选名和文件夹：{loadError}
          <button className="text-button name-prompt__link" onClick={() => setReloadKey((key) => key + 1)} type="button">
            重试
          </button>
        </p>
      )}
      <div className="name-prompt__actions">
        {defaultAction === "create_requirement" ? [requirementBlock, projectBlock] : [projectBlock, requirementBlock]}
      </div>
      {suggestion && (
        <SimilarProjectQuestion
          disabled={locked}
          onForce={() => void buildProject(true)}
          onUse={(projectId) => {
            setSuggestion(null);
            void onAssign(projectId, others.map((meeting) => meeting.id));
          }}
          suggestion={suggestion}
        />
      )}
      <div className="name-prompt__secondary">
        {hint.kind === "requirement" ? (
          <button className="text-button" disabled={locked} onClick={() => void ignore()} type="button">
            不算新需求
          </button>
        ) : (
          <>
            <button className="text-button" disabled={locked} onClick={() => void ignore()} type="button">
              不是新项目
            </button>
            {pickExisting}
          </>
        )}
      </div>
      {pickerOpen && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={parentBusy}
          error={pickerError}
          onClose={() => setPickerOpen(false)}
          onConfirm={(path) => void saveProjectParent(path, true)}
          {...PROJECT_PARENT_PICKER}
        />
      )}
    </div>
  );
}
