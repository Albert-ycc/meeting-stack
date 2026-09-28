import { useCallback, useContext, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import {
  ApiError,
  isOldBackend,
  type ApiClient,
  type RelationAnswer,
  type RelationAnswerResult,
  type RelationQuestion,
} from "../../api";
import { reassignNote } from "../../cardCopy";
import type { PreviewTarget, Project } from "../../types";
import { ProjectAsk } from "../ask/ProjectAsk";
import { hasDraft } from "../ask/askStore";
import { NoticeBanner, UNDO_NOTICE_MS, useNotice, type NoticeAction, type NoticeTone } from "../Notice";
import { OLD_BACKEND_TEXT, RecentAnswersContext } from "../links/useRelationAnswer";
import { useLinksFlags } from "../links/LinksFlagsContext";
import { drawableEdges, drawnEdges, meetingAges } from "./drawnEdges";
import { LocalGraphPanel } from "./LocalGraphPanel";
import { LocalGraphView, MAP_FAILED, type LocalError } from "./LocalGraphView";
import { TRACE_FAILED } from "../links/TraceList";
import { GraphCanvas, type DoorstepAnswer, type DropTarget } from "./GraphCanvas";
import { FocusPanel } from "./FocusPanel";
import { GraphPanel, clearBriefCache } from "./GraphPanel";
import { GraphSearch } from "./GraphSearch";
import { forgetBrief, loadBrief, localUndoUntil, type GraphNoticeUndo } from "./panelParts";
import type {
  BriefFile,
  GraphEdge,
  GraphFile,
  GraphLocal,
  GraphPayload,
  GraphRootsPayload,
  GraphWindow,
  LocalGraph,
  MeetingFocus,
  RelatedEdges,
  StatusPhrase,
  TracePayload,
} from "./graphTypes";
import { fileExt, withRecentFiles, withRelatedEdges, type PinnedFile } from "./graphFiles";
import {
  readGraphLines,
  readGraphWindow,
  recordGraphOpen,
  writeGraphLines,
  writeGraphWindow,
  type GraphLines,
} from "./graphPrefs";
import { attentionOrder, layoutStarMap, mentionLabel, type StarLayout } from "./layout";
import { MeetingFocusView } from "./MeetingFocusView";
import { useMiniPlayer } from "./MiniPlayer";
import { forgetViewportViews } from "./useGraphViewport";
import "./ProjectGraph.css";

/** 画布开着时每 30 秒对一次数据；没变化时服务器回 304，几乎不花钱 */
const REFRESH_MS = 30_000;
const TRAIL_MAX = 5;
/** ⌘Z 最多往回退这么多步 */
const UNDO_STACK_MAX = 10;

const WINDOW_OPTIONS: Array<{ key: GraphWindow; label: string }> = [
  { key: "7d", label: "7 天" },
  { key: "28d", label: "28 天" },
  { key: "90d", label: "90 天" },
  { key: "all", label: "全部" },
];

/** 短 hash（FNV-1a，36 进制）：子文件夹节点 id 里用它代替相对路径，中文路径不进地址栏 */
export function shortHash(text: string): string {
  let hash = 0x811c9dc5;
  for (const char of text) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(36);
}

/** 根目录外侧挂最近改过的子文件夹（资料盘状态里带着，最多 3 个），细线连回根目录 */
function withSubfolders(graph: GraphPayload, roots: GraphRootsPayload | null): GraphPayload {
  if (!roots) return graph;
  const folders: GraphPayload["folders"] = [];
  const edges: GraphPayload["edges"] = [];
  for (const folder of graph.folders) {
    if (folder.kind !== "root" || folder.root_id === undefined) continue;
    const root = roots.roots.find((item) => item.root_id === folder.root_id);
    if (!root || root.state !== "online") continue;
    for (const recent of (root.recent_dirs ?? []).slice(0, 3)) {
      const id = `sub:${folder.root_id}:${shortHash(recent.dir)}`;
      folders.push({
        id,
        kind: "subfolder",
        name: recent.name,
        path: recent.path,
        ring: "outer",
        root_id: folder.root_id,
        dir: recent.dir,
        mtime: recent.mtime,
      });
      edges.push({ id: `e:${id}`, kind: "folder", from: folder.id, to: id, label: "" });
    }
  }
  if (!folders.length) return graph;
  return { ...graph, folders: [...graph.folders, ...folders], edges: [...graph.edges, ...edges] };
}

/**
 * 选中一场会时，用简报补出它提到的全部文件（照子文件夹的做法：补的节点排在最后，不挤动已有节点），
 * 已在图上的文件只补这场会连过去的线。
 */
function withMentionedFiles(graph: GraphPayload, meetingId: string | null, files: BriefFile[] | null): GraphPayload {
  if (!meetingId || !files?.length) return graph;
  const from = `m:${meetingId}`;
  if (!graph.meetings.some((meeting) => meeting.id === from)) return graph;
  const known = new Set((graph.files ?? []).map((file) => file.id));
  const edgeIds = new Set(graph.edges.map((edge) => edge.id));
  const addFiles: GraphFile[] = [];
  const addEdges: GraphEdge[] = [];
  for (const file of files) {
    const id = `file:${file.file_id}`;
    if (!known.has(id)) {
      known.add(id);
      addFiles.push({
        id,
        kind: "file",
        file_id: file.file_id,
        name: file.name,
        ext: fileExt(file.name),
        rel_path: file.rel_path,
        root_id: file.root_id,
        folder: `root:${file.root_id}`,
        extra: true,
      });
    }
    const edgeId = `e:file:${file.file_id}:${meetingId}`;
    if (edgeIds.has(edgeId)) continue;
    edgeIds.add(edgeId);
    addEdges.push({
      id: edgeId,
      kind: "mentioned",
      from,
      to: id,
      label: mentionLabel(file),
      count: file.count,
      source: file.source,
      needle: file.needle,
      stem_key: file.stem_key,
      meeting_id: meetingId,
      anchors_ms: file.first_ms === null ? [] : [file.first_ms],
    });
  }
  if (!addFiles.length && !addEdges.length) return graph;
  return { ...graph, files: [...(graph.files ?? []), ...addFiles], edges: [...graph.edges, ...addEdges] };
}

/** 文件节点、「提到」线和相关线（4f）：会议到文件的线保留那场会的上下文 */
function isFileSelection(id: string) {
  return id.startsWith("file:") || id.startsWith("e:file:") || id.startsWith("e:rel:");
}

/** 4f：图例（底部［图例］按钮点开的小窗），九行 */
export const LEGEND_ROWS: Array<{ sample: string; text: string }> = [
  { sample: "", text: "位置：左会议 · 右材料 · 上需求 · 下线索词，越靠中心越新" },
  { sample: "solid", text: "实线：归属、讨论" },
  { sample: "dashed", text: "细虚线：文件夹" },
  { sample: "arrow", text: "带箭头的实线：交付物" },
  { sample: "quote", text: "细线带引号：会上提到这份文件" },
  { sample: "ask", text: "琥珀色虚线：在等你回答的产出和可能过时" },
  { sample: "review", text: "流动的琥珀色虚线：待复核的归属" },
  { sample: "dotted", text: "浅灰点线：相关（两边有共同词），默认关着" },
  { sample: "short", text: "短虚线：跨项目、像是新需求" },
];

export const RELATED_EMPTY_TEXT = "这个时间窗里还没有相关的线";
export const RELATED_FAILED_TEXT = "相关的线没取到";
export const RELATED_OFF_TITLE = "打开后每个节点最多 3 条";

function Legend() {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onDown = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [open]);
  return (
    <span className="project-graph__legend-wrap" ref={boxRef}>
      <button aria-expanded={open} className="ghost-button" onClick={() => setOpen((value) => !value)} type="button">
        图例
      </button>
      {open && (
        <div aria-label="图例" className="project-graph__legend-box" role="dialog">
          <strong>图例</strong>
          <ul>
            {LEGEND_ROWS.map((row) => (
              <li key={row.text}>
                <i aria-hidden="true" className={`legend-sample legend-sample--${row.sample || "none"}`} />
                {row.text}
              </li>
            ))}
          </ul>
        </div>
      )}
    </span>
  );
}

