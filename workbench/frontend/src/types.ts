import type { RelationQuestion } from "./api";

export type HealthLevel = "healthy" | "degraded" | "failed" | "unknown";

export interface BootstrapPayload {
  csrf_token: string;
  mobile_read_only: boolean;
  mobile_task_write: boolean;
  semantic_enabled: boolean;
  pending_confirm_count: number;
  /** 本机打开声档时才有［在访达中显示］［打开文件夹］（3e） */
  can_reveal?: boolean;
  /** 第四期（4a）：不是布尔值就是旧后台，第四期的控件一律不画 */
  links_enabled?: boolean;
  llm_configured?: boolean;
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
    /** 材料还没读的活文件（3e）；旧后端没有 */
    material_pending?: number;
  };
  details?: {
    attention?: { by_kind: Record<AttentionKind, number> | null; quarantined?: number };
    process?: { open_files: number | null; open_files_limit: number | null };
    materials?: MaterialsProgress;
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
  /** 项目的其他叫法；former 是改名前的名字，merged 是合并进来的项目名 */
  also_names?: ProjectAlsoName[];
  /** 只在 POST /api/projects 的响应里出现 */
  meetings_assigned?: number;
  needs_review_meeting_ids?: string[];
  folder_pending?: { path: string; reason: string };
  /** 从「像是新项目」建（带 source_name）时：会上说得最多的叫法记进了也叫，可以撤销 */
  spoken_added?: SpokenAlsoAdded | null;
  /** 从提示建时，归进来的会当场补写了几张卡片 */
  cards_written?: number;
  /** 盘不在时先建了项目、插上后再补建的文件夹（旧后端没有） */
  pending_folder?: PendingProjectFolder | null;
  /** v17：需求池「我的方向」里的座次名次，从 1 开始连续；未排座次为 null（旧后端没有） */
  seat?: number | null;
  /** v17：归属这个项目的会议里录音时间最晚的一场 */
  latest_meeting_date?: string | null;
}

/** waiting：等资料盘插上；stopped：建不了（位置不存在、没权限……），reason 说为什么，不再每轮重试 */
export interface PendingProjectFolder {
  path: string;
  parent: string;
  state: "waiting" | "stopped";
  reason: string | null;
}

export interface ProjectAlsoName {
  name: string;
  /** spoken：从「像是新项目」建成时，会上说得最多的叫法自动记进来的 */
  source: "manual" | "former" | "merged" | "spoken";
}

/** 建成项目时记进也叫的会上叫法；event_id 用来撤销 */
export interface SpokenAlsoAdded {
  name: string;
  count: number;
  event_id: number;
  undo_until: string;
}

/** 新建项目撞上近似重名时 409 带回来的已有项目 */
export interface SimilarProjectSuggestion {
  project_id: string;
  /** 和 project_id 同值（新后端才有） */
  id?: string;
  name: string;
  also_names: string[];
  matched: string;
  match: "same" | "similar";
  /** 正式名完全相同：只能用它，不能仍然新建 */
  exact?: boolean;
}

export interface FolderMatch {
  path: string;
  name: string;
  match: "exact" | "similar" | null;
  modified_at: string;
}

export interface FolderMatchesPayload {
  /** checking：后台还在看磁盘，matches / recent 先是空的，2 秒后再查（旧后端没有，当作 ready） */
  state?: "ready" | "checking";
  matches: FolderMatch[];
  recent: FolderMatch[];
  /** 新文件夹放哪；null：还没有可参照的项目文件夹，这次先不建文件夹 */
  create_parent: string | null;
  /** setting：项目总文件夹；suggested：多数项目文件夹所在的位置（推荐，没写进设置） */
  create_parent_source?: "setting" | "suggested" | null;
  create_parent_state: FolderListingState | null;
  create_name: string;
  create_replaced: string[];
}

/** 后台缓存里一个目录的状态：比根目录多「读不了」和「还在看」 */
export type FolderListingState = MaterialRootState | "unreadable" | "checking";

// ---------------------------------------------------------------------------
// 项目总文件夹（2a）
// ---------------------------------------------------------------------------

/** 项目总文件夹下还没挂到项目的一级文件夹，带默认动作 */
export interface UnclaimedFolder {
  path: string;
  name: string;
  modified_at: string;
  /**
   * exact：和一个还没挂文件夹的项目同名；exact_mounted：和一个已经挂了文件夹的项目同名；
   * similar：名字相近；generic：通用名，看起来不是项目；new：和谁都不像，建成项目
   */
  kind: "exact" | "exact_mounted" | "similar" | "new" | "generic";
  /** generic 时为 null */
  action: "mount" | "create" | null;
  project_id: string | null;
  project_name: string | null;
  /** exact_mounted 时是这个项目已挂的文件夹 */
  project_roots: string[];
  /** 默认勾选 */
  checked: boolean;
}

export interface ProjectParentStatus {
  path: string | null;
  /** invalid / conflict 时 reason 是原因文案，可直接显示 */
  state: FolderListingState | "invalid" | "conflict" | null;
  reason: string | null;
  /** 只在 path 为 null 时给：已挂根目录里最常见的父目录 */
  suggested: { path: string; count: number; total: number } | null;
  unclaimed: {
    state: "unset" | "ready" | FolderListingState | "invalid" | "conflict";
    folders: UnclaimedFolder[];
    total: number;
  };
}

