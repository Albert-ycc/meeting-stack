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
  SearchItem,
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

export default function App({ apiClient = api }: AppProps) {
  const isMobile = useMobileBreakpoint();
  // 落地页是工作台（最近的会、待确认任务、处理中的录音）；按日期回忆某场会走侧栏「录音档案」。
  const [view, setView] = useState<AppView>("overview");
  // 冷加载时地址栏里的 #tasks 等锚点要先被读进视图，之后才允许把视图反写回地址栏，
  // 否则首帧 view=overview 会先把 hash 清空，applyHash 再也读不到（冷加载 #tasks 被拉回工作台）。
  const hashReadyRef = useRef(false);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [healthUnreachable, setHealthUnreachable] = useState(false);
  const healthFailureCount = useRef(0);
  const [meetings, setMeetings] = useState<MeetingSummary[]>([]);
  const [meetingOffset, setMeetingOffset] = useState(0);
  const [meetingTotal, setMeetingTotal] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [openProjectId, setOpenProjectId] = useState<string | null>(null);
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
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [detailError, setDetailError] = useState("");
  const [initialSeekMs, setInitialSeekMs] = useState(0);
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState<"exact" | "semantic">("exact");
  const [searchItems, setSearchItems] = useState<SearchItem[]>([]);
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

  const applyHash = useCallback(() => {
    const hash = window.location.hash;
    if (hash.startsWith("#projects/")) {
      const projectId = decodeURIComponent(hash.slice("#projects/".length));
      if (projectId) {
        setOpenProjectId(projectId);
        setView("projectDetail");
      }
    } else if (hash.startsWith("#requirements/")) {
      const requirementId = decodeURIComponent(hash.slice("#requirements/".length));
      if (requirementId) {
        setOpenRequirementId(requirementId);
        setView("requirementDetail");
      }
    } else if (hash === "#requirements") {
      setView("requirements");
    } else if (hash === "#tasks") {
      setView("tasks");
    } else if (hash.startsWith("#glossary/project/")) {
      const projectId = decodeURIComponent(hash.slice("#glossary/project/".length));
      if (projectId) {
        setGlossaryProjectId(projectId);
        setView("glossary");
      }
    } else if (hash === "#glossary") {
      setGlossaryProjectId(null);
      setView("glossary");
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
        applyHash();
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

  const loadDetail = useCallback(async (meetingId: string) => {
    const requestSequence = ++detailRequestSequence.current;
    setDetailNavigationLocked(false);
    setDetailState("loading");
    setDetailError("");
    try {
      const payload = await apiClient.meeting(meetingId);
      if (requestSequence !== detailRequestSequence.current) return;
      setDetail(payload);
      setDetailDirty(false);
      setDetailState("ready");
    } catch (error) {
      if (requestSequence !== detailRequestSequence.current) return;
      setDetail(null);
      setDetailState("error");
      setDetailError(error instanceof Error ? error.message : "会议档案读取失败");
    }
  }, [apiClient]);

  const openMeeting = (meetingId: string, seekMs = 0) => {
    if (detailNavigationLocked) return;
    if (
      detail &&
      detailDirty &&
      !window.confirm("当前会议仍有未保存修改。放弃这些修改并打开其他会议吗？")
    ) {
      return;
    }
    setInitialSeekMs(seekMs);
    setSearchActive(false);
    setDetailDirty(false);
    void loadDetail(meetingId);
  };

  const performNavigate = (nextView: AppView) => {
    detailRequestSequence.current += 1;
    setView(nextView);
    setDetail(null);
    setDetailDirty(false);
    setDetailNavigationLocked(false);
    setDetailState("idle");
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

  const openProjectDetail = (projectId: string) => {
    setOpenProjectId(projectId);
    performNavigate("projectDetail");
  };

  const openRequirementDetail = (requirementId: string) => {
    setOpenRequirementId(requirementId);
    performNavigate("requirementDetail");
  };

  // 项目详情页「在词典中查看 →」：跳去词典页并预选中这个项目的 chip。
  const openGlossaryForProject = (projectId: string) => {
    performNavigate("glossary");
    setGlossaryProjectId(projectId);
  };

  // 与 #tasks / #glossary(/project/<id>) / #projects/<id> / #requirements(/<id>) 锚点同步，供飞书卡片跳转直达对应视图。
  useEffect(() => {
    if (!hashReadyRef.current) return;
    const path =
      view === "tasks"
        ? "#tasks"
        : view === "glossary"
          ? glossaryProjectId
            ? `#glossary/project/${glossaryProjectId}`
            : "#glossary"
          : view === "projectDetail" && openProjectId
            ? `#projects/${openProjectId}`
            : view === "requirementDetail" && openRequirementId
              ? `#requirements/${openRequirementId}`
              : view === "requirements"
                ? "#requirements"
                : "";
    if (window.location.hash !== path) {
      history.replaceState(null, "", window.location.pathname + window.location.search + path);
    }
  }, [glossaryProjectId, openProjectId, openRequirementId, view]);

  // 浏览器前进/后退或手动改地址栏 hash 时反向同步视图。
  useEffect(() => {
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, [applyHash]);

  const submitSearch = async () => {
    if (detailNavigationLocked) return;
    const normalized = query.trim();
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
    setDetail(null);
    setDetailDirty(false);
    setSearchActive(true);
    setSearchState("loading");
    setSearchError("");
    try {
      const payload = await apiClient.search(normalized, searchMode);
      if (requestSequence !== searchRequestSequence.current) return;
      setSearchItems(payload.items);
      setSearchState(payload.items.length ? "ready" : "empty");
    } catch (error) {
      if (requestSequence !== searchRequestSequence.current) return;
      setSearchItems([]);
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
      <div className="search-mode">
        <button aria-pressed={searchMode === "exact"} disabled={detailNavigationLocked} onClick={() => setSearchMode("exact")} type="button">原句</button>
        <button aria-pressed={searchMode === "semantic"} disabled={detailNavigationLocked} onClick={() => setSearchMode("semantic")} type="button">语义</button>
      </div>
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
        isMobile={isMobile}
        meeting={detail}
        onBack={() => performNavigate("library")}
        onClassificationSaved={refreshProjects}
        onDirtyChange={setDetailDirty}
        onNavigationLockChange={setDetailNavigationLocked}
        onOpenRequirement={openRequirementDetail}
        onOpenTasks={() => navigate("tasks")}
        onReload={async () => {
          await Promise.all([
            loadDetail(detail.id),
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
        items={searchItems}
        mode={searchMode}
        onOpen={openMeeting}
        query={query.trim()}
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
        onOpenAttributionReview={() => {
          applyFilters({ attribution: "needs_review" });
          navigate("library");
        }}
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
        onBack={() => navigate("requirements")}
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
    content = (
      <ProjectDetailPage
        apiClient={apiClient}
        canPickFolders={!isMobile}
        canWrite={!isMobile || mobileTaskWrite}
        onBack={() => navigate("projects")}
        onOpenGlossary={openGlossaryForProject}
        onOpenMeeting={openMeeting}
        onOpenRequirement={openRequirementDetail}
        onOpenTask={setTaskDrawerId}
        onProjectUpdated={refreshProjects}
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
        onUpload={async (file, hotwords) => {
          const receipt = await uploadRecordingInChunks(apiClient, file, hotwords);
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
