import { useLayoutEffect, useRef, useState } from "react";
import type { GlossaryScope, GlossaryTerm } from "../types";
import { Highlight, IconPlus, IconTrash } from "./glossaryUi";

export interface TermSection {
  key: string;
  /** 「全部」视图下才有分组头 */
  chip: GlossaryScope | null;
  items: GlossaryTerm[];
}

interface TermListProps {
  sections: TermSection[];
  /** all：按范围分组平铺（归属看分组头）；project：项目里；other：公共 / 旧分组 */
  layout: "all" | "project" | "other";
  needle: string;
  selectedId: string | null;
  canWrite: boolean;
  onSelect: (id: string) => void;
  onDelete: (term: GlossaryTerm) => void;
  onNewIn: (chip: GlossaryScope) => void;
}

/** 量不到宽度时（测试环境、还没排版）按字数估一行放几块 */
function guessFit(aliases: string[]) {
  let used = 0;
  let count = 0;
  for (const alias of aliases) {
    if (used + alias.length > 13 && count > 0) break;
    count += 1;
    used += alias.length + 1;
  }
  return count;
}

// 和 GlossaryWorkbench.css 里 .gw-w 那几条对着改
const ARROW_GAP = 8; // .gw-w__arr 的 margin-right
const CHIP_GAP = 5; // 相邻两块之间，记在后一块的 margin-left
const CHIP_CHROME = 14; // .gw-w__chip 左右 padding 6 加边框 1
const ONE_CHAR = 13; // 块里一个字
/** 「+N」连同左边距 6，12px 的字每个约 7px */
const moreRoom = (count: number) => 6 + 7 * (String(count).length + 1);

/** 一格放几块错写。chips 是每块完整显示要的宽度，跟现在收起了几块无关，算出来才不会来回跳。
 *  全放得下就全放，放不下的后面留出「+N」；第一块都放不全时，还能露出一个字就压着放、让它自己出省略号，
 *  再窄就一块不放，只留「← +N」。只有一块时总是放它，没有别的可显示 */
export function fitWrongChips(width: number, arrow: number, chips: number[]): number {
  const start = arrow + ARROW_GAP;
  let used = start;
  let count = 0;
  for (const [index, chipWidth] of chips.entries()) {
    const rest = chips.length - index - 1;
    if (used + chipWidth + (rest > 0 ? moreRoom(rest) : 0) > width) break;
    count = index + 1;
    used += chipWidth + CHIP_GAP;
  }
  if (count > 0) return count;
  if (chips.length <= 1) return chips.length;
  return start + CHIP_CHROME + ONE_CHAR + moreRoom(chips.length - 1) <= width ? 1 : 0;
}

/** 块里文字排开的宽度加外壳：块被压窄、被收起时量出来也一样 */
function chipWidth(chip: HTMLElement) {
  const range = document.createRange();
  range.selectNodeContents(chip);
  const text = range.getBoundingClientRect?.().width ?? 0;
  return text ? text + CHIP_CHROME : 0;
}

/** 列表里的错写：按这一格的实际宽度放，放不下的收成「+N」，详情里看全部。
 *  收起的块不显示但留在格子里（.gw-w__chip--spare），列宽变了能直接量出多放几块 */
