import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, type ApiClient } from "./api";
import type {
  AttentionPayload,
  AttributionSummary,
  HealthPayload,
  Job,
  LoadState,
  MeetingDetail,
  MeetingFilters,
  MeetingSummary,
  Project,
  SearchPayload,
  Tag,
} from "./types";
import { AppShell, type AppView } from "./components/AppShell";
import { AsyncState } from "./components/AsyncState";
import { FadeContent } from "./components/motion/FadeContent";
import { MagneticButton } from "./components/motion/MagneticButton";
import { GlossaryPage } from "./components/GlossaryPage";
import { JobsPage } from "./components/JobsPage";
import { LibraryPage } from "./components/LibraryPage";
import { MeetingDetailPage } from "./components/MeetingDetailPage";
import { OverviewPage } from "./components/OverviewPage";
import { ProjectDetailPage } from "./components/ProjectDetailPage";
import { ProjectGraph } from "./components/graph/ProjectGraph";
import { ViewModeToggle } from "./components/graph/ViewModeToggle";
import { readProjectMode, writeProjectMode, type ProjectViewMode } from "./components/graph/graphPrefs";
import { ProjectsPage } from "./components/ProjectsPage";
import { RequirementDetailPage } from "./components/RequirementDetailPage";
import { RequirementsPage } from "./components/RequirementsPage";
import { SearchPage } from "./components/SearchPage";
import { TaskDrawer } from "./components/TaskDrawer";
import { TasksPage } from "./components/TasksPage";
import { uploadRecordingInChunks } from "./upload";

interface AppProps {
  apiClient?: ApiClient;
}

export const MOBILE_READ_ONLY_QUERY = "(max-width: 767px), (pointer: coarse)";
export const MEETING_PAGE_SIZE = 50;

const terminalJobStates = new Set([
  "completed_unreviewed",
  "draft_modified",
  "published",
  "failed",
  "cancelled",
  "interrupted",
]);

