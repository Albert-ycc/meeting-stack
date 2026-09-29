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
    // 4g：问答卡和面板、出处小块、原文列表；搜索页的「想要一句话的回答？」
    "./components/ask/*.{ts,tsx}",
    "!./components/ask/*.test.{ts,tsx}",
    "./components/SearchPage.tsx",
    // 4e：任务抽屉的产出问题、展开一场会的交付物小签（问题块本身在 links/* 里）
    "./components/TaskDrawer.tsx",
    "./components/graph/MeetingFocusView.tsx",
    "./components/graph/focusLayout.ts",
    // 4f：图例、连线开关、相关的状态、第四期的线和它们的面板、局部图和来龙去脉（TraceList 在 links/* 里）、小签
    "./components/graph/ProjectGraph.tsx",
    "./components/graph/GraphCanvas.tsx",
    "./components/graph/GraphPanel.tsx",
    "./components/graph/MaterialPanels.tsx",
    "./components/graph/LocalGraphView.tsx",
    "./components/graph/LocalGraphPanel.tsx",
    "./components/graph/localLayout.ts",
    "./components/graph/drawnEdges.ts",
    "./components/graph/graphFiles.ts",
    "./components/graph/layout.ts",
    // 4h：从材料里找到的词（三种样子）和挂它的词典块、词典页、会议页词典小节
    "./components/MaterialWords.tsx",
    "./components/ProjectGlossary.tsx",
    "./components/GlossaryPage.tsx",
    "./components/GlossaryCandidates.tsx",
    "./components/GlossaryScopeRail.tsx",
    "./components/GlossarySuggestionsPane.tsx",
    "./components/GlossaryTermEditor.tsx",
    "./components/GlossaryTermList.tsx",
    "./components/MeetingGlossaryPanel.tsx",
    // 4h：需求页的［复制给 Claude Code］和它的提示
    "./components/RequirementDetailPage.tsx",
    // 第四期改过、含界面文字的老组件：整份源码照同一套查（前三期的老文案加进来时也没有禁词，不设豁免）
    "./components/ProjectCardsRow.tsx",
    "./components/ProjectDetailPage.tsx",
    "./App.tsx",
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

  it("4g 的问答卡、出处和提示", () => {
    const copy = [
      "问这个项目",
      "比如：报价最后定的是多少？",
      "只在『云图AI』的会和材料里找；要把材料原文发出去时会先告诉你",
      "正在找相关的原话…",
      "找到会议里的 5 段、材料里的 3 段",
      "将发送 3 段材料原文给 api.deepseek.com",
      "将发送 3 段材料原文给 127.0.0.1",
      "只用会议回答",
      "看看是哪几段",
      "在等 AI 回答",
      "会议和材料里都没找到和这个问题有关的原话",
      "换个说法，或者用文件名、词典里的词问",
      "没配置 AI，先列出找到的原话",
      "问答的 AI 回答已关闭，先列出找到的原话",
      "今天问答的次数到上限了，先列出找到的原话",
      "只看了最相关的 3 段材料、5 段会议里的原话",
      "另有 2 场没归项目的会也说到这些词，这次没用上",
      "用本机模型回答",
      "回答太长，后面截掉了",
      "会议和材料里没找到能回答这个问题的原话",
      "AI 的回答没指到原文，没列出来；下面是找到的原话",
      "AI 没回（等了 90 秒），先列出找到的原话",
      "上一个问题还在回答",
      "这次找到的原话过期了",
      "后台还是旧版本，重启声档后再试",
      "之前问过",
      "引用",
      "复制回答",
      "已复制",
      "再问一次",
      "资料盘未连接",
      "后来改了 9/28",
      "出处：9/21 初审规则沟通 12:34",
      "出处：报价单 v3.xlsx 表『预算』",
      "回答引用的这段",
      "想要一句话的回答？到『云图AI』里问",
    ];
    expect(copy.flatMap(violations)).toEqual([]);
    const files = Object.keys(SOURCES);
    for (const name of ["ProjectAsk.tsx", "AnswerText.tsx", "CitationChip.tsx", "SourceList.tsx", "askStore.ts"]) {
      expect(files.some((file) => file.endsWith(name))).toBe(true);
    }
  });

  it("4e 的证据、问法、可能过时的说法、小签和提示", () => {
    const copy = [
      "会后 3 天新增在『能耗看板/』",
      "会后 3 天新增，文件名里也有『能耗看板』",
      "会后 3 天改过，文件名里也有『报价单』",
      "任务确认后 3 天新增在『能耗看板/』",
      "会后当天新增在『能耗看板/』",
      "确认当天新增在『能耗看板/』",
      "是任务『写一版方案』的交付物吗？",
      "是这条任务的交付物吗？",
      "可能过时：9/21 决议『总价下调 5%』",
      "第 2 页：『…总价在原基础上下调 3%…』",
      "『报价单 v3』之后没改过，可能过时",
      "1 个文件可能过时",
      "交付物？：能耗看板方案.key，等你认交付物",
      "是",
      "不是",
      "已更新",
      "不相关",
      "已登记为『写一版方案』的交付物",
      "已记下：不是这条任务的交付物",
      "已标为更新过",
      "已记下：和这条决议不相关",
      "已撤销",
      "这条任务已经取消了，先恢复任务再登记",
      "这条任务还没确认，先确认任务再登记",
      "后台还是旧版本，重启声档后再试",
    ];
    expect(copy.flatMap(violations)).toEqual([]);
    // 『x/』里的文件夹只写最后一层，中间没有「/」
    const folders = copy.flatMap((text) => [...text.matchAll(/『([^』]*)\/』/g)].map((match) => match[1]));
    expect(folders.length).toBeGreaterThan(0);
    expect(folders.filter((folder) => folder.includes("/"))).toEqual([]);
    const files = Object.keys(SOURCES);
    for (const name of ["TaskDrawer.tsx", "MeetingFocusView.tsx", "focusLayout.ts", "RelationQuestion.tsx", "FocusPanel.tsx"]) {
      expect(files.some((file) => file.endsWith(name))).toBe(true);
    }
  });

  it("4f 的图例、［相关］的状态、线上的字、局部图和来龙去脉的状态和错误", () => {
    const copy = [
      "图例",
      "位置：左会议 · 右材料 · 上需求 · 下线索词，越靠中心越新",
      "实线：归属、讨论",
      "细虚线：文件夹",
      "带箭头的实线：交付物",
      "细线带引号：会上提到这份文件",
      "琥珀色虚线：在等你回答的产出和可能过时",
      "流动的琥珀色虚线：待复核的归属",
      "浅灰点线：相关（两边有共同词），默认关着",
      "短虚线：跨项目、像是新需求",
      "连线",
      "提到",
      "相关",
      "打开后每个节点最多 3 条",
      "这个时间窗里还没有相关的线",
      "相关的线没取到",
      "会后 3 天新增在『能耗看板/』，是任务『写一版方案』的交付物吗？",
      "9/21 定的『总价下调 5%』，报价单 v3 之后没改过",
      "任务『整理接口清单』的交付物 · 你标的",
      "共同词：报价单、驻场",
      "会上：『报价单再看一下』· 00:00:01",
      "材料：『报价单的驻场部分』 · 第 1 页",
      "2 个文件可能过时",
      "1 个新文件等你认交付物",
      "可能过时",
      "交付物？",
      "连线 · 产出",
      "连线 · 可能过时",
      "还有 3 条线没画出来",
      "打开任务",
      "以它为中心看",
      "以『报价单 v3.xlsx』为中心",
      "回到关系图",
      "还有 7 个没画出来",
      "7/30 周会 · 会上说『报价单』2 次",
      "会上提到这条任务 · 00:05:10",
      "同属『报价单』",
      "同属需求『能耗看板』的文件夹",
      "这场会定的",
      "连线：这场会定的",
      "↓ 你标过已更新",
      "来龙去脉",
      "『报价单 v3.xlsx』的来龙去脉",
      "在关系图上看 →",
      "正在取这份文件的关系",
      "还没有会提到这份文件，也没有任务或决议连到它",
      "这份文件挪到了『2026』文件夹里",
      "这份文件挪到了项目文件夹的最上层",
      "这份文件已经不在资料盘里了，下面是它还在时的关系",
      "局部图没取到",
      "来龙去脉没取到",
      "这份文件还没有带原话的来龙去脉",
      "这场会还没有带原话的来龙去脉",
      "往前走到 3 步为止，更早的没展开",
      "往后走到 3 步为止，更晚的没展开",
      "后台还是旧版本，重启声档后再试",
      "3 场会提到",
      "在 3 场会上被提到",
    ];
    expect(copy.flatMap(violations)).toEqual([]);
    const files = Object.keys(SOURCES);
    for (const name of ["LocalGraphView.tsx", "LocalGraphPanel.tsx", "TraceList.tsx", "drawnEdges.ts", "ProjectGraph.tsx"]) {
      expect(files.some((file) => file.endsWith(name))).toBe(true);
    }
  });

  it("第四期新组件的源码里没有不许出现的词", () => {
    const files = Object.keys(SOURCES);
    expect(files.length).toBeGreaterThan(0);
    // 老组件真的读到了（路径写错时 glob 不报错，只是少查）
    expect(files).toEqual(
      expect.arrayContaining(["./components/ProjectCardsRow.tsx", "./components/ProjectDetailPage.tsx", "./App.tsx"]),
    );
    const problems = files.flatMap((file) =>
      uiStrings(SOURCES[file]).flatMap((text) => violations(text).map((word) => `${file}：「${text}」里有「${word}」`)),
    );
    expect(problems).toEqual([]);
  });
});
