import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AsyncState } from "./AsyncState";
import { useConfirm } from "./ConfirmDialog";
import { GlossaryTermModal } from "./GlossaryTermModal";
import { formatDate } from "../format";
import type { ApiClient } from "../api";
import type {
  GlossaryScope,
  GlossarySuggestion,
  GlossaryTerm,
  LoadState,
  MeetingSummary,
  Project,
} from "../types";

import { LegacyGroupsNote } from "./LegacyGroupsNote";
import "./GlossaryPage.css";
import { NoticeBanner, useNotice } from "./Notice";
import { usePersistentState } from "../viewState";

type SuggestionStatus = "pending" | "confirmed" | "rejected";
type TabKey = "terms" | "suggestions";

interface GlossaryPageProps {
  apiClient: ApiClient;
  canWrite: boolean;
  meetings: MeetingSummary[];
  projects: Project[];
  /** 从项目详情页「在词典中查看」跳转过来时预选中的项目 chip；只在首次挂载生效一次。 */
  initialProjectId?: string | null;
  onPendingChange?: () => void;
}

const SUGGESTION_TABS: Array<{ key: SuggestionStatus; label: string }> = [
  { key: "pending", label: "待确认" },
  { key: "confirmed", label: "已确认" },
  { key: "rejected", label: "已驳回" },
];

const SUGGESTION_STATUS_LABEL: Record<SuggestionStatus, string> = {
  pending: "待确认",
  confirmed: "已确认",
  rejected: "已驳回",
};

const ALL_KEY = "all";

/** 来源会议只显示标题；会议不在当前列表页时退成档案号前段，仍可辨识。 */
function meetingLabel(meetingId: string | null, meetings: MeetingSummary[]): string {
  if (!meetingId) return "编辑纪要时捕获";
  const meeting = meetings.find((item) => item.id === meetingId);
  return meeting?.title ?? `会议 ${meetingId.slice(0, 8)}…`;
}

function chipKey(chip: Pick<GlossaryScope, "kind" | "key">): string {
  return `${chip.kind}:${chip.key}`;
}

/** 合并本地/远端 chip 列表时的去重身份：只有一个「通用」分组，
 * 后端 key 是「通用」这个 label 本身，本地兜底算的是常量 "general"，两边字面不相等，
 * 不按 kind 归一就会在「全部」视图里裂出两个通用分组、且各自算出一半计数。 */
function chipIdentity(chip: Pick<GlossaryScope, "kind" | "key">): string {
  return chip.kind === "general" ? "general" : chipKey(chip);
}

/** 术语归到哪个 chip：project_id 优先；否则按 scope 字符串落「通用」或某个自定义桶。 */
function matchesChip(term: GlossaryTerm, chip: GlossaryScope): boolean {
  if (chip.kind === "project") return term.project_id === chip.key;
  if (term.project_id) return false;
  if (chip.kind === "general") return !term.scope || term.scope === "通用";
  return term.scope === chip.key;
}

/** 后端 /api/glossary/scopes 还没上线，或返回为空时的本地兜底：从已加载的术语里现算分组。 */
function deriveLocalScopes(terms: GlossaryTerm[]): GlossaryScope[] {
  const projectMap = new Map<string, { label: string; color: string | null; count: number }>();
  const bucketMap = new Map<string, number>();
  let generalCount = 0;
  terms.forEach((term) => {
    if (term.project_id) {
      const entry = projectMap.get(term.project_id) ?? {
        label: term.project_name ?? term.scope ?? "项目",
        color: term.project_color ?? null,
        count: 0,
      };
      entry.count += 1;
      projectMap.set(term.project_id, entry);
    } else if (term.scope && term.scope !== "通用") {
      bucketMap.set(term.scope, (bucketMap.get(term.scope) ?? 0) + 1);
    } else {
      generalCount += 1;
    }
  });
  const chips: GlossaryScope[] = [
    { kind: "general", key: "general", label: "通用", color: null, count: generalCount },
  ];
  [...projectMap.entries()]
    .sort((left, right) => left[1].label.localeCompare(right[1].label, "zh-CN"))
    .forEach(([id, entry]) =>
      chips.push({ kind: "project", key: id, label: entry.label, color: entry.color, count: entry.count }),
    );
  [...bucketMap.entries()]
    .sort((left, right) => left[0].localeCompare(right[0], "zh-CN"))
    .forEach(([name, count]) => chips.push({ kind: "bucket", key: name, label: name, color: null, count }));
  return chips;
}