export interface ClaimItem {
  path: string;
  action: "mount" | "create";
  project_id?: string;
  /** 近似重名时你选了「仍然新建」 */
  force?: boolean;
}

/** 认领撞上已有项目时带回来的那个项目（id 和 project_id 同值） */
export interface ClaimSuggestion {
  id: string;
  project_id?: string;
  name: string;
  also_names?: string[];
  /** 正式名完全相同：只能挂到它，不能仍然新建 */
  exact?: boolean;
}

export interface ClaimItemResult {
  path: string;
  action: "mount" | "create";
  ok: boolean;
  project_id?: string;
  project_name?: string | null;
  /** 挂上的文件夹里还嵌着别的项目的文件夹 */
  nested?: { path: string; project_id: string; project_name: string }[];
  cards_written?: number;
  error?: string;
  suggestion?: ClaimSuggestion | null;
}

export interface ClaimResult {
  items: ClaimItemResult[];
  created: number;
  mounted: number;
  cards_written: number;
  /** 新项目建好后，没认出的会里提到它、改成待你选的场数 */
  needs_review: number;
}

/** 根目录找不到了（盘在、文件夹没了）时，可能是它改名后的样子 */
export interface RenameCandidate {
  path: string;
  name: string;
  modified_at: string;
  /** 3：里面有这个项目的会议卡片；2：子文件夹对得上；1：名字相近 */
  strength: 1 | 2 | 3;
  evidence: {
    kind: "cards" | "children" | "name";
    /** 可直接显示的一句话，如「里面有这个项目的会议卡片」 */
    text: string;
    count?: number;
    matched?: number;
    total?: number;
  };
}

export interface RenameCandidatesPayload {
  /** online / volume_offline：文件夹其实不算找不到了，没有候选 */
  state: "checking" | "ready" | "online" | "volume_offline";
  candidates: RenameCandidate[];
  /** 前两名一样强时为 null，不默认选 */
  default_path: string | null;
}

/** 根目录换到新路径（［是它］、重新选…）后：嵌在里面一起跟着改的 */
export interface MaterialRootRepoint extends MaterialRoot {
  moved_roots?: { id: number; project_id: string; project_name: string; old: string; new: string }[];
  /** 一起改了的需求文件夹个数 */
  moved_folders?: number;
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
  /** 列表接口带：归属状态、待你选的候选（≤2）、像新项目时的名字 */
  attribution_state?: AttributionState;
  candidates?: AttributionCandidate[];
  new_project_name?: string | null;
  /** 「像是新项目 / 新需求」（2b，旧后端没有） */
  name_hint?: NameHint | null;
  audio_artifact_id?: number | null;
  segment_count?: number;
  conflict?: number;
  tags: Tag[];
  created_at?: string;
  updated_at?: string;
}

export interface LeftTask {
  id: string;
  title: string;
  requirement_id: string;
  requirement_title: string;
}

/** PATCH 会议改了项目时返回：哪些任务跟着一起移了、哪些留在旧项目的需求上。 */
export interface MeetingProjectEffects {
  tasks_moved: number;
  tasks_left: LeftTask[];
  undo_until: string;
  /** 从 AI 归属改走、证据里有项目词时：可以问「以后不再用这个词判断项目」 */
  cue_hint?: AttributionCueHint;
  /** 会议卡片跟着去了哪（改归属、撤销时） */
  card?: MeetingCardEffect;
}

/** 卡片写不了、还没写的原因 */
export type MeetingCardReason =
  | "queued"
  | "waiting_minutes"
  | "waiting_project"
  | "needs_review"
  | "not_backfilled"
  | "no_root"
  | "root_offline"
  | "root_missing"
  | "root_in_archive"
  | "root_shared"
  | "paused"
  | "disabled";

/**
 * 会议详情里的项目卡片状态。category 是界面的三类：ok 已写入；waiting 在等什么；
 * stopped 为什么停了（你改过、被删了、暂停了……）。
 */
export interface MeetingCard {
  state: "pending" | "synced" | "user_edited" | "missing" | "retired" | "blocked";
  reason: MeetingCardReason | null;
  category: "ok" | "waiting" | "stopped";
  path: string | null;
  synced_at: string | null;
  error: string | null;
}

/** 改归属后卡片的去向；from / to 是「项目文件夹名/声档会议记录/文件名」 */
export interface MeetingCardEffect {
  action: "moved" | "written" | "retired" | "updated" | "waiting" | "none";
  from: string | null;
  to: string | null;
  reason: MeetingCardReason | null;
}

/** 项目看板的卡片汇总 */
export interface ProjectCardsSummary {
  /** 卡片文件夹（项目最早挂上的根目录下的「声档会议记录」），没挂文件夹时为 null */
  root: string | null;
  index_path: string | null;
  written: number;
  edited: number;
  missing: number;
  waiting: number;
  waiting_reason: MeetingCardReason | null;
  paused: boolean;
  /** 上线前、还没补写卡片的会（补写后为 0；旧后端没有） */
  history?: number;
  /** 已写好、没改过的补写卡片（上线前的会）；旧后端没有 */
  backfilled?: number;
}

/** 上线前的历史会议能补写多少张卡片 */
export interface CardsBackfillPreview {
  meetings: number;
  projects: number;
  ai_attributed: number;
  no_folder_projects: number;
  no_folder_meetings: number;
  top: { project_id: string; project_name: string; count: number; path: string | null; paused: boolean }[];
}

