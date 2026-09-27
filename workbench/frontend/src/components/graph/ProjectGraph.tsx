import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import { reassignNote } from "../../cardCopy";
import type { Project } from "../../types";
import { NoticeBanner, useNotice, type NoticeTone } from "../Notice";
import { GraphCanvas, forgetGraphViews, type DoorstepAnswer, type DropTarget } from "./GraphCanvas";
import { FocusPanel } from "./FocusPanel";
import { GraphPanel, clearBriefCache } from "./GraphPanel";
import { GraphSearch } from "./GraphSearch";
import { localUndoUntil, type GraphNoticeUndo } from "./panelParts";
import type { GraphPayload, GraphRootsPayload, GraphWindow, MeetingFocus, StatusPhrase } from "./graphTypes";
import { readGraphWindow, recordGraphOpen, writeGraphWindow } from "./graphPrefs";
import { attentionOrder, layoutStarMap, type StarLayout } from "./layout";
import { MeetingFocusView } from "./MeetingFocusView";
import { useMiniPlayer } from "./MiniPlayer";
import "./ProjectGraph.css";

/** 带［撤销］的提示多停一会儿，和会议页一致 */
export const UNDO_NOTICE_MS = 10_000;
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
      const id = `sub:${folder.root_id}:${recent.dir}`;
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

// 按（项目，时间窗，深链目标）缓存一份：切回画布先画旧数据，再到后台对一次
const graphCache = new Map<string, GraphPayload>();
const rootsCache = new Map<string, GraphRootsPayload>();

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
  forgetGraphViews();
  clearBriefCache();
}

function sameUndo(a: GraphNoticeUndo, b: GraphNoticeUndo) {
  if (a.kind === "project" && b.kind === "project") return a.meetingId === b.meetingId;
  if ((a.kind === "link" || a.kind === "unlink") && a.kind === b.kind) {
    return a.meetingId === b.meetingId && a.requirementId === b.requirementId;
  }
  if (a.kind === "task" && b.kind === "task") return a.taskId === b.taskId;
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
}: ProjectGraphProps) {
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
  // 展开时面板里选中的决议（dec:<下标>）、任务（task:<id>）或「+N」；不进地址栏
  const [focusSel, setFocusSel] = useState<string | null>(null);
  const [focusData, setFocusData] = useState<MeetingFocus | null>(null);
  const [focusError, setFocusError] = useState("");
  const [focusTick, setFocusTick] = useState(0);
  const { notice, setNotice, dismissNotice } = useNotice();
  const player = useMiniPlayer();
  const requestRef = useRef(0);
  const missingRef = useRef<string | null>(null);

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
    } catch (reason) {
      if (token !== requestRef.current) return;
      setLoadError(errorText(reason, "关系图读取失败"));
    } finally {
      if (token === requestRef.current) setLoading(false);
    }
  }, [apiClient, focus, projectId, windowChoice]);

  // 换时间窗：有缓存先画缓存，没有就留着旧图等新数据
  useEffect(() => {
    const cached = cachedGraph(projectId, windowChoice, focus);
    if (cached) setGraph(cached);
    void load();
  }, [key, load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void load();
      setRootsTick((tick) => tick + 1);
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

  // 残影只在撤销期内画；最早的一个到期时重画一次
  const liveGraph = useMemo(() => {
    if (!graph) return null;
    const movedOut = graph.moved_out.filter((item) => Date.parse(item.undo_until) > clock);
    return withSubfolders(movedOut.length === graph.moved_out.length ? graph : { ...graph, moved_out: movedOut }, roots);
  }, [clock, graph, roots]);

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
  const attention = useMemo(() => (layout ? attentionOrder(layout) : []), [layout]);

  const resolved = liveGraph && layout && selection ? resolveSelection(liveGraph, layout, selection) : null;

  // 深链目标不在图上（需求已结束、会被折叠之外）：说一声，不留空面板
  useEffect(() => {
    if (!graph || !layout || !selection) return;
    if (resolved === selection) return;
    // 子文件夹跟着资料盘状态一起到，先等一等
    if (selection.startsWith("sub:") && !roots) return;
    if (resolved) {
      onSelectionChange(resolved);
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
  }, [graph, layout, onSelectionChange, resolved, roots, selection, setNotice]);

  const select = useCallback(
    (id: string | null) => {
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
    (message: string, undoTarget?: GraphNoticeUndo, tone: NoticeTone = "success") => {
      setNotice(message, tone, undoTarget ? UNDO_NOTICE_MS : undefined);
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
      } else {
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
      showNotice(errorText(reason, "撤销失败"), undefined, "error");
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
  let stage: ReactNode;
  if (expanded) {
    stage = (
      <>
        <MeetingFocusView
          error={focusError}
          focus={shownFocus}
          meetingId={expanded}
          onCollapse={() => setExpanded(null)}
          onExpand={(meetingId) => setExpanded(meetingId)}
          onOpenMeeting={onOpenMeeting}
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
              onOpenRequirement={onOpenRequirement}
              onSelect={setFocusSel}
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
          dropProjects={dropProjects}
          graph={liveGraph ?? graph}
          highlight={highlight?.ids ?? searchIds}
          layout={layout}
          onAnswerDoorstep={(answer) => void answerDoorstep(answer)}
          onDropMeeting={(meetingId, target) => void dropMeeting(meetingId, target)}
          onExpandMeeting={setExpanded}
          onNothingToDo={() => showNotice("这张图上没有要你处理的了")}
          onOpenRequirement={onOpenRequirement}
          onSelect={select}
          onUndoGhost={(meetingId) => void undoChange(meetingId)}
          panelOpen={Boolean(resolved)}
          previousPositions={previousPositions}
          roots={roots}
          selectedId={resolved}
          viewKey={projectId}
        />
        {resolved && (
          <div className="project-graph__panel" onKeyDown={onPanelKeyDown}>
            <GraphPanel
              apiClient={apiClient}
              canGoBack={trail.length > 0}
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
            onToggle={(phrase) => setHighlight(phrase ? { key: phrase.text, ids: new Set(phrase.node_ids) } : null)}
            status={graph.status}
          />
        )}
        {modeToggle && <span className="project-graph__mode">{modeToggle}</span>}
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
        {lastUndo && notice?.tone === "success" && (
          <button className="text-button action-banner__undo" disabled={busy} onClick={() => void runUndo(lastUndo)} type="button">
            撤销
          </button>
        )}
      </NoticeBanner>
      <div className={`project-graph__stage${(expanded ? focusSel : resolved) ? " has-panel" : ""}`}>{stage}</div>
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
          <span className="project-graph__legend" title="位置按类型和时间排：左会议 · 右材料 · 上需求 · 下线索词，越靠中心越新">
            位置按类型和时间排：左会议 · 右材料 · 上需求 · 下线索词，越靠中心越新
          </span>
          {loading && graph && !graphCache.has(key) && <span className="project-graph__sync">正在换时间窗…</span>}
          {loadError && graph && <span className="project-graph__sync is-error">刷新失败：{loadError}</span>}
        </footer>
      )}
    </section>
  );
}
