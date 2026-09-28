import { describe, expect, it } from "vitest";

/*
 * 第四期界面上的字的用词（和后端 test_copy_vocabulary.py 同一套）：先去掉『…』里引用的原话，
 * 再查不许出现的词。只查含汉字的字符串字面量和 JSX 文字，CSS 里的 100% 不算。
 * 4b 到 4h 每步把自己的新组件加进 SOURCES。
 */
const SOURCES = import.meta.glob(["./components/links/*.{ts,tsx}", "!./components/links/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const FORBIDDEN = ["导致", "因为", "推翻", "影响了", "%", "相似度", "置信度", "分数"];
/** 另外三样：没有「又说了一次」；不出现 key 的位置和环境变量 */
const ALSO_FORBIDDEN = ["又说了一次", "~/.config", "MEETING_WORKBENCH_"];
const HAN = /[\u4e00-\u9fff]/;

/** 去掉『…』里引用的原话 */
function stripQuoted(text: string): string {
  return text.replace(/『[^』]*』/g, "");
}

function violations(text: string): string[] {
  const bare = stripQuoted(text);
  return [...FORBIDDEN, ...ALSO_FORBIDDEN].filter((word) => bare.includes(word));
}

/** 源码里含汉字的字符串字面量和 JSX 文字；注释不算 */
function uiStrings(source: string): string[] {
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:\\])\/\/.*$/gm, "$1");
  const found: string[] = [];
  const literal = /"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/g;
  for (const match of code.matchAll(literal)) {
    if (HAN.test(match[0])) found.push(match[0].slice(1, -1));
  }
  // JSX 文字：标签或 {…} 之间的一段
  const jsxText = /[>}]([^<>{}"'`;]*)[<{]/g;
  for (const match of code.matchAll(jsxText)) {
    if (HAN.test(match[1])) found.push(match[1].trim());
  }
  return found;
}

describe("第四期界面的用词", () => {
  it("去掉『』里的原话再查：引用的原话照原样显示，不受这条管", () => {
    expect(violations("可能过时：9/21 决议『总价下调 5%』")).toEqual([]);
    expect(violations("这两份的相似度 80%")).toEqual(["%", "相似度"]);
    expect(violations("没配置 AI（~/.config/ds/api-key）")).toEqual(["~/.config"]);
    expect(uiStrings('// 因为注释不算\nconst a = "已记下";\nconst b = <p>后来又提到</p>;')).toEqual(["已记下", "后来又提到"]);
  });

  it("第四期新组件的源码里没有不许出现的词", () => {
    const files = Object.keys(SOURCES);
    expect(files.length).toBeGreaterThan(0);
    const problems = files.flatMap((file) =>
      uiStrings(SOURCES[file]).flatMap((text) => violations(text).map((word) => `${file}：「${text}」里有「${word}」`)),
    );
    expect(problems).toEqual([]);
  });
});