/**
 * 卡片提示。没有 kind：某个项目第一次建出「声档会议记录/」；
 * folder_created：盘不在时建的项目，插上资料盘后补建好了文件夹（path 是项目文件夹）。
 */
export interface CardsNotice {
  kind?: "folder_created";
  project_id: string;
  project_name: string;
  path: string;
  at: string;
  /** folder_created：挂上后补写了几张会议卡片 */
  cards_written?: number;
}

export interface CardsBanner {
  backfill: CardsBackfillPreview | null;
  notices: CardsNotice[];
}

/**
 * 归属状态（前后端同一套规则）：manual 你选的；manual_none 你标了不归项目；
 * needs_review 待你选；auto 自动归属；ai_pending 等 AI 判断；none 没认出；new_project 像新项目。
 */
export type AttributionState =
  | "manual"
  | "manual_none"
  | "needs_review"
  | "auto"
  | "ai_pending"
  | "none"
  | "new_project";

export interface AttributionCandidate {
  project_id: string;
  project_name: string;
  project_color?: string;
  count: number;
  llm: boolean;
  /** 会议现在就在这个项目里（复评时「原来的」那个） */
  current: boolean;
}

export interface AttributionEvidence {
  kind: "literal" | "llm";
  project_id?: string | null;
  project_name?: string | null;
  cue?: string;
  /** injection：relay 出纪要前按逐字稿认出这个项目、用它的词典纠的错 */
  source?: "name" | "also" | "folder" | "term" | "injection";
  count?: number;
  anchors_ms?: number[];
  where?: { title: number; transcript: number; minutes: number };
  term_id?: string;
  confidence?: "high" | "low";
  reason?: string;
}

export interface AttributionCueHint {
  term_id: string;
  term: string;
  cue?: string;
}

export interface AttributionReassignedFrom {
  project_id: string | null;
  project_name: string | null;
  origin_before: "manual" | "ai" | null;
  at: string;
  undo_until: string;
  can_undo: boolean;
  tasks_left: LeftTask[];
  cue_hint: AttributionCueHint | null;
}

export interface MeetingAttribution {
  state: AttributionState;
  project_id: string | null;
  origin: "manual" | "ai" | null;
  method: string | null;
  evidence: AttributionEvidence[];
  candidates: AttributionCandidate[];
  reason: string;
  new_project_name: string | null;
  /** 「像是新项目 / 新需求」提示；会议页、简报、资料库各处同一套判断（2b，旧后端没有） */
  name_hint?: NameHint | null;
  reassigned_from: AttributionReassignedFrom | null;
  ai_configured: boolean;
}

// ---------------------------------------------------------------------------
// 「像是新项目 / 新需求」提示（2b）
// ---------------------------------------------------------------------------

/**
 * project：会还没归项目（像新项目，或待你选但 AI 没选项目、提了新项目名）；
 * requirement：会已归项目（自动或你归的），AI 觉得主要在谈这个项目里一件还没有的事。
 * spoken 是这个名字在纪要里的原样写法。
 */
export type NameHint =
  | { kind: "project"; name: string; spoken: string[] }
  | { kind: "requirement"; name: string; spoken: string[]; project_id: string; project_name: string };

export interface NameCandidate {
  name: string;
  /** 磁盘上有这个文件夹：同名、还没挂的（需求提示时是项目根目录下同名的一级子文件夹） */
  folder_path: string | null;
  /** 会上说过几次；first_ms 和最多 5 个锚点可以播放 */
  spoken: { count: number; first_ms: number; anchors_ms: number[] } | null;
  /** AI 起的名字 */
  ai: boolean;
  /** 名字相近的文件夹：点它只填名字，要挂它得点「改成挂上这个文件夹」 */
  similar_folder_path: string | null;
}

/** 默认那个名字的文件夹动作（名字改了以后前端自己算） */
export type NameFolderAction =
  | { mode: "mount"; path: string }
  | { mode: "create"; parent: string; name: string; parent_state: FolderListingState | null; inferred: boolean }
  | { mode: "none"; reason: string | null };

/** GET /api/meetings/{id}/name-candidates：点开提示时取，只查库和文件夹缓存 */
export interface NameCandidatesPayload {
  /** null：什么都不显示 */
  hint: NameHint | null;
  /** 已按优先顺序排好，输入框预填第一个 */
  candidates: NameCandidate[];
  /** 同名的会，第一场就是这场；said_ms 是第一次说到这个名字的时间 */
  meetings: { id: string; title: string; date: string; said_ms: number | null }[];
  /** 实心的那个主按钮 */
  default_action: "create_project" | "create_requirement" | null;
  /** 需求提示：建在这个项目 */
  project: { id: string; name: string } | null;
  /** 项目提示时［建成需求］的项目下拉，已排好（AI 选的、线索命中的在前，suggested=true） */
  requirement_projects: { id: string; name: string; color: string; suggested: boolean }[];
  folder: NameFolderAction;
  /** checking：缓存还没好，2 秒后再取，最多等 15 秒 */
  folders_state: "ready" | "checking";
  /** 新文件夹放哪；null：还没有可参照的项目文件夹，这次先不建 */
  create_parent: string | null;
  create_parent_source: "setting" | "suggested" | null;
  create_parent_state: FolderListingState | null;
}

