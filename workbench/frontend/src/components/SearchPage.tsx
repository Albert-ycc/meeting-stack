import type { LoadState, Project, SearchPayload } from "../types";
import { AsyncState } from "./AsyncState";
import { SearchResults } from "./SearchResults";

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
}

export function SearchPage({
  error,
  result,
  onOpen,
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
  const nothing = state === "ready" && items.length === 0 && similar.length === 0 && unattributed === 0;

  return (
    <section className="search-page page-content">
      <header className="page-heading">
        <div>
          <span className="eyebrow">SEARCH</span>
          <h1>“{query}”</h1>
          <p>包含这个词的在前，意思相近的列在后面。纪要和标题也一起搜。</p>
        </div>
        <div className="record-count"><strong>{items.length}</strong><span>条包含原词</span></div>
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
      {nothing && <AsyncState message="没搜到。试试更短的词，或者把范围换成全部项目。" state="empty" />}
      {state === "ready" && !nothing && (
        <>
          {items.length > 0 ? (
            <SearchResults highlight items={items} onOpen={onOpen} query={query} />
          ) : (
            <p className="search-note">
              {similar.length > 0
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
          {similar.length > 0 && (
            <section aria-label="意思相近的" className="search-similar">
              <h2>意思相近的</h2>
              <SearchResults highlight={false} items={similar} onOpen={onOpen} query={query} />
            </section>
          )}
        </>
      )}
      {state === "ready" && result?.semantic_unavailable && (
        <p className="search-note">意思相近的这次没搜：{result.semantic_unavailable}</p>
      )}
    </section>
  );
}
