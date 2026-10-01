import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, type ApiClient } from "../api";
import { formatMonthDay } from "../format";
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
  ProjectSubfoldersPayload,
  UnreadableReason,
} from "../types";
import { AsyncState } from "./AsyncState";
import { nestedHints } from "./ClaimFoldersDialog";
import { FolderIcon } from "./FolderIcon";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import { ProjectCardsRow } from "./ProjectCardsRow";
import { ProjectFormModal } from "./ProjectFormModal";
import { ProjectRecognitionCard } from "./ProjectRecognitionCard";
import { RootRenameQuestion, movedNote } from "./RootRenameQuestion";
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
import { RecordingsTab } from "./projects/detail/RecordingsTab";
import { WorkTab } from "./projects/detail/WorkTab";

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
  /** 「关系图」标签页里的内容（手机端没有关系图，不传就没有这个标签页） */
  graphTab?: ReactNode;
  /** 地址栏记着的视图：graph＝停在关系图标签页。切到或离开关系图标签页时通过 onViewModeChange 告诉外面 */
  viewMode?: "graph" | "list";
  onViewModeChange?: (mode: "graph" | "list") => void;
  /** 「去认领」：外面把需求池切到待认领并只筛本项目，再跳过去 */
  onClaimCandidates?: (projectId: string) => void;
  /** 4g：问答出处里的材料打开预览抽屉到「回答引用的这段」；不传时退回 onOpenPreview(文件 id) */
  onOpenPreviewTarget?: (target: PreviewTarget) => void;
  /** 4g：手机上问答卡占满宽度 */
  isMobile?: boolean;
}

type LoadState = "loading" | "ready" | "error";
type BoardState = LoadState | "missing";
type DetailTab = "work" | "recordings" | "graph" | "materials";

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

