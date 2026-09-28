/*
 * 全部项目概览（2c）：左侧导航「关系图」打开的页面。取数（ETag、30 秒对一次）、深链（#graph?sel=）、提示条、
 * 取径器和认领框在这里；画布是 OverviewCanvas，右侧面板是 OverviewPanel。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import type { Project, UnclaimedFolder } from "../../types";
import { pollWhileChecking } from "../checkingPoll";
import { ClaimFoldersDialog } from "../ClaimFoldersDialog";
import { MaterialRootPickerModal } from "../MaterialRootPickerModal";
import { NoticeBanner, UNDO_NOTICE_MS, useNotice, type NoticeAction, type NoticeTone } from "../Notice";
import { PROJECT_PARENT_PICKER } from "../ProjectParentRow";
import type { GraphWindow } from "./graphTypes";
import { readGraphWindow, writeGraphWindow } from "./graphPrefs";
import { ISLANDS_MORE_ID, layoutOverview, overviewAttention } from "./layoutOverview";
import { OverviewCanvas, PANEL_KINDS } from "./OverviewCanvas";
import { OverviewPanel } from "./OverviewPanel";
import type { GraphOverview, OverviewFolders } from "./overviewTypes";
import { forgetViewportViews } from "./useGraphViewport";
import "./ProjectGraph.css";
import "./OverviewGraph.css";

/** 画布开着时每 30 秒对一次数据；没变化时服务器回 304 */
const REFRESH_MS = 30_000;
/** 带［撤销］的提示多停一会儿，和项目图一致 */
/** 时间窗偏好和项目图存在一处，键是 overview */
const PREF_KEY = "overview";
const DEFAULT_WINDOW: GraphWindow = "28d";

const WINDOW_OPTIONS: Array<{ key: GraphWindow; label: string }> = [
  { key: "7d", label: "7 天" },
  { key: "28d", label: "28 天" },
  { key: "90d", label: "90 天" },
  { key: "all", label: "全部" },
];

// 按时间窗缓存一份（连同 etag）：切回来先画旧数据，再带 If-None-Match 对一次
const overviewCache = new Map<GraphWindow, { overview: GraphOverview; etag: string | null }>();
let foldersCache: OverviewFolders | null = null;

/** 测试之间清空模块级缓存 */
export function forgetOverviewCache() {
  overviewCache.clear();
  foldersCache = null;
  forgetViewportViews();
}