/** 4f：「在等你」里在问的文件（先可能过时，后等你认交付物），N 键在需求之后走它们 */
function askFileIdsOf(status: GraphPayload["status"] | undefined): string[] {
  return [...new Set((status?.waiting ?? []).flatMap((phrase) => phrase.node_ids.filter((id) => id.startsWith("file:"))))];
}

/** 从哪场会点进文件面板或「提到」线：往回找上一个选中的会，中间只隔着文件或「提到」线 */
function contextMeetingOf(selection: string | null, trail: string[]): string | null {
  if (!selection) return null;
  if (selection.startsWith("m:")) return selection.slice(2);
  if (!isFileSelection(selection)) return null;
  for (let index = trail.length - 1; index >= 0; index -= 1) {
    const id = trail[index];
    if (id.startsWith("m:")) return id.slice(2);
    if (!isFileSelection(id)) return null;
  }
  return null;
}

// 按（项目，时间窗，深链目标）缓存一份：切回画布先画旧数据，再到后台对一次
const graphCache = new Map<string, GraphPayload>();
const rootsCache = new Map<string, GraphRootsPayload>();
// 4f：相关线按（项目，时间窗）缓存一份，带 ETag
const relatedCache = new Map<string, { etag: string | null; related: RelatedEdges }>();

function cacheKey(projectId: string, window: GraphWindow | null, focus: string | null) {
  return `${projectId}|${window ?? "auto"}|${focus ?? ""}`;
}

/** 从对象页返回时地址栏带着 ?sel=，深链目标换了缓存键也先画同一时间窗的旧图 */
function cachedGraph(projectId: string, window: GraphWindow | null, focus: string | null) {
  return graphCache.get(cacheKey(projectId, window, focus)) ?? graphCache.get(cacheKey(projectId, window, null)) ?? null;
}

/** 测试之间清空模块级缓存 */
export function forgetGraphCache() {
  graphCache.clear();
  rootsCache.clear();
  relatedCache.clear();
  forgetViewportViews();
  clearBriefCache();
}

function sameUndo(a: GraphNoticeUndo, b: GraphNoticeUndo) {
  if (a.kind === "project" && b.kind === "project") return a.meetingId === b.meetingId;
  if ((a.kind === "link" || a.kind === "unlink") && a.kind === b.kind) {
    return a.meetingId === b.meetingId && a.requirementId === b.requirementId;
  }
  if (a.kind === "task" && b.kind === "task") return a.taskId === b.taskId;
  if (a.kind === "mention" && b.kind === "mention") return a.meetingId === b.meetingId && a.stemKey === b.stemKey;
  if (a.kind === "deliverable" && b.kind === "deliverable") return a.deliverableId === b.deliverableId;
  if (a.kind === "relation" && b.kind === "relation") return a.relationId === b.relationId;
  return false;
}

