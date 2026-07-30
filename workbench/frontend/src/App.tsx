import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, type ApiClient } from "./api";
import type {
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
import { JobsPage } from "./components/JobsPage";
import { LibraryPage } from "./components/LibraryPage";
import { MeetingDetailPage } from "./components/MeetingDetailPage";
import { OverviewPage } from "./components/OverviewPage";
import { ProjectsPage } from "./components/ProjectsPage";
import { SearchPage } from "./components/SearchPage";
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
  // 实际使用方式是按日期回忆某场会，所以落地页直接是资料库。
  const [view, setView] = useState<AppView>("library");
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [meetings, setMeetings] = useState<MeetingSummary[]>([]);
  const [meetingOffset, setMeetingOffset] = useState(0);
  const [meetingTotal, setMeetingTotal] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [filters, setFilters] = useState<MeetingFilters>({});
  const [libraryState, setLibraryState] = useState<LoadState>("loading");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsAvailable, setJobsAvailable] = useState(false);
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

  useEffect(() => {
    let active = true;
    const initialize = async () => {
      try {
        await apiClient.bootstrap();
        const [healthPayload, projectPayload, tagPayload] = await Promise.all([
          apiClient.health(),
          apiClient.projects(),
          apiClient.tags(),
        ]);
        if (!active) return;
        setHealth(healthPayload);
        setProjects(projectPayload);
        setTags(tagPayload);
      } catch (error) {
        if (!active) return;
        setLibraryState("error");
        setDetailError(error instanceof Error ? error.message : "无法连接本地工作台");
      }
      if (active) {
        await Promise.all([loadMeetings({}, 0), loadJobs()]);
      }
    };
    void initialize();
    return () => {
      active = false;
    };
  }, [apiClient, loadJobs, loadMeetings]);

  const hasActiveJobs = useMemo(
    () => jobs.some((job) => !terminalJobStates.has(job.state)),
    [jobs],
  );

  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      void apiClient.health().then(setHealth).catch(() => undefined);
      void loadJobs(true);
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
  }, [apiClient, hasActiveJobs, loadJobs, view]);

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
    if (!health) return "unknown" as const;
    if (health.status === "ok") return "healthy" as const;
    return "degraded" as const;
  }, [health]);

  const searchSlot = (
    <form
      className="global-search"
      onSubmit={(event) => {
        event.preventDefault();
        void submitSearch();
      }}
      role="search"
    >
      <span aria-hidden="true" className="search-glyph">⌕</span>
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
      <button className="search-submit" disabled={detailNavigationLocked} type="submit">检索</button>
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
        initialSeekMs={initialSeekMs}
        isMobile={isMobile}
        meeting={detail}
        onBack={() => performNavigate("library")}
        onClassificationSaved={refreshProjects}
        onDirtyChange={setDetailDirty}
        onNavigationLockChange={setDetailNavigationLocked}
        onReload={async () => {
          await Promise.all([
            loadDetail(detail.id),
            loadMeetings(filters, meetingOffset, true),
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
        health={health}
        jobs={jobs}
        jobsAvailable={jobsAvailable}
        jobsInteractive={!isMobile}
        meetings={meetings}
        onOpenJobs={() => navigate("jobs")}
        onOpenLibrary={() => navigate("library")}
        onOpenMeeting={openMeeting}
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
        canEdit={!isMobile}
        meetings={meetings}
        onCreateProject={async (name, color) => {
          const created = await apiClient.createProject(name, color);
          setProjects((current) => [...current, created].sort((left, right) => left.name.localeCompare(right.name, "zh-CN")));
        }}
        onCreateTag={async (name, color) => {
          const created = await apiClient.createTag(name, color);
          setTags((current) => [...current, created].sort((left, right) => left.name.localeCompare(right.name, "zh-CN")));
        }}
        onOpenProject={(projectId) => {
          applyFilters({ project_id: projectId });
          navigate("library");
        }}
        projects={projects}
        tags={tags}
      />
    );
  }

  return (
    <AppShell
      activeView={view}
      health={healthLevel}
      isMobile={isMobile}
      navigationLocked={detailNavigationLocked}
      onNavigate={navigate}
      searchSlot={searchSlot}
    >
      {content}
    </AppShell>
  );
}