function errorText(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export interface OverviewGraphProps {
  apiClient: ApiClient;
  projects: Project[];
  /** 地址栏里的选中（#graph?sel=p:<id>）；null 表示没选 */
  selection: string | null;
  onSelectionChange: (id: string | null) => void;
  /** 双击岛、面板里的［打开项目图］ */
  onOpenProjectGraph: (projectId: string) => void;
  onOpenProject: (projectId: string) => void;
  onOpenMeeting: (meetingId: string) => void;
  onOpenRequirement: (requirementId: string) => void;
  /** none：资料库里没归项目的会；new_project：「像新项目」筛选 */
  onOpenLibrary: (filter: "none" | "new_project") => void;
  onProjectsChanged?: () => void | Promise<void>;
}

export function OverviewGraph({
  apiClient,
  projects,
  selection,
  onSelectionChange,
  onOpenProjectGraph,
  onOpenProject,
  onOpenMeeting,
  onOpenRequirement,
  onOpenLibrary,
  onProjectsChanged,
}: OverviewGraphProps) {
  const [windowChoice, setWindowChoice] = useState<GraphWindow>(() => readGraphWindow(PREF_KEY) ?? DEFAULT_WINDOW);
  const [overview, setOverview] = useState<GraphOverview | null>(() => overviewCache.get(windowChoice)?.overview ?? null);
  const [folders, setFolders] = useState<OverviewFolders | null>(() => foldersCache);
  const [foldersFailed, setFoldersFailed] = useState(false);
  const [foldersTick, setFoldersTick] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);
  // 作答后先藏起来的行和节点，等重取回来的数据里自然没有了
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerError, setPickerError] = useState("");
  const [parentBusy, setParentBusy] = useState(false);
  const [claimList, setClaimList] = useState<UnclaimedFolder[] | null>(null);
  const { notice, setNotice, dismissNotice } = useNotice();
  const requestRef = useRef(0);
  const missingRef = useRef<string | null>(null);
  // 在图上出现过的选中：作答后它从图上消失时悄悄收起面板，不当成找不到
  const shownRef = useRef<string | null>(null);
  // 刚设好总文件夹：下面的文件夹列出来后有没挂的就弹认领框
  const claimAfterRef = useRef(false);
  // 最近一条提示里的［撤销］：⌘Z 也能撤
  const undoRef = useRef<(() => void) | null>(null);
  const canLoad = typeof apiClient.getGraphOverview === "function";

  const load = useCallback(async () => {
    if (!canLoad) return;
    const token = ++requestRef.current;
    setLoading(true);
    try {
      const cached = overviewCache.get(windowChoice);
      let result = await apiClient.getGraphOverview(windowChoice, cached?.etag ?? null);
      // 304 却没有手上的那份（缓存刚清掉）：不带 etag 再取一次
      if (!result.overview && !cached) result = await apiClient.getGraphOverview(windowChoice, null);
      const next = result.overview ?? cached?.overview;
      if (!next) throw new Error("关系图读取失败");
      overviewCache.set(windowChoice, { overview: next, etag: result.etag });
      if (token !== requestRef.current) return;
      // 304 时还是同一个对象，画布不重画
      setOverview(next);
      setLoadError("");
    } catch (reason) {
      if (token !== requestRef.current) return;
      setLoadError(errorText(reason, "关系图读取失败"));
    } finally {
      if (token === requestRef.current) setLoading(false);
    }
  }, [apiClient, canLoad, windowChoice]);

  // 换时间窗：有缓存先画缓存，没有就留着旧图等新数据
  useEffect(() => {
    const cached = overviewCache.get(windowChoice);
    if (cached) setOverview(cached.overview);
    void load();
  }, [load, windowChoice]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void load();
      setFoldersTick((tick) => tick + 1);
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // 没挂的文件夹走单独的接口（读缓存）：还在看时每 2 秒再问
  useEffect(() => {
    if (typeof apiClient.getOverviewFolders !== "function") {
      setFoldersFailed(true);
      return;
    }
    return pollWhileChecking(
      () => apiClient.getOverviewFolders(),
      (value) => value.state === "checking",
      (value) => {
        foldersCache = value;
        setFolders(value);
        setFoldersFailed(false);
        if (claimAfterRef.current && value.state !== "checking") {
          claimAfterRef.current = false;
          if (value.state === "ready" && value.folders.length > 0) void openClaim();
        }
      },
      // 右手那一列读不到就空着，不打扰
      { onError: () => setFoldersFailed(true) },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, foldersTick]);

  // 作答后先藏起来的名字和文件夹：画布上立刻去掉，不等重取
  const shownOverview = useMemo(() => {
    if (!overview) return null;
    const ghosts = overview.suggested_projects.filter((item) => !hidden.has(`np:${item.key}`));
    return ghosts.length === overview.suggested_projects.length ? overview : { ...overview, suggested_projects: ghosts };
  }, [hidden, overview]);
  const shownFolders = useMemo(() => {
    if (!folders) return null;
    const rest = folders.folders.filter((folder) => !hidden.has(`fd:${folder.path}`));
    return rest.length === folders.folders.length ? folders : { ...folders, folders: rest };
  }, [folders, hidden]);

  const layout = useMemo(
    () => (shownOverview ? layoutOverview(shownOverview, shownFolders) : null),
    [shownFolders, shownOverview],
  );
  const attention = useMemo(() => (layout ? overviewAttention(layout) : []), [layout]);

  // 深链目标：折进「其余 N 个项目」的项目选中那个节点
  let resolved: string | null = null;
  if (layout && shownOverview && selection) {
    if (layout.byId.has(selection)) resolved = selection;
    else if (selection.startsWith("p:") && shownOverview.islands_more?.project_ids.includes(selection.slice(2))) {
      resolved = ISLANDS_MORE_ID;
    }
  }
  const selectedNode = resolved ? layout?.byId.get(resolved) ?? null : null;
  const panelNode = selectedNode && PANEL_KINDS.has(selectedNode.kind) ? selectedNode : null;

  useEffect(() => {
    if (resolved) shownRef.current = resolved;
  }, [resolved]);

  // 深链目标不在图上：说一声，不留空面板；刚作答掉的就悄悄收起
  useEffect(() => {
    if (!layout || !selection) return;
    if (resolved === selection) return;
    // 文件夹那一列晚一步到，先等一等
    if ((selection.startsWith("fd:") || selection.startsWith("folders:")) && !folders && !foldersFailed) return;
    if (resolved) {
      onSelectionChange(resolved);
      return;
    }
    if (shownRef.current === selection) {
      shownRef.current = null;
      onSelectionChange(null);
      return;
    }
    if (missingRef.current === selection) return;
    missingRef.current = selection;
    setNotice("要看的节点不在当前的图上，换个时间窗试试", "warning");
    onSelectionChange(null);
  }, [folders, foldersFailed, layout, onSelectionChange, resolved, selection, setNotice]);

  const showNotice = useCallback(
    (message: string, tone: NoticeTone = "success", actions?: NoticeAction[]) => {
      setNotice(message, tone, actions?.length ? UNDO_NOTICE_MS : undefined, actions);
      const undo = actions?.find((action) => action.label === "撤销");
      if (undo) undoRef.current = undo.onClick;
      else if (tone === "success") undoRef.current = null;
    },
    [setNotice],
  );

  // ⌘Z / Ctrl+Z：撤销最近一条提示里的操作；在输入框里时交给输入框自己
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key.toLowerCase() !== "z" || event.shiftKey || !(event.metaKey || event.ctrlKey)) return;
      if (event.isComposing) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest?.("input, textarea, select, [contenteditable='true']")) return;
      event.preventDefault();
      const undo = undoRef.current;
      undoRef.current = null;
      if (undo) undo();
      else setNotice("没有能撤销的操作了", "warning");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setNotice]);

  const changed = useCallback(async () => {
    setVersion((current) => current + 1);
    setFoldersTick((tick) => tick + 1);
    await load();
    await onProjectsChanged?.();
  }, [load, onProjectsChanged]);

  const hide = useCallback((key: string) => {
    setHidden((current) => new Set(current).add(key));
  }, []);

  const unhide = useCallback((key: string) => {
    setHidden((current) => {
      const next = new Set(current);
      next.delete(key);
      return next;
    });
  }, []);

  /** 认领框要全部没挂的文件夹（概览只带 12 个）：从项目总文件夹接口取 */
  const openClaim = async () => {
    let list = foldersCache?.folders ?? [];
    if (typeof apiClient.projectParent === "function") {
      try {
        const status = await apiClient.projectParent();
        if (status.unclaimed.folders.length > 0) list = status.unclaimed.folders;
      } catch {
        // 读不到就用手上的这 12 个
      }
    }
    if (list.length > 0) setClaimList(list);
  };

  const saveParent = async (path: string, fromPicker: boolean) => {
    if (typeof apiClient.setProjectParent !== "function") return;
    setParentBusy(true);
    setPickerError("");
    try {
      await apiClient.setProjectParent(path);
      setPickerOpen(false);
      claimAfterRef.current = true;
      if (selection === "folders:hint") onSelectionChange(null);
      setFoldersTick((tick) => tick + 1);
    } catch (reason) {
      const message = errorText(reason, "没设成，请稍后重试");
      // 取径器还开着就在取径器里说
      if (fromPicker) setPickerError(message);
      else showNotice(message, "error");
    } finally {
      setParentBusy(false);
    }
  };

  const openPicker = () => {
    setPickerError("");
    setPickerOpen(true);
  };

  const chooseWindow = (next: GraphWindow) => {
    writeGraphWindow(PREF_KEY, next);
    setWindowChoice(next);
  };

  const onPanelKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
    if ((event.target as HTMLElement).closest("select, input, textarea")) return;
    onSelectionChange(null);
  };

  const parentUnset = folders?.state === "unset";
  let stage: ReactNode;
  if (!canLoad) {
    stage = (
      <div className="project-graph__empty">
        <p>这个版本的服务还没有全部项目概览</p>
      </div>
    );
  } else if (!shownOverview || !layout) {
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
        <OverviewCanvas
          attention={attention}
          layout={layout}
          onNothingToDo={() => showNotice("这张图上没有要你处理的了")}
          onOpenIsland={onOpenProjectGraph}
          onOpenMoreFolders={() => void openClaim()}
          onOpenMoreNames={() => onOpenLibrary("new_project")}
          onPickParent={openPicker}
          onSelect={onSelectionChange}
          overview={shownOverview}
          panelOpen={Boolean(panelNode)}
          parentUnset={parentUnset}
          selectedId={resolved}
        />
        {panelNode && (
          <div className="project-graph__panel" onKeyDown={onPanelKeyDown}>
            <OverviewPanel
              apiClient={apiClient}
              hidden={hidden}
              node={panelNode}
              onChanged={changed}
              onClose={() => onSelectionChange(null)}
              onHide={hide}
              onNotice={showNotice}
              onOpenLibrary={onOpenLibrary}
              onOpenMeeting={onOpenMeeting}
              onOpenProject={onOpenProject}
              onOpenProjectGraph={onOpenProjectGraph}
              onOpenRequirement={onOpenRequirement}
              onPickParent={openPicker}
              onSelect={onSelectionChange}
              onUnhide={unhide}
              onUseSuggestedParent={(path) => void saveParent(path, false)}
              overview={shownOverview}
              parentBusy={parentBusy}
              projects={projects}
              version={version}
            />
          </div>
        )}
      </>
    );
  }

  return (
    <section aria-label="全部项目关系图" className="project-graph overview-graph page-content">
      <header className="project-graph__top">
        <h1 className="project-graph__title">全部项目</h1>
      </header>
      <NoticeBanner className="project-graph__notice" notice={notice} onDismiss={dismissNotice} />
      <div className={`project-graph__stage${panelNode ? " has-panel" : ""}`}>{stage}</div>
      <footer className="project-graph__bottom">
        <div aria-label="时间窗" className="project-graph__windows" role="group">
          {WINDOW_OPTIONS.map((option) => (
            <button
              aria-pressed={(overview?.window.effective ?? windowChoice) === option.key}
              key={option.key}
              onClick={() => chooseWindow(option.key)}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
        <span
          className="project-graph__legend"
          title="左边是没归项目的会和像新项目的名字，中间是项目（按建立先后），右边是还没挂到项目的文件夹；双击项目进项目图"
        >
          左边是没归项目的会和像新项目的名字，中间是项目（按建立先后），右边是还没挂到项目的文件夹；双击项目进项目图
        </span>
        {loading && overview && !overviewCache.has(windowChoice) && <span className="project-graph__sync">正在换时间窗…</span>}
        {loadError && overview && <span className="project-graph__sync is-error">刷新失败：{loadError}</span>}
      </footer>

      {pickerOpen && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={parentBusy}
          description={PROJECT_PARENT_PICKER.description}
          error={pickerError}
          onClose={() => setPickerOpen(false)}
          onConfirm={(path) => void saveParent(path, true)}
          title={PROJECT_PARENT_PICKER.title}
        />
      )}

      {claimList && (
        <ClaimFoldersDialog
          apiClient={apiClient}
          folders={claimList}
          onChanged={changed}
          onClose={(summary) => {
            setClaimList(null);
            if (summary) setNotice(summary, "success", 12_000);
            setFoldersTick((tick) => tick + 1);
          }}
        />
      )}
    </section>
  );
}
