import type {
  AttentionPayload,
  AttributionSummary,
  FolderMatchesPayload,
  MeetingAttribution,
  BootstrapPayload,
  HealthPayload,
  Job,
  JobSubstate,
  JobSubstateName,
  JobSubstateStatus,
  JobsPayload,
  MaterialBrowsePayload,
  MaterialRoot,
  ProjectMeetingRow,
  ProjectSubfoldersPayload,
  RequirementDetail,
  RequirementFilesPayload,
  RequirementFilters,
  RequirementPriority,
  RequirementStatus,
  RequirementsPayload,
  AsrGoldSample,
  AsrShadowRun,
  GlossaryConfirmResult,
  GlossaryScope,
  GlossarySuggestion,
  GlossaryTarget,
  GlossaryTerm,
  GlossaryTermConflict,
  MeetingDetail,
  MeetingFilters,
  MinutesBackend,
  MeetingSummary,
  MeetingsPayload,
  Project,
  ProjectBoard,
  SearchPayload,
  Segment,
  Tag,
  Task,
  TaskConfirmResult,
  TaskDetail,
  TaskFilters,
  TasksPayload,
  TranscriptComparisonPayload,
  MinutesEvidence,
  TranscriptVersion,
  SimilarProjectSuggestion,
  ColdStartFoldersPayload,
  LegacyGroupsSummary,
  CardsBanner,
  MeetingCard,
  MeetingGlossary,
  MeetingCardEffect,
  ProjectCardsSummary,
} from "./types";
import type {
  CollapsedPayload,
  CueTermDetail,
  ExpandPayload,
  FulltextPayload,
  GraphPayload,
  GraphRootsPayload,
  GraphWindow,
  MeetingBrief,
  MeetingFocus,
  QuotesPayload,
} from "./components/graph/graphTypes";

let csrfToken = "";

interface RelayJobPayload {
  job_id: string;
  status: Job["state"];
  meeting_id?: string | null;
  meeting_title?: string | null;
  stop_after_stage?: boolean;
  failure_stage?: string | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
  active_attempt_id?: string | null;
  whisper_status?: JobSubstateStatus;
  index_status?: JobSubstateStatus;
  substates?: Partial<Record<JobSubstateName, JobSubstate>>;
}

function normalizeJob(job: RelayJobPayload): Job {
  return {
    id: job.job_id,
    meeting_id: job.meeting_id,
    meeting_title: job.meeting_title,
    state: job.status,
    stop_after_stage: job.stop_after_stage ? 1 : 0,
    failure_stage: job.failure_stage,
    failure_reason: job.last_error,
    created_at: job.created_at,
    updated_at: job.updated_at,
    active_attempt_id: job.active_attempt_id,
    whisper_status: job.substates?.whisper?.status ?? job.whisper_status,
    index_status: job.substates?.index?.status ?? job.index_status,
    substates: job.substates,
  };
}

export class ApiError extends Error {
  readonly status: number;
  /** 原始响应体（例如 409 带回来的 suggestion） */
  readonly data: unknown;

  constructor(message: string, status: number, data?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

/** 新建、改词条撞上已有词条时，从 409 里取出那条词条；别的错误返回 null。 */
export function termConflictFrom(error: unknown): GlossaryTermConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const data = error.data as { conflict?: GlossaryTermConflict } | null | undefined;
  return data?.conflict ?? null;
}

/** 新建项目撞上近似重名时，从 409 里取出已有的那个项目；别的错误返回 null。 */
export function similarProjectFrom(error: unknown): SimilarProjectSuggestion | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const data = error.data as { suggestion?: SimilarProjectSuggestion } | null | undefined;
  return data?.suggestion ?? null;
}

export type ConflictResolutionAction = "keep_draft" | "accept_external" | "discard_draft";

export interface UploadSession {
  upload_id: string;
  chunk_bytes: number;
  chunk_count: number;
}

