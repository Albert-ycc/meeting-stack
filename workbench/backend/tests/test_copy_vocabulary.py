"""第四期的用词（4a 建骨架，4b 到 4h 每步把自己的字表、新模块和样本接口加进来）。

先用『[^』]*』去掉『』里引用的原话，再查不许出现的词。后端查两样：各步新模块里含汉字的字符串
常量（用 ast 读源码，跳过文档字符串、提示词常量和 logger 的参数；cli.py 和 doctor 是终端里的开发者
工具，不查）；接口返回里 text、label、ask、title、detail 这几个键的值。前端有一份同样的
src/copy.vocabulary.test.ts。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "meeting_workbench"

FORBIDDEN = ("导致", "因为", "推翻", "影响了", "%", "相似度", "置信度", "分数")
# 另外三样：不说「又说了一次」；不出现 key 文件的位置和环境变量名
ALSO_FORBIDDEN = ("又说了一次", "~/.config", "MEETING_WORKBENCH_")
QUOTED = re.compile(r"『[^』]*』")
HAN = re.compile(r"[一-鿿]")
COPY_KEYS = frozenset({"text", "label", "ask", "title", "detail"})

# 各步的新模块（meeting_workbench/ 下）。还没写到的步跳过。
PHASE_FOUR_MODULES = (
    "file_events.py",
    "relations.py",
    "relation_read.py",
    "decisions.py",
    "deep_links.py",
    "links_llm.py",
    "llm.py",
    "loose_mentions.py",
    "decision_pairs.py",
    "timeline.py",
    "related.py",
    "related_read.py",
    "ask_retrieval.py",
    "asks.py",
    "produced.py",
    "affects.py",
    "graph_local.py",
    "card_index.py",
    "glossary_mining.py",
)

# 4a：relation_read.links_state 的状态句（第 3 节「状态和提示」，每种一句话、最多一个按钮）
STATE_SENTENCES_4A = (
    "这场会没找到相关材料",
    "会议在转写，关联先停一下，转完接着整理",
    "还有 3 场会在整理关联",
    "材料还在读，读完才能找相关段落",
    "材料的向量还在补，补完才能找意思相近的段落",
    "会上换了叫法的文件还在整理",
    "没配置 AI，会上换了叫法的文件先不整理",
    "AI 的 key 不对，会上换了叫法的文件先不整理",
    "今天的 AI 用量到上限了，明天接着整理",
    "AI 账户余额不足，会上换了叫法的文件先不整理",
    "AI 连不上，过一会儿自动再试",
    "这场会的 AI 整理没做成",
    "这场会的决议没对比成",
    "关联整理已关闭，在终端运行 meeting-workbench doctor 看原因",
    "语义索引关着，找不了相关材料",
    "资料盘未连接，插上后接着整理",
)
# 4a：回答和撤销的八种错误说法、按钮和提示
ANSWER_COPY_4A = (
    "这条关联已经不在了",
    "这条已经处理过了",
    "这条关联已经不在这个项目里了",
    "已超过撤销时间，请直接改回",
    "已经撤销过了",
    "这类关联不能这样回答",
    "只能换成这个项目文件夹里同名的另一份文件",
    "这份文件已经不在了",
    "现在重试",
    "已撤销",
    "后台还是旧版本，重启声档后再试",
)
# 4b：会议面板「会上提到的文件」下面的七句（loose_mentions.LOOSE_SENTENCES）、线上的字、小字和回答的提示
STATE_SENTENCES_4B = (
    "会上换了叫法的文件还在整理",
    "没配置 AI，会上换了叫法的文件先不整理",
    "AI 的 key 不对，会上换了叫法的文件先不整理",
    "今天的 AI 用量到上限了，明天接着整理",
    "AI 账户余额不足，会上换了叫法的文件先不整理",
    "AI 连不上，过一会儿自动再试",
    "这场会的 AI 整理没做成",
)
COPY_4B = (
    *STATE_SENTENCES_4B,
    "会上说『上周那版报价单』· 00:12:34",
    "会上说『上周那版报价单』等 2 处 · 00:12:34",
    "说的是『上周那版报价单』",
    "已记下：『上周那版报价单』不是这份文件",
    "已换成「报价单 v4.xlsx」",
    "你标过「周会」说的不是这份文件",
    "不是这份文件",
    "换成这份",
    "撤销",
)
# 4c：需求卡和时间线［决议］的九句状态句（decisions.PAIR_SENTENCES）、时间线的三句、卡和时间线上的字、提示
STATE_SENTENCES_4C = (
    "还在对比前后几场会的决议，对完会标出后来改了的",
    "没配置 AI，不标哪些决议后来改了",
    "AI 的 key 不对，不标哪些决议后来改了",
    "今天的 AI 用量到上限了，明天接着对比",
    "AI 账户余额不足，不标哪些决议后来改了",
    "AI 连不上，过一会儿自动再对比",
    "这场会的决议没对比成",
    "后台 AI 整理关着，不标哪些决议后来改了",
    "关联整理关着，决议按纪要现读，不标后来改了",
)
TIMELINE_SENTENCES_4C = (
    "这个项目还没挂材料文件夹，时间线里只有会议和任务",
    "资料盘未连接，插上后接着记文件的变化",
    "正在第一次收文件名，收完后开始记文件的新增和修改",
)
COPY_4C = (
    *STATE_SENTENCES_4C,
    *TIMELINE_SENTENCES_4C,
    "决议",
    "12 条 · 3 条后来改了",
    "9月24日 周三 · 周会",
    "9月10日 · 需求评审　这场纪要没有决议段",
    "后来改了：9月28日 周会『阈值改成 0.7』",
    "这次改了 9月20日 周会定的『阈值先按 0.8 执行』",
    "后来又提到：9月30日 周会",
    "不是一回事",
    "你标过和 9月28日 周会那条不是一回事",
    "这场会还有 2 条决议没归到具体需求",
    "放到这个需求",
    "不属于这个需求",
    "放到需求 ▾",
    "已去掉这条『后来改了』",
    "已分开，两条各列各的",
    "已从这个需求里拿掉，项目时间线的『决议』里还能看到",
    "已放到这个需求",
    "已放到『初审规则 V2』",
    "没读到决议，稍后再试",
    "还没有关联会议，关联以后这里列出每场会定了什么",
    "关联的会还没写好纪要",
    "关联的 3 场会，纪要里没有列出决议",
    "时间线",
    "全部",
    "任务",
    "文件",
    "今天",
    "昨天",
    "2025年12月30日 周二",
    "14:30 会议『初审规则沟通』· 48 分钟",
    "定了：阈值先按 0.8 执行 · 9月28日后来改了",
    "还有 5 条",
    "确认了任务：写一版方案",
    "确认了 3 条任务：写一版方案、…",
    "完成了 3 条任务：…",
    "『整理报价单』的产出：报价单_v3.xlsx",
    "『能耗看板』里新增 5 个、改了 2 个：报价单_v3.xlsx、排期表.xlsx 等",
    "项目文件夹里新增 2 个：…",
    "另有 4 个文件夹有变化",
    "『能耗看板』里 3 个文件最后一次修改在这天：…",
    "没归到具体需求",
    "更早",
    "文件从 9月27日 起记录",
    "这个项目还没有会议、任务和文件的变化",
    "这个项目的会还没有列出决议",
    "还没有确认或完成的任务",
    "还没有记到文件的新增和修改",
    "挂上文件夹",
    "· 9月28日后来改了",
)
# 4d：相关材料栏的状态句（十五种里服务端给的十四种，「相关材料没取到」前端自己画）、栏和抽屉的字、小签、提示
STATE_SENTENCES_4D = (
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
)
COPY_4D = (
    *STATE_SENTENCES_4D,
    "相关材料没取到",
    "重试",
    "去项目页",
    "相关材料",
    "12:00 前后",
    "相关材料（12:00 前后）2 份",
    "相关材料（12:00 前后）这一段没有",
    "收起",
    "展开",
    "共同词：字段命名、驻场",
    "从 12:14 播放会上这段",
    "预览 接口文档.docx 第 3 节",
    "录音 05:12",
    "不相关",
    "用本机应用打开",
    "这场会的另一份记录：纪要-0921.docx",
    "预览",
    "这一段没找到相关材料",
    "别的时间有：",
    "有 1 份材料标过不相关",
    "看看",
    "改回相关",
    "已记下：『接口文档.docx』和这场会不相关",
    "已撤销",
    "已改回相关：『接口文档.docx』",
    "和会上相关的这段",
    "搜到的这段",
    "回答引用的这段",
    "文件后来改过，这是改之前读到的那段",
    "这段在文件里找不到了（文件可能改过）",
    "内容相关的会",
    "周会 · 9月21日",
    "3 场会提到",
    "在 3 场会上被提到",
    "会议不存在",
    "这份文件不在索引里了",
    "这场会没归项目",
    "这份文件不在这个项目的资料盘里",
    "只能在声档所在的这台电脑上打开文件",
    "这种文件不在声档里直接打开，可以在访达中显示",
    "资料盘未连接",
    "找不到这个文件了",
    "这台电脑上找不到能打开文件的程序",
)
# 4g：问答的说明、错误、停了的八句和页面上的字（第 9 节、第 12 节）
COPY_4G = (
    "正在转写，这次只按原词找",
    "材料全文索引在重建，结果可能不全",
    "有些材料还没读完，可能找不全",
    "时间到了，只找了一部分",
    "用本机模型回答",
    "项目不存在",
    "问题最少 2 个字，最多 300 个字",
    "问题里有不能用的控制字符",
    "这次找到的原话过期了，请再问一次",
    "这个项目上一个问题还在回答",
    "这次没有可以发送的原话",
    "今天问答的次数到上限了，明天再问",
    "没配置 AI，先列出找到的原话",
    "问答的 AI 回答已关闭，先列出找到的原话",
    "在等 AI 回答",
    "这次的回答过期了，请再问一次",
    "AI 没回（等了 90 秒），先列出找到的原话",
    "连不上 AI，先列出找到的原话",
    "AI 那边太忙，过一会儿再问",
    "AI 那边出错了，先列出找到的原话",
    "AI 的 key 不对，先列出找到的原话",
    "AI 不接受这次的请求，先列出找到的原话",
    "AI 正在回答别的问题，过一会儿再问",
    "出了点问题，先列出找到的原话",
    "问这个项目",
    "比如：报价最后定的是多少？",
    "只在『云图AI』的会和材料里找；要把材料原文发出去时会先告诉你",
    "正在找相关的原话…",
    "找到会议里的 5 段、材料里的 3 段",
    "将发送 3 段材料原文给 api.deepseek.com",
    "将发送 3 段材料原文给 127.0.0.1",
    "发送",
    "只用会议回答",
    "看看是哪几段",
    "会议和材料里都没找到和这个问题有关的原话",
    "换个说法，或者用文件名、词典里的词问",
    "今天问答的次数到上限了，先列出找到的原话",
    "只看了最相关的 3 段材料、5 段会议里的原话",
    "另有 2 场没归项目的会也说到这些词，这次没用上",
    "回答太长，后面截掉了",
    "会议和材料里没找到能回答这个问题的原话",
    "看看找到的原话",
    "AI 的回答没指到原文，没列出来；下面是找到的原话",
    "引用",
    "复制回答",
    "已复制",
    "再问一次",
    "上一个问题还在回答",
    "这次找到的原话过期了",
    "之前问过",
    # 日期写「9/21」，这张表不许有斜杠，所以样例里去掉日期
    "初审规则沟通 · 纪要",
    "后来改了",
    "出处：初审规则沟通 12:34",
    "出处：报价单 v3.xlsx 表『预算』",
    "想要一句话的回答？到『云图AI』里问",
    "回答引用的这段",
)
# 4e：产出的证据（六种）、两处问题的问法、可能过时的两种说法和小签、按钮、回答以后的提示和两处新错误
COPY_4E = (
    "会后 3 天新增在『能耗看板/』",
    "会后 3 天新增，文件名里也有『能耗看板』",
    "会后 3 天改过，文件名里也有『报价单』",
    "任务确认后 3 天新增在『能耗看板/』",
    "会后当天新增在『能耗看板/』",
    "确认当天新增在『能耗看板/』",
    "是任务『写一版方案』的交付物吗？",
    "是这条任务的交付物吗？",
    "可能过时：9/21 决议『总价下调 5%』",
    "可能过时：2025/9/21 决议『总价下调 5%』",
    "第 2 页：『…总价在原基础上下调 3%，含税…』",
    "『报价单 v3』之后没改过，可能过时",
    "1 个文件可能过时",
    "是",
    "不是",
    "已更新",
    "不相关",
    "撤销",
    "已登记为『写一版方案』的交付物",
    "已记下：不是这条任务的交付物",
    "已标为更新过",
    "已记下：和这条决议不相关",
    "已撤销",
    "这条任务已经取消了，先恢复任务再登记",
    "这条任务还没确认，先确认任务再登记",
    "这份文件已经不在了",
    "这条已经处理过了",
    "还没有登记交付物",
)
# 4f：图例、［相关］的状态、线上的字、在等你的两句、局部图和来龙去脉的标题、状态和错误
COPY_4F = (
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
    "重试",
    "后台还是旧版本，重启声档后再试",
    "会后 3 天新增在『能耗看板/』，是任务『写一版方案』的交付物吗？",
    "9/21 定的『总价下调 5%』，报价单 v3 之后没改过",
    "任务『整理接口清单』的交付物 · 你标的",
    "共同词：报价单、驻场",
    "会上说『上周那版报价单』· 00:12:34",
    "2 个文件可能过时",
    "1 个新文件等你认交付物",
    "可能过时",
    "交付物？",
    "，可能过时",
    "，等你认交付物",
    "连线 · 产出",
    "连线 · 可能过时",
    "产出",
    "交付物",
    "打开任务",
    "还有 3 条线没画出来",
    "以它为中心看",
    "以『报价单 v3.xlsx』为中心",
    "回到关系图",
    "还有 7 个没画出来",
    "7/30 周会 · 会上说『报价单』2 次",
    "会上提到这条任务 · 00:05:10",
    "同属『报价单』",
    "同属需求『能耗看板』的文件夹",
    "这场会定的",
    "后来改了",
    "后来又提到",
    "来龙去脉",
    "『报价单 v3.xlsx』的来龙去脉",
    "『报价沟通』的来龙去脉",
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
    "这条决议还没有带原话的来龙去脉",
    "这条任务还没有带原话的来龙去脉",
    "← 回到中心",
    "往前走到 3 步为止，更早的没展开",
    "往后走到 3 步为止，更晚的没展开",
    "这份文件不在索引里了",
    "会议不存在",
    "这条决议已经不在了",
    "这条任务已经不在了",
    "这份文件不在任何项目的资料盘里",
    "这场会没归项目",
    "会上提到这份文件",
    "你标过已更新",
    "↓ 你标过已更新",
    "连线：这场会定的",
    "3 场会提到",
    "在 3 场会上被提到",
)
# 4h：00 索引.md v2 的固定字句（card_index.py；这是写进项目文件夹、给 Claude Code 读的文件，
# 模板里的 {…} 是填进去的日期、次数和标题）
COPY_4H_INDEX = (
    "> 本文件由声档自动维护，请勿手改。把这个项目文件夹交给 Claude Code 时，先让它读这一份。",
    "> 这里只有会上说过的话、任务和文件位置，不摘材料的内容；文件内容请直接打开文件看。",
    " · 会议记录索引",
    "## 进行中的行动项",
    "暂无进行中的行动项。",
    "## 需求",
    "### 已完成或搁置",
    "## 其他决议（不属于哪个需求）",
    "## 关键文件",
    "## 会议（按时间倒序）",
    "这个项目还没有会议卡片。",
    "- 优先级：{priority}",
    "- 文件夹：{path}",
    "- 会议：{links}",
    "（另有 {n} 场会的纪要不在这个文件夹）",
    "另有 {n} 场会的纪要不在这个文件夹",
    "- 定了什么：",
    "  - 另有 {n} 条，见各场会的纪要",
    "- 行动项：{items}",
    "- 产出：{items}",
    "这次改了 {date} 定的『{text}』",
    "{dates} 后来又提到",
    " · 这次改了 {date} 定的『{text}』",
    " · {dates} 后来又提到",
    "「{task}」的产出",
    "在 {n} 场会上被提到",
    "  - 摘要：{text}",
    " · AI 自动归属",
    " · 你改过这张卡",
    "进行中",
    "已完成",
    "搁置",
)

# 4h：需求背景（［复制给 Claude Code］复制到剪贴板的 Markdown）的模板
COPY_4H_CONTEXT = (
    "> 声档生成的背景。纪要是完整的，先读纪要；原话在「逐字稿」里，时间戳是录音时间。这里不摘材料的内容，文件请直接打开看。",
    "# {title}（{project} · 需求 · {priority} · {status}）",
    "## 文件夹",
    "## 会议",
    "## 定了什么",
    "## 行动项",
    "## 产出",
    "（归档文件夹，纪要不在项目文件夹里）",
    "需求不存在",
)

# 4h：从材料里找到的词，接口的 text 和错误的说法（前端的文案在 copy.vocabulary.test.ts）
COPY_4H_WORDS = (
    "项目不存在",
    "这个词已经不在了",
    "这个词已经处理过了",
    "已超过撤销时间，请直接改回",
    "已经撤销过了",
    "这个词后来改过了，请在词典里直接改",
    "这个词不能记入词典",
    "已记入『{term}』",
    "已记入『{term}』，错写：{wrongs}",
    "已把『{wrongs}』记成『{term}』的错写",
    "『{term}』已经在词典里了",
    "已记入『{term}』；『{skipped}』已经用在别的词条上，没加成错写",
    "；『{skipped}』已经用在别的词条上，没加成错写",
    "『{skipped}』已经用在别的词条上，没加成错写",
    "至少留一个错写",
    "以后不再提『{term}』",
    "已撤销，『{term}』回到这里",
)

COPY_TABLES = {
    "4a 状态句": STATE_SENTENCES_4A,
    "4a 回答和撤销": ANSWER_COPY_4A,
    "4b 状态句和提到": COPY_4B,
    "4c 决议卡和时间线": COPY_4C,
    "4d 相关材料栏": COPY_4D,
    "4g 问答": COPY_4G,
    "4e 产出和可能过时": COPY_4E,
    "4f 关系图的线、局部图和来龙去脉": COPY_4F,
    "4h 00 索引.md": COPY_4H_INDEX,
    "4h 从材料里找到的词": COPY_4H_WORDS,
    "4h 需求背景": COPY_4H_CONTEXT,
}


def unquote(text: str) -> str:
    return QUOTED.sub("", text)


def problems(text: str) -> list[str]:
    plain = unquote(text)
    return [word for word in (*FORBIDDEN, *ALSO_FORBIDDEN) if word in plain]


def collect_copy(payload: Any) -> list[str]:
    """递归收接口返回里 text、label、ask、title、detail 的字符串值。"""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in COPY_KEYS and isinstance(value, str):
                found.append(value)
            else:
                found.extend(collect_copy(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(collect_copy(item))
    return found


def _skipped_nodes(tree: ast.AST) -> set[int]:
    """文档字符串、提示词常量（名字里有 PROMPT 或 SYSTEM）和 logger 调用里的字符串不算界面文字。"""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skipped.add(id(body[0].value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if any("PROMPT" in name.upper() or "SYSTEM" in name.upper() for name in names):
                skipped.update(id(child) for child in ast.walk(node))
        elif isinstance(node, ast.Call):
            func = node.func
            owner = func.value if isinstance(func, ast.Attribute) else None
            if isinstance(owner, ast.Name) and owner.id in ("logger", "log", "logging"):
                skipped.update(id(child) for child in ast.walk(node))
    return skipped


def ui_strings(path: Path) -> list[tuple[int, str]]:
    """模块里含汉字的字符串常量（行号，文字）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skipped = _skipped_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
        and HAN.search(node.value)
    ]


