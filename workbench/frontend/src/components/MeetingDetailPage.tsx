import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError, type ApiClient, type ConflictResolutionAction } from "../api";
import { reassignNote } from "../cardCopy";
import { formatDate, isDoneStatus, isUntitled, statusLabel, statusTone, versionKindLabel } from "../format";
import { parseHotwordsInput, validateHotwordsInput } from "../hotwords";
import type {
  AsrGoldSample,
  AsrShadowRun,
  AttributionState,
  GlossarySuggestion,
  LoadState,
  MeetingConflict,
  MeetingAttribution,
  MeetingCard,
  MeetingDetail,
  MinutesEvidence,
  Project,
  RequirementRef,
  Segment,
  Tag,
  TranscriptComparisonPayload,
  TranscriptVersion,
} from "../types";
import { AttributionBar, type AttributionChange } from "./AttributionBar";
import { AudioPlayer, type AudioPlayerHandle } from "./AudioPlayer";
import { useConfirm, type ConfirmOptions } from "./ConfirmDialog";
import { CopyFolderPathButton } from "./CopyFolderPathButton";
import { MeetingCardStatus } from "./MeetingCardStatus";
import { MeetingRequirementPicker } from "./MeetingRequirementPicker";
import { MeetingTasksPanel } from "./MeetingTasksPanel";
import { MinutesCorrectionsBar } from "./MinutesCorrectionsBar";
import { MinutesEvidencePanel, TranscriptComparisonPanel } from "./QualityReviewPanels";
import { TranscriptPanel } from "./TranscriptPanel";
import { NoticeBanner, useNotice, type NoticeTone } from "./Notice";

interface MeetingDetailPageProps {
  apiClient: ApiClient;
  initialSeekMs: number;
  isMobile: boolean;
  meeting: MeetingDetail;
  /** 本场任务确认/驳回之后通知外层，刷新侧栏「任务池」的待确认角标。 */
  onTasksChanged?: () => void;
  /** 「← 返回」按钮上显示的去处，默认录音档案。 */
  backLabel?: string;
  onBack: () => void;
  onClassificationSaved?: () => Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
  onNavigationLockChange?: (locked: boolean) => void;
  onReload: () => Promise<void>;
  projects: Project[];
  tags: Tag[];
  canWriteTasks?: boolean;
  onOpenTasks?: () => void;
  onOpenRequirement?: (requirementId: string) => void;
  /** 卡片状态条「去挂文件夹」「去项目页」 */
  onOpenProject?: (projectId: string) => void;
  /** 纠错词记入或撤销以后刷新侧栏词典的待确认角标 */
  onGlossaryChanged?: () => void;
}

type DetailTab = "transcript" | "minutes" | "tasks";

// 检查器主项目下拉里的特殊取值：「不归项目」（没项目的会上显式标一下）和「交给 AI 判断」。
const MARK_NO_PROJECT = "__none__";
// 带［撤销］的操作提示停 10 秒，比普通成功提示长一些
const UNDO_NOTICE_MS = 10_000;
const RETURN_TO_AI = "__ai__";

const EMPTY_PROJECT_LABELS: Partial<Record<AttributionState, string>> = {
  ai_pending: "等 AI 判断",
  none: "AI 没认出",
  new_project: "AI 没认出",
  needs_review: "等你选",
  manual_none: "不归项目（你标的）",
};

interface LiveProject {
  id: string | null;
  name: string | null;
  color: string | null;
  origin: "manual" | "ai" | null;
}

function liveProjectOf(meeting: MeetingDetail): LiveProject {
  return {
    id: meeting.project_id ?? null,
    name: meeting.project_name ?? null,
    color: meeting.project_color ?? null,
    origin: meeting.project_origin ?? null,
  };
}

interface QualitySnapshot {
  shadowRuns: AsrShadowRun[];
  transcriptVersions: TranscriptVersion[];
}

function segmentSnapshot(segments: Segment[]) {
  return JSON.stringify(
    segments.map(({ id, ordinal, start_ms, end_ms, speaker_label, speaker_name, text }) => ({
      id,
      ordinal,
      start_ms,
      end_ms,
      speaker_label: speaker_label ?? null,
      speaker_name: speaker_name ?? null,
      text,
    })),
  );
}

