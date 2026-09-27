import { useState } from "react";

import type { LoadState, Project, SearchPayload } from "../types";
import { AsyncState } from "./AsyncState";
import { SearchMaterials } from "./SearchMaterials";
import { SearchResults } from "./SearchResults";

const COUNT = new Intl.NumberFormat("en-US");

/** 「材料里的」标题下的灰字：没读完、索引在重建、预算用完、两个字的词只搜了文件名 */
export function materialNotes(result: SearchPayload | null, query: string, scope: string): string[] {
  const state = result?.material_state;
  if (!state || scope === "none") return [];
  const notes: string[] = [];
  if (state.pending > 0) notes.push(`还有 ${COUNT.format(state.pending)} 个材料没读完，结果可能不全`);
  if (state.rebuilding) notes.push("材料的全文索引在重建，结果可能不全");
  if (state.partial) notes.push("材料结果可能不全");
  if (!scope && query.trim().length > 0 && query.trim().length < 3) {
    notes.push("两个字的词只搜了文件名，选个项目能搜正文");
  }
  return notes;
}

interface SearchPageProps {
  result: SearchPayload | null;
  onOpen: (meetingId: string, startMs: number, tab?: "minutes") => void;
  query: string;
  state: LoadState;
  error?: string;
  /** "" 全部；"none" 没归项目的会；其他是项目 id */
  scope: string;
  projects: Project[];
  onScopeChange: (scope: string) => void;
  /** 点「也可以搜」里的写法：换成这个词再搜一次 */
  onSearchWord: (word: string) => void;
  /** 材料的［预览］和 ▶：打开预览抽屉，▶ 从那个时间开始放 */
  onOpenMaterial?: (fileId: number, startMs?: number) => void;
}

export function SearchPage({
  error,
  result,
  onOpen,
  onOpenMaterial,
  onScopeChange,
  onSearchWord,
  projects,
  query,
  scope,
  state,
}: SearchPageProps) {
  const items = result?.items ?? [];
  const similar = result?.similar ?? [];
  const expanded = result?.expanded ?? [];
  const hints = result?.expand_hints ?? [];
  const unattributed = scope && scope !== "none" ? result?.unattributed_hits ?? 0 : 0;
  // 范围选「没归项目的会」时不显示材料
  const materials = scope === "none" ? [] : result?.materials ?? [];
  const materialSimilar = scope === "none" ? [] : result?.material_similar ?? [];
  const notes = materialNotes(result, query, scope);
  const [notice, setNotice] = useState("");
  const nothing =
    state === "ready" &&
    items.length === 0 &&
    similar.length === 0 &&
    unattributed === 0 &&
    materials.length === 0 &&
    materialSimilar.length === 0;
  const meetingCount = `会议 ${COUNT.format(items.length)} 条`;

  return (
    <section className="search-page page-content">
      <header className="page-heading">
        <div>
          <span className="eyebrow">SEARCH</span>
          <h1>“{query}”</h1>
          <p>包含这个词的在前，意思相近的列在后面。纪要、标题和材料也一起搜。</p>
        </div>
        <div className="record-count record-count--text">
          <span>{materials.length > 0 ? `${meetingCount} · 材料 ${COUNT.format(materials.length)} 份` : meetingCount}</span>
        </div>
      </header>

      <div className="search-toolbar">
        <label className="search-scope">
          <span>范围</span>
          <select aria-label="搜索范围" onChange={(event) => onScopeChange(event.target.value)} value={scope}>
            <option value="">全部项目</option>
            <option value="none">没归项目的会</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.name}</option>
            ))}
          </select>
        </label>
        {state === "ready" && expanded.length > 0 && (
          <span className="search-expanded">同时搜了：{expanded.join("、")}</span>
        )}
        {state === "ready" && hints.length > 0 && (
          <span className="search-expanded">
            也可以搜：
            {hints.map((word) => (
              <button className="text-button" key={word} onClick={() => onSearchWord(word)} type="button">
                {word}
              </button>
            ))}
          </span>
        )}
      </div>

      {state === "loading" && <AsyncState state="loading" />}
      {state === "error" && <AsyncState message={error} state="error" />}
      {nothing && notes.length === 0 && (
        <AsyncState message="没搜到。试试更短的词，或者把范围换成全部项目。" state="empty" />
      )}
      {state === "ready" && (!nothing || notes.length > 0) && (
        <>
          {items.length > 0 ? (
            <SearchResults highlight items={items} onOpen={onOpen} query={query} />
          ) : (
            <p className="search-note">
              {materials.length > 0 || notes.length > 0
                ? `会议里没有包含「${query}」的内容`
                : similar.length > 0 || materialSimilar.length > 0
                  ? `没有包含「${query}」的内容，下面是意思相近的。`
                  : `这个范围里没有包含「${query}」的内容。`}
            </p>
          )}
          {unattributed > 0 && (
            <p className="search-note search-note--row">
              还有 {unattributed} 条来自没归项目的会
              <button className="text-button" onClick={() => onScopeChange("none")} type="button">
                看看
              </button>
            </p>
          )}
          {(materials.length > 0 || notes.length > 0) && (
            <section aria-label="材料里的" className="search-similar search-materials">
              <h2>材料里的</h2>
              {notes.map((note) => (
                <p className="search-note" key={note}>
                  {note}
                </p>
              ))}
              {materials.length > 0 && (
                <SearchMaterials items={materials} onNotice={setNotice} onOpenMaterial={onOpenMaterial} query={query} />
              )}
            </section>
          )}
          {(similar.length > 0 || materialSimilar.length > 0) && (
            <section aria-label="意思相近的" className="search-similar">
              <h2>意思相近的</h2>
              {similar.length > 0 && <SearchResults highlight={false} items={similar} onOpen={onOpen} query={query} />}
              {materialSimilar.length > 0 && (
                <SearchMaterials
                  items={materialSimilar}
                  onNotice={setNotice}
                  onOpenMaterial={onOpenMaterial}
                  query={query}
                  similar
                />
              )}
            </section>
          )}
          {notice && (
            <p className="search-note" role="status">
              {notice}
            </p>
          )}
        </>
      )}
      {state === "ready" && result?.semantic_unavailable && (
        <p className="search-note">意思相近的这次没搜：{result.semantic_unavailable}</p>
      )}
    </section>
  );
}