def test_quoted_speech_is_not_checked():
    assert problems("会上说的是『因为预算不够』") == []
    assert problems("因为预算不够") == ["因为"]
    assert problems("相近程度 80%") == ["%"]
    assert problems("看 ~/.config/ds/api-key") == ["~/.config"]


@pytest.mark.parametrize("table", sorted(COPY_TABLES))
def test_phase_four_copy_tables(table):
    for text in COPY_TABLES[table]:
        assert problems(text) == [], text
        # 不出现路径（『』里引用的原话和「9/21」这种日期不算，4e 的文件夹写在『能耗看板/』里）
        plain = re.sub(r"\d+(?:/\d+)+", "", unquote(text))
        assert "/" not in plain and "~" not in plain, text


def test_state_sentences_are_one_sentence_each():
    assert len(STATE_SENTENCES_4A) == 16
    for text in (*STATE_SENTENCES_4A, *STATE_SENTENCES_4B, *STATE_SENTENCES_4C, *TIMELINE_SENTENCES_4C):
        assert not re.search(r"[。！？!?]", text.rstrip("。")), text


def test_loose_state_sentences_match_the_module():
    from meeting_workbench.loose_mentions import LOOSE_SENTENCES

    assert LOOSE_SENTENCES == STATE_SENTENCES_4B


