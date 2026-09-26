import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import { reassignNote } from "../../cardCopy";
import type { Project } from "../../types";
import { NoticeBanner, useNotice, type NoticeTone } from "../Notice";
import { GraphCanvas, forgetGraphViews, type DoorstepAnswer } from "./GraphCanvas";
import { GraphPanel, clearBriefCache, type GraphNoticeUndo } from "./GraphPanel";
import type { GraphPayload, GraphRootsPayload, GraphWindow, StatusPhrase } from "./graphTypes";
import { readGraphWindow, recordGraphOpen, writeGraphWindow } from "./graphPrefs";
import { layoutStarMap, type StarLayout } from "./layout";
import { useMiniPlayer } from "./MiniPlayer";
import "./ProjectGraph.css";

/** 带［撤销］的提示多停一会儿，和会议页一致 */
export const UNDO_NOTICE_MS = 10_000;
/** 画布开着时每 30 秒对一次数据；没变化时服务器回 304，几乎不花钱 */
const REFRESH_MS = 30_000;
const TRAIL_MAX = 5;

const WINDOW_OPTIONS: Array<{ key: GraphWindow; label: string }> = [
  { key: "7d", label: "7 天" },
  { key: "28d", label: "28 天" },
  { key: "90d", label: "90 天" },
  { key: "all", label: "全部" },
];

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
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const [previousPositions, setPreviousPositions] = useState<Map<string, { x: number; y: number }> | undefined>();
  const [undo, setUndo] = useState<GraphNoticeUndo | null>(null);
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

  const layout = useMemo(() => (graph ? layoutStarMap(graph) : null), [graph]);

  const resolved = graph && layout && selection ? resolveSelection(graph, layout, selection) : null;

  // 深链目标不在图上（需求已结束、会被折叠之外）：说一声，不留空面板
  useEffect(() => {
    if (!graph || !layout || !selection) return;
    if (resolved === selection) return;
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
  }, [graph, layout, onSelectionChange, resolved, selection, setNotice]);

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
      setUndo(undoTarget ?? null);
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
      showNotice(note ? `${head}；${note}` : head, effects?.undo_until ? { meetingId, until: effects.undo_until } : undefined);
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

  const undoChange = async (meetingId: string) => {
    if (busy) return;
    setBusy(true);
    rememberPositions();
    try {
      const detail = await apiClient.undoMeetingProject(meetingId);
      showNotice(detail.effects?.card?.action === "moved" ? "已撤销刚才的改动，会议卡片也搬回去了" : "已撤销刚才的改动");
      await changed();
    } catch (reason) {
      showNotice(errorText(reason, "撤销失败"), undefined, "error");
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

  const now = Date.now();
  const movedOut = (graph?.moved_out ?? []).filter((item) => Date.parse(item.undo_until) > now);
  const undoOpen = undo && Date.parse(undo.until) > now;

  let stage: ReactNode;
  if (!graph || !layout) {
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
          busy={busy}
          graph={graph}
          highlight={highlight?.ids ?? null}
          layout={layout}
          onAnswerDoorstep={(answer) => void answerDoorstep(answer)}
          onSelect={select}
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
              graph={graph}
              layout={layout}
              onAnswerDoorstep={(meetingId, target) => void answerDoorstep({ meetingId, projectId: target })}
              onBack={goBack}
              onChanged={changed}
              onClose={() => select(null)}
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
        {undoOpen && notice?.tone === "success" && (
          <button className="text-button action-banner__undo" disabled={busy} onClick={() => void undoChange(undo.meetingId)} type="button">
            撤销
          </button>
        )}
      </NoticeBanner>
      <div className={`project-graph__stage${resolved ? " has-panel" : ""}`}>{stage}</div>
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
        <span className="project-graph__legend">位置按类型和时间排：左会议 · 右材料 · 上需求 · 下线索词，越靠中心越新</span>
        {loading && graph && !graphCache.has(key) && <span className="project-graph__sync">正在换时间窗…</span>}
        {loadError && graph && <span className="project-graph__sync is-error">刷新失败：{loadError}</span>}
      </footer>
    </section>
  );
}
