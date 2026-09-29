import type { GlossaryScope, GlossaryTerm } from "../types";
import { Highlight, IconPlus, IconTarget, IconTrash } from "./glossaryUi";

export interface TermSection {
  key: string;
  /** 「全部」视图下才有分组头 */
  chip: GlossaryScope | null;
  items: GlossaryTerm[];
}

interface TermListProps {
  sections: TermSection[];
  /** all：按范围分组平铺；project：项目里，第四列是「识别」；other：公共 / 旧分组，没有第四列 */
  layout: "all" | "project" | "other";
  needle: string;
  selectedId: string | null;
  canWrite: boolean;
  onSelect: (id: string) => void;
  onDelete: (term: GlossaryTerm) => void;
  onNewIn: (chip: GlossaryScope) => void;
}

/** 列表里的错写：一行放不下的用「+N」收起，详情里看全部 */
function inlineWrongs(aliases: string[]) {
  let used = 0;
  const shown: string[] = [];
  for (const alias of aliases) {
    if (used + alias.length > 13 && shown.length > 0) break;
    shown.push(alias);
    used += alias.length + 1;
  }
  return { shown, rest: aliases.length - shown.length };
}

function OwnerCell({ term }: { term: GlossaryTerm }) {
  if (term.project_id) {
    return (
      <span className="gw-o">
        <i className="gw-dot" style={{ background: term.project_color ?? "var(--muted-2)" }} />
        <span>{term.project_name ?? term.scope}</span>
      </span>
    );
  }
  if (!term.scope || term.scope === "通用") {
    return (
      <span className="gw-o">
        <span>公共</span>
      </span>
    );
  }
  return (
    <span className="gw-o">
      <i className="gw-dot gw-dot--hollow" />
      <span>{term.scope}</span>
    </span>
  );
}

export function GlossaryTermList({ sections, layout, needle, selectedId, canWrite, onSelect, onDelete, onNewIn }: TermListProps) {
  const fourth = layout === "all" ? "归属" : layout === "project" ? "识别" : "";
  return (
    <div aria-label="词条" role="listbox">
      <div className={`gw-colh gw-cols gw-cols--${layout}`}>
        <span>正确写法</span>
        <span>错写（会被改正）</span>
        <span>分类</span>
        <span>{fourth}</span>
        <span />
      </div>
      {sections.map((section) => (
        <section key={section.key}>
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
          {section.items.map((term) => {
            const { shown, rest } = inlineWrongs(term.aliases);
            const also = term.also ?? [];
            return (
              <div
                aria-selected={selectedId === term.id}
                className={`gw-row gw-cols gw-cols--${layout}`}
                key={term.id}
                onClick={() => onSelect(term.id)}
                role="option"
              >
                <span className="gw-t">
                  <Highlight needle={needle} text={term.term} />
                </span>
                <span className="gw-w">
                  {shown.length > 0 && (
                    <>
                      <span className="gw-w__arr">←</span>
                      {shown.map((alias, index) => (
                        <span key={alias}>
                          {index > 0 && <span className="gw-w__sep">·</span>}
                          <Highlight needle={needle} text={alias} />
                        </span>
                      ))}
                      {rest > 0 && <span className="gw-w__more">+{rest}</span>}
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
                <span className="gw-c">{term.category}</span>
                {layout === "all" ? (
                  <OwnerCell term={term} />
                ) : layout === "project" ? (
                  <span className="gw-cue" title={term.is_cue ? "用来识别项目" : undefined}>
                    {term.is_cue ? <IconTarget /> : null}
                  </span>
                ) : (
                  <span />
                )}
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
            );
          })}
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
          <span />
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
