import type {
  BootstrapPayload,
  HealthPayload,
  Job,
  JobSubstate,
  JobSubstateName,
  JobSubstateStatus,
  JobsPayload,
  AsrGoldSample,
  AsrShadowRun,
  MeetingDetail,
  MeetingFilters,
  MeetingSummary,
  MeetingsPayload,
  Project,
  SearchItem,
  Segment,
  Tag,
  TranscriptComparisonPayload,
  MinutesEvidence,
  TranscriptVersion,
} from "./types";

let csrfToken = "";

interface RelayJobPayload {
  job_id: string;
  status: Job["state"];
  meeting_id?: string | null;
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

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
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
    throw new ApiError(detail, response.status);
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
  search: (query: string, mode: "exact" | "semantic") =>
    read<{ mode: "exact" | "semantic"; items: SearchItem[] }>(
      `/api/search${queryString({ q: query, mode })}`,
    ),
  projects: () => read<Project[]>("/api/projects"),
  tags: () => read<Tag[]>("/api/tags"),
  createProject: (name: string, color: string) =>
    write<Project>("/api/projects", "POST", { name, color }),
  createTag: (name: string, color: string) =>
    write<Tag>("/api/tags", "POST", { name, color }),
  updateMeeting: (
    meetingId: string,
    metadata: { project_id?: string; tag_ids?: string[]; title?: string },
  ) => write<MeetingDetail>(`/api/meetings/${meetingId}`, "PATCH", metadata),
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
    write<{ version_id: string }>(`/api/meetings/${meetingId}/minutes`, "PUT", {
      base_version_id: baseVersionId,
      markdown,
    }),
  regenerateMinutes: (meetingId: string) =>
    write<{ status: string }>(`/api/meetings/${meetingId}/minutes/regenerate`, "POST", {}),
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
