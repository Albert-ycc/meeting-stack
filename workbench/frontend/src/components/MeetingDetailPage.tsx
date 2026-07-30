import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError, type ApiClient, type ConflictResolutionAction } from "../api";
import { formatDate, isDoneStatus, isUntitled, statusLabel, statusTone } from "../format";
import { parseHotwordsInput, validateHotwordsInput } from "../hotwords";
import type {
  AsrGoldSample,
  AsrShadowRun,
  LoadState,
  MeetingConflict,
  MeetingDetail,
  MinutesEvidence,
  Project,
  Segment,
  Tag,
  TranscriptComparisonPayload,
  TranscriptVersion,
} from "../types";
import { AudioPlayer, type AudioPlayerHandle } from "./AudioPlayer";
import { CopyFolderPathButton } from "./CopyFolderPathButton";
import { MinutesEvidencePanel, TranscriptComparisonPanel } from "./QualityReviewPanels";
import { TranscriptPanel } from "./TranscriptPanel";

interface MeetingDetailPageProps {
  apiClient: ApiClient;
  initialSeekMs: number;
  isMobile: boolean;
  meeting: MeetingDetail;
  onBack: () => void;
  onClassificationSaved?: () => Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
  onNavigationLockChange?: (locked: boolean) => void;
  onReload: () => Promise<void>;
  projects: Project[];
  tags: Tag[];
}

