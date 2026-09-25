import { useRef, useState } from "react";

import type { Job, JobSubstateName, JobSubstateStatus, LoadState } from "../types";
import { formatDate, statusLabel, statusTone, failureStageLabel } from "../format";
import { parseHotwordsInput, validateHotwordsInput } from "../hotwords";
import { AsyncState } from "./AsyncState";

interface JobsPageProps {
  available: boolean;
  jobs: Job[];
  message?: string;
  onCancel: (jobId: string) => Promise<void>;
  onRetry: (jobId: string, stage: string, hotwords?: string[]) => Promise<void>;
  onRetrySubstate: (jobId: string, name: JobSubstateName) => Promise<void>;
  onStopAfterStage: (jobId: string) => Promise<void>;
  onUpload: (file: File, hotwords?: string[]) => Promise<string>;
  stale?: boolean;
  state: LoadState;
}

// 校对与发布不再作为流水线阶段：纪要生成完就是终点。
const pipeline = [
  "discovered",
  "stabilizing",
  "queued",
  "transcribing",
  "transcript_ready",
  "minutes_generating",
  "done",
];

const substateLabels: Record<JobSubstateStatus, string> = {
  pending: "待处理",
  queued: "已排队",
  running: "处理中",
  ready: "已就绪",
  failed: "失败",
  unavailable: "不可用",
  absent: "尚未创建",
};

