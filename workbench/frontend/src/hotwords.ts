const HOTWORD_SEPARATOR = /[,，\n\r]+/u;
const FORBIDDEN_CODE_POINT = /\p{C}/u;

function casefoldIdentity(value: string): string {
  return value
    .toLocaleLowerCase("und")
    .replaceAll("ß", "ss")
    .replaceAll("ς", "σ");
}

export function parseHotwordsInput(value: string): string[] {
  const seen = new Set<string>();
  const terms: string[] = [];
  for (const raw of value.split(HOTWORD_SEPARATOR)) {
    const normalized = raw.normalize("NFKC").trim();
    if (!normalized) continue;
    const identity = casefoldIdentity(normalized);
    if (seen.has(identity)) continue;
    seen.add(identity);
    terms.push(normalized);
  }
  return terms;
}

export function validateHotwordsInput(value: string): string {
  const normalizedTerms = value
    .split(HOTWORD_SEPARATOR)
    .map((term) => term.normalize("NFKC").trim())
    .filter(Boolean);
  if (normalizedTerms.some((term) => FORBIDDEN_CODE_POINT.test(term))) {
    return "热词不能包含控制字符";
  }
  const terms = parseHotwordsInput(value);
  if (terms.length > 20) return "本场热词最多 20 个";
  if (terms.some((term) => Array.from(term).length > 80)) return "每个热词最多 80 个字符";
  return "";
}
