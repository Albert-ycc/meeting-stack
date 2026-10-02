import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

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
  PoolFlash,
  PreviewTarget,
  Project,
  SearchPayload,
  Tag,
} from "./types";
import { AppShell, type AppView } from "./components/AppShell";
import { AsyncState } from "./components/AsyncState";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { FadeContent } from "./components/motion/FadeContent";
import { MagneticButton } from "./components/motion/MagneticButton";
import { GlossaryPage } from "./components/GlossaryPage";
import { JobsPage } from "./components/JobsPage";
import { LibraryPage } from "./components/LibraryPage";
import { MeetingDetailPage } from "./components/MeetingDetailPage";
import { OverviewPage } from "./components/OverviewPage";
import { ProjectDetailPage } from "./components/ProjectDetailPage";
import { OverviewGraph } from "./components/graph/OverviewGraph";
import { ProjectGraph } from "./components/graph/ProjectGraph";
import type { GraphLocal } from "./components/graph/graphTypes";
import type { ProjectViewMode } from "./components/graph/graphPrefs";
import { ProjectsPage } from "./components/ProjectsPage";
import { RequirementDetailPage } from "./components/RequirementDetailPage";
import { RequirementsPage } from "./components/RequirementsPage";
import {
  LEAVE_FORM_CONFIRM,
  RequirementFormPage,
  type RequirementFormResult,
  type RequirementPrefill,
} from "./components/pool/RequirementFormPage";
import {
  POOL_PRIORITIES_KEY,
  POOL_PRIORITIES_STORE,
  POOL_PROJECTS_KEY,
  POOL_PROJECTS_STORE,
  POOL_QUERY_KEY,
  POOL_QUERY_STORE,
  POOL_TAB_KEY,
  RequirementPoolPage,
  mergedMessage,
} from "./components/pool/RequirementPoolPage";
import { readPersistentState, writePersistentState } from "./viewState";
import { SearchPage } from "./components/SearchPage";
import { setDraft as setAskDraft } from "./components/ask/askStore";
import { TaskDrawer } from "./components/TaskDrawer";
import { MaterialPreviewDrawer } from "./components/MaterialPreview";
import { TasksPage } from "./components/TasksPage";
import { LinksFlagsContext, linksFlagsFrom, type LinksFlags } from "./components/links/LinksFlagsContext";
import { RecentAnswersContext, createRecentAnswerStore } from "./components/links/useRelationAnswer";
import { isComposingKeydown } from "./keyboard";
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

type RequirementFormState =
  | { mode: "create"; prefill?: RequirementPrefill }
  | { mode: "claim"; candidateId: string }
  | { mode: "edit"; requirementId: string };

/** 新增、认领、修改需求二级页的地址 */
function requirementFormPath(form: RequirementFormState): string {
  if (form.mode === "create") return "#requirements/new";
  if (form.mode === "edit") return `#requirements/${encodeURIComponent(form.requirementId)}/edit`;
  return `#requirements/claim/${encodeURIComponent(form.candidateId)}`;
}

// 会议详情页「← 返回」按钮上显示的去处：打开会议前所在的视图。
const VIEW_LABELS: Record<AppView, string> = {
  overview: "工作台",
  library: "录音档案",
  requirements: "需求池",
  requirementDetail: "需求详情",
  requirementForm: "需求池",
  tasks: "待办",
  glossary: "词典",
  jobs: "转写录音",
  projects: "项目管理",
  projectDetail: "项目详情",
  graph: "关系图",
};

/**
 * 项目详情的地址：清单是 #projects/<id>，关系图是 #projects/<id>/graph，选中节点时带 ?sel=m:<id>，
 * 展开一场会时带 expand=<会议 id>；4f：以文件为中心的局部图带 file=<文件 id>，来龙去脉带 trace=<节点>
 * （和 expand 互斥，两个都有时留 expand）
 */
function projectGraphPath(
  projectId: string,
  graph: boolean,
  selection: string | null,
  expanded: string | null = null,
  local: GraphLocal | null = null,
) {
  if (!graph) return `#projects/${projectId}`;
  const shownLocal = expanded ? null : local;
  const params = [
    expanded ? `expand=${encodeURIComponent(expanded)}` : "",
    shownLocal?.kind === "file" ? `file=${shownLocal.fileId}` : "",
    shownLocal?.kind === "trace" ? `trace=${shownLocal.node.split(":").map(encodeURIComponent).join(":")}` : "",
    selection ? `sel=${selection.split(":").map(encodeURIComponent).join(":")}` : "",
  ].filter(Boolean);
  return `#projects/${projectId}/graph${params.length ? `?${params.join("&")}` : ""}`;
}

/** 全部项目概览的地址：#graph，选中一个岛时带 ?sel=p:<id> */
function overviewPath(selection: string | null) {
  return selection ? `#graph?sel=${selection.split(":").map(encodeURIComponent).join(":")}` : "#graph";
}

function expandParam(hash: string) {
  const query = hash.split("?")[1];
  return query ? new URLSearchParams(query).get("expand") : null;
}

/** 4f：地址栏里的局部图、来龙去脉（file=、trace=）；有 expand 时不算 */
function localFromQuery(params: URLSearchParams): GraphLocal | null {
  if (params.get("expand")) return null;
  const file = params.get("file");
  if (file && /^\d{1,12}$/.test(file)) return { kind: "file", fileId: Number(file) };
  const trace = params.get("trace");
  if (trace && /^(file:\d{1,12}|m:[A-Za-z0-9_-]{1,64}|dec:[A-Za-z0-9_-]{1,64}|task:[A-Za-z0-9_-]{1,64})$/.test(trace)) {
    return { kind: "trace", node: trace };
  }
  return null;
}

function localParam(hash: string): string | null {
  const local = localFromQuery(new URLSearchParams(hash.split("?")[1] ?? ""));
  return local ? (local.kind === "file" ? `file:${local.fileId}` : `trace:${local.node}`) : null;
}