export function JobsPage({
  available,
  jobs,
  message,
  onCancel,
  onRetry,
  onRetrySubstate,
  onStopAfterStage,
  onUpload,
  stale = false,
  state,
}: JobsPageProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [actionMessage, setActionMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploadHotwordText, setUploadHotwordText] = useState("");
  const [retryHotwordText, setRetryHotwordText] = useState<Record<string, string>>({});
  const uploadHotwordError = validateHotwordsInput(uploadHotwordText);

  const execute = async (action: () => Promise<void>) => {
    setBusy(true);
    setActionMessage("");
    try {
      await action();
      setActionMessage("操作已由后端确认");
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const upload = async (file: File) => {
    const requestHotwordText = uploadHotwordText;
    setBusy(true);
    try {
      const result = await onUpload(file, parseHotwordsInput(requestHotwordText));
      setUploadHotwordText((current) => current === requestHotwordText ? "" : current);
      setActionMessage(result);
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : "上传失败");
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <section className="jobs-page page-content desktop-only">
      <header className="page-heading">
        <div>
          <span className="eyebrow">RELAY CONTROL / 任务</span>
          <h1>录音流水线</h1>
          <p>控制只在阶段边界生效；运行中的主转写不会被强杀。</p>
        </div>
        <div className="job-import-control">
          <label>
            <span>本场热词（可选）</span>
            <textarea
              aria-describedby={uploadHotwordError ? "upload-hotword-error" : undefined}
              aria-label="手工导入本场热词"
              disabled={busy}
              onChange={(event) => setUploadHotwordText(event.target.value)}
              placeholder="逗号或换行分隔"
              rows={2}
              value={uploadHotwordText}
            />
          </label>
          {uploadHotwordError && <small className="field-error" id="upload-hotword-error">{uploadHotwordError}</small>}
          <input
            accept=".m4a,.mp3,.wav"
            aria-label="选择录音文件"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void upload(file);
            }}
            ref={fileRef}
            type="file"
          />
          <button className="primary-button" disabled={busy || Boolean(uploadHotwordError)} onClick={() => fileRef.current?.click()} type="button">
            ＋ 手工导入录音
          </button>
        </div>
      </header>

      {actionMessage && <div className="action-banner" role="status">{actionMessage}</div>}
      {stale && jobs.length > 0 && (
        <div className="action-banner action-banner--warning" role="status">
          显示上次成功读取的任务，当前状态可能已过期。{message ? ` ${message}` : ""}
        </div>
      )}
      {!available ? (
        <div className="interface-guard" role="alert">
          <span className="state-mark">!</span>
          <div>
            <h2>任务控制接口尚未接入</h2>
            <p>{message || "后端没有返回 /api/jobs。资料库和播放器仍可使用，但这里不会伪造队列状态或成功回执。"}</p>
          </div>
        </div>
      ) : state === "loading" ? (
        <AsyncState state="loading" />
      ) : state === "error" && jobs.length === 0 ? (
        <AsyncState message={message || "任务台账读取失败"} state="error" />
      ) : jobs.length === 0 ? (
        <AsyncState message="当前没有任务；新录音会从“已发现”开始进入状态链。" state="empty" />
      ) : (
        <div className="job-stack">
          {jobs.map((job) => {
            const currentIndex = pipeline.indexOf(statusTone(job.state));
            const canCancel = ["discovered", "stabilizing", "queued"].includes(job.state);
            const canRetry = ["failed", "cancelled", "interrupted", "completed_unreviewed"].includes(job.state);
            const terminal = [
              "completed_unreviewed",
              "draft_modified",
              "published",
              "cancelled",
              "failed",
              "interrupted",
            ].includes(job.state);
            const substates = [
              {
                name: "whisper" as const,
                title: "Whisper 对照稿",
                retryText: "重试 Whisper 对照稿",
                description: "独立生成的对照稿，不阻塞 FunASR 主稿与纪要。",
                status: job.substates?.whisper?.status ?? job.whisper_status ?? "absent",
                error: job.substates?.whisper?.error,
              },
              {
                name: "index" as const,
                title: "检索索引",
                retryText: "重试检索索引",
                description: "独立构建全文与语义索引，不改变主链完成状态。",
                status: job.substates?.index?.status ?? job.index_status ?? "absent",
                error: job.substates?.index?.error,
              },
              {
                name: "qwen" as const,
                title: "离线 Qwen 影子稿",
                retryText: "请在会议详情重试 Qwen",
                description: "本机离线生成的质量对照，不进入也不阻塞主流水线。",
                status: job.substates?.qwen?.status ?? "absent",
                error: job.substates?.qwen?.error,
              },
            ];
            return (
              <article className="job-card" key={job.id}>
                <div className="job-card__head">
                  <div>
                    <span className="archive-code">{job.id}</span>
                    <h2>{job.meeting_title || job.meeting_id || "等待关联会议"}</h2>
                    <small>{formatDate(job.created_at)}</small>
                  </div>
                  {job.failure_stage === "pending_archive" ? (
                    // relay 把这类任务记成已完成，但纪要没放进会议文件夹，不能挂绿色「已完成」
                    <span className="status-badge status-badge--failed">归档未完成</span>
                  ) : (
                    <span className={`status-badge status-badge--${statusTone(job.state)}`}>
                      {statusLabel(statusTone(job.state))}
                    </span>
                  )}
                </div>
                <ol className="stage-track" aria-label="处理阶段">
                  {pipeline.map((stage, index) => (
                    <li
                      className={index < currentIndex ? "is-done" : index === currentIndex ? "is-current" : ""}
                      key={stage}
                      title={statusLabel(stage)}
                    >
                      <span />
                      <small>{statusLabel(stage)}</small>
                    </li>
                  ))}
                </ol>
                {job.failure_reason && (
                  <div className="failure-reason">
                    <strong>{job.failure_stage ? `${failureStageLabel(job.failure_stage)}这一步：` : ""}</strong>
                    {job.failure_reason}
                  </div>
                )}
                <div className="job-substates" aria-label="非阻塞子状态">
                  {substates.map((substate) => (
                    <section className="job-substate" key={substate.name}>
                      <div className="job-substate__head">
                        <strong>{substate.title}</strong>
                        <span className={`substate-badge substate-badge--${substate.status}`}>
                          {substateLabels[substate.status]}
                        </span>
                      </div>
                      <p>{substate.description}</p>
                      {substate.status === "failed" && substate.error && (
                        <small className="job-substate__error">{substate.error}</small>
                      )}
                      {substate.status === "failed" && substate.name !== "qwen" && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void execute(() => onRetrySubstate(job.id, substate.name))
                          }
                          type="button"
                        >
                          {substate.retryText}
                        </button>
                      )}
                    </section>
                  ))}
                </div>
                <div className="job-actions">
                  {canRetry && (
                    <label className="job-retry-hotwords">
                      <span>重试热词（可选）</span>
                      <textarea
                        aria-label={`${job.id} 重试热词`}
                        onChange={(event) => setRetryHotwordText((current) => ({ ...current, [job.id]: event.target.value }))}
                        placeholder="本场术语"
                        rows={1}
                        value={retryHotwordText[job.id] ?? ""}
                      />
                      {validateHotwordsInput(retryHotwordText[job.id] ?? "") && (
                        <small className="field-error">{validateHotwordsInput(retryHotwordText[job.id] ?? "")}</small>
                      )}
                    </label>
                  )}
                  {job.state === "failed" && (
                    <button disabled={busy || Boolean(validateHotwordsInput(retryHotwordText[job.id] ?? ""))} onClick={() => void execute(() => onRetry(job.id, job.failure_stage || "transcribing", parseHotwordsInput(retryHotwordText[job.id] ?? "")))} type="button">
                      从失败阶段重试
                    </button>
                  )}
                  {canRetry && (
                    <button disabled={busy || Boolean(validateHotwordsInput(retryHotwordText[job.id] ?? ""))} onClick={() => void execute(() => onRetry(job.id, "transcribing", parseHotwordsInput(retryHotwordText[job.id] ?? "")))} type="button">
                      重新转写
                    </button>
                  )}
                  {canRetry && (
                    <button disabled={busy} onClick={() => void execute(() => onRetry(job.id, "minutes_generating"))} type="button">
                      重新生成纪要
                    </button>
                  )}
                  {canCancel && (
                    <button disabled={busy} onClick={() => void execute(() => onCancel(job.id))} type="button">取消排队</button>
                  )}
                  {!canCancel && !terminal && (
                    <button disabled={busy || Boolean(job.stop_after_stage)} onClick={() => void execute(() => onStopAfterStage(job.id))} type="button">
                      {job.stop_after_stage ? "已请求阶段后停止" : "当前阶段结束后停止"}
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