type DetailTab = "transcript" | "minutes";

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
  onBack,
  onClassificationSaved,
  onDirtyChange,
  onNavigationLockChange,
  onReload,
  projects,
  tags,
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
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [savingKind, setSavingKind] = useState<"transcript" | "minutes" | "classification" | null>(null);
  const [speakerLabel, setSpeakerLabel] = useState(meeting.speakers[0]?.label ?? "");
  const [speakerName, setSpeakerName] = useState("");
  const [selectedProjectId, setSelectedProjectId] = useState(meeting.project_id ?? "");
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>(meeting.tags.map((tag) => tag.id));
  const [baselineProjectId, setBaselineProjectId] = useState(meeting.project_id ?? "");
  const [baselineTagIds, setBaselineTagIds] = useState<string[]>(meeting.tags.map((tag) => tag.id));
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
  const classificationDirty =
    selectedProjectId !== baselineProjectId ||
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
    setBaselineTagIds(meeting.tags.map((tag) => tag.id));
    revisions.current = { transcript: 0, minutes: 0, classification: 0 };
    setEditingTranscript(false);
    setEditingMinutes(false);
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
  const run = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    setNotice("");
    try {
      await action();
      setNotice(success);
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
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
      setNotice(qwenNotice(result));
      if (result.state === "ready" && typeof apiClient.meeting === "function") {
        const sequence = ++qwenPollSequence.current;
        const snapshot = await apiClient.meeting(meeting.id);
        if (sequence === qwenPollSequence.current) {
          acceptQualitySnapshot(snapshot);
        }
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Qwen 影子稿操作失败");
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
  const saveTranscript = async () => {
    const snapshot = segments.map((segment) => ({ ...segment }));
    const requestRevision = revisions.current.transcript;
    setBusy(true);
    setSavingKind("transcript");
    setNotice("");
    try {
      const result = await apiClient.saveTranscript(
        meeting.id,
        snapshot,
        transcriptBaseVersionId,
      );
      setTranscriptBaseVersionId(result.version_id);
      setBaselineSegments(snapshot);
      if (revisions.current.transcript !== requestRevision) {
        setNotice("请求中的逐字稿已保存；请求发出后的本地修改仍保留，请再次保存");
        return;
      }
      setNotice("逐字稿已保存在工作台，尚未写回文件夹");
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setSavingKind(null);
      setBusy(false);
    }
  };
  const saveMinutes = async () => {
    const snapshot = minutes;
    const requestRevision = revisions.current.minutes;
    setBusy(true);
    setSavingKind("minutes");
    setNotice("");
    try {
      const result = await apiClient.saveMinutes(
        meeting.id,
        snapshot,
        minutesBaseVersionId,
      );
      setMinutesBaseVersionId(result.version_id);
      setBaselineMinutes(snapshot);
      if (revisions.current.minutes !== requestRevision) {
        setNotice("请求中的纪要已保存；请求发出后的本地修改仍保留，请再次保存");
        return;
      }
      setNotice("纪要草稿已保存");
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setSavingKind(null);
      setBusy(false);
    }
  };
  const saveClassification = async () => {
    const snapshotProjectId = selectedProjectId;
    const snapshotTagIds = [...selectedTagIds];
    const requestRevision = revisions.current.classification;
    setBusy(true);
    setSavingKind("classification");
    setNotice("");
    try {
      await apiClient.updateMeeting(meeting.id, {
        project_id: snapshotProjectId,
        tag_ids: snapshotTagIds,
      });
      setBaselineProjectId(snapshotProjectId);
      setBaselineTagIds(snapshotTagIds);
      let refreshWarning = "";
      try {
        await onClassificationSaved?.();
      } catch {
        refreshWarning = "；项目统计暂未刷新，请稍后重新进入项目页查看";
      }
      if (revisions.current.classification !== requestRevision) {
        setNotice(`请求中的归档归属已保存${refreshWarning}；后续本地修改仍保留，请再次保存`);
        return;
      }
      setNotice(`会议归档归属已保存${refreshWarning}`);
      await onReload();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
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
    setNotice("已在本地拆分；保存草稿后才会写入资料库");
  };
  const merge = (firstId: string, secondId: string) => {
    setSegments((current) => {
      const next = mergeSegmentsLocally(current, firstId, secondId);
      if (next !== current) revisions.current.transcript += 1;
      return next;
    });
    setNotice("已在本地合并；保存草稿后才会写入资料库");
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

  return (
    <section className={`detail-page ${isMobile ? "detail-page--mobile" : ""}`}>
      <header className="detail-header">
        <button className="back-button" disabled={isSaving} onClick={leaveDetail} type="button">← 返回资料库</button>
        <div className="detail-title-row">
          <div>
            <span className="archive-code">{meeting.id}</span>
            <h1 id={`detail-title-${meeting.id}`}>
              {meeting.title}
              {isUntitled(meeting.title, meeting.id) && <em className="untitled-chip">标题待生成</em>}
            </h1>
            <div className="detail-meta">
              <span>{formatDate(meeting.recording_date)}</span>
              {meeting.project_name && <span className="project-mark"><i style={{ background: meeting.project_color || "#65736f" }} />{meeting.project_name}</span>}
              {meeting.tags.map((tag) => <em key={tag.id}>{tag.name}</em>)}
            </div>
          </div>
          <div className="detail-state">
            <CopyFolderPathButton
              describedById={`detail-title-${meeting.id}`}
              path={meeting.canonical_dir}
              withLabel
            />
            {!isDoneStatus(meeting.status) && (
              <span className={`status-badge status-badge--${statusTone(meeting.status)}`}>
                {statusLabel(statusTone(meeting.status))}
              </span>
            )}
            {isMobile && <span className="read-only-chip">只读</span>}
          </div>
        </div>
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
                  onClick={() => void resolveConflict(conflict, "discard_draft")}
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

      {notice && <div className="action-banner" role="status">{notice}</div>}

      <div className="detail-tabs" role="tablist">
        <button aria-selected={tab === "transcript"} disabled={isSaving || goldDirty} onClick={() => setTab("transcript")} role="tab" type="button">逐字稿 <span>{segments.length}</span></button>
        <button aria-selected={tab === "minutes"} disabled={isSaving || goldDirty} onClick={() => setTab("minutes")} role="tab" type="button">会议纪要 <span>{meeting.minutes_versions.length}</span></button>
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
                  <span>主项目</span>
                  <select aria-label="主项目" disabled={isSaving} onChange={(event) => changeProject(event.target.value)} value={selectedProjectId}>
                    <option value="">未归项目</option>
                    {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                  </select>
                </label>
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
                  <select disabled={isSaving} onChange={(event) => setSpeakerLabel(event.target.value)} value={speakerLabel}>
                    <option value="">选择说话人</option>
                    {meeting.speakers.map((speaker) => <option key={speaker.id} value={speaker.label}>{speaker.display_name || speaker.label}</option>)}
                  </select>
                  <input disabled={isSaving} onChange={(event) => setSpeakerName(event.target.value)} placeholder="新的显示名称" value={speakerName} />
                  <button
                    disabled={!speakerLabel || !speakerName.trim() || destructiveBlocked}
                    onClick={() => void run(() => apiClient.renameSpeaker(meeting.id, speakerLabel, speakerName.trim()), "说话人已批量更新")}
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
                    <option key={version.id} value={version.id}>v{version.version_no} · {version.kind}{version.published ? " · 已写回" : ""}</option>
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
      ) : (
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
              <button disabled={destructiveBlocked} onClick={() => void run(() => apiClient.regenerateMinutes(meeting.id), "纪要重生成任务已排队") } type="button">重新生成纪要</button>
              <select disabled={isSaving} onChange={(event) => setMinutesVersion(event.target.value)} value={minutesVersion}>
                <option value="">选择历史版本</option>
                {meeting.minutes_versions.map((version) => <option key={version.id} value={version.id}>v{version.version_no} · {version.kind}{version.published ? " · 已写回" : ""}</option>)}
              </select>
              <button disabled={!minutesVersion || minutesVersion === meeting.current_minutes_version_id || destructiveBlocked} onClick={() => void run(() => apiClient.rollbackMinutes(meeting.id, minutesVersion), "已回滚纪要工作版本")} type="button">回滚纪要版本</button>
              <div className="publish-rule" />
              <button className="publish-button" disabled={destructiveBlocked} onClick={() => void run(() => apiClient.publish(meeting.id), "已写回会议文件夹")} type="button">写回会议文件夹</button>
            </aside>
          )}
        </div>
      )}
    </section>
  );
}
