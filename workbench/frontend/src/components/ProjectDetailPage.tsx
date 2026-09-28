import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import type { ApiClient } from "../api";
import { formatDurationText, formatMonthDay } from "../format";
import type {
  MaterialCoverage,
  MaterialCoverageRoot,
  MaterialIndexRoot,
  MaterialIndexStatus,
  MaterialRoot,
  MaterialUnreadableItem,
  MaterialRootRepoint,
  PreviewTarget,
  Project,
  ProjectBoard,
  ProjectMeetingRow,
  ProjectSubfoldersPayload,
  RequirementRef,
  RequirementStatus,
  RequirementSummary,
  RequirementsPayload,
  UnreadableReason,
} from "../types";
import { AsyncState } from "./AsyncState";
import { nestedHints } from "./ClaimFoldersDialog";
import { FolderIcon } from "./FolderIcon";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import { ProjectCardsRow } from "./ProjectCardsRow";
import { Pagination } from "./Pagination";
import { ProjectFormModal } from "./ProjectFormModal";
import { ProjectRecognitionCard } from "./ProjectRecognitionCard";
import { RootRenameQuestion, movedNote } from "./RootRenameQuestion";
import { PriorityBadge, RequirementStatusBadge } from "./RequirementBadges";
// FE-1 负责的需求弹窗；写这个文件时它可能还不存在，tsc 报「模块不存在」属于预期（简报第 5 节已钉死 props）。
import { RequirementModal } from "./RequirementModal";
import { useToast } from "./Toast";
import "./ProjectDetailPage.css";
import { useConfirm } from "./ConfirmDialog";
import { copyText } from "../clipboard";
import { NoticeBanner, useNotice } from "./Notice";
import { ProjectGlossary } from "./ProjectGlossary";
import { usePersistentState } from "../viewState";
import { ProjectTimeline } from "./decisions/ProjectTimeline";
import { ProjectAsk } from "./ask/ProjectAsk";

interface ProjectDetailPageProps {
  apiClient: ApiClient;
  projectId: string;
  canWrite: boolean;
  onBack: () => void;
  onOpenGlossary: (projectId: string) => void;
  /** 4c：时间线里点决议从那里放、「还有 N 条」打开纪要，所以放宽成 (meetingId, seekMs?, tab?) */
  onOpenMeeting: (meetingId: string, seekMs?: number, tab?: "transcript" | "minutes") => void;
  onOpenTask: (taskId: string) => void;
  /** 读不了的列表里点［预览］打开 App 根部的材料预览抽屉（3e） */
  onOpenPreview?: (fileId: number) => void;
  reloadKey?: number;
  onProjectUpdated?: () => void;
  /** 新建需求弹窗要拿全量项目列表填「所属项目」下拉 */
  projects: Project[];
  onOpenRequirement: (id: string) => void;
  /** 挂根目录 / 选材料文件夹只在桌面端出现 */
  canPickFolders: boolean;
  /** 3g：本机打开声档时才给［打开文件夹］，远程的设备改成［复制路径］ */
  canReveal?: boolean;
  onProjectsChanged?: () => void | Promise<void>;
  /** 合并后跳到目标项目 */
  onOpenProject?: (projectId: string) => void;
  /** 标题行右侧的［关系图｜清单］（手机端没有关系图，不传） */
  modeToggle?: ReactNode;
  /** 4g：问答出处里的材料打开预览抽屉到「回答引用的这段」；不传时退回 onOpenPreview(文件 id) */
  onOpenPreviewTarget?: (target: PreviewTarget) => void;
  /** 4g：手机上问答卡占满宽度 */
  isMobile?: boolean;
}

type LoadState = "loading" | "ready" | "error";

const REQUIREMENTS_PAGE_SIZE = 10;
const MEETINGS_PAGE_SIZE = 8;
/** 文件名还在认（pending / walking）、内容还在读时隔一会儿再问一次进度 */
export const INDEX_POLL_MS = 15_000;

const COUNT_FORMAT = new Intl.NumberFormat("en-US");

