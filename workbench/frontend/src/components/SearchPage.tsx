import type { LoadState, SearchItem } from "../types";
import { AsyncState } from "./AsyncState";
import { SearchResults } from "./SearchResults";

interface SearchPageProps {
  items: SearchItem[];
  mode: "exact" | "semantic";
  onOpen: (meetingId: string, startMs: number) => void;
  query: string;
  state: LoadState;
  error?: string;
}

export function SearchPage({ error, items, mode, onOpen, query, state }: SearchPageProps) {
  return (
    <section className="search-page page-content">
      <header className="page-heading">
        <div>
          <span className="eyebrow">SEARCH / {mode === "exact" ? "精确检索" : "语义检索"}</span>
          <h1>“{query}”</h1>
          <p>{mode === "exact" ? "按原句命中最新逐字稿。" : "使用本机中文向量模型查找语义相近内容。"}</p>
        </div>
        <div className="record-count"><strong>{items.length}</strong><span>条结果</span></div>
      </header>
      {state === "loading" && <AsyncState state="loading" />}
      {state === "error" && <AsyncState message={error} state="error" />}
      {state === "empty" && <AsyncState message="没有命中原句，试试更短的关键词或切换检索方式。" state="empty" />}
      {state === "ready" && <SearchResults items={items} mode={mode} onOpen={onOpen} query={query} />}
    </section>
  );
}
