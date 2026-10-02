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
  ProjectRecordingRow,
  ProjectWorkPayload,
  ProjectSubfoldersPayload,
  CandidateDetail,
  CandidateExtraction,
  RequirementSourceInput,
  TitleConflict,
  DroppedCandidates,
  MergeTargets,
  PoolFilters,
  RequirementDetail,
  RequirementFilesPayload,
  RequirementFilters,
  RequirementPoolPayload,
  RequirementPriority,
  RequirementStatus,
  RequirementsPayload,
  AsrGoldSample,
  AsrShadowRun,
  GlossaryCandidatesInbox,
  GlossaryConfirmResult,
  GlossaryScope,
  GlossarySuggestion,
  GlossaryTarget,
  GlossaryTerm,
  GlossaryTermConflict,
  MeetingDetail,
  MeetingFilters,
  MinutesBackend,
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
  MaterialWordAcceptResult,
  MaterialWordRejectResult,
  MaterialWordUndoResult,
  MaterialWordsList,
  RequirementContext,
  MeetingCardEffect,
  ProjectCardsSummary,
  ProjectParentStatus,
  ClaimItem,
  ClaimResult,
  RenameCandidatesPayload,
  MaterialRootRepoint,
  NameAsRequirementResult,
  NameCandidatesPayload,
  NameDecisionResult,
  MaterialCoverage,
  MaterialFilePreview,
  MaterialIndexStatus,
  MaterialUnreadablePage,
  AskJob,
  AskPlan,
  ConfirmAllResult,
  RequirementOptionsPayload,
  ReviewCardsPayload,
  TodoFilters,
  TodoPayload,
} from "./types";
import type {
  CardsFilesPayload,
  CollapsedPayload,
  CueTermDetail,
  ExpandPayload,
  FileMentionResult,
  FulltextPayload,
  GraphFileDetail,
  GraphPayload,
  GraphRootsPayload,
  GraphWindow,
  LocalGraph,
  MeetingBrief,
  MeetingFocus,
  QuotesPayload,
  RelatedEdges,
  TracePayload,
} from "./components/graph/graphTypes";
import type { GraphOverview, GraphOverviewFetch, OverviewFolders } from "./components/graph/overviewTypes";

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

/**
 * 第四期的新接口在旧后台上不存在：FastAPI 的 404 "Not Found" 或 405。
 * 404 的 detail 是中文（「这条关联已经不在了」）时是正式回答，不算旧后台。
 */
export function isOldBackend(e: unknown): boolean {
  return (
    e instanceof ApiError &&
    (e.status === 405 || (e.status === 404 && (e.data as { detail?: unknown } | null)?.detail === "Not Found"))
  );
}

// ---------------------------------------------------------------- 深度关联（4a）

/** 关联的类：提到、相关、产出、影响、后来改了、后来又提到 */
export type RelationKind = "mention" | "related" | "produced" | "affects" | "later_changed" | "restated";

/** 回答：pick 要带 file_id；restore 只对 rejected、resolved */
export type RelationAnswer = "yes" | "no" | "updated" | "pick" | "restore";

/** 关联行（服务端白名单输出，不带分数和原样的证据） */
export interface Relation {
  id: number;
  kind: RelationKind;
  status: string;
  by_you: boolean;
  meeting_id: string | null;
  at_ms: number | null;
  task_id: string | null;
  decision_id: string | null;
  to_decision_id: string | null;
  file: { id: number; name: string } | null;
  quote: string | null;
  evidence: unknown;
  decided_at: string | null;
}

/** 一个待回答的问题：文件面板、预览抽屉、任务抽屉、需求卡共用 */
export interface RelationQuestion {
  relation_id: number;
  kind: RelationKind;
  text: string;
  ask?: string | null;
  decision?: {
    id: string;
    text: string;
    date: string;
    meeting_id: string;
    meeting_title: string;
    start_ms: number | null;
    audio_url: string | null;
  } | null;
  task?: { id: string; title: string; status: string } | null;
  file: { id: number; name: string; folder?: string | null };
  /** 材料位置加现取的片段，最多 60 字；片段被回收时为 null */
  passage?: { loc: string; text: string } | null;
  words?: string[];
  answers: RelationAnswer[];
}

/** 各页面的一句话状态；action 为 retry 时就是［现在重试］（POST /api/links/retry） */
export interface LinksState {
  kind: "ok" | "waiting" | "stopped";
  text: string;
  action: { kind: string; label: string } | null;
}

// ---------------------------------------------------------------- 相关材料栏（4d）

/** 栏的状态：一句话、最多一个按钮；text 为 null 表示不写 */
export interface RelatedState {
  kind: "ok" | "waiting" | "stopped";
  text: string | null;
  action: { kind: "open_project"; label: string; project_id: string } | null;
}

/** 栏里用到的文件，按内容标识 */
export interface RelatedFile {
  file_id: number;
  name: string;
  ext: string;
  root_online: boolean;
  /** 音视频材料 */
  playable: boolean;
  /** ［用本机应用打开］出不出，前端只看它 */
  can_open: boolean;
  state_text: string;
}

