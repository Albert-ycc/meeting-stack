/*
 * 全部项目概览的舞台：倾斜的星图（Canvas2D，overviewScene.ts 驱动）+ 正对屏幕的 DOM 名字、琥珀数、圈名和读数。
 * 横拖转盘、纵拖调仰角、滚轮缩放，0 键 / 双击空白 / 复位按钮复位；点行星镜头飞入后进项目图，点琥珀数开右侧面板；
 * 左上是港湾（没归项目的会）和像新项目的名字，右上是还没挂的文件夹；「在图上找」按 / 聚焦。
 */
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { useTheme } from "../../theme";
import {
  BELT_ID,
  OVERVIEW_REGION_NAMES,
  RING_NAMES,
  monthDay,
  sunCountText,
  type IslandNode,
  type OverviewLayout,
  type OverviewNode,
  type OverviewRegion,
} from "./layoutOverview";
import { breakName } from "./overviewLabels";
import { sizeKeyValues } from "./overviewRenderer";
import { OverviewScene, type SceneElements } from "./overviewScene";
import type { GraphOverview, OverviewFolders, OverviewHarbour, OverviewIsland } from "./overviewTypes";
import "./OverviewGraph.css";

/** 这几类节点选中时开右侧面板（项目是点琥珀数、按 N 键或深链选中；点项目本身是飞进项目图） */
export const PANEL_KINDS = new Set<OverviewNode["kind"]>(["island", "belt", "harbour", "ghost", "folder", "folder_hint"]);

const HARBOUR_LABELS: Array<{ key: keyof OverviewHarbour["counts"]; label: string }> = [
  { key: "ai_pending", label: "等 AI 判断" },
  { key: "needs_review", label: "待你选" },
  { key: "none", label: "AI 没认出" },
  { key: "new_project", label: "像新项目" },
];

/** 港湾上的两行：「9 场没归项目的会」和四种状态的数（是 0 的不写） */
export function harbourLines(harbour: OverviewHarbour): { head: string; counts: string } {
  const head = harbour.ai_configured
    ? harbour.total
      ? `${harbour.total} 场没归项目的会`
      : "这段时间没有没归项目的会"
    : `没配置 AI：${harbour.total} 场会要你自己选项目`;
  const counts = HARBOUR_LABELS.filter((item) => harbour.counts[item.key] > 0)
    .map((item) => `${item.label} ${harbour.counts[item.key]}`)
    .join(" · ");
  return { head, counts };
}

/** 琥珀数悬停时的说明：「2 场归属待复核、1 场可能是这个项目的、3 条任务待确认」 */
function waitingTitle(island: OverviewIsland): string {
  return [
    island.waiting.review ? `${island.waiting.review} 场归属待复核` : "",
    island.waiting.doorstep ? `${island.waiting.doorstep} 场可能是这个项目的` : "",
    island.waiting.tasks ? `${island.waiting.tasks} 条任务待确认` : "",
  ]
    .filter(Boolean)
    .join("、");
}

const pad2 = (value: number) => String(value).padStart(2, "0");

/** 方向键转视角：每按一下方位角转 10°、仰角调 3° */
const ARROW_TURN: Record<string, [number, number]> = {
  ArrowLeft: [-10, 0],
  ArrowRight: [10, 0],
  ArrowUp: [0, 3],
  ArrowDown: [0, -3],
};

/** 名字后面那一小段：窗口内有会写场次，没有写几天前，从没开过会写「没开过会」 */
function labelMeta(node: IslandNode): ReactNode {
  if (!node.data.last_day) return "没开过会";
  if (node.data.meetings > 0) {
    return (
      <>
        <span className="num">{node.data.meetings}</span> 场
      </>
    );
  }
  return (
    <>
      <span className="num">{node.age ?? 0}</span> 天前
    </>
  );
}

/** 名字的先后（DOM 顺序、Tab 默认停在第一个）：和名字预算同一个优先级 */
function byPriority(a: IslandNode, b: IslandNode) {
  return (
    Number(b.waiting > 0) - Number(a.waiting > 0) ||
    b.data.meetings - a.data.meetings ||
    (a.age ?? 1e9) - (b.age ?? 1e9) ||
    a.ring - b.ring ||
    a.rank - b.rank
  );
}

