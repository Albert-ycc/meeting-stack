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
COPY_TABLES = {"4a 状态句": STATE_SENTENCES_4A, "4a 回答和撤销": ANSWER_COPY_4A}


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
    for text in STATE_SENTENCES_4A:
        assert not re.search(r"[。！？!?]", text.rstrip("。")), text


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