export function useMobileBreakpoint() {
  const [isMobile, setIsMobile] = useState(() =>
    typeof window === "undefined"
      ? false
      : window.matchMedia(MOBILE_READ_ONLY_QUERY).matches,
  );
  useEffect(() => {
    const media = window.matchMedia(MOBILE_READ_ONLY_QUERY);
    const update = () => setIsMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return isMobile;
}

// 会议详情页「← 返回」按钮上显示的去处：打开会议前所在的视图。
const VIEW_LABELS: Record<AppView, string> = {
  overview: "工作台",
  library: "录音档案",
  requirements: "需求池",
  requirementDetail: "需求详情",
  tasks: "任务池",
  glossary: "词典",
  jobs: "转写录音",
  projects: "项目管理",
  projectDetail: "项目详情",
};

/**
 * 项目详情的地址：清单是 #projects/<id>，关系图是 #projects/<id>/graph，选中节点时带 ?sel=m:<id>，
 * 展开一场会时带 expand=<会议 id>
 */
function projectGraphPath(projectId: string, graph: boolean, selection: string | null, expanded: string | null = null) {
  if (!graph) return `#projects/${projectId}`;
  const params = [
    expanded ? `expand=${encodeURIComponent(expanded)}` : "",
    selection ? `sel=${selection.split(":").map(encodeURIComponent).join(":")}` : "",
  ].filter(Boolean);
  return `#projects/${projectId}/graph${params.length ? `?${params.join("&")}` : ""}`;
}

function expandParam(hash: string) {
  const query = hash.split("?")[1];
  return query ? new URLSearchParams(query).get("expand") : null;
}

export default function App({ apiClient = api }: AppProps) {
  const isMobile = useMobileBreakpoint();
  // 落地页是工作台（最近的会、待确认任务、处理中的录音）；按日期回忆某场会走侧栏「录音档案」。
  const [view, setView] = useState<AppView>("overview");
  // 冷加载时地址栏里的 #tasks 等锚点要先被读进视图，之后才允许把视图反写回地址栏，
  // 否则首帧 view=overview 会先把 hash 清空，applyHash 再也读不到（冷加载 #tasks 被拉回工作台）。
  const hashReadyRef = useRef(false);
  // 这一轮视图变化来自浏览器前进/后退（或冷加载），地址栏已经是对的，只能 replace 不能再 push，
  // 否则每按一次后退都会多压一条历史，后退键永远退不出去。
  const historySyncRef = useRef(false);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [healthUnreachable, setHealthUnreachable] = useState(false);
  const healthFailureCount = useRef(0);
  const [meetings, setMeetings] = useState<MeetingSummary[]>([]);
  const [meetingOffset, setMeetingOffset] = useState(0);
  const [meetingTotal, setMeetingTotal] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [openProjectId, setOpenProjectId] = useState<string | null>(null);
  // 项目详情看关系图还是清单；关系图里选中的节点（地址栏 ?sel=m:<id>）和深链目标
  const [projectMode, setProjectMode] = useState<ProjectViewMode>("list");
  const [graphSelection, setGraphSelection] = useState<string | null>(null);
  const [graphFocus, setGraphFocus] = useState<string | null>(null);
  // 关系图里展开的那场会；展开压一条历史，后退键收起
  const [graphExpanded, setGraphExpanded] = useState<string | null>(null);
  // 从关系图点进需求页时，面包屑写「关系图」，返回回到画布
  const [requirementFromGraph, setRequirementFromGraph] = useState<{ projectId: string; selection: string } | null>(null);
  const [openRequirementId, setOpenRequirementId] = useState<string | null>(null);
  // 从项目详情页跳进词典时预选中的项目 chip；普通侧栏导航进词典时为 null（不预筛）。
  const [glossaryProjectId, setGlossaryProjectId] = useState<string | null>(null);
  const [taskDrawerId, setTaskDrawerId] = useState<string | null>(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [glossaryPending, setGlossaryPending] = useState(0);
  const [mobileTaskWrite, setMobileTaskWrite] = useState(true);
  const [boardVersion, setBoardVersion] = useState(0);
  const [filters, setFilters] = useState<MeetingFilters>({});
  const [libraryState, setLibraryState] = useState<LoadState>("loading");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsAvailable, setJobsAvailable] = useState(false);
  const [attention, setAttention] = useState<AttentionPayload | null>(null);
  const [attributionSummary, setAttributionSummary] = useState<AttributionSummary | null>(null);
  const [jobsState, setJobsState] = useState<LoadState>("loading");
  const [jobsMessage, setJobsMessage] = useState("");
  const [jobsStale, setJobsStale] = useState(false);
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  // 正在打开（或已打开）的会议。detail 要等接口回来才有，地址栏 #meetings/<id> 以它为准。
  const [openMeetingId, setOpenMeetingId] = useState<string | null>(null);
  // applyHash 挂在 popstate 上，只能经 ref 读到最新的会议状态和处理函数。
  const openMeetingIdRef = useRef<string | null>(null);
  const detailDirtyRef = useRef(false);
  const historyHandlersRef = useRef<{
    openMeeting: (meetingId: string, seekMs?: number) => void;
    leaveMeeting: () => boolean;
  }>({
    openMeeting: () => {},
    leaveMeeting: () => true,
  });
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [detailError, setDetailError] = useState("");
  const [initialSeekMs, setInitialSeekMs] = useState(0);
  const [initialDetailTab, setInitialDetailTab] = useState<"transcript" | "minutes">("transcript");
  const [query, setQuery] = useState("");
  // 检索结果页：提交时的搜索词和范围（"" 全部；"none" 没归项目的会；其他是项目 id）
  const [searchedQuery, setSearchedQuery] = useState("");
  const [searchScope, setSearchScope] = useState("");
  const [searchResult, setSearchResult] = useState<SearchPayload | null>(null);
  const [searchState, setSearchState] = useState<LoadState>("idle");
  const [searchError, setSearchError] = useState("");
  const [searchActive, setSearchActive] = useState(false);
  const [detailDirty, setDetailDirty] = useState(false);
  const [detailNavigationLocked, setDetailNavigationLocked] = useState(false);
  const meetingRequestSequence = useRef(0);
  const jobsRequestSequence = useRef(0);
  const detailRequestSequence = useRef(0);
  const searchRequestSequence = useRef(0);

  const loadJobs = useCallback(async (silent = false) => {
    const requestSequence = ++jobsRequestSequence.current;
    if (!silent) setJobsState("loading");
    try {
      const payload = await apiClient.jobs();
      if (requestSequence !== jobsRequestSequence.current) return;
      setJobs(payload.items);
      setJobsAvailable(true);
      setJobsState(payload.items.length ? "ready" : "empty");
      setJobsMessage("");
      setJobsStale(false);
    } catch (error) {
      if (requestSequence !== jobsRequestSequence.current) return;
      const unavailable = error instanceof ApiError && error.status === 404;
      setJobsAvailable(!unavailable);
      setJobsState("error");
      setJobsStale(!unavailable);
      setJobsMessage(
        unavailable
          ? "当前后端没有提供 /api/jobs；控制台已保持只读降级。"
          : error instanceof Error
            ? error.message
            : "任务台账读取失败",
      );
    }
  }, [apiClient]);

  const loadMeetings = useCallback(async (
    nextFilters: MeetingFilters,
    nextOffset: number,
    silent = false,
  ) => {
    const requestSequence = ++meetingRequestSequence.current;
    if (!silent) setLibraryState("loading");
    try {
      let payload = await apiClient.meetings({
        ...nextFilters,
        limit: MEETING_PAGE_SIZE,
        offset: nextOffset,
      });
      if (requestSequence !== meetingRequestSequence.current) return;
      if (nextOffset > 0 && nextOffset >= payload.total) {
        const correctedOffset = payload.total
          ? Math.floor((payload.total - 1) / MEETING_PAGE_SIZE) * MEETING_PAGE_SIZE
          : 0;
        payload = await apiClient.meetings({
          ...nextFilters,
          limit: MEETING_PAGE_SIZE,
          offset: correctedOffset,
        });
        if (requestSequence !== meetingRequestSequence.current) return;
      }
      setMeetings(payload.items);
      setMeetingOffset(payload.offset);
      setMeetingTotal(payload.total);
      setLibraryState(payload.items.length ? "ready" : "empty");
    } catch {
      if (requestSequence !== meetingRequestSequence.current) return;
      if (!silent) setLibraryState("error");
    }
  }, [apiClient]);

  const refreshProjects = useCallback(async () => {
    const payload = await apiClient.projects();
    setProjects(payload);
  }, [apiClient]);

  const loadPendingCount = useCallback(async (silent = false) => {
    try {
      const payload = await apiClient.tasks({ status: "pending_confirm", limit: 1 });
      setPendingCount(payload.total);
    } catch {
      if (!silent) setPendingCount(0);
    }
  }, [apiClient]);

  // 词典待确认建议数只影响侧栏角标；接口不可用（旧后端）时静默为 0，不打断主链。
  const loadGlossaryPending = useCallback(async () => {
    try {
      const items = await apiClient.glossarySuggestions?.("pending");
      setGlossaryPending(Array.isArray(items) ? items.length : 0);
    } catch {
      setGlossaryPending(0);
    }
  }, [apiClient]);

  // 归属汇总：工作台「N 场会等你选项目」、资料库「待归属 N」「像新项目 N」。拿不到就不显示。
  const loadAttributionSummary = useCallback(async () => {
    try {
      const payload = await apiClient.attributionSummary?.();
      if (payload) setAttributionSummary(payload);
    } catch {
      // 忽略：下一次刷新再取
    }
  }, [apiClient]);

  // 资料库「需要处理」：失败任务 + 隔离目录。拿不到时保留上一份，不影响资料库主体。
  const loadAttention = useCallback(async () => {
    try {
      const payload = await apiClient.attention?.();
      if (payload) setAttention(payload);
    } catch {
      // 忽略：下一轮轮询再取
    }
  }, [apiClient]);

  // 地址栏 → 视图。冷加载、浏览器前进/后退、手动改 hash 都走这里；
  // 会议详情与离开会议要用到后面才定义的函数和最新状态，经 ref 读取，避免闭包过期。
  const applyHash = useCallback(() => {
    historySyncRef.current = true;
    const hash = window.location.hash;
    if (hash.startsWith("#meetings/")) {
      const target = decodeURIComponent(hash.slice("#meetings/".length));
      // 会议卡片里的时间点链接 #meetings/<id>@<秒>：打开这场会并从那一秒开始播放
      const match = /^(.+?)(?:@(\d+(?:\.\d+)?))?$/.exec(target);
      const meetingId = match?.[1] ?? "";
      const seekMs = match?.[2] ? Math.round(Number(match[2]) * 1000) : 0;
      if (match?.[2]) {
        // 秒数用过就从地址栏去掉，同一个时间点的链接再点一次还能触发跳转
        history.replaceState(
          window.history.state,
          "",
          `${window.location.pathname}${window.location.search}#meetings/${encodeURIComponent(meetingId)}`,
        );
      }
      if (meetingId && (meetingId !== openMeetingIdRef.current || seekMs)) {
        historyHandlersRef.current.openMeeting(meetingId, seekMs);
      }
      return;
    }
    if (openMeetingIdRef.current) {
      // 从会议详情后退：留在原视图（检索结果也保留），只关掉详情。
      if (!historyHandlersRef.current.leaveMeeting()) {
        // 保存进行中或用户选择留下：把会议锚点放回地址栏。
        history.pushState({ app: true }, "", `#meetings/${encodeURIComponent(openMeetingIdRef.current)}`);
        historySyncRef.current = false;
        return;
      }
    } else {
      setSearchActive(false);
    }
    setTaskDrawerId(null);
    if (hash.startsWith("#projects/")) {
      // #projects/<id> 是清单，#projects/<id>/graph?sel=m:<id> 是关系图并选中一个节点
      const [pathPart, queryPart = ""] = hash.slice("#projects/".length).split("?");
      const [rawId, sub] = pathPart.split("/");
      const projectId = decodeURIComponent(rawId ?? "");
      if (projectId) {
        const graphMode = sub === "graph";
        const params = new URLSearchParams(queryPart);
        const selection = graphMode ? params.get("sel") : null;
        setOpenProjectId(projectId);
        setProjectMode(graphMode ? "graph" : "list");
        setGraphSelection(selection);
        setGraphFocus(selection);
        setGraphExpanded(graphMode ? params.get("expand") : null);
        setView("projectDetail");
      }
    } else if (hash.startsWith("#requirements/")) {
      const requirementId = decodeURIComponent(hash.slice("#requirements/".length));
      if (requirementId) {
        setOpenRequirementId(requirementId);
        setView("requirementDetail");
      }
    } else if (hash.startsWith("#glossary/project/")) {
      const projectId = decodeURIComponent(hash.slice("#glossary/project/".length));
      if (projectId) {
        setGlossaryProjectId(projectId);
        setView("glossary");
      }
    } else if (hash === "#glossary") {
      setGlossaryProjectId(null);
      setView("glossary");
    } else {
      const simpleViews: Record<string, AppView> = {
        "": "overview",
        "#overview": "overview",
        "#library": "library",
        "#requirements": "requirements",
        "#tasks": "tasks",
        "#projects": "projects",
        "#jobs": "jobs",
      };
      const target = simpleViews[hash];
      if (target) setView(target);
    }
  }, []);

  useEffect(() => {
    let active = true;
    const initialize = async () => {
      try {
        const boot = await apiClient.bootstrap();
        const [healthPayload, projectPayload, tagPayload] = await Promise.all([
          apiClient.health(),
          apiClient.projects(),
          apiClient.tags(),
        ]);
        if (!active) return;
        setHealth(healthPayload);
        setProjects(projectPayload);
        setTags(tagPayload);
        setMobileTaskWrite(boot.mobile_task_write);
        setPendingCount(boot.pending_confirm_count);
        // 空锚点就是默认的工作台；启动期间用户可能已经点了别的视图，不能再拉回来。
        if (window.location.hash) applyHash();
        hashReadyRef.current = true;
      } catch (error) {
        hashReadyRef.current = true;
        if (!active) return;
        setLibraryState("error");
        setDetailError(error instanceof Error ? error.message : "无法连接本地工作台");
      }
      if (active) {
        await Promise.all([loadMeetings({}, 0), loadJobs()]);
        void loadGlossaryPending();
        void loadAttention();
        void loadAttributionSummary();
      }
    };
    void initialize();
    return () => {
      active = false;
    };
  }, [apiClient, applyHash, loadAttention, loadAttributionSummary, loadGlossaryPending, loadJobs, loadMeetings]);

  const hasActiveJobs = useMemo(
    () => jobs.some((job) => !terminalJobStates.has(job.state)),
    [jobs],
  );

  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      void apiClient
        .health()
        .then((payload) => {
          healthFailureCount.current = 0;
          setHealthUnreachable(false);
          setHealth(payload);
        })
        .catch(() => {
          healthFailureCount.current += 1;
          if (healthFailureCount.current >= 2) setHealthUnreachable(true);
        });
      void loadJobs(true);
      void loadPendingCount(true);
      void loadGlossaryPending();
      void loadAttention();
    };
    const interval = window.setInterval(
      refresh,
      view === "jobs" || hasActiveJobs ? 5_000 : 15_000,
    );
    const onVisibility = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [apiClient, hasActiveJobs, loadAttention, loadGlossaryPending, loadJobs, loadPendingCount, view]);

  useEffect(() => {
    if (view !== "library" || detail || searchActive) return;
    const refresh = () => {
      if (!document.hidden) void loadMeetings(filters, meetingOffset, true);
    };
    const interval = window.setInterval(refresh, 15_000);
    const onVisibility = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [detail, filters, loadMeetings, meetingOffset, searchActive, view]);

  useEffect(() => {
    if (isMobile && view === "jobs") setView("library");
  }, [isMobile, view]);

  const applyFilters = (next: MeetingFilters) => {
    setFilters(next);
    setMeetingOffset(0);
    void loadMeetings(next, 0);
  };

  // silent：保存、回滚、改说话人之后的刷新。保留当前页面只换数据，不闪加载态，
  // 这样标签页、滚动位置、播放进度和「已保存」提示都还在。
  const loadDetail = useCallback(async (meetingId: string, { silent = false } = {}) => {
    const requestSequence = ++detailRequestSequence.current;
    if (!silent) {
      setDetailNavigationLocked(false);
      setDetailState("loading");
      setDetailError("");
    }
    try {
      const payload = await apiClient.meeting(meetingId);
      if (requestSequence !== detailRequestSequence.current) return;
      setDetail(payload);
      setDetailDirty(false);
      setDetailState("ready");
    } catch (error) {
      if (requestSequence !== detailRequestSequence.current) return;
      // 静默刷新失败时留着手上的版本，页面上的操作提示已经说明了结果。
      if (silent) return;
      setDetail(null);
      setDetailState("error");
      setDetailError(error instanceof Error ? error.message : "会议档案读取失败");
    }
  }, [apiClient]);

  const openMeeting = (
    meetingId: string,
    seekMs = 0,
    fromHistory = false,
    tab: "transcript" | "minutes" = "transcript",
  ) => {
    if (detailNavigationLocked) return;
    if (!fromHistory) historySyncRef.current = false;
    if (
      detail &&
      detailDirty &&
      !window.confirm("当前会议仍有未保存修改。放弃这些修改并打开其他会议吗？")
    ) {
      return;
    }
    setInitialSeekMs(seekMs);
    setInitialDetailTab(tab);
    // 检索结果不清：从会议返回时要回到刚才那页结果。
    setDetailDirty(false);
    setTaskDrawerId(null);
    setOpenMeetingId(meetingId);
    void loadDetail(meetingId);
  };

  const resetDetailState = () => {
    detailRequestSequence.current += 1;
    setDetail(null);
    setOpenMeetingId(null);
    setDetailDirty(false);
    setDetailNavigationLocked(false);
    setDetailState("idle");
  };

  // 会议详情页的「← 返回」。是本应用压进来的历史就直接后退，浏览器后退键和这个按钮行为一致；
  // 冷加载直达的会议没有上一条可退，就原地关掉详情。离开前的未保存确认由详情页自己做过了。
  const closeMeeting = () => {
    detailDirtyRef.current = false;
    if ((window.history.state as { app?: boolean } | null)?.app) {
      window.history.back();
      return;
    }
    resetDetailState();
  };

  openMeetingIdRef.current = openMeetingId;
  detailDirtyRef.current = detailDirty;
  historyHandlersRef.current = {
    openMeeting: (meetingId: string, seekMs = 0) => openMeeting(meetingId, seekMs, true),
    leaveMeeting: () => {
      if (detailNavigationLocked) return false;
      if (detailDirtyRef.current && !window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")) {
        return false;
      }
      resetDetailState();
      return true;
    },
  };

  const performNavigate = (nextView: AppView) => {
    historySyncRef.current = false;
    resetDetailState();
    setView(nextView);
    setSearchActive(false);
    setTaskDrawerId(null);
    // 默认清空词典预筛；openGlossaryForProject 会在这之后同一批更新里重新设上。
    setGlossaryProjectId(null);
  };

  const navigate = (nextView: AppView) => {
    if (detailNavigationLocked) return;
    if (isMobile && nextView === "jobs") return;
    if (
      detail &&
      detailDirty &&
      !window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")
    ) {
      return;
    }
    performNavigate(nextView);
  };

  // 应用内打开项目：按这个项目上次选的视图（关系图或清单）；手机端只有清单
  const openProjectDetail = (projectId: string) => {
    setOpenProjectId(projectId);
    setProjectMode(readProjectMode(projectId));
    setGraphSelection(null);
    setGraphFocus(null);
    setGraphExpanded(null);
    performNavigate("projectDetail");
  };

  // 会议页、需求页的「在关系图里看」：打开项目的关系图并选中目标；目标在时间窗外时后端自动放宽
  const openProjectGraph = (projectId: string, selection: string) => {
    setOpenProjectId(projectId);
    setProjectMode("graph");
    setGraphSelection(selection);
    setGraphFocus(selection);
    setGraphExpanded(null);
    performNavigate("projectDetail");
  };

  // 收起展开的会：展开是本应用压进来的那一条历史就后退，地址栏和视角一起回去
  const changeGraphExpand = (meetingId: string | null) => {
    if (meetingId === null && (window.history.state as { graphExpand?: boolean } | null)?.graphExpand) {
      window.history.back();
      return;
    }
    setGraphExpanded(meetingId);
  };

  const changeProjectMode = (mode: ProjectViewMode) => {
    if (!openProjectId) return;
    writeProjectMode(openProjectId, mode);
    setProjectMode(mode);
    setGraphSelection(null);
    setGraphFocus(null);
    setGraphExpanded(null);
  };

  const openRequirementDetail = (requirementId: string) => {
    setOpenRequirementId(requirementId);
    setRequirementFromGraph(null);
    performNavigate("requirementDetail");
  };

  const openRequirementFromGraph = (requirementId: string) => {
    if (!openProjectId) return;
    setOpenRequirementId(requirementId);
    performNavigate("requirementDetail");
    setRequirementFromGraph({ projectId: openProjectId, selection: `r:${requirementId}` });
  };

  const leaveRequirement = () => {
    const origin = requirementFromGraph;
    setRequirementFromGraph(null);
    if (!origin) {
      navigate("requirements");
      return;
    }
    // 是本应用压进来的历史就后退，地址栏里的 ?sel= 会把选中和视角一起带回来
    if ((window.history.state as { app?: boolean } | null)?.app) window.history.back();
    else openProjectGraph(origin.projectId, origin.selection);
  };

  // 项目详情页「在词典中查看 →」：跳去词典页并预选中这个项目的 chip。
  const openGlossaryForProject = (projectId: string) => {
    performNavigate("glossary");
    setGlossaryProjectId(projectId);
  };

  // 视图 → 地址栏。每个视图和打开的会议都有自己的锚点，飞书卡片可以直达，刷新不丢位置；
  // 用户操作产生的切换压入历史，浏览器后退键就能回到上一个视图或关掉会议。
  useEffect(() => {
    if (!hashReadyRef.current) return;
    const path = openMeetingId
      ? `#meetings/${encodeURIComponent(openMeetingId)}`
      : view === "glossary"
        ? glossaryProjectId
          ? `#glossary/project/${glossaryProjectId}`
          : "#glossary"
        : view === "projectDetail" && openProjectId
          ? projectGraphPath(openProjectId, !isMobile && projectMode === "graph", graphSelection, graphExpanded)
          : view === "requirementDetail" && openRequirementId
            ? `#requirements/${openRequirementId}`
            : view === "overview"
              ? ""
              : view === "projectDetail" || view === "requirementDetail"
                ? ""
                : `#${view}`;
    const fromHistory = historySyncRef.current;
    historySyncRef.current = false;
    if (window.location.hash === path) return;
    const url = window.location.pathname + window.location.search + path;
    // 关系图里换选中只改地址栏的 ?sel=，不压历史，后退键直接回到上一个页面；
    // 展开一场会压一条（后退键收起），展开着换到前后场只替换
    const sameBase = window.location.hash.split("?")[0] === path.split("?")[0];
    const wasExpanded = expandParam(window.location.hash);
    const nowExpanded = expandParam(path);
    if (!fromHistory && sameBase && !wasExpanded && nowExpanded) {
      history.pushState({ app: true, graphExpand: true }, "", url);
    } else if (fromHistory || sameBase) history.replaceState(window.history.state, "", url);
    else history.pushState({ app: true }, "", url);
  }, [
    glossaryProjectId,
    graphExpanded,
    graphSelection,
    isMobile,
    openMeetingId,
    openProjectId,
    openRequirementId,
    projectMode,
    view,
  ]);

  // 浏览器前进/后退或手动改地址栏 hash 时反向同步视图。
  useEffect(() => {
    window.addEventListener("popstate", applyHash);
    window.addEventListener("hashchange", applyHash);
    return () => {
      window.removeEventListener("popstate", applyHash);
      window.removeEventListener("hashchange", applyHash);
    };
  }, [applyHash]);

  // word：点「也可以搜」换一个词；scope：结果页换范围
  const submitSearch = async (overrides: { word?: string; scope?: string } = {}) => {
    if (detailNavigationLocked) return;
    const normalized = (overrides.word ?? query).trim();
    // 从别的页面重新搜时回到全部项目；在结果页里接着搜就沿用刚才选的范围
    const scope = overrides.scope ?? (searchActive ? searchScope : "");
    if (!normalized) {
      setSearchActive(false);
      return;
    }
    if (
      detail &&
      detailDirty &&
      !window.confirm("当前会议仍有未保存修改。放弃这些修改并开始搜索吗？")
    ) {
      return;
    }
    const requestSequence = ++searchRequestSequence.current;
    historySyncRef.current = false;
    resetDetailState();
    if (overrides.word !== undefined) setQuery(normalized);
    setSearchScope(scope);
    setSearchedQuery(normalized);
    setSearchActive(true);
    setSearchState("loading");
    setSearchError("");
    try {
      const payload = await apiClient.search(normalized, scope || undefined);
      if (requestSequence !== searchRequestSequence.current) return;
      setSearchResult(payload);
      setSearchState("ready");
    } catch (error) {
      if (requestSequence !== searchRequestSequence.current) return;
      setSearchResult(null);
      setSearchState("error");
      setSearchError(error instanceof Error ? error.message : "检索失败");
    }
  };

  const healthLevel = useMemo(() => {
    if (healthUnreachable) return "failed" as const;
    if (!health) return "unknown" as const;
    if (health.status === "ok") return "healthy" as const;
    return "degraded" as const;
  }, [health, healthUnreachable]);

  const searchSlot = (
    <form
      className="global-search"
      onSubmit={(event) => {
        event.preventDefault();
        void submitSearch();
      }}
      role="search"
    >
      <span aria-hidden="true" className="search-glyph">
        <svg fill="none" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 14 14">
          <circle cx="6.2" cy="6.2" r="4.6" />
          <path d="M9.7 9.7 13 13" strokeLinecap="round" />
        </svg>
      </span>
      <input
        aria-label="全局检索"
        disabled={detailNavigationLocked}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索会议、原句或关键词"
        value={query}
      />
      <MagneticButton className="search-submit" disabled={detailNavigationLocked} type="submit">
        检索
      </MagneticButton>
    </form>
  );

  let content;
  if (detailState === "loading") {
    content = <AsyncState state="loading" />;
  } else if (detailState === "error") {
    content = <AsyncState message={detailError} state="error" />;
  } else if (detail) {
    content = (
      <MeetingDetailPage
        apiClient={apiClient}
        canWriteTasks={!isMobile || mobileTaskWrite}
        initialSeekMs={initialSeekMs}
        initialTab={initialDetailTab}
        isMobile={isMobile}
        meeting={detail}
        backLabel={searchActive ? "检索结果" : VIEW_LABELS[view]}
        onBack={closeMeeting}
        onClassificationSaved={refreshProjects}
        onDirtyChange={setDetailDirty}
        onNavigationLockChange={setDetailNavigationLocked}
        onOpenProject={(projectId) => {
          if (detailNavigationLocked) return;
          if (detailDirty && !window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")) return;
          openProjectDetail(projectId);
        }}
        onOpenInGraph={(projectId, meetingId) => {
          if (detailNavigationLocked) return;
          if (detailDirty && !window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")) return;
          openProjectGraph(projectId, `m:${meetingId}`);
        }}
        onOpenRequirement={openRequirementDetail}
        onOpenTasks={() => navigate("tasks")}
        onGlossaryChanged={() => void loadGlossaryPending()}
        onTasksChanged={() => void loadPendingCount(true)}
        onReload={async () => {
          await Promise.all([
            loadDetail(detail.id, { silent: true }),
            loadMeetings(filters, meetingOffset, true),
            loadPendingCount(),
          ]);
        }}
        projects={projects}
        tags={tags}
      />
    );
  } else if (searchActive) {
    content = (
      <SearchPage
        error={searchError}
        onOpen={(meetingId, startMs, tab) => openMeeting(meetingId, startMs, false, tab)}
        onScopeChange={(scope) => void submitSearch({ word: searchedQuery, scope })}
        onSearchWord={(word) => void submitSearch({ word })}
        projects={projects}
        query={searchedQuery}
        result={searchResult}
        scope={searchScope}
        state={searchState}
      />
    );
  } else if (view === "overview") {
    content = (
      <OverviewPage
        apiClient={apiClient}
        health={health}
        jobs={jobs}
        jobsAvailable={jobsAvailable}
        jobsInteractive={!isMobile}
        meetings={meetings}
        onOpenJobs={() => navigate("jobs")}
        onOpenLibrary={() => navigate("library")}
        onOpenMeeting={openMeeting}
        onOpenTasks={() => navigate("tasks")}
        attributionSummary={attributionSummary}
        canPickFolders={!isMobile}
        onProjectsChanged={refreshProjects}
        onOpenAttributionReview={() => {
          applyFilters({ attribution: "needs_review" });
          navigate("library");
        }}
        onTasksChanged={() => void loadPendingCount(true)}
      />
    );
  } else if (view === "library") {
    content = (
      <LibraryPage
        filters={filters}
        limit={MEETING_PAGE_SIZE}
        meetings={meetings}
        offset={meetingOffset}
        onFilter={applyFilters}
        onOpen={openMeeting}
        onPageChange={(offset) => {
          setMeetingOffset(offset);
          void loadMeetings(filters, offset);
        }}
        projects={projects}
        state={libraryState}
        tags={tags}
        total={meetingTotal}
        attention={attention}
        attributionSummary={attributionSummary}
        onAssignProject={
          isMobile
            ? undefined
            : async (meetingId, projectId) => {
                await apiClient.updateMeeting(meetingId, { project_id: projectId });
                await Promise.all([
                  loadMeetings(filters, meetingOffset, true),
                  loadAttributionSummary(),
                  refreshProjects(),
                ]);
              }
        }
        onAcknowledgeJob={
          isMobile
            ? undefined
            : async (jobId) => {
                await apiClient.acknowledgeJob(jobId);
                await loadAttention();
                setHealth(await apiClient.health());
              }
        }
        onOpenJobs={isMobile ? undefined : () => navigate("jobs")}
      />
    );
  } else if (view === "requirements") {
    content = (
      <RequirementsPage
        apiClient={apiClient}
        canPickFolders={!isMobile}
        canWrite={!isMobile || mobileTaskWrite}
        onOpenProject={openProjectDetail}
        onOpenRequirement={openRequirementDetail}
        onProjectsChanged={refreshProjects}
        projects={projects}
      />
    );
  } else if (view === "requirementDetail" && openRequirementId) {
    content = (
      <RequirementDetailPage
        apiClient={apiClient}
        canPickFolders={!isMobile}
        canWrite={!isMobile || mobileTaskWrite}
        backLabel={requirementFromGraph ? "关系图" : undefined}
        onBack={leaveRequirement}
        onOpenInGraph={isMobile ? undefined : (projectId, requirementId) => openProjectGraph(projectId, `r:${requirementId}`)}
        onOpenMeeting={openMeeting}
        onOpenProject={openProjectDetail}
        onOpenTask={setTaskDrawerId}
        onProjectsChanged={refreshProjects}
        projects={projects}
        reloadKey={boardVersion}
        requirementId={openRequirementId}
      />
    );
  } else if (view === "tasks") {
    content = (
      <TasksPage
        apiClient={apiClient}
        canWrite={!isMobile || mobileTaskWrite}
        onOpenMeeting={openMeeting}
        onOpenProject={openProjectDetail}
        onOpenRequirement={openRequirementDetail}
        projects={projects}
      />
    );
  } else if (view === "glossary") {
    content = (
      <GlossaryPage
        apiClient={apiClient}
        canWrite={!isMobile || mobileTaskWrite}
        initialProjectId={glossaryProjectId}
        meetings={meetings}
        onPendingChange={loadGlossaryPending}
        projects={projects}
      />
    );
  } else if (view === "projectDetail" && openProjectId) {
    content =
      !isMobile && projectMode === "graph" ? (
        <ProjectGraph
          apiClient={apiClient}
          expanded={graphExpanded}
          focus={graphFocus}
          key={openProjectId}
          modeToggle={<ViewModeToggle mode="graph" onChange={changeProjectMode} />}
          onBack={() => navigate("projects")}
          onExpandChange={changeGraphExpand}
          onOpenAttributionReview={() => {
            applyFilters({ attribution: "needs_review" });
            navigate("library");
          }}
          onOpenGlossary={openGlossaryForProject}
          onOpenMeeting={openMeeting}
          onOpenProject={openProjectDetail}
          onOpenRequirement={openRequirementFromGraph}
          onProjectsChanged={refreshProjects}
          onSelectionChange={setGraphSelection}
          projectId={openProjectId}
          projects={projects}
          selection={graphSelection}
        />
      ) : (
        <ProjectDetailPage
          apiClient={apiClient}
          key={openProjectId}
          modeToggle={isMobile ? undefined : <ViewModeToggle mode="list" onChange={changeProjectMode} />}
          canPickFolders={!isMobile}
          canWrite={!isMobile || mobileTaskWrite}
          onBack={() => navigate("projects")}
          onOpenGlossary={openGlossaryForProject}
          onOpenMeeting={openMeeting}
          onOpenRequirement={openRequirementDetail}
          onOpenTask={setTaskDrawerId}
          onProjectUpdated={refreshProjects}
          onOpenProject={openProjectDetail}
          onProjectsChanged={refreshProjects}
          projectId={openProjectId}
          projects={projects}
          reloadKey={boardVersion}
        />
      );
  } else if (view === "jobs") {
    content = (
      <JobsPage
        available={jobsAvailable}
        jobs={jobs}
        message={jobsMessage}
        onCancel={async (jobId) => { await apiClient.cancelJob(jobId); await loadJobs(); }}
        onRetry={async (jobId, stage, hotwords) => { await apiClient.retryJob(jobId, stage, hotwords); await loadJobs(); }}
        onRetrySubstate={async (jobId, name) => { await apiClient.retryJobSubstate(jobId, name); await loadJobs(); }}
        onStopAfterStage={async (jobId) => { await apiClient.stopAfterStage(jobId); await loadJobs(); }}
        onUpload={async (file, hotwords, onProgress) => {
          const receipt = await uploadRecordingInChunks(apiClient, file, hotwords, onProgress);
          await loadJobs();
          return receipt.job_id
            ? `已保存并入队：${receipt.job_id}（${receipt.size_bytes.toLocaleString()} 字节）`
            : `录音已安全保存在本机（${receipt.size_bytes.toLocaleString()} 字节），后台会自动重试入队`;
        }}
        stale={jobsStale}
        state={jobsState}
      />
    );
  } else {
    content = (
      <ProjectsPage
        apiClient={apiClient}
        canEdit={!isMobile}
        meetings={meetings}
        onCreateTag={async (name, color) => {
          const created = await apiClient.createTag(name, color);
          setTags((current) => [...current, created].sort((left, right) => left.name.localeCompare(right.name, "zh-CN")));
        }}
        onOpenProject={openProjectDetail}
        onProjectsChanged={refreshProjects}
        projects={projects}
        tags={tags}
      />
    );
  }

  return (
    <AppShell
      activeView={view}
      glossaryBadge={glossaryPending}
      health={healthLevel}
      isMobile={isMobile}
      navigationLocked={detailNavigationLocked}
      onNavigate={navigate}
      searchSlot={searchSlot}
      taskBadge={pendingCount}
    >
      <FadeContent transitionKey={view}>{content}</FadeContent>
      {taskDrawerId && (
        <TaskDrawer
          apiClient={apiClient}
          canWrite={!isMobile || mobileTaskWrite}
          onChanged={() => {
            void loadPendingCount();
            void refreshProjects();
            setBoardVersion((version) => version + 1); // 任务状态变了，刷新看板 KPI 与任务卡
          }}
          onClose={() => setTaskDrawerId(null)}
          onOpenMeeting={openMeeting}
          onOpenRequirement={openRequirementDetail}
          taskId={taskDrawerId}
        />
      )}
    </AppShell>
  );
}
