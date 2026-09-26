export type HealthLevel = "healthy" | "degraded" | "failed" | "unknown";

export interface BootstrapPayload {
  csrf_token: string;
  mobile_read_only: boolean;
  mobile_task_write: boolean;
  semantic_enabled: boolean;
  pending_confirm_count: number;
}

export interface HealthPayload {
  status: "ok" | "degraded";
  services: Record<string, string>;
  counts: {
    meetings: number;
    unreviewed: number;
    failed_jobs: number;
    queued_jobs?: number;
    scan_errors: number;
    /** 还没被确认归档的失败/归档未完成任务数；relay 清单暂不可用时为 null */
    attention_jobs?: number | null;
    acknowledged_jobs?: number;
  };
  details?: {
    attention?: { by_kind: Record<AttentionKind, number> | null; quarantined?: number };
    process?: { open_files: number | null; open_files_limit: number | null };
  };
  scanner?: Record<string, unknown>;
  semantic?: Record<string, unknown>;
  relay_worker?: Record<string, unknown>;
  backup?: Record<string, unknown>;
}

export type AttentionKind = "transcription" | "minutes" | "archive" | "other";

/** 资料库「需要处理」里的一条失败或归档未完成的转写任务 */
export interface AttentionJob {
  job_id: string;
  status?: string | null;
  stage?: string | null;
  kind: AttentionKind;
  summary: string;
  next_step: string;
  error?: string | null;
  meeting_id?: string | null;
  meeting_title?: string | null;
  created_at?: string | null;
  updated_at: string;
}

/** 被扫描器隔离、没能导入的会议文件夹 */
export interface AttentionQuarantine {
  directory: string;
  name: string;
  summary: string;
  reason: string;
  next_step: string;
}

export interface AttentionPayload {
  jobs: AttentionJob[];
  jobs_available: boolean;
  acknowledged_count: number;
  quarantined: AttentionQuarantine[];
}

export interface Project {
  id: string;
  name: string;
  color: string;
  origin?: "manual" | "ai";
  created_at?: string;
  meeting_count?: number;
  task_count?: number;
  pending_count?: number;
  in_progress_count?: number;
  done_count?: number;
  deliverable_count?: number;
  recent_at?: string | null;
  recent_body?: string | null;
  recent_kind?: string | null;
  /** 项目挂的外置盘材料根目录（人工挂，可多个） */
  material_roots?: MaterialRoot[];
  requirement_counts?: RequirementCounts;
  /** 未完成任务＝待确认＋已确认＋进行中 */
  open_task_count?: number;
}

export interface Tag {
  id: string;
  name: string;
  color: string;
  created_at?: string;
}

export interface Artifact {
  id: number;
  meeting_id: string;
  kind: string;
  role: string;
  source_root: string;
  path: string;
  sha256?: string | null;
  size_bytes?: number | null;
}

export interface Segment {
  id: string;
  ordinal: number;
  start_ms: number;
  end_ms: number;
  speaker_label?: string | null;
  speaker_name?: string | null;
  text: string;
  version_id?: string;
  meeting_id?: string;
}

export interface TranscriptVersion {
  id: string;
  meeting_id: string;
  version_no: number;
  kind: string;
  based_on_id?: string | null;
  published: number;
  created_at: string;
  segment_count?: number;
}

export interface MinutesVersion {
  id: string;
  meeting_id: string;
  version_no: number;
  markdown: string;
  html?: string | null;
  kind: string;
  based_on_id?: string | null;
  content_sha256?: string | null;
  published: number;
  created_at: string;
}

export type MeetingConflictKind =
  | "external_source_change"
  | "audio_integrity"
  | "publish_recovery"
  | "publish_post_commit"
  | "legacy_unknown";

export interface MeetingConflict {
  id: string;
  meeting_id: string;
  kind: MeetingConflictKind;
  status: "open" | "resolved";
  source_signature?: string | null;
  payload?: Record<string, unknown>;
  resolution?: string | null;
  created_at: string;
  updated_at: string;
  resolved_at?: string | null;
}

export interface Speaker {
  id: string;
  meeting_id: string;
  label: string;
  display_name?: string | null;
}

export interface MeetingSummary {
  id: string;
  title: string;
  canonical_dir?: string | null;
  recording_date?: string | null;
  duration_ms?: number | null;
  status: string;
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  project_origin?: "manual" | "ai" | null;
  audio_artifact_id?: number | null;
  segment_count?: number;
  conflict?: number;
  tags: Tag[];
  created_at?: string;
  updated_at?: string;
}