function HoverCard({ node, overview }: { node: IslandNode; overview: GraphOverview }) {
  const island = node.data;
  const days = overview.window.days;
  const parts = [
    island.waiting.review ? `待复核的会 ${island.waiting.review}` : "",
    island.waiting.doorstep ? `门口的会 ${island.waiting.doorstep}` : "",
    island.waiting.tasks ? `待确认任务 ${island.waiting.tasks}` : "",
  ].filter(Boolean);
  const last = island.last_day;
  return (
    <>
      <div className="star-card__eyebrow">
        <i style={{ background: island.color }} />
        <span className="num">R{node.ring + 1}</span>
        {RING_NAMES[node.ring]}
      </div>
      <div className="star-card__title">{island.name}</div>
      <dl>
        <dt>{days ? `这 ${days} 天` : "一共"}</dt>
        <dd>
          <span className="num">{island.meetings}</span> 场会
        </dd>
        <dt>最近一次</dt>
        <dd>
          {last ? (
            <>
              {node.age === 0 ? "今天" : <><span className="num">{node.age}</span> 天前</>} · {monthDay(last)}
            </>
          ) : (
            "还没开过会"
          )}
        </dd>
        <dt>在等你</dt>
        <dd className={node.waiting ? "is-warn" : undefined}>
          {node.waiting ? (
            <>
              <span className="num">{node.waiting}</span> 件 · {parts.join("，")}
            </>
          ) : (
            "没有"
          )}
        </dd>
        {island.stopped_cards > 0 && (
          <>
            <dt>停了</dt>
            <dd>
              <span className="num">{island.stopped_cards}</span> 张卡片
            </dd>
          </>
        )}
      </dl>
      <div className="star-card__foot">
        点击进入项目图
        {node.waiting > 0 && (
          <>
            <br />
            点琥珀数看在等你的事
          </>
        )}
      </div>
    </>
  );
}

export interface OverviewCanvasProps {
  overview: GraphOverview;
  layout: OverviewLayout;
  /** 没挂的文件夹；还没取到时为 null */
  folders: OverviewFolders | null;
  selectedId: string | null;
  panelOpen: boolean;
  /** 总文件夹没设（空状态上给［选项目总文件夹…］） */
  parentUnset: boolean;
  /** N 键依次跳的节点；没有时调 onNothingToDo */
  attention: string[];
  /** 面板的小行星带列表里指着的那个项目：图上给它四角括号 */
  peekId: string | null;
  onSelect: (id: string | null) => void;
  /** 飞入结束：进项目图 */
  onOpenIsland: (projectId: string) => void;
  /** 「还有 N 个像新项目的名字」：进资料库「像新项目」筛选 */
  onOpenMoreNames: () => void;
  /** 「还有 N 个」文件夹：打开认领框 */
  onOpenMoreFolders: () => void;
  onPickParent: () => void;
  onNothingToDo: () => void;
}