/** 一条相关的材料片段（不带分数、名次和关联 id） */
export interface RelatedItem {
  content_key: string;
  ordinal: number;
  loc: string | null;
  /** 音视频材料的时间点 */
  start_ms: number | null;
  /** 以第一个共同词为中心，最多 120 字 */
  text: string;
  words: string[];
  /** ▶ 从会上这里放 */
  at_ms: number;
}

export interface RelatedWindow {
  start_ms: number;
  end_ms: number;
  items: RelatedItem[];
}

export interface RelatedMaterials {
  state: RelatedState;
  files: Record<string, RelatedFile>;
  /** 这场会自己的另一份记录（逐字稿导出到资料盘） */
  copies: Array<{ file_id: number; name: string }>;
  windows: RelatedWindow[];
  /** 标过不相关的份数 */
  rejected: number;
}

export interface RelatedRejected {
  items: Array<{ relation_id: number; name: string; decided_at: string }>;
}

export interface RelationAnswerResult {
  relation: Relation;
  /** 撤销期以它为准（回答时间加 600 秒） */
  undo_until: string;
  deliverable_id?: number | null;
}

export interface RelationUndoResult {
  relation: Relation;
  removed_deliverable_id: number | null;
}

/** null 表示回到自动；ai 只用于撤销时把原值原样发回 */
export type DecisionPlacement = "none" | "picked" | "ai" | null;

export interface DecisionPlacementResult {
  decision: { id: string; placement: DecisionPlacement; requirement_id: string | null } & Record<string, unknown>;
  /** 撤销时原样发回 */
  undo: { placement: DecisionPlacement; requirement_id: string | null };
  undo_until: string;
}

// ---------------------------------------------------------------- 决议日志和时间线（4c）

/** 另一条决议（后来改了、这次改了的那一头）：需求卡和展开一场会同一个样子 */
export interface DecisionLinkRef {
  relation_id: number;
  decision_id: string;
  meeting: { id: string; title: string; date: string };
  text: string;
  start_ms: number | null;
  quote: string;
  /** 那场会的录音；没有录音时为 null，这时不出 ▶ */
  audio_url: string | null;
}

/** 后来又提到：挂在一组里最早那条下面，每个后来的会一行 */
export interface DecisionRestatedRef {
  relation_id: number | null;
  decision_id: string;
  meeting: { id: string; title: string; date: string };
  start_ms: number | null;
  audio_url: string | null;
}

/** 你标过［不是一回事］的：过了 600 秒也能从这里改回（restore） */
export interface DecisionDismissed {
  relation_id: number;
  kind: "later_changed" | "restated";
  other: { date: string; meeting_title: string; text: string };
  decided_at: string | null;
}

/** only 只关联一个需求；title 需求名对上；ai AI 放的；picked 你放的；unplaced 没归到具体需求 */
export type DecisionPlacementHow = "only" | "title" | "ai" | "picked" | "unplaced";

export interface DecisionLogEntry {
  /** 台账落后或关联整理关着时为 null：按纪要现读，没有标记和按钮 */
  id: string | null;
  text: string;
  detail: string;
  start_ms: number | null;
  end_ms: number | null;
  placement: { how: DecisionPlacementHow; requirement_id: string | null };
  later: DecisionLinkRef[];
  earlier: DecisionLinkRef[];
  restated: DecisionRestatedRef[];
  dismissed: DecisionDismissed[];
  /** 4e 起有内容 */
  stale_files?: RelationQuestion[];
}

export interface DecisionLogMeeting {
  meeting: { id: string; title: string; date: string; audio_url: string | null };
  /** 「这场纪要没有决议段」这类说法，有决议时为 null */
  note: string | null;
  decisions: DecisionLogEntry[];
  unplaced: DecisionLogEntry[];
}

export interface RequirementDecisionLog {
  requirement: { id: string; title: string };
  counts: { decisions: number; later_changed: number; unplaced: number };
  state: LinksState | { kind: "ok"; text: null; action: null };
  meetings: DecisionLogMeeting[];
}

export type TimelineKind = "all" | "decisions" | "tasks" | "files";

export interface TimelineDecision {
  id: string;
  text: string;
  start_ms: number | null;
  end_ms?: number | null;
  detail?: string;
  later: { date: string; text: string } | null;
}

