import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useConfirm } from "./ConfirmDialog";
import {
  CandidateDetail,
  CandidateList,
  type CandidateEntry,
  useGlossaryCandidates,
  visibleCandidateIds,
} from "./GlossaryCandidates";
import { GlossaryScopeRail } from "./GlossaryScopeRail";
import { GlossarySuggestionsPane, type SuggestionStatus } from "./GlossarySuggestionsPane";
import { GlossaryTermEditor, type TermEditorSaved } from "./GlossaryTermEditor";
import { GlossaryTermList, NewTermButton, TermListSkeleton, type TermSection } from "./GlossaryTermList";
import { targetName } from "./GlossaryTargetButton";
import { PUBLIC_GLOSSARY_KEY } from "./ProjectGlossary";
import { LegacyGroupsNote } from "./LegacyGroupsNote";
import { useLinksFlags } from "./links/LinksFlagsContext";
import { NoticeBanner, UNDO_NOTICE_MS, useNotice } from "./Notice";
import { useMutex } from "./useMutex";
import {
  ALL_KEY,
  chipIdentity,
  chipKey,
  correctionsFirst,
  deriveLocalScopes,
  matchesChip,
  matchesSearch,
  termChipKey,
} from "./glossaryModel";
import { IconBack, IconChevron, IconSearch, Kbd } from "./glossaryUi";
import { usePersistentState } from "../viewState";
import type { ApiClient } from "../api";
import type {
  GlossaryScope,
  GlossarySuggestion,
  GlossaryTarget,
  GlossaryTerm,
  LoadState,
  MeetingSummary,
  Project,
} from "../types";
import "./GlossaryWorkbench.css";

type TabKey = "terms" | "suggestions";
type ViewKey = "scope" | "inbox";
type ProjectTab = "terms" | "material";
/** 手机单栏时正在看哪一级；桌面三栏时只是个没有用的标记 */
type MobileLevel = "rail" | "list" | "detail";

interface GlossaryPageProps {
  apiClient: ApiClient;
  canWrite: boolean;
  meetings: MeetingSummary[];
  projects: Project[];
  /** 从项目详情页「在词典中查看」跳转过来时预选中的项目；只在首次挂载生效一次。 */
  initialProjectId?: string | null;
  onPendingChange?: () => void;
  /** 4h：待认词里的时间点，打开那场会并从那里放 */
  onOpenMeeting?: (meetingId: string, seekMs: number) => void;
}

const STATUSES: SuggestionStatus[] = ["pending", "confirmed", "rejected"];
const EMPTY_SUGGESTIONS: Record<SuggestionStatus, GlossarySuggestion[]> = { pending: [], confirmed: [], rejected: [] };

function ownerOf(term: GlossaryTerm): { name: string; color: string | null } {
  if (term.project_id) return { name: term.project_name ?? term.scope, color: term.project_color ?? null };
  if (!term.scope || term.scope === "通用") return { name: "公共", color: null };
  return { name: `旧分组「${term.scope}」`, color: null };
}

/** 答完一个以后跳到下一个还没答的；后面没有了就从头找 */
function nextPending(ids: string[], current: string, answered: (id: string) => boolean): string {
  const at = ids.indexOf(current);
  return ids.slice(at + 1).find((id) => !answered(id)) ?? ids.find((id) => id !== current && !answered(id)) ?? current;
}