function WrongsCell({ aliases, also, needle }: { aliases: string[]; also: string[]; needle: string }) {
  const cellRef = useRef<HTMLSpanElement>(null);
  // squeeze：第一块放不全，允许它压窄出省略号；平时不许压，免得被后面「也叫」的间距挤掉几个字
  const [{ fit, squeeze }, setLayout] = useState(() => ({ fit: guessFit(aliases), squeeze: false }));
  useLayoutEffect(() => {
    const cell = cellRef.current;
    if (!cell || aliases.length === 0) return;
    const measure = () => {
      // 用带小数的宽度：几块取整后的误差攒起来，会让最后一块被挤掉
      const width = cell.getBoundingClientRect().width;
      const arrow = cell.querySelector<HTMLElement>(".gw-w__arr")?.getBoundingClientRect().width ?? 0;
      const chips = [...cell.querySelectorAll<HTMLElement>(".gw-w__chip")].map(chipWidth);
      if (!width || !arrow || chips.some((each) => !each)) return;
      const next = fitWrongChips(width, arrow, chips);
      const hidden = chips.length - next;
      const tight = next === 1 && arrow + ARROW_GAP + chips[0] + (hidden > 0 ? moreRoom(hidden) : 0) > width;
      setLayout((prev) => (prev.fit === next && prev.squeeze === tight ? prev : { fit: next, squeeze: tight }));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    // 列宽变了要重算；块自己变宽（网页字体晚到、搜索高亮）也要
    const observer = new ResizeObserver(measure);
    observer.observe(cell);
    cell.querySelectorAll(".gw-w__chip").forEach((chip) => observer.observe(chip));
    return () => observer.disconnect();
  }, [aliases]);

  const rest = aliases.length - fit;
  return (
    <span className="gw-w" ref={cellRef}>
      {aliases.length > 0 && (
        <>
          <span aria-hidden="true" className="gw-w__arr">
            ←
          </span>
          {aliases.map((alias, index) => (
            <span
              className={`gw-w__chip${index >= fit ? " gw-w__chip--spare" : index === 0 && squeeze ? " gw-w__chip--squeeze" : ""}`}
              key={alias}
            >
              <Highlight needle={needle} text={alias} />
            </span>
          ))}
          {rest > 0 && (
            <span className="gw-w__more" title={`还有 ${rest} 个错写，详情里看全部`}>
              +{rest}
            </span>
          )}
        </>
      )}
      {also.length > 0 && (
        <span className="gw-w__also">
          也叫{" "}
          {also.map((name, index) => (
            <span key={name}>
              {index > 0 && "、"}
              <Highlight needle={needle} text={name} />
            </span>
          ))}
        </span>
      )}
    </span>
  );
}

export function GlossaryTermList({ sections, layout, needle, selectedId, canWrite, onSelect, onDelete, onNewIn }: TermListProps) {
  return (
    <div aria-label="词条" role="listbox">
      <div className={`gw-colh gw-cols gw-cols--${layout}`}>
        <span>正确写法 · 分类</span>
        <span>
          <span aria-hidden="true">←</span> 错写（会被改正）
        </span>
        <span />
      </div>
      {sections.map((section) => (
        // 行里不再写归属，读屏靠分组名知道这一组属于哪
        <section aria-label={section.chip?.label} key={section.key} role={section.chip ? "group" : undefined}>
          {section.chip && (
            <div className="gw-gh">
              {section.chip.kind === "general" ? null : (
                <i
                  className={`gw-dot${section.chip.kind === "bucket" ? " gw-dot--hollow" : ""}`}
                  style={section.chip.color ? { background: section.chip.color } : undefined}
                />
              )}
              <strong>{section.chip.label}</strong>
              <span className="gw-gh__n">{section.items.length}</span>
              <span className="gw-gh__sp" />
              {canWrite && (
                <button className="gw-lb" onClick={() => onNewIn(section.chip!)} type="button">
                  ＋ 在这里新增
                </button>
              )}
            </div>
          )}
          {section.items.map((term) => (
            <div
              aria-selected={selectedId === term.id}
              className={`gw-row gw-cols gw-cols--${layout}`}
              key={term.id}
              onClick={() => onSelect(term.id)}
              role="option"
            >
              <span className="gw-t">
                <span className="gw-t__name">
                  <Highlight needle={needle} text={term.term} />
                </span>
                <span className="gw-c">{term.category}</span>
              </span>
              {/* 错写改了就按新内容重新估一次放几块 */}
              <WrongsCell aliases={term.aliases} also={term.also ?? []} key={term.aliases.join("\n")} needle={needle} />
              {canWrite ? (
                <button
                  aria-label={`删除『${term.term}』`}
                  className="gw-rdel"
                  onClick={(event) => {
                    event.stopPropagation();
                    onDelete(term);
                  }}
                  type="button"
                >
                  <IconTrash />
                </button>
              ) : (
                <span />
              )}
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

export function TermListSkeleton() {
  return (
    <div aria-busy="true" aria-label="正在载入词典">
      {Array.from({ length: 14 }, (_, index) => (
        <div className="gw-row gw-cols gw-cols--all" key={index}>
          <span className="gw-skel" style={{ width: 60 + ((index * 37) % 60) }} />
          <span className="gw-skel" style={{ width: (index * 53) % 140 }} />
          <span />
        </div>
      ))}
      <p className="gw-note">正在载入词典…</p>
    </div>
  );
}

export function NewTermButton({ onClick, label = "新增术语" }: { onClick: () => void; label?: string }) {
  return (
    <button className="gw-btn gw-btn--pri" onClick={onClick} type="button">
      <IconPlus />
      {label}
    </button>
  );
}