/** PATCH 会议改了项目时返回：哪些任务跟着一起移了、哪些留在旧项目的需求上。 */
export interface MeetingProjectEffects {
  tasks_moved: number;
  tasks_left: { id: string; title: string; requirement_id: string; requirement_title: string }[];
  undo_until: string;
}

export interface MeetingDetail extends MeetingSummary {
  /** 只在 PATCH 改了项目的响应里出现 */
  effects?: MeetingProjectEffects;
  canonical_dir?: string | null;
  current_transcript_version_id?: string | null;
  current_minutes_version_id?: string | null;
  artifacts: Artifact[];
  segments: Segment[];
  transcript_versions: TranscriptVersion[];
  minutes_versions: MinutesVersion[];
  speakers: Speaker[];
  events: Array<Record<string, unknown>>;
  conflicts?: MeetingConflict[];
  asr_shadow_runs?: AsrShadowRun[];
  /** 这场会手动关联的需求（会议与需求多对多） */
  requirements?: RequirementRef[];
}

export type AsrShadowState = "queued" | "running" | "ready" | "failed" | "unavailable";

export interface AsrShadowRun {
  id: string;
  meeting_id: string;
  engine: string;
  model: string;
  state: AsrShadowState;
  transcript_version_id?: string | null;
  audio_sha256?: string | null;
  metrics?: Record<string, number> | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

export type TranscriptRiskKind = "missing_candidate" | "latin_term" | "number" | "text";

export interface TranscriptComparisonItem {
  primary_segment_id: string;
  candidate_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  primary_text: string;
  candidate_text: string;
  risk_kinds: TranscriptRiskKind[];
  similarity: number;
}

export interface TranscriptComparisonPayload {
  primary_version_id: string;
  candidate_version_id: string;
  candidate_kind: string;
  items: TranscriptComparisonItem[];
}

export interface AsrGoldSample {
  id: string;
  meeting_id: string;
  segment_id?: string | null;
  start_ms: number;
  end_ms: number;
  reference: string;
  entities: string[];
  numbers: string[];
  tags: string[];
  source_audio_sha256?: string | null;
  created_at: string;
  updated_at: string;
}

export interface MinutesEvidenceItem {
  item_id: string;
  kind: "fact" | "decision" | "action" | "risk" | "open_question" | "number";
  text: string;
  status: "included" | "omitted";
  source_start_sec: number;
  source_end_sec: number;
  minutes_anchor?: string;
  omitted_reason?: string;
  owner?: string;
  deadline?: string;
}

export interface MinutesEvidence {
  coverage: { total_items: number; included_items: number; omitted_items: number };
  topics: Array<{
    topic_id: string;
    title: string;
    start_sec: number;
    end_sec: number;
    items: MinutesEvidenceItem[];
  }>;
  anchors: Array<{
    item_id: string;
    minutes_anchor: string;
    source_start_sec: number;
    source_end_sec: number;
  }>;
}

export interface SearchItem {
  segment_id: string;
  meeting_id: string;
  title: string;
  canonical_dir?: string | null;
  recording_date?: string | null;
  start_ms: number;
  end_ms: number;
  speaker_name?: string | null;
  speaker_label?: string | null;
  text: string;
  score?: number;
  match_kind?: "title" | "segment";
}

export interface MeetingFilters {
  q?: string;
  project_id?: string;
  tag_id?: string;
  status?: string;
  date_from?: string;
  date_to?: string;
  min_duration_ms?: number;
  max_duration_ms?: number;
  limit?: number;
  offset?: number;
}

export interface MeetingsPayload {
  items: MeetingSummary[];
  limit: number;
  offset: number;
  total: number;
}

export type JobState =
  | "discovered"
  | "stabilizing"
  | "queued"
  | "transcribing"
  | "transcript_ready"
  | "minutes_generating"
  | "completed_unreviewed"
  | "draft_modified"
  | "published"
  | "failed"
  | "cancelled"
  | "interrupted";

export type JobSubstateName = "whisper" | "index" | "qwen";
export type JobSubstateStatus = "pending" | "queued" | "running" | "ready" | "failed" | "unavailable" | "absent";

export interface JobSubstate {
  status: JobSubstateStatus;
  error?: string | null;
  updated_at?: string | null;
  attempt?: number;
}

export interface Job {
  id: string;
  meeting_id?: string | null;
  meeting_title?: string | null;
  state: JobState;
  stop_after_stage?: number;
  failure_stage?: string | null;
  failure_reason?: string | null;
  created_at: string;
  updated_at: string;
  active_attempt_id?: string | null;
  whisper_status?: JobSubstateStatus;
  index_status?: JobSubstateStatus;
  substates?: Partial<Record<JobSubstateName, JobSubstate>>;
}

export interface JobsPayload {
  items: Job[];
  counts?: Record<string, number>;
}

export type LoadState = "idle" | "loading" | "ready" | "empty" | "error";

/** 纪要生成跑在哪个模型后端；不传则跟 relay 的全局默认（当前 DeepSeek）。 */
export type MinutesBackend = "claude" | "deepseek";

// ---------------------------------------------------------------------------
// 任务代办与项目看板（260804 新增）
// ---------------------------------------------------------------------------

export type TaskStatus =
  | "pending_confirm"
  | "confirmed"
  | "in_progress"
  | "done"
  | "cancelled"
  | "expired";
export type TaskAssignee = "ai" | "me";
export type TaskOrigin = "ai" | "manual";
export type DeliverableKind = "figma" | "lark" | "file" | "link";

export interface TaskEvent {
  id: number;
  task_id: string;
  kind: string;
  body: string;
  created_at: string;
}

export interface Deliverable {
  id: number;
  task_id: string;
  kind: DeliverableKind;
  url: string;
  title: string;
  note: string;
  created_at: string;
}

export interface Task {
  id: string;
  title: string;
  detail: string;
  status: TaskStatus;
  origin: TaskOrigin;
  assignee: TaskAssignee;
  meeting_id?: string | null;
  project_id?: string | null;
  extraction_id?: number | null;
  anchor_ms?: number | null;
  anchor_quote?: string | null;
  suggested_project_name?: string | null;
  status_changed_at: string;
  created_at: string;
  updated_at: string;
  meeting_title?: string | null;
  project_name?: string | null;
  project_color?: string | null;
  stall_days: number;
  stall_since?: string | null;
  stalled: boolean;
  /** 所属需求；任务的优先级从需求派生，任务本身不设优先级 */
  requirement_id?: string | null;
  requirement_title?: string | null;
  requirement_priority?: RequirementPriority | null;
  requirement_status?: RequirementStatus | null;
  meeting_recording_date?: string | null;
}

export interface TaskDetail extends Task {
  events: TaskEvent[];
  deliverables: Deliverable[];
}

export interface TaskFilters {
  /** 单个状态，或逗号分隔的多个状态（如 "confirmed,in_progress"） */
  status?: string;
  /** 项目 id；传 "none" 只看没挂项目的任务 */
  project_id?: string;
  meeting_id?: string;
  /** 需求 id；传 "none" 只看没挂需求的任务 */
  requirement_id?: string;
  assignee?: TaskAssignee;
  /** 来源会议日期，与 /api/meetings 的 date_from/date_to 同一口径 */
  meeting_date_from?: string;
  meeting_date_to?: string;
  /** 任务标题包含 */
  q?: string;
  limit?: number;
  offset?: number;
}

export interface TasksPayload {
  items: Task[];
  total: number;
  limit: number;
  offset: number;
  /** 各状态总数：不受状态筛选影响，受其余全部筛选影响 */
  counts?: Partial<Record<TaskStatus, number>>;
  /** 按项目分的各状态总数，没挂项目的记在 "none" 下：不受项目/状态筛选影响，受其余全部筛选影响 */
  project_counts?: Record<string, Partial<Record<TaskStatus, number>>>;
}

export interface TaskConfirmResult {
  confirmed: string[];
  failed: Array<{ task_id: string; error: string }>;
}

export interface BoardMeeting {
  id?: string | null;
  title: string;
  recording_date?: string | null;
  duration_ms?: number | null;
  tasks: Task[];
}

/** 项目看板词典区的精简术语行；完整字段在词典页自己拉取。 */
export interface BoardGlossaryTerm {
  id: string;
  term: string;
  aliases: string[];
  category: string;
}

export interface ProjectBoard extends Project {
  meetings: BoardMeeting[];
  glossary_count?: number;
  glossary_terms?: BoardGlossaryTerm[];
}

// ---------------------------------------------------------------------------
// 术语词典（260820 新增）
// ---------------------------------------------------------------------------

/** 分类字典与后端 CATEGORIES 契约一致，别单独改。 */
export type GlossaryCategory = "人名" | "机构" | "术语" | "药品" | "地名" | "其他";

export interface GlossaryTerm {
  id: string;
  term: string;
  aliases: string[];
  scope: string;
  category: string;
  /** SQLite 里存 0/1，接口原样返回，判断时按真值用。 */
  confirmed: boolean;
  source: string;
  hit_count: number;
  created_at: string;
  updated_at: string;
  /** 260905 新增：术语挂到某个项目下时非空；scope 此时等于项目名，仅供展示兼容。 */
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
}

/** 词典筛选 chip：通用 → 项目 → 其他桶，只含有术语的分组，由后端定序。 */
export interface GlossaryScope {
  kind: "general" | "project" | "bucket";
  key: string;
  label: string;
  color: string | null;
  count: number;
}

export interface GlossarySuggestion {
  id: string;
  wrong: string;
  correct: string;
  scope: string;
  meeting_id: string | null;
  context: string | null;
  status: "pending" | "confirmed" | "rejected";
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// 项目 → 需求 → 任务三层（260915 新增）
// ---------------------------------------------------------------------------

export type RequirementPriority = "P0" | "P1" | "P2" | "P3";
export type RequirementStatus = "active" | "done" | "shelved";

/** online：文件夹在；missing：盘在但文件夹没了；volume_offline：资料盘没插 */
export type MaterialRootState = "online" | "missing" | "volume_offline";

export interface MaterialRoot {
  id: number;
  project_id: string;
  path: string;
  /** 等于 state === "online"；盘没插、目录被移走时为 false，记录保留 */
  exists: boolean;
  /** 旧后端没有这个字段，按 exists 推断 */
  state?: MaterialRootState;
  created_at: string;
  /** 挂载或替换时返回：和别的项目根目录互相嵌套的情况 */
  nested?: { path: string; project_id: string; project_name: string }[];
}

export interface MaterialDirEntry {
  name: string;
  path: string;
}

export interface MaterialBrowsePayload {
  /** 允许浏览的最上层（MEETING_WORKBENCH_MATERIAL_BROWSE_ROOT） */
  base: string;
  path: string;
  /** 已到浏览根时为 null */
  parent: string | null;
  /** 从浏览根到当前目录，含两端 */
  breadcrumbs: MaterialDirEntry[];
  /** 当前目录下的子文件夹，不含隐藏项 */
  dirs: MaterialDirEntry[];
}

export interface MaterialFolderStat {
  name: string;
  path: string;
  exists: boolean;
  /** 递归文件数，跳过隐藏项；超过上限时只数到上限并置 file_count_capped */
  file_count: number;
  file_count_capped: boolean;
  /** 文件夹内最新文件的修改时间 */
  modified_at: string | null;
}

export interface ProjectSubfoldersRoot {
  root_id: number;
  root_path: string;
  exists: boolean;
  /** 按修改时间倒序 */
  folders: MaterialFolderStat[];
}

export interface ProjectSubfoldersPayload {
  roots: ProjectSubfoldersRoot[];
}

export interface RequirementCounts {
  active: number;
  done: number;
  shelved: number;
  all: number;
}

export interface RequirementRef {
  id: string;
  title: string;
  priority: RequirementPriority;
  status: RequirementStatus;
  project_id: string;
}

export interface RequirementSummary {
  id: string;
  project_id: string;
  project_name: string;
  project_color: string;
  title: string;
  priority: RequirementPriority;
  status: RequirementStatus;
  created_at: string;
  updated_at: string;
  /** 未完成任务＝待确认＋已确认＋进行中 */
  open_task_count: number;
  meeting_count: number;
  /** 关联会议里最新的 recording_date 原值 */
  latest_meeting_date: string | null;
  folder_count: number;
}

export interface RequirementFile {
  /** 相对所在材料文件夹的路径，如「考据/01-接口字段事实.md」 */
  relative_path: string;
  size_bytes: number;
  modified_at: string;
}

export interface RequirementFolder extends MaterialFolderStat {
  id: number;
  /** 前 6 个文件，按相对路径排序 */
  preview_files: RequirementFile[];
}

export interface RequirementMeeting {
  id: string;
  title: string;
  recording_date: string | null;
  duration_ms: number | null;
  canonical_dir: string | null;
}

export interface RequirementDetail extends RequirementSummary {
  folders: RequirementFolder[];
  /** 按 recording_date 倒序 */
  meetings: RequirementMeeting[];
  /** 未完成的在前，已完成、已过期、已取消在后 */
  tasks: Task[];
}

export interface RequirementFilters {
  project_id?: string;
  /** 单个或逗号分隔的多个状态 */
  status?: string;
  /** 单个或逗号分隔的多个优先级 */
  priority?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface RequirementsPayload {
  items: RequirementSummary[];
  total: number;
  limit: number;
  offset: number;
  /** 不受状态筛选影响，受其余筛选影响 */
  counts: RequirementCounts;
}

export interface RequirementFilesPayload {
  folder_id: number;
  path: string;
  exists: boolean;
  total: number;
  capped: boolean;
  items: RequirementFile[];
}

export interface ProjectMeetingRow {
  id: string;
  title: string;
  recording_date: string | null;
  duration_ms: number | null;
  canonical_dir: string | null;
  requirements: RequirementRef[];
}
