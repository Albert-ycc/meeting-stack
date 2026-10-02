"""文件名词干：结尾的版本修饰（括号里的版本号、第 N 版 / 第 N 稿、终稿 / 初稿这类）。

同一份「报价单」的各种叫法要并进同一个词干，会上说「报价单」才连得上它们；词干本身含这些字的
（「第三季度报告」「终稿评审会纪要」）不能被削掉。
"""

import time

import pytest

from meeting_workbench.file_stems import derive_stem, stem_key


@pytest.mark.parametrize(
    "name",
    [
        # 括号里的版本号：中英文括号、前面有无空格、大小写、小数点版本
        "报价单 (v2).xlsx",
        "报价单(v2).xlsx",
        "报价单（v2）.xlsx",
        "报价单 （V2）.xlsx",
        "报价单 (V2.1).xlsx",
        "报价单 [v3].xlsx",
        "报价单【v3】.xlsx",
        "报价单 ( v2 ).xlsx",
        # 第 N 版、第 N 稿：阿拉伯数字、中文数字、中间有空格
        "报价单 第三版.docx",
        "报价单第三版.docx",
        "报价单 第3版.docx",
        "报价单 第 3 版.docx",
        "报价单第十二版.docx",
        "报价单_第二稿.docx",
        "报价单-第2稿.docx",
        # 终稿、初稿、最终稿、修订稿、修改稿：有无括号、有无空格
        "报价单（终稿）.xlsx",
        "报价单 (终稿).xlsx",
        "报价单终稿.xlsx",
        "报价单 终稿.xlsx",
        "报价单-终稿.xlsx",
        "报价单（初稿）.xlsx",
        "报价单初稿.xlsx",
        "报价单最终稿.xlsx",
        "报价单（最终稿）.xlsx",
        "报价单修订稿.xlsx",
        "报价单（修改稿）.xlsx",
        # 原来只认不带括号的修饰，带括号写法也并进来
        "报价单（定稿）.xlsx",
        "报价单(修订版).xlsx",
        "报价单（最终版）.xlsx",
        "报价单 (final).xlsx",
        "报价单（FINAL）.xlsx",
        "报价单 (副本).xlsx",
        # 叠在一起：从后往前一层层去
        "报价单 第三版（终稿）.xlsx",
        "报价单(v2)(1).xlsx",
        "报价单 终稿 v2.xlsx",
        "报价单 (v2) 260926.xlsx",
        "260926 报价单 第三版 终稿.xlsx",
        # 原有的写法照旧
        "报价单.xlsx",
        "报价单 v3 终版.xlsx",
        "报价单（1）.xlsx",
        "报价单 副本 2.xlsx",
        "报价单_v1.2.xlsx",
    ],
)
def test_versioned_names_collapse_into_the_base_stem(name):
    assert derive_stem(name) == "报价单"
    assert stem_key(derive_stem(name)) == stem_key("报价单")


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        # 词干本身含这些字：不在结尾，不能削
        ("第三季度报告.docx", "第三季度报告"),
        ("终稿评审会纪要.docx", "终稿评审会纪要"),
        ("初稿评审记录.docx", "初稿评审记录"),
        ("第三版本规划.docx", "第三版本规划"),
        ("第三版权说明.docx", "第三版权说明"),
        ("报价单 (v2) 评审.xlsx", "报价单 (v2) 评审"),
        # 结尾的字长得像、但不是修饰
        ("最终稿件管理办法.docx", "最终稿件管理办法"),
        ("审计底稿.xlsx", "审计底稿"),
        ("讲稿.docx", "讲稿"),
        ("第一稿件.docx", "第一稿件"),
        # 括号里是别的内容：不是版本号
        ("报价单(终稿评审).xlsx", "报价单(终稿评审)"),
        ("报价单(v2版本说明).xlsx", "报价单(v2版本说明)"),
        ("报价单 (吉士医医生端 · 一期).png", "报价单 (吉士医医生端 · 一期)"),
        ("报价单(2024).xlsx", "报价单(2024)"),
        ("论文[3].pdf", "论文[3]"),
        # 英文：v 前面是字母的不是版本号（dev3、rev2）
        ("rev2.docx", "rev2"),
        ("Quote(rev2).xlsx", "Quote(rev2)"),
        ("Quote (v2).xlsx", "Quote"),
        # 整个名字只剩修饰词时留着，不削成空
        ("终稿.docx", "终稿"),
        ("第三版.docx", "第三版"),
        ("final.docx", "final"),
    ],
)
def test_names_that_only_look_like_versions_are_left_alone(name, stem):
    assert derive_stem(name) == stem


# 文件名上限 255 个字符（exFAT 按 UTF-16 单元算，全是汉字也能有 255 个）。最坏的输入：一长串分隔符、
# 反复可削的修饰、没闭合的括号、一长串空白。规则加了括号和第 N 版以后不能比原来更慢。
_WORST_CASES = {
    "分隔符": "报价单" + "-" * 252,
    "空白": "报价单" + " " * 252,
    "混合分隔符": "报价单" + "_.-· " * 50,
    # 点和间隔号不会被折叠，是原来规则里回溯最多的一类
    "点和间隔号交替": "报价单" + ".·" * 125 + "x",
    "间隔号": "报价单" + "·" * 251 + "x",
    "点": "报价单" + "." * 251 + "x",
    "间隔号后接括号": "报价单" + "·" * 200 + "(v",
    "重复的括号版本": "报价单" + "(v1)" * 63,
    "重复的括号副本": "报价单" + "(1)" * 84,
    "重复的第N版": "报价单" + "第一版" * 84,
    "重复的终稿": "报价单" + " 终稿" * 84,
    "没闭合的括号加空白": "报价单(" + " " * 240,
    "很多个没闭合的括号": "报价单" + "( " * 120,
    "方括号加空白": "报价单[" + " " * 240,
    "第加数字不接版": "报价单第" + "一" * 240,
    "第加空白": "报价单第" + " " * 240,
}


@pytest.mark.parametrize("label", sorted(_WORST_CASES))
def test_worst_case_file_names_stay_fast(label):
    name = _WORST_CASES[label]
    assert len(name) <= 255
    started = time.perf_counter()
    derive_stem(name)
    elapsed = time.perf_counter() - started
    # 实测每条在几毫秒以内；给 0.5 秒是防回溯失控（超线性时同样长度的串要几十秒），不是性能指标
    assert elapsed < 0.5, f"{label} 用了 {elapsed:.3f} 秒"


def test_stem_key_ignores_the_bracket_style():
    keys = {
        stem_key(derive_stem(name))
        for name in (
            "报价单 (v2).xlsx",
            "报价单（v2）.xlsx",
            "报价单[v2].xlsx",
            "报价单【v2】.xlsx",
        )
    }
    assert keys == {"报价单"}
