import type { AskSource } from "../../types";
import { CitationChip, type ChipHandlers } from "./CitationChip";

/*
 * 找到的原文，按会议里的、材料里的分两组（4g）：「看看是哪几段」、「引用」和退回时都用它。
 * 每行一个出处小块，下面是原话。
 */
export function SourceList({
  sources,
  handlers,
  label = "找到的原话",
}: {
  sources: AskSource[];
  handlers: ChipHandlers;
  label?: string;
}) {
  const meetings = sources.filter((source) => source.kind !== "material");
  const materials = sources.filter((source) => source.kind === "material");
  return (
    <div aria-label={label} className="ask-sources" role="group">
      {[
        { key: "meetings", title: "会议里的", items: meetings },
        { key: "materials", title: "材料里的", items: materials },
      ]
        .filter((group) => group.items.length > 0)
        .map((group) => (
          <section className="ask-sources__group" key={group.key}>
            <h4>{group.title}</h4>
            <ul>
              {group.items.map((source) => (
                <li key={source.id}>
                  <CitationChip handlers={handlers} source={source} />
                  <p className="ask-sources__quote">{source.quote || source.text}</p>
                </li>
              ))}
            </ul>
          </section>
        ))}
    </div>
  );
}