def test_decision_state_sentences_match_the_modules():
    from meeting_workbench.decisions import PAIR_SENTENCES
    from meeting_workbench.timeline import TIMELINE_SENTENCES

    assert PAIR_SENTENCES == STATE_SENTENCES_4C
    assert TIMELINE_SENTENCES == TIMELINE_SENTENCES_4C


def test_phase_four_get_payloads_4c(tmp_path):
    """4c 的样本库打一遍决议卡和时间线的 GET：text、label、title 这些键里没有不许出现的词。"""
    from datetime import date

    from meeting_workbench import decisions, timeline

    from .test_decisions_log import LIVE, changed_row, world

    db = world(tmp_path)
    changed_row(db, "old", "new")
    with db.autocommit() as connection:
        payloads = [decisions.requirement_log(connection, "r1", settings=LIVE)]
        for kind in timeline.KINDS:
            payloads.append(timeline.project_timeline(connection, "p", kind=kind, before=date(2026, 9, 28)))
    found = [text for payload in payloads for text in collect_copy(payload)]
    assert found, "样本什么字都没有，这个测试什么都没验证"
    assert [text for text in found if problems(text)] == []


@pytest.mark.parametrize("module", PHASE_FOUR_MODULES)
def test_phase_four_module_strings(module):
    path = PACKAGE / module
    if not path.exists():
        pytest.skip(f"{module} 还没有")
    bad = [(line, text) for line, text in ui_strings(path) if problems(text)]
    assert bad == []