/** POST /api/meetings/{id}/name-as-requirement */
export interface NameAsRequirementResult {
  requirement_id: string;
  requirement_title: string;
  project_id: string;
  project_name: string;
  /** 这个项目里已有同名需求：没新建，只把会关联过去 */
  existing: boolean;
  priority: RequirementPriority;
  meetings_linked: number;
  meetings_assigned: number;
  meeting_ids: string[];
  folder_attached: string | null;
  /** 需求文件夹没挂上的原因；需求照常建好 */
  folder_error: string | null;
  event_id: number;
  undo_until: string;
}

/** 「不是新项目」「不算新需求」 */
export interface NameDecisionResult {
  name: string;
  norm_key: string;
  kind: "project" | "requirement";
  meetings_updated: number;
  event_id: number;
  undo_until: string;
}

export interface AttributionSummary {
  /** 最近 14 天等你选项目的会 */
  needs_review_recent: number;
  needs_review_total: number;
  new_project_names: {
    name: string;
    norm_key: string;
    meeting_count: number;
    meeting_ids: string[];
    last_at: string;
  }[];
  auto_30d: number;
  corrected_30d: number;
}

/** relay 出纪要时这场用了哪些词（glossary-injection.json 回执的摘要） */
export interface MeetingGlossaryReceipt {
  id: number;
  job_id: string | null;
  attempt: number | null;
  project_id: string | null;
  project_name: string | null;
  /** hint：工作台告诉 relay 的；transcript：relay 按逐字稿认出的 */
  project_source: "hint" | "transcript" | null;
  term_count: number;
  project_terms: number;
  public_terms: number;
  snapshot_missing: boolean;
  generated_at: string | null;
}

export interface MeetingGlossaryHit {
  kind: "corrected" | "missed";
  term: string;
  wrong: string;
  term_project_id: string | null;
  transcript_count: number;
  minutes_count: number;
}

/** 会议页「词典」小节 */
export interface MeetingGlossary {
  /** receipt 按回执的项目 / meeting 按会议当前项目 / chosen 你指定的 / public 只有公共词 */
  basis: "receipt" | "meeting" | "chosen" | "public";
  project: { id: string; name: string | null; color: string | null } | null;
  meeting_project: { id: string; name: string | null; color: string | null } | null;
  /** 会议归属的项目和查纪要用的项目不一样 */
  mismatch: boolean;
  receipt: MeetingGlossaryReceipt | null;
  minutes_version_id: string | null;
  /** 纪要在上次体检之后又改过 */
  stale: boolean;
  checked_at: string;
  corrected: MeetingGlossaryHit[];
  missed: MeetingGlossaryHit[];
  applied: { by: "auto" | "user"; count: number; at: string; can_undo: boolean } | null;
  /** 4h：这场会听错的、材料里有正确写法的词，最多 2 个（旧后台没有这个字段，不出那两行） */
  material_pairs?: MaterialPair[];
}

export interface MeetingDetail extends MeetingSummary {
  /** 只在 PATCH 改了项目的响应里出现 */
  effects?: MeetingProjectEffects;
  attribution?: MeetingAttribution;
  /** 项目文件夹里的会议卡片状态（旧后端没有） */
  card?: MeetingCard;
  /** 按词典查纪要的结果（还没查过时是 null，旧后端没有） */
  glossary?: MeetingGlossary | null;
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
  /** 纪要命中没有段落 id */
  segment_id: string | null;
  meeting_id: string;
  title: string;
  canonical_dir?: string | null;
  recording_date?: string | null;
  /** 纪要命中所在行没有时间点时为 null，点开直接看纪要 */
  start_ms: number | null;
  end_ms: number | null;
  speaker_name?: string | null;
  speaker_label?: string | null;
  text: string;
  score?: number;
  match_kind?: "title" | "segment" | "minutes";
  /** 命中的是哪个写法（原词或词典展开出来的错写），用来高亮 */
  matched?: string;
  project_id?: string | null;
  project_name?: string | null;
  project_color?: string | null;
}

export interface SearchPayload {
  mode: "hybrid" | "exact" | "semantic";
  /** 包含原词（或词典里记的其他写法）的命中 */
  items: SearchItem[];
  /** 意思相近的段落，已去掉 items 里列过的 */
  similar?: SearchItem[];
  /** 自动一起搜了的其他写法 */
  expanded?: string[];
  /** 两个字的其他写法，只作为可点的提示 */
  expand_hints?: string[];
  /** 在项目里搜时，没归项目的会里还有几条命中 */
  unattributed_hits?: number;
  /** 意思相近的这次没搜成的原因（正在转写、模型不可用） */
  semantic_unavailable?: string;
  /** 3f：材料里包含这个词的，一份内容一行，最多 20 份，按修改时间从新到旧 */
  materials?: MaterialSearchItem[];
  /** 3f：意思相近的材料，已去掉 materials 里列过的 */
  material_similar?: MaterialSearchItem[];
  material_state?: MaterialSearchState;
}

export type MaterialHitKind = "text" | "pdf" | "image" | "media";

export interface MaterialHit {
  kind: MaterialHitKind;
  /** 「第 3 页」「表『预算』」这类位置 */
  loc: string | null;
  /** 录音文字的时间点 */
  start_ms: number | null;
  text: string;
  matched: string;
  /** 4d：段号，［预览］定位到那一段；旧后台没有 */
  ordinal?: number | null;
}

