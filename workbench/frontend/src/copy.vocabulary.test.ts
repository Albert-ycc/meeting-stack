import { describe, expect, it } from "vitest";

/*
 * 第四期界面上的字的用词（和后端 test_copy_vocabulary.py 同一套）：先去掉『…』里引用的原话，
 * 再查不许出现的词。只查含汉字的字符串字面量和 JSX 文字，CSS 里的 100% 不算。
 * 4b 到 4h 每步把自己的新组件加进 SOURCES。
 */
const SOURCES = import.meta.glob(
  [
    "./components/links/*.{ts,tsx}",
    "!./components/links/*.test.{ts,tsx}",
    // 4b：会议面板、文件面板里放宽的提到和状态句；预览抽屉的小字
    "./components/graph/FilePanels.tsx",
    "./components/MaterialPreview.tsx",
    // 4c：决议卡、时间线、决议行和它们的字；原话挪出来的 quotes.tsx；简报、展开一场会的标记
    "./components/decisions/*.{ts,tsx}",
    "!./components/decisions/*.test.{ts,tsx}",
    "./components/graph/quotes.tsx",
    "./components/graph/FocusPanel.tsx",
    // 4d：相关材料栏、「内容相关的会」、预览抽屉的定位块（上面 links/* 已含）、小签、会议页和搜索页的新字
    "./components/files/*.{ts,tsx}",
    "!./components/files/*.test.{ts,tsx}",
    "./components/MeetingDetailPage.tsx",
    "./components/SearchMaterials.tsx",
    "./components/TranscriptPanel.tsx",
  ],
  {
    query: "?raw",
    import: "default",
    eager: true,
  },
) as Record<string, string>;

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

  it("4b 的状态句、小字和提示", () => {
    const copy = [
      "会上换了叫法的文件还在整理",
      "没配置 AI，会上换了叫法的文件先不整理",
      "AI 的 key 不对，会上换了叫法的文件先不整理",
      "今天的 AI 用量到上限了，明天接着整理",
      "AI 账户余额不足，会上换了叫法的文件先不整理",
      "AI 连不上，过一会儿自动再试",
      "这场会的 AI 整理没做成",
      "说的是『上周那版报价单』",
      "已记下：『上周那版报价单』不是这份文件",
      "会上说『上周那版报价单』等 2 处 · 00:12:34",
    ];
    expect(copy.flatMap(violations)).toEqual([]);
    expect(Object.keys(SOURCES).some((file) => file.endsWith("looseMention.ts"))).toBe(true);
  });

  it("4c 的决议卡、时间线、状态句和提示", () => {
    const copy = [
      "还在对比前后几场会的决议，对完会标出后来改了的",
      "没配置 AI，不标哪些决议后来改了",
      "AI 的 key 不对，不标哪些决议后来改了",
      "今天的 AI 用量到上限了，明天接着对比",
      "AI 账户余额不足，不标哪些决议后来改了",
      "AI 连不上，过一会儿自动再对比",
      "这场会的决议没对比成",
      "后台 AI 整理关着，不标哪些决议后来改了",
      "关联整理关着，决议按纪要现读，不标后来改了",
      "这个项目还没挂材料文件夹，时间线里只有会议和任务",
      "资料盘未连接，插上后接着记文件的变化",
      "正在第一次收文件名，收完后开始记文件的新增和修改",
      "后来改了：9月28日 周会『阈值改成 0.7』",
      "这次改了 9月20日 周会定的『阈值先按 0.8 执行』",
      "后来又提到：9月30日 周会",
      "你标过和 9月28日 周会那条不是一回事",
      "已从这个需求里拿掉，项目时间线的『决议』里还能看到",
      "『能耗看板』里新增 5 个、改了 2 个：报价单_v3.xlsx、排期表.xlsx 等",
      "『能耗看板』里 3 个文件最后一次修改在这天：…",
    ];
    expect(copy.flatMap(violations)).toEqual([]);
    const files = Object.keys(SOURCES);
    for (const name of ["DecisionRow.tsx", "DecisionLogCard.tsx", "ProjectTimeline.tsx", "decisionText.ts"]) {
      expect(files.some((file) => file.endsWith(name))).toBe(true);
    }
  });

  it("4d 的相关材料栏、抽屉、小签和提示", () => {
    const copy = [
      "这场会没找到相关材料",
      "这个项目材料太多，较早的一部分没有比对",
      "正在找相关材料",
      "这场会还在转写，转完再找相关材料",
      "会议在转写，转完再找相关材料",
      "这个项目还有 3 份材料没读完，读完的先列在这里",
      "这个项目的材料还没读完，读完会接着找",
      "这场会没归项目，相关材料只在项目文件夹里找",
      "这个项目还没挂材料文件夹",
      "还没有逐字稿",
      "本地语义模型没装好，找不了相关材料",
      "语义索引关着，找不了相关材料",
      "材料正文读取关着，找不了相关材料",
      "关联整理关着，找不了相关材料",
      "相关材料没取到",
      "相关材料（12:00 前后）2 份",
      "共同词：字段命名、驻场",
      "这场会的另一份记录：纪要-0921.docx",
      "这一段没找到相关材料",
      "别的时间有：",
      "有 1 份材料标过不相关",
      "已记下：『接口文档.docx』和这场会不相关",
      "已改回相关：『接口文档.docx』",
      "和会上相关的这段",
      "文件后来改过，这是改之前读到的那段",
      "这段在文件里找不到了（文件可能改过）",
      "3 场会提到",
      "在 3 场会上被提到",
    ];
    expect(copy.flatMap(violations)).toEqual([]);
    const files = Object.keys(SOURCES);
    for (const name of ["RelatedMaterials.tsx", "RelatedMeetings.tsx", "relatedWindows.ts", "MentionedBadge.tsx"]) {
      expect(files.some((file) => file.endsWith(name))).toBe(true);
    }
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