/** 术语/别名子串匹配，不区分大小写；空搜索词永远命中。 */
function matchesSearch(term: GlossaryTerm, needle: string): boolean {
  if (!needle) return true;
  if (term.term.toLowerCase().includes(needle)) return true;
  return term.aliases.some((alias) => alias.toLowerCase().includes(needle));
}

export function GlossaryPage({
  apiClient,
  canWrite,
  meetings,
  projects,
  initialProjectId,
  onPendingChange,
}: GlossaryPageProps) {
  const [activeTab, setActiveTab] = usePersistentState<TabKey>("glossary.activeTab", "terms");
  const { notice, setNotice, dismissNotice } = useNotice();
  const [busy, setBusy] = useState(false);
  const [confirm, confirmDialog] = useConfirm();
  const busyRef = useRef(false);
  // 术语与建议各自独立计数：共用同一个 counter 会互相覆盖，先启动的请求被误判为过期丢弃。
  const termsSeqRef = useRef(0);
  const suggestionsSeqRef = useRef(0);

  // —— 术语库 ——
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  const [termsState, setTermsState] = useState<LoadState>("loading");
  const [remoteScopes, setRemoteScopes] = useState<GlossaryScope[] | null>(null);
  const [activeChipKey, setActiveChipKey] = usePersistentState<string>("glossary.activeChipKey", ALL_KEY);
  const [search, setSearch] = usePersistentState("glossary.search", "");
  const [editing, setEditing] = useState<GlossaryTerm | null>(null);
  const [creating, setCreating] = useState(false);
  const appliedInitialProjectRef = useRef(false);

  // —— 待确认 ——
  const [suggestionStatus, setSuggestionStatus] = usePersistentState<SuggestionStatus>("glossary.suggestionStatus", "pending");
  const [suggestions, setSuggestions] = useState<GlossarySuggestion[]>([]);
  const [suggestionsState, setSuggestionsState] = useState<LoadState>("loading");
  const [pendingTotal, setPendingTotal] = useState(0);

  const loadTerms = useCallback(async () => {
    const seq = ++termsSeqRef.current;
    try {
      const items = await apiClient.glossaryTerms();
      if (seq !== termsSeqRef.current) return;
      setTerms(items);
      setTermsState(items.length ? "ready" : "empty");
    } catch {
      if (seq !== termsSeqRef.current) return;
      setTermsState("error");
    }
  }, [apiClient]);

  // 分组 chip 的权威定序来自这个接口；后端没上线或报错时退回本地按术语现算，页面照常可用。
  const loadScopes = useCallback(async () => {
    try {
      const items = await apiClient.glossaryScopes?.();
      setRemoteScopes(Array.isArray(items) && items.length ? items : null);
    } catch {
      setRemoteScopes(null);
    }
  }, [apiClient]);

  const loadSuggestions = useCallback(async (status: SuggestionStatus) => {
    const seq = ++suggestionsSeqRef.current;
    try {
      const items = await apiClient.glossarySuggestions(status);
      if (seq !== suggestionsSeqRef.current) return;
      setSuggestions(items);
      setSuggestionsState(items.length ? "ready" : "empty");
    } catch {
      if (seq !== suggestionsSeqRef.current) return;
      setSuggestionsState("error");
    }
  }, [apiClient]);

  // 侧栏角标与「待确认」页签数字，确认/驳回后要跟着变。
  const refreshPendingTotal = useCallback(async () => {
    try {
      const items = await apiClient.glossarySuggestions("pending");
      setPendingTotal(Array.isArray(items) ? items.length : 0);
    } catch {
      setPendingTotal(0);
    }
  }, [apiClient]);

  useEffect(() => {
    void loadTerms();
    void loadScopes();
    void loadSuggestions("pending");
    void refreshPendingTotal();
  }, [loadScopes, loadSuggestions, loadTerms, refreshPendingTotal]);

  // 从项目详情页「在词典中查看」跳转过来：预选中该项目 chip，只在首次挂载时生效一次，
  // 之后用户自己切 chip 不应该被这个 prop 打断。
  useEffect(() => {
    if (initialProjectId && !appliedInitialProjectRef.current) {
      appliedInitialProjectRef.current = true;
      setActiveTab("terms");
      setActiveChipKey(chipKey({ kind: "project", key: initialProjectId }));
    }
  }, [initialProjectId]);

  const chips = useMemo<GlossaryScope[]>(() => {
    const local = deriveLocalScopes(terms);
    if (!remoteScopes) return local;
    const localByIdentity = new Map(local.map((chip) => [chipIdentity(chip), chip]));
    const merged = remoteScopes.map((chip) => ({
      ...chip,
      count: localByIdentity.get(chipIdentity(chip))?.count ?? 0,
    }));
    const mergedIdentities = new Set(merged.map((chip) => chipIdentity(chip)));
    // 远端还没收录的新分组（比如刚选中一个此前没有术语的项目）补在末尾，避免 chip 消失。
    local.forEach((chip) => {
      if (!mergedIdentities.has(chipIdentity(chip))) merged.push(chip);
    });
    return merged;
  }, [remoteScopes, terms]);

  const activeChip = chips.find((chip) => chipKey(chip) === activeChipKey) ?? null;
  const needle = search.trim().toLowerCase();

  const bucketSuggestions = useMemo(
    () => chips.filter((chip) => chip.kind === "bucket").map((chip) => chip.label),
    [chips],
  );

  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await action();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败，请稍后重试", "error");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const reloadAfterWrite = () => Promise.all([loadTerms(), loadScopes()]);

  const removeTerm = async (term: GlossaryTerm) => {
    const confirmed = await confirm({
      title: `删除术语「${term.term}」？`,
      message: "删除后无法撤销。",
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!confirmed) return;
    void run(async () => {
      await apiClient.deleteGlossaryTerm(term.id);
      setNotice(`已删除「${term.term}」`);
      await reloadAfterWrite();
    });
  };

  const confirmSuggestion = (suggestion: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.confirmGlossarySuggestion(suggestion.id);
      setNotice(`已确认：「${suggestion.wrong}」→「${suggestion.correct}」写入词典`);
      await Promise.all([loadSuggestions(suggestionStatus), refreshPendingTotal()]);
      onPendingChange?.();
    });

  const rejectSuggestion = (suggestion: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.rejectGlossarySuggestion(suggestion.id);
      setNotice(`已驳回「${suggestion.wrong} → ${suggestion.correct}」`);
      await Promise.all([loadSuggestions(suggestionStatus), refreshPendingTotal()]);
      onPendingChange?.();
    });

  const renderCard = (term: GlossaryTerm) => {
    const shownAliases = term.aliases.slice(0, 3);
    const extra = term.aliases.length - shownAliases.length;
    return (
      <div
        className="glossary-card"
        key={term.id}
        style={term.project_color ? { borderLeftColor: term.project_color } : undefined}
      >
        <div className="glossary-card__head">
          <strong className="glossary-card__term">{term.term}</strong>
          <span className="glossary-chip">{term.category}</span>
        </div>
        {term.aliases.length > 0 && (
          <div className="glossary-card__aliases">
            {shownAliases.map((alias) => (
              <span className="glossary-alias" key={alias}>
                {alias}
              </span>
            ))}
            {extra > 0 && <span className="glossary-card__alias-more">+{extra}</span>}
          </div>
        )}
        <div className="glossary-card__foot">
          {/* 命中数没有写入方（relay 只读快照，从不回写），接通之前不显示，免得每张卡都是 0 */}
          {canWrite && (
            <span className="glossary-card__ops">
              <button
                className="text-button"
                disabled={busy}
                onClick={() => setEditing(term)}
                type="button"
              >
                编辑
              </button>
              <button
                className="text-button text-button--muted"
                disabled={busy}
                onClick={() => void removeTerm(term)}
                type="button"
              >
                删除
              </button>
            </span>
          )}
        </div>
      </div>
    );
  };

  const renderSuggestionRow = (suggestion: GlossarySuggestion) => (
    <div className="glossary-suggestion" key={suggestion.id}>
      <div className="glossary-suggestion__body">
        <p className="glossary-suggestion__pair">
          <span className="glossary-suggestion__wrong">{suggestion.wrong}</span>
          <span className="glossary-suggestion__arrow" aria-hidden="true">→</span>
          <span className="glossary-suggestion__correct">{suggestion.correct}</span>
        </p>
        {suggestion.context && (
          <p className="glossary-suggestion__context">「{suggestion.context}」</p>
        )}
        <div className="glossary-suggestion__meta">
          <span>
            来自 {meetingLabel(suggestion.meeting_id, meetings)} ·{" "}
            {suggestion.scope || "通用"}
          </span>
          <span>{formatDate(suggestion.created_at)}</span>
        </div>
      </div>
      {canWrite && suggestion.status === "pending" && (
        <span className="glossary-suggestion__ops">
          <button
            className="text-button text-button--accent"
            disabled={busy}
            onClick={() => confirmSuggestion(suggestion)}
            type="button"
          >
            确认
          </button>
          <button
            className="text-button text-button--muted"
            disabled={busy}
            onClick={() => rejectSuggestion(suggestion)}
            type="button"
          >
            驳回
          </button>
        </span>
      )}
    </div>
  );

  const termsBody = () => {
    if (termsState === "loading") return <AsyncState state="loading" />;
    if (termsState === "error") return <AsyncState message="术语读取失败" state="error" />;
    if (termsState === "empty") {
      return (
        <AsyncState
          message="词典还是空的。先加一条权威写法，纪要生成时就会用它。"
          state="empty"
        />
      );
    }

    if (activeChipKey === ALL_KEY) {
      const groups = chips
        .map((chip) => ({
          chip,
          matched: terms.filter((term) => matchesChip(term, chip) && matchesSearch(term, needle)),
        }))
        .filter((group) => group.matched.length > 0);
      if (groups.length === 0) {
        return <AsyncState message={`没有匹配「${search.trim()}」的术语`} state="empty" />;
      }
      return (
        <>
          {groups.map(({ chip, matched }) => (
            <section className="glossary-group" key={chipKey(chip)}>
              <header className="glossary-group__head">
                <i
                  aria-hidden="true"
                  className="glossary-group__dot"
                  style={chip.color ? { background: chip.color } : undefined}
                />
                <strong>{chip.label}</strong>
                <span>{matched.length}</span>
              </header>
              <div className="glossary-card-grid">{matched.map(renderCard)}</div>
            </section>
          ))}
        </>
      );
    }

    const matched = activeChip
      ? terms.filter((term) => matchesChip(term, activeChip) && matchesSearch(term, needle))
      : [];
    if (matched.length === 0) {
      return (
        <AsyncState
          message={
            search.trim()
              ? `没有匹配「${search.trim()}」的术语`
              : `「${activeChip?.label ?? "这个范围"}」还没有术语。`
          }
          state="empty"
        />
      );
    }
    return <div className="glossary-card-grid">{matched.map(renderCard)}</div>;
  };

  return (
    <section className="page-content glossary-page">
      <header className="page-heading glossary-heading">
        <div>
          <span className="eyebrow">GLOSSARY / 术语词典</span>
          <h1>词典</h1>
          <p>
            纪要生成时会把权威写法注入给 AI，编辑纪要时的错字更正会进待确认队列，确认后反写回词典。
          </p>
        </div>
      </header>

      <div aria-label="词典功能" className="glossary-tabs" role="tablist">
        <button
          aria-selected={activeTab === "terms"}
          onClick={() => setActiveTab("terms")}
          role="tab"
          type="button"
        >
          术语库
          <span>{terms.length}</span>
        </button>
        <button
          aria-selected={activeTab === "suggestions"}
          onClick={() => setActiveTab("suggestions")}
          role="tab"
          type="button"
        >
          待确认
          <span>{pendingTotal}</span>
        </button>
      </div>

      <NoticeBanner notice={notice} onDismiss={dismissNotice} />

      {activeTab === "terms" && (
        <>
          <LegacyGroupsNote
            apiClient={apiClient}
            canWrite={canWrite}
            onChanged={async () => {
              await Promise.all([loadTerms(), loadScopes()]);
            }}
          />
          <div className="glossary-toolbar">
            <div aria-label="按归属筛选" className="glossary-chipbar" role="tablist">
              <button
                aria-selected={activeChipKey === ALL_KEY}
                className="glossary-chipbar__item"
                onClick={() => setActiveChipKey(ALL_KEY)}
                role="tab"
                type="button"
              >
                全部 <span>{terms.length}</span>
              </button>
              {chips.map((chip) => (
                <button
                  aria-selected={activeChipKey === chipKey(chip)}
                  className="glossary-chipbar__item"
                  key={chipKey(chip)}
                  onClick={() => setActiveChipKey(chipKey(chip))}
                  role="tab"
                  type="button"
                >
                  {chip.color && (
                    <i aria-hidden="true" style={{ background: chip.color }} />
                  )}
                  {chip.label} <span>{chip.count}</span>
                </button>
              ))}
            </div>
            <div className="glossary-toolbar__actions">
              <input
                aria-label="搜索术语"
                className="glossary-search"
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索术语或别名"
                value={search}
              />
              {canWrite && (
                <button
                  className="glossary-create"
                  onClick={() => setCreating(true)}
                  type="button"
                >
                  ＋ 新增术语
                </button>
              )}
            </div>
          </div>

          {termsBody()}
        </>
      )}

      {activeTab === "suggestions" && (
        <>
          <div aria-label="建议状态" className="glossary-subtabs" role="tablist">
            {SUGGESTION_TABS.map((tab) => (
              <button
                aria-selected={suggestionStatus === tab.key}
                key={tab.key}
                onClick={() => setSuggestionStatus(tab.key)}
                role="tab"
                type="button"
              >
                {tab.label}
              </button>
            ))}
          </div>

          {suggestionsState === "loading" && <AsyncState state="loading" />}
          {suggestionsState === "error" && <AsyncState message="待确认建议读取失败" state="error" />}
          {suggestionsState === "ready" && suggestions.length === 0 && (
            <AsyncState
              message={
                suggestionStatus === "pending"
                  ? "这里还没有待确认建议。编辑纪要时的错字更正会出现在这里等你处理。"
                  : `还没有${SUGGESTION_STATUS_LABEL[suggestionStatus]}的建议。`
              }
              state="empty"
            />
          )}
          {suggestionsState === "ready" && suggestions.length > 0 && (
            <div className="glossary-suggestion-list">
              {suggestions.map(renderSuggestionRow)}
            </div>
          )}
        </>
      )}

      {confirmDialog}

      {(creating || editing) && (
        <GlossaryTermModal
          apiClient={apiClient}
          bucketSuggestions={bucketSuggestions}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            setCreating(false);
            setEditing(null);
            void reloadAfterWrite();
          }}
          projects={projects}
          term={editing}
        />
      )}
    </section>
  );
}