export function GlossaryPage({
  apiClient,
  canWrite,
  meetings,
  projects,
  initialProjectId,
  onPendingChange,
  onOpenMeeting,
}: GlossaryPageProps) {
  const flags = useLinksFlags();
  const { notice, setNotice, dismissNotice } = useNotice();
  const [confirm, confirmDialog] = useConfirm();
  const { busy, run, runAfterCurrent } = useMutex((message) => setNotice(message, "error"));

  // —— 页面状态：刷新和离开再回来都保留（不写进地址栏，地址只管视图） ——
  const [activeTab, setActiveTab] = usePersistentState<TabKey>("glossary.activeTab", "terms");
  const [view, setView] = usePersistentState<ViewKey>("glossary.view", "scope");
  const [activeChipKey, setActiveChipKey] = usePersistentState<string>("glossary.activeChipKey", ALL_KEY);
  const [projectTab, setProjectTab] = usePersistentState<ProjectTab>("glossary.projectTab", "terms");
  const [search, setSearch] = usePersistentState("glossary.search", "");
  const [selectedTermId, setSelectedTermId] = usePersistentState<string | null>("glossary.selectedTermId", null);
  const [selectedCandId, setSelectedCandId] = usePersistentState<string | null>("glossary.selectedCandId", null);
  const [suggestionStatus, setSuggestionStatus] = usePersistentState<SuggestionStatus>("glossary.suggestionStatus", "pending");

  const [mobileLevel, setMobileLevel] = useState<MobileLevel>("list");
  const [isNew, setIsNew] = useState(false);
  const [newSeed, setNewSeed] = useState<{ text: string; projectId: string | null }>({ text: "", projectId: null });
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});
  const appliedInitialProjectRef = useRef(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const dirtyRef = useRef(false);
  const pendingSelectRef = useRef<string | null>(null);

  // —— 术语库 ——
  // 术语与建议各自独立计数：共用同一个 counter 会互相覆盖，先启动的请求被误判为过期丢弃。
  const termsSeqRef = useRef(0);
  const suggestionsSeqRef = useRef(0);
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  const [termsState, setTermsState] = useState<LoadState>("loading");
  const [remoteScopes, setRemoteScopes] = useState<GlossaryScope[] | null>(null);

  // —— 待确认 ——
  const [suggestionLists, setSuggestionLists] = useState(EMPTY_SUGGESTIONS);
  const [suggestionsState, setSuggestionsState] = useState<LoadState>("loading");
  // 「只记 2 字」勾选：按建议 id 记
  const [shortPicks, setShortPicks] = useState<Record<string, boolean>>({});

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

  // 分组的权威定序来自这个接口；后端没上线或报错时退回本地按术语现算，页面照常可用。
  const loadScopes = useCallback(async () => {
    try {
      const items = await apiClient.glossaryScopes?.();
      setRemoteScopes(Array.isArray(items) && items.length ? items : null);
    } catch {
      setRemoteScopes(null);
    }
  }, [apiClient]);

  // 三个状态一起读：页签上的数字要同时对
  const loadSuggestions = useCallback(async () => {
    const seq = ++suggestionsSeqRef.current;
    try {
      const [pending, confirmed, rejected] = await Promise.all(STATUSES.map((status) => apiClient.glossarySuggestions(status)));
      if (seq !== suggestionsSeqRef.current) return;
      const list = (items: GlossarySuggestion[]) => (Array.isArray(items) ? items : []);
      setSuggestionLists({ pending: list(pending), confirmed: list(confirmed), rejected: list(rejected) });
      setSuggestionsState("ready");
    } catch {
      if (seq !== suggestionsSeqRef.current) return;
      setSuggestionsState("error");
    }
  }, [apiClient]);

  const reloadAfterWrite = useCallback(async () => {
    await Promise.all([loadTerms(), loadScopes()]);
  }, [loadScopes, loadTerms]);

  // —— 待认词（收件箱 + 项目里的「从材料里找到的词」） ——
  const notifyCandidate = useCallback(
    (message: string, tone: "success" | "error", undoAction?: () => void) =>
      setNotice(
        message,
        tone,
        undoAction ? UNDO_NOTICE_MS : undefined,
        undoAction ? [{ label: "撤销", onClick: undoAction }] : undefined,
      ),
    [setNotice],
  );
  const candidateApiReady =
    typeof apiClient.acceptGlossaryCandidate === "function" &&
    typeof apiClient.rejectGlossaryCandidate === "function" &&
    typeof apiClient.undoGlossaryCandidate === "function";
  const candidates = useGlossaryCandidates({
    apiClient,
    enabled: Boolean(flags) && candidateApiReady,
    onTermsChanged: reloadAfterWrite,
    notify: notifyCandidate,
  });
  const { loadInbox, loadProject } = candidates;

  useEffect(() => {
    void loadTerms();
    void loadScopes();
    void loadInbox();
  }, [loadInbox, loadScopes, loadTerms]);

  useEffect(() => {
    void loadSuggestions();
  }, [loadSuggestions, activeTab]);

  // 从项目详情页「在词典中查看」跳转过来：预选中该项目，只在首次挂载时生效一次，
  // 之后用户自己切范围不应该被这个 prop 打断。
  useEffect(() => {
    if (initialProjectId && !appliedInitialProjectRef.current) {
      appliedInitialProjectRef.current = true;
      setActiveTab("terms");
      setView("scope");
      setProjectTab("terms");
      setActiveChipKey(
        initialProjectId === PUBLIC_GLOSSARY_KEY
          ? chipKey({ kind: "general", key: "通用" })
          : chipKey({ kind: "project", key: initialProjectId }),
      );
    }
  }, [initialProjectId]);

  // —— 范围 ——
  const chips = useMemo<GlossaryScope[]>(() => {
    const local = deriveLocalScopes(terms);
    let merged = local;
    if (remoteScopes) {
      const localByIdentity = new Map(local.map((chip) => [chipIdentity(chip), chip]));
      merged = remoteScopes.map((chip) => ({
        ...chip,
        count: localByIdentity.get(chipIdentity(chip))?.count ?? 0,
      }));
      const mergedIdentities = new Set(merged.map((chip) => chipIdentity(chip)));
      // 远端还没收录的新分组（比如刚选中一个此前没有术语的项目）补在末尾，避免 chip 消失。
      local.forEach((chip) => {
        if (!mergedIdentities.has(chipIdentity(chip))) merged.push(chip);
      });
    }
    // 公共在最前，有词的项目按词数从多到少，旧分组桶最后；没有词的项目留给左栏折叠区
    const general = merged.filter((chip) => chip.kind === "general");
    const projectChips = merged.filter((chip) => chip.kind === "project");
    const withWords = projectChips.filter((chip) => chip.count > 0).sort((a, b) => b.count - a.count);
    const noWords = projectChips.filter((chip) => chip.count === 0);
    return [...general, ...withWords, ...noWords, ...merged.filter((chip) => chip.kind === "bucket")];
  }, [remoteScopes, terms]);

  const rawChip = chips.find((chip) => chipKey(chip) === activeChipKey) ?? null;
  const activeChip = rawChip;
  // 存下来的范围已经不在了（项目删了、旧分组整理了）就回到全部
  const scopeKey = activeChipKey === ALL_KEY || (!activeChip && termsState !== "loading") ? ALL_KEY : activeChipKey;
  const projectChip = activeChip?.kind === "project" ? activeChip : null;
  const needle = search.trim().toLowerCase();

  const inboxUsable = candidates.inboxState !== "unavailable";
  const hasMaterial =
    projectChip !== null &&
    (candidates.pendingIn(projectChip.key) > 0 ||
      candidates.entries.some((entry) => entry.projectId === projectChip.key && candidates.answers[entry.id]));
  const effectiveView: ViewKey = view === "inbox" && inboxUsable ? "inbox" : "scope";
  const effectiveProjectTab: ProjectTab = projectTab === "material" && hasMaterial ? "material" : "terms";
  const termsMode = activeTab === "terms";
  const candMode = termsMode && (effectiveView === "inbox" || (projectChip !== null && effectiveProjectTab === "material"));
  const candidateProjectId = effectiveView === "inbox" ? null : projectChip?.key ?? null;
  const candidateScope: "inbox" | "project" = effectiveView === "inbox" ? "inbox" : "project";

  // 项目进入时以项目自己的待认词接口为准
  const projectChipKey = projectChip?.key;
  const projectChipLabel = projectChip?.label;
  const projectChipColor = projectChip?.color ?? null;
  useEffect(() => {
    if (projectChipKey) void loadProject(projectChipKey, projectChipLabel ?? "", projectChipColor);
  }, [loadProject, projectChipColor, projectChipKey, projectChipLabel]);

  // —— 列表数据 ——
  const sections = useMemo<TermSection[]>(() => {
    if (scopeKey === ALL_KEY) {
      return chips
        .map((chip) => ({
          key: chipKey(chip),
          chip,
          items: correctionsFirst(terms.filter((term) => matchesChip(term, chip) && matchesSearch(term, needle))),
        }))
        .filter((section) => section.items.length > 0);
    }
    if (!activeChip) return [];
    const items = correctionsFirst(terms.filter((term) => matchesChip(term, activeChip) && matchesSearch(term, needle)));
    return items.length ? [{ key: scopeKey, chip: null, items }] : [];
  }, [activeChip, chips, needle, scopeKey, terms]);
  const visibleTermIds = useMemo(() => sections.flatMap((section) => section.items.map((term) => term.id)), [sections]);
  const visibleCandIds = useMemo(
    () => (candMode ? visibleCandidateIds(candidates, candidateScope, candidateProjectId, needle, expandedGroups) : []),
    // candidates 每次渲染都是新对象；它的内容变化都落在这几个值上
    [candMode, candidates.groups, candidates.entries, candidateScope, candidateProjectId, needle, expandedGroups],
  );
  const selectedTerm = terms.find((term) => term.id === selectedTermId) ?? null;
  const selectedEntry = candidates.entries.find((entry) => entry.id === selectedCandId) ?? null;

  // 选中的不在当前列表里（换了范围、搜索、删了）就落到第一条，桌面三栏右边永远有东西看
  useEffect(() => {
    if (!termsMode || candMode || isNew || termsState === "loading" || termsState === "error") return;
    if (dirtyRef.current) return;
    if (pendingSelectRef.current) {
      if (pendingSelectRef.current === selectedTermId && !terms.some((term) => term.id === selectedTermId)) return;
      pendingSelectRef.current = null;
    }
    if (selectedTermId && visibleTermIds.includes(selectedTermId)) return;
    setSelectedTermId(visibleTermIds[0] ?? null);
  }, [candMode, isNew, selectedTermId, setSelectedTermId, terms, termsMode, termsState, visibleTermIds]);

  useEffect(() => {
    if (!candMode) return;
    if (selectedCandId && visibleCandIds.includes(selectedCandId)) return;
    setSelectedCandId(visibleCandIds.find((id) => !candidates.answers[id]) ?? visibleCandIds[0] ?? null);
  }, [candMode, selectedCandId, visibleCandIds]);

  // —— 未保存守卫：换词条、换范围、换页签之前，右栏有没存的修改就先问 ——
  const onDirtyChange = useCallback((dirty: boolean) => {
    dirtyRef.current = dirty;
  }, []);
  const guardLeave = useCallback(
    (action: () => void) => {
      if (!dirtyRef.current) {
        action();
        return;
      }
      void confirm({
        title: "这条还没保存，要放弃修改吗？",
        message: "放弃后，刚才改的内容不会保留。",
        confirmLabel: "放弃修改",
        cancelLabel: "继续编辑",
        tone: "danger",
      }).then((ok) => {
        if (!ok) return;
        dirtyRef.current = false;
        action();
      });
    },
    [confirm],
  );

  // —— 导航动作 ——
  const goScope = (key: string) =>
    guardLeave(() => {
      setActiveTab("terms");
      setView("scope");
      setActiveChipKey(key);
      setProjectTab("terms");
      setSearch("");
      setIsNew(false);
      setMobileLevel("list");
    });

  const goInbox = () =>
    guardLeave(() => {
      setActiveTab("terms");
      setView("inbox");
      setSearch("");
      setIsNew(false);
      setMobileLevel("list");
      void loadInbox();
    });

  const goSuggestions = () =>
    guardLeave(() => {
      setActiveTab("suggestions");
      setIsNew(false);
      setMobileLevel("list");
    });

  const goTermsTab = () =>
    guardLeave(() => {
      setActiveTab("terms");
      setMobileLevel("list");
    });

  const goProjectTab = (tab: ProjectTab) =>
    guardLeave(() => {
      setProjectTab(tab);
      setSearch("");
      setIsNew(false);
    });

  const openProjectMaterial = (projectId: string) =>
    guardLeave(() => {
      setView("scope");
      setActiveChipKey(chipKey({ kind: "project", key: projectId }));
      setProjectTab("material");
      setSearch("");
      setMobileLevel("list");
    });

  const selectTerm = (id: string) => {
    if (id === selectedTermId && !isNew) {
      setMobileLevel("detail");
      return;
    }
    guardLeave(() => {
      setIsNew(false);
      setSelectedTermId(id);
      setMobileLevel("detail");
    });
  };

  const selectCandidate = (id: string) => {
    setSelectedCandId(id);
    setMobileLevel("detail");
  };

  const startNew = (text = "", chip: GlossaryScope | null = null) => {
    if (!canWrite) return;
    guardLeave(() => {
      const target = chip ?? activeChip;
      setNewSeed({ text, projectId: target?.kind === "project" ? target.key : null });
      if (chip && scopeKey !== ALL_KEY) setActiveChipKey(chipKey(chip));
      if (candMode) {
        setView("scope");
        setProjectTab("terms");
      }
      setIsNew(true);
      setMobileLevel("detail");
    });
  };

  const move = (direction: 1 | -1) => {
    if (candMode) {
      if (!visibleCandIds.length) return;
      const at = visibleCandIds.indexOf(selectedCandId ?? "");
      setSelectedCandId(visibleCandIds[Math.max(0, Math.min(visibleCandIds.length - 1, at < 0 ? 0 : at + direction))]);
      return;
    }
    if (!visibleTermIds.length) return;
    const at = visibleTermIds.indexOf(selectedTermId ?? "");
    const next = visibleTermIds[Math.max(0, Math.min(visibleTermIds.length - 1, at < 0 ? 0 : at + direction))];
    if (next !== selectedTermId || isNew) selectTerm(next);
  };

  // 选中项跟着键盘走时，让它留在可视范围里。保存后它可能换了位置（补了或删光错写、改了归属、新词刚出现），
  // 等列表刷新完再跟一次；别的行增删不跟，免得把用户滚到别处的列表拽回来
  const [savedTick, setSavedTick] = useState(0);
  useEffect(() => {
    document.querySelector<HTMLElement>('.gw-list [role="option"][aria-selected="true"]')?.scrollIntoView?.({ block: "nearest" });
  }, [selectedTermId, selectedCandId, savedTick]);

  // —— 词条写操作 ——
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
      const at = visibleTermIds.indexOf(term.id);
      const next = visibleTermIds[at + 1] ?? visibleTermIds[at - 1];
      // 没有相邻的就先不动：列表还没刷新时置空，上面的兜底会把刚删的这条又选回来，再去取它的详情拿到 404
      if (term.id === selectedTermId && next) setSelectedTermId(next);
      setNotice(`已删除「${term.term}」`);
      await reloadAfterWrite();
    });
  };

  const handleSaved = ({ message, termId, term: saved, owner }: TermEditorSaved) => {
    setNotice(message);
    setIsNew(false);
    if (termId) {
      pendingSelectRef.current = termId;
      setSelectedTermId(termId);
    }
    // 在某个范围里改了归属，左栏跟着换到这条现在所在的范围
    const landed = termChipKey({ project_id: owner.project_id, scope: owner.scope });
    if (scopeKey !== ALL_KEY && landed !== scopeKey && chips.some((chip) => chipKey(chip) === landed)) {
      setActiveChipKey(landed);
    }
    if (saved && needle && !matchesSearch(saved, needle)) setSearch("");
    void reloadAfterWrite().then(() => setSavedTick((tick) => tick + 1));
  };

  // —— 待确认写操作 ——
  const afterSuggestionChange = async () => {
    await Promise.all([loadSuggestions(), reloadAfterWrite()]);
    onPendingChange?.();
  };

  // 提示条上的［撤销］比这次记入的重新取数先出来：排在手头那次后面，不被互斥吞掉
  const undoSuggestion = (suggestion: GlossarySuggestion) =>
    void runAfterCurrent(async () => {
      await apiClient.undoGlossarySuggestion(suggestion.id);
      setNotice(`已撤销，「${suggestion.wrong} → ${suggestion.correct}」回到待确认`);
      await afterSuggestionChange();
    });

  const confirmSuggestion = (suggestion: GlossarySuggestion, target: GlossaryTarget) =>
    void run(async () => {
      const result = await apiClient.confirmGlossarySuggestion(suggestion.id, {
        target,
        short: Boolean(shortPicks[suggestion.id]),
      });
      const where = targetName(result.term?.project_name);
      setNotice(
        result.created
          ? `已记入 ${where}：${result.wrong} → ${result.correct}`
          : `已加到 ${where} 的『${result.correct}』：${result.wrong} → ${result.correct}`,
        "success",
        UNDO_NOTICE_MS,
        [{ label: "撤销", onClick: () => undoSuggestion(suggestion) }],
      );
      await afterSuggestionChange();
    });

  const rejectSuggestion = (suggestion: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.rejectGlossarySuggestion(suggestion.id);
      setNotice(`已标成不是错字：「${suggestion.wrong} → ${suggestion.correct}」，在「已驳回」里可以恢复`);
      await afterSuggestionChange();
    });

  const restoreSuggestion = (suggestion: GlossarySuggestion) =>
    void run(async () => {
      await apiClient.restoreGlossarySuggestion(suggestion.id);
      setNotice(`已恢复，「${suggestion.wrong} → ${suggestion.correct}」回到待确认`);
      await afterSuggestionChange();
    });

  // —— 待认词的回答：答完自动跳到下一个 ——
  const answerCandidate = (kind: "accept" | "reject", entryId: string) => {
    const entry = candidates.entries.find((item) => item.id === entryId);
    if (!entry || !canWrite) return;
    setSelectedCandId(nextPending(visibleCandIds, entry.id, (id) => id === entry.id || Boolean(candidates.answers[id])));
    void candidates[kind](entry);
  };

  // —— 一组一起记入：先说清楚是哪些词；选中的在这批里就先跳到这批之外的下一个 ——
  const acceptAllCandidates = (projectName: string, list: CandidateEntry[]) => {
    if (!canWrite || list.length === 0) return;
    const names = list.slice(0, 6).map((entry) => `『${entry.word.term}』`).join("");
    void confirm({
      title: `把${projectName}的 ${list.length} 个词都记入词典？`,
      message: `${names}${list.length > 6 ? ` 等 ${list.length} 个` : ""}。记完可以在提示条上一起撤销。`,
      confirmLabel: `记入 ${list.length} 个`,
    }).then((ok) => {
      if (!ok) return;
      const ids = new Set(list.map((entry) => entry.id));
      if (selectedCandId && ids.has(selectedCandId)) {
        setSelectedCandId(nextPending(visibleCandIds, selectedCandId, (id) => ids.has(id) || Boolean(candidates.answers[id])));
      }
      void candidates.acceptMany(list);
    });
  };

  // —— 键盘：J/K 上下、/ 搜索、C 新增、1 记入、2 不是；输入框里和输入法组合中一律不触发 ——
  const keyHandler = useRef<(event: KeyboardEvent) => void>(() => undefined);
  keyHandler.current = (event: KeyboardEvent) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (document.querySelector('[role="alertdialog"], [role="dialog"]')) return;
    if (!termsMode) return;
    const key = event.key.toLowerCase();
    if (key === "j") {
      event.preventDefault();
      move(1);
    } else if (key === "k") {
      event.preventDefault();
      move(-1);
    } else if (key === "/") {
      event.preventDefault();
      searchRef.current?.focus();
    } else if (key === "c" && !candMode && canWrite) {
      event.preventDefault();
      startNew();
    } else if ((key === "1" || key === "2") && candMode && canWrite && selectedEntry && !candidates.answers[selectedEntry.id]) {
      event.preventDefault();
      answerCandidate(key === "1" ? "accept" : "reject", selectedEntry.id);
    }
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => keyHandler.current(event);
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, []);

  // —— 渲染 ——
  const pendingCount = suggestionLists.pending.length;
  const inboxTotal = candidates.pendingTotal;
  const existingScope = (termId: string) => {
    const found = terms.find((term) => term.id === termId);
    return found ? ownerOf(found).name : "公共";
  };

  const listTitle = candMode && effectiveView === "inbox"
    ? { name: "待认词", meta: `${inboxTotal} 个等你认`, color: null as string | null }
    : scopeKey === ALL_KEY || !activeChip
      ? { name: "全部", meta: `${terms.length} 条`, color: null }
      : { name: activeChip.label, meta: `${activeChip.count} 条`, color: activeChip.color };

  const layout: "all" | "project" | "other" = scopeKey === ALL_KEY ? "all" : projectChip ? "project" : "other";

  const renderTermsBody = () => {
    if (termsState === "loading") return <TermListSkeleton />;
    if (termsState === "error") {
      return (
        <div className="gw-empty" role="alert">
          <strong>词典没载入成功</strong>
          服务可能刚重启，稍后再试。
          <br />
          <button className="gw-btn" onClick={() => void loadTerms()} type="button">
            重试
          </button>
        </div>
      );
    }
    if (termsState === "empty") {
      return (
        <div className="gw-empty">
          <strong>词典还是空的。</strong>
          先加一条权威写法，纪要生成时就会用它。
          {canWrite && (
            <>
              <br />
              <NewTermButton onClick={() => startNew()} />
            </>
          )}
        </div>
      );
    }
    if (sections.length === 0) {
      const text = search.trim();
      if (text) {
        return (
          <div className="gw-empty">
            <strong>没找到「{text}」</strong>
            正确写法、错写和也叫里都没有这个词。
            {canWrite && (
              <>
                <br />
                <NewTermButton label={`把「${text}」加进词典`} onClick={() => startNew(text)} />
              </>
            )}
          </div>
        );
      }
      if (projectChip) {
        return (
          <div className="gw-empty">
            <strong>『{projectChip.label}』还没有项目词。</strong>
            项目词只在这个项目的会里用来纠错和识别项目。
            {hasMaterial && (
              <>
                <br />
                <button className="gw-btn" onClick={() => goProjectTab("material")} type="button">
                  看看材料里找到的 {candidates.pendingIn(projectChip.key)} 个词
                </button>
              </>
            )}
            {canWrite && (
              <>
                <br />
                <NewTermButton onClick={() => startNew()} />
              </>
            )}
          </div>
        );
      }
      return (
        <div className="gw-empty">
          <strong>「{activeChip?.label ?? "这个范围"}」还没有术语。</strong>
          加一条正确写法和它常被听错的样子。
        </div>
      );
    }
    return (
      <GlossaryTermList
        canWrite={canWrite}
        layout={layout}
        needle={needle}
        onDelete={(term) => void removeTerm(term)}
        onNewIn={(chip) => startNew("", chip)}
        onSelect={selectTerm}
        sections={sections}
        selectedId={isNew ? null : selectedTermId}
      />
    );
  };

  const renderList = () => (
    <div className="gw-col gw-list">
      <div className="gw-lhead">
        <button className="gw-btn gw-btn--sm gw-btn--ghost gw-mback" onClick={() => setMobileLevel("rail")} type="button">
          <IconBack />
          范围
        </button>
        <div className="gw-lt">
          {listTitle.color && <i className="gw-dot" style={{ background: listTitle.color }} />}
          <h2>{listTitle.name}</h2>
          <span className="gw-lt__n">{listTitle.meta}</span>
        </div>
        <label className="gw-lsearch">
          <IconSearch />
          <input
            aria-label={candMode ? "搜索候选词" : "搜索术语"}
            autoComplete="off"
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape" || event.nativeEvent.isComposing || event.keyCode === 229) return;
              event.stopPropagation();
              if (search) setSearch("");
              event.currentTarget.blur();
            }}
            placeholder={candMode ? "搜索候选词或听错的写法" : "搜索正确写法、错写、也叫"}
            ref={searchRef}
            value={search}
          />
          <Kbd>/</Kbd>
        </label>
        {canWrite && (
          <button className="gw-btn gw-btn--pri" onClick={() => startNew()} type="button">
            <span aria-hidden="true">＋</span>
            新增术语 <Kbd>C</Kbd>
          </button>
        )}
      </div>

      {effectiveView === "scope" && projectChip && hasMaterial && (
        <div className="gw-vtabs" role="tablist">
          <button aria-selected={effectiveProjectTab === "terms"} onClick={() => goProjectTab("terms")} role="tab" type="button">
            词条 <span className="gw-cnt">{projectChip.count}</span>
          </button>
          <button aria-selected={effectiveProjectTab === "material"} onClick={() => goProjectTab("material")} role="tab" type="button">
            从材料里找到的词{" "}
            <span className={`gw-cnt${candidates.pendingIn(projectChip.key) > 0 ? " gw-cnt--hot" : ""}`}>
              {candidates.pendingIn(projectChip.key)}
            </span>
          </button>
        </div>
      )}
      {effectiveView === "scope" && (inboxUsable || pendingCount > 0) && (
        <button className="gw-mstrip" onClick={() => setMobileLevel("rail")} type="button">
          等你处理
          {inboxUsable && (
            <span>
              待认词 <b>{inboxTotal}</b>
            </span>
          )}
          <span>
            待确认 <b>{pendingCount}</b>
          </span>
          <span className="gw-mstrip__sp" />
          <IconChevron />
        </button>
      )}

      <div className="gw-lbody">
        {candMode ? (
          candidates.inboxState === "idle" && effectiveView === "inbox" ? (
            <p className="gw-note">正在读取待认词…</p>
          ) : (
            <CandidateList
              canAnswer={canWrite}
              expanded={expandedGroups}
              mode={candidateScope}
              needle={needle}
              onAcceptAll={acceptAllCandidates}
              onExpand={(id) => setExpandedGroups((current) => ({ ...current, [id]: true }))}
              onOpenProject={openProjectMaterial}
              onSelect={selectCandidate}
              projectId={candidateProjectId}
              selectedId={selectedCandId}
              store={candidates}
            />
          )
        ) : (
          renderTermsBody()
        )}
      </div>

      <div className="gw-lfoot">
        <span>
          <Kbd>J</Kbd>
          <Kbd>K</Kbd> 上下
        </span>
        <span>
          <Kbd>/</Kbd> 搜索
        </span>
        {candMode ? (
          canWrite && (
            <>
              <span>
                <Kbd>1</Kbd> 记入
              </span>
              <span>
                <Kbd>2</Kbd> 不是
              </span>
            </>
          )
        ) : (
          canWrite && (
            <>
              <span>
                <Kbd>C</Kbd> 新增
              </span>
              <span>
                <Kbd>Esc</Kbd> 放弃修改
              </span>
            </>
          )
        )}
      </div>
    </div>
  );

  const renderDetail = () => {
    let body;
    if (candMode) {
      body = (
        <CandidateDetail
          canAnswer={canWrite}
          entry={selectedEntry}
          existingScope={existingScope}
          onAccept={(entry) => answerCandidate("accept", entry.id)}
          onBack={() => setMobileLevel("list")}
          onOpenMeeting={onOpenMeeting}
          onReject={(entry) => answerCandidate("reject", entry.id)}
          pendingLeft={visibleCandIds.filter((id) => !candidates.answers[id]).length}
          store={candidates}
        />
      );
    } else if (isNew) {
      body = (
        <GlossaryTermEditor
          apiClient={apiClient}
          canWrite={canWrite}
          defaultProjectId={newSeed.projectId}
          initialText={newSeed.text}
          key="new"
          onBack={() => setMobileLevel("list")}
          onCancelNew={() => {
            dirtyRef.current = false;
            setIsNew(false);
            setMobileLevel("list");
          }}
          onDirtyChange={onDirtyChange}
          onOpenMeeting={onOpenMeeting}
          onSaved={handleSaved}
          projects={projects}
          term={null}
        />
      );
    } else if (selectedTerm) {
      body = (
        <GlossaryTermEditor
          apiClient={apiClient}
          canWrite={canWrite}
          key={`${selectedTerm.id}:${selectedTerm.updated_at}`}
          onBack={() => setMobileLevel("list")}
          onCancelNew={() => undefined}
          onDelete={(term) => void removeTerm(term)}
          onDirtyChange={onDirtyChange}
          onOpenMeeting={onOpenMeeting}
          onSaved={handleSaved}
          ownerLabel={ownerOf(selectedTerm)}
          projects={projects}
          term={selectedTerm}
        />
      );
    } else {
      body = (
        <div className="gw-dempty">
          <div>
            选一条词看详情
            <div className="gw-keys">
              <Kbd>J</Kbd>
              <span>下一条</span>
              <Kbd>K</Kbd>
              <span>上一条</span>
              <Kbd>/</Kbd>
              <span>搜索</span>
              {canWrite && (
                <>
                  <Kbd>C</Kbd>
                  <span>新增术语</span>
                </>
              )}
            </div>
          </div>
        </div>
      );
    }
    return (
      <aside aria-label={candMode ? "候选词详情" : "词条详情"} className="gw-col gw-detail">
        {body}
      </aside>
    );
  };

  return (
    <section className="page-content glossary-page gw">
      <header className="gw-head">
        <div>
          <span className="eyebrow">GLOSSARY / 术语词典</span>
          <div className="gw-titleline">
            <h1>词典</h1>
            <p>出纪要时，按这场会的内容挑出相关词条交给 AI 纠错；词典不改逐字稿。</p>
          </div>
        </div>
        <div aria-label="词典功能" className="gw-bigtabs" role="tablist">
          <button aria-selected={termsMode} onClick={goTermsTab} role="tab" type="button">
            术语库 <span className="gw-cnt">{terms.length}</span>
          </button>
          <button aria-selected={!termsMode} onClick={goSuggestions} role="tab" type="button">
            待确认 <span className={`gw-cnt${pendingCount > 0 ? " gw-cnt--hot" : ""}`}>{pendingCount}</span>
          </button>
        </div>
      </header>

      <section className={`gw-wb${termsMode ? "" : " gw-wb--sug"}`} data-m={mobileLevel}>
        <GlossaryScopeRail
          activeChipKey={scopeKey}
          chips={chips}
          inbox={{ available: inboxUsable && candidates.inboxState === "ready", total: inboxTotal, active: termsMode && effectiveView === "inbox" }}
          legacy={(withHeading) => (
            <LegacyGroupsNote apiClient={apiClient} canWrite={canWrite} onChanged={reloadAfterWrite} withHeading={withHeading} />
          )}
          onInbox={goInbox}
          onScope={goScope}
          onSuggestions={goSuggestions}
          pendingIn={candidates.pendingIn}
          scopeActive={termsMode && effectiveView === "scope"}
          suggestions={{ count: pendingCount, active: !termsMode }}
          totalTerms={terms.length}
        />
        {termsMode ? (
          <>
            {renderList()}
            {renderDetail()}
          </>
        ) : (
          <GlossarySuggestionsPane
            busy={busy}
            canWrite={canWrite}
            counts={{
              pending: suggestionLists.pending.length,
              confirmed: suggestionLists.confirmed.length,
              rejected: suggestionLists.rejected.length,
            }}
            items={suggestionLists[suggestionStatus]}
            meetings={meetings}
            onBack={() => setMobileLevel("rail")}
            onConfirm={confirmSuggestion}
            onReject={rejectSuggestion}
            onRestore={restoreSuggestion}
            onRetry={() => void loadSuggestions()}
            onShortPick={(id, on) => setShortPicks((current) => ({ ...current, [id]: on }))}
            onStatus={setSuggestionStatus}
            onUndo={undoSuggestion}
            projects={projects}
            shortPicks={shortPicks}
            state={suggestionsState}
            status={suggestionStatus}
          />
        )}
      </section>

      <div className="gw-toasts">
        <NoticeBanner className="gw-notice" notice={notice} onDismiss={dismissNotice} />
      </div>

      {confirmDialog}
    </section>
  );
}
