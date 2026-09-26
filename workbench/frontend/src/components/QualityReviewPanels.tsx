import { useEffect, useMemo, useState } from "react";

import { formatTime } from "../format";
import type {
  AsrGoldSample,
  MinutesEvidence,
  TranscriptComparisonItem,
  TranscriptRiskKind,
} from "../types";
import { NoticeBanner, useNotice } from "./Notice";

const riskLabels: Record<TranscriptRiskKind, string> = {
  missing_candidate: "候选缺失",
  latin_term: "英文术语差异",
  number: "数字差异",
  text: "文本差异",
};

interface TranscriptComparisonPanelProps {
  candidateLabel: string;
  currentTimeMs: number;
  goldSamples: AsrGoldSample[];
  goldState?: "loading" | "ready" | "error";
  isMobile: boolean;
  items: TranscriptComparisonItem[];
  onGoldDirtyChange?: (dirty: boolean) => void;
  onRetryGold?: () => void;
  onSaveGold: (segmentId: string, reference: string) => Promise<AsrGoldSample>;
  onSeek: (milliseconds: number) => void;
}

export function TranscriptComparisonPanel({
  candidateLabel,
  currentTimeMs,
  goldSamples,
  goldState = "ready",
  isMobile,
  items,
  onGoldDirtyChange,
  onRetryGold,
  onSaveGold,
  onSeek,
}: TranscriptComparisonPanelProps) {
  const [samples, setSamples] = useState(goldSamples);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [reference, setReference] = useState("");
  const [saving, setSaving] = useState(false);
  const { notice: message, setNotice: setMessage, dismissNotice: dismissMessage } = useNotice();
  useEffect(() => setSamples(goldSamples), [goldSamples]);
  useEffect(() => {
    onGoldDirtyChange?.(editingId !== null);
  }, [editingId, onGoldDirtyChange]);
  const samplesBySegment = useMemo(
    () => new Map(samples.filter((sample) => sample.segment_id).map((sample) => [sample.segment_id as string, sample])),
    [samples],
  );

  const openGoldEditor = (item: TranscriptComparisonItem) => {
    if (goldState !== "ready" || saving) return;
    if (editingId === item.primary_segment_id) return;
    if (
      editingId &&
      editingId !== item.primary_segment_id &&
      !window.confirm("当前金标尚未保存。放弃修改并编辑另一段吗？")
    ) {
      setMessage("已保留当前未保存金标", "warning");
      return;
    }
    setEditingId(item.primary_segment_id);
    setReference(samplesBySegment.get(item.primary_segment_id)?.reference ?? item.primary_text);
    setMessage("");
  };
  const saveGold = async () => {
    if (goldState !== "ready" || !editingId || !reference.trim()) return;
    const requestEditingId = editingId;
    const requestReference = reference.trim();
    setSaving(true);
    setMessage("");
    try {
      const saved = await onSaveGold(requestEditingId, requestReference);
      setSamples((current) => [
        ...current.filter((sample) => sample.segment_id !== requestEditingId),
        saved,
      ]);
      setEditingId((current) => current === requestEditingId ? null : current);
      setMessage("金标已保存");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "金标保存失败", "error");
    } finally {
      setSaving(false);
    }
  };
  const activateRow = (item: TranscriptComparisonItem) => onSeek(item.start_ms);

  return (
    <section className="comparison-panel" aria-label={`${candidateLabel}逐段对照`}>
      <header className="comparison-panel__head">
        <div><span className="eyebrow">ASR REVIEW</span><strong>逐段证据对照</strong></div>
        <p>按时间重叠对齐；风险提示只标明差异类型，不伪造字词级精确高亮。</p>
      </header>
      {goldState === "error" && (
        <div className="gold-ledger-state gold-ledger-state--error" role="alert">
          <span>金标读取失败，当前已禁止新增或覆盖金标。</span>
          <button onClick={onRetryGold} type="button">重试读取金标</button>
        </div>
      )}
      <NoticeBanner className="comparison-message" notice={message} onDismiss={dismissMessage} />
      <div className="comparison-columns" aria-hidden="true">
        <span>时间 / 风险</span><span>FunASR 主稿</span><span>{candidateLabel}</span>
      </div>
      <div className="comparison-list">
        {items.map((item, index) => {
          const sample = samplesBySegment.get(item.primary_segment_id);
          const current = currentTimeMs >= item.start_ms && currentTimeMs < item.end_ms;
          return (
            <div
              aria-label={`对照第 ${index + 1} 段`}
              className={`comparison-row ${current ? "is-current" : ""} ${item.risk_kinds.length ? "has-risk" : ""}`}
              key={item.primary_segment_id}
              onClick={(event) => {
                if (!(event.target as HTMLElement).closest("button,input,textarea")) activateRow(item);
              }}
            >
              <div className="comparison-row__meta">
                <button aria-label={`对照第 ${index + 1} 段，跳转到 ${formatTime(item.start_ms)}`} onClick={() => activateRow(item)} type="button">
                  {formatTime(item.start_ms)}
                </button>
                <span>{Math.round(item.similarity * 100)}% 相似</span>
                <div className="risk-badges">
                  {item.risk_kinds.length
                    ? item.risk_kinds.map((risk) => <em className={`risk-badge risk-badge--${risk}`} key={risk}>{riskLabels[risk]}</em>)
                    : <em className="risk-badge risk-badge--clear">未见结构性风险</em>}
                </div>
              </div>
              <div className="comparison-copy comparison-copy--primary">
                <p>{item.primary_text}</p>
                {!isMobile && (
                  <button className="gold-trigger" disabled={saving || goldState !== "ready" || editingId === item.primary_segment_id} onClick={() => openGoldEditor(item)} type="button">
                    {goldState === "loading" ? "金标加载中" : sample ? "更新金标" : "标为金标"}
                  </button>
                )}
              </div>
              <div className={`comparison-copy comparison-copy--candidate ${item.candidate_text ? "" : "is-missing"}`}>
                <p>{item.candidate_text || "该时间段没有候选内容"}</p>
              </div>
              {!isMobile && editingId === item.primary_segment_id && (
                <div className="gold-inline-editor" onClick={(event) => event.stopPropagation()}>
                  <label>
                    <span>人工金标</span>
                    <textarea aria-label={`${item.primary_segment_id} 金标文本`} disabled={saving || goldState !== "ready"} onChange={(event) => setReference(event.target.value)} rows={3} value={reference} />
                  </label>
                  <div>
                    <button disabled={saving || goldState !== "ready" || !reference.trim()} onClick={() => void saveGold()} type="button">确认保存金标</button>
                    <button disabled={saving} onClick={() => setEditingId(null)} type="button">取消</button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

type EvidenceState = "loading" | "ready" | "missing" | "unavailable";

interface MinutesEvidencePanelProps {
  evidence: MinutesEvidence | null;
  message?: string;
  onSeek: (milliseconds: number) => void;
  state: EvidenceState;
}

export function MinutesEvidencePanel({ evidence, message, onSeek, state }: MinutesEvidencePanelProps) {
  if (state === "loading") return <section className="evidence-panel" aria-label="纪要证据"><p>正在核验纪要证据…</p></section>;
  if (state === "missing") return <section className="evidence-panel evidence-panel--empty" aria-label="纪要证据"><h3>该会议暂无可验证证据</h3><p>旧版纪要或尚未生成 v3 证据的会议会显示此状态。</p></section>;
  if (state === "unavailable") return <section className="evidence-panel evidence-panel--warning" aria-label="纪要证据"><h3>证据暂不可验证</h3><p>{message || "当前纪要与来源证据未能完成一致性复验。"}</p></section>;
  if (!evidence) return null;
  return (
    <section className="evidence-panel" aria-label="纪要证据">
      <header>
        <div><span className="eyebrow">EVIDENCE COVERAGE</span><h3>纪要证据账本</h3></div>
        <div className="evidence-metrics">
          <strong>{evidence.coverage.total_items} 条证据</strong>
          <span>已写入 {evidence.coverage.included_items}</span>
          <span>明确省略 {evidence.coverage.omitted_items}</span>
        </div>
      </header>
      <div className="evidence-topics">
        {evidence.topics.map((topic) => (
          <section key={topic.topic_id}>
            <div className="evidence-topic-title"><span>{topic.topic_id}</span><h4>{topic.title}</h4></div>
            <ul>
              {topic.items.map((item) => (
                <li className={`evidence-item evidence-item--${item.status}`} key={item.item_id}>
                  <button aria-label={`跳转到证据 ${formatTime(item.source_start_sec * 1000)}`} onClick={() => onSeek(item.source_start_sec * 1000)} type="button">
                    {formatTime(item.source_start_sec * 1000)}
                  </button>
                  <div>
                    <div><em>{item.status === "included" ? "已写入" : "明确省略"}</em><span>{item.kind}</span></div>
                    <p>{item.text}</p>
                    {item.omitted_reason && <small>{item.omitted_reason}</small>}
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </section>
  );
}