export interface MaterialSearchItem {
  file_id: number;
  content_key: string | null;
  name: string;
  ext: string;
  path: string;
  rel_path: string;
  folder_path: string;
  root_id: number;
  project_id: string;
  project_name: string;
  project_color: string | null;
  modified_at: string | null;
  root_online: boolean;
  playable: boolean;
  /** 同一份内容还放在别的几个地方 */
  copies: number;
  name_hit: boolean;
  hits: MaterialHit[];
  /** 没列出来的命中处数 */
  more_hits: number;
  /** 「读不了：要密码」「资料盘未连接」 */
  state_text: string | null;
  mentioned_meetings: number;
  /** 意思相近的才有 */
  score?: number;
}

export interface MaterialSearchState {
  /** 还没读完的材料个数 */
  pending: number;
  /** 恢复备份后全文索引在重建 */
  rebuilding: boolean;
  /** 查询预算用完，结果可能不全 */
  partial: boolean;
}

export interface MeetingFilters {
  q?: string;
  /** "none" 只看没归项目的会 */
  project_id?: string;
  attribution?: AttributionState;
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
  /** 3g：file 类交付物连到的资料盘文件；挪了位置按内容找，找不到时 gone */
  file_id?: number | null;
  name?: string;
  gone?: boolean;
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
  /** 3g：POST 交付物时回刚登记的那一条，［撤销］用 */
  deliverable_id?: number;
  /** 4e：在问的产出（「是这条任务的交付物吗？」）；旧后台没有 */
  suggestions?: RelationQuestion[];
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

/** 项目看板词典区的术语行（最多 50 条，新加的在前）；完整字段在词典页自己拉取。 */
export interface BoardGlossaryTerm {
  id: string;
  term: string;
  aliases: string[];
  category: string;
  also?: string[];
  is_cue?: boolean;
  source?: string;
}

export interface ProjectRecognitionProfile {
  also_names: ProjectAlsoName[];
  folder_names: string[];
  cue_terms: { total: number; cue: number };
  auto_30d: number;
  corrected_30d: number;
}

export interface ProjectBoard extends Project {
  meetings: BoardMeeting[];
  glossary_count?: number;
  glossary_terms?: BoardGlossaryTerm[];
  /** 「另有 N 条公共词也会用于本项目」 */
  public_glossary_count?: number;
  profile?: ProjectRecognitionProfile;
  cards?: ProjectCardsSummary;
  /** 4h：从材料里找到的词，前 6 项（旧后台没有这个字段，块不出） */
  glossary_candidates?: MaterialWord[];
  glossary_candidate_total?: number;
}

// ---------------------------------------------------------------------------
// 4h：从材料里找到的词（等你认；没有分数）
// ---------------------------------------------------------------------------

export interface MaterialWordHeard {
  meeting: { id: string; title: string; date: string };
  start_ms: number;
  /** 会上的原话（逐字稿里那一段的前后几个字） */
  quote: string;
  audio_url: string | null;
}

export interface MaterialWord {
  /** 按 (项目, key) 回答 */
  key: string;
  term: string;
  /** 原词是已有词条时：［记到『…』］只给那条加错写 */
  existing_term: { id: string; term: string } | null;
  /** 会上可能听成的写法，最多 3 个 */
  wrongs: { text: string; meetings: number }[];
  /** 正文里出现的文件数 */
  files: number;
  /** 会上说过几次 */
  spoken: number;
  heard: MaterialWordHeard[];
  file_names: { file_id: number; name: string }[];
  /** 只在会上没说过时给：词前后共 40 字 */
  file_quote: { file_id: number; quote: string } | null;
}

export interface MaterialWordsList {
  items: MaterialWord[];
  total: number;
}

/** 词典页收件箱：各项目的待认词汇总，待认多的项目在前；挖词关闭时 projects 为空 */
export interface GlossaryCandidateGroup {
  project_id: string;
  project_name: string;
  project_color: string | null;
  items: MaterialWord[];
  total: number;
}

export interface GlossaryCandidatesInbox {
  projects: GlossaryCandidateGroup[];
  total: number;
}

/** 会议页词典小节：这场会听错的、待认的写法 */
export interface MaterialPair {
  key: string;
  term: string;
  wrong: string;
  start_ms: number;
  quote: string;
  project: { id: string; name: string };
}

export interface MaterialWordAcceptResult {
  term: { id: string; term: string; aliases: string[]; is_cue: boolean };
  created: boolean;
  added_aliases: string[];
  skipped_aliases: string[];
  already: boolean;
  text: string;
  undo_until: string;
}

export interface MaterialWordRejectResult {
  text: string;
  undo_until: string;
}

export interface MaterialWordUndoResult {
  status: "pending";
  text: string;
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
  /** 项目词是否参与认项目 */
  is_cue?: boolean;
  /** 也叫：不改写，只用于识别项目和搜索 */
  also?: string[];
}

/**
 * 词典筛选 chip：公共（总在）→ 全部项目（含 0 个词的，按最近开会排序）→ 旧分组桶
 * （只在还有没整理的旧分组时出现），由后端定序。
 */
export interface GlossaryScope {
  kind: "general" | "project" | "bucket";
  key: string;
  label: string;
  color: string | null;
  count: number;
  last_meeting_at?: string | null;
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
  /** 2 字片段扩成整词前的那一对（「只记 2 字」） */
  alt_wrong?: string | null;
  alt_correct?: string | null;
  confirmed_term_id?: string | null;
  confirmed_wrong?: string | null;
  meeting_title?: string | null;
  /** 会议当前所属的项目：默认记到这里 */
  target_project_id?: string | null;
  target_project_name?: string | null;
  target_project_color?: string | null;
  /** 正确写法已是某条词条时，那条词条在哪 */
  existing_term_id?: string | null;
  existing_term_project_id?: string | null;
  existing_term_project_name?: string | null;
  /** 保存纪要时直接记入了（改成的写法已是词条、会议有项目），前端提示可撤销 */
  auto_recorded?: boolean;
}

/** 确认一条建议记到哪：auto=会议当前的项目；public=公共；其余是 project_id */
export type GlossaryTarget = "auto" | "public" | string;

export interface GlossaryConfirmResult {
  ok: boolean;
  term: GlossaryTerm | null;
  created: boolean;
  wrong: string;
  correct: string;
  suggestion: GlossarySuggestion | null;
}

/** 词条重名 409 带回来的已有词条 */
export interface GlossaryTermConflict {
  term_id: string;
  term: string;
  project_id: string | null;
  project_name: string | null;
  aliases: string[];
  also: string[];
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
  /** 老数据里同一个文件夹还挂在别的项目下 */
  shared_with?: { project_id: string; project_name: string }[];
  /** 会议卡片写给谁：同一文件夹挂在几个项目下时，只写给最早挂上的那个 */
  cards_owner_id?: string;
  /** 挂载或替换后当场补写了几张会议卡片 */
  cards_written?: number;
}

/** 文件名索引的进度（2d），项目页材料那一节每个根目录一行 */
export interface MaterialIndexRoot {
  root_id: number;
  project_id: string;
  path: string;
  state: "pending" | "walking" | "done" | "offline" | "missing" | "error";
  /** 已认得的文件名个数 */
  files: number;
  /** node_modules、.git 等只记了个数的文件夹 */
  name_only_dirs: number;
  /** 至少扫完过一整轮 */
  indexed_once: boolean;
  last_full_at: string | null;
  updated_at: string | null;
  error: string | null;
}

export interface MaterialIndexStatus {
  roots: MaterialIndexRoot[];
}

/** 首页「材料 还剩 N 个」（3e）：来自材料循环内存里的计数 */
export interface MaterialsProgress {
  pending: number;
  /** busy：转写会议时先停 */
  paused: "busy" | null;
  /** 还剩的里面在没插的盘上的 */
  offline_pending: number;
}

export type UnreadableReason = "password" | "corrupt" | "unsupported" | "timeout" | "permission";
export type MaterialNote = "small_image" | "no_text" | "no_speech" | "truncated" | "meeting_audio";

/** 每个根目录读了多少、为什么停（3e），按文件数算 */
export interface MaterialCoverageRoot {
  root_id: number;
  project_id: string;
  path: string;
  state: MaterialIndexRoot["state"];
  online: boolean;
  names: { files: number; name_only_dirs: number; symlinks: number };
  content: {
    total: number;
    done: number;
    pending: number;
    paused: "busy" | null;
    waiting: { what: string; files: number; hint: string }[];
    unreadable: Record<UnreadableReason, number>;
    notes: Record<MaterialNote, number>;
    names_only: { cards: number; other: number };
  };
}

export interface MaterialCoverage {
  roots: MaterialCoverageRoot[];
}

export interface MaterialUnreadableItem {
  file_id: number;
  name: string;
  rel_path: string;
  path: string;
  root_id: number;
  reason: UnreadableReason;
  checked_at: string | null;
}

export interface MaterialUnreadablePage {
  items: MaterialUnreadableItem[];
  total: number;
  next_offset: number | null;
}

export type MaterialStateKind = "done" | "pending" | "waiting" | "unreadable" | "names_only" | "gone";

/** 文件状态：text 是后端说法表里的那一句，前端只显示它 */
export interface MaterialFileState {
  kind: MaterialStateKind;
  reason: UnreadableReason | null;
  note: string | null;
  what: string | null;
  paused: "busy" | null;
  meeting: { id: string; title: string } | null;
  text: string;
}

export type MaterialPreviewKind = "text" | "table" | "image" | "pdf" | "media" | "none";

export interface MaterialPreviewContent {
  kind: MaterialPreviewKind;
  lines: string[];
  /** 超出显示的行数 */
  more: boolean;
  rows: string[][];
  sheet: string | null;
  image_url: string | null;
  page_url: string | null;
  media_url: string | null;
  playable: boolean;
  duration_ms: number | null;
  transcript: { start_ms: number | null; end_ms: number | null; text: string }[];
}

export interface MaterialFileInfo {
  id: number;
  name: string;
  ext: string;
  rel_path: string;
  root_id: number;
  folder_path: string;
  path: string;
  size: number | null;
  modified_at: string | null;
  project_id: string;
  project_name: string;
  root_online: boolean;
  gone: boolean;
  /** 4d：［用本机应用打开］出不出（只在这台 Mac 上、只开文档图片音视频）；旧后台没有 */
  can_open?: boolean;
}

export interface MaterialMention {
  meeting_id: string;
  title: string;
  date: string;
  count: number;
  first_ms: number | null;
  quote: string;
  audio_url: string | null;
  /** 4b 放宽的提到才有（字面行为 null，旧后台没有） */
  relation_id?: number | null;
  /** 4b：会上的那句说法，小字写「说的是『…』」代替「N 次」 */
  phrase?: string | null;
  via?: "stem" | "alias" | "time_hint" | null;
}

export interface MaterialDeliverable {
  deliverable_id: number;
  task_id: string;
  title: string;
  status: string;
}

/** 预览抽屉和关系图文件面板的数据；?parts=preview 时没有后三项 */
export interface MaterialFilePreview {
  file: MaterialFileInfo;
  state: MaterialFileState;
  preview: MaterialPreviewContent;
  mentions?: MaterialMention[];
  /** 在几场会上被提到（先数再取，不受 mentions 最多 40 条限制）；旧后台没有 */
  mentioned_meetings?: number;
  deliverables?: MaterialDeliverable[];
  can_reveal?: boolean;
  /** 4d：定位的那一段（只在请求时给了段号时有；null 是找不到了） */
  passage?: MaterialPassage | null;
  /** 4d：「内容相关的会」最多 5 条 */
  related_meetings?: RelatedMeeting[];
  /** 4e：在问的可能过时和产出（影响在前）；parts=preview 和旧后台没有 */
  questions?: RelationQuestion[];
}

/** 4d：预览定位到的那一段；stale 是文件后来改过、这是改之前读到的那段 */
export interface MaterialPassage {
  loc: string | null;
  start_ms: number | null;
  text: string;
  stale: boolean;
}

/** 4d：「内容相关的会」的一行（这里的行有 relation_id，［不相关］走 answerRelation） */
export interface RelatedMeeting {
  meeting_id: string;
  title: string;
  date: string;
  at_ms: number | null;
  quote: string;
  words: string[];
  audio_url: string | null;
  relation_id: number;
}

/** 冷启动：还没挂文件夹的项目找到的同名（默认勾选）或相近（默认不勾）文件夹 */
export interface ColdStartFolderItem {
  project_id: string;
  project_name: string;
  path: string;
  folder_name: string;
  match: "exact" | "similar";
}

export interface ColdStartFoldersPayload {
  items: ColdStartFolderItem[];
  snoozed_until: string | null;
  /** checking：后台还在看磁盘，2 秒后再查（旧后端没有，当作 ready） */
  state?: "ready" | "checking";
}

/** 词典里自动整理过的旧分组；project_id 为空表示归到了公共 */
export interface LegacyGroupsSummary {
  event_id: number;
  at: string;
  undone: boolean;
  groups: { scope: string; count: number; project_id: string | null; project_name: string | null }[];
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
  /** v17：说明（一两句话，最多 70 字），旧后端没有 */
  summary?: string;
  /** v17：所属项目的座次名次，未排座次为 null */
  project_seat?: number | null;
}

/** v17：需求的来源——提出它的会议、会上原话和时间锚（origin），或合并进来的原话（merged） */
export interface RequirementSource {
  id: number;
  kind: "origin" | "merged";
  meeting_id: string;
  meeting_title: string;
  recording_date: string | null;
  duration_ms: number | null;
  /** 画波形用的录音（/api/media/<id>/peaks）；这场会没有录音时为 null */
  audio_artifact_id: number | null;
  quote: string;
  anchor_ms: number | null;
  /** 合并自哪条候选 */
  via_candidate_title: string | null;
}

/** 新建、修改需求时传的来源：会议必填，原话和时间锚可空（只选了会、没挑原话） */
export interface RequirementSourceInput {
  meeting_id: string;
  quote?: string;
  anchor_ms?: number | null;
}

/** 需求池的状态：待认领（候选）＋需求的三态 */
export type PoolStatus = "pending" | RequirementStatus;
export type PoolTab = PoolStatus | "all";

/** 海报墙上的一张海报：正式需求或待认领候选，字段对齐 */
export interface PoolItem {
  kind: "requirement" | "candidate";
  id: string;
  title: string;
  summary: string;
  status: PoolStatus;
  /** 候选没有等级 */
  priority: RequirementPriority | null;
  /** 候选跟着来源会议的归属走，会议没归项目时为 null（未归项目） */
  project_id: string | null;
  project_name: string | null;
  project_color: string | null;
  project_seat: number | null;
  open_task_count: number;
  meeting_count: number;
  folder_count: number;
  latest_meeting_date: string | null;
  /** 提出它的那句；没有来源时海报不显示来源录音 */
  source: RequirementSource | null;
  /** 之后又跟进了几场＝关联会议数减 1 */
  follow_up_count: number;
  /** 候选：AI 判断的相近需求（还能合并时才有），默认动作是合并 */
  similar_requirement: { id: string; title: string; status: RequirementStatus } | null;
  default_action: "claim" | "merge" | null;
  /** 候选所属项目下有进行中或已搁置的需求时才能合并 */
  can_merge: boolean;
  created_at: string;
  updated_at: string;
  /** 候选认领建成或合并进去的需求 */
  requirement_id?: string | null;
  dropped_at?: string | null;
}

/** 「我的方向」条上的项目：已排座次的在前（seat 是名次），其后按最近会议排；count 是当前页签下的条数 */
export interface PoolProject {
  id: string;
  name: string;
  color: string;
  seat: number | null;
  latest_meeting_date: string | null;
  count: number;
}

export interface PoolCounts {
  pending: number;
  active: number;
  done: number;
  shelved: number;
  all: number;
}

export interface RequirementPoolPayload {
  items: PoolItem[];
  total: number;
  limit: number;
  offset: number;
  status: PoolTab;
  /** 随项目、优先级、名称变，不随所选页签变 */
  counts: PoolCounts;
  projects: PoolProject[];
  unassigned_count: number;
  /** 30 天内丢掉、还能撤销的候选 */
  dropped_count: number;
}

export interface PoolFilters {
  status: PoolTab;
  /** 逗号分隔的多个项目，unassigned 是未归项目 */
  project_id?: string;
  /** 逗号分隔的多个优先级 */
  priority?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

/** 候选自己的状态：认领、合并、丢掉过的候选不在墙上，认领页打开时才看得到 */
export type CandidateStatus = "pending" | "claimed" | "merged" | "dropped";

/** 认领页用：海报上的字段加全部来源 */
export interface CandidateDetail extends Omit<PoolItem, "status"> {
  status: CandidateStatus;
  sources: RequirementSource[];
}

export interface DroppedCandidates {
  items: Array<Omit<PoolItem, "status"> & { status: CandidateStatus; restore_until: string }>;
  total: number;
  undo_days: number;
}

export interface MergeTarget {
  id: string;
  title: string;
  status: RequirementStatus;
  priority: RequirementPriority;
  /** 提出它的那场会（没有来源时取最近一场关联会议） */
  meeting_title: string | null;
  recording_date: string | null;
  /** AI 判断的相近需求 */
  recommended: boolean;
}

export interface MergeTargets {
  project_id: string | null;
  items: MergeTarget[];
}

/** 会议详情［抽需求候选］的结果：新建、原地更新、并进已有候选、撤下（这场会原来待认领、这次没再抽到）各几条 */
export interface CandidateExtraction {
  status: "done" | "unavailable" | "failed";
  created: number;
  updated?: number;
  merged: number;
  removed?: number;
}

/** 认领撞名（409）：同项目已有的那条需求 */
export interface TitleConflict {
  detail: string;
  existing: { id: string; title: string; status: RequirementStatus } | null;
}

export interface RequirementFile {
  /** 相对所在材料文件夹的路径，如「考据/01-接口字段事实.md」 */
  relative_path: string;
  size_bytes: number;
  modified_at: string;
  /** 3g：从文件名索引查出来时带着，能在关系图上打开；读盘时没有 */
  file_id?: number;
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

/** 4h：需求页［复制给 Claude Code］复制的背景（Markdown 一律绝对路径，只进剪贴板，不在界面上显示） */
export interface RequirementContext {
  markdown: string;
  /** 旧［复制材料清单］的全部路径，加上卡片和交付物的路径 */
  paths: string[];
  /** 关联的会里，纪要卡片不在项目文件夹里的场数 */
  cards_missing: number;
}

export interface RequirementDetail extends RequirementSummary {
  folders: RequirementFolder[];
  /** 按 recording_date 倒序 */
  meetings: RequirementMeeting[];
  /** 未完成的在前，已完成、已过期、已取消在后 */
  tasks: Task[];
  /** v17：提出它的那句（头部波形取这场会） */
  source?: RequirementSource | null;
  /** v17：提出它的和合并进来的原话，按会议时间先后 */
  sources?: RequirementSource[];
  follow_up_count?: number;
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

/** 4d：预览抽屉打开哪份文件；passage 定位到那一段（from 只决定那一块的标题，默认 related） */
export interface PreviewPassage {
  contentKey: string;
  ordinal: number;
  from?: "related" | "search" | "answer";
  /** 加亮的词（共同词、搜的词） */
  words?: string[];
}

export interface PreviewTarget {
  fileId: number;
  startMs?: number;
  passage?: PreviewPassage;
}

// ---------------------------------------------------------------- 4g 项目内问答（都没有分数字段）

/** 说明：busy、fts_rebuilding、materials_pending、partial、local_model，页面最多显示 2 条 */
export interface AskNote {
  kind: string;
  text: string;
}

/** 一段原文。D 决议、N 纪要里的一行、T 会上原话、M 材料段落；name、loc 只给页面，不发给 AI */
export interface AskSource {
  id: string;
  kind: "decision" | "minutes" | "meeting" | "material";
  text: string;
  quote: string;
  start_ms: number | null;
  meeting_id?: string;
  decision_id?: string;
  title?: string | null;
  date?: string;
  end_ms?: number;
  audio_url?: string | null;
  speaker?: string | null;
  later_changed?: { date: string; decision_id: string } | null;
  file_id?: number;
  name?: string;
  content_key?: string;
  ordinal?: number;
  loc?: string | null;
  playable?: boolean;
  root_online?: boolean;
  /** 任务返回时才有：这段发出去了没有 */
  sent?: boolean;
}

export type AskLlmState = "ok" | "no_key" | "off" | "capped";

/** prepare 的返回：这一步什么都不发；confirm 不为空时［发送］正上方写它 */
export interface AskPlan {
  plan_id: string;
  expires_in: number;
  question: string;
  counts: { meetings: number; materials: number };
  confirm: { text: string; host: string } | null;
  local_model: boolean;
  llm: AskLlmState;
  highlight: string[];
  sources: AskSource[];
  notes: AskNote[];
  unattributed_meetings: number;
}

export interface AskAnswer {
  text: string;
  cited: string[];
  found: boolean;
  no_evidence: boolean;
  truncated: boolean;
}

export type AskJob =
  | { state: "waiting"; text: string }
  | {
      state: "done";
      answer: AskAnswer;
      sent: { meetings: number; materials: number };
      sources: AskSource[];
      notes: AskNote[];
      local_model: boolean;
    }
  | { state: "stopped"; reason: string; text: string; retry: boolean; sources: AskSource[] };