function agoText(value: string | null, now: number): string {
  if (!value) return "";
  const at = Date.parse(value);
  if (Number.isNaN(at)) return "";
  const minutes = Math.floor((now - at) / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} 小时前` : `${Math.floor(hours / 24)} 天前`;
}

/** 材料那一节每个根目录一行文件名索引的进度（2d）。有内容那一行时「只记了个数」挪到内容那行的括号里（withDirs=false） */
export function indexStatusText(item: MaterialIndexRoot, now = Date.now(), withDirs = true): string {
  switch (item.state) {
    case "done": {
      const ago = agoText(item.updated_at, now);
      const dirs =
        withDirs && item.name_only_dirs ? `（node_modules、.git 等 ${item.name_only_dirs} 个文件夹只记了个数）` : "";
      return `已认得 ${COUNT_FORMAT.format(item.files)} 个文件名${ago ? ` · ${ago}` : ""}${dirs}`;
    }
    case "offline":
      return "资料盘未连接，插上后接着认";
    case "missing":
      return "找不到这个文件夹，先按上次认得的算";
    case "error":
      return `读不了：${item.error ?? "原因不明"}`;
    default:
      return `正在认文件名，已认 ${COUNT_FORMAT.format(item.files)} 个`;
  }
}

const UNREADABLE_LABELS: Record<UnreadableReason, string> = {
  password: "要密码",
  corrupt: "文件损坏",
  unsupported: "格式不支持",
  timeout: "处理超时",
  permission: "没有权限",
};

export interface CoverageText {
  /** 「已读 N 个，还剩 M 个」或「内容都读完了」；没有能读内容的文件时为 null */
  progress: string | null;
  /** 「读不了 7 个（要密码 2、…）」，后面接［看看］ */
  unreadable: string | null;
  /** 识别程序没装时各一句，用后端状态表里那句 */
  waiting: string[];
  /** 「（声档会议记录 12 个…只收文件名；…只记了个数；符号链接 2 个没跟进去）」 */
  names: string | null;
}

/** 材料根目录那一行下面的内容状态（3e），数字照第二期 1,234 的写法 */
export function coverageText(root: MaterialCoverageRoot): CoverageText {
  const { content, names } = root;
  const waitingFiles = content.waiting.reduce((sum, item) => sum + item.files, 0);
  const left = content.pending + waitingFiles;
  let progress: string | null = null;
  if (left > 0) {
    progress = `正文、图片文字、录音已读 ${COUNT_FORMAT.format(content.done)} 个，还剩 ${COUNT_FORMAT.format(left)} 个`;
    if (content.paused === "busy") progress += " · 转写会议时先停，转写完接着读";
  } else if (content.total > 0) {
    progress = "内容都读完了";
  }
  const reasons = (Object.keys(UNREADABLE_LABELS) as UnreadableReason[]).filter((key) => content.unreadable[key] > 0);
  const unreadableTotal = reasons.reduce((sum, key) => sum + content.unreadable[key], 0);
  const unreadable =
    unreadableTotal > 0
      ? `读不了 ${COUNT_FORMAT.format(unreadableTotal)} 个（${reasons
          .map((key) => `${UNREADABLE_LABELS[key]} ${COUNT_FORMAT.format(content.unreadable[key])}`)
          .join("、")}）`
      : null;
  const onlyNames: string[] = [];
  if (content.names_only.cards > 0) onlyNames.push(`声档会议记录 ${COUNT_FORMAT.format(content.names_only.cards)} 个`);
  if (content.names_only.other > 0) onlyNames.push(`压缩包等 ${COUNT_FORMAT.format(content.names_only.other)} 个`);
  const parts: string[] = [];
  if (onlyNames.length > 0) parts.push(`${onlyNames.join("、")}只收文件名`);
  if (names.name_only_dirs > 0) {
    parts.push(`node_modules、.git 等 ${COUNT_FORMAT.format(names.name_only_dirs)} 个文件夹只记了个数`);
  }
  if (names.symlinks > 0) parts.push(`符号链接 ${COUNT_FORMAT.format(names.symlinks)} 个没跟进去`);
  return {
    progress,
    unreadable,
    waiting: content.waiting.map((item) => item.hint),
    names: parts.length > 0 ? `（${parts.join("；")}）` : null,
  };
}

/** 还要不要接着问进度：有根目录在认文件名，或在线的根目录还有没读完的内容 */
export function materialsStillMoving(index: MaterialIndexStatus | null, coverage: MaterialCoverage | null): boolean {
  if (index?.roots.some((item) => item.state === "pending" || item.state === "walking")) return true;
  return Boolean(coverage?.roots.some((item) => item.online && item.content.pending > 0));
}

/** ［看看］展开的读不了的文件，按根目录取，每次 100 个 */
function UnreadableList({
  apiClient,
  projectId,
  rootId,
  onCopy,
  onOpenPreview,
}: {
  apiClient: ApiClient;
  projectId: string;
  rootId: number;
  onCopy: (path: string) => void;
  onOpenPreview?: (fileId: number) => void;
}) {
  const [items, setItems] = useState<MaterialUnreadableItem[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [state, setState] = useState<LoadState>("loading");

  const load = useCallback(
    async (offset: number) => {
      setState("loading");
      try {
        const page = await apiClient.getMaterialUnreadable(projectId, rootId, offset);
        setItems((current) => (offset === 0 ? page.items : [...current, ...page.items]));
        setNext(page.next_offset);
        setState("ready");
      } catch {
        setState("error");
      }
    },
    [apiClient, projectId, rootId],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  return (
    <div className="material-unreadable">
      {items.length > 0 && (
        <ul className="material-unreadable__list">
          {items.map((item) => (
            <li key={item.file_id}>
              <span className="material-unreadable__name" title={item.path}>
                {item.rel_path}
              </span>
              <span className="material-unreadable__reason">{UNREADABLE_LABELS[item.reason] ?? ""}</span>
              <span className="material-root-row__ops">
                <button onClick={() => onCopy(item.path)} type="button">
                  复制路径
                </button>
                {onOpenPreview && (
                  <button onClick={() => onOpenPreview(item.file_id)} type="button">
                    预览
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {state === "loading" && <p className="material-unreadable__muted">正在列…</p>}
      {state === "error" && (
        <p className="material-unreadable__muted">
          没列出来
          <button className="material-unreadable__more" onClick={() => void load(items.length)} type="button">
            再试一次
          </button>
        </p>
      )}
      {state === "ready" && next !== null && (
        <button className="material-unreadable__more" onClick={() => void load(next)} type="button">
          再列 100 个
        </button>
      )}
    </div>
  );
}

const REQUIREMENT_TABS: Array<{ key: RequirementStatus | "all"; label: string }> = [
  { key: "active", label: "进行中" },
  { key: "done", label: "已完成" },
  { key: "shelved", label: "已搁置" },
  { key: "all", label: "全部" },
];

function RequirementRefChips({ items }: { items: RequirementRef[] }) {
  if (items.length === 0) return <span className="detail-table__muted">—</span>;
  const shown = items.slice(0, 2);
  const rest = items.length - shown.length;
  return (
    <span className="detail-chip-row">
      {shown.map((item) => (
        <span className="detail-chip" key={item.id}>
          {item.title}
        </span>
      ))}
      {rest > 0 && <span className="detail-chip detail-chip--more">等 {rest} 个</span>}
    </span>
  );
}

export function ProjectDetailPage({
  apiClient,
  projectId,
  canWrite,
  onBack,
  onOpenGlossary,
  onOpenMeeting,
  onOpenTask: _onOpenTask,
  onOpenPreview,
  reloadKey = 0,
  onProjectUpdated,
  projects,
  onOpenRequirement,
  canPickFolders,
  canReveal = true,
  onProjectsChanged,
  onOpenProject,
  modeToggle,
  onOpenPreviewTarget,
  isMobile = false,
}: ProjectDetailPageProps) {
  const [board, setBoard] = useState<ProjectBoard | null>(null);
  const [boardState, setBoardState] = useState<LoadState>("loading");
  const [subfolders, setSubfolders] = useState<ProjectSubfoldersPayload | null>(null);
  const [indexStatus, setIndexStatus] = useState<MaterialIndexStatus | null>(null);
  const [coverage, setCoverage] = useState<MaterialCoverage | null>(null);
  // 哪些根目录的［看看］展开着
  const [unreadableOpen, setUnreadableOpen] = useState<Set<number>>(() => new Set());

  const [meetingRows, setMeetingRows] = useState<ProjectMeetingRow[] | null>(null);
  const [meetingsState, setMeetingsState] = useState<LoadState>("loading");
  // 项目详情里两张子表的页签与页码按项目分别记住，离开再回来还在原处。
  const [meetingsPage, setMeetingsPage] = usePersistentState(`project.${projectId}.meetingsPage`, 0);

  const [requirementsTab, setRequirementsTab] = usePersistentState<RequirementStatus | "all">(`project.${projectId}.requirementsTab`, "active");
  const [requirementsPage, setRequirementsPage] = usePersistentState(`project.${projectId}.requirementsPage`, 0);
  const [requirementsPayload, setRequirementsPayload] = useState<RequirementsPayload | null>(null);
  const [requirementsState, setRequirementsState] = useState<LoadState>("loading");

  const [editingProject, setEditingProject] = useState<false | "edit" | "merge" | "delete">(false);
  const [creatingRequirement, setCreatingRequirement] = useState(false);
  const [addingRoot, setAddingRoot] = useState(false);
  const [reselectingRoot, setReselectingRoot] = useState<MaterialRoot | null>(null);
  // 等补建的文件夹停了：重新选位置
  const [movingPending, setMovingPending] = useState(false);
  const [confirm, confirmDialog] = useConfirm();
  const [rootBusy, setRootBusy] = useState(false);
  // 挂/重选根目录失败的原因：显示在还开着的取径器里（D27），不是页面级 notice
  const [rootError, setRootError] = useState("");
  const { notice, setNotice, dismissNotice } = useNotice();

  const { toastNode, showToast } = useToast();

  const loadBoard = useCallback(async () => {
    try {
      const data = await apiClient.projectBoard(projectId);
      setBoard(data);
      setBoardState("ready");
    } catch {
      setBoardState("error");
    }
  }, [apiClient, projectId]);

  const loadSubfolders = useCallback(async () => {
    try {
      setSubfolders(await apiClient.projectMaterialSubfolders(projectId));
    } catch {
      // 子文件夹数只是辅助信息，读取失败不影响主卡片
      setSubfolders(null);
    }
  }, [apiClient, projectId]);

  const loadMeetings = useCallback(async () => {
    setMeetingsState("loading");
    try {
      const rows = await apiClient.projectMeetings(projectId);
      setMeetingRows(rows);
      setMeetingsState("ready");
    } catch {
      setMeetingsState("error");
    }
  }, [apiClient, projectId]);

  const loadRequirements = useCallback(async () => {
    setRequirementsState("loading");
    try {
      const payload = await apiClient.requirements({
        project_id: projectId,
        ...(requirementsTab === "all" ? {} : { status: requirementsTab }),
        limit: REQUIREMENTS_PAGE_SIZE,
        offset: requirementsPage * REQUIREMENTS_PAGE_SIZE,
      });
      setRequirementsPayload(payload);
      setRequirementsState("ready");
    } catch {
      setRequirementsState("error");
    }
  }, [apiClient, projectId, requirementsTab, requirementsPage]);

  useEffect(() => {
    void loadBoard();
    void loadSubfolders();
  }, [loadBoard, loadSubfolders, reloadKey]);

  useEffect(() => {
    void loadMeetings();
  }, [loadMeetings, reloadKey]);

  useEffect(() => {
    void loadRequirements();
  }, [loadRequirements, reloadKey]);

  // 文件名索引和内容的进度：挂的根目录变了就重问；同一个定时器每 15 秒两样一起问，
  // 有根目录在认文件名、或在线根目录还有没读完的内容时继续，否则停
  const rootsKey = (board?.material_roots ?? []).map((root) => `${root.id}:${root.path}`).join("|");
  useEffect(() => {
    if (!rootsKey || typeof apiClient.getMaterialIndexStatus !== "function") return;
    let active = true;
    let timer = 0;
    const run = async () => {
      try {
        const [payload, contentPayload] = await Promise.all([
          apiClient.getMaterialIndexStatus(projectId),
          // 旧后端没有 coverage：只是少一行内容状态
          typeof apiClient.getMaterialCoverage === "function"
            ? apiClient.getMaterialCoverage(projectId).catch(() => null)
            : Promise.resolve(null),
        ]);
        if (!active) return;
        setIndexStatus(payload);
        setCoverage(contentPayload);
        if (materialsStillMoving(payload, contentPayload)) {
          timer = window.setTimeout(() => void run(), INDEX_POLL_MS);
        }
      } catch {
        // 进度只是辅助信息，读不到就不写这一行
      }
    };
    void run();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [apiClient, projectId, reloadKey, rootsKey]);

  const subfolderCounts = useMemo(() => {
    const map = new Map<number, number>();
    subfolders?.roots.forEach((entry) => map.set(entry.root_id, entry.folders.length));
    return map;
  }, [subfolders]);

  const meetingsPageCount = Math.ceil((meetingRows?.length ?? 0) / MEETINGS_PAGE_SIZE);
  const visibleMeetings = (meetingRows ?? []).slice(
    meetingsPage * MEETINGS_PAGE_SIZE,
    meetingsPage * MEETINGS_PAGE_SIZE + MEETINGS_PAGE_SIZE,
  );
  const requirementsPageCount = requirementsPayload
    ? Math.max(1, Math.ceil(requirementsPayload.total / REQUIREMENTS_PAGE_SIZE))
    : 1;

  const switchRequirementsTab = (key: RequirementStatus | "all") => {
    if (key === requirementsTab) return;
    setRequirementsTab(key);
    setRequirementsPage(0);
  };

  const copyWithToast = async (text: string, done: string) => {
    try {
      await copyText(text);
      showToast(done);
    } catch {
      setNotice("复制失败，请手动复制", "error");
    }
  };
  const copyPath = (path: string) => copyWithToast(path, "已复制路径");
  // 挂上文件夹时当场补写的会议卡片张数
  const cardsWrittenNote = (root: MaterialRoot | undefined) =>
    root?.cards_written ? `，已补写 ${root.cards_written} 张会议卡片` : "";

  const refreshAfterRootChange = async () => {
    await Promise.all([loadBoard(), loadSubfolders()]);
    await onProjectsChanged?.();
  };

  const addRoot = async (path: string) => {
    setRootBusy(true);
    setRootError("");
    try {
      const added = await apiClient.addProjectMaterialRoot(projectId, path);
      setAddingRoot(false);
      setNotice([`材料根目录已添加${cardsWrittenNote(added)}`, ...nestedHints(added.path, added.nested)].join("。"));
      await refreshAfterRootChange();
    } catch (error) {
      // D27：取径器还开着，错误要就地显示在弹窗里，不能吞掉／丢到被弹窗盖住的页面级提示
      setRootError(error instanceof Error ? error.message : "添加失败，请稍后重试");
    } finally {
      setRootBusy(false);
    }
  };

  const reselectRoot = async (path: string) => {
    if (!reselectingRoot) return;
    setRootBusy(true);
    setRootError("");
    try {
      // 原子替换：只改路径，根目录 id 不变；失败时旧根目录原样保留，不会「删了没加上」。
      const replaced = await apiClient.replaceProjectMaterialRoot(projectId, reselectingRoot.id, path);
      setReselectingRoot(null);
      setNotice(`材料根目录已更新${movedNote(replaced)}`);
      await refreshAfterRootChange();
    } catch (error) {
      setRootError(error instanceof Error ? error.message : "更新失败，请稍后重试");
    } finally {
      setRootBusy(false);
    }
  };

  // 改名找回的［是它］：说出一起改了哪些，再重读项目
  const onRepointed = async (result: MaterialRootRepoint) => {
    setNotice(`材料根目录已改到 ${result.path}${movedNote(result)}`);
    await refreshAfterRootChange();
  };

  // 等补建的文件夹换个位置：盘在线就当场建好并挂上
  const movePending = async (parent: string) => {
    setRootBusy(true);
    setRootError("");
    try {
      const detail = await apiClient.movePendingFolder(projectId, parent);
      setMovingPending(false);
      const mounted = detail.material_roots?.[0];
      const pending = detail.pending_folder;
      if (!pending && mounted) setNotice(`已建好 ${mounted.path}，挂到了这个项目`);
      else if (pending?.state === "waiting") setNotice(`位置改好了，插上资料盘后自动建 ${pending.path}`);
      else setNotice(`位置改好了，但还没建成：${pending?.reason ?? "请稍后再试"}`, "warning");
      await refreshAfterRootChange();
    } catch (error) {
      setRootError(error instanceof Error ? error.message : "没改成，请稍后重试");
    } finally {
      setRootBusy(false);
    }
  };

  // 「不建了，以后自己挂文件夹」
  const dropPending = async () => {
    setRootBusy(true);
    setRootError("");
    try {
      await apiClient.dropPendingFolder(projectId);
      setMovingPending(false);
      setNotice("好的，不建了；以后在这里挂文件夹就行");
      await refreshAfterRootChange();
    } catch (error) {
      setRootError(error instanceof Error ? error.message : "操作失败，请稍后重试");
    } finally {
      setRootBusy(false);
    }
  };

  // 确认弹窗里执行移除：失败原因留在弹窗里显示，不再写到被弹窗盖住的页面提示上。
  const removeRoot = async (root: MaterialRoot) => {
    setNotice("");
    const removed = await confirm({
      title: "移除材料根目录",
      message: (
        <span className="confirm-modal__path">
          <FolderIcon className="confirm-modal__path-icon" />
          {root.path}
        </span>
      ),
      confirmLabel: "移除",
      tone: "danger",
      action: () => apiClient.removeProjectMaterialRoot(projectId, root.id),
    });
    if (!removed) return;
    setNotice("材料根目录已移除");
    await refreshAfterRootChange();
  };

  const openAddRoot = () => {
    setRootError("");
    setAddingRoot(true);
  };

  const openReselectRoot = (root: MaterialRoot) => {
    setRootError("");
    setReselectingRoot(root);
  };

  const roots = board?.material_roots ?? [];
  const pendingFolder = roots.length === 0 ? board?.pending_folder ?? null : null;
  const requirementCounts = board?.requirement_counts;
  // 挂/移/重选根目录既要能写这个项目，也要在桌面端；复制路径不受限，谁都能读
  const canManageFolders = canWrite && canPickFolders;
  const isEmptyProject = (board?.meeting_count ?? 0) === 0 && (requirementCounts?.all ?? 0) === 0;

  return (
    <section className="detail-page page-content">
      {toastNode}
      <header className="detail-head">
        <nav aria-label="面包屑" className="detail-breadcrumb">
          <button onClick={onBack} type="button">
            项目管理
          </button>
          <span aria-hidden="true">/</span>
          <span>{board?.name ?? ""}</span>
        </nav>
        <div className="detail-head__row">
          {board && (
            <div className="detail-head__title">
              <i aria-hidden="true" style={{ background: board.color }} />
              <h1>{board.name}</h1>
            </div>
          )}
          {modeToggle}
          {canWrite && board && (
            <span className="detail-head__actions">
              <button className="detail-head__edit" onClick={() => setEditingProject("edit")} type="button">
                编辑项目
              </button>
              <button className="detail-head__create-requirement" onClick={() => setCreatingRequirement(true)} type="button">
                ＋ 新建需求
              </button>
            </span>
          )}
        </div>
        {board && (
          <p className="detail-head__subtitle">
            会议 {board.meeting_count ?? meetingRows?.length ?? 0} 场 · 需求 {requirementCounts?.all ?? 0} 个 ·
            未完成任务 {board.open_task_count ?? 0} 条
          </p>
        )}
      </header>

      <NoticeBanner notice={notice} onDismiss={dismissNotice} />

      {boardState === "loading" && <AsyncState state="loading" />}
      {boardState === "error" && (
        <div className="detail-error">
          <span>项目详情读取失败</span>
          <button onClick={() => void loadBoard()} type="button">
            重试
          </button>
        </div>
      )}

      {board && boardState === "ready" && (
        <>
          {board.origin === "ai" && roots.length === 0 && (
            <div className="detail-hint" role="note">
              <span>这个项目是 AI 自动建的，还没挂文件夹。是重复的就合并到别的项目，用不上可以删掉。</span>
              {canWrite && (
                <span className="detail-hint__actions">
                  {canManageFolders && (
                    <button className="text-button" onClick={openAddRoot} type="button">
                      挂上文件夹
                    </button>
                  )}
                  <button className="text-button" onClick={() => setEditingProject("merge")} type="button">
                    合并到…
                  </button>
                  {isEmptyProject && (
                    <button className="text-button" onClick={() => setEditingProject("delete")} type="button">
                      删除
                    </button>
                  )}
                </span>
              )}
            </div>
          )}

          {/* 4g：「问这个项目」卡在「AI 自动建的项目」提示之后、时间线之上（手机上也有）；没有 askPrepare 时不画 */}
          <ProjectAsk
            apiClient={apiClient}
            isMobile={isMobile}
            onOpenMeeting={onOpenMeeting}
            onOpenPreview={(target) =>
              onOpenPreviewTarget ? onOpenPreviewTarget(target) : onOpenPreview?.(target.fileId)
            }
            projectId={projectId}
            projectName={board.name}
            variant="card"
          />

          {/* 4c：时间线在「AI 自动建的项目」提示之后、「材料根目录」卡之前 */}
          <ProjectTimeline
            apiClient={apiClient}
            canWrite={canWrite}
            onAttachRoot={canManageFolders ? openAddRoot : undefined}
            onOpenMeeting={onOpenMeeting}
            projectId={projectId}
            reloadKey={reloadKey}
          />

          <section className="detail-card">
            <header className="detail-card__head">
              <h2>材料根目录</h2>
              {canManageFolders && (
                <button className="detail-card__add" onClick={openAddRoot} type="button">
                  ＋ 添加目录
                </button>
              )}
            </header>
            {pendingFolder ? (
              <ul className="material-root-list">
                <li className="material-root-row material-root-row--pending">
                  <FolderIcon className="material-root-row__icon" />
                  {pendingFolder.state === "waiting" ? (
                    <span className="material-root-row__path">资料盘未连接，插上后自动建 {pendingFolder.path}</span>
                  ) : (
                    <>
                      <span className="material-root-row__path">{pendingFolder.path}</span>
                      <span className="material-root-row__missing">{pendingFolder.reason ?? "文件夹没建成"}</span>
                      {canManageFolders && typeof apiClient.movePendingFolder === "function" && (
                        <span className="material-root-row__ops">
                          <button
                            onClick={() => {
                              setRootError("");
                              setMovingPending(true);
                            }}
                            type="button"
                          >
                            重新选位置…
                          </button>
                        </span>
                      )}
                    </>
                  )}
                </li>
              </ul>
            ) : roots.length === 0 ? (
              <div className="detail-card__empty">
                <p>还没有材料根目录</p>
                {canManageFolders && (
                  <button onClick={openAddRoot} type="button">
                    ＋ 添加目录
                  </button>
                )}
              </div>
            ) : (
              <ul className="material-root-list">
                {roots.map((root) => {
                  const state = root.state ?? (root.exists ? "online" : "missing");
                  const index = indexStatus?.roots.find((item) => item.root_id === root.id);
                  const covered = coverage?.roots.find((item) => item.root_id === root.id);
                  const contentText = covered ? coverageText(covered) : null;
                  const showUnreadable = unreadableOpen.has(root.id);
                  return (
                    <li className="material-root-row" key={root.id}>
                      <FolderIcon className="material-root-row__icon" />
                      <span className="material-root-row__path">{root.path}</span>
                      {state === "online" ? (
                        <span className="material-root-row__count">
                          {subfolderCounts.get(root.id) ?? 0} 个子文件夹
                        </span>
                      ) : state === "volume_offline" ? (
                        <span className="material-root-row__offline">资料盘未连接，插上后自动恢复</span>
                      ) : (
                        // 盘在、文件夹没了：问是不是改了名，没候选时照旧「重新选…」
                        <RootRenameQuestion
                          apiClient={apiClient}
                          canManage={canManageFolders}
                          onRepointed={onRepointed}
                          onReselect={() => openReselectRoot(root)}
                          projectId={projectId}
                          root={root}
                        />
                      )}
                      {index && (
                        <span
                          className={`material-root-row__index${
                            index.state === "error" || index.state === "missing" ? " is-stopped" : ""
                          }`}
                        >
                          {indexStatusText(index, Date.now(), !covered)}
                        </span>
                      )}
                      {contentText &&
                        (contentText.progress || contentText.unreadable || contentText.names || contentText.waiting.length > 0) && (
                          <div className="material-root-row__content">
                            {(contentText.progress || contentText.unreadable || contentText.names) && (
                              <p>
                                {[contentText.progress, contentText.unreadable].filter(Boolean).join("，")}
                                {contentText.unreadable && (
                                  <button
                                    aria-expanded={showUnreadable}
                                    className="material-root-row__look"
                                    onClick={() =>
                                      setUnreadableOpen((current) => {
                                        const nextOpen = new Set(current);
                                        if (nextOpen.has(root.id)) nextOpen.delete(root.id);
                                        else nextOpen.add(root.id);
                                        return nextOpen;
                                      })
                                    }
                                    type="button"
                                  >
                                    {showUnreadable ? "收起" : "看看"}
                                  </button>
                                )}
                                {contentText.names && <span className="material-root-row__names">{contentText.names}</span>}
                              </p>
                            )}
                            {contentText.waiting.map((hint) => (
                              <p className="material-root-row__waiting" key={hint}>
                                {hint}
                              </p>
                            ))}
                            {showUnreadable && (
                              <UnreadableList
                                apiClient={apiClient}
                                onCopy={(path) => void copyPath(path)}
                                onOpenPreview={onOpenPreview}
                                projectId={projectId}
                                rootId={root.id}
                              />
                            )}
                          </div>
                        )}
                      {(root.shared_with?.length ?? 0) > 0 && (
                        <span className="material-root-row__shared" role="note">
                          也挂在{root.shared_with!.map((entry) => `「${entry.project_name}」`).join("")}下，
                          {root.cards_owner_id === projectId
                            ? "会议卡片写在这个项目里"
                            : `会议卡片只写给先挂上的「${
                                root.shared_with!.find((entry) => entry.project_id === root.cards_owner_id)?.project_name ?? ""
                              }」，不需要可以在这里移除`}
                        </span>
                      )}
                      <span className="material-root-row__ops">
                        {state === "online" && (
                          <button onClick={() => void copyPath(root.path)} type="button">
                            复制路径
                          </button>
                        )}
                        {canManageFolders && (
                          <button
                            className="material-root-row__remove"
                            onClick={() => void removeRoot(root)}
                            type="button"
                          >
                            移除
                          </button>
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {board.cards && (
              <ProjectCardsRow
                apiClient={apiClient}
                canPickFolders={Boolean(canPickFolders)}
                canReveal={canReveal}
                canWrite={canWrite}
                cards={board.cards}
                onChanged={loadBoard}
                onCopy={copyWithToast}
                projectId={projectId}
              />
            )}
          </section>

          {board.profile && (
            <ProjectRecognitionCard
              apiClient={apiClient}
              canWrite={canWrite}
              onChanged={async () => {
                await loadBoard();
                await onProjectsChanged?.();
              }}
              profile={board.profile}
              projectId={projectId}
              projectName={board.name}
            />
          )}

          <section className="detail-card">
            <header className="detail-card__head">
              <h2>需求</h2>
            </header>
            <div aria-label="需求状态" className="detail-subtabs" role="tablist">
              {REQUIREMENT_TABS.map((tab) => (
                <button
                  aria-selected={requirementsTab === tab.key}
                  key={tab.key}
                  onClick={() => switchRequirementsTab(tab.key)}
                  role="tab"
                  type="button"
                >
                  {tab.label}
                  <span>
                    {tab.key === "all" ? requirementCounts?.all ?? 0 : requirementCounts?.[tab.key] ?? 0}
                  </span>
                </button>
              ))}
            </div>
            {requirementsState === "loading" && <AsyncState state="loading" />}
            {requirementsState === "error" && (
              <div className="detail-error">
                <span>需求读取失败</span>
                <button onClick={() => void loadRequirements()} type="button">
                  重试
                </button>
              </div>
            )}
            {requirementsState === "ready" && requirementsPayload && requirementsPayload.items.length === 0 && (
              <AsyncState message="还没有需求" state="empty" />
            )}
            {requirementsState === "ready" && requirementsPayload && requirementsPayload.items.length > 0 && (
              <>
                <table className="detail-table">
                  <thead>
                    <tr>
                      <th>需求名称</th>
                      <th>优先级</th>
                      <th>状态</th>
                      <th>未完成任务</th>
                      <th>关联会议</th>
                      <th>最近会议</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {requirementsPayload.items.map((item: RequirementSummary) => (
                      <tr key={item.id}>
                        <td>
                          <button className="detail-table__link" onClick={() => onOpenRequirement(item.id)} type="button">
                            {item.title}
                          </button>
                        </td>
                        <td>
                          <PriorityBadge priority={item.priority} />
                        </td>
                        <td>
                          <RequirementStatusBadge status={item.status} />
                        </td>
                        <td>{item.open_task_count}</td>
                        <td>{item.meeting_count}</td>
                        <td>{formatMonthDay(item.latest_meeting_date)}</td>
                        <td>
                          <button className="text-button" onClick={() => onOpenRequirement(item.id)} type="button">
                            查看
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="detail-table__pagination">
                  <p className="detail-table__count">共 {requirementsPayload.total} 条</p>
                  <Pagination onChange={setRequirementsPage} page={requirementsPage} pageCount={requirementsPageCount} />
                </div>
              </>
            )}
          </section>

          <section className="detail-card">
            <header className="detail-card__head">
              <h2>会议</h2>
            </header>
            {meetingsState === "loading" && <AsyncState state="loading" />}
            {meetingsState === "error" && (
              <div className="detail-error">
                <span>会议读取失败</span>
                <button onClick={() => void loadMeetings()} type="button">
                  重试
                </button>
              </div>
            )}
            {meetingsState === "ready" && (meetingRows?.length ?? 0) === 0 && (
              <AsyncState message="还没有会议" state="empty" />
            )}
            {meetingsState === "ready" && (meetingRows?.length ?? 0) > 0 && (
              <>
                <table className="detail-table">
                  <thead>
                    <tr>
                      <th>日期</th>
                      <th>会议</th>
                      <th>时长</th>
                      <th>关联需求</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleMeetings.map((row) => (
                      <tr key={row.id}>
                        <td>{formatMonthDay(row.recording_date)}</td>
                        <td>{row.title}</td>
                        <td>{row.duration_ms != null ? formatDurationText(row.duration_ms) : "--"}</td>
                        <td>
                          <RequirementRefChips items={row.requirements} />
                        </td>
                        <td>
                          <button className="text-button" onClick={() => onOpenMeeting(row.id)} type="button">
                            打开
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="detail-table__pagination">
                  <p className="detail-table__count">共 {meetingRows?.length ?? 0} 场</p>
                  <Pagination onChange={setMeetingsPage} page={meetingsPage} pageCount={meetingsPageCount} />
                </div>
              </>
            )}
          </section>

          <ProjectGlossary
            apiClient={apiClient}
            canWrite={canWrite}
            onChanged={async (message) => {
              setNotice(message);
              await loadBoard();
            }}
            onOpenGlossary={onOpenGlossary}
            projectId={projectId}
            projectName={board.name}
            publicCount={board.public_glossary_count ?? 0}
            terms={board.glossary_terms ?? []}
            total={board.glossary_count ?? 0}
          />
        </>
      )}

      {editingProject && board && (
        <ProjectFormModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          mode="edit"
          onClose={() => setEditingProject(false)}
          onSaved={() => {
            setEditingProject(false);
            setNotice("项目已更新");
            void loadBoard();
            onProjectUpdated?.();
            void onProjectsChanged?.();
          }}
          initialAction={editingProject === "edit" ? undefined : editingProject}
          onDeleted={() => {
            void onProjectsChanged?.();
            onBack();
          }}
          onMerged={(target) => {
            void onProjectsChanged?.();
            if (onOpenProject) onOpenProject(target.id);
            else onBack();
          }}
          project={board}
          projects={projects}
        />
      )}

      {creatingRequirement && (
        <RequirementModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          defaultProjectId={projectId}
          mode="create"
          onClose={() => setCreatingRequirement(false)}
          onSaved={() => {
            setCreatingRequirement(false);
            setRequirementsPage(0);
            void loadBoard();
            void loadRequirements();
          }}
          projects={projects}
        />
      )}

      {addingRoot && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={rootBusy}
          error={rootError}
          onClose={() => setAddingRoot(false)}
          onConfirm={(path) => void addRoot(path)}
        />
      )}

      {reselectingRoot && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={rootBusy}
          error={rootError}
          onClose={() => setReselectingRoot(null)}
          onConfirm={(path) => void reselectRoot(path)}
        />
      )}

      {movingPending && board?.pending_folder && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={rootBusy}
          description={`项目文件夹「${board.pending_folder.path.split("/").filter(Boolean).pop() ?? ""}」会建在选中的文件夹里面`}
          error={rootError}
          extraOption={{ label: "不建了，以后自己挂文件夹", onSelect: () => void dropPending() }}
          onClose={() => setMovingPending(false)}
          onConfirm={(path) => void movePending(path)}
          title="重新选位置"
        />
      )}

      {confirmDialog}
    </section>
  );
}