function errorText(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/** 深链目标可能不在图上原样出现：没归项目的会在门口、太旧的会折进了「更早 N 场」 */
function resolveSelection(graph: GraphPayload, layout: StarLayout, id: string): string | null {
  if (layout.byId.has(id) || graph.edges.some((edge) => edge.id === id)) return id;
  if (id.startsWith("m:")) {
    const meetingId = id.slice(2);
    if (layout.byId.has(`d:${meetingId}`)) return `d:${meetingId}`;
    const group = graph.collapsed.find((item) => item.meeting_ids.includes(meetingId));
    if (group) return group.id;
  }
  return null;
}

function WeeklyBars({ weekly }: { weekly: GraphPayload["weekly"] }) {
  if (!weekly.length) return null;
  const max = Math.max(1, ...weekly.map((item) => item.count));
  const total = weekly.reduce((sum, item) => sum + item.count, 0);
  const barW = 7;
  const gap = 2;
  const height = 22;
  return (
    <svg
      aria-label={`最近 ${weekly.length} 周每周会数，共 ${total} 场`}
      className="project-graph__weekly"
      height={height}
      role="img"
      width={weekly.length * (barW + gap) - gap}
    >
      {weekly.map((item, index) => {
        const h = item.count ? Math.max(3, (item.count / max) * height) : 1;
        return (
          <rect
            className={item.count ? "" : "is-empty"}
            height={h}
            key={item.from}
            rx={1.5}
            width={barW}
            x={index * (barW + gap)}
            y={height - h}
          >
            <title>
              {item.from.slice(5).replace("-", "/")}–{item.to.slice(5).replace("-", "/")} {item.count} 场
            </title>
          </rect>
        );
      })}
    </svg>
  );
}

function StatusLine({
  status,
  active,
  onToggle,
}: {
  status: GraphPayload["status"];
  active: string | null;
  onToggle: (phrase: StatusPhrase | null) => void;
}) {
  const groups: Array<{ key: "ok" | "waiting" | "stopped"; label: string; items: StatusPhrase[] }> = [
    { key: "ok", label: "好了", items: status.ok },
    { key: "waiting", label: "在等你", items: status.waiting },
    { key: "stopped", label: "停了", items: status.stopped },
  ];
  const shown = groups.filter((group) => group.items.length);
  return (
    <p className="project-graph__status">
      {shown.map((group, groupIndex) => (
        <span className={`project-graph__status-group project-graph__status-group--${group.key}`} key={group.key}>
          {groupIndex > 0 && <span aria-hidden="true" className="project-graph__sep"> · </span>}
          <span className="project-graph__status-label">{group.label}</span>{" "}
          {group.items.map((phrase, index) => (
            <span key={phrase.text}>
              {index > 0 && "、"}
              <button
                aria-pressed={active === phrase.text}
                className="project-graph__phrase"
                disabled={phrase.node_ids.length === 0}
                onClick={() => onToggle(active === phrase.text ? null : phrase)}
                title={phrase.node_ids.length ? "点一下在图上点亮，再点一下取消" : undefined}
                type="button"
              >
                {phrase.text}
              </button>
            </span>
          ))}
        </span>
      ))}
      {shown.length === 0 && <span className="project-graph__status-empty">这段时间没有会</span>}
      {status.note && <span className="project-graph__status-note">{status.note}</span>}
    </p>
  );
}

export interface ProjectGraphProps {
  apiClient: ApiClient;
  projectId: string;
  projects: Project[];
  /** 地址栏里的选中（sel=m:<id>）；null 表示没选 */
  selection: string | null;
  /** 从会议页、需求页深链进来的目标：不在时间窗里时后端自动放宽 */
  focus?: string | null;
  onSelectionChange: (id: string | null) => void;
  onBack: () => void;
  /** 展开的那场会（地址栏 expand=<会议 id>）；不传 onExpandChange 时由画布自己记 */
  expanded?: string | null;
  onExpandChange?: (meetingId: string | null) => void;
  /** 标题行右侧的［关系图｜清单］ */
  modeToggle?: ReactNode;
  onOpenMeeting: (meetingId: string) => void;
  onOpenRequirement: (requirementId: string) => void;
  onOpenGlossary: (projectId: string) => void;
  onOpenProject: (projectId: string) => void;
  onOpenAttributionReview?: () => void;
  onProjectsChanged?: () => void | Promise<void>;
  /** 3g：打开预览抽屉（图上放不下的文件、展开一场会里的交付物小签） */
  onOpenPreview?: (fileId: number, startMs?: number) => void;
  /** 4g：问答出处打开会议（带时间和标签页）；不传时退回 onOpenMeeting(id) */
  onOpenMeetingAt?: (meetingId: string, seekMs?: number, tab?: "transcript" | "minutes") => void;
  /** 4g：问答出处里的材料打开预览抽屉到「回答引用的这段」 */
  onOpenPreviewTarget?: (target: PreviewTarget) => void;
  /** 4f：局部图、来龙去脉（地址栏 file=、trace=）；不传 onLocalChange 时由画布自己记 */
  local?: GraphLocal | null;
  /** replace：挪过位置换成新 id 时替换地址，不压历史 */
  onLocalChange?: (local: GraphLocal | null, options?: { replace?: boolean }) => void;
  /** 4f：交付物线、局部图任务面板的［打开任务］ */
  onOpenTask?: (taskId: string) => void;
}

export function ProjectGraph({
  apiClient,
  projectId,
  projects,
  selection,
  focus: initialFocus = null,
  onSelectionChange,
  onBack,
  expanded: expandedProp = null,
  onExpandChange,
  modeToggle,
  onOpenMeeting,
  onOpenRequirement,
  onOpenGlossary,
  onOpenProject,
  onOpenAttributionReview,
  onProjectsChanged,
  onOpenPreview,
  onOpenMeetingAt,
  onOpenPreviewTarget,
  local: localProp = null,
  onLocalChange,
  onOpenTask,
}: ProjectGraphProps) {
  const flags = useLinksFlags();
  // 4f：局部图、来龙去脉要 v16 的表（flags 不为 null）和两个接口
  const canLocal =
    flags !== null && typeof apiClient.graphFileMap === "function" && typeof apiClient.graphTrace === "function";
  const relatedAvailable = Boolean(flags?.linksEnabled && flags?.semanticEnabled) && typeof apiClient.graphRelated === "function";
  const [lines, setLines] = useState<GraphLines>(() => readGraphLines(projectId));
  const [windowChoice, setWindowChoice] = useState<GraphWindow | null>(() => readGraphWindow(projectId));
  // 用户一旦自己选了时间窗，深链目标就不再撑大窗口
  const [focus, setFocus] = useState<string | null>(initialFocus);
  const key = cacheKey(projectId, windowChoice, focus);
  const [graph, setGraph] = useState<GraphPayload | null>(() => cachedGraph(projectId, windowChoice, focus));
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);
  const [roots, setRoots] = useState<GraphRootsPayload | null>(() => rootsCache.get(projectId) ?? null);
  const [rootsTick, setRootsTick] = useState(0);
  const [trail, setTrail] = useState<string[]>([]);
  const [highlight, setHighlight] = useState<{ key: string; ids: Set<string> } | null>(null);
  // 「在图上找」点亮的节点；状态句、面板里的点亮优先
  const [searchIds, setSearchIds] = useState<Set<string> | null>(null);
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const [previousPositions, setPreviousPositions] = useState<Map<string, { x: number; y: number }> | undefined>();
  // 最近几步能撤销的操作，最新的在最后：提示条上的［撤销］和 ⌘Z 都撤最后一步
  const [undoStack, setUndoStack] = useState<GraphNoticeUndo[]>([]);
  const [clock, setClock] = useState(() => Date.now());
  const [ownExpanded, setOwnExpanded] = useState<string | null>(null);
  const expanded = onExpandChange ? expandedProp : ownExpanded;
  const [ownLocal, setOwnLocal] = useState<GraphLocal | null>(null);
  // 展开一场会和局部图互斥：两个都有时留展开
  const local = expanded || !canLocal ? null : onLocalChange ? localProp : ownLocal;
  const [localData, setLocalData] = useState<{ key: string; payload: LocalGraph | TracePayload } | null>(null);
  const [localError, setLocalError] = useState<LocalError | null>(null);
  const [localTick, setLocalTick] = useState(0);
  const [localSel, setLocalSel] = useState<string | null>(null);
  const [moved, setMoved] = useState<{ fileId: number; folder: string } | null>(null);
  // 展开时面板里选中的决议（dec:<决议 id>，没有 id 时 dec:<下标>）、任务（task:<id>）或「+N」；不进地址栏
  const [focusSel, setFocusSel] = useState<string | null>(null);
  const [focusData, setFocusData] = useState<MeetingFocus | null>(null);
  const [focusError, setFocusError] = useState("");
  const [focusTick, setFocusTick] = useState(0);
  const { notice, setNotice, dismissNotice } = useNotice();
  // 回答以后收成的那一行（App 一层）；⌘Z 撤销了就一起收掉
  const recentAnswers = useContext(RecentAnswersContext);
  const player = useMiniPlayer();
  const requestRef = useRef(0);
  const missingRef = useRef<string | null>(null);
  // 4f：线上回答以后，新数据到了再把选中挪到新的交付物线（没有就挪到那份文件）
  const afterAnswerRef = useRef<{ edgeId: string | null; fileId: number } | null>(null);
  // 3g：从面板、深链点出来要在图上补出的那一个文件；深链的文件先取名字和根目录
  const [pinned, setPinned] = useState<PinnedFile | null>(null);
  const [fileLookup, setFileLookup] = useState<{ id: string; state: "loading" | "failed" } | null>(null);

  useEffect(() => {
    recordGraphOpen();
  }, []);

  const load = useCallback(async () => {
    const token = ++requestRef.current;
    setLoading(true);
    try {
      const payload = await apiClient.graph(projectId, windowChoice ?? undefined, focus ?? undefined);
      graphCache.set(cacheKey(projectId, windowChoice, focus), payload);
      if (token !== requestRef.current) return;
      setGraph(payload);
      setLoadError("");
      const moveTo = afterAnswerRef.current;
      if (moveTo) {
        afterAnswerRef.current = null;
        const found = moveTo.edgeId !== null && payload.edges.some((edge) => edge.id === moveTo.edgeId);
        onSelectionChange(found ? moveTo.edgeId : `file:${moveTo.fileId}`);
      }
    } catch (reason) {
      if (token !== requestRef.current) return;
      setLoadError(errorText(reason, "关系图读取失败"));
    } finally {
      if (token === requestRef.current) setLoading(false);
    }
    // onSelectionChange 由宿主传，通常是 setState，不跟着重挂
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, focus, projectId, windowChoice]);

  // 换时间窗：有缓存先画缓存，没有就留着旧图等新数据
  useEffect(() => {
    const cached = cachedGraph(projectId, windowChoice, focus);
    if (cached) setGraph(cached);
    void load();
  }, [key, load]);

  const looseWaitingRef = useRef<string | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void load();
      setRootsTick((tick) => tick + 1);
      const waiting = looseWaitingRef.current;
      if (waiting) {
        forgetBrief(waiting);
        setVersion((current) => current + 1);
      }
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // 资料盘状态走单独的接口：后台缓存还在检查时隔一会儿再问，最多问 10 次
  useEffect(() => {
    let active = true;
    let timer = 0;
    let tries = 0;
    const run = async () => {
      try {
        const payload = await apiClient.graphRoots(projectId);
        rootsCache.set(projectId, payload);
        if (!active) return;
        setRoots(payload);
        if (payload.checking && tries < 10) {
          tries += 1;
          timer = window.setTimeout(() => void run(), 1500);
        }
      } catch {
        if (active && tries < 3) {
          tries += 1;
          timer = window.setTimeout(() => void run(), 3000);
        }
      }
    };
    void run();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [apiClient, projectId, rootsTick]);

  // 选中的会（或从它点进去的文件、「提到」线）：从简报补出它提到的全部文件
  const contextMeeting = contextMeetingOf(selection, trail);
  const contextOnGraph =
    contextMeeting && graph?.meetings.some((meeting) => meeting.meeting_id === contextMeeting) ? contextMeeting : null;
  const [briefFiles, setBriefFiles] = useState<{ meetingId: string; files: BriefFile[] } | null>(null);
  useEffect(() => {
    if (!contextOnGraph) {
      // 离开那场会：30 秒的刷新不再顺带重取它的简报
      looseWaitingRef.current = null;
      return;
    }
    let active = true;
    loadBrief(apiClient, contextOnGraph)
      .then((brief) => {
        if (!active) return;
        // 4b：会上换了叫法的文件还在整理时，30 秒的刷新顺带重取这场会的简报
        looseWaitingRef.current = brief.loose_state?.kind === "waiting" ? contextOnGraph : null;
        setBriefFiles({ meetingId: contextOnGraph, files: brief.files ?? [] });
      })
      .catch(() => active && setBriefFiles({ meetingId: contextOnGraph, files: [] }));
    return () => {
      active = false;
    };
  }, [apiClient, contextOnGraph, version]);
  const extraFiles = briefFiles && briefFiles.meetingId === contextOnGraph ? briefFiles.files : null;

  // 4f：［相关］开着才取相关线（带 If-None-Match，相关重算只动 related_rev）
  const relatedOn = lines.related && relatedAvailable;
  const relatedKey = `${projectId}|${graph?.window.effective ?? windowChoice ?? "28d"}`;
  const [related, setRelated] = useState<{ key: string; data: RelatedEdges } | null>(() => {
    const cached = relatedCache.get(relatedKey);
    return cached ? { key: relatedKey, data: cached.related } : null;
  });
  const [relatedState, setRelatedState] = useState<"idle" | "loading" | "ready" | "failed" | "old">("idle");
  const [relatedTick, setRelatedTick] = useState(0);
  useEffect(() => {
    if (!relatedOn || !graph) return;
    let active = true;
    const cached = relatedCache.get(relatedKey);
    if (cached) setRelated({ key: relatedKey, data: cached.related });
    setRelatedState((current) => (cached ? "ready" : current === "ready" ? current : "loading"));
    apiClient
      .graphRelated(projectId, graph.window.effective, cached?.etag ?? null)
      .then(({ related: fresh, etag }) => {
        if (!active) return;
        const data = fresh ?? cached?.related ?? null;
        if (data) {
          relatedCache.set(relatedKey, { etag, related: data });
          setRelated({ key: relatedKey, data });
        }
        setRelatedState("ready");
      })
      .catch((reason: unknown) => active && setRelatedState(isOldBackend(reason) ? "old" : "failed"));
    return () => {
      active = false;
    };
    // graph 每 30 秒刷新一次：跟着对一次相关线（没变时 304）
  }, [apiClient, graph, projectId, relatedKey, relatedOn, relatedTick]);
  const relatedData = relatedOn && related?.key === relatedKey ? related.data : null;

  const toggleLines = (change: Partial<GraphLines>) => {
    setLines((current) => {
      const next = { ...current, ...change };
      writeGraphLines(projectId, next);
      return next;
    });
  };

  // 4f：局部图、来龙去脉：每换一次中心整张重取（不缓存）；回答以后也整张重取
  const localKey = local ? (local.kind === "file" ? `file:${local.fileId}` : `trace:${local.node}`) : null;
  useEffect(() => {
    setLocalSel(null);
  }, [localKey]);
  useEffect(() => {
    if (!local || !localKey) return;
    let active = true;
    setLocalError(null);
    const request =
      local.kind === "file"
        ? apiClient.graphFileMap(local.fileId, { related: relatedOn })
        : apiClient.graphTrace(local.node);
    request
      .then((payload: LocalGraph | TracePayload) => {
        if (!active) return;
        setLocalData({ key: localKey, payload });
        // 挪过位置：地址换成新 id（替换，不压历史），那一句留着
        const center = payload.center;
        if (local.kind === "file" && center.moved_from !== undefined && center.file_id !== undefined) {
          setMoved({ fileId: center.file_id, folder: center.folder ?? "" });
          changeLocal({ kind: "file", fileId: center.file_id }, { replace: true });
        }
      })
      .catch((reason: unknown) => {
        if (!active) return;
        if (isOldBackend(reason)) setLocalError({ text: OLD_BACKEND_TEXT, retry: false });
        else if (reason instanceof ApiError && [404, 409, 422].includes(reason.status)) {
          setLocalError({ text: reason.message, retry: false });
        } else setLocalError({ text: local.kind === "file" ? MAP_FAILED : TRACE_FAILED, retry: true });
      });
    return () => {
      active = false;
    };
    // local 每次渲染都是新对象，按 localKey 比
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, localKey, localTick, relatedOn]);

  // 残影只在撤销期内画；最早的一个到期时重画一次
  const liveGraph = useMemo(() => {
    if (!graph) return null;
    const movedOut = graph.moved_out.filter((item) => Date.parse(item.undo_until) > clock);
    const base = withSubfolders(movedOut.length === graph.moved_out.length ? graph : { ...graph, moved_out: movedOut }, roots);
    // 3g：会上提到的文件优先，再补从面板点出来的那一个、最近改过的文件；4f：相关线接在提到之后、最近之前
    return withRecentFiles(
      withRelatedEdges(withMentionedFiles(base, contextOnGraph, extraFiles), relatedData),
      roots,
      pinned,
    );
  }, [clock, contextOnGraph, extraFiles, graph, pinned, relatedData, roots]);

  useEffect(() => {
    const deadlines = [
      ...(liveGraph?.moved_out ?? []).map((item) => Date.parse(item.undo_until)),
      ...undoStack.map((item) => Date.parse(item.until)),
    ].filter((value) => value > Date.now());
    if (deadlines.length === 0) return;
    const wait = Math.min(...deadlines) - Date.now() + 50;
    const timer = window.setTimeout(() => setClock(Date.now()), Math.min(wait, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [liveGraph, undoStack]);

  // 展开的会：换会、数据有变（version）、点重试时重读
  useEffect(() => {
    if (!expanded) {
      setFocusData(null);
      setFocusError("");
      return;
    }
    let active = true;
    setFocusError("");
    apiClient
      .graphMeetingFocus(expanded)
      .then((payload) => active && setFocusData(payload))
      .catch((reason: unknown) => active && setFocusError(errorText(reason, "这场会读取失败")));
    return () => {
      active = false;
    };
  }, [apiClient, expanded, focusTick, version]);

  // 换了展开的会（包括浏览器前进、后退）时，上一场会里选中的决议、任务不再作数
  useEffect(() => {
    setFocusSel(null);
  }, [expanded]);

  const setExpanded = useCallback(
    (meetingId: string | null) => {
      if (onExpandChange) onExpandChange(meetingId);
      else setOwnExpanded(meetingId);
    },
    [onExpandChange],
  );

  const layout = useMemo(() => (liveGraph ? layoutStarMap(liveGraph) : null), [liveGraph]);
  const askFileIds = useMemo(() => askFileIdsOf(graph?.status), [graph?.status]);
  const attention = useMemo(() => (layout ? attentionOrder(layout, askFileIds) : []), [askFileIds, layout]);

  const resolved = liveGraph && layout && selection ? resolveSelection(liveGraph, layout, selection) : null;
  // 4f：选中节点时碰到它却没画出来的线（面板里列出来，点一行选中那条线）
  const hiddenEdges = useMemo(() => {
    if (!liveGraph || !layout || !resolved || !layout.byId.has(resolved)) return [];
    const drawable = drawableEdges(liveGraph.edges, (id) => layout.byId.has(id), !lines.mention);
    return drawnEdges(drawable, resolved, null, { meetingAge: meetingAges(liveGraph.meetings) }).hidden;
  }, [layout, lines.mention, liveGraph, resolved]);

  // 深链目标不在图上（需求已结束、会被折叠之外）：说一声，不留空面板
  useEffect(() => {
    if (!graph || !layout || !selection) return;
    if (resolved === selection) return;
    // 子文件夹跟着资料盘状态一起到，先等一等
    if (selection.startsWith("sub:") && !roots) return;
    // 从简报补出来的文件节点、「提到」线：简报还在路上时也等一等
    if (isFileSelection(selection) && contextOnGraph && !extraFiles) return;
    if (resolved) {
      onSelectionChange(resolved);
      return;
    }
    // 3g：点出来的文件补到了图上却放不下（材料那一侧的槽位满了）：直接打开预览抽屉，不留空选中
    if (pinned && selection === `file:${pinned.file_id}`) {
      setPinned(null);
      onSelectionChange(null);
      if (onOpenPreview) onOpenPreview(pinned.file_id);
      else setNotice("图上放不下这个文件了", "warning");
      return;
    }
    // 3g：深链到不在图上的文件：先取文件信息补成节点，取回前不说「不在当前的图上」
    const fileId = /^file:(\d+)$/.exec(selection)?.[1];
    if (fileId && selection === focus && typeof apiClient.getGraphFile === "function") {
      if (fileLookup?.id !== selection) {
        setFileLookup({ id: selection, state: "loading" });
        apiClient
          .getGraphFile(Number(fileId))
          .then((detail) => {
            if (detail.file.project_id !== projectId || detail.file.gone) throw new Error("不在这个项目里");
            setPinned({
              file_id: detail.file.id,
              name: detail.file.name,
              rel_path: detail.file.rel_path,
              root_id: detail.file.root_id,
              folder: `root:${detail.file.root_id}`,
            });
          })
          .catch(() => setFileLookup({ id: selection, state: "failed" }));
        return;
      }
      if (fileLookup.state === "loading") return;
    }
    // 会上提到的文件、像是新需求、等补建的文件夹会随着数据来去（常常是你作答以后就没了）：
    // 不是深链进来的，就悄悄收起面板，不说「不在当前的图上」
    if (selection !== focus && /^(file:|e:file:|nr:|e:nr:|pending:|e:prod:|e:aff:|e:dlv:|e:rel:)/.test(selection)) {
      onSelectionChange(null);
      return;
    }
    if (missingRef.current === selection) return;
    missingRef.current = selection;
    setNotice(
      selection.startsWith("r:")
        ? "这个需求不在进行中，关系图只画进行中的需求"
        : "要看的节点不在当前的图上，换个时间窗试试",
      "warning",
    );
    onSelectionChange(null);
  }, [
    apiClient,
    contextOnGraph,
    extraFiles,
    fileLookup,
    focus,
    graph,
    layout,
    onOpenPreview,
    onSelectionChange,
    pinned,
    projectId,
    resolved,
    roots,
    selection,
    setNotice,
  ]);


  // 4g：问答面板开在右侧面板的位置，本地状态，不占 sel=；点节点或 Esc 关掉。
  // 搜索页交过来的问题（草稿）在时一打开就展开，由 ProjectAsk 的 takeDraft 填进输入框（不自动发）
  const canAsk = typeof apiClient.askPrepare === "function";
  const [askOpen, setAskOpen] = useState(() => canAsk && hasDraft(projectId));
  useEffect(() => {
    // 不重新挂载、换了项目时也看一次
    if (canAsk && hasDraft(projectId)) setAskOpen(true);
  }, [canAsk, projectId]);

  const select = useCallback(
    (id: string | null) => {
      if (id !== null) setAskOpen(false);
      if (id === null) {
        setTrail([]);
        setHighlight(null);
      } else if (selection && id !== selection) {
        setTrail((current) => [...current, selection].slice(-TRAIL_MAX));
      }
      onSelectionChange(id);
    },
    [onSelectionChange, selection],
  );

  /** 文件夹面板、最近改过的文件、散放文件的文件行：在图上补出这个文件并打开文件面板（放不下时打开预览抽屉） */
  const openFile = useCallback(
    (file: PinnedFile) => {
      setPinned(file);
      select(`file:${file.file_id}`);
    },
    [select],
  );

  /** 4f：不在图上的在问的文件（状态句、N 键走到的）：取回文件信息钉上它并选中，一次钉一个 */
  const pinAskFile = useCallback(
    (id: string) => {
      const fileId = Number(id.slice("file:".length));
      if (!Number.isFinite(fileId) || typeof apiClient.getGraphFile !== "function") return;
      const phrase = (graph?.status.waiting ?? []).find((item) => item.node_ids.includes(id));
      const stale = Boolean(phrase?.text.endsWith("可能过时"));
      apiClient
        .getGraphFile(fileId)
        .then((detail) => {
          setPinned({
            file_id: detail.file.id,
            name: detail.file.name,
            rel_path: detail.file.rel_path,
            root_id: detail.file.root_id,
            folder: `root:${detail.file.root_id}`,
            ...(stale ? { stale: true } : { asks_deliverable: true }),
          });
          select(id);
        })
        .catch(() => setNotice("这份文件读不到了", "warning"));
    },
    [apiClient, graph?.status.waiting, select, setNotice],
  );

  const selectOrPin = useCallback(
    (id: string | null) => {
      if (id && /^file:\d+$/.test(id) && layout && !layout.byId.has(id) && askFileIds.includes(id)) {
        pinAskFile(id);
        return;
      }
      select(id);
    },
    [askFileIds, layout, pinAskFile, select],
  );

  /** 状态句：点亮图上有的；在问的文件一个都不在图上时钉上第一个并选中它 */
  const togglePhrase = (phrase: StatusPhrase | null) => {
    if (!phrase) {
      setHighlight(null);
      return;
    }
    const present = phrase.node_ids.filter((id) => layout?.byId.has(id));
    if (!present.length && phrase.node_ids[0]?.startsWith("file:")) {
      pinAskFile(phrase.node_ids[0]);
      return;
    }
    setHighlight({ key: phrase.text, ids: new Set(phrase.node_ids) });
  };

  /** 4f：在线上（文件面板）回答以后，选中挪到新的交付物线（［是］）或那份文件，文件钉住 */
  const answered = useCallback(
    (question: RelationQuestion, answer: RelationAnswer, result: RelationAnswerResult) => {
      if (local) {
        setLocalTick((tick) => tick + 1);
        return;
      }
      const fileId = question.file?.id;
      if (!fileId) return;
      const node = liveGraph?.files.find((file) => file.file_id === fileId);
      // 文件钉住（不再在问时它可能不该上图了），还是同一个节点
      if (node) {
        setPinned({ file_id: node.file_id, name: node.name, rel_path: node.rel_path, root_id: node.root_id, folder: node.folder });
      }
      // 宿主紧接着重取：新数据到了再挪选中（［是］挪到新的实线交付物线，其余挪到文件）
      afterAnswerRef.current = {
        edgeId: answer === "yes" && result.deliverable_id ? `e:dlv:${result.deliverable_id}` : null,
        fileId,
      };
    },
    [liveGraph?.files, local],
  );

  /** 进出局部图、换中心（App 压历史；没有 App 时自己记） */
  const changeLocal = useCallback(
    (next: GraphLocal | null, options?: { replace?: boolean }) => {
      if (next === null) setMoved(null);
      if (onLocalChange) onLocalChange(next, options);
      else {
        // 展开一场会和局部图互斥：从展开的决议面板进来时先收起（有 App 时由 App 收）
        if (next !== null) setOwnExpanded(null);
        setOwnLocal(next);
      }
    },
    [onLocalChange],
  );

  const goBack = () => {
    const previous = trail[trail.length - 1];
    setTrail((current) => current.slice(0, -1));
    onSelectionChange(previous ?? null);
  };

  const refresh = useCallback(async () => {
    clearBriefCache();
    setVersion((current) => current + 1);
    await load();
    setRootsTick((tick) => tick + 1);
  }, [load]);

  const changed = useCallback(async () => {
    await refresh();
    await onProjectsChanged?.();
  }, [onProjectsChanged, refresh]);

  const showNotice = useCallback(
    (message: string, undoTarget?: GraphNoticeUndo, tone: NoticeTone = "success", actions?: NoticeAction[]) => {
      setNotice(message, tone, undoTarget || actions?.length ? UNDO_NOTICE_MS : undefined, actions);
      if (undoTarget) setUndoStack((current) => [...current, undoTarget].slice(-UNDO_STACK_MAX));
    },
    [setNotice],
  );

  /** 记下当前每个节点的位置，数据回来后节点从这里飞到新位置；门口的会作答后变成 m: 节点 */
  const rememberPositions = () => {
    if (!layout) return;
    const positions = new Map<string, { x: number; y: number }>();
    for (const node of layout.nodes) {
      positions.set(node.id, { x: node.x, y: node.y });
      if (node.id.startsWith("d:")) positions.set(`m:${node.id.slice(2)}`, { x: node.x, y: node.y });
    }
    setPreviousPositions(positions);
  };

  const answerDoorstep = async ({ meetingId, projectId: target }: DoorstepAnswer) => {
    if (busy || !graph) return;
    setBusy(true);
    rememberPositions();
    try {
      const detail = await apiClient.updateMeeting(meetingId, { project_id: target ?? "" });
      const effects = detail.effects;
      const name =
        target === graph.project.id ? graph.project.name : projects.find((project) => project.id === target)?.name ?? "";
      const head = target ? `已归到 ${name || "所选项目"}` : "已标为不归这些项目";
      const note = effects ? reassignNote(effects.tasks_moved, effects.tasks_left.length, effects.card) : "";
      showNotice(
        note ? `${head}；${note}` : head,
        effects?.undo_until ? { kind: "project", meetingId, until: effects.undo_until } : undefined,
      );
      if (selection === `d:${meetingId}`) {
        setTrail([]);
        onSelectionChange(target === graph.project.id ? `m:${meetingId}` : null);
      }
      await changed();
    } catch (reason) {
      showNotice(errorText(reason, "操作失败"), undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  /** 撤销一步。改归属走服务器的撤销；关联需求就解除；搬任务就按原样搬回来（连需求一起） */
  const runUndo = async (entry: GraphNoticeUndo) => {
    if (busy) return;
    setBusy(true);
    rememberPositions();
    setUndoStack((current) => current.filter((item) => item !== entry && !sameUndo(item, entry)));
    try {
      if (entry.kind === "project") {
        const detail = await apiClient.undoMeetingProject(entry.meetingId);
        showNotice(detail.effects?.card?.action === "moved" ? "已撤销刚才的改动，会议卡片也搬回去了" : "已撤销刚才的改动");
      } else if (entry.kind === "link") {
        await apiClient.removeRequirementMeeting(entry.requirementId, entry.meetingId);
        clearBriefCache();
        showNotice(`已撤销：这场会不再关联「${entry.title}」`);
      } else if (entry.kind === "unlink") {
        await apiClient.addRequirementMeeting(entry.requirementId, entry.meetingId);
        clearBriefCache();
        showNotice(`已撤销：重新关联了「${entry.title}」`);
      } else if (entry.kind === "mention") {
        await apiClient.restoreFileMention(entry.meetingId, entry.stemKey);
        clearBriefCache();
        showNotice(`已撤销：这场会又连回「${entry.name}」`);
      } else if (entry.kind === "deliverable") {
        await apiClient.removeDeliverable(entry.taskId, entry.deliverableId);
        showNotice(`已撤销：「${entry.name}」不再是「${entry.taskTitle}」的交付物`);
        // 展开的会马上重读，交付物小签跟着消失
        setFocusTick((tick) => tick + 1);
      } else if (entry.kind === "relation") {
        await apiClient.undoRelation(entry.relationId);
        recentAnswers?.drop(entry.relationId);
        showNotice("已撤销");
      } else if (entry.kind === "task") {
        await apiClient.updateTask(entry.taskId, entry.before);
        clearBriefCache();
        showNotice(
          entry.what === "edit"
            ? `已撤销：任务改回「${entry.before.title ?? entry.title}」`
            : `已撤销：任务「${entry.title}」搬回去了`,
        );
      }
      await changed();
    } catch (reason) {
      // 关联的撤销过期、已撤销过：原样显示服务端那句，用 warning（role="status"）
      const status = reason instanceof ApiError ? reason.status : 0;
      const told = entry.kind === "relation" && (status === 409 || status === 422);
      // 过了撤销期、已经撤销过：收成的那一行也撤不了了
      if (entry.kind === "relation" && status === 409) recentAnswers?.drop(entry.relationId);
      showNotice(errorText(reason, "撤销失败"), undefined, told ? "warning" : "error");
    } finally {
      setBusy(false);
    }
  };

  const undoChange = (meetingId: string) =>
    runUndo(
      undoStack.find((item) => item.kind === "project" && item.meetingId === meetingId) ?? {
        kind: "project",
        meetingId,
        until: new Date(Date.now() + 60_000).toISOString(),
      },
    );

  const liveUndo = undoStack.filter((item) => Date.parse(item.until) > clock);
  const lastUndo = liveUndo[liveUndo.length - 1] ?? null;
  const undoRef = useRef<() => void>(() => undefined);
  undoRef.current = () => {
    const now = Date.now();
    const entry = [...undoStack].reverse().find((item) => Date.parse(item.until) > now);
    if (entry) void runUndo(entry);
    else showNotice("没有能撤销的操作了（只保留 10 分钟内的）", undefined, "warning");
  };

  // ⌘Z / Ctrl+Z：撤销画布上最近一步；在输入框里时交给输入框自己
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key.toLowerCase() !== "z" || event.shiftKey || !(event.metaKey || event.ctrlKey)) return;
      if (event.isComposing) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest?.("input, textarea, select, [contenteditable='true']")) return;
      event.preventDefault();
      undoRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const dropMeeting = async (meetingId: string, target: DropTarget) => {
    if (busy || !graph) return;
    if (target.kind === "requirement") {
      const linked = graph.edges.some(
        (edge) => edge.kind === "discussion" && edge.meeting_id === meetingId && edge.requirement_id === target.requirementId,
      );
      if (linked) {
        showNotice(`已经关联过「${target.title}」了`, undefined, "warning");
        return;
      }
      setBusy(true);
      try {
        await apiClient.addRequirementMeeting(target.requirementId, meetingId);
        clearBriefCache();
        showNotice(`已关联到「${target.title}」`, {
          kind: "link",
          requirementId: target.requirementId,
          meetingId,
          title: target.title,
          until: localUndoUntil(),
        });
        await changed();
      } catch (reason) {
        showNotice(errorText(reason, "关联失败"), undefined, "error");
      } finally {
        setBusy(false);
      }
      return;
    }
    setBusy(true);
    rememberPositions();
    try {
      const detail = await apiClient.updateMeeting(meetingId, { project_id: target.projectId });
      const effects = detail.effects;
      const note = effects ? reassignNote(effects.tasks_moved, effects.tasks_left.length, effects.card) : "";
      const head = `已改到 ${target.name}`;
      showNotice(
        note ? `${head}；${note}` : head,
        effects?.undo_until ? { kind: "project", meetingId, until: effects.undo_until } : undefined,
      );
      if (selection === `m:${meetingId}`) {
        setTrail([]);
        onSelectionChange(null);
      }
      await changed();
    } catch (reason) {
      showNotice(errorText(reason, "改归属失败"), undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  const chooseWindow = (next: GraphWindow) => {
    writeGraphWindow(projectId, next);
    setFocus(null);
    setWindowChoice(next);
  };

  const onPanelKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
    if ((event.target as HTMLElement).closest("select, input, textarea")) return;
    select(null);
  };

  const openAsk = () => {
    select(null);
    setAskOpen(true);
  };
  // 问答显示期间出处里的会和文件点亮（key: "ask"），关掉时只清自己的
  const askHighlight = useCallback((ids: string[] | null) => {
    setHighlight((current) => {
      if (ids) return { key: "ask", ids: new Set(ids) };
      return current?.key === "ask" ? null : current;
    });
  }, []);
  const onAskKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
    // 焦点在输入框里时 Esc 不关
    if ((event.target as HTMLElement).closest("select, input, textarea")) return;
    setAskOpen(false);
  };

  // 画得下的会在原槽位留残影；太旧、窗口外的放在顶上一行
  const movedOut = (liveGraph?.moved_out ?? []).filter((item) => !layout?.byId.has(`g:${item.meeting_id}`));
  const dropProjects = useMemo(
    () =>
      projects
        .filter((project) => project.id !== projectId)
        .map((project) => ({ id: project.id, name: project.name, color: project.color })),
    [projectId, projects],
  );

  const shownFocus = focusData && focusData.meeting.id === expanded ? focusData : null;
  const localPayload = localData && localData.key === localKey ? localData.payload : null;
  let stage: ReactNode;
  if (local && !expanded) {
    const panelContext = liveGraph && layout && graph
      ? {
          apiClient,
          graph: liveGraph,
          layout,
          roots,
          selectedId: localSel ?? "",
          projects,
          canGoBack: false,
          version,
          player,
          playerNode: player.node,
          onBack: () => undefined,
          onClose: () => setLocalSel(null),
          onSelect: (id: string) => setLocalSel(id),
          onHighlight: () => undefined,
          onChanged: async () => {
            await changed();
            setLocalTick((tick) => tick + 1);
          },
          onNotice: showNotice,
          onAnswerDoorstep: () => undefined,
          onOpenMeeting,
          onOpenRequirement,
          onOpenGlossary,
          onOpenProject,
          onOpenFile: (file: PinnedFile) => changeLocal({ kind: "file", fileId: file.file_id }),
          onOpenLocal: (next: GraphLocal) => changeLocal(next),
          onOpenPreviewTarget,
          onOpenTask,
          onRelationAnswered: answered,
        }
      : null;
    stage = (
      <>
        <LocalGraphView
          error={localError}
          local={local}
          movedFolder={moved && local.kind === "file" && moved.fileId === local.fileId ? moved.folder : null}
          onBack={() => changeLocal(null)}
          onRecenter={(fileId) => changeLocal({ kind: "file", fileId })}
          onRetry={() => setLocalTick((tick) => tick + 1)}
          onSelect={setLocalSel}
          panelOpen={Boolean(localPayload && panelContext)}
          payload={localPayload}
          selectedId={localSel}
        />
        {localPayload && panelContext && (
          <div
            className="project-graph__panel"
            onKeyDown={(event) => {
              if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
              if ((event.target as HTMLElement).closest("select, input, textarea")) return;
              setLocalSel(null);
            }}
          >
            <LocalGraphPanel
              onClose={() => setLocalSel(null)}
              onExpandMeeting={(meetingId) => {
                changeLocal(null, { replace: true });
                setExpanded(meetingId);
              }}
              onOpenLocal={(next) => changeLocal(next)}
              onOpenTask={onOpenTask}
              onRecenter={(fileId) => changeLocal({ kind: "file", fileId })}
              onSelect={setLocalSel}
              panel={panelContext}
              payload={localPayload}
              selectedId={localSel}
            />
          </div>
        )}
      </>
    );
  } else if (expanded) {
    stage = (
      <>
        <MeetingFocusView
          error={focusError}
          focus={shownFocus}
          meetingId={expanded}
          onCollapse={() => setExpanded(null)}
          onExpand={(meetingId) => setExpanded(meetingId)}
          onOpenMeeting={onOpenMeeting}
          onOpenPreview={onOpenPreview}
          onRetry={() => setFocusTick((tick) => tick + 1)}
          onSelect={setFocusSel}
          panelOpen={Boolean(focusSel && shownFocus)}
          player={player}
          selectedId={focusSel}
          today={graph?.today ?? new Date().toISOString().slice(0, 10)}
        />
        {focusSel && shownFocus && (
          <div
            className="project-graph__panel"
            onKeyDown={(event) => {
              if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
              if ((event.target as HTMLElement).closest("select, input, textarea")) return;
              setFocusSel(null);
            }}
          >
            <FocusPanel
              apiClient={apiClient}
              focus={shownFocus}
              onChanged={changed}
              onClose={() => setFocusSel(null)}
              onNotice={showNotice}
              onOpenPreview={onOpenPreview}
              onOpenRequirement={onOpenRequirement}
              onSelect={setFocusSel}
              onTrace={canLocal ? (node) => changeLocal({ kind: "trace", node }) : undefined}
              player={player}
              selectedId={focusSel}
            />
          </div>
        )}
      </>
    );
  } else if (!graph || !layout) {
    stage = loadError ? (
      <div className="project-graph__empty" role="alert">
        <p>{loadError}</p>
        <button className="ghost-button" onClick={() => void load()} type="button">
          重试
        </button>
      </div>
    ) : (
      <div className="project-graph__empty">
        <p>正在画关系图…</p>
      </div>
    );
  } else {
    stage = (
      <>
        <GraphCanvas
          attention={attention}
          busy={busy}
          hideMentions={!lines.mention}
          onOpenFileLocal={canLocal ? (fileId) => changeLocal({ kind: "file", fileId }) : undefined}
          dropProjects={dropProjects}
          graph={liveGraph ?? graph}
          highlight={highlight?.ids ?? searchIds}
          layout={layout}
          onAnswerDoorstep={(answer) => void answerDoorstep(answer)}
          onDropMeeting={(meetingId, target) => void dropMeeting(meetingId, target)}
          onExpandMeeting={setExpanded}
          onNothingToDo={() => showNotice("这张图上没有要你处理的了")}
          onOpenRequirement={onOpenRequirement}
          onSelect={selectOrPin}
          onUndoGhost={(meetingId) => void undoChange(meetingId)}
          panelOpen={Boolean(resolved) || askOpen}
          previousPositions={previousPositions}
          roots={roots}
          selectedId={resolved}
          viewKey={projectId}
        />
        {askOpen && (
          <div className="project-graph__panel" onKeyDown={onAskKeyDown}>
            <div className="graph-panel project-graph__ask-panel">
              <ProjectAsk
                apiClient={apiClient}
                onClose={() => setAskOpen(false)}
                onHighlight={askHighlight}
                onOpenMeeting={(meetingId, seekMs, tab) =>
                  onOpenMeetingAt ? onOpenMeetingAt(meetingId, seekMs, tab) : onOpenMeeting(meetingId)
                }
                onOpenPreview={(target) =>
                  onOpenPreviewTarget ? onOpenPreviewTarget(target) : onOpenPreview?.(target.fileId, target.startMs)
                }
                player={player}
                projectId={projectId}
                projectName={graph.project.name}
                variant="panel"
              />
            </div>
          </div>
        )}
        {resolved && !askOpen && (
          <div className="project-graph__panel" onKeyDown={onPanelKeyDown}>
            <GraphPanel
              apiClient={apiClient}
              canGoBack={trail.length > 0}
              contextMeetingId={contextOnGraph}
              graph={liveGraph ?? graph}
              layout={layout}
              onAnswerDoorstep={(meetingId, target) => void answerDoorstep({ meetingId, projectId: target })}
              onBack={goBack}
              onChanged={changed}
              onClose={() => select(null)}
              onExpandMeeting={setExpanded}
              onHighlight={(ids) => setHighlight(ids ? { key: "panel", ids: new Set(ids) } : null)}
              onNotice={showNotice}
              onOpenAttributionReview={onOpenAttributionReview}
              onOpenGlossary={onOpenGlossary}
              onOpenMeeting={onOpenMeeting}
              onOpenProject={onOpenProject}
              onOpenRequirement={onOpenRequirement}
              onOpenFile={openFile}
              hiddenEdges={hiddenEdges}
              onOpenLocal={canLocal ? (next) => changeLocal(next) : undefined}
              onOpenPreviewTarget={onOpenPreviewTarget}
              onOpenTask={onOpenTask}
              onRelationAnswered={answered}
              onSelect={select}
              player={player}
              playerNode={player.node}
              projects={projects}
              roots={roots}
              selectedId={resolved}
              version={version}
            />
          </div>
        )}
      </>
    );
  }

  return (
    <section aria-label="项目关系图" className="project-graph page-content">
      {player.audioElement}
      <header className="project-graph__top">
        <nav aria-label="面包屑" className="project-graph__crumb">
          {/* 全部项目概览的路由在 App 里（#graph） */}
          <a href="#graph">全部项目</a>
          <span aria-hidden="true">/</span>
          <button onClick={onBack} type="button">
            项目管理
          </button>
          <span aria-hidden="true">/</span>
        </nav>
        <h1 className="project-graph__title">
          <i aria-hidden="true" style={{ background: graph?.project.color ?? "var(--muted)" }} />
          {graph?.project.name ?? projects.find((project) => project.id === projectId)?.name ?? ""}
        </h1>
        {graph && (
          <StatusLine
            active={highlight && highlight.key !== "panel" ? highlight.key : null}
            onToggle={togglePhrase}
            status={graph.status}
          />
        )}
        {(modeToggle || (canAsk && !expanded)) && (
          <span className="project-graph__mode">
            {canAsk && !expanded && (
              <button
                aria-pressed={askOpen}
                className="ghost-button project-graph__ask"
                onClick={() => (askOpen ? setAskOpen(false) : openAsk())}
                type="button"
              >
                问这个项目
              </button>
            )}
            {modeToggle}
          </span>
        )}
      </header>
      {movedOut.length > 0 && (
        <ul aria-label="刚移走的会" className="project-graph__moved">
          {movedOut.map((item) => (
            <li key={item.meeting_id}>
              刚把「{item.title}」改到 {item.to_project_name ?? "不归项目"}
              <button className="text-button" disabled={busy} onClick={() => void undoChange(item.meeting_id)} type="button">
                撤销
              </button>
            </li>
          ))}
        </ul>
      )}
      <NoticeBanner className="project-graph__notice" notice={notice} onDismiss={dismissNotice}>
        {/* 提示自带按钮（像是新项目 / 新需求的［撤销］）时，不再另给一个撤销最近一步的 */}
        {lastUndo && notice?.tone === "success" && !notice.actions && (
          <button className="text-button action-banner__undo" disabled={busy} onClick={() => void runUndo(lastUndo)} type="button">
            撤销
          </button>
        )}
      </NoticeBanner>
      <div
        className={`project-graph__stage${
          (local && !expanded ? localPayload : expanded ? focusSel : resolved || askOpen) ? " has-panel" : ""
        }`}
      >
        {stage}
      </div>
      {expanded ? (
        <footer className="project-graph__bottom">
          <span className="project-graph__legend">
            录音条上面是定了什么，下面是任务，按说到的时间对齐；点条上任意处从那里播，← → 换到上一场、下一场，Esc 回到关系图
          </span>
        </footer>
      ) : (
        <footer className="project-graph__bottom">
          <div aria-label="时间窗" className="project-graph__windows" role="group">
            {WINDOW_OPTIONS.map((option) => (
              <button
                aria-pressed={graph?.window.effective === option.key}
                key={option.key}
                onClick={() => chooseWindow(option.key)}
                type="button"
              >
                {option.label}
              </button>
            ))}
          </div>
          {graph?.window.widened_reason && <span className="project-graph__widened">{graph.window.widened_reason}</span>}
          {graph && <WeeklyBars weekly={graph.weekly} />}
          {liveGraph && layout && (
            <GraphSearch
              apiClient={apiClient}
              graph={liveGraph}
              layout={layout}
              onHighlight={(ids) => setSearchIds(ids ? new Set(ids) : null)}
              onSelect={select}
            />
          )}
          <div aria-label="连线" className="project-graph__lines" role="group">
            <span className="project-graph__lines-label">连线</span>
            <button aria-pressed={lines.mention} onClick={() => toggleLines({ mention: !lines.mention })} type="button">
              提到
            </button>
            {relatedAvailable && (
              <button
                aria-pressed={lines.related}
                onClick={() => toggleLines({ related: !lines.related })}
                title={lines.related ? undefined : RELATED_OFF_TITLE}
                type="button"
              >
                相关
              </button>
            )}
          </div>
          {relatedOn && relatedState === "ready" && !liveGraph?.edges.some((edge) => edge.kind === "related") && (
            <span className="project-graph__sync">{RELATED_EMPTY_TEXT}</span>
          )}
          {relatedOn && relatedState === "failed" && (
            <span className="project-graph__sync is-error">
              {RELATED_FAILED_TEXT}
              <button className="text-button" onClick={() => setRelatedTick((tick) => tick + 1)} type="button">
                重试
              </button>
            </span>
          )}
          {relatedOn && relatedState === "old" && <span className="project-graph__sync is-error">{OLD_BACKEND_TEXT}</span>}
          <Legend />
          {loading && graph && !graphCache.has(key) && <span className="project-graph__sync">正在换时间窗…</span>}
          {loadError && graph && <span className="project-graph__sync is-error">刷新失败：{loadError}</span>}
        </footer>
      )}
    </section>
  );
}
