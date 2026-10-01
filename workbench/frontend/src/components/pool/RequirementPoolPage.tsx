import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api";
import type { PoolFlash, PoolItem, PoolTab, RequirementPoolPayload, RequirementPriority } from "../../types";
import { usePersistentState } from "../../viewState";
import { REQUIREMENT_PRIORITIES } from "../RequirementBadges";
import { useToast } from "../Toast";
import { DirectionBar } from "./DirectionBar";
import { DroppedCandidatesDialog } from "./DroppedCandidatesDialog";
import { MergeCandidateDialog } from "./MergeCandidateDialog";
import { PosterCard } from "./PosterCard";
import { copiedMessage, copyFailureReason, copyRequirementBackground } from "./requirementCopy";
import "./RequirementPoolPage.css";

/** 页签和筛选记在本机（R02-8），刷新、关掉再开都保持上次的选择 */
export const POOL_TAB_KEY = "requirementPool.tab";
export const POOL_PROJECTS_KEY = "requirementPool.projects";
export const POOL_PRIORITIES_KEY = "requirementPool.priorities";
export const POOL_QUERY_KEY = "requirementPool.q";
// 墙上一次挂完：现在是几十条的量级，不分页
const WALL_LIMIT = 500;
/** 认领、新建后落位的那张海报描边多久（R01-13） */
const HIGHLIGHT_MS = 3000;

/** 合并成功的轻提示（R01-14）：从认领页合并回来时由 App 用同一句 */
export const mergedMessage = (title: string) => `已合并到「${title}」，这场会和原话已加进去`;
/** 撤销合并以后：这 10 分钟里被改挂到别处的任务没有退回，说一声 */
export const unmergedMessage = (keptTaskCount = 0) =>
  keptTaskCount > 0 ? `已撤销合并；有 ${keptTaskCount} 条任务已经挂到别处，没有退回` : "已撤销合并";

function findPoster(wall: HTMLElement | null, id: string): HTMLElement | null {
  if (!wall) return null;
  return Array.from(wall.querySelectorAll<HTMLElement>("[data-poster-id]")).find((node) => node.dataset.posterId === id) ?? null;
}

const TABS: Array<{ key: PoolTab; label: string }> = [
  { key: "pending", label: "待认领" },
  { key: "active", label: "进行中" },
  { key: "done", label: "已完成" },
  { key: "shelved", label: "已搁置" },
  { key: "all", label: "全部" },
];

const EMPTY_TEXT: Record<PoolTab, { title: string; hint: string }> = {
  pending: { title: "没有待认领的候选", hint: "会后 AI 会从纪要里抽需求候选，放进这里" },
  active: { title: "墙上还没有需求", hint: "会后 AI 会从纪要里抽需求候选，放进待认领" },
  done: { title: "还没有已完成的需求", hint: "做完的需求在修改需求里改成已完成" },
  shelved: { title: "还没有搁置的需求", hint: "暂时不跟进的需求在修改需求里改成已搁置" },
  all: { title: "墙上还没有需求", hint: "会后 AI 会从纪要里抽需求候选，放进待认领" },
};

interface RequirementPoolPageProps {
  apiClient: ApiClient;
  canWrite: boolean;
  /** 认领、新建后回到需求池时要提示的话，显示一次 */
  /** App 带过来的提示：认领、新建、从认领页合并之后（高亮哪张、能不能撤销见 PoolFlash） */
  flash?: PoolFlash | null;
  onFlashShown?: () => void;
  onOpenRequirement: (requirementId: string) => void;
  onClaimCandidate: (candidateId: string) => void;
  onCreateRequirement: () => void;
  onOpenMeeting: (meetingId: string, atMs: number) => void;
  onProjectsChanged?: () => void | Promise<void>;
}

// 本机记着的值可能被扩展、手改或旧版本写坏：形状不对就当没记，不能让整页白屏
const isTab = (value: unknown) => TABS.some((item) => item.key === value);
const isStringList = (value: unknown) => Array.isArray(value) && value.every((item) => typeof item === "string");
const isPriorityList = (value: unknown) =>
  Array.isArray(value) && value.every((item) => (REQUIREMENT_PRIORITIES as readonly unknown[]).includes(item));
const isString = (value: unknown) => typeof value === "string";
export const POOL_TAB_STORE = { local: true, valid: isTab } as const;
export const POOL_PROJECTS_STORE = { local: true, valid: isStringList } as const;
export const POOL_PRIORITIES_STORE = { local: true, valid: isPriorityList } as const;
export const POOL_QUERY_STORE = { local: true, valid: isString } as const;