def test_ui_strings_skip_docstrings_prompts_and_logs(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""因为这是文档。"""\n'
        'SYSTEM_PROMPT = "只因为提示词"\n'
        "def f():\n"
        '    """因为。"""\n'
        '    logger.info("因为日志 %s", 1)\n'
        '    return "界面上的话" + f"{1}个"\n',
        encoding="utf-8",
    )
    assert [text for _line, text in ui_strings(sample)] == ["界面上的话", "个"]


def test_collect_copy_walks_nested_payloads():
    payload = {
        "relation": {"quote": "原话不查", "file": {"name": "报价单.xlsx"}},
        "questions": [{"text": "是这条任务的交付物吗？", "ask": "看一下", "answers": ["yes", "no"]}],
        "state": {"kind": "waiting", "text": "还有 2 场会在整理关联", "action": None},
        "detail": "这条关联已经不在了",
        "label": 3,
    }
    assert sorted(collect_copy(payload)) == sorted(
        ["是这条任务的交付物吗？", "看一下", "还有 2 场会在整理关联", "这条关联已经不在了"]
    )


def test_related_panel_sentences_match_the_module():
    from meeting_workbench import related_read

    assert set(related_read.PANEL_SENTENCES) | {related_read.reading_text(3)} == set(STATE_SENTENCES_4D)


def test_phase_four_get_payloads_4d(tmp_path):
    """4d 的样本库打一遍栏（每种状态）、标过不相关的和关系图的相关线：text、label 这些键里没有不许出现的词。"""
    from datetime import date

    from meeting_workbench import related_read

    from .test_related import build, settings, worker_for

    w = build(tmp_path)
    payloads = []
    with w.db.autocommit() as connection:
        for config in (settings(), settings(links_enabled=False), settings(semantic_enabled=False)):
            payloads.append(related_read.panel(connection, "m", worker=None, settings=config, local=True))
    worker_for(w).run_round()
    with w.db.autocommit() as connection:
        payloads.append(related_read.panel(connection, "m", worker=None, settings=w.settings, local=True))
        payloads.append(related_read.rejected_items(connection, "m"))
        payloads.append(related_read.project_related(connection, "p", window="all", today=date(2026, 9, 28)))
    found = [text for payload in payloads for text in collect_copy(payload)]
    assert found, "样本什么字都没有，这个测试什么都没验证"
    assert [text for text in found if problems(text)] == []


def test_ask_texts_match_the_modules():
    from meeting_workbench import ask_retrieval, asks

    texts = [
        *ask_retrieval.NOTE_TEXTS.values(),
        ask_retrieval.TOO_SHORT_OR_LONG,
        ask_retrieval.BAD_CONTROL,
        *(text.format(seconds=90) for text, _retry in asks.STOP_TEXTS.values()),
        asks.WAITING_TEXT, asks.PROJECT_MISSING, asks.PLAN_EXPIRED, asks.JOB_EXPIRED, asks.PROJECT_BUSY,
        asks.NOTHING_TO_SEND, asks.CAPPED, asks.NO_KEY, asks.QA_OFF,
    ]
    assert set(texts) <= set(COPY_4G)
    assert len(asks.STOP_TEXTS) == 8


def test_phase_four_payloads_4g(tmp_path):
    """4g 的样本库打一遍 prepare、ask 和任务（好了、停了、在等）：text、title 这些键里没有不许出现的词。"""
    from .test_ask_api import FakeChat, ask, make, prepare

    client, app, headers, pending = make(tmp_path)
    payloads = [prepare(client, headers).json(), prepare(client, headers, "单列").json()]
    plan_id = payloads[0]["plan_id"]
    started = ask(client, headers, plan_id).json()
    payloads += [started, client.get(f"/api/ask/{started['job_id']}").json()]
    from meeting_workbench.llm import LLMError

    app.state.asks.chat = FakeChat(error=LLMError("timeout"))
    stopped = ask(client, headers, plan_id).json()
    payloads.append(client.get(f"/api/ask/{stopped['job_id']}").json())
    for bad in (ask(client, headers, "nope"), prepare(client, headers, "报")):
        payloads.append(bad.json())
    found = [text for payload in payloads for text in collect_copy(payload)]
    assert found, "样本什么字都没有，这个测试什么都没验证"
    assert [text for text in found if problems(text)] == []


# 『x/』里的文件夹只写最后一层，不写路径
_QUOTED_FOLDER = re.compile(r"『([^』]*)/』")


def one_level_folders(texts) -> list[str]:
    """『x/』里 x 中间还有「/」的句子（应该是空的）。"""
    return [text for text in texts for match in _QUOTED_FOLDER.finditer(text) if "/" in match.group(1)]


def test_4e_folders_are_one_level():
    assert any(_QUOTED_FOLDER.search(text) for text in COPY_4E)
    assert one_level_folders(COPY_4E) == []
    assert one_level_folders(["会后 3 天新增在『交付/能耗看板/』"]) == ["会后 3 天新增在『交付/能耗看板/』"]


def test_phase_four_payloads_4e(tmp_path):
    """4e 的样本库打一遍文件面板、预览、任务、需求卡、展开一场会和回答：text、ask、title 这些键里没有不许
    出现的词。"""
    from types import SimpleNamespace

    from meeting_workbench import decisions

    from .test_relation_questions import world
    from .test_tasks_api import write_headers

    w = world(tmp_path)
    live = SimpleNamespace(links_enabled=True, links_llm_enabled=True, links_llm_daily_calls=200)
    payloads = [
        w.client.get(f"/api/graph/files/{w.quote_id}").json(),
        w.client.get(f"/api/graph/files/{w.plan_id}").json()["questions"],
        w.client.get(f"/api/materials/files/{w.quote_id}/preview").json()["questions"],
        w.client.get("/api/tasks/t").json()["suggestions"],
        w.client.get("/api/graph/meetings/m").json()["decisions"],
    ]
    with w.db.autocommit() as connection:
        payloads.append(decisions.requirement_log(connection, "r", settings=live))
    found = [text for payload in payloads for text in collect_copy(payload)]
    assert "『报价单 v3』之后没改过，可能过时" in found and "会后 3 天新增在『能耗看板/』" in found
    headers = write_headers(w.client)
    produced_id = w.db.query_one("SELECT id FROM relations WHERE kind = 'produced'")["id"]
    w.db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = 't'")
    refused = w.client.post(f"/api/relations/{produced_id}/answer", json={"answer": "yes"}, headers=headers).json()
    found += collect_copy(refused)
    assert refused["detail"] in COPY_4E
    w.db.execute("UPDATE tasks SET status = 'pending_confirm' WHERE id = 't'")
    unconfirmed = w.client.post(f"/api/relations/{produced_id}/answer", json={"answer": "yes"}, headers=headers).json()
    found += collect_copy(unconfirmed)
    assert unconfirmed["detail"] in COPY_4E
    assert one_level_folders(found) == []
    # 决议原文（decision.text、决议卡的 text）和现读的材料片段（passage.text）在页面上放在『』里照原样显示，
    # 不受用词规则管
    quoted = {"总价下调 5%", "报价说明：总价在原基础上下调 3%，含税"}
    found = [text for text in found if text not in quoted]
    assert [text for text in found if problems(text)] == []


def test_local_graph_errors_match_the_module():
    from meeting_workbench import graph_local

    for text in (graph_local.FILE_MISSING, graph_local.FILE_NO_PROJECT, graph_local.MEETING_MISSING,
                 graph_local.DECISION_MISSING, graph_local.TASK_MISSING, graph_local.MEETING_NO_PROJECT,
                 graph_local.LATER_CHANGED_TEXT, graph_local.RESTATED_TEXT):
        assert text in COPY_4F


def test_phase_four_payloads_4f(tmp_path):
    """4f：用 4e 的样本库打一遍星图、局部图（相关开着）和来龙去脉：text、label、title、detail 里没有不许
    出现的词。"""
    from .test_relation_questions import world

    w = world(tmp_path)
    payloads = [
        w.client.get("/api/graph/projects/p").json(),
        w.client.get(f"/api/graph/files/{w.quote_id}/map", params={"related": 1}).json(),
        w.client.get(f"/api/graph/files/{w.plan_id}/map").json(),
        w.client.get("/api/graph/trace", params={"node": f"file:{w.quote_id}"}).json(),
        w.client.get("/api/graph/trace", params={"node": "m:m"}).json(),
        w.client.get("/api/graph/trace", params={"node": "m:nope"}).json(),
        w.client.get("/api/graph/files/999999/map").json(),
    ]
    found = [text for payload in payloads for text in collect_copy(payload)]
    assert any("之后没改过" in text for text in found) and any("的交付物吗？" in text for text in found)
    quoted = {"总价下调 5%", "写一版方案"}
    found = [text for text in found if text not in quoted]
    assert [text for text in found if problems(text)] == []


def test_index_copy_matches_the_module():
    """4h：索引里的固定字句都在字表里（字表进上面的用词检查）。"""
    from meeting_workbench import card_index

    module = [
        value
        for name, value in vars(card_index).items()
        if name.isupper() and isinstance(value, str) and HAN.search(value)
    ]
    module += list(card_index.REQUIREMENT_STATUS.values())
    assert sorted(module) == sorted(COPY_4H_INDEX + COPY_4H_CONTEXT)


def test_candidate_texts_match_the_module():
    from meeting_workbench import glossary_mining as gm

    module = {
        gm.PROJECT_MISSING, gm.TERM_GONE, gm.ALREADY_DONE, gm.UNDO_EXPIRED, gm.UNDO_TWICE, gm.TERM_CHANGED,
        gm.TERM_INVALID, gm.ACCEPTED_TEXT, gm.ACCEPTED_WRONGS_TEXT, gm.APPENDED_TEXT, gm.ALREADY_TEXT,
        gm.SKIPPED_TEXT, gm.SKIPPED_TAIL, gm.NOTHING_ADDED_TEXT, gm.KEEP_ONE_WRONG, gm.REJECTED_TEXT,
        gm.UNDONE_TEXT,
    }
    assert module == set(COPY_4H_WORDS)


def test_phase_four_payloads_4h(tmp_path):
    """4h：候选词的 GET、看板和三个写接口的 text、detail 里没有不许出现的词（词本身在『』里）。"""
    from meeting_workbench.db import Database

    from .gm_world import mine, world
    from .test_tasks_api import make_client, write_headers

    client, settings = make_client(tmp_path)
    settings.links_enabled = True
    db = Database(settings.database_path)
    world(tmp_path, db)
    mine(db)
    headers = write_headers(client)
    payloads = [
        client.get("/api/projects/p/glossary-candidates").json(),
        client.get("/api/projects/p/board").json().get("glossary_candidates"),
        client.post("/api/projects/p/glossary-candidates/accept", json={"key": "司美格鲁肽"}, headers=headers).json(),
        client.post("/api/projects/p/glossary-candidates/accept", json={"key": "司美格鲁肽"}, headers=headers).json(),
        client.post("/api/projects/p/glossary-candidates/undo", json={"key": "司美格鲁肽"}, headers=headers).json(),
        client.post("/api/projects/p/glossary-candidates/reject", json={"key": "驻场服务"}, headers=headers).json(),
        client.post("/api/projects/p/glossary-candidates/reject", json={"key": "没有"}, headers=headers).json(),
        client.get("/api/projects/nope/glossary-candidates").json(),
    ]
    found = [text for payload in payloads for text in collect_copy(payload)]
    assert any("以后不再提" in text for text in found) and any("已记入" in text for text in found)
    assert [text for text in found if problems(text)] == []


def test_requirement_context_payload_4h(tmp_path):
    """4h：需求背景的 Markdown（剪贴板上的文字）里没有不许出现的词。"""
    from meeting_workbench import card_index

    from .test_requirement_context import world

    db = world(tmp_path)[0]
    with db.autocommit() as connection:
        text = card_index.requirement_context(connection, "r-1")["markdown"]
    assert "## 定了什么" in text
    assert problems(text) == []
