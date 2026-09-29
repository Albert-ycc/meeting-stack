import type { GlossaryCategory, GlossaryScope, GlossaryTerm, MaterialWord } from "../types";

/** 分类字典与后端 CATEGORIES 契约一致，别单独改。 */
export const GLOSSARY_CATEGORIES: GlossaryCategory[] = ["人名", "机构", "术语", "药品", "地名", "其他"];

export const ALL_KEY = "all";

export function chipKey(chip: Pick<GlossaryScope, "kind" | "key">): string {
  return `${chip.kind}:${chip.key}`;
}

/** 合并本地/远端 chip 列表时的去重身份：只有一个「公共」分组。
 * 公共分组的 key 在库里是 scope 值「通用」，按 kind 归一，免得哪边 key 写法不同时
 * 在「全部」视图里裂出两个公共分组、且各自算出一半计数。 */
export function chipIdentity(chip: Pick<GlossaryScope, "kind" | "key">): string {
  return chip.kind === "general" ? "general" : chipKey(chip);
}

/** 术语归到哪个 chip：project_id 优先；否则按 scope 字符串落「通用」或某个自定义桶。 */
export function matchesChip(term: GlossaryTerm, chip: GlossaryScope): boolean {
  if (chip.kind === "project") return term.project_id === chip.key;
  if (term.project_id) return false;
  if (chip.kind === "general") return !term.scope || term.scope === "通用";
  return term.scope === chip.key;
}

/** 一条术语落在哪个 chip 上（保存后跟着切范围用） */
export function termChipKey(term: Pick<GlossaryTerm, "project_id" | "scope">): string {
  if (term.project_id) return chipKey({ kind: "project", key: term.project_id });
  if (!term.scope || term.scope === "通用") return chipKey({ kind: "general", key: "通用" });
  return chipKey({ kind: "bucket", key: term.scope });
}

/** 后端 /api/glossary/scopes 还没上线，或返回为空时的本地兜底：从已加载的术语里现算分组。 */
export function deriveLocalScopes(terms: GlossaryTerm[]): GlossaryScope[] {
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
    { kind: "general", key: "通用", label: "公共", color: null, count: generalCount },
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

/** 术语/错写/也叫子串匹配，不区分大小写；空搜索词永远命中。 */
export function matchesSearch(term: GlossaryTerm, needle: string): boolean {
  if (!needle) return true;
  if (term.term.toLowerCase().includes(needle)) return true;
  if (term.aliases.some((alias) => alias.toLowerCase().includes(needle))) return true;
  return (term.also ?? []).some((name) => name.toLowerCase().includes(needle));
}

/** 待认词的搜索：词本身或任一听错的写法 */
export function matchesCandidateSearch(word: MaterialWord, needle: string): boolean {
  if (!needle) return true;
  return word.term.toLowerCase().includes(needle) || word.wrongs.some((wrong) => wrong.text.toLowerCase().includes(needle));
}

/** 正确写法：1–40 字，须含中文或字母；空串不报错（还没写）。 */
export function termNameError(text: string): string {
  if (!text) return "";
  if (text.length > 40 || !/[一-龥A-Za-z]/.test(text)) return "1–40 字，须包含中文或字母";
  return "";
}

/** 「2026年08月25日」；不是合法时间就原样返回前 10 位 */
export function formatFullDate(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}年${pad(date.getMonth() + 1)}月${pad(date.getDate())}日`;
}

/** 「9月28日」 */
export function formatMonthDay(value: string | null | undefined): string {
  if (!value) return "";
  const [, month, day] = value.slice(0, 10).split("-");
  return month && day ? `${Number(month)}月${Number(day)}日` : value;
}