function toggled<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

/**
 * 需求池海报墙（R02）：一条需求一张海报，按「我的方向」座次 → P0～P3 → 最近会议排（排序在后端）。
 * 三级筛选：状态页签、项目（「我的方向」条上点选，多选）、优先级（多选），另有需求名称搜索。
 * 待认领页签挂的是 AI 从纪要里抽出的候选，可以认领、合并或丢掉（R01）。
 */
export function RequirementPoolPage({
  apiClient,
  canWrite,
  flash,
  onFlashShown,
  onOpenRequirement,
  onClaimCandidate,
  onCreateRequirement,
  onOpenMeeting,
  onProjectsChanged,
}: RequirementPoolPageProps) {
  const { toastNode, showToast } = useToast(3000);
  const [tab, setTab] = usePersistentState<PoolTab>(POOL_TAB_KEY, "active", POOL_TAB_STORE);
  const [projectIds, setProjectIds] = usePersistentState<string[]>(POOL_PROJECTS_KEY, [], POOL_PROJECTS_STORE);
  const [priorities, setPriorities] = usePersistentState<RequirementPriority[]>(
    POOL_PRIORITIES_KEY,
    [],
    POOL_PRIORITIES_STORE,
  );
  const [query, setQuery] = usePersistentState(POOL_QUERY_KEY, "", POOL_QUERY_STORE);
  const [draftQuery, setDraftQuery] = useState(query);
  const [payload, setPayload] = useState<RequirementPoolPayload | null>(null);
  const [failed, setFailed] = useState(false);
  const [merging, setMerging] = useState<PoolItem | null>(null);
  const [showDropped, setShowDropped] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const requestRef = useRef(0);
  const wallRef = useRef<HTMLDivElement>(null);
  // 认领、新建回来要高亮的那张：墙面还没取到时先记在 pending，海报挂上去了才开始描边、计 3 秒
  const [pendingHighlight, setPendingHighlight] = useState<string | null>(null);
  const [highlightId, setHighlightId] = useState<string | null>(null);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    try {
      const next = await apiClient.requirementPool({
        status: tab,
        project_id: projectIds.join(",") || undefined,
        priority: priorities.join(",") || undefined,
        q: query.trim() || undefined,
        limit: WALL_LIMIT,
      });
      // 筛选连着点的时候只认最后一次的结果
      if (request !== requestRef.current) return;
      setPayload(next);
      setFailed(false);
      // 记着的项目已经删掉、合并掉，或者是条上没有的「未归项目」：条上看不到它被选着，墙却被它筛了，去掉
      const known = new Set(next.projects.map((project) => project.id));
      if (projectIds.some((id) => !known.has(id))) {
        setProjectIds((current) => current.filter((id) => known.has(id)));
      }
    } catch {
      if (request === requestRef.current) setFailed(true);
    }
  }, [apiClient, priorities, projectIds, query, tab]);

  useEffect(() => {
    void load();
  }, [load]);

  // 名称搜索打字时不每个字都查，停下来 300ms 再查
  useEffect(() => {
    if (draftQuery === query) return;
    const timer = window.setTimeout(() => setQuery(draftQuery), 300);
    return () => window.clearTimeout(timer);
  }, [draftQuery, query, setQuery]);

  const filtered = projectIds.length > 0 || priorities.length > 0 || query.trim() !== "";
  const clearFilters = () => {
    setProjectIds([]);
    setPriorities([]);
    setQuery("");
    setDraftQuery("");
  };

  const refreshAfterChange = async () => {
    await load();
    await onProjectsChanged?.();
  };

  // 提示上的［撤销］停 10 秒，期间页签、筛选可能已经换了：回调里用最新的刷新，别拿点击那一刻的旧筛选重新取
  const refreshRef = useRef(refreshAfterChange);
  useEffect(() => {
    refreshRef.current = refreshAfterChange;
  });

  // 合并后撤销（R01-14）：成功回到待认领，失败（多半过了 10 分钟）提示后端给的原因；两种都把墙换成最新的
  const undoMerge = useCallback(
    async (candidateId: string) => {
      try {
        const result = await apiClient.undoCandidateMerge(candidateId);
        showToast(unmergedMessage(result?.kept_task_count));
      } catch (err) {
        showToast(err instanceof Error ? err.message : "撤销失败，请稍后重试");
      }
      await refreshRef.current();
    },
    [apiClient, showToast],
  );

  // 丢掉后撤销（R01-15）：和「已丢掉」里的撤销是同一个接口
  const undoDrop = useCallback(
    async (item: PoolItem) => {
      try {
        await apiClient.restoreCandidate(item.id);
        showToast(`「${item.title}」回到待认领了`);
      } catch (err) {
        showToast(err instanceof Error ? err.message : "撤销失败，请稍后重试");
      }
      await refreshRef.current();
    },
    [apiClient, showToast],
  );

  useEffect(() => {
    if (!flash) return;
    const mergedCandidateId = flash.undoMergeCandidateId;
    showToast(flash.message, mergedCandidateId ? { onUndo: () => undoMerge(mergedCandidateId) } : undefined);
    if (flash.highlightId) setPendingHighlight(flash.highlightId);
    onFlashShown?.();
  }, [flash, onFlashShown, showToast, undoMerge]);

  // 墙面取到后：要高亮的海报在墙上就滚进视野、描边 3 秒；不在当前列表里（筛掉了、在别的页签）就不管，不报错
  useEffect(() => {
    if (!pendingHighlight || !payload) return;
    setPendingHighlight(null);
    const node = findPoster(wallRef.current, pendingHighlight);
    if (!node) return;
    const reduceMotion = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView?.({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
    setHighlightId(pendingHighlight);
  }, [pendingHighlight, payload]);

  useEffect(() => {
    if (!highlightId) return;
    const timer = window.setTimeout(() => setHighlightId(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [highlightId]);

  const saveSeats = async (ids: string[]) => {
    try {
      await apiClient.saveProjectSeats(ids);
      showToast("座次已保存");
      await refreshAfterChange();
    } catch {
      // 多半是项目在别处改过（删掉、合并、另一个窗口排过座次）：重新取一遍，用新的一排再拖
      showToast("座次没保存：项目有变化，已刷新，请再拖一次");
      await load();
    }
  };

  const dropCandidate = async (item: PoolItem) => {
    setBusyId(item.id);
    try {
      await apiClient.dropCandidate(item.id);
      showToast(`已丢掉「${item.title}」，30 天内可在已丢掉里撤销`, { onUndo: () => undoDrop(item) });
      await load();
    } catch (err) {
      // 多半是在别处已经认领、合并或丢掉了：提示原因，墙上换成最新的
      showToast(err instanceof Error ? err.message : "丢掉失败，请稍后重试");
      await load();
    } finally {
      setBusyId(null);
    }
  };

  const openItem = (item: PoolItem) => {
    if (item.kind === "candidate") onClaimCandidate(item.id);
    else onOpenRequirement(item.id);
  };

  // 「接下」（R02-9）：把需求背景复制出去，交给 Claude Code 去干；不改需求状态。
  // 这个函数由点击处理函数同步调用，复制在手势里发起（见 requirementCopy）
  const takeRequirement = async (item: PoolItem): Promise<boolean> => {
    try {
      const context = await copyRequirementBackground(() => apiClient.requirementContext(item.id));
      showToast(copiedMessage(context));
      return true;
    } catch (error) {
      showToast(`没复制成功：${copyFailureReason(error)}`);
      return false;
    }
  };

  const counts = payload?.counts;
  const items = payload?.items ?? [];
  const total = payload?.total ?? 0;
  const empty = EMPTY_TEXT[tab];

  return (
    <section className="page-content pool-page">
      {toastNode}
      <header className="pool-head">
        <div className="pool-head__title">
          <span className="pool-eyebrow">REQUIREMENTS / 悬赏墙</span>
          <div className="pool-head__row">
            <h1>需求池</h1>
            <p>
              会上提出来、等你接下的事。先按「我的方向」排项目，同一项目里等级高的挂在前面。
              <br />
              点原话时间，回到录音里那一秒。
            </p>
          </div>
        </div>
        <div className="pool-head__side">
          <div className="pool-count">
            <strong>{total}</strong>
            <span>
              {tab === "pending" ? "条候选" : "张挂在墙上"}
              <br />
              {TABS.find((item) => item.key === tab)?.label}
            </span>
          </div>
          {canWrite && (
            <button className="pool-create" onClick={onCreateRequirement} type="button">
              <span aria-hidden="true">＋</span> 新建需求
            </button>
          )}
        </div>
      </header>

      <div className="pool-filters">
        <div className="pool-tabs" role="tablist">
          {TABS.map((item) => (
            <button
              aria-selected={tab === item.key}
              className={item.key === "pending" && (counts?.pending ?? 0) > 0 ? "has-dot" : undefined}
              key={item.key}
              onClick={() => setTab(item.key)}
              role="tab"
              type="button"
            >
              {item.label}
              <span>{counts ? counts[item.key] : "–"}</span>
            </button>
          ))}
        </div>
        <div className="pool-filters__right">
          <span className="pool-filters__label">优先级</span>
          <div aria-label="优先级" className="pool-priorities" role="group">
            {REQUIREMENT_PRIORITIES.map((value) => (
              <button
                aria-pressed={priorities.includes(value)}
                key={value}
                onClick={() => setPriorities((current) => toggled(current, value))}
                type="button"
              >
                {value}
              </button>
            ))}
          </div>
          {filtered && (
            <button className="pool-clear" onClick={clearFilters} type="button">
              清空筛选
            </button>
          )}
          <label className="pool-search">
            <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 16 16" width="14">
              <circle cx="7" cy="7" r="5" />
              <path d="M11 11l3.5 3.5" strokeLinecap="round" />
            </svg>
            <input
              aria-label="搜需求名称"
              onChange={(event) => setDraftQuery(event.target.value)}
              placeholder="搜需求名称"
              value={draftQuery}
            />
          </label>
        </div>
      </div>

      {payload && (
        <DirectionBar
          canWrite={canWrite}
          onSeatsChange={saveSeats}
          onToggle={(projectId) => setProjectIds((current) => toggled(current, projectId))}
          projects={payload.projects}
          selected={projectIds}
        />
      )}

      {failed && !payload && (
        <div className="pool-state pool-state--error" role="alert">
          需求池读取失败
          <button onClick={() => void load()} type="button">
            重试
          </button>
        </div>
      )}
      {!payload && !failed && <div className="pool-state">正在读取需求池…</div>}

      {payload && items.length === 0 && (
        <div className="pool-empty">
          <span aria-hidden="true" className="pool-empty__poster">
            <span />
          </span>
          {filtered ? (
            <>
              <p className="pool-empty__title">没有符合筛选条件的需求</p>
              <button className="pool-empty__action is-secondary" onClick={clearFilters} type="button">
                清空筛选
              </button>
            </>
          ) : (
            <>
              <p className="pool-empty__title">{empty.title}</p>
              <p className="pool-empty__hint">{empty.hint}</p>
              {canWrite && (tab === "active" || tab === "all") && (
                <button className="pool-empty__action" onClick={onCreateRequirement} type="button">
                  <span aria-hidden="true">＋</span> 新建需求
                </button>
              )}
            </>
          )}
        </div>
      )}

      {items.length > 0 && (
        <div aria-busy={busyId !== null} className="pool-wall" ref={wallRef}>
          {items.map((item) => (
            <PosterCard
              canWrite={canWrite && busyId !== item.id}
              highlighted={highlightId === item.id}
              item={item}
              key={`${item.kind}-${item.id}`}
              onClaim={(target) => onClaimCandidate(target.id)}
              onDrop={(target) => void dropCandidate(target)}
              onMerge={setMerging}
              onOpen={openItem}
              onOpenMeeting={onOpenMeeting}
              onTake={takeRequirement}
            />
          ))}
        </div>
      )}

      {tab === "pending" && payload && payload.dropped_count > 0 && (
        <p className="pool-dropped">
          <button onClick={() => setShowDropped(true)} type="button">
            已丢掉 {payload.dropped_count} 条
          </button>
          <span> · 30 天内可撤销</span>
        </p>
      )}

      {merging && (
        <MergeCandidateDialog
          apiClient={apiClient}
          candidate={merging}
          onClose={() => {
            // 没合成（取消，或候选在别处已经处理了）：墙上换成最新的
            setMerging(null);
            void load();
          }}
          onMerged={(requirement) => {
            const candidateId = merging.id;
            setMerging(null);
            showToast(mergedMessage(requirement.title), { onUndo: () => undoMerge(candidateId) });
            void refreshAfterChange();
          }}
        />
      )}
      {showDropped && (
        <DroppedCandidatesDialog
          apiClient={apiClient}
          onClose={() => setShowDropped(false)}
          onRestored={(title) => {
            showToast(`「${title}」回到待认领了`);
            void load();
          }}
        />
      )}
    </section>
  );
}