function nextSegmentId() {
  const suffix = globalThis.crypto?.randomUUID?.().replaceAll("-", "") ??
    `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `seg-${suffix}`;
}

export function splitSegmentsLocally(
  segments: Segment[],
  segmentId: string,
  characterIndex: number,
) {
  const index = segments.findIndex((segment) => segment.id === segmentId);
  if (index < 0) return segments;
  const source = segments[index];
  if (characterIndex <= 0 || characterIndex >= source.text.length) return segments;
  const midpoint = Math.round(
    source.start_ms + (source.end_ms - source.start_ms) * (characterIndex / source.text.length),
  );
  const replacement: Segment[] = [
    {
      ...source,
      id: nextSegmentId(),
      end_ms: midpoint,
      text: source.text.slice(0, characterIndex).trim(),
    },
    {
      ...source,
      id: nextSegmentId(),
      start_ms: midpoint,
      text: source.text.slice(characterIndex).trim(),
    },
  ];
  return [...segments.slice(0, index), ...replacement, ...segments.slice(index + 1)].map(
    (segment, ordinal) => ({ ...segment, ordinal }),
  );
}

export function mergeSegmentsLocally(segments: Segment[], firstId: string, secondId: string) {
  const firstIndex = segments.findIndex((segment) => segment.id === firstId);
  const secondIndex = segments.findIndex((segment) => segment.id === secondId);
  if (firstIndex < 0 || secondIndex !== firstIndex + 1) return segments;
  const first = segments[firstIndex];
  const second = segments[secondIndex];
  const separator = first.text.slice(-1).match(/[，。！？；：]/u) ? "" : " ";
  const merged: Segment = {
    ...first,
    id: nextSegmentId(),
    end_ms: second.end_ms,
    speaker_label: first.speaker_label === second.speaker_label ? first.speaker_label : null,
    speaker_name: first.speaker_name === second.speaker_name ? first.speaker_name : null,
    text: `${first.text}${separator}${second.text}`,
  };
  return [...segments.slice(0, firstIndex), merged, ...segments.slice(secondIndex + 1)].map(
    (segment, ordinal) => ({ ...segment, ordinal }),
  );
}

const conflictCopy: Record<MeetingConflict["kind"], { title: string; body: string }> = {
  external_source_change: {
    title: "外部文件版本冲突",
    body: "外部逐字稿或纪要与当前工作版本不一致，系统不会自动覆盖。",
  },
  audio_integrity: {
    title: "原音频完整性异常",
    body: "正式归档中的规范音频缺失或哈希不一致。请先恢复并重新执行完整性巡检。",
  },
  publish_recovery: {
    title: "写回恢复尚未完成",
    body: "上一次写回在文件切换阶段中断，需要完成恢复复验后才能继续写回。",
  },
  publish_post_commit: {
    title: "写回后复验失败",
    body: "数据库已提交，但正式文件复验不一致。系统已保留恢复材料，请先处理恢复。",
  },
  legacy_unknown: {
    title: "待人工核查的历史冲突",
    body: "该冲突来自旧版本且无法安全分类，请核对事件记录后处理。",
  },
};

function SafeMarkdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      components={{
        a: ({ href, children: linkChildren, ...props }) => {
          let safe = false;
          if (href) {
            try {
              safe = ["http:", "https:"].includes(new URL(href).protocol);
            } catch {
              safe = false;
            }
          }
          return safe ? (
            <a {...props} href={href} rel="noreferrer noopener" target="_blank">
              {linkChildren}
            </a>
          ) : (
            <span>{linkChildren}</span>
          );
        },
        img: ({ alt }) => <span>{alt ?? "图片已隐藏"}</span>,
      }}
      remarkPlugins={[remarkGfm]}
      skipHtml
    >
      {children}
    </ReactMarkdown>
  );
}

export function MeetingDetailPage({
  apiClient,
  initialSeekMs,
  isMobile,
  meeting,
  backLabel = "录音档案",
  onTasksChanged,
  onBack,
  onClassificationSaved,
  onDirtyChange,
  onNavigationLockChange,
  onReload,
  projects,
  tags,
  canWriteTasks = false,
  onOpenTasks,
  onOpenRequirement,
  onOpenProject,
  onGlossaryChanged,
}: MeetingDetailPageProps) {
  const playerRef = useRef<AudioPlayerHandle>(null);
  const [currentMs, setCurrentMs] = useState(initialSeekMs);
  const [tab, setTab] = useState<DetailTab>("transcript");
  const [engine, setEngine] = useState<"funasr" | "whisper" | "qwen">("funasr");
  const [candidateSegments, setCandidateSegments] = useState<Segment[]>([]);
  const [comparison, setComparison] = useState<TranscriptComparisonPayload | null>(null);
  const [candidateState, setCandidateState] = useState<LoadState>("idle");
  const [candidateError, setCandidateError] = useState("");
  const [goldSamples, setGoldSamples] = useState<AsrGoldSample[]>([]);
  const [goldState, setGoldState] = useState<"loading" | "ready" | "error">(
    isMobile ? "ready" : "loading",
  );
  const [goldDirty, setGoldDirty] = useState(false);
  const [pendingTaskCount, setPendingTaskCount] = useState(0);
  const [shadowRuns, setShadowRuns] = useState<AsrShadowRun[]>(meeting.asr_shadow_runs ?? []);
  const [qualityVersions, setQualityVersions] = useState<TranscriptVersion[]>(meeting.transcript_versions);
  const [hotwordText, setHotwordText] = useState("");
  const [evidence, setEvidence] = useState<MinutesEvidence | null>(null);
  const [evidenceState, setEvidenceState] = useState<"loading" | "ready" | "missing" | "unavailable">("loading");
  const [evidenceMessage, setEvidenceMessage] = useState("");
  const [evidenceVersionId, setEvidenceVersionId] = useState<string | null>(null);
  const [editingTranscript, setEditingTranscript] = useState(false);
  const [editingMinutes, setEditingMinutes] = useState(false);
  const [segments, setSegments] = useState<Segment[]>(meeting.segments);
  const [baselineSegments, setBaselineSegments] = useState<Segment[]>(meeting.segments);
  const { notice, setNotice, dismissNotice } = useNotice();
  const [undoUntil, setUndoUntil] = useState<string | null>(null);
  const [attribution, setAttribution] = useState<MeetingAttribution | undefined>(meeting.attribution);
  const [card, setCard] = useState<MeetingCard | undefined>(meeting.card);
  const [liveProject, setLiveProject] = useState<LiveProject>(() => liveProjectOf(meeting));
  const [busy, setBusy] = useState(false);
  const [confirm, confirmDialog] = useConfirm();
  const [savingKind, setSavingKind] = useState<"transcript" | "minutes" | "classification" | null>(null);
  const [saveConflict, setSaveConflict] = useState<"transcript" | "minutes" | null>(null);
  // 最近一次保存纪要捕获到的错字更正，在编辑器下方就地确认
  const [corrections, setCorrections] = useState<GlossarySuggestion[]>([]);
  const [speakerLabel, setSpeakerLabel] = useState(meeting.speakers[0]?.label ?? "");
  const [speakerName, setSpeakerName] = useState("");
  const [selectedProjectId, setSelectedProjectId] = useState(meeting.project_id ?? "");
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>(meeting.tags.map((tag) => tag.id));
  const [baselineProjectId, setBaselineProjectId] = useState(meeting.project_id ?? "");
  const [baselineTagIds, setBaselineTagIds] = useState<string[]>(meeting.tags.map((tag) => tag.id));
  const [selectedRequirementRefs, setSelectedRequirementRefs] = useState<RequirementRef[]>(meeting.requirements ?? []);
  const [baselineRequirementRefs, setBaselineRequirementRefs] = useState<RequirementRef[]>(meeting.requirements ?? []);
  const [transcriptVersion, setTranscriptVersion] = useState(meeting.current_transcript_version_id ?? "");
  const [minutesVersion, setMinutesVersion] = useState(meeting.current_minutes_version_id ?? "");
  const [transcriptBaseVersionId, setTranscriptBaseVersionId] = useState<string | null>(
    meeting.current_transcript_version_id ?? null,
  );
  const [minutesBaseVersionId, setMinutesBaseVersionId] = useState<string | null>(
    meeting.current_minutes_version_id ?? null,
  );
  const currentMinutes = useMemo(
    () => meeting.minutes_versions.find((version) => version.id === meeting.current_minutes_version_id) ?? meeting.minutes_versions[0],
    [meeting],
  );
  const currentMinutesVersionId = meeting.current_minutes_version_id ?? currentMinutes?.id ?? null;
  const [minutes, setMinutes] = useState(currentMinutes?.markdown ?? "");
  const [baselineMinutes, setBaselineMinutes] = useState(currentMinutes?.markdown ?? "");
  const revisions = useRef({ transcript: 0, minutes: 0, classification: 0 });
  const evidenceRequestSequence = useRef(0);
  const goldRequestSequence = useRef(0);
  const goldDirtyRef = useRef(false);
  const pendingQualitySnapshot = useRef<QualitySnapshot | null>(null);
  const qwenPollSequence = useRef(0);
  const whisperVersion = useMemo(
    () =>
      [...qualityVersions]
        .filter((version) => version.kind === "whisper_reference")
        .sort((left, right) => right.version_no - left.version_no)[0],
    [qualityVersions],
  );
  const latestQwenRun = useMemo(
    () => [...shadowRuns].sort((left, right) => String(right.created_at ?? "").localeCompare(String(left.created_at ?? "")))[0],
    [shadowRuns],
  );
  const qwenVersion = useMemo(() => {
    const byRun = latestQwenRun?.transcript_version_id
      ? qualityVersions.find((version) => version.id === latestQwenRun.transcript_version_id)
      : undefined;
    return byRun ?? [...qualityVersions]
      .filter((version) => version.kind === "qwen_reference")
      .sort((left, right) => right.version_no - left.version_no)[0];
  }, [latestQwenRun?.transcript_version_id, qualityVersions]);
  const candidateVersion: TranscriptVersion | undefined = engine === "whisper"
    ? whisperVersion
    : engine === "qwen"
      ? qwenVersion
      : undefined;
  const hotwordError = validateHotwordsInput(hotwordText);
  const transcriptDirty = useMemo(
    () => segmentSnapshot(segments) !== segmentSnapshot(baselineSegments),
    [baselineSegments, segments],
  );
  const minutesDirty = minutes !== baselineMinutes;
  const requirementsDirty =
    selectedRequirementRefs.map((requirement) => requirement.id).sort().join(",") !==
    baselineRequirementRefs.map((requirement) => requirement.id).sort().join(",");
  const classificationDirty =
    selectedProjectId !== baselineProjectId ||
    requirementsDirty ||
    [...selectedTagIds].sort().join("\u0000") !==
      [...baselineTagIds].sort().join("\u0000");
  const hasUnsavedChanges = transcriptDirty || minutesDirty || classificationDirty || goldDirty;
  const isSaving = savingKind !== null;
  const destructiveBlocked = busy || hasUnsavedChanges;
  const openConflicts = useMemo(() => {
    const typed = (meeting.conflicts ?? []).filter((conflict) => conflict.status === "open");
    if (typed.length > 0) return typed;
    if (!meeting.conflict) return [];
    return [
      {
        id: "",
        meeting_id: meeting.id,
        kind: "external_source_change",
        status: "open",
        payload: {},
        created_at: meeting.updated_at ?? "",
        updated_at: meeting.updated_at ?? "",
      } satisfies MeetingConflict,
    ];
  }, [meeting.conflict, meeting.conflicts, meeting.id, meeting.updated_at]);

  useEffect(() => {
    setSegments(meeting.segments);
    setBaselineSegments(meeting.segments);
    setMinutes(currentMinutes?.markdown ?? "");
    setBaselineMinutes(currentMinutes?.markdown ?? "");
    setTranscriptVersion(meeting.current_transcript_version_id ?? "");
    setMinutesVersion(meeting.current_minutes_version_id ?? "");
    setTranscriptBaseVersionId(meeting.current_transcript_version_id ?? null);
    setMinutesBaseVersionId(meeting.current_minutes_version_id ?? null);
    setEngine("funasr");
    setCandidateSegments([]);
    setComparison(null);
    setCandidateState("idle");
    setCandidateError("");
    setGoldSamples([]);
    setGoldState(isMobile ? "ready" : "loading");
    setGoldDirty(false);
    goldDirtyRef.current = false;
    pendingQualitySnapshot.current = null;
    setShadowRuns(meeting.asr_shadow_runs ?? []);
    setQualityVersions(meeting.transcript_versions);
    setHotwordText("");
    setSelectedProjectId(meeting.project_id ?? "");
    setSelectedTagIds(meeting.tags.map((tag) => tag.id));
    setBaselineProjectId(meeting.project_id ?? "");
    setAttribution(meeting.attribution);
    setCard(meeting.card);
    setLiveProject(liveProjectOf(meeting));
    setBaselineTagIds(meeting.tags.map((tag) => tag.id));
    setSelectedRequirementRefs(meeting.requirements ?? []);
    setBaselineRequirementRefs(meeting.requirements ?? []);
    revisions.current = { transcript: 0, minutes: 0, classification: 0 };
    setEditingTranscript(false);
    setEditingMinutes(false);
    setSaveConflict(null);
  }, [currentMinutes?.markdown, isMobile, meeting]);

  useEffect(() => {
    onDirtyChange?.(hasUnsavedChanges);
    return () => onDirtyChange?.(false);
  }, [hasUnsavedChanges, onDirtyChange]);

  useEffect(() => {
    onNavigationLockChange?.(isSaving);
    return () => {
      if (isSaving) onNavigationLockChange?.(false);
    };
  }, [isSaving, onNavigationLockChange]);

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasUnsavedChanges]);

  useEffect(() => {
    if (engine === "funasr") return;
    if (!candidateVersion) {
      setCandidateSegments([]);
      setComparison(null);
      setCandidateState("empty");
      setCandidateError("");
      return;
    }
    let active = true;
    setCandidateState("loading");
    setCandidateError("");
    setComparison(null);
    const request = typeof apiClient.transcriptComparison === "function"
      ? apiClient.transcriptComparison(meeting.id, candidateVersion.id)
          .then((payload) => ({ kind: "comparison" as const, payload }))
      : apiClient.transcriptVersionSegments(meeting.id, candidateVersion.id)
          .then((payload) => ({ kind: "segments" as const, payload }));
    void request
      .then((payload) => {
        if (!active) return;
        if (payload.kind === "comparison") {
          setComparison(payload.payload);
          setCandidateSegments([]);
          setCandidateState(payload.payload.items.length ? "ready" : "empty");
        } else {
          setCandidateSegments(payload.payload.items);
          setCandidateState(payload.payload.items.length ? "ready" : "empty");
        }
      })
      .catch((error) => {
        if (!active) return;
        setCandidateSegments([]);
        setComparison(null);
        setCandidateState("error");
        setCandidateError(error instanceof Error ? error.message : "对照稿读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, candidateVersion, engine, meeting.id]);

  const loadGoldSamples = useCallback(async () => {
    if (isMobile || typeof apiClient.asrGoldSamples !== "function") {
      setGoldState("ready");
      return;
    }
    const sequence = ++goldRequestSequence.current;
    setGoldState("loading");
    try {
      const payload = await apiClient.asrGoldSamples(meeting.id);
      if (sequence !== goldRequestSequence.current) return;
      setGoldSamples(payload.items);
      setGoldState("ready");
    } catch {
      if (sequence !== goldRequestSequence.current) return;
      setGoldState("error");
    }
  }, [apiClient, isMobile, meeting.id]);

  const acceptQualitySnapshot = useCallback((snapshot: MeetingDetail) => {
    const qualitySnapshot: QualitySnapshot = {
      shadowRuns: snapshot.asr_shadow_runs ?? [],
      transcriptVersions: snapshot.transcript_versions,
    };
    if (goldDirtyRef.current) {
      pendingQualitySnapshot.current = qualitySnapshot;
      // Runtime status may advance, but the version list remains pinned so the active candidate cannot switch.
      setShadowRuns(qualitySnapshot.shadowRuns);
      return;
    }
    pendingQualitySnapshot.current = null;
    setShadowRuns(qualitySnapshot.shadowRuns);
    setQualityVersions(qualitySnapshot.transcriptVersions);
  }, []);

  const changeGoldDirty = useCallback((dirty: boolean) => {
    goldDirtyRef.current = dirty;
    setGoldDirty(dirty);
    if (dirty || !pendingQualitySnapshot.current) return;
    const snapshot = pendingQualitySnapshot.current;
    pendingQualitySnapshot.current = null;
    setShadowRuns(snapshot.shadowRuns);
    setQualityVersions(snapshot.transcriptVersions);
  }, []);

  const loadPendingTaskCount = useCallback(async () => {
    try {
      const payload = await apiClient.tasks({ meeting_id: meeting.id, status: "pending_confirm", limit: 1 });
      setPendingTaskCount(payload.total);
    } catch {
      setPendingTaskCount(0);
    }
  }, [apiClient, meeting.id]);

  useEffect(() => {
    void loadPendingTaskCount();
  }, [loadPendingTaskCount]);

  useEffect(() => {
    void loadGoldSamples();
    return () => {
      goldRequestSequence.current += 1;
    };
  }, [loadGoldSamples]);

  useEffect(() => {
    if (
      !latestQwenRun ||
      !["queued", "running"].includes(latestQwenRun.state) ||
      typeof apiClient.meeting !== "function"
    ) return;
    let active = true;
    const refreshQualitySnapshot = async () => {
      if (document.hidden) return;
      const sequence = ++qwenPollSequence.current;
      try {
        const snapshot = await apiClient.meeting(meeting.id);
        if (!active || sequence !== qwenPollSequence.current) return;
        acceptQualitySnapshot(snapshot);
      } catch {
        // Polling is advisory. The current state remains visible until a later refresh succeeds.
      }
    };
    const timer = window.setInterval(() => void refreshQualitySnapshot(), 5_000);
    const refreshWhenVisible = () => {
      if (!document.hidden) void refreshQualitySnapshot();
    };
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      active = false;
      qwenPollSequence.current += 1;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [acceptQualitySnapshot, apiClient, latestQwenRun?.id, latestQwenRun?.state, meeting.id]);

  useEffect(() => {
    if (tab !== "minutes" || typeof apiClient.minutesEvidence !== "function") return;
    const requestSequence = ++evidenceRequestSequence.current;
    const requestVersionId = currentMinutesVersionId;
    setEvidence(null);
    setEvidenceVersionId(requestVersionId);
    setEvidenceState("loading");
    setEvidenceMessage("");
    void apiClient.minutesEvidence(meeting.id)
      .then((payload) => {
        if (requestSequence !== evidenceRequestSequence.current) return;
        setEvidence(payload);
        setEvidenceState("ready");
      })
      .catch((error) => {
        if (requestSequence !== evidenceRequestSequence.current) return;
        setEvidence(null);
        if (error instanceof ApiError && error.status === 404) {
          setEvidenceState("missing");
        } else {
          setEvidenceState("unavailable");
          const raw = error instanceof Error ? error.message : "来源证据未通过复验";
          setEvidenceMessage(raw.includes("/") ? "来源证据未通过一致性复验" : raw);
        }
      });
    return () => {
      if (requestSequence === evidenceRequestSequence.current) {
        evidenceRequestSequence.current += 1;
      }
    };
  }, [apiClient, currentMinutesVersionId, meeting.id, tab]);

  useEffect(() => {
    if (initialSeekMs > 0) playerRef.current?.seekTo(initialSeekMs);
  }, [initialSeekMs]);

  const audio = meeting.artifacts.find((artifact) => artifact.kind === "audio");
  const mediaUrl = audio ? `/api/media/${audio.id}` : null;
  const run = async <T,>(action: () => Promise<T>, success: string | ((result: T) => string)) => {
    setBusy(true);
    setNotice("");
    try {
      const result = await action();
      setNotice(typeof success === "function" ? success(result) : success);
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setBusy(false);
    }
  };
  const qwenNotice = (result: AsrShadowRun) => {
    if (result.state === "queued") return "Qwen 影子稿已排队";
    if (result.state === "running") return "Qwen 影子稿正在生成";
    if (result.state === "ready") return "Qwen 影子稿已就绪";
    if (result.state === "unavailable") return result.error || "Qwen 影子稿当前不可用";
    return result.error || "Qwen 影子稿生成失败";
  };
  const runQwenAction = async (action: () => Promise<AsrShadowRun>) => {
    setBusy(true);
    setNotice("");
    try {
      const result = await action();
      if (result.id) {
        setShadowRuns((current) => [result, ...current.filter((runItem) => runItem.id !== result.id)]);
      }
      setNotice(qwenNotice(result), result.state === "failed" || result.state === "unavailable" ? "warning" : "success");
      if (result.state === "ready" && typeof apiClient.meeting === "function") {
        const sequence = ++qwenPollSequence.current;
        const snapshot = await apiClient.meeting(meeting.id);
        if (sequence === qwenPollSequence.current) {
          acceptQualitySnapshot(snapshot);
        }
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Qwen 影子稿操作失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const changeSegments = (next: Segment[]) => {
    revisions.current.transcript += 1;
    setSegments(next);
  };
  const changeMinutes = (next: string) => {
    revisions.current.minutes += 1;
    setMinutes(next);
  };
  const changeProject = (next: string) => {
    revisions.current.classification += 1;
    setSelectedProjectId(next);
  };
  const changeTags = (next: string[]) => {
    revisions.current.classification += 1;
    setSelectedTagIds(next);
  };
  const changeRequirements = (next: RequirementRef[]) => {
    revisions.current.classification += 1;
    setSelectedRequirementRefs(next);
  };
  const commitTranscript = async (baseVersionId: string | null) => {
    const snapshot = segments.map((segment) => ({ ...segment }));
    const requestRevision = revisions.current.transcript;
    const result = await apiClient.saveTranscript(meeting.id, snapshot, baseVersionId);
    setTranscriptBaseVersionId(result.version_id);
    setBaselineSegments(snapshot);
    setSaveConflict(null);
    if (revisions.current.transcript !== requestRevision) {
      setNotice("请求中的逐字稿已保存；请求发出后的本地修改仍保留，请再次保存", "warning");
      return;
    }
    setNotice("逐字稿已保存在工作台，尚未写回文件夹");
    await onReload();
  };
  const saveTranscriptWith = async (baseVersionId: string | null) => {
    setBusy(true);
    setSavingKind("transcript");
    setNotice("");
    try {
      await commitTranscript(baseVersionId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setSaveConflict("transcript");
        setNotice(error.message, "error");
      } else {
        setSaveConflict(null);
        setNotice(error instanceof Error ? error.message : "操作失败", "error");
      }
    } finally {
      setSavingKind(null);
      setBusy(false);
    }
  };
  const saveTranscript = () => saveTranscriptWith(transcriptBaseVersionId);
  const overwriteTranscript = async () => {
    try {
      const fresh = await apiClient.meeting(meeting.id);
      await saveTranscriptWith(fresh.current_transcript_version_id ?? null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败", "error");
    }
  };
  const commitMinutes = async (baseVersionId: string | null) => {
    const snapshot = minutes;
    const requestRevision = revisions.current.minutes;
    const result = await apiClient.saveMinutes(meeting.id, snapshot, baseVersionId);
    setMinutesBaseVersionId(result.version_id);
    setBaselineMinutes(snapshot);
    setSaveConflict(null);
    if (result.corrections?.length) {
      setCorrections(result.corrections);
      if (result.corrections.some((item) => item.auto_recorded)) onGlossaryChanged?.();
    }
    if (revisions.current.minutes !== requestRevision) {
      setNotice("请求中的纪要已保存；请求发出后的本地修改仍保留，请再次保存", "warning");
      return;
    }
    setNotice("纪要草稿已保存");
    await onReload();
  };
  const saveMinutesWith = async (baseVersionId: string | null) => {
    setBusy(true);
    setSavingKind("minutes");
    setNotice("");
    try {
      await commitMinutes(baseVersionId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setSaveConflict("minutes");
        setNotice(error.message, "error");
      } else {
        setSaveConflict(null);
        setNotice(error instanceof Error ? error.message : "操作失败", "error");
      }
    } finally {
      setSavingKind(null);
      setBusy(false);
    }
  };
  const saveMinutes = () => saveMinutesWith(minutesBaseVersionId);
  const overwriteMinutes = async () => {
    try {
      const fresh = await apiClient.meeting(meeting.id);
      await saveMinutesWith(fresh.current_minutes_version_id ?? null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败", "error");
    }
  };
  const confirmThen = async (options: ConfirmOptions, action: () => Promise<unknown>) => {
    if (await confirm(options)) await action();
  };
  const discardSaveConflict = async () => {
    setBusy(true);
    setNotice("");
    setSaveConflict(null);
    try {
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setBusy(false);
    }
  };
  const saveClassification = async () => {
    const snapshotProjectId = selectedProjectId;
    const snapshotTagIds = [...selectedTagIds];
    const snapshotRequirementRefs = [...selectedRequirementRefs];
    const requestRevision = revisions.current.classification;
    setBusy(true);
    setSavingKind("classification");
    setNotice("");
    // 只带和 baseline 相比改过的字段：只勾标签不能顺手把项目写一遍，
    // 否则后端会把「AI 还没判断」的会当成你手动选了「不归项目」。
    const changes: { project_id?: string; tag_ids?: string[]; requirement_ids?: string[] } = {};
    if (snapshotProjectId !== baselineProjectId) {
      changes.project_id = snapshotProjectId === MARK_NO_PROJECT ? "" : snapshotProjectId;
    }
    if ([...snapshotTagIds].sort().join("\u0000") !== [...baselineTagIds].sort().join("\u0000")) {
      changes.tag_ids = snapshotTagIds;
    }
    const snapshotRequirementIds = snapshotRequirementRefs.map((requirement) => requirement.id);
    if (
      [...snapshotRequirementIds].sort().join(",") !==
      baselineRequirementRefs.map((requirement) => requirement.id).sort().join(",")
    ) {
      changes.requirement_ids = snapshotRequirementIds;
    }
    try {
      const saved = await apiClient.updateMeeting(meeting.id, changes);
      const effects = saved?.effects;
      const movedNote = effects
        ? reassignNote(effects.tasks_moved, effects.tasks_left.length, effects.card)
        : "";
      const effectsNote = movedNote ? `；${movedNote}` : "";
      setBaselineProjectId(snapshotProjectId);
      setBaselineTagIds(snapshotTagIds);
      setBaselineRequirementRefs(snapshotRequirementRefs);
      let refreshWarning = "";
      try {
        await onClassificationSaved?.();
      } catch {
        refreshWarning = "；项目统计暂未刷新，请稍后重新进入项目页查看";
      }
      if (revisions.current.classification !== requestRevision) {
        setNotice(`请求中的归档归属已保存${effectsNote}${refreshWarning}；后续本地修改仍保留，请再次保存`, "warning");
        return;
      }
      // 带［撤销］的提示多停一会儿；过后在归属条的「刚改过」里还能改回
      setNotice(
        `会议归档归属已保存${effectsNote}${refreshWarning}`,
        refreshWarning ? "warning" : "success",
        effects?.undo_until ? UNDO_NOTICE_MS : undefined,
      );
      setUndoUntil(effects?.undo_until ?? null);
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      setSavingKind(null);
      setBusy(false);
    }
  };
  const split = (segmentId: string, characterIndex: number) => {
    setSegments((current) => {
      const next = splitSegmentsLocally(current, segmentId, characterIndex);
      if (next !== current) revisions.current.transcript += 1;
      return next;
    });
    setNotice("已在本地拆分；保存草稿后才会写入资料库", "warning");
  };
  const merge = (firstId: string, secondId: string) => {
    setSegments((current) => {
      const next = mergeSegmentsLocally(current, firstId, secondId);
      if (next !== current) revisions.current.transcript += 1;
      return next;
    });
    setNotice("已在本地合并；保存草稿后才会写入资料库", "warning");
  };
  const leaveDetail = () => {
    if (isSaving) return;
    if (
      !hasUnsavedChanges ||
      window.confirm("当前会议仍有未保存修改。放弃这些修改并离开吗？")
    ) {
      onBack();
    }
  };
  const resolveConflict = (conflict: MeetingConflict, action: ConflictResolutionAction) =>
    run(
      () =>
        conflict.id
          ? apiClient.resolveConflict(meeting.id, conflict.id, action)
          : apiClient.resolveLegacyConflict(meeting.id, action),
      action === "keep_draft"
        ? "已保留当前草稿，写回时将覆盖外部文本"
        : action === "accept_external"
          ? "已采用外部版本，原草稿保留在版本历史"
          : "草稿已丢弃，当前版本已切换为外部文件",
    );
  const requestRetranscription = () => {
    const hotwords = parseHotwordsInput(hotwordText);
    return hotwords.length
      ? apiClient.retranscribe(meeting.id, hotwords)
      : apiClient.retranscribe(meeting.id);
  };
  const saveGoldSample = async (segmentId: string, reference: string) => {
    if (goldState !== "ready") throw new Error("金标尚未完成加载");
    const existing = goldSamples.find((sample) => sample.segment_id === segmentId);
    const saved = await apiClient.saveAsrGoldSample(meeting.id, {
      segment_id: segmentId,
      reference,
      entities: existing?.entities ?? [],
      numbers: existing?.numbers ?? [],
      tags: existing?.tags ?? [],
    });
    setGoldSamples((current) => [
      ...current.filter((sample) => sample.segment_id !== segmentId),
      saved,
    ]);
    return saved;
  };

  // 归属条只 PATCH 项目，成功后就地同步检查器的项目下拉，不重载详情，
  // 没保存的纪要和逐字稿都不受影响。
  const refreshCard = async () => {
    // 确认归属不带回卡片；旧后端没有这个接口时状态条保持原样
    if (typeof apiClient.meetingCard !== "function") return;
    try {
      setCard(await apiClient.meetingCard(meeting.id));
    } catch {
      // 只是状态条，读不到下次进来再看
    }
  };
  const applyAttributionChange = (change: AttributionChange) => {
    setAttribution(change.attribution);
    if (change.card) setCard(change.card);
    else void refreshCard();
    if (change.project) {
      const next = change.project;
      setLiveProject(next);
      setSelectedProjectId(next.id ?? "");
      setBaselineProjectId(next.id ?? "");
    } else {
      setLiveProject((current) => ({ ...current, origin: change.attribution.origin }));
    }
  };
  const showAttributionNotice = (message: string, until?: string, tone: NoticeTone = "success") => {
    setNotice(message, tone, until ? UNDO_NOTICE_MS : undefined);
    setUndoUntil(until ?? null);
  };
  const undoFromBanner = async () => {
    setBusy(true);
    try {
      const detail = await apiClient.undoMeetingProject(meeting.id);
      if (detail.attribution) {
        applyAttributionChange({
          attribution: detail.attribution,
          project: {
            id: detail.project_id ?? null,
            name: detail.project_name ?? null,
            color: detail.project_color ?? null,
            origin: detail.project_origin ?? null,
          },
          card: detail.card,
        });
      }
      showAttributionNotice(
        detail.effects?.card?.action === "moved" ? "已撤销刚才的改动，会议卡片也搬回去了" : "已撤销刚才的改动",
      );
      await onClassificationSaved?.();
    } catch (error) {
      showAttributionNotice(error instanceof Error ? error.message : "撤销失败", undefined, "error");
    } finally {
      setBusy(false);
    }
  };
  const canUndo = undoUntil !== null && Date.now() < Date.parse(undoUntil);
  const projectDirty = selectedProjectId !== baselineProjectId;
  const effectiveProjectId =
    selectedProjectId === MARK_NO_PROJECT || selectedProjectId === RETURN_TO_AI ? "" : selectedProjectId;
  const attributionStateNow = attribution?.state;
  const emptyProjectLabel = baselineProjectId
    ? "不归项目"
    : (attributionStateNow && EMPTY_PROJECT_LABELS[attributionStateNow]) ?? "未归项目";

  return (
    <section className={`detail-page ${isMobile ? "detail-page--mobile" : ""}`}>
      {confirmDialog}
      <header className="detail-header">
        <button className="back-button" disabled={isSaving} onClick={leaveDetail} type="button">← 返回{backLabel}</button>
        <div className="detail-title-row">
          <div>
            <span className="archive-code">{meeting.id}</span>
            <h1 id={`detail-title-${meeting.id}`}>
              {meeting.title}
              {isUntitled(meeting.title, meeting.id) && <em className="untitled-chip">标题待生成</em>}
            </h1>
            <div className="detail-meta">
              <span>{formatDate(meeting.recording_date)}</span>
              {liveProject.name && <span className="project-mark"><i style={{ background: liveProject.color || "#767676" }} />{liveProject.name}</span>}
              {meeting.tags.map((tag) => <em key={tag.id}>{tag.name}</em>)}
            </div>
          </div>
          <div className="detail-state">
            <CopyFolderPathButton
              describedById={`detail-title-${meeting.id}`}
              label="复制归档文件夹路径"
              path={meeting.canonical_dir}
              withLabel
            />
            {!isDoneStatus(meeting.status) && (
              <span className={`status-badge status-badge--${statusTone(meeting.status)}`}>
                {statusLabel(statusTone(meeting.status))}
              </span>
            )}
            {isMobile && <span className="read-only-chip">只读 · 可改项目</span>}
          </div>
        </div>
        {attribution && (
          <AttributionBar
            apiClient={apiClient}
            attribution={attribution}
            lockedReason={projectDirty ? "右侧有未保存的归属修改" : undefined}
            meetingId={meeting.id}
            onChange={applyAttributionChange}
            onNotice={showAttributionNotice}
            onProjectsChanged={onClassificationSaved}
            onSeek={(milliseconds) => playerRef.current?.seekTo(milliseconds)}
            projects={projects}
          />
        )}
        {card && (
          <MeetingCardStatus
            apiClient={apiClient}
            card={card}
            meetingId={meeting.id}
            onCardChange={setCard}
            onOpenProject={onOpenProject}
            projectId={liveProject.id}
            projectName={liveProject.name}
          />
        )}
      </header>

      {openConflicts.map((conflict) => {
        const legacy = !conflict.id;
        const copy = legacy
          ? {
              title: "检测到外部文件变更",
              body: "外部逐字稿与工作台里尚未写回的草稿不一致，系统不会自动覆盖。",
            }
          : conflictCopy[conflict.kind];
        const actionable = conflict.kind === "external_source_change";
        return (
          <section className="conflict-panel" key={conflict.id || "legacy-conflict"} role="alert">
            <div className="conflict-panel__copy">
              <span className="state-mark">!</span>
              <div>
                <h2>{copy.title}</h2>
                <p>{copy.body}</p>
              </div>
            </div>
            {!isMobile && actionable && (
              <div className="conflict-panel__actions desktop-only">
                <button
                  disabled={destructiveBlocked}
                  onClick={() => void resolveConflict(conflict, "keep_draft")}
                  type="button"
                >
                  保留草稿并确认覆盖
                </button>
                <button
                  disabled={destructiveBlocked}
                  onClick={() => void resolveConflict(conflict, "accept_external")}
                  type="button"
                >
                  采用外部版本
                </button>
                <button
                  className="danger-button"
                  disabled={destructiveBlocked}
                  onClick={() => void confirmThen(
                    {
                      title: "丢弃草稿？",
                      message: "工作台里这场会未写回的草稿会被丢弃，改用外部文件里的版本。丢弃后无法恢复。",
                      confirmLabel: "丢弃草稿",
                      tone: "danger",
                    },
                    () => resolveConflict(conflict, "discard_draft"),
                  )}
                  type="button"
                >
                  丢弃草稿
                </button>
              </div>
            )}
          </section>
        );
      })}

      <AudioPlayer
        durationMs={meeting.duration_ms}
        initialSeekMs={initialSeekMs}
        mediaUrl={mediaUrl}
        peaksUrl={audio ? `/api/media/${audio.id}/peaks` : null}
        onTimeChange={setCurrentMs}
        ref={playerRef}
      />

      <NoticeBanner notice={notice} onDismiss={dismissNotice}>
        {canUndo && (
          <button className="text-button action-banner__undo" disabled={busy} onClick={() => void undoFromBanner()} type="button">
            撤销
          </button>
        )}
      </NoticeBanner>

      {saveConflict && (
        <section className="conflict-panel" role="alert">
          <div className="conflict-panel__copy">
            <span className="state-mark">!</span>
            <div>
              <h2>{saveConflict === "transcript" ? "逐字稿有更新版本" : "纪要有更新版本"}</h2>
              <p>保存时发现工作台上已有更新版本，你的修改还留在本地，没有丢失。旧版本仍在版本历史里，可以回滚。</p>
            </div>
          </div>
          {!isMobile && (
            <div className="conflict-panel__actions desktop-only">
              <button
                disabled={busy}
                onClick={() => void (saveConflict === "transcript" ? overwriteTranscript() : overwriteMinutes())}
                type="button"
              >
                以我的内容覆盖最新版本
              </button>
              <button
                className="danger-button"
                disabled={busy}
                onClick={() => void confirmThen(
                  {
                    title: "丢弃我的修改？",
                    message: "你在这个页面上还没保存的修改会被丢掉，页面载入别处保存的最新版本。丢弃后无法恢复。",
                    confirmLabel: "丢弃并加载最新",
                    tone: "danger",
                  },
                  discardSaveConflict,
                )}
                type="button"
              >
                丢弃我的修改并加载最新
              </button>
            </div>
          )}
        </section>
      )}

      <div className="detail-tabs" role="tablist">
        <button aria-selected={tab === "transcript"} disabled={isSaving || goldDirty} onClick={() => setTab("transcript")} role="tab" type="button">逐字稿 <span>{segments.length}</span></button>
        <button aria-selected={tab === "minutes"} disabled={isSaving || goldDirty} onClick={() => setTab("minutes")} role="tab" type="button">会议纪要 <span>{meeting.minutes_versions.length}</span></button>
        {onOpenTasks && (
          <button aria-selected={tab === "tasks"} disabled={isSaving || goldDirty} onClick={() => setTab("tasks")} role="tab" type="button">本场任务{pendingTaskCount > 0 && <span>{pendingTaskCount}</span>}</button>
        )}
      </div>

      {tab === "transcript" ? (
        <div className="detail-workspace">
          <section className="editor-main">
            <div className="engine-bar">
              <div className="engine-switch" aria-label="转写引擎对照">
                <button aria-pressed={engine === "funasr"} disabled={isSaving || goldDirty} onClick={() => setEngine("funasr")} type="button">FunASR 主稿</button>
                <button aria-pressed={engine === "whisper"} disabled={isSaving || transcriptDirty || goldDirty} onClick={() => setEngine("whisper")} type="button">Whisper 对照</button>
                {qwenVersion && <button aria-pressed={engine === "qwen"} disabled={isSaving || transcriptDirty || goldDirty} onClick={() => setEngine("qwen")} type="button">Qwen 影子</button>}
              </div>
              {!isMobile && (
                <div className="qwen-run-control desktop-only">
                  {!latestQwenRun ? (
                    <button disabled={destructiveBlocked} onClick={() => void runQwenAction(() => apiClient.requestQwenShadow(meeting.id))} type="button">生成 Qwen 影子稿</button>
                  ) : latestQwenRun.state === "failed" || latestQwenRun.state === "unavailable" ? (
                    <>
                      <span className="qwen-state qwen-state--failed">
                        {latestQwenRun.state === "unavailable" ? "Qwen 当前不可用" : latestQwenRun.error || "Qwen 影子稿失败"}
                      </span>
                      {latestQwenRun.state === "unavailable" && latestQwenRun.error && <span className="qwen-state-detail">{latestQwenRun.error}</span>}
                      <button disabled={destructiveBlocked} onClick={() => void runQwenAction(() => apiClient.retryQwenShadow(meeting.id, latestQwenRun.id))} type="button">重试 Qwen 影子稿</button>
                    </>
                  ) : latestQwenRun.state === "queued" ? <span className="qwen-state">Qwen 已排队</span>
                    : latestQwenRun.state === "running" ? <span className="qwen-state">Qwen 生成中</span>
                      : latestQwenRun.state === "ready" && qwenVersion ? <span className="qwen-state qwen-state--ready">Qwen 已就绪</span>
                        : latestQwenRun.state === "ready" ? <span className="qwen-state qwen-state--failed">Qwen 版本不可用</span>
                        : <span className="qwen-state qwen-state--failed">Qwen 当前不可用</span>}
                </div>
              )}
            </div>
            {!isMobile && (
              <div className="editor-actions editor-actions--quality desktop-only">
                <label className="meeting-hotwords">
                  <span>本场热词</span>
                  <textarea aria-describedby={hotwordError ? "meeting-hotword-error" : undefined} aria-label="重新转写本场热词" disabled={isSaving} onChange={(event) => setHotwordText(event.target.value)} placeholder="逗号或换行分隔，最多 20 个" rows={1} value={hotwordText} />
                  {hotwordError && <small className="field-error" id="meeting-hotword-error">{hotwordError}</small>}
                </label>
                  <button
                    disabled={destructiveBlocked || Boolean(hotwordError)}
                    onClick={() =>
                      void run(
                        requestRetranscription,
                        "重新转写任务已排队",
                      )
                    }
                    type="button"
                  >
                    重新转写
                  </button>
                  {engine === "funasr" && (
                    <button disabled={isSaving} onClick={() => setEditingTranscript((value) => !value)} type="button">{editingTranscript ? "退出编辑" : "编辑逐字稿"}</button>
                  )}
                  {engine === "funasr" && editingTranscript && <button className="primary-button" disabled={busy || !transcriptDirty} onClick={() => void saveTranscript()} type="button">保存草稿</button>}
              </div>
            )}
            {engine === "funasr" ? (
              <TranscriptPanel
                currentTimeMs={currentMs}
                disabled={isSaving}
                editable={!isMobile && editingTranscript}
                onChange={changeSegments}
                onMerge={merge}
                onSeek={(milliseconds) => playerRef.current?.seekTo(milliseconds)}
                onSplit={split}
                segments={segments}
              />
            ) : candidateState === "loading" ? (
              <div className="comparison-empty" role="status">
                <span>{engine === "qwen" ? "Q" : "W"}</span>
                <div><h2>正在读取 {engine === "qwen" ? "Qwen" : "Whisper"} 对照稿…</h2><p>正在加载最新的独立对照版本 v{candidateVersion?.version_no}。</p></div>
              </div>
            ) : candidateState === "error" ? (
              <div className="comparison-empty comparison-empty--error" role="alert">
                <span>!</span>
                <div><h2>对照稿读取失败</h2><p>{candidateError}</p></div>
              </div>
            ) : candidateState === "ready" && comparison ? (
              <TranscriptComparisonPanel
                candidateLabel={engine === "qwen" ? "Qwen 影子稿" : "Whisper 对照稿"}
                currentTimeMs={currentMs}
                goldSamples={goldSamples}
                goldState={goldState}
                isMobile={isMobile}
                items={comparison.items}
                onGoldDirtyChange={changeGoldDirty}
                onRetryGold={() => void loadGoldSamples()}
                onSaveGold={saveGoldSample}
                onSeek={(milliseconds) => playerRef.current?.seekTo(milliseconds)}
              />
            ) : candidateState === "ready" ? (
                <TranscriptPanel currentTimeMs={currentMs} editable={false} onSeek={(milliseconds) => playerRef.current?.seekTo(milliseconds)} segments={candidateSegments} />
            ) : (
              <div className="comparison-empty">
                <span>{engine === "qwen" ? "Q" : "W"}</span>
                <div>
                  <h2>{candidateVersion ? `${engine === "qwen" ? "Qwen" : "Whisper"} v${candidateVersion.version_no} 暂无段落` : `暂无 ${engine === "qwen" ? "Qwen" : "Whisper"} 对照稿`}</h2>
                  <p>{candidateVersion ? "该版本存在，但后端没有返回可显示段落。" : "对照转写在独立子状态完成后会出现在这里，不阻塞主稿与纪要。"}</p>
                </div>
              </div>
            )}
          </section>

          {!isMobile && (
            <aside className="edit-inspector desktop-only">
              <div className="inspector-section classification-inspector">
                <span className="eyebrow">CLASSIFICATION</span>
                <h2>归档归属</h2>
                <label>
                  <span>
                    主项目{" "}
                    {liveProject.origin === "ai" && liveProject.id && (
                      <em className="project-card__badge project-card__badge--new">AI 归属</em>
                    )}
                  </span>
                  <select aria-label="主项目" disabled={isSaving} onChange={(event) => changeProject(event.target.value)} value={selectedProjectId}>
                    <option value="">{emptyProjectLabel}</option>
                    {!baselineProjectId && attributionStateNow && attributionStateNow !== "manual_none" && (
                      <option value={MARK_NO_PROJECT}>不归项目</option>
                    )}
                    {attributionStateNow === "manual_none" && <option value={RETURN_TO_AI}>交给 AI 判断</option>}
                    {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                  </select>
                </label>
                {liveProject.origin === "ai" && liveProject.id && (
                  <p className="muted">由会议纪要自动匹配；改选项目后以你选的为准</p>
                )}
                <MeetingRequirementPicker
                  apiClient={apiClient}
                  disabled={isSaving}
                  onChange={changeRequirements}
                  onOpenRequirement={onOpenRequirement}
                  onProjectPicked={changeProject}
                  projectId={effectiveProjectId}
                  projects={projects}
                  selected={selectedRequirementRefs}
                />
                <fieldset className="tag-checklist" disabled={isSaving}>
                  <legend>标签</legend>
                  {tags.length === 0 ? <span className="muted">暂无标签，请先在项目页创建。</span> : tags.map((tag) => (
                    <label key={tag.id}>
                      <input
                        checked={selectedTagIds.includes(tag.id)}
                        onChange={(event) => changeTags(event.target.checked ? [...selectedTagIds, tag.id] : selectedTagIds.filter((id) => id !== tag.id))}
                        type="checkbox"
                      />
                      <i style={{ background: tag.color }} />
                      {tag.name}
                    </label>
                  ))}
                </fieldset>
                <button
                  disabled={busy || transcriptDirty || minutesDirty || goldDirty || !classificationDirty}
                  onClick={() => void saveClassification()}
                  type="button"
                >
                  保存归档归属
                </button>
              </div>
              {meeting.speakers.length > 0 && (
                <div className="inspector-section">
                  <span className="eyebrow">SPEAKER TURNS</span>
                  <h2>标记说话人姓名</h2>
                  <p className="speaker-scope-note">这里只标记同一场录音内的说话人，不会跨会议识别具体身份。</p>
                  <p className="speaker-scope-note">切块转写的会议里，带「片段N」前缀的说话人跨片段可能是同一人，也可能不是；改名只对当前标签下的段落生效。</p>
                  <select disabled={isSaving} onChange={(event) => setSpeakerLabel(event.target.value)} value={speakerLabel}>
                    <option value="">选择说话人</option>
                    {meeting.speakers.map((speaker) => <option key={speaker.id} value={speaker.label}>{speaker.display_name || speaker.label}</option>)}
                  </select>
                  <input disabled={isSaving} onChange={(event) => setSpeakerName(event.target.value)} placeholder="新的显示名称" value={speakerName} />
                  <button
                    disabled={!speakerLabel || !speakerName.trim() || destructiveBlocked}
                    onClick={() =>
                      void run(
                        () => apiClient.renameSpeaker(meeting.id, speakerLabel, speakerName.trim()),
                        ({ updated }) => (updated > 0 ? `已更新 ${updated} 段说话人标注` : "没有匹配的段落，未做任何更新"),
                      )
                    }
                    type="button"
                  >
                    应用到全部段落
                  </button>
                </div>
              )}
              <div className="inspector-section">
                <span className="eyebrow">VERSIONS</span>
                <h2>逐字稿版本</h2>
                <select disabled={isSaving} onChange={(event) => setTranscriptVersion(event.target.value)} value={transcriptVersion}>
                  {meeting.transcript_versions.map((version) => (
                    <option key={version.id} value={version.id}>v{version.version_no} · {versionKindLabel(version.kind)}{version.published ? " · 已写回" : ""}</option>
                  ))}
                </select>
                <button
                  disabled={!transcriptVersion || transcriptVersion === meeting.current_transcript_version_id || destructiveBlocked}
                  onClick={() => void run(() => apiClient.rollbackTranscript(meeting.id, transcriptVersion), "已回滚逐字稿工作版本")}
                  type="button"
                >
                  回滚到所选版本
                </button>
              </div>
            </aside>
          )}
        </div>
      ) : tab === "minutes" ? (
        <div className="minutes-workspace">
          <article className="minutes-document">
            <div className="document-head">
              <div><span className="eyebrow">MEETING NOTES</span><h2>会议纪要</h2></div>
              {!isMobile && (
                <button className="desktop-only" disabled={isSaving} onClick={() => setEditingMinutes((value) => !value)} type="button">{editingMinutes ? "预览" : "编辑纪要"}</button>
              )}
            </div>
            {!isMobile && editingMinutes ? (
              <textarea aria-label="会议纪要编辑器" className="minutes-editor" disabled={isSaving} onChange={(event) => changeMinutes(event.target.value)} value={minutes} />
            ) : !currentMinutes ? (
              <div className="comparison-empty"><span>∅</span><div><h2>尚无会议纪要</h2><p>桌面端可新建第一版纪要，或请求后台重新生成。</p></div></div>
            ) : (
              <div className="markdown-safe"><SafeMarkdown>{minutes}</SafeMarkdown></div>
            )}
            {corrections.length > 0 && (
              <MinutesCorrectionsBar
                apiClient={apiClient}
                corrections={corrections}
                key={corrections.map((item) => item.id).join(",")}
                onChanged={onGlossaryChanged}
                onClose={() => setCorrections([])}
                projects={projects}
              />
            )}
            <MinutesEvidencePanel
              evidence={evidenceVersionId === currentMinutesVersionId ? evidence : null}
              message={evidenceMessage}
              onSeek={(milliseconds) => playerRef.current?.seekTo(milliseconds)}
              state={typeof apiClient.minutesEvidence === "function"
                ? evidenceVersionId === currentMinutesVersionId ? evidenceState : "loading"
                : "missing"}
            />
          </article>
          {!isMobile && (
            <aside className="minutes-actions desktop-only">
              <span className="eyebrow">WRITE BACK</span>
              <h2>编辑与写回</h2>
              <p>在这里改的内容先存在工作台里。写回之后，会议文件夹里的纪要和逐字稿才会同步更新，原音频永不改写。</p>
              {editingMinutes && <button disabled={busy || !minutesDirty} onClick={() => void saveMinutes()} type="button">保存纪要草稿</button>}
              <div className="regen-group">
                <button disabled={destructiveBlocked} onClick={() => void run(() => apiClient.regenerateMinutes(meeting.id), "纪要重生成任务已排队") } type="button">重新生成纪要</button>
                <button
                  className="regen-claude"
                  disabled={destructiveBlocked}
                  onClick={() => void run(() => apiClient.regenerateMinutes(meeting.id, "claude"), "已排队用 Claude 重写纪要")}
                  title="改用 Claude 重跑一版纪要，完成后覆盖当前纪要（旧版本仍可回滚）"
                  type="button"
                >用 Claude 重写</button>
              </div>
              <p className="regen-hint">纪要默认由 DeepSeek 生成。写得不到位时用 Claude 重写一版，完成后覆盖当前纪要，旧版本仍留在下面的历史里可回滚。</p>
              <select disabled={isSaving} onChange={(event) => setMinutesVersion(event.target.value)} value={minutesVersion}>
                <option value="">选择历史版本</option>
                {meeting.minutes_versions.map((version) => <option key={version.id} value={version.id}>v{version.version_no} · {versionKindLabel(version.kind)}{version.published ? " · 已写回" : ""}</option>)}
              </select>
              <button disabled={!minutesVersion || minutesVersion === meeting.current_minutes_version_id || destructiveBlocked} onClick={() => void run(() => apiClient.rollbackMinutes(meeting.id, minutesVersion), "已回滚纪要工作版本")} type="button">回滚纪要版本</button>
              <div className="publish-rule" />
              <button className="publish-button" disabled={destructiveBlocked} onClick={() => void confirmThen(
                {
                  title: "写回会议文件夹？",
                  message: "用工作台里的当前版本更新归档目录里的逐字稿和纪要文件，原音频不动。",
                  confirmLabel: "写回",
                },
                () => run(() => apiClient.publish(meeting.id), "已写回会议文件夹"),
              )} type="button">写回会议文件夹</button>
            </aside>
          )}
        </div>
      ) : (
        <div className="tasks-workspace">
          <MeetingTasksPanel
            apiClient={apiClient}
            canWrite={canWriteTasks}
            meetingId={meeting.id}
            meetingTitle={isUntitled(meeting.title, meeting.id) ? meeting.id : meeting.title}
            onChanged={() => {
              void loadPendingTaskCount();
              onTasksChanged?.();
            }}
            onOpenTasks={onOpenTasks!}
            projects={projects}
          />
        </div>
      )}
    </section>
  );
}