export interface UploadReceipt {
  path: string;
  size_bytes: number;
  status: string;
  job_id: string | null;
}

export function setCsrfToken(token: string) {
  csrfToken = token;
}

function queryString(values: object): string {
  const params = new URLSearchParams();
  Object.entries(values as Record<string, string | number | undefined>).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

function formatValidationLocation(location: unknown): string {
  if (!Array.isArray(location)) return "";
  const fieldNames: Record<string, string> = {
    hotwords: "热词",
    filename: "文件名",
    size_bytes: "文件大小",
  };
  return location
    .filter((part) => part !== "body" && part !== "query" && part !== "path")
    .map((part) => typeof part === "number" ? `第 ${part + 1} 项` : fieldNames[String(part)] ?? String(part))
    .join(" ");
}

function formatErrorDetail(data: unknown, status: number): string {
  if (typeof data === "string" && data) return data;
  if (typeof data !== "object" || data === null || !("detail" in data)) {
    return `请求失败（${status}）`;
  }
  const detail = (data as { detail: unknown }).detail;
  if (typeof detail === "string" && detail) return detail;
  const entries = Array.isArray(detail) ? detail : [detail];
  const messages = entries.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null) return [];
    const message = "msg" in entry && typeof entry.msg === "string" ? entry.msg : "";
    if (!message) return [];
    const location = "loc" in entry ? formatValidationLocation(entry.loc) : "";
    return [location ? `${location}：${message}` : message];
  });
  return messages.length ? messages.join("；") : `请求失败（${status}）`;
}

async function parseResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type") ?? "";
  const data = contentType.includes("application/json")
    ? ((await response.json()) as unknown)
    : await response.text();
  if (!response.ok) {
    const detail = formatErrorDetail(data, response.status);
    throw new ApiError(detail, response.status, data);
  }
  return data as T;
}

async function read<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  return parseResponse<T>(response);
}

async function write<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  body: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}