export type TimelineItem =
  | {
      type: "meeting";
      at: string | null;
      time: string | null;
      meeting: { id: string; title: string; duration_sec: number | null; audio_url: string | null };
      decisions: TimelineDecision[];
      decisions_more: number;
    }
  | {
      type: "tasks";
      event: "confirmed" | "done";
      at: string | null;
      time: string | null;
      tasks: Array<{ id: string; title: string }>;
      more: number;
    }
  | {
      type: "deliverable";
      at: string | null;
      time: string | null;
      task: { id: string; title: string };
      deliverable: { id: number; name: string };
    }
  | {
      type: "files";
      at: string | null;
      time: string | null;
      root_id: number;
      /** 文件夹只给最后一段；根目录本身为空 */
      folder: string;
      added: number;
      changed: number;
      /** 记录开始前按修改时间归到这天的文件个数 */
      count?: number;
      names: string[];
      prelog: boolean;
    }
  | {
      type: "decision";
      at: string | null;
      time: string | null;
      decision: TimelineDecision;
      meeting: { id: string; title: string; audio_url: string | null };
      requirement: { id: string; title: string; how: DecisionPlacementHow } | null;
      how: DecisionPlacementHow | "project" | "none";
      linked_requirement_ids: string[];
    };

export interface TimelineDay {
  day: string;
  label: string;
  items: TimelineItem[];
  more_dirs: number;
}

export interface ProjectTimelinePayload {
  kind: TimelineKind;
  days: TimelineDay[];
  requirements: Array<{ id: string; title: string }>;
  next_before: string | null;
  file_log_since: string | null;
  state: { kind: "ok" | "waiting" | "stopped"; reason: string | null; text: string | null; action: { kind: string; label: string } | null };
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

/** 材料原文件的播放地址：从搜索的 ▶ 进来时不等预览数据，由 file_id 直接拼出 */
export function materialMediaUrl(fileId: number): string {
  return `/api/materials/files/${fileId}/media`;
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
    title: "名称",
  };
  return location
    .filter((part) => part !== "body" && part !== "query" && part !== "path")
    .map((part) => typeof part === "number" ? `第 ${part + 1} 项` : fieldNames[String(part)] ?? String(part))
    .join(" ");
}

/**
 * pydantic v2 内置校验器的 msg 是英文原文（"String should have at most 200 characters"），
 * D9 的场景（需求名超长）就是这种。这里只翻这几种固定文案的内置消息——能精确匹配上的才翻，
 * 翻不出来的（比如后端自定义 Value error 校验器写的话）原样返回，不额外杜撰通用兜底文案，
 * 免得把后端本来就写清楚的话盖掉。
 */