export default function App({ apiClient = api }: AppProps) {
  const isMobile = useMobileBreakpoint();
  // applyHash 挂在 popstate 上，经 ref 读到是不是手机
  const isMobileRef = useRef(isMobile);
  isMobileRef.current = isMobile;
  // 落地页是工作台（最近的会、待确认任务、处理中的录音）；按日期回忆某场会走侧栏「录音档案」。
  const [view, setView] = useState<AppView>("overview");
  // 冷加载时地址栏里的 #tasks 等锚点要先被读进视图，之后才允许把视图反写回地址栏，
  // 否则首帧 view=overview 会先把 hash 清空，applyHash 再也读不到（冷加载 #tasks 被拉回工作台）。
  const hashReadyRef = useRef(false);
  // 启动接口回来之前用户已经点过侧栏、打开过会、搜过：启动完成后不再按地址栏里的旧锚点拉回去，
  // 反过来把用户所在的视图写回地址栏（bootSettled 变一次，让下面「视图 → 地址栏」再跑一遍）
  const navigatedDuringBootRef = useRef(false);
  const [bootSettled, setBootSettled] = useState(false);
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
  // 4f：关系图的局部图、来龙去脉；每换一次中心压一条历史（state 里记 localDepth），返回键回到上一个中心
  const [graphLocal, setGraphLocal] = useState<GraphLocal | null>(null);
  const localPushRef = useRef(false);
  // ［回到关系图］一次退了好几层：落到的那一条要是进局部图的那一条（state 里记着 localRoot），就换成星图
  const localExitRef = useRef(false);
  // 全部项目概览里选中的节点（地址栏 #graph?sel=p:<id>）
  const [overviewSelection, setOverviewSelection] = useState<string | null>(null);
  // 从关系图点进需求页时，面包屑写「关系图」，返回回到画布
  const [requirementFromGraph, setRequirementFromGraph] = useState<{ projectId: string; selection: string } | null>(null);
  const [openRequirementId, setOpenRequirementId] = useState<string | null>(null);
  // 新增、认领、修改需求的二级页（地址 #requirements/new、#requirements/claim/<候选 id>、#requirements/<id>/edit）；
  // 从逐字稿选句进来的新增（S10）带着来源，刷新后来源不在了，就是一张空的新增页
  const [requirementForm, setRequirementForm] = useState<RequirementFormState | null>(null);
  // 表单页上有没保存的改动（表单经 onDirtyChange 报上来）。存成 ref：保存成功时表单先报 false 再 onDone，
  // 紧跟着的跳转要读到新值。formPathRef 是正开着的表单页的地址，浏览器后退被拦下时放回去（审查 B1）
  const formDirtyRef = useRef(false);
  const formPathRef = useRef<string | null>(null);
  // 从逐字稿选句进来的新增页（S10）盖在会议页上：会议页不卸载、只是藏起来，取消回来滚动位置、播放进度、
  // 返回去处都还在（R04-1、审查 M7）。meetingScrollRef 记着盖上之前整页和逐字稿框各滚到哪了
  const [meetingFormPrefill, setMeetingFormPrefill] = useState<RequirementPrefill | null>(null);
  const meetingFormRef = useRef<RequirementPrefill | null>(null);
  const meetingScrollRef = useRef<{ page: number; transcript: number } | null>(null);
  // 从需求详情打开会议，退回来时要回到原来的滚动位置。需求详情退回来是重新挂上、重新取数的，浏览器在 popstate
  // 那一刻按它记下的位置恢复时页面还是空的，落到顶上。打开会议时把位置记进需求详情那一条历史（listScroll），
  // 退回来时取出来放这里，等详情的数据到了（onReady）再滚回去。录音档案、检索结果的数据在 App 手里，
  // 回来时列表一下就画全了，浏览器自己恢复就是对的，不用管
  const listScrollRef = useRef<number | null>(null);
  // 侧栏、代码里换视图：新视图从顶上看起（浏览器前进后退不归零，由浏览器恢复）。要等新的一条历史压进去以后再滚，
  // 先滚的话浏览器给旧的那一条记下的位置就成了 0，后退回去回不到原处
  const scrollResetRef = useRef(false);
  // 认领、合并后回到需求池时提示一句；建完、改完需求进详情页时也提示一句
  const [poolFlash, setPoolFlash] = useState<PoolFlash | null>(null);
  const [requirementFlash, setRequirementFlash] = useState<string | null>(null);
  // 从项目详情页跳进词典时预选中的项目 chip；普通侧栏导航进词典时为 null（不预筛）。
  const [glossaryProjectId, setGlossaryProjectId] = useState<string | null>(null);
  const [taskDrawerId, setTaskDrawerId] = useState<string | null>(null);
  // 材料预览抽屉（3e）：和任务抽屉一样挂在根部，换视图时一起关
  const [previewTarget, setPreviewTarget] = useState<PreviewTarget | null>(null);
  // 在本机打开页面才有「在访达中显示」
  const [canReveal, setCanReveal] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [glossaryPending, setGlossaryPending] = useState(0);
  const [mobileTaskWrite, setMobileTaskWrite] = useState(true);
  // 第四期的开关（bootstrap 里 links_enabled 是布尔值才有）；为 null 是旧后台，第四期的控件一律不画
  const [linksFlags, setLinksFlags] = useState<LinksFlags | null>(null);
  // 回答以后收成的那一行［撤销］：只在内存里，宿主关掉再打开，撤销期内还在
  const [recentAnswers] = useState(createRecentAnswerStore);
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
  // 手工导入录音的进度（百分比），没在传是 null。挂在 App 上：切到别的页面上传照样在跑，刷新、关标签页也要先问
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
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
  // 从需求池、需求详情点原话时间打开会议：跳到那一秒并开始放
  const [initialAutoplay, setInitialAutoplay] = useState(false);
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
    // 表单页上有没保存的改动时按了浏览器后退、前进：先问；留下就把表单页的地址放回去（审查 B1）
    const formPath = formPathRef.current;
    // 地址就是正开着的这张表单页：什么都不做。浏览器后退会先后发 popstate、hashchange，前一次拦下后把地址
    // 放回来了，后一次读到的就是表单页自己——当成「打开新增页」处理会关掉会议页、换成一张空白新增页
    if (formPath && hash === formPath) return;
    if (formPath && formDirtyRef.current) {
      if (!window.confirm(LEAVE_FORM_CONFIRM)) {
        history.pushState({ app: true }, "", formPath);
        historySyncRef.current = false;
        return;
      }
      formDirtyRef.current = false;
    }
    const exitingLocal = localExitRef.current;
    localExitRef.current = false;
    if (hash.startsWith("#meetings/")) {
      const target = decodeURIComponent(hash.slice("#meetings/".length));
      // 会议卡片里的时间点链接 #meetings/<id>@<秒>：打开这场会并从那一秒开始播放；秒数不是数字就当没带
      const match = /^(.+?)(?:@([^@]*))?$/.exec(target);
      const meetingId = match?.[1] ?? "";
      const seconds = match?.[2] !== undefined && /^\d+(?:\.\d+)?$/.test(match[2]) ? Number(match[2]) : 0;
      const seekMs = Math.round(seconds * 1000);
      if (match?.[2] !== undefined) {
        // 秒数用过就从地址栏去掉，同一个时间点的链接再点一次还能触发跳转
        history.replaceState(
          window.history.state,
          "",
          `${window.location.pathname}${window.location.search}#meetings/${encodeURIComponent(meetingId)}`,
        );
      }
      if (meetingFormRef.current && meetingId === openMeetingIdRef.current) {
        // 从盖在会议页上的新增页退回来（取消、浏览器后退）：揭开新增页，会议页原样还在
        setMeetingFormPrefill(null);
        if (!seekMs) return;
      }
      // 退回到一场会：回到当初打开它时所在的视图，返回按钮和侧栏才对得上（审查 M7：从需求详情后退回到
      // 会议，返回按钮写着「需求详情」、点了却去录音档案）；从这场会选句建过需求的，停在选的那一句
      const landed = window.history.state as { behind?: string; resumeAt?: number | null } | null;
      if (
        landed?.behind &&
        landed.behind !== "requirementForm" &&
        landed.behind in VIEW_LABELS &&
        meetingId !== openMeetingIdRef.current
      ) {
        setView(landed.behind as AppView);
      }
      if (meetingId && (meetingId !== openMeetingIdRef.current || seekMs)) {
        historyHandlersRef.current.openMeeting(meetingId, seekMs || (landed?.resumeAt ?? 0));
      }
      return;
    }
    // 前进、后退回到「盖在会议页上的新增页」那一条：带来的来源记在这条历史里（第二轮审查一般-3）。
    // 会议还开着就重新盖上、不关会议页；会议已经关了（中途去了别处、刷新过）就按普通新增页打开，来源照样带上
    const carried =
      hash === "#requirements/new"
        ? (window.history.state as { meetingForm?: RequirementPrefill } | null)?.meetingForm
        : undefined;
    if (carried && carried.source.meeting_id === openMeetingIdRef.current) {
      meetingScrollRef.current = {
        page: document.documentElement.scrollTop,
        transcript: document.querySelector<HTMLElement>(".transcript-scroll")?.scrollTop ?? 0,
      };
      setMeetingFormPrefill(carried);
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
      const listScroll = (window.history.state as { listScroll?: number } | null)?.listScroll;
      if (typeof listScroll === "number") {
        listScrollRef.current = listScroll;
        // 浏览器在 popstate 之后会按它记下的位置滚一次，这时详情还没取回来、页面不够高，会落到顶上。
        // 这一条这次不让浏览器管，滚完再交还给它（之后从别处后退到这一条照常由浏览器恢复）
        history.scrollRestoration = "manual";
        window.setTimeout(() => {
          history.scrollRestoration = "auto";
        }, 0);
      }
    } else {
      setSearchActive(false);
    }
    setTaskDrawerId(null);
    setPreviewTarget(null);
    if (hash === "#graph" || hash.startsWith("#graph?")) {
      // 全部项目概览，#graph?sel=p:<id> 选中一个岛；要在 #projects/ 之前认。手机上没有关系图，退回项目列表
      if (isMobileRef.current) {
        setView("projects");
      } else {
        setOverviewSelection(new URLSearchParams(hash.split("?")[1] ?? "").get("sel"));
        setView("graph");
      }
    } else if (hash.startsWith("#projects/")) {
      // #projects/<id> 是清单，#projects/<id>/graph?sel=m:<id> 是关系图并选中一个节点
      const [pathPart, queryPart = ""] = hash.slice("#projects/".length).split("?");
      const [rawId, sub] = pathPart.split("/");
      const projectId = decodeURIComponent(rawId ?? "");
      if (projectId) {
        const graphMode = sub === "graph";
        const params = new URLSearchParams(queryPart);
        const selection = graphMode ? params.get("sel") : null;
        let local = graphMode ? localFromQuery(params) : null;
        // ［回到关系图］退到了进局部图的那一条（冷启动深链、从别的页进来的，它自己就带着 file、trace）：
        // 不再打开那个中心，换成星图。地址当场替换掉 file、trace（紧跟着的 hashchange 读到的就是星图）
        const landed = window.history.state as Record<string, unknown> | null;
        if (exitingLocal && local && landed?.localRoot) {
          local = null;
          params.delete("file");
          params.delete("trace");
          const { localRoot: _dropped, ...rest } = landed;
          const query = params.toString();
          history.replaceState(
            rest,
            "",
            `${window.location.pathname}${window.location.search}#projects/${pathPart}${query ? `?${query}` : ""}`,
          );
        }
        // 4f：手机上没有局部图和来龙去脉的舞台：照 #graph 的规矩退回项目列表
        if (local && isMobileRef.current) {
          setView("projects");
          return;
        }
        setGraphLocal(local);
        setOpenProjectId(projectId);
        setProjectMode(graphMode ? "graph" : "list");
        setGraphSelection(selection);
        setGraphFocus(selection);
        setGraphExpanded(graphMode ? params.get("expand") : null);
        setView("projectDetail");
      }
    } else if (hash === "#requirements/new" || hash.startsWith("#requirements/claim/")) {
      // 新增、认领需求的二级页只在电脑上有（手机端只读）：照 #graph 的规矩退回需求池
      const candidateId = hash.startsWith("#requirements/claim/")
        ? decodeURIComponent(hash.slice("#requirements/claim/".length))
        : null;
      if (isMobileRef.current) {
        setView("requirements");
      } else if (candidateId === null) {
        setRequirementForm({ mode: "create", prefill: carried });
        setView("requirementForm");
      } else if (candidateId) {
        setRequirementForm({ mode: "claim", candidateId });
        setView("requirementForm");
      }
    } else if (/^#requirements\/[^/]+\/edit$/.test(hash)) {
      // 修改需求的二级页（S11）只在电脑上有：手机上退回需求详情
      const requirementId = decodeURIComponent(hash.slice("#requirements/".length, -"/edit".length));
      if (isMobileRef.current) {
        setOpenRequirementId(requirementId);
        setView("requirementDetail");
      } else {
        setRequirementForm({ mode: "edit", requirementId });
        setView("requirementForm");
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
        setCanReveal(Boolean(boot.can_reveal));
        setLinksFlags(linksFlagsFrom(boot));
        setPendingCount(boot.pending_confirm_count);
        // 空锚点就是默认的工作台；启动期间用户可能已经点了别的视图，不能再拉回来。
        if (window.location.hash && !navigatedDuringBootRef.current) applyHash();
        hashReadyRef.current = true;
      } catch (error) {
        hashReadyRef.current = true;
        if (!active) return;
        setLibraryState("error");
        setDetailError(error instanceof Error ? error.message : "无法连接本地工作台");
      }
      if (active && navigatedDuringBootRef.current) {
        // 用户在启动期间去过的地方替换掉地址栏里的旧锚点，不压历史
        historySyncRef.current = true;
        setBootSettled(true);
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

  // 上传中关页面或刷新会中断分块上传，先让浏览器问一句；只在开始、结束上传时挂拆，进度数字变化不用重挂
  const uploading = uploadPercent !== null;
  useEffect(() => {
    if (!uploading) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [uploading]);

  const hasActiveJobs = useMemo(
    () => jobs.some((job) => !terminalJobStates.has(job.state)),
    [jobs],
  );

  // 只认快慢档，不认具体视图：依赖 view 的话每换一页定时器就重来，一直在页面间点来点去，角标、状态永远不刷新
  const fastPoll = view === "jobs" || hasActiveJobs;
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
    const interval = window.setInterval(refresh, fastPoll ? 5_000 : 15_000);
    const onVisibility = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [apiClient, fastPoll, loadAttention, loadGlossaryPending, loadJobs, loadPendingCount]);

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
    // 全部项目概览只在电脑上有：窗口变窄到手机布局时退回项目列表
    if (isMobile && view === "graph") setView("projects");
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
      // 静默刷新时另一侧没保存的编辑还留在会议页里，有没有未保存修改由会议页换了详情后重新报上来
      if (!silent) setDetailDirty(false);
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
    autoplay = false,
  ) => {
    if (detailNavigationLocked) return;
    if (!fromHistory) {
      historySyncRef.current = false;
      navigatedDuringBootRef.current = true;
    }
    if (
      detail &&
      detailDirty &&
      !window.confirm("当前会议仍有未保存修改。放弃这些修改并打开其他会议吗？")
    ) {
      return;
    }
    listScrollRef.current = null;
    if (!fromHistory && !openMeetingId && !searchActive && view === "requirementDetail") {
      window.history.replaceState(
        { ...((window.history.state as Record<string, unknown> | null) ?? {}), listScroll: document.documentElement.scrollTop },
        "",
      );
    }
    setInitialSeekMs(seekMs);
    setInitialAutoplay(autoplay);
    setInitialDetailTab(tab);
    // 检索结果不清：从会议返回时要回到刚才那页结果。
    setDetailDirty(false);
    setTaskDrawerId(null);
    setPreviewTarget(null);
    setOpenMeetingId(meetingId);
    void loadDetail(meetingId);
  };

  // 需求池海报、需求详情「出自录音」上点原话时间、波形：打开会议并从这一秒开始放（R02-3、R05-1）；
  // 没有时间（只关联了会议）就只打开
  const openMeetingAtQuote = (meetingId: string, atMs?: number) =>
    openMeeting(meetingId, atMs ?? 0, false, "transcript", atMs !== undefined);

  const resetDetailState = () => {
    detailRequestSequence.current += 1;
    setMeetingFormPrefill(null);
    meetingScrollRef.current = null;
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
  meetingFormRef.current = meetingFormPrefill;
  formPathRef.current = meetingFormPrefill
    ? "#requirements/new"
    : view === "requirementForm" && requirementForm
      ? requirementFormPath(requirementForm)
      : null;
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
    navigatedDuringBootRef.current = true;
    listScrollRef.current = null;
    scrollResetRef.current = true;
    resetDetailState();
    setView(nextView);
    // 从侧栏回到全部项目概览时不带上次的选中
    if (nextView === "graph") setOverviewSelection(null);
    setSearchActive(false);
    setTaskDrawerId(null);
    setPreviewTarget(null);
    // 默认清空词典预筛；openGlossaryForProject 会在这之后同一批更新里重新设上。
    setGlossaryProjectId(null);
  };

  // 离开表单页之前：有没保存的改动先确认（审查 B1）。确认了就清掉标记，免得接下来的跳转再问一遍
  const confirmLeaveForm = (): boolean => {
    if (!formPathRef.current || !formDirtyRef.current) return true;
    if (!window.confirm(LEAVE_FORM_CONFIRM)) return false;
    formDirtyRef.current = false;
    return true;
  };

  const navigate = (nextView: AppView) => {
    if (detailNavigationLocked) return;
    if (isMobile && (nextView === "jobs" || nextView === "graph")) return;
    if (!confirmLeaveForm()) return;
    if (
      detail &&
      detailDirty &&
      !window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")
    ) {
      return;
    }
    performNavigate(nextView);
  };

  // 应用内打开项目
  const openProjectDetail = (projectId: string) => {
    setOpenProjectId(projectId);
    // 从列表、别的页进项目一律先看「需求与任务」；关系图只由 #projects/<id>/graph 深链或点标签进入
    setProjectMode("list");
    setGraphSelection(null);
    setGraphFocus(null);
    setGraphExpanded(null);
    setGraphLocal(null);
    performNavigate("projectDetail");
  };

  // 会议页、需求页的「在关系图里看」：打开项目的关系图并选中目标；目标在时间窗外时后端自动放宽。
  // 全部项目概览里双击岛进来时不选中什么
  const openProjectGraph = (projectId: string, selection: string | null = null, local: GraphLocal | null = null) => {
    setOpenProjectId(projectId);
    setProjectMode("graph");
    setGraphSelection(selection);
    setGraphFocus(selection);
    setGraphExpanded(null);
    setGraphLocal(local);
    performNavigate("projectDetail");
  };

  // 4f：进出局部图、换中心。进来和每换一次中心压一条历史；［回到关系图］一次退回星图，
  // 冷启动深链进来的（state 里没有 localDepth）就替换地址去掉 file、trace
  const changeGraphLocal = (next: GraphLocal | null, options: { replace?: boolean } = {}) => {
    if (next === null) {
      const depth = (window.history.state as { localDepth?: number } | null)?.localDepth;
      if (depth && !options.replace) {
        localExitRef.current = true;
        window.history.go(-depth);
        return;
      }
      localPushRef.current = false;
      setGraphLocal(null);
      return;
    }
    localPushRef.current = !options.replace;
    // 从展开一场会的决议面板进来：展开和局部图互斥，收起展开
    setGraphExpanded(null);
    setGraphLocal(next);
  };

  // 收起展开的会：展开是本应用压进来的那一条历史就后退，地址栏和视角一起回去
  const changeGraphExpand = (meetingId: string | null) => {
    if (meetingId === null && (window.history.state as { graphExpand?: boolean } | null)?.graphExpand) {
      window.history.back();
      return;
    }
    // 展开一场会和局部图互斥
    if (meetingId !== null) setGraphLocal(null);
    setGraphExpanded(meetingId);
  };

  // 全部项目概览面板里的［打开项目页］：打开清单，不改这个项目记住的视图
  const openProjectList = (projectId: string) => {
    setOpenProjectId(projectId);
    setProjectMode("list");
    setGraphSelection(null);
    setGraphFocus(null);
    setGraphExpanded(null);
    setGraphLocal(null);
    performNavigate("projectDetail");
  };

  const changeProjectMode = (mode: ProjectViewMode) => {
    if (!openProjectId) return;
    setProjectMode(mode);
    setGraphSelection(null);
    setGraphFocus(null);
    setGraphExpanded(null);
    setGraphLocal(null);
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

  // 新增、认领、修改需求的二级页（R04-1）：保存或放弃后回到进入前的页面
  const openRequirementCreate = (prefill?: RequirementPrefill) => {
    if (prefill && openMeetingId) {
      // 面包屑第一段写这场会是从哪儿打开的，和会议页的返回按钮一致
      const carried: RequirementPrefill = { ...prefill, from: searchActive ? "检索结果" : VIEW_LABELS[view] };
      meetingScrollRef.current = {
        page: document.documentElement.scrollTop,
        transcript: document.querySelector<HTMLElement>(".transcript-scroll")?.scrollTop ?? 0,
      };
      // 会议这一条记下选的那一句：建完需求后退回来时会议是重新打开的，停在这一句，接着往下挑
      window.history.replaceState(
        { ...((window.history.state as Record<string, unknown> | null) ?? {}), resumeAt: prefill.source.anchor_ms },
        "",
      );
      // 先压历史、再盖上新增页：新增页一挂上就滚到顶，等它挂上以后再压，浏览器给会议这一条记下的滚动位置就是 0，
      // 后退时它按 0 恢复，盖掉我们放回去的位置。来源记在这一条里，前进后退、刷新回到这一条时还在
      history.pushState(
        { app: true, meetingForm: carried },
        "",
        `${window.location.pathname}${window.location.search}#requirements/new`,
      );
      historySyncRef.current = false;
      setMeetingFormPrefill(carried);
      return;
    }
    setRequirementForm({ mode: "create", prefill });
    performNavigate("requirementForm");
  };

  // 新增页从会议页上揭开（会议还开着）：滚动放回盖上之前的位置
  useLayoutEffect(() => {
    const saved = meetingScrollRef.current;
    if (meetingFormPrefill || !saved || !openMeetingId) return;
    meetingScrollRef.current = null;
    const box = document.querySelector<HTMLElement>(".transcript-scroll");
    if (box) box.scrollTop = saved.transcript;
    document.documentElement.scrollTop = saved.page;
  }, [meetingFormPrefill, openMeetingId]);

  // 从会议退回需求详情、详情的数据到了：滚回打开会议之前的位置
  const restoreListScroll = useCallback(() => {
    const saved = listScrollRef.current;
    if (saved === null) return;
    listScrollRef.current = null;
    document.documentElement.scrollTop = saved;
  }, []);

  const openRequirementEdit = (requirementId: string) => {
    setRequirementForm({ mode: "edit", requirementId });
    performNavigate("requirementForm");
  };

  const openCandidateClaim = (candidateId: string) => {
    setRequirementForm({ mode: "claim", candidateId });
    performNavigate("requirementForm");
  };

  // 冷启动直接打开二级页、没有可退的历史时去哪：修改回需求详情，逐字稿选句建的回那场会，其余回需求池
  const formFallback = (): (() => void) | undefined => {
    if (requirementForm?.mode === "edit") {
      const { requirementId } = requirementForm;
      return () => openRequirementDetail(requirementId);
    }
    if (requirementForm?.mode === "create" && requirementForm.prefill) {
      const meetingId = requirementForm.prefill.source.meeting_id;
      return () => openMeeting(meetingId);
    }
    return undefined;
  };

  const leaveRequirementForm = () => {
    if (meetingFormPrefill) {
      // 盖在会议页上的：后退一条就揭开；没有可退的（不会发生，兜底）就地揭开
      if ((window.history.state as { app?: boolean } | null)?.app) window.history.back();
      else setMeetingFormPrefill(null);
      return;
    }
    // 是本应用压进来的历史就后退，回到进入前的页签和筛选
    const fallback = formFallback();
    if ((window.history.state as { app?: boolean } | null)?.app) window.history.back();
    else if (fallback) fallback();
    else navigate("requirements");
  };

  const finishRequirementForm = (result: RequirementFormResult) => {
    const { requirement } = result;
    if (result.kind === "edited") {
      // 改完回到需求详情（S11 → S12）
      setRequirementFlash("已保存");
      void refreshProjects();
      leaveRequirementForm();
      return;
    }
    if (result.kind === "created") {
      // 新增需求创建后直接进新需求的详情页，轻提示「需求已创建」（R04-8、S09-d）。新增页那一条历史换成详情页，
      // 后退回到进新增页之前的地方：需求池，或者选句的那场会
      setRequirementFlash("需求已创建");
      void refreshProjects();
      openRequirementDetail(requirement.id);
      historySyncRef.current = true;
      return;
    }
    const candidateId = requirementForm?.mode === "claim" ? requirementForm.candidateId : undefined;
    // 认领后回到「进行中」，新海报按排序落位、高亮 3 秒（R01-13、S02-c）；合并后回到「待认领」，提示带［撤销］（R01-14、S03-b）
    let unfiltered = false;
    if (result.kind === "merged") {
      writePersistentState(POOL_TAB_KEY, "pending", { local: true });
    } else {
      writePersistentState(POOL_TAB_KEY, "active", { local: true });
      // 记着的筛选会把新海报挡住（项目、优先级不含它，名称搜不到它）：清空筛选，不然「挂上墙了」墙上却没有
      const projectIds = readPersistentState<string[]>(POOL_PROJECTS_KEY, [], POOL_PROJECTS_STORE);
      const priorities = readPersistentState<string[]>(POOL_PRIORITIES_KEY, [], POOL_PRIORITIES_STORE);
      const query = readPersistentState(POOL_QUERY_KEY, "", POOL_QUERY_STORE).trim().toLowerCase();
      unfiltered =
        (projectIds.length > 0 && !projectIds.includes(requirement.project_id)) ||
        (priorities.length > 0 && !priorities.includes(requirement.priority)) ||
        (query !== "" && !requirement.title.toLowerCase().includes(query));
      if (unfiltered) {
        writePersistentState(POOL_PROJECTS_KEY, [], POOL_PROJECTS_STORE);
        writePersistentState(POOL_PRIORITIES_KEY, [], POOL_PRIORITIES_STORE);
        writePersistentState(POOL_QUERY_KEY, "", POOL_QUERY_STORE);
      }
    }
    setPoolFlash(
      result.kind === "merged"
        ? {
            message: mergedMessage(requirement.title),
            undoMergeCandidateId: candidateId,
          }
        : {
            message: `已认领「${requirement.title}」${unfiltered ? "；原来的筛选会挡住它，已清空筛选" : ""}`,
            highlightId: requirement.id,
          },
    );
    void refreshProjects();
    leaveRequirementForm();
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
    const path = meetingFormPrefill
      ? "#requirements/new"
      : openMeetingId
      ? `#meetings/${encodeURIComponent(openMeetingId)}`
      : view === "glossary"
        ? glossaryProjectId
          ? `#glossary/project/${glossaryProjectId}`
          : "#glossary"
        : view === "projectDetail" && openProjectId
          ? projectGraphPath(openProjectId, !isMobile && projectMode === "graph", graphSelection, graphExpanded, graphLocal)
          : view === "graph"
            ? overviewPath(overviewSelection)
            : view === "requirementDetail" && openRequirementId
              ? `#requirements/${openRequirementId}`
              : view === "requirementForm" && requirementForm
                ? requirementFormPath(requirementForm)
              : view === "overview"
                ? ""
                : view === "projectDetail" || view === "requirementDetail" || view === "requirementForm"
                  ? ""
                  : `#${view}`;
    const fromHistory = historySyncRef.current;
    historySyncRef.current = false;
    // 4f：进局部图、来龙去脉的那一条（冷启动深链、从别的页进来，state 里没有 localDepth）记 localRoot，
    // ［回到关系图］退到它上面时换成星图；地址不再带 file、trace 时去掉这个记号
    const nowLocal = localParam(path);
    const rootState = () => {
      const state = (window.history.state ?? null) as Record<string, unknown> | null;
      if (nowLocal && !state?.localDepth) return { ...(state ?? {}), app: true, localRoot: true };
      if (!nowLocal && state?.localRoot) {
        const { localRoot: _dropped, ...rest } = state;
        return rest;
      }
      return state;
    };
    if (window.location.hash === path) {
      // 冷启动深链：地址不用改，只给这一条记上 localRoot
      const state = window.history.state as { localRoot?: boolean; localDepth?: number } | null;
      if (nowLocal && !state?.localDepth && !state?.localRoot) history.replaceState(rootState(), "", window.location.href);
      return;
    }
    const url = window.location.pathname + window.location.search + path;
    // 关系图里换选中只改地址栏的 ?sel=，不压历史，后退键直接回到上一个页面；
    // 展开一场会压一条（后退键收起），展开着换到前后场只替换
    const sameBase = window.location.hash.split("?")[0] === path.split("?")[0];
    const wasExpanded = expandParam(window.location.hash);
    const nowExpanded = expandParam(path);
    // 4f：进局部图、来龙去脉和在里面每换一次中心压一条历史，记下第几层（［回到关系图］一次退回去）
    const pushLocal = localPushRef.current;
    localPushRef.current = false;
    if (!fromHistory && sameBase && pushLocal && nowLocal && nowLocal !== localParam(window.location.hash)) {
      const depth = ((window.history.state as { localDepth?: number } | null)?.localDepth ?? 0) + 1;
      history.pushState({ app: true, graphLocal: true, localDepth: depth }, "", url);
    } else if (!fromHistory && sameBase && !wasExpanded && nowExpanded) {
      history.pushState({ app: true, graphExpand: true }, "", url);
    } else if (fromHistory || sameBase) history.replaceState(rootState(), "", url);
    // 打开一场会时记下它盖在哪个视图上：后退回到这场会时照它放回去（返回按钮、侧栏对得上）
    else
      history.pushState(
        nowLocal ? { app: true, localRoot: true } : path.startsWith("#meetings/") ? { app: true, behind: view } : { app: true },
        "",
        url,
      );
  }, [
    bootSettled,
    glossaryProjectId,
    graphExpanded,
    graphLocal,
    graphSelection,
    isMobile,
    meetingFormPrefill,
    openMeetingId,
    openProjectId,
    openRequirementId,
    overviewSelection,
    projectMode,
    requirementForm,
    view,
  ]);

  // 紧跟在上面「视图 → 地址栏」之后：新的一条历史已经压进去了
  useEffect(() => {
    if (!scrollResetRef.current) return;
    scrollResetRef.current = false;
    document.documentElement.scrollTop = 0;
  });

  // 浏览器前进/后退或手动改地址栏 hash 时反向同步视图。
  // 浏览器在 hash 变了的前进、后退（以及手改地址栏）里会先后发 popstate、hashchange，同一次导航只能认一次：
  // 第二遍时会议已经关了，会被当成「从别处切过来」清掉检索结果；没来得及渲染的话还会把「放弃修改吗」再问一遍。
  // popstate 处理完记下落到的地址（处理中可能替换、压回了地址），紧跟着的 hashchange 读到的还是它就跳过
  useEffect(() => {
    let handledHref: string | null = null;
    const onPopState = () => {
      handledHref = null;
      applyHash();
      handledHref = window.location.href;
    };
    const onHashChange = () => {
      const handled = handledHref;
      handledHref = null;
      if (handled === window.location.href) return;
      applyHash();
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener("hashchange", onHashChange);
    return () => {
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("hashchange", onHashChange);
    };
  }, [applyHash]);

  // word：点「也可以搜」换一个词；scope：结果页换范围
  const submitSearch = async (overrides: { word?: string; scope?: string } = {}) => {
    if (detailNavigationLocked) return;
    if (!confirmLeaveForm()) return;
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
    navigatedDuringBootRef.current = true;
    listScrollRef.current = null;
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
        // 输入法用回车选字时浏览器也派发回车（Safari 尤其），不拦会隐式提交表单、把没敲完的拼音拿去搜
        onKeyDown={(event) => {
          if (event.key === "Enter" && isComposingKeydown(event)) event.preventDefault();
        }}
        maxLength={200}
        placeholder="搜索会议、原句、材料或关键词"
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
    // 会议不存在、读不出来：和项目、需求不存在一样给一个回去的入口
    content = (
      <div className="detail-error" role="alert">
        <span>{detailError || "会议档案读取失败"}</span>
        <button onClick={closeMeeting} type="button">
          ← 返回{searchActive ? "检索结果" : VIEW_LABELS[view]}
        </button>
      </div>
    );
  } else if (detail) {
    const meetingPage = (
      <MeetingDetailPage
        apiClient={apiClient}
        canWriteTasks={!isMobile || mobileTaskWrite}
        initialSeekMs={initialSeekMs}
        autoplay={initialAutoplay}
        covered={meetingFormPrefill !== null}
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
        onOpenMeeting={(meetingId, seekMs) => openMeeting(meetingId, seekMs)}
        onOpenPreview={setPreviewTarget}
        onOpenPendingCandidates={() => {
          writePersistentState(POOL_TAB_KEY, "pending", { local: true });
          navigate("requirements");
        }}
        onCreateRequirement={(prefill) => {
          if (detailNavigationLocked) return;
          if (detailDirty && !window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")) return;
          openRequirementCreate(prefill);
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
    // 会议页始终挂在同一个位置，盖上新增页时只是藏起来：取消回来，查找词、播放进度、没保存的修改都还在。
    // 结构要两种情况一样，换成另一层 React 会把会议页卸掉重建；没盖的时候 display: contents，排版和没有这层一样
    content = (
      <>
        <div style={{ display: meetingFormPrefill ? "none" : "contents" }}>{meetingPage}</div>
        {meetingFormPrefill && (
          <RequirementFormPage
            apiClient={apiClient}
            canPickFolders={!isMobile}
            key="new-from-meeting"
            mode="create"
            onCancel={leaveRequirementForm}
            onDirtyChange={(dirty) => {
              formDirtyRef.current = dirty;
            }}
            onDone={finishRequirementForm}
            onOpenProject={(projectId) => confirmLeaveForm() && openProjectDetail(projectId)}
            onOpenRequirement={(requirementId) => confirmLeaveForm() && openRequirementDetail(requirementId)}
            prefill={meetingFormPrefill}
            projects={projects}
          />
        )}
      </>
    );
  } else if (searchActive) {
    content = (
      <SearchPage
        error={searchError}
        onOpen={(meetingId, startMs, tab) => openMeeting(meetingId, startMs, false, tab)}
        onOpenMaterial={(fileId, startMs, passage) => setPreviewTarget({ fileId, startMs, passage })}
        // 4g：把问题交给项目的问答，打开项目（按存的模式），不自动发
        onAskProject={
          typeof apiClient.askPrepare === "function"
            ? (projectId, question) => {
                setAskDraft(projectId, question);
                openProjectDetail(projectId);
              }
            : undefined
        }
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
        jobsFailed={jobsStale}
        jobsInteractive={!isMobile}
        meetings={meetings}
        onOpenJobs={() => navigate("jobs")}
        onOpenLibrary={() => navigate("library")}
        onOpenMeeting={openMeeting}
        onOpenTasks={() => navigate("tasks")}
        attributionSummary={attributionSummary}
        canPickFolders={!isMobile}
        canReveal={canReveal}
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
        onConfirmProject={
          isMobile
            ? undefined
            : async (meetingId) => {
                await apiClient.confirmMeetingProject(meetingId);
                await Promise.all([loadMeetings(filters, meetingOffset, true), loadAttributionSummary()]);
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
    // 手机端需求池本期不改（R02 备注），维持原来的只读列表
    content = isMobile ? (
      <RequirementsPage
        apiClient={apiClient}
        canPickFolders={!isMobile}
        canWrite={!isMobile || mobileTaskWrite}
        onOpenProject={openProjectDetail}
        onOpenRequirement={openRequirementDetail}
        onProjectsChanged={refreshProjects}
        projects={projects}
      />
    ) : (
      <RequirementPoolPage
        apiClient={apiClient}
        canWrite
        flash={poolFlash}
        onClaimCandidate={openCandidateClaim}
        onCreateRequirement={() => openRequirementCreate()}
        onFlashShown={() => setPoolFlash(null)}
        onOpenMeeting={openMeetingAtQuote}
        onOpenRequirement={openRequirementDetail}
        onProjectsChanged={refreshProjects}
      />
    );
  } else if (view === "requirementForm" && requirementForm) {
    content = (
      <RequirementFormPage
        apiClient={apiClient}
        canPickFolders={!isMobile}
        candidateId={requirementForm.mode === "claim" ? requirementForm.candidateId : undefined}
        key={
          requirementForm.mode === "claim"
            ? requirementForm.candidateId
            : requirementForm.mode === "edit"
              ? `edit-${requirementForm.requirementId}`
              : "new"
        }
        mode={requirementForm.mode}
        onCancel={leaveRequirementForm}
        onDirtyChange={(dirty) => {
          formDirtyRef.current = dirty;
        }}
        onDone={finishRequirementForm}
        onOpenProject={(projectId) => confirmLeaveForm() && openProjectDetail(projectId)}
        onOpenRequirement={(requirementId) => confirmLeaveForm() && openRequirementDetail(requirementId)}
        prefill={requirementForm.mode === "create" ? requirementForm.prefill : undefined}
        projects={projects}
        requirementId={requirementForm.mode === "edit" ? requirementForm.requirementId : undefined}
      />
    );
  } else if (view === "requirementDetail" && openRequirementId) {
    content = (
      <RequirementDetailPage
        apiClient={apiClient}
        canPickFolders={!isMobile}
        canWrite={!isMobile || mobileTaskWrite}
        backLabel={requirementFromGraph ? "关系图" : undefined}
        flash={requirementFlash}
        onBack={leaveRequirement}
        // 电脑上编辑需求是二级页（R04-1）；手机端本期不改，还是原来的弹窗
        onEdit={isMobile ? undefined : () => openRequirementEdit(openRequirementId)}
        onFlashShown={() => setRequirementFlash(null)}
        onOpenInGraph={isMobile ? undefined : (projectId, requirementId) => openProjectGraph(projectId, `r:${requirementId}`)}
        onOpenMeeting={openMeetingAtQuote}
        onOpenPreview={(fileId) => setPreviewTarget({ fileId })}
        onOpenProject={openProjectDetail}
        onOpenTask={setTaskDrawerId}
        onProjectsChanged={refreshProjects}
        onReady={restoreListScroll}
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
        onClaimCandidate={openCandidateClaim}
        onOpenMeeting={openMeeting}
        onOpenPreview={(fileId) => setPreviewTarget({ fileId })}
        onOpenProject={openProjectDetail}
        onOpenRequirement={openRequirementDetail}
        onTasksChanged={() => void loadPendingCount(true)}
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
        onOpenMeeting={(meetingId, seekMs) => openMeeting(meetingId, seekMs)}
        onPendingChange={loadGlossaryPending}
        projects={projects}
      />
    );
  } else if (view === "projectDetail" && openProjectId) {
    content = (
      <ProjectDetailPage
        apiClient={apiClient}
        key={openProjectId}
        // 关系图是项目详情的一个标签页；地址栏里的视图（深链、前进后退）通过 viewMode 带进来
        graphTab={
          isMobile ? undefined : (
            <ProjectGraph
              apiClient={apiClient}
              expanded={graphExpanded}
              focus={graphFocus}
              local={graphLocal}
              onLocalChange={changeGraphLocal}
              onOpenTask={setTaskDrawerId}
              onBack={() => navigate("projects")}
              onExpandChange={changeGraphExpand}
              onOpenAttributionReview={() => {
                applyFilters({ attribution: "needs_review" });
                navigate("library");
              }}
              onOpenGlossary={openGlossaryForProject}
              onOpenMeeting={openMeeting}
              onOpenPreview={(fileId, startMs) => setPreviewTarget({ fileId, startMs })}
              // 4g：问答出处带时间和标签页打开会议；材料出处打开预览抽屉到那一段
              onOpenMeetingAt={(meetingId, seekMs, tab) => openMeeting(meetingId, seekMs ?? 0, false, tab)}
              onOpenPreviewTarget={setPreviewTarget}
              onOpenProject={openProjectDetail}
              onOpenRequirement={openRequirementFromGraph}
              onProjectsChanged={refreshProjects}
              onSelectionChange={setGraphSelection}
              projectId={openProjectId}
              projects={projects}
              selection={graphSelection}
            />
          )
        }
        viewMode={isMobile ? "list" : projectMode}
        onViewModeChange={changeProjectMode}
        onClaimCandidates={(projectId) => {
          // 去需求池的「待认领」页签，只筛出本项目
          writePersistentState(POOL_TAB_KEY, "pending", { local: true });
          writePersistentState(POOL_PROJECTS_KEY, [projectId], POOL_PROJECTS_STORE);
          // 记着的等级筛选、搜索词会把本项目的候选挡在外面，落到空页：一起清掉
          writePersistentState(POOL_PRIORITIES_KEY, [], POOL_PRIORITIES_STORE);
          writePersistentState(POOL_QUERY_KEY, "", POOL_QUERY_STORE);
          navigate("requirements");
        }}
        canPickFolders={!isMobile}
        canReveal={canReveal}
        canWrite={!isMobile || mobileTaskWrite}
        onBack={() => navigate("projects")}
        onOpenGlossary={openGlossaryForProject}
        // 4c：openMeeting 的第三个参数是 fromHistory，时间线给的是 (id, 毫秒, 标签页)
        onOpenMeeting={(meetingId, seekMs, tab) => openMeeting(meetingId, seekMs, false, tab)}
        onOpenRequirement={openRequirementDetail}
        onOpenPreview={(fileId) => setPreviewTarget({ fileId })}
        onOpenPreviewTarget={setPreviewTarget}
        isMobile={isMobile}
        onOpenTask={setTaskDrawerId}
        onProjectUpdated={refreshProjects}
        onOpenProject={openProjectDetail}
        onProjectsChanged={refreshProjects}
        projectId={openProjectId}
        projects={projects}
        reloadKey={boardVersion}
      />
    );
  } else if (view === "graph" && !isMobile) {
    content = (
      <OverviewGraph
        apiClient={apiClient}
        onOpenLibrary={(filter) => {
          applyFilters(filter === "none" ? { project_id: "none" } : { attribution: "new_project" });
          navigate("library");
        }}
        onOpenMeeting={openMeeting}
        onOpenProject={openProjectList}
        onOpenProjectGraph={(projectId) => openProjectGraph(projectId)}
        onOpenRequirement={openRequirementDetail}
        onProjectsChanged={refreshProjects}
        onSelectionChange={setOverviewSelection}
        projects={projects}
        selection={overviewSelection}
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
        onUpload={async (file, hotwords) => {
          setUploadPercent(0);
          try {
            const receipt = await uploadRecordingInChunks(apiClient, file, hotwords, (sent, total) =>
              setUploadPercent(total > 0 ? Math.floor((sent / total) * 100) : 0),
            );
            await loadJobs();
            return receipt.job_id
              ? `已保存并入队：${receipt.job_id}（${receipt.size_bytes.toLocaleString()} 字节）`
              : `录音已安全保存在本机（${receipt.size_bytes.toLocaleString()} 字节），后台会自动重试入队`;
          } finally {
            setUploadPercent(null);
          }
        }}
        stale={jobsStale}
        state={jobsState}
        uploadPercent={uploadPercent}
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
    <LinksFlagsContext.Provider value={linksFlags}>
      <RecentAnswersContext.Provider value={recentAnswers}>
        <AppShell
          activeView={meetingFormPrefill ? "requirementForm" : view}
          glossaryBadge={glossaryPending}
          health={healthLevel}
          isMobile={isMobile}
          navigationLocked={detailNavigationLocked}
          onNavigate={navigate}
          searchSlot={searchSlot}
          taskBadge={pendingCount}
        >
          <ErrorBoundary
            onReset={() => {
              if (openMeetingId) void loadDetail(openMeetingId);
            }}
            resetKey={[view, openMeetingId, searchActive, openProjectId, openRequirementId].join("|")}
          >
            <FadeContent transitionKey={view}>{content}</FadeContent>
          </ErrorBoundary>
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
              onOpenPreview={(fileId) => setPreviewTarget({ fileId })}
              onOpenRequirement={openRequirementDetail}
              taskId={taskDrawerId}
            />
          )}
          {previewTarget && (
            <MaterialPreviewDrawer
              apiClient={apiClient}
              canReveal={canReveal}
              canWrite={!isMobile || mobileTaskWrite}
              fileId={previewTarget.fileId}
              isMobile={isMobile}
              key={`${previewTarget.fileId}:${previewTarget.startMs ?? ""}:${
                previewTarget.passage ? `${previewTarget.passage.contentKey}:${previewTarget.passage.ordinal}` : ""
              }`}
              onClose={() => setPreviewTarget(null)}
              onOpenInGraph={(projectId, fileId) => {
                setPreviewTarget(null);
                openProjectGraph(projectId, `file:${fileId}`);
              }}
              onOpenTrace={(projectId, node) => {
                setPreviewTarget(null);
                openProjectGraph(projectId, null, { kind: "trace", node });
              }}
              onOpenMeeting={(meetingId, seekMs) => {
                setPreviewTarget(null);
                openMeeting(meetingId, seekMs ?? 0);
              }}
              onOpenTask={(taskId) => {
                setPreviewTarget(null);
                setTaskDrawerId(taskId);
              }}
              passage={previewTarget.passage}
              startMs={previewTarget.startMs}
            />
          )}
        </AppShell>
      </RecentAnswersContext.Provider>
    </LinksFlagsContext.Provider>
  );
}
