export type HealthLevel = "healthy" | "degraded" | "failed" | "unknown";

export interface BootstrapPayload {
  csrf_token: string;
  mobile_read_only: boolean;
  semantic_enabled: boolean;
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
  };
  scanner?: Record<string, unknown>;
  semantic?: Record<string, unknown>;
  relay_worker?: Record<string, unknown>;
  backup?: Record<string, unknown>;
}

export interface Project {
  id: string;
  name: string;
  color: string;
  created_at?: string;
  meeting_count?: number;
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
  audio_artifact_id?: number | null;
  segment_count?: number;
  conflict?: number;
  tags: Tag[];
  created_at?: string;
  updated_at?: string;
}

export interface MeetingDetail extends MeetingSummary {
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