export const api = {
  read,
  write,
  async bootstrap() {
    const payload = await read<BootstrapPayload>("/api/bootstrap");
    setCsrfToken(payload.csrf_token);
    return payload;
  },
  health: () => read<HealthPayload>("/api/health"),
  attention: () => read<AttentionPayload>("/api/attention"),
  acknowledgeJob: (jobId: string) =>
    write<{ job_id: string; acknowledged_at: string }>(
      `/api/jobs/${encodeURIComponent(jobId)}/acknowledge`,
      "POST",
      {},
    ),
  meetings: (filters: MeetingFilters = {}) =>
    read<MeetingsPayload>(
      `/api/meetings${queryString(filters)}`,
    ),
  meeting: (meetingId: string) => read<MeetingDetail>(`/api/meetings/${meetingId}`),
  transcriptVersionSegments: (meetingId: string, versionId: string) =>
    read<{ version: TranscriptVersion; items: Segment[] }>(
      `/api/meetings/${meetingId}/transcript-versions/${versionId}/segments`,
    ),
  transcriptComparison: (meetingId: string, candidateVersionId: string) =>
    read<TranscriptComparisonPayload>(
      `/api/meetings/${meetingId}/transcript-comparison${queryString({ candidate_version_id: candidateVersionId })}`,
    ),
  asrGoldSamples: (meetingId: string) =>
    read<{ items: AsrGoldSample[] }>(`/api/meetings/${meetingId}/asr-gold-samples`),
  saveAsrGoldSample: (
    meetingId: string,
    sample: Pick<AsrGoldSample, "reference" | "entities" | "numbers" | "tags"> & { segment_id: string },
  ) => write<AsrGoldSample>(`/api/meetings/${meetingId}/asr-gold-samples`, "POST", sample),
  requestQwenShadow: (meetingId: string) =>
    write<AsrShadowRun>(`/api/meetings/${meetingId}/asr-shadow/qwen`, "POST", {}),
  retryQwenShadow: (meetingId: string, runId: string) =>
    write<AsrShadowRun>(
      `/api/meetings/${meetingId}/asr-shadow/qwen/${encodeURIComponent(runId)}/retry`,
      "POST",
      {},
    ),
  minutesEvidence: (meetingId: string) =>
    read<MinutesEvidence>(`/api/meetings/${meetingId}/minutes-evidence`),
  /** projectId：不传搜全部；"none" 只搜没归项目的会 */
  search: (query: string, projectId?: string) =>
    read<SearchPayload>(`/api/search${queryString({ q: query, project_id: projectId })}`),
  // ---------------------------------------------------------------- 关系图（1g）
  /** window 不传：默认 28 天，会少时自动放宽；focus：深链目标，如 "m:<会议 id>" */
  graph: (projectId: string, window?: GraphWindow, focus?: string) =>
    read<GraphPayload>(
      `/api/graph/projects/${encodeURIComponent(projectId)}${queryString({ window, focus })}`,
    ),
  graphRoots: (projectId: string) =>
    read<GraphRootsPayload>(`/api/graph/projects/${encodeURIComponent(projectId)}/roots`),
  graphCollapsed: (projectId: string, group: string, window?: GraphWindow) =>
    read<CollapsedPayload>(
      `/api/graph/projects/${encodeURIComponent(projectId)}/collapsed${queryString({ group, window })}`,
    ),
  meetingBrief: (meetingId: string) =>
    read<MeetingBrief>(`/api/meetings/${encodeURIComponent(meetingId)}/brief`),
  /** span=wide：决议、任务面板要前后各 20 秒 */
  meetingQuotes: (meetingId: string, at: number[], span?: "wide") => {
    const params = new URLSearchParams();
    at.forEach((value) => params.append("at", String(Math.round(value))));
    if (span) params.append("span", span);
    const encoded = params.toString();
    return read<QuotesPayload>(
      `/api/meetings/${encodeURIComponent(meetingId)}/quotes${encoded ? `?${encoded}` : ""}`,
    );
  },
  glossaryTermDetail: (termId: string) =>
    read<CueTermDetail>(`/api/glossary/terms/${encodeURIComponent(termId)}`),
  // ---------------------------------------------------------------- 关系图（1h）
  graphMeetingFocus: (meetingId: string) =>
    read<MeetingFocus>(`/api/graph/meetings/${encodeURIComponent(meetingId)}`),
  /** 材料根目录里的一层；dir 是相对根目录的路径 */
  graphExpand: (rootId: number, dir = "") =>
    read<ExpandPayload>(`/api/graph/expand${queryString({ root: rootId, dir })}`),
  /** 一个词在这个项目全部逐字稿里的次数；term 是词条 id，会连错写、也叫一起数 */
  graphFulltext: (projectId: string, query: { q?: string; term?: string }) =>
    read<FulltextPayload>(
      `/api/graph/projects/${encodeURIComponent(projectId)}/fulltext${queryString(query)}`,
    ),
  revealMaterial: (path: string) =>
    write<{ ok: boolean; path: string }>("/api/materials/reveal", "POST", { path }),
  projects: () => read<Project[]>("/api/projects"),
  tags: () => read<Tag[]>("/api/tags"),
  createProject: (name: string, color: string, materialRoots?: string[]) =>
    write<Project>(
      "/api/projects",
      "POST",
      materialRoots ? { name, color, material_roots: materialRoots } : { name, color },
    ),
  /** 新建项目的完整入口：近似重名时 409（ApiError.data.suggestion），force 仍然新建。 */
  createProjectWith: (body: {
    name: string;
    color: string;
    material_roots?: string[];
    folder?: { mode: "mount" | "create"; path: string; name?: string };
    meeting_ids?: string[];
    force?: boolean;
  }) => write<Project>("/api/projects", "POST", body),
  deleteProject: (projectId: string) =>
    write<{ ok: boolean; tasks_unassigned: number; terms_to_public: number }>(
      `/api/projects/${encodeURIComponent(projectId)}`,
      "DELETE",
      {},
    ),
  mergeProject: (projectId: string, targetId: string) =>
    write<Project>(
      `/api/projects/${encodeURIComponent(projectId)}/merge-into/${encodeURIComponent(targetId)}`,
      "POST",
      {},
    ),
  folderMatches: (name?: string) =>
    read<FolderMatchesPayload>(`/api/projects/folder-matches${queryString({ name })}`),
  projectFolderSuggestions: (projectId: string) =>
    read<FolderMatchesPayload>(
      `/api/projects/${encodeURIComponent(projectId)}/folder-suggestions`,
    ),
  ignoreProjectName: (name: string) =>
    write<{ name: string; meetings_updated: number }>("/api/project-names/ignore", "POST", { name }),
  attributionSummary: () => read<AttributionSummary>("/api/attribution/summary"),
  coldStartFolders: () => read<ColdStartFoldersPayload>("/api/cold-start/folders"),
  declineFolderSuggestions: (projectIds: string[]) =>
    write<{ ok: boolean }>("/api/cold-start/folders/decline", "POST", { project_ids: projectIds }),
  snoozeFolderSuggestions: () =>
    write<{ snoozed_until: string }>("/api/cold-start/folders/snooze", "POST", {}),
  legacyGroups: () => read<{ summary: LegacyGroupsSummary | null }>("/api/glossary/legacy-groups"),
  undoLegacyGroups: () => write<{ restored: number }>("/api/glossary/legacy-groups/undo", "POST", {}),
  dismissLegacyGroups: () => write<{ ok: boolean }>("/api/glossary/legacy-groups/dismiss", "POST", {}),
  meetingGlossary: (meetingId: string) =>
    read<{ glossary: MeetingGlossary | null }>(`/api/meetings/${encodeURIComponent(meetingId)}/glossary`),
  /** projectId 不传：沿用上次按哪个项目查；null：回到默认；项目 id：按这个项目查 */
  checkMeetingGlossary: (meetingId: string, projectId?: string | null) =>
    write<{ glossary: MeetingGlossary | null }>(
      `/api/meetings/${encodeURIComponent(meetingId)}/glossary/check`,
      "POST",
      projectId === undefined ? {} : { project_id: projectId },
    ),
  applyMeetingGlossary: (meetingId: string, baseVersionId: string) =>
    write<{ version_id: string; replaced: number; glossary: MeetingGlossary | null }>(
      `/api/meetings/${encodeURIComponent(meetingId)}/glossary/apply`,
      "POST",
      { base_version_id: baseVersionId },
    ),
  undoMeetingGlossary: (meetingId: string) =>
    write<{ version_id: string; glossary: MeetingGlossary | null }>(
      `/api/meetings/${encodeURIComponent(meetingId)}/glossary/undo`,
      "POST",
      {},
    ),
  meetingCard: (meetingId: string) => read<MeetingCard>(`/api/meetings/${encodeURIComponent(meetingId)}/card`),
  meetingCardAction: (meetingId: string, action: "rewrite" | "regenerate") =>
    write<MeetingCard>(`/api/meetings/${encodeURIComponent(meetingId)}/card`, "POST", { action }),
  cardsBanner: () => read<CardsBanner>("/api/cards/banner"),
  answerCardsBackfill: (answer: "yes" | "no" | "later") =>
    write<{ answer: string }>("/api/cards/backfill", "POST", { answer }),
  dismissCardsNotice: (projectId: string) =>
    write<{ ok: boolean }>("/api/cards/notices/dismiss", "POST", { project_id: projectId }),
  revealCards: (target: { project_id?: string; meeting_id?: string }) =>
    write<{ path: string }>("/api/cards/reveal", "POST", target),
  retireAllCards: () =>
    write<{ retired: number; kept: { meeting_id: string; title: string | null; path: string }[] }>(
      "/api/cards/retire-all",
      "POST",
      {},
    ),
  enableCards: () => write<{ ok: boolean }>("/api/cards/enable", "POST", {}),
  pauseProjectCards: (projectId: string) =>
    write<{ retired: number }>(`/api/projects/${encodeURIComponent(projectId)}/cards/pause`, "POST", {}),
  resumeProjectCards: (projectId: string) =>
    write<{ cards: ProjectCardsSummary; written: number }>(
      `/api/projects/${encodeURIComponent(projectId)}/cards/resume`,
      "POST",
      {},
    ),
  confirmMeetingProject: (meetingId: string) =>
    write<MeetingAttribution>(
      `/api/meetings/${encodeURIComponent(meetingId)}/project/confirm`,
      "POST",
      {},
    ),
  undoMeetingProject: (meetingId: string) =>
    write<Omit<MeetingDetail, "effects"> & { effects?: { tasks_restored: number; card?: MeetingCardEffect } }>(
      `/api/meetings/${encodeURIComponent(meetingId)}/project/undo`,
      "POST",
      {},
    ),
  createTag: (name: string, color: string) =>
    write<Tag>("/api/tags", "POST", { name, color }),
  updateProject: (
    projectId: string,
    data: { name?: string; color?: string; material_roots?: string[]; also_names?: string[] },
  ) =>
    write<Project>(`/api/projects/${encodeURIComponent(projectId)}`, "PATCH", data),
  projectBoard: (projectId: string) =>
    read<ProjectBoard>(`/api/projects/${encodeURIComponent(projectId)}/board`),
  tasks: (filters: TaskFilters = {}) =>
    read<TasksPayload>(`/api/tasks${queryString(filters)}`),
  task: (taskId: string) => read<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`),
  createTask: (data: {
    title: string;
    detail?: string;
    project_id?: string | null;
    requirement_id?: string | null;
    assignee?: string;
  }) =>
    write<TaskDetail>("/api/tasks", "POST", data),
  updateTask: (
    taskId: string,
    data: {
      title?: string;
      detail?: string;
      project_id?: string | null;
      requirement_id?: string | null;
      assignee?: string;
    },
  ) => write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`, "PATCH", data),
  confirmTask: (
    taskId: string,
    data: {
      title?: string;
      detail?: string;
      project_id?: string | null;
      requirement_id?: string | null;
      assignee?: string;
    } = {},
  ) => write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/confirm`, "POST", data),
  rejectTask: (taskId: string) =>
    write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/reject`, "POST", {}),
  setTaskStatus: (taskId: string, status: Task["status"]) =>
    write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/status`, "POST", { status }),
  batchConfirmTasks: (taskIds: string[]) =>
    write<TaskConfirmResult>("/api/tasks/batch-confirm", "POST", { task_ids: taskIds }),
  batchRejectTasks: (taskIds: string[]) =>
    write<{ rejected: string[]; failed: Array<{ task_id: string; error: string }> }>(
      "/api/tasks/batch-reject",
      "POST",
      { task_ids: taskIds },
    ),
  undoTaskReview: (taskIds: string[]) =>
    write<{ reverted: string[]; failed: Array<{ task_id: string; error: string }> }>(
      "/api/tasks/undo-review",
      "POST",
      { task_ids: taskIds },
    ),
  addTaskComment: (taskId: string, body: string) =>
    write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/comments`, "POST", { body }),
  addDeliverable: (
    taskId: string,
    data: { kind: string; url: string; title?: string; note?: string; mark_done?: boolean },
  ) => write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/deliverables`, "POST", data),
  reExtractTasks: (meetingId: string, supplement: string) =>
    write<{ status: string }>(
      `/api/meetings/${encodeURIComponent(meetingId)}/tasks/re-extract`,
      "POST",
      { supplement },
    ),
  /** 只带改过的字段。project_id："" 表示不归项目，"__ai__" 表示交还 AI 判断。 */
  updateMeeting: (
    meetingId: string,
    metadata: { project_id?: string; tag_ids?: string[]; title?: string; requirement_ids?: string[] },
  ) => write<MeetingDetail>(`/api/meetings/${meetingId}`, "PATCH", metadata),
  // ---------------------------------------------------------------- 项目 → 需求 → 任务三层
  browseMaterials: (path?: string) =>
    read<MaterialBrowsePayload>(`/api/materials/browse${queryString({ path })}`),
  projectMaterialSubfolders: (projectId: string) =>
    read<ProjectSubfoldersPayload>(
      `/api/projects/${encodeURIComponent(projectId)}/material-subfolders`,
    ),
  addProjectMaterialRoot: (projectId: string, path: string) =>
    write<MaterialRoot>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots`,
      "POST",
      { path },
    ),
  replaceProjectMaterialRoot: (projectId: string, rootId: number, path: string) =>
    write<MaterialRoot>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}/replace`,
      "POST",
      { path },
    ),
  removeProjectMaterialRoot: (projectId: string, rootId: number) =>
    write<{ ok: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}`,
      "DELETE",
      {},
    ),
  projectMeetings: (projectId: string) =>
    read<ProjectMeetingRow[]>(`/api/projects/${encodeURIComponent(projectId)}/meetings`),
  requirements: (filters: RequirementFilters = {}) =>
    read<RequirementsPayload>(`/api/requirements${queryString(filters)}`),
  requirement: (requirementId: string) =>
    read<RequirementDetail>(`/api/requirements/${encodeURIComponent(requirementId)}`),
  createRequirement: (data: {
    project_id: string;
    title: string;
    priority: RequirementPriority;
    folder_paths?: string[];
  }) => write<RequirementDetail>("/api/requirements", "POST", data),
  updateRequirement: (
    requirementId: string,
    data: {
      title?: string;
      project_id?: string;
      priority?: RequirementPriority;
      status?: RequirementStatus;
      folder_paths?: string[];
    },
  ) =>
    write<RequirementDetail>(
      `/api/requirements/${encodeURIComponent(requirementId)}`,
      "PATCH",
      data,
    ),
  requirementFolderFiles: (
    requirementId: string,
    folderId: number,
    params: { limit?: number; offset?: number } = {},
  ) =>
    read<RequirementFilesPayload>(
      `/api/requirements/${encodeURIComponent(requirementId)}/folders/${folderId}/files${queryString(params)}`,
    ),
  removeRequirementFolder: (requirementId: string, folderId: number) =>
    write<RequirementDetail>(
      `/api/requirements/${encodeURIComponent(requirementId)}/folders/${folderId}`,
      "DELETE",
      {},
    ),
  setRequirementMeetings: (requirementId: string, meetingIds: string[]) =>
    write<RequirementDetail>(
      `/api/requirements/${encodeURIComponent(requirementId)}/meetings`,
      "PUT",
      { meeting_ids: meetingIds },
    ),
  addRequirementMeeting: (requirementId: string, meetingId: string) =>
    write<RequirementDetail>(
      `/api/requirements/${encodeURIComponent(requirementId)}/meetings/${encodeURIComponent(meetingId)}`,
      "POST",
      {},
    ),
  removeRequirementMeeting: (requirementId: string, meetingId: string) =>
    write<RequirementDetail>(
      `/api/requirements/${encodeURIComponent(requirementId)}/meetings/${encodeURIComponent(meetingId)}`,
      "DELETE",
      {},
    ),
  attachRequirementTasks: (requirementId: string, taskIds: string[]) =>
    write<RequirementDetail>(
      `/api/requirements/${encodeURIComponent(requirementId)}/tasks`,
      "POST",
      { task_ids: taskIds },
    ),
  jobs: async () => {
    const payload = await read<{ items: RelayJobPayload[]; counts?: Record<string, number> }>("/api/jobs");
    return { ...payload, items: payload.items.map(normalizeJob) } satisfies JobsPayload;
  },
  saveTranscript: (
    meetingId: string,
    segments: Segment[],
    baseVersionId: string | null,
  ) =>
    write<{ version_id: string }>(`/api/meetings/${meetingId}/transcript`, "PUT", {
      base_version_id: baseVersionId,
      segments: segments.map(({ id, ordinal, start_ms, end_ms, speaker_label, speaker_name, text }) => ({
        id,
        ordinal,
        start_ms,
        end_ms,
        speaker_label,
        speaker_name,
        text,
      })),
    }),
  renameSpeaker: (meetingId: string, label: string, displayName: string) =>
    write<{ updated: number }>(`/api/meetings/${meetingId}/speakers/rename`, "POST", {
      label,
      display_name: displayName,
    }),
  splitSegment: (meetingId: string, segmentId: string, characterIndex: number) =>
    write<{ segment_ids: string[] }>(`/api/meetings/${meetingId}/segments/split`, "POST", {
      segment_id: segmentId,
      character_index: characterIndex,
    }),
  mergeSegments: (meetingId: string, firstSegmentId: string, secondSegmentId: string) =>
    write<{ segment_id: string }>(`/api/meetings/${meetingId}/segments/merge`, "POST", {
      first_segment_id: firstSegmentId,
      second_segment_id: secondSegmentId,
    }),
  saveMinutes: (meetingId: string, markdown: string, baseVersionId: string | null) =>
    write<{ version_id: string; corrections?: GlossarySuggestion[] }>(`/api/meetings/${meetingId}/minutes`, "PUT", {
      base_version_id: baseVersionId,
      markdown,
    }),
  regenerateMinutes: (meetingId: string, backend?: MinutesBackend) =>
    write<{ status: string }>(
      `/api/meetings/${meetingId}/minutes/regenerate`,
      "POST",
      backend ? { backend } : {},
    ),
  retranscribe: (meetingId: string, hotwords: string[] = []) =>
    write<{ status: string; job_id?: string }>(
      `/api/meetings/${meetingId}/retranscribe`,
      "POST",
      hotwords.length ? { hotwords } : {},
    ),
  resolveConflict: (
    meetingId: string,
    conflictId: string,
    action: ConflictResolutionAction,
  ) =>
    write<MeetingDetail>(
      `/api/meetings/${meetingId}/conflicts/${encodeURIComponent(conflictId)}/resolve`,
      "POST",
      { action },
    ),
  resolveLegacyConflict: (meetingId: string, action: ConflictResolutionAction) =>
    write<MeetingDetail>(`/api/meetings/${meetingId}/conflict/resolve`, "POST", { action }),
  rollbackTranscript: (meetingId: string, versionId: string) =>
    write<{ ok: boolean }>(`/api/meetings/${meetingId}/rollback/transcript`, "POST", {
      version_id: versionId,
    }),
  rollbackMinutes: (meetingId: string, versionId: string) =>
    write<{ ok: boolean }>(`/api/meetings/${meetingId}/rollback/minutes`, "POST", {
      version_id: versionId,
    }),
  publish: (meetingId: string) =>
    write<Record<string, unknown>>(`/api/meetings/${meetingId}/publish`, "POST", {}),
  retryJob: (jobId: string, stage: string, hotwords: string[] = []) =>
    write<Record<string, unknown>>(
      `/api/jobs/${jobId}/retry`,
      "POST",
      hotwords.length ? { stage, hotwords } : { stage },
    ),
  cancelJob: (jobId: string) => write<Record<string, unknown>>(`/api/jobs/${jobId}/cancel`, "POST", {}),
  stopAfterStage: (jobId: string) =>
    write<Record<string, unknown>>(`/api/jobs/${jobId}/stop-after-stage`, "POST", {}),
  retryJobSubstate: (jobId: string, name: JobSubstateName) =>
    write<Record<string, unknown>>(
      `/api/jobs/${jobId}/substates/${name}/retry`,
      "POST",
      {},
    ),
  glossaryTerms: (params: { scope?: string; project_id?: string } = {}) =>
    read<GlossaryTerm[]>(`/api/glossary/terms${queryString(params)}`),
  glossaryScopes: () => read<GlossaryScope[]>("/api/glossary/scopes"),
  createGlossaryTerm: (data: {
    term: string;
    aliases?: string[];
    scope?: string;
    project_id?: string | null;
    category?: string;
    source?: string;
    confirmed?: boolean;
    is_cue?: boolean;
    also?: string[];
  }) => write<GlossaryTerm>("/api/glossary/terms", "POST", data),
  updateGlossaryTerm: (
    termId: string,
    data: {
      term?: string;
      aliases?: string[];
      scope?: string;
      project_id?: string | null;
      category?: string;
      confirmed?: boolean;
      is_cue?: boolean;
      also?: string[];
    },
  ) =>
    write<GlossaryTerm>(
      `/api/glossary/terms/${encodeURIComponent(termId)}`,
      "PUT",
      data,
    ),
  /** 重名时把新写的错写、叫法并进已有词条；makePublic 时顺手改成公共词 */
  mergeGlossaryTerm: (termId: string, data: { aliases?: string[]; also?: string[]; make_public?: boolean }) =>
    write<GlossaryTerm>(`/api/glossary/terms/${encodeURIComponent(termId)}/merge`, "POST", data),
  deleteGlossaryTerm: (termId: string) =>
    write<{ ok: boolean }>(
      `/api/glossary/terms/${encodeURIComponent(termId)}`,
      "DELETE",
      {},
    ),
  glossarySuggestions: (status?: "pending" | "confirmed" | "rejected") =>
    read<GlossarySuggestion[]>(
      `/api/glossary/suggestions${queryString({ status })}`,
    ),
  confirmGlossarySuggestion: (suggestionId: string, options: { target?: GlossaryTarget; short?: boolean } = {}) =>
    write<GlossaryConfirmResult>(
      `/api/glossary/suggestions/${encodeURIComponent(suggestionId)}/confirm`,
      "POST",
      options,
    ),
  undoGlossarySuggestion: (suggestionId: string) =>
    write<{ ok: boolean; suggestion: GlossarySuggestion | null }>(
      `/api/glossary/suggestions/${encodeURIComponent(suggestionId)}/undo`,
      "POST",
      {},
    ),
  rejectGlossarySuggestion: (suggestionId: string) =>
    write<{ ok: boolean }>(
      `/api/glossary/suggestions/${encodeURIComponent(suggestionId)}/reject`,
      "POST",
      {},
    ),
  restoreGlossarySuggestion: (suggestionId: string) =>
    write<{ ok: boolean; suggestion: GlossarySuggestion | null }>(
      `/api/glossary/suggestions/${encodeURIComponent(suggestionId)}/restore`,
      "POST",
      {},
    ),
  startUpload: (filename: string, sizeBytes: number, hotwords: string[] = []) =>
    write<UploadSession>("/api/uploads/start", "POST", {
      filename,
      size_bytes: sizeBytes,
      ...(hotwords.length ? { hotwords } : {}),
    }),
  uploadChunk: (uploadId: string, index: number, contentBase64: string) =>
    write<{ upload_id: string; index: number; bytes: number; sha256: string }>(
      `/api/uploads/${encodeURIComponent(uploadId)}/chunks/${index}`,
      "PUT",
      { content_base64: contentBase64 },
    ),
  completeUpload: (uploadId: string) =>
    write<UploadReceipt>(
      `/api/uploads/${encodeURIComponent(uploadId)}/complete`,
      "POST",
      {},
    ),

};

export type ApiClient = typeof api;