export function OverviewCanvas({
  overview,
  layout,
  folders,
  selectedId,
  panelOpen,
  parentUnset,
  attention,
  peekId,
  onSelect,
  onOpenIsland,
  onOpenMoreNames,
  onOpenMoreFolders,
  onPickParent,
  onNothingToDo,
}: OverviewCanvasProps) {
  const theme = useTheme().resolved;
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const findRef = useRef<HTMLInputElement>(null);
  const sceneRef = useRef<OverviewScene | null>(null);
  // 场景每帧直接改这些元素：同一份可变的表交给场景，渲染时由 ref 回调登记，元素换了场景也跟着换
  const [elements] = useState<Omit<SceneElements, "viewport" | "canvas">>(() => ({
    keyCanvas: null,
    labels: new Map(),
    badges: new Map(),
    bridgeLabels: new Map(),
    chips: [null, null, null],
    hud: [null, null, null, null, null, null, null],
    sunLabel: null,
    card: null,
    readout: { az: null, el: null, zoom: null, hint: null },
  }));
  const latest = useRef({ onOpenIsland, onSelect });
  latest.current = { onOpenIsland, onSelect };
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [entering, setEntering] = useState(false);
  const [query, setQuery] = useState("");
  const [foundCount, setFoundCount] = useState<number | null>(null);
  const [active, setActive] = useState<Partial<Record<OverviewRegion, string>>>({});

  const days = overview.window.days;
  const islands = useMemo(() => [...layout.islands].sort(byPriority), [layout.islands]);
  const harbourNodes = layout.nodes.filter((node) => node.region === "harbour");
  const folderNodes = layout.nodes.filter((node) => node.region === "folders");
  const folderCount = folders ? folders.folders.length + folders.more : 0;
  const keyValues = sizeKeyValues(Math.max(0, ...layout.islands.map((node) => node.data.meetings)));
  const empty = layout.islands.length === 0 && !layout.belt;
  const hovered = hoverId ? layout.byId.get(hoverId) : undefined;
  const hoverNode = hovered?.kind === "island" ? hovered : null;

  // 场景只建一次；舞台大小变了告诉它
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const canvas = canvasRef.current;
    if (!viewport || !canvas) return;
    const scene = new OverviewScene(Object.assign(elements, { viewport, canvas }), {
      onHover: setHoverId,
      onEnteringChange: setEntering,
      onEnter: (projectId) => latest.current.onOpenIsland(projectId),
      onSelect: (id) => latest.current.onSelect(id),
    });
    sceneRef.current = scene;
    const fit = () => scene.resize(viewport.clientWidth, viewport.clientHeight);
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(viewport);
    // Outfit 到了以后名字变宽一点，重量一次
    let alive = true;
    void document.fonts?.ready.then(() => alive && scene.measure());
    return () => {
      alive = false;
      observer.disconnect();
      scene.destroy();
      sceneRef.current = null;
    };
  }, [elements]);

  // 数据和时间窗：名字、场次换了，重量尺寸；在找的话按新数据再找一次
  useLayoutEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    scene.setData({ layout, harbour: overview.harbour, folderCount });
    scene.measure();
    if (query.trim()) setFoundCount(scene.find(query).count);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout, overview.harbour, folderCount]);

  useEffect(() => {
    sceneRef.current?.setTheme();
  }, [theme]);

  useEffect(() => {
    sceneRef.current?.setSelection(selectedId, panelOpen);
  }, [selectedId, panelOpen]);

  useEffect(() => {
    sceneRef.current?.setPeek(peekId);
  }, [peekId]);

  // 悬停卡片的内容换了，按新尺寸摆
  useLayoutEffect(() => {
    sceneRef.current?.positionCard();
  }, [hoverId, layout]);

  // / 键聚焦「在图上找」（在别的输入框里、开着弹窗时不抢）
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest?.("input, textarea, select, [contenteditable='true']")) return;
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;
      event.preventDefault();
      findRef.current?.focus();
      findRef.current?.select();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runFind = (value: string) => {
    setQuery(value);
    const result = sceneRef.current?.find(value);
    setFoundCount(value.trim() ? result?.count ?? 0 : null);
  };

  const onFindKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter") {
      event.preventDefault();
      if (sceneRef.current?.enterFound()) findRef.current?.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      runFind("");
      findRef.current?.blur();
    }
  };

  // 每个区一个 Tab 停靠点，区里用方向键走；记下的节点藏起来了（认领了、不是项目）就退回这个区的第一个
  const focusable = useMemo(
    () => [
      ...layout.nodes.filter((node) => node.region === "harbour"),
      ...islands,
      ...layout.nodes.filter((node) => node.region === "folders" && node.kind !== "folder_status"),
    ],
    [islands, layout.nodes],
  );
  const tabIndexFor = (node: OverviewNode) => {
    const remembered = active[node.region];
    const chosen =
      remembered && focusable.some((item) => item.id === remembered)
        ? remembered
        : focusable.find((item) => item.region === node.region)?.id;
    return chosen === node.id ? 0 : -1;
  };
  const remember = (node: OverviewNode) => setActive((current) => ({ ...current, [node.region]: node.id }));

  const focusNode = (id: string) => {
    const nodes = viewportRef.current?.querySelectorAll<HTMLElement>("[data-node-id]") ?? [];
    Array.from(nodes).find((element) => element.dataset.nodeId === id)?.focus();
  };

  /** 方向键在名字之间走：按这一帧名字的屏幕位置找那个方向上最近的；没摆过名字时按 DOM 顺序 */
  const moveFocus = (from: string, key: string) => {
    const centers = sceneRef.current?.labelCenters() ?? new Map<string, { x: number; y: number }>();
    const origin = centers.get(from);
    let next: string | undefined;
    if (origin) {
      let best = Number.POSITIVE_INFINITY;
      for (const [id, at] of centers) {
        if (id === from) continue;
        const dx = at.x - origin.x;
        const dy = at.y - origin.y;
        const along = key === "ArrowUp" ? -dy : key === "ArrowDown" ? dy : key === "ArrowLeft" ? -dx : dx;
        if (along <= 1) continue;
        const score = along + (key === "ArrowUp" || key === "ArrowDown" ? Math.abs(dx) : Math.abs(dy)) * 2;
        if (score < best) {
          best = score;
          next = id;
        }
      }
    } else {
      const at = islands.findIndex((node) => node.id === from);
      const step = key === "ArrowRight" || key === "ArrowDown" ? 1 : -1;
      next = islands[(at + step + islands.length) % islands.length]?.id;
    }
    if (next) {
      const node = layout.byId.get(next);
      if (node) remember(node);
      focusNode(next);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // 打中文时不触发单字母快捷键
    if (event.nativeEvent.isComposing || event.key === "Process") return;
    const target = event.target as HTMLElement;
    if (target.closest("select, input, textarea")) return;
    const scene = sceneRef.current;
    if (event.key === "Escape") {
      if (scene?.isEntering()) scene.cancelEnter();
      else if (!panelOpen && query) runFind("");
      else onSelect(null);
      return;
    }
    if ((event.key === "n" || event.key === "N") && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      if (attention.length === 0) {
        onNothingToDo();
        return;
      }
      const at = selectedId ? attention.indexOf(selectedId) : -1;
      const next = attention[(at + 1) % attention.length];
      const node = layout.byId.get(next);
      if (node) remember(node);
      onSelect(next);
      window.requestAnimationFrame(() => focusNode(next));
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "0") {
      event.preventDefault();
      scene?.resetView();
    } else if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      scene?.zoomBy(1.2);
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      scene?.zoomBy(1 / 1.2);
    } else if (event.key.startsWith("Arrow")) {
      event.preventDefault();
      // 焦点在名字上时在名字之间走；在舞台本身上时转视角（左右转盘，上下调仰角）
      const label = target.closest<HTMLElement>(".star-label")?.dataset.nodeId;
      if (label) moveFocus(label, event.key);
      else if (target === viewportRef.current) {
        const [daz, del] = ARROW_TURN[event.key] ?? [0, 0];
        scene?.rotateBy(daz, del);
      }
    }
  };

  const harbour = harbourNodes.find((node) => node.kind === "harbour");
  const lines = harbourLines(overview.harbour);
  const ghostItems = harbourNodes.filter((node) => node.kind === "ghost" || node.kind === "ghost_more");
  const hint = folderNodes.find((node) => node.kind === "folder_hint");
  const status = folderNodes.find((node) => node.kind === "folder_status");
  const folderItems = folderNodes.filter((node) => node.kind === "folder" || node.kind === "folder_more");
  const folderShown = folderNodes.filter((node) => node.kind === "folder").length;

  const select = (node: OverviewNode) => onSelect(node.id === selectedId ? null : node.id);

  return (
    <div
      aria-label="全部项目关系图"
      className={`overview-viewport${entering ? " is-entering" : ""}`}
      onKeyDown={onKeyDown}
      ref={viewportRef}
      role="application"
      tabIndex={0}
    >
      <canvas aria-hidden="true" className="star-canvas" ref={canvasRef} />

      <div className="star-finder" ref={(el) => void (elements.hud[0] = el)} role="search">
        <svg aria-hidden="true" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16">
          <circle cx="7" cy="7" r="4.8" />
          <path d="M10.6 10.6 14 14" />
        </svg>
        <input
          aria-label="在图上找项目，按 / 键聚焦"
          autoComplete="off"
          onChange={(event: ChangeEvent<HTMLInputElement>) => runFind(event.target.value)}
          onKeyDown={onFindKey}
          placeholder="在图上找项目"
          ref={findRef}
          spellCheck={false}
          type="text"
          value={query}
        />
        {foundCount !== null && (
          <span className="star-finder__count">
            {foundCount ? (
              <>
                <span className="num">{foundCount}</span> 个
              </>
            ) : (
              "没找到"
            )}
          </span>
        )}
        <kbd aria-hidden="true">/</kbd>
      </div>

      <div aria-hidden="true" className="star-hud-top" ref={(el) => void (elements.hud[1] = el)}>
        <span className="num">{overview.today}</span>
        {layout.latestAge !== null && (
          <>
            <i />
            <span>
              最近一次会 <b className="num">T−{pad2(layout.latestAge)}D</b>
            </span>
          </>
        )}
        <i />
        <span>
          <b className="num">{layout.projectCount}</b> 个项目
        </span>
      </div>

      {!empty && (
        <div className="star-layer">
          {layout.rings.map((ring, k) =>
            ring.belt && layout.belt ? (
              <button
                aria-label={layout.belt.label}
                aria-pressed={selectedId === BELT_ID}
                className="star-chip is-belt"
                data-node-id={BELT_ID}
                key="belt"
                onClick={() => select(layout.belt!)}
                ref={(el) => void (elements.chips[k] = el)}
                type="button"
              >
                <span className="star-chip__code num">R3</span>更早 · <span className="num">{ring.count}</span> 个项目
                <span aria-hidden="true" className="star-chip__chev">
                  ›
                </span>
              </button>
            ) : (
              <span aria-hidden="true" className="star-chip" key={ring.index} ref={(el) => void (elements.chips[k] = el)}>
                <span className="star-chip__code num">R{ring.index + 1}</span>
                {ring.name}
              </span>
            ),
          )}
        </div>
      )}

      {!empty && (
        <div
          aria-label={`全部项目：${sunCountText(days, layout.meetings)}`}
          className="star-sun"
          ref={(el) => void (elements.sunLabel = el)}
          role="note"
        >
          <b>全部项目</b>
          <span>
            {days ? (
              <>
                <span className="num">{days}</span> 天
              </>
            ) : (
              "全部"
            )}{" "}
            <span className="num">{layout.meetings}</span> 场会
          </span>
        </div>
      )}

      <div aria-label={OVERVIEW_REGION_NAMES.islands} className="star-layer" role="group">
        {layout.bridges.map((bridge) => (
          <span
            aria-hidden="true"
            className="star-bridge is-gone"
            key={bridge.id}
            ref={(el) => {
              if (el) elements.bridgeLabels.set(bridge.id, el);
              else elements.bridgeLabels.delete(bridge.id);
            }}
          >
            ×{bridge.count}
          </span>
        ))}
        {islands.map((node) => (
          <button
            aria-label={node.label}
            className="star-label"
            data-node-id={node.id}
            key={node.id}
            onBlur={() => sceneRef.current?.leaveHover(node.id)}
            onClick={() => sceneRef.current?.enter(node)}
            onFocus={() => {
              remember(node);
              sceneRef.current?.setHover(node);
            }}
            onPointerEnter={() => sceneRef.current?.setHover(node, "label")}
            onPointerLeave={() => sceneRef.current?.leaveHover(node.id)}
            ref={(el) => {
              if (el) elements.labels.set(node.id, el);
              else elements.labels.delete(node.id);
            }}
            tabIndex={tabIndexFor(node)}
            type="button"
          >
            <span className="star-label__name">
              {breakName(node.data.name).map((line, index) => (
                <Fragment key={index}>
                  {index > 0 && <br />}
                  {line}
                </Fragment>
              ))}
              <span className="star-label__meta">{labelMeta(node)}</span>
              {node.data.stopped_cards > 0 && (
                <span className="star-label__stop" title={`${node.data.stopped_cards} 张卡片停了`}>
                  ⊘
                </span>
              )}
            </span>
          </button>
        ))}
        {islands
          .filter((node) => node.waiting > 0)
          .map((node) => (
            <button
              aria-label={`${node.data.name}：${node.waiting} 件在等你，打开面板`}
              aria-pressed={node.id === selectedId}
              className="star-badge"
              key={`badge-${node.id}`}
              onClick={() => select(node)}
              onPointerEnter={() => sceneRef.current?.setHover(node, "badge")}
              onPointerLeave={() => sceneRef.current?.leaveHover(node.id)}
              ref={(el) => {
                if (el) elements.badges.set(node.id, el);
                else elements.badges.delete(node.id);
              }}
              tabIndex={-1}
              title={waitingTitle(node.data)}
              type="button"
            >
              {node.waiting}
            </button>
          ))}
      </div>

      {empty && (
        <div className="star-empty" role="note">
          <p>
            {parentUnset
              ? "还没有项目。设好项目总文件夹后，下面的文件夹可以直接建成项目"
              : "还没有项目。项目总文件夹下面的文件夹可以直接建成项目"}
          </p>
          {parentUnset && (
            <button className="star-dock__action" onClick={onPickParent} type="button">
              选项目总文件夹…
            </button>
          )}
        </div>
      )}

      <div aria-label={OVERVIEW_REGION_NAMES.harbour} className="star-dock star-dock--harbour" role="group">
        {/* 整块（左边画在画布上的小盘加右边的字）是一个按钮 */}
        {harbour && (
          <button
            aria-label={harbour.label}
            aria-pressed={selectedId === harbour.id}
            className="star-dock__hit"
            data-node-id={harbour.id}
            onClick={() => select(harbour)}
            onFocus={() => remember(harbour)}
            onPointerEnter={() => sceneRef.current?.setHoverDock(true)}
            onPointerLeave={() => sceneRef.current?.setHoverDock(false)}
            tabIndex={tabIndexFor(harbour)}
            type="button"
          >
            <span className="star-dock__text" ref={(el) => void (elements.hud[2] = el)}>
              <span className="star-dock__eyebrow">港湾</span>
              <span className={`star-dock__title${overview.harbour.ai_configured ? "" : " is-warn"}`}>{lines.head}</span>
              {lines.counts && <span className="star-dock__meta">{lines.counts}</span>}
            </span>
          </button>
        )}
        {ghostItems.length > 0 && (
          <div className="star-ghosts" ref={(el) => void (elements.hud[6] = el)}>
            <span className="star-dock__eyebrow">像新项目</span>
            {ghostItems.map((node) =>
              node.kind === "ghost" ? (
                <button
                  aria-label={node.label}
                  aria-pressed={selectedId === node.id}
                  className="star-ghost"
                  data-node-id={node.id}
                  key={node.id}
                  onClick={() => select(node)}
                  onFocus={() => remember(node)}
                  tabIndex={tabIndexFor(node)}
                  title={node.label}
                  type="button"
                >
                  『{node.data.name}』<span className="num">{node.data.meeting_count}</span> 场
                </button>
              ) : node.kind === "ghost_more" ? (
                <button
                  className="star-ghost is-more"
                  data-node-id={node.id}
                  key={node.id}
                  onClick={onOpenMoreNames}
                  onFocus={() => remember(node)}
                  tabIndex={tabIndexFor(node)}
                  type="button"
                >
                  {node.label}
                </button>
              ) : null,
            )}
          </div>
        )}
      </div>

      <div
        aria-label={OVERVIEW_REGION_NAMES.folders}
        className="star-dock star-dock--folder"
        ref={(el) => void (elements.hud[3] = el)}
        role="group"
      >
        {folders && <span className="star-dock__eyebrow">文件夹</span>}
        {hint && (
          <>
            <button
              aria-label={hint.label}
              aria-pressed={selectedId === hint.id}
              className="star-dock__note"
              data-node-id={hint.id}
              onClick={() => select(hint)}
              onFocus={() => remember(hint)}
              tabIndex={tabIndexFor(hint)}
              type="button"
            >
              设项目总文件夹后，这里会列出
              <br />
              还没挂的文件夹
            </button>
            <button className="star-dock__action" onClick={onPickParent} tabIndex={-1} type="button">
              选项目总文件夹…
            </button>
          </>
        )}
        {folderItems.length > 0 && (
          <>
            <span className="star-dock__title">
              <span className="num">{folderCount || folderShown}</span> 个文件夹还没挂到项目
            </span>
            <span className="star-folders">
              {folderItems.map((node) =>
                node.kind === "folder" ? (
                  <button
                    aria-label={node.label}
                    aria-pressed={selectedId === node.id}
                    className="star-folder"
                    data-node-id={node.id}
                    key={node.id}
                    onClick={() => select(node)}
                    onFocus={() => remember(node)}
                    tabIndex={tabIndexFor(node)}
                    title={node.data.path}
                    type="button"
                  >
                    {node.data.name}
                  </button>
                ) : node.kind === "folder_more" ? (
                  <button
                    className="star-folder is-more"
                    data-node-id={node.id}
                    key={node.id}
                    onClick={onOpenMoreFolders}
                    onFocus={() => remember(node)}
                    tabIndex={tabIndexFor(node)}
                    type="button"
                  >
                    还有 {node.data.count} 个
                  </button>
                ) : null,
              )}
            </span>
          </>
        )}
        {status && (
          <p className="star-dock__meta" role="status">
            {status.label}
          </p>
        )}
      </div>

      <div aria-label="缩放与视角" className="star-zoom" role="group">
        <button aria-label="放大" onClick={() => sceneRef.current?.zoomBy(1.2)} type="button">
          +
        </button>
        <button aria-label="缩小" onClick={() => sceneRef.current?.zoomBy(1 / 1.2)} type="button">
          −
        </button>
        <button aria-label="复位" onClick={() => sceneRef.current?.resetView()} type="button">
          0
        </button>
      </div>

      <div aria-hidden="true" className="star-readout" ref={(el) => void (elements.hud[4] = el)}>
        <span>
          方位 <b className="num" ref={(el) => void (elements.readout.az = el)}>000°</b>　仰角{" "}
          <b className="num" ref={(el) => void (elements.readout.el = el)}>32°</b>　缩放{" "}
          <b className="num" ref={(el) => void (elements.readout.zoom = el)}>1.00×</b>
        </span>
        <span className="star-readout__hint" ref={(el) => void (elements.readout.hint = el)}>
          按 <kbd>0</kbd> 或双击空白处复位
        </span>
      </div>

      {!empty && (
        <div className="star-key" ref={(el) => void (elements.hud[5] = el)}>
          <div className="star-key__head">
            <span>比例尺</span>
            <span>这段时间开了几场会</span>
          </div>
          <canvas aria-hidden="true" className="star-key__canvas" ref={(el) => void (elements.keyCanvas = el)} />
          {/* 数字标在比例尺每一档的正下方：横坐标和 overviewRenderer 的 sizeKeyX 同一个算法 */}
          <div className="star-key__nums">
            {keyValues.map((value, index) => (
              <span
                key={value}
                style={{
                  left: keyValues.length === 1 ? "50%" : `calc(22px + (100% - 44px) * ${index / (keyValues.length - 1)})`,
                }}
              >
                <b className="num">{value}</b>
                {index === keyValues.length - 1 ? " 场" : ""}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className={`star-card${hoverNode ? " is-on" : ""}`} ref={(el) => void (elements.card = el)} role="tooltip">
        {hoverNode && <HoverCard node={hoverNode} overview={overview} />}
      </div>
    </div>
  );
}