function translateValidationMessage(message: string): string {
  const maxChars = message.match(/^String should have at most (\d+) characters?$/);
  if (maxChars) return `不能超过 ${maxChars[1]} 个字`;
  const minChars = message.match(/^String should have at least (\d+) characters?$/);
  if (minChars) return minChars[1] === "1" ? "不能为空" : `不能少于 ${minChars[1]} 个字`;
  const maxItems = message.match(/^(?:List|Set|Tuple) should have at most (\d+) items?$/);
  if (maxItems) return `最多 ${maxItems[1]} 项`;
  const minItems = message.match(/^(?:List|Set|Tuple) should have at least (\d+) items?$/);
  if (minItems) return `至少要 ${minItems[1]} 项`;
  const ge = message.match(/^Input should be greater than or equal to (-?\d+(?:\.\d+)?)$/);
  if (ge) return `不能小于 ${ge[1]}`;
  const gt = message.match(/^Input should be greater than (-?\d+(?:\.\d+)?)$/);
  if (gt) return `要大于 ${gt[1]}`;
  if (message === "Field required") return "不能为空";
  return message;
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
    const rawMessage = "msg" in entry && typeof entry.msg === "string" ? entry.msg : "";
    if (!rawMessage) return [];
    const message = translateValidationMessage(rawMessage);
    const location = "loc" in entry ? formatValidationLocation(entry.loc) : "";
    // 翻成中文的消息直接接在字段名后面念成一句话；翻不出来的保持原来的「字段：原文」分隔
    const translated = message !== rawMessage;
    return [location ? (translated ? `${location}${message}` : `${location}：${message}`) : message];
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

// 后端的 CSRF 令牌跟进程同寿命：重启过（或页面启动时没取到）以后，手上的令牌全部作废。
// 文案照 backend/meeting_workbench/security.py 的两句 403
const CSRF_REJECTIONS = new Set(["CSRF 校验失败", "缺少 CSRF 校验"]);
let csrfRefresh: Promise<void> | null = null;

/** 重取启动接口换上新令牌（它顺带重设 cookie）；同一时刻撞 403 的几个写请求共用这一次 */
function refreshCsrfToken(): Promise<void> {
  csrfRefresh ??= read<BootstrapPayload>("/api/bootstrap")
    .then((payload) => setCsrfToken(payload.csrf_token))
    .finally(() => {
      csrfRefresh = null;
    });
  return csrfRefresh;
}

async function isCsrfRejection(response: Response): Promise<boolean> {
  if (response.status !== 403) return false;
  try {
    const data = (await response.clone().json()) as { detail?: unknown };
    return typeof data.detail === "string" && CSRF_REJECTIONS.has(data.detail);
  } catch {
    return false;
  }
}

async function write<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  body: Record<string, unknown>,
): Promise<T> {
  const send = (token: string) =>
    fetch(path, {
      method,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": token,
      },
      body: JSON.stringify(body),
    });
  const sentToken = csrfToken;
  const response = await send(sentToken);
  if (!(await isCsrfRejection(response))) return parseResponse<T>(response);
  // 别的请求已经换过令牌了就不用再取；只重放这一次，重放还失败照常报错
  if (csrfToken === sentToken) await refreshCsrfToken();
  return parseResponse<T>(await send(csrfToken));
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
  /**
   * 4f：关系图的相关线（4d 的接口）。带上次的 etag 时发 If-None-Match：没变是 304，返回 related: null，
   * 照旧用手上的那份。只在［相关］开着时取。
   */
  graphRelated: async (
    projectId: string,
    window: GraphWindow,
    etag?: string | null,
  ): Promise<{ related: RelatedEdges | null; etag: string | null }> => {
    const response = await fetch(
      `/api/graph/projects/${encodeURIComponent(projectId)}/related${queryString({ window })}`,
      {
        credentials: "same-origin",
        headers: { Accept: "application/json", ...(etag ? { "If-None-Match": etag } : {}) },
      },
    );
    if (response.status === 304) return { related: null, etag: response.headers.get("etag") ?? etag ?? null };
    const related = await parseResponse<RelatedEdges>(response);
    return { related, etag: response.headers.get("etag") };
  },
  /** 4f：以一份文件为中心的局部图（只查库，不缓存）；related 只在关系图的［相关］开着时为 true */
  graphFileMap: (fileId: number, options?: { related?: boolean }) =>
    read<LocalGraph>(`/api/graph/files/${fileId}/map${queryString({ related: options?.related ? 1 : 0 })}`),
  /** 4f：来龙去脉；node 是 file:、m:、dec:、task: 开头的 id */
  graphTrace: (node: string) => read<TracePayload>(`/api/graph/trace${queryString({ node })}`),
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
  // ---------------------------------------------------------------- 全部项目概览（2c）
  /**
   * 全部项目概览。带上次的 etag 时发 If-None-Match：没变是 304，返回 overview: null，照旧用手上的那份。
   */
  getGraphOverview: async (window: GraphWindow, etag?: string | null): Promise<GraphOverviewFetch> => {
    const response = await fetch(`/api/graph/overview${queryString({ window })}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...(etag ? { "If-None-Match": etag } : {}) },
    });
    if (response.status === 304) return { overview: null, etag: response.headers.get("etag") ?? etag ?? null };
    const overview = await parseResponse<GraphOverview>(response);
    return { overview, etag: response.headers.get("etag") };
  },
  /** 项目总文件夹下还没挂的文件夹（读缓存）；state 是 checking 时 2 秒后再取 */
  getOverviewFolders: () => read<OverviewFolders>("/api/graph/overview/folders"),
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
  // ---------------------------------------------------------------- 会上提到的文件、文件名索引（2d）
  /** 文件面板：文件信息、同名的其他文件、在哪几场会上被提到；404 是文件不在索引里 */
  getGraphFile: (fileId: number) => read<GraphFileDetail>(`/api/graph/files/${fileId}`),
  /** 声档会议记录里的文件（3g）：只给文件名和 file_id */
  graphCardsFiles: (projectId: string) =>
    read<CardsFilesPayload>(`/api/graph/projects/${encodeURIComponent(projectId)}/cards-files`),
  /** ［不是这份文件］：只挡这场会；立即生效 */
  rejectFileMention: (meetingId: string, stemKey: string) =>
    write<FileMentionResult>(
      `/api/meetings/${encodeURIComponent(meetingId)}/file-mentions/${encodeURIComponent(stemKey)}/reject`,
      "POST",
      {},
    ),
  /** 撤销［不是这份文件］ */
  restoreFileMention: (meetingId: string, stemKey: string) =>
    write<FileMentionResult>(
      `/api/meetings/${encodeURIComponent(meetingId)}/file-mentions/${encodeURIComponent(stemKey)}/restore`,
      "POST",
      {},
    ),
  /** ［换成这份］：换成同名的另一份文件，以后不再自动改；400 是不在这个项目文件夹里或不同名 */
  pickFileMention: (meetingId: string, stemKey: string, fileId: number) =>
    write<FileMentionResult>(
      `/api/meetings/${encodeURIComponent(meetingId)}/file-mentions/${encodeURIComponent(stemKey)}/pick`,
      "POST",
      { file_id: fileId },
    ),
  /** 每个根目录的文件名索引进度 */
  getMaterialIndexStatus: (projectId: string) =>
    read<MaterialIndexStatus>(`/api/materials/index-status${queryString({ project_id: projectId })}`),
  /** 每个根目录的内容读了多少、为什么停、读不了的分类数（3e） */
  getMaterialCoverage: (projectId: string) =>
    read<MaterialCoverage>(`/api/materials/coverage${queryString({ project_id: projectId })}`),
  /** 读不了的文件，每页 100 个 */
  getMaterialUnreadable: (projectId: string, rootId: number, offset = 0) =>
    read<MaterialUnreadablePage>(
      `/api/materials/unreadable${queryString({ project_id: projectId, root_id: rootId, offset })}`,
    ),
  /** 预览抽屉的数据；parts=preview 只要状态和预览（关系图文件面板用）；passage 定位到那一段（4d） */
  getMaterialPreview: (
    fileId: number,
    parts?: "preview",
    passage?: { contentKey: string; ordinal: number } | null,
  ) =>
    read<MaterialFilePreview>(
      `/api/materials/files/${fileId}/preview${queryString({
        parts,
        passage_key: passage?.contentKey,
        passage_ordinal: passage?.ordinal,
      })}`,
    ),
  // ---------------------------------------------------------------- 相关材料栏（4d）
  /** 会议页的相关材料栏；打开时服务端顺手把这场会排到前面（只在内存里） */
  relatedMaterials: (meetingId: string) =>
    read<RelatedMaterials>(`/api/meetings/${encodeURIComponent(meetingId)}/related-materials`),
  /** 标过不相关的；［改回相关］用 answerRelation(id, {answer: "restore"}) */
  relatedRejected: (meetingId: string) =>
    read<RelatedRejected>(`/api/meetings/${encodeURIComponent(meetingId)}/related-materials/rejected`),
  /** 栏里的［不相关］：没连成线的也能标（服务端建一行） */
  rejectRelatedMaterial: (meetingId: string, body: { content_key: string; file_id: number }) =>
    write<RelationAnswerResult>(
      `/api/meetings/${encodeURIComponent(meetingId)}/related-materials/reject`,
      "POST",
      body,
    ),
  /** 列表里的小签「3 场会提到」：一个列表一次请求，最多 200 个 */
  mentionedCounts: (fileIds: number[]) =>
    read<{ counts: Record<string, number> }>(
      `/api/materials/mentioned-counts${queryString({ file_ids: fileIds.join(",") })}`,
    ),
  /** ［用本机应用打开］：只在这台 Mac 上 */
  openMaterialFile: (fileId: number) => write<{ ok: boolean }>(`/api/materials/files/${fileId}/open`, "POST", {}),
  projects: () => read<Project[]>("/api/projects"),
  tags: () => read<Tag[]>("/api/tags"),
  createProject: (name: string, color: string, materialRoots?: string[]) =>
    write<Project>(
      "/api/projects",
      "POST",
      materialRoots ? { name, color, material_roots: materialRoots } : { name, color },
    ),
  /**
   * 新建项目的完整入口：近似重名（或完全同名）时 409（ApiError.data.suggestion），force 仍然新建。
   * source_name：从「像是新项目」提示建时 AI 起的名字，建好后它和最终名字都不再提示。
   */
  createProjectWith: (body: {
    name: string;
    color: string;
    material_roots?: string[];
    folder?: { mode: "mount" | "create"; path: string; name?: string };
    meeting_ids?: string[];
    force?: boolean;
    source_name?: string;
  }) => write<Project>("/api/projects", "POST", body),
  /** 撤销建成项目时自动记进也叫的会上叫法；过期或已撤销是 409 */
  undoSpokenAlsoName: (projectId: string, eventId: number) =>
    write<{ ok: boolean; removed: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}/also-names/spoken/undo`,
      "POST",
      { event_id: eventId },
    ),
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
  /**
   * 「不是新项目」（kind 默认 project）/「不算新需求」（kind=requirement，要带 project_id）。
   * meeting_id 只用来记事件。返回的 event_id 用来撤销。
   */
  ignoreProjectName: (
    name: string,
    options: { kind?: "project" | "requirement"; project_id?: string; meeting_id?: string } = {},
  ) => write<NameDecisionResult>("/api/project-names/ignore", "POST", { name, ...options }),
  /** 撤销「不是新项目」「不算新需求」；过期或已撤销是 409 */
  undoNameDecision: (eventId: number) =>
    write<{ ok: boolean; meetings_restored: number }>("/api/name-decisions/undo", "POST", { event_id: eventId }),
  // ---------------------------------------------------------------- 像是新项目 / 新需求（2b）
  /** 提示的候选名、同名的会、默认动作和文件夹动作；文件夹缓存没好时 folders_state=checking */
  nameCandidates: (meetingId: string) =>
    read<NameCandidatesPayload>(`/api/meetings/${encodeURIComponent(meetingId)}/name-candidates`),
  /** 建成需求：会已归项目时 project_id 可省；没归项目时必填 */
  nameAsRequirement: (meetingId: string, body: { title: string; project_id?: string; folder_path?: string }) =>
    write<NameAsRequirementResult>(
      `/api/meetings/${encodeURIComponent(meetingId)}/name-as-requirement`,
      "POST",
      body,
    ),
  /** 10 分钟内撤销建成需求；过期 409 */
  undoNameAsRequirement: (meetingId: string) =>
    write<{ ok: boolean; requirement_deleted: boolean; meetings_restored: number; meeting_ids: string[] }>(
      `/api/meetings/${encodeURIComponent(meetingId)}/name-as-requirement/undo`,
      "POST",
      {},
    ),
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
  /** 撤下补写的历史卡片：卡片照常开着，以后不再补写 */
  retireBackfilledCards: () =>
    write<{
      retired: number;
      kept: { meeting_id: string; title: string | null; path: string }[];
      /** 在没连接的资料盘上、这次撤不了的 */
      skipped?: number;
    }>(
      "/api/cards/retire-backfilled",
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
    due_date?: string | null;
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
      /** null＝清空截止 */
      due_date?: string | null;
      /** 和 requirement_id 二选一；null＝不挂候选 */
      candidate_id?: string | null;
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
      due_date?: string | null;
      candidate_id?: string | null;
    } = {},
  ) => write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/confirm`, "POST", data),
  /** 待办：已确认、进行中的任务按截止分五组（R07-4） */
  todo: (filters: TodoFilters = {}) => read<TodoPayload>(`/api/todo${queryString(filters)}`),
  /** 挂到需求的推荐和可选范围（R07-8、R07-14） */
  taskRequirementOptions: (taskId: string, q?: string) =>
    read<RequirementOptionsPayload>(
      `/api/tasks/${encodeURIComponent(taskId)}/requirement-options${queryString(q ? { q } : {})}`,
    ),
  /** 待确认按会议的审核卡（R07-9） */
  reviewCards: (filters: { project_id?: string; meeting_date_from?: string; meeting_date_to?: string } = {}) =>
    read<ReviewCardsPayload>(`/api/review-cards${queryString(filters)}`),
  /** 审核卡「全部确认」：每条按自己的默认推荐挂需求；撤销用 undoTaskReview */
  confirmAllInMeeting: (meetingId: string) =>
    write<ConfirmAllResult>(`/api/review-cards/${encodeURIComponent(meetingId)}/confirm-all`, "POST", {}),
  /** 10 分钟内撤销刚才的完成 */
  undoTaskComplete: (taskIds: string[]) =>
    write<{ reverted: string[]; failed: Array<{ task_id: string; error: string }> }>(
      "/api/tasks/undo-complete",
      "POST",
      { task_ids: taskIds },
    ),
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
  /** url 和 file_id 二选一；给 file_id 时 kind、url、title 由服务端填（3g） */
  addDeliverable: (
    taskId: string,
    data:
      | { kind: string; url: string; title?: string; note?: string; mark_done?: boolean }
      | { file_id: number; title?: string; note?: string },
  ) => write<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}/deliverables`, "POST", data),
  /** 删一个交付物（［撤销］用） */
  removeDeliverable: (taskId: string, deliverableId: number) =>
    write<TaskDetail>(
      `/api/tasks/${encodeURIComponent(taskId)}/deliverables/${deliverableId}`,
      "DELETE",
      {},
    ),
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
    write<MaterialRootRepoint>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}/replace`,
      "POST",
      { path },
    ),
  /** 根目录「找不到」时：可能是它改名后的样子；后台缓存没好时 state=checking */
  renameCandidates: (projectId: string, rootId: number) =>
    read<RenameCandidatesPayload>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}/rename-candidates`,
    ),
  /** ［是它］：根目录换到改名后的路径，嵌在里面的文件夹一起跟着改 */
  repointProjectMaterialRoot: (projectId: string, rootId: number, path: string) =>
    write<MaterialRootRepoint>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}/repoint`,
      "POST",
      { path },
    ),
  /** ［不是］：这个候选以后不再问 */
  declineRenameCandidate: (projectId: string, rootId: number, path: string) =>
    write<{ ok: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}/rename-decline`,
      "POST",
      { path },
    ),
  /** 等补建的文件夹换个位置；盘在线就当场建好，返回项目详情 */
  movePendingFolder: (projectId: string, parent: string) =>
    write<Project>(`/api/projects/${encodeURIComponent(projectId)}/pending-folder`, "PUT", { parent }),
  /** 「不建了，以后自己挂文件夹」 */
  dropPendingFolder: (projectId: string) =>
    write<{ ok: boolean }>(`/api/projects/${encodeURIComponent(projectId)}/pending-folder`, "DELETE", {}),
  // ---------------------------------------------------------------- 项目总文件夹（2a）
  projectParent: () => read<ProjectParentStatus>("/api/settings/project-parent"),
  /** path 为 null 清除；400 的 detail（「这个文件夹不存在」等）直接显示 */
  setProjectParent: (path: string | null) =>
    write<ProjectParentStatus>("/api/settings/project-parent", "PUT", { path }),
  /** 认领：一次发整批，逐项返回结果 */
  claimFolders: (items: ClaimItem[]) =>
    write<ClaimResult>("/api/settings/project-parent/claim", "POST", { items }),
  /** 「不是项目」 */
  declineFolder: (path: string) =>
    write<{ ok: boolean }>("/api/settings/project-parent/decline", "POST", { path }),
  undeclineFolder: (path: string) =>
    write<{ ok: boolean }>(`/api/settings/project-parent/decline${queryString({ path })}`, "DELETE", {}),
  removeProjectMaterialRoot: (projectId: string, rootId: number) =>
    write<{ ok: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}/material-roots/${rootId}`,
      "DELETE",
      {},
    ),
  projectMeetings: (projectId: string) =>
    read<ProjectMeetingRow[]>(`/api/projects/${encodeURIComponent(projectId)}/meetings`),
  /** 项目详情「需求与任务」（R06-5～13） */
  projectWork: (projectId: string) =>
    read<ProjectWorkPayload>(`/api/projects/${encodeURIComponent(projectId)}/work`),
  /** 项目详情「录音」：倒序，每场带关联需求和抽出的任务数（R06-10） */
  projectRecordings: (projectId: string) =>
    read<ProjectRecordingRow[]>(`/api/projects/${encodeURIComponent(projectId)}/recordings`),
  requirements: (filters: RequirementFilters = {}) =>
    read<RequirementsPayload>(`/api/requirements${queryString(filters)}`),
  requirement: (requirementId: string) =>
    read<RequirementDetail>(`/api/requirements/${encodeURIComponent(requirementId)}`),
  createRequirement: (data: {
    project_id: string;
    title: string;
    priority: RequirementPriority;
    folder_paths?: string[];
    /** v17：说明，最多 70 字，可空 */
    summary?: string;
    /** v17：来源（提出它的会议、原话、时间锚），选定的会议同时加进关联会议 */
    source?: RequirementSourceInput | null;
  }) => write<RequirementDetail>("/api/requirements", "POST", data),
  /** 新增、修改页边填边查重（R04-9）：同项目里重名的那条需求，没有是 null；修改时传 excludeId 不和自己比 */
  requirementTitleCheck: (projectId: string, title: string, excludeId?: string) =>
    read<{ existing: TitleConflict["existing"] }>(
      `/api/requirements/title-check${queryString({ project_id: projectId, title, exclude_id: excludeId })}`,
    ),
  /** v17 需求池海报墙：正式需求和待认领候选一起排 */
  requirementPool: (filters: PoolFilters) =>
    read<RequirementPoolPayload>(`/api/requirement-pool${queryString(filters)}`),
  /** 「我的方向」整排保存：排了座次的项目从第 1 位起的先后，不在里面的回到未排座次 */
  saveProjectSeats: (projectIds: string[]) =>
    write<{ seats: Array<{ project_id: string; seat: number }> }>("/api/project-seats", "PUT", {
      project_ids: projectIds,
    }),
  requirementCandidate: (candidateId: string) =>
    read<CandidateDetail>(`/api/requirement-candidates/${encodeURIComponent(candidateId)}`),
  /** projectId：认领页上改选的项目，不传按来源会议的归属 */
  candidateMergeTargets: (candidateId: string, projectId?: string | null) =>
    read<MergeTargets>(
      `/api/requirement-candidates/${encodeURIComponent(candidateId)}/merge-targets${queryString({
        project_id: projectId ?? undefined,
      })}`,
    ),
  /** 撞名时抛 ApiError(409)，data 是 TitleConflict（带同项目已有的那条需求） */
  claimCandidate: (
    candidateId: string,
    data: {
      title: string;
      summary?: string;
      project_id?: string;
      priority?: RequirementPriority;
      folder_paths?: string[];
    },
  ) =>
    write<RequirementDetail>(
      `/api/requirement-candidates/${encodeURIComponent(candidateId)}/claim`,
      "POST",
      data,
    ),
  mergeCandidate: (candidateId: string, requirementId: string, projectId?: string | null) =>
    write<RequirementDetail>(
      `/api/requirement-candidates/${encodeURIComponent(candidateId)}/merge`,
      "POST",
      projectId ? { requirement_id: requirementId, project_id: projectId } : { requirement_id: requirementId },
    ),
  /** 会议详情［抽需求候选］：历史会议手动补抽，只抽候选、不动任务（同步，等 AI 回完） */
  extractRequirementCandidates: (meetingId: string) =>
    write<CandidateExtraction>(
      `/api/meetings/${encodeURIComponent(meetingId)}/requirement-candidates/extract`,
      "POST",
      {},
    ),
  dropCandidate: (candidateId: string) =>
    write<CandidateDetail>(`/api/requirement-candidates/${encodeURIComponent(candidateId)}/drop`, "POST", {}),
  restoreCandidate: (candidateId: string) =>
    write<CandidateDetail>(
      `/api/requirement-candidates/${encodeURIComponent(candidateId)}/restore`,
      "POST",
      {},
    ),
  droppedCandidates: () => read<DroppedCandidates>("/api/requirement-candidates/dropped"),
  /** 合并后 10 分钟内撤销（R01-14）：候选回到待认领，合并带进去的原话、关联会议、任务都退回；过了时间 409 */
  undoCandidateMerge: (candidateId: string) =>
    write<CandidateDetail & { kept_task_count?: number }>(
      `/api/requirement-candidates/${encodeURIComponent(candidateId)}/unmerge`,
      "POST",
      {},
    ),
  updateRequirement: (
    requirementId: string,
    data: {
      title?: string;
      project_id?: string;
      priority?: RequirementPriority;
      status?: RequirementStatus;
      folder_paths?: string[];
      summary?: string;
      /** 传了才改来源：对象是换掉提出它的那句，null 是清空 */
      source?: RequirementSourceInput | null;
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
  /** 4h：需求页［复制给 Claude Code］的背景；和需求详情一起取 */
  requirementContext: (requirementId: string) =>
    read<RequirementContext>(`/api/requirements/${encodeURIComponent(requirementId)}/context`),
  // ---------------------------------------------------------------- 4h 从材料里找到的词
  /** 词典页收件箱：所有项目的待认词，按项目分组 */
  glossaryCandidatesInbox: () => read<GlossaryCandidatesInbox>("/api/glossary/candidates"),
  /** 词典页：这个项目全部待认的词（最多 30 项） */
  glossaryCandidates: (projectId: string) =>
    read<MaterialWordsList>(`/api/projects/${encodeURIComponent(projectId)}/glossary-candidates`),
  /** ［记入］：not_wrong 是去掉的听错写法；only_wrong 是会议页这一行的写法（只记它，别的写法不跟着记） */
  acceptGlossaryCandidate: (
    projectId: string,
    body: { key: string; not_wrong?: string[]; only_wrong?: string },
  ) =>
    write<MaterialWordAcceptResult>(
      `/api/projects/${encodeURIComponent(projectId)}/glossary-candidates/accept`,
      "POST",
      body,
    ),
  /** ［不是］：这个项目里不再提 */
  rejectGlossaryCandidate: (projectId: string, body: { key: string }) =>
    write<MaterialWordRejectResult>(
      `/api/projects/${encodeURIComponent(projectId)}/glossary-candidates/reject`,
      "POST",
      body,
    ),
  /** ［撤销］：600 秒内 */
  undoGlossaryCandidate: (projectId: string, body: { key: string }) =>
    write<MaterialWordUndoResult>(
      `/api/projects/${encodeURIComponent(projectId)}/glossary-candidates/undo`,
      "POST",
      body,
    ),
  // ---------------------------------------------------------------- 深度关联（4a）
  /** 回答一条关联；file_id 只在 pick 时给。409、422 的 detail 原样显示 */
  answerRelation: (relationId: number, body: { answer: RelationAnswer; file_id?: number }) =>
    write<RelationAnswerResult>(`/api/relations/${relationId}/answer`, "POST", body),
  /** 撤销上一次回答：600 秒内一次，第二次 409 */
  undoRelation: (relationId: number) =>
    write<RelationUndoResult>(`/api/relations/${relationId}/undo`, "POST", {}),
  /** ［现在重试］：失败的放回排队，清掉 AI 循环的暂停 */
  retryLinks: () => write<{ requeued: number }>("/api/links/retry", "POST", {}),
  /** 决议归需求；撤销时把返回的 undo 原样发回 */
  placeDecision: (
    decisionId: string,
    body: { placement: DecisionPlacement; requirement_id: string | null },
  ) =>
    write<DecisionPlacementResult>(
      `/api/decisions/${encodeURIComponent(decisionId)}/placement`,
      "POST",
      body,
    ),
  // ---------------------------------------------------------------- 决议日志和时间线（4c）
  /** 需求页「决议」卡；404「需求不存在」，旧后台是 FastAPI 的 404 Not Found */
  requirementDecisions: (requirementId: string) =>
    read<RequirementDecisionLog>(`/api/requirements/${encodeURIComponent(requirementId)}/decisions`),
  /** 项目时间线：按有动静的天翻页，before 是上一页给的 next_before */
  projectTimeline: (
    projectId: string,
    options: { before?: string | null; days?: number; kind?: TimelineKind } = {},
  ) =>
    read<ProjectTimelinePayload>(
      `/api/projects/${encodeURIComponent(projectId)}/timeline${queryString({
        before: options.before ?? undefined,
        days: options.days,
        kind: options.kind,
      })}`,
    ),
  // ---------------------------------------------------------------- 项目内问答（4g）
  /** 在本机找原文，从不调 AI；问题只在请求体里，从不进网址 */
  askPrepare: (projectId: string, question: string) =>
    write<AskPlan>(`/api/projects/${encodeURIComponent(projectId)}/ask/prepare`, "POST", { question }),
  /** 按计划号把这几段发出去；withMaterials 为假时不发任何材料原文。回 202 和任务号 */
  ask: (projectId: string, planId: string, withMaterials: boolean) =>
    write<{ job_id: string; state: "waiting"; text: string }>(
      `/api/projects/${encodeURIComponent(projectId)}/ask`,
      "POST",
      { plan_id: planId, with_materials: withMaterials },
    ),
  /** 轮询任务：网址里只有随机的任务号 */
  askJob: (jobId: string) => read<AskJob>(`/api/ask/${encodeURIComponent(jobId)}`),
};

export type ApiClient = typeof api;
