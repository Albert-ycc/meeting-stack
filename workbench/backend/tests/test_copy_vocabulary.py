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
COPY_TABLES = {
    "4a 状态句": STATE_SENTENCES_4A,
    "4a 回答和撤销": ANSWER_COPY_4A,
    "4b 状态句和提到": COPY_4B,
    "4c 决议卡和时间线": COPY_4C,
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
        # 不出现路径
        assert "/" not in text and "~" not in text, text


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