export function ProjectDetailPage({
  apiClient,
  projectId,
  canWrite,
  onBack,
  onOpenGlossary,
  onOpenMeeting,
  onOpenTask,
  onOpenPreview,
  reloadKey = 0,
  onProjectUpdated,
  projects,
  onOpenRequirement,
  canPickFolders,
  canReveal = true,
  onProjectsChanged,
  onOpenProject,
  graphTab,
  viewMode,
  onViewModeChange,
  onClaimCandidates,
  onOpenPreviewTarget,
  isMobile = false,
}: ProjectDetailPageProps) {
  const [board, setBoard] = useState<ProjectBoard | null>(null);
  const [boardState, setBoardState] = useState<BoardState>("loading");
  const titleRef = useRef<HTMLHeadingElement>(null);
  const titleFocused = useRef(false);
  const [subfolders, setSubfolders] = useState<ProjectSubfoldersPayload | null>(null);
  const [indexStatus, setIndexStatus] = useState<MaterialIndexStatus | null>(null);
  const [coverage, setCoverage] = useState<MaterialCoverage | null>(null);
  // 哪些根目录的［看看］展开着
  const [unreadableOpen, setUnreadableOpen] = useState<Set<number>>(() => new Set());

  const [tab, setTab] = useState<DetailTab>(viewMode === "graph" && graphTab ? "graph" : "work");
  // 新建需求后「需求与任务」要重取
  const [workKey, setWorkKey] = useState(0);

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
    } catch (error) {
      // 项目不存在（被删、被合并、链接过期）：单独一种状态，只出一个「项目不存在」
      setBoardState(error instanceof ApiError && error.status === 404 ? "missing" : "error");
    }
  }, [apiClient, projectId]);

  // 进详情后焦点落到页面标题：键盘进来时不停在 body 上
  useEffect(() => {
    if (board && !titleFocused.current) {
      titleFocused.current = true;
      titleRef.current?.focus({ preventScroll: true });
    }
  }, [board]);

  const loadSubfolders = useCallback(async () => {
    try {
      setSubfolders(await apiClient.projectMaterialSubfolders(projectId));
    } catch {
      // 子文件夹数只是辅助信息，读取失败不影响主卡片
      setSubfolders(null);
    }
  }, [apiClient, projectId]);

  useEffect(() => {
    void loadBoard();
    void loadSubfolders();
  }, [loadBoard, loadSubfolders, reloadKey]);

  // 地址栏（前进、后退、深链）改了视图：标签页跟着走
  const hasGraph = Boolean(graphTab);
  useEffect(() => {
    if (viewMode === "graph" && hasGraph) setTab("graph");
    else if (viewMode === "list") setTab((current) => (current === "graph" ? "work" : current));
  }, [viewMode, hasGraph]);

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

  const selectTab = (next: DetailTab) => {
    if (next === tab) return;
    setTab(next);
    if (next === "graph" || tab === "graph") onViewModeChange?.(next === "graph" ? "graph" : "list");
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

  const firstRoot = roots[0];
  // 看板接口不带座次和最近一场会的日子，项目列表里有
  const listed = projects.find((project) => project.id === projectId);
  const seat = board?.seat ?? listed?.seat ?? null;
  const latestMeeting = board?.latest_meeting_date ?? listed?.latest_meeting_date ?? null;
  const subtitleParts: string[] = [];
  if (board) {
    subtitleParts.push(`${board.meeting_count ?? 0} 场会`);
    if (latestMeeting) subtitleParts.push(`最近 ${formatMonthDay(latestMeeting)}`);
    if (firstRoot) subtitleParts.push(`材料根目录 ${firstRoot.path}${roots.length > 1 ? `（另 ${roots.length - 1} 个）` : ""}`);
  }

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
              <h1 ref={titleRef} tabIndex={-1}>
                {board.name}
              </h1>
              {seat !== null && (
                <span aria-label={`座次 ${seat}`} className="detail-head__seat">
                  {seat}
                </span>
              )}
            </div>
          )}
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
        {board && <p className="detail-head__subtitle">{subtitleParts.join(" · ")}</p>}
        {board && boardState === "ready" && (
          <div aria-label="项目内容" className="project-tabs" role="tablist">
            <button aria-selected={tab === "work"} onClick={() => selectTab("work")} role="tab" type="button">
              需求与任务
            </button>
            <button aria-selected={tab === "recordings"} onClick={() => selectTab("recordings")} role="tab" type="button">
              录音
              {(board.meeting_count ?? 0) > 0 && <small>{board.meeting_count}</small>}
            </button>
            {graphTab && (
              <button aria-selected={tab === "graph"} onClick={() => selectTab("graph")} role="tab" type="button">
                关系图
              </button>
            )}
            <button aria-selected={tab === "materials"} onClick={() => selectTab("materials")} role="tab" type="button">
              材料
            </button>
          </div>
        )}
      </header>

      <NoticeBanner notice={notice} onDismiss={dismissNotice} />

      {boardState === "loading" && <AsyncState state="loading" />}
      {boardState === "missing" && (
        <div className="detail-error" role="alert">
          <span>项目不存在，可能已被删除或合并到别的项目</span>
          <button onClick={onBack} type="button">
            返回项目列表
          </button>
        </div>
      )}
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

          {tab === "work" && (
            <WorkTab
              apiClient={apiClient}
              canWrite={canWrite}
              onChanged={() => void onProjectsChanged?.()}
              onClaim={onClaimCandidates ? () => onClaimCandidates(projectId) : undefined}
              onOpenMeeting={onOpenMeeting}
              onOpenRequirement={onOpenRequirement}
              onOpenTask={onOpenTask}
              projectId={projectId}
              projectName={board.name}
              projectSeat={seat}
              projects={projects}
              reloadKey={reloadKey + workKey}
              showToast={showToast}
            />
          )}

          {tab === "recordings" && (
            <RecordingsTab
              apiClient={apiClient}
              onOpenMeeting={onOpenMeeting}
              onOpenRequirement={onOpenRequirement}
              projectId={projectId}
              reloadKey={reloadKey}
            />
          )}

          {tab === "materials" && (
            <>
            {/* 4g：「问这个项目」卡在「材料」页签最上面（手机上也有）；没有 askPrepare 时不画 */}
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
              candidates={board.glossary_candidates}
              candidateTotal={board.glossary_candidate_total}
              onOpenMeeting={(meetingId, seekMs) => onOpenMeeting(meetingId, seekMs)}
              onReload={loadBoard}
            />
            {/* 决议时间线收在「材料」页签的最后 */}
            <ProjectTimeline
              apiClient={apiClient}
              canWrite={canWrite}
              onAttachRoot={canManageFolders ? openAddRoot : undefined}
              onOpenMeeting={onOpenMeeting}
              projectId={projectId}
              reloadKey={reloadKey}
            />

            </>
          )}
        </>
      )}

      {/* 关系图自己管加载和出错，不等看板读完；地址栏深链直接落在这个标签页时也能打开 */}
      {tab === "graph" && graphTab && boardState !== "missing" && <div className="detail-tab-graph">{graphTab}</div>}

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
            setWorkKey((key) => key + 1);
            void loadBoard();
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
