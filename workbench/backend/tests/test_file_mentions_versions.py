"""词干并组之后，组内挑文件要认得并进来的几种写法：「第 N 版 / 第 N 稿」当版本号，「终稿 / 最终稿」当终版。

file_stems 把 (v2)、第三版、终稿、最终稿这些结尾的修饰去掉、并进同一个词干；file_mentions 在同一个词干的几份
文件里挑哪一份时，原来只认文件名里的 vN 和「最终版 / 终版 / 定稿 / final」：「报价单 第三版」和「报价单（终稿）」
并进「报价单」以后，会上说「第三版」「终稿」挑不到它们、退回按会议日期挑。这里把两边写法并排对一遍。
"""

import pytest

from meeting_workbench import file_mentions
from meeting_workbench.file_stems import derive_stem

from .test_file_mentions import add_file, mentions, run, said, setup
from .test_graph import add_meeting

# 同一份「报价单」的几种叫法（file_stems 都并进词干「报价单」）：(文件名, 版本号, 是不是终版这一类)
NAMES = [
    ("报价单 (v2).xlsx", (2,), False),
    ("报价单（V2.1）.xlsx", (2, 1), False),
    ("报价单 第三版.docx", (3,), False),
    ("报价单第 3 版.docx", (3,), False),
    ("报价单第十二版.docx", (12,), False),
    ("报价单_第二稿.docx", (2,), False),
    ("报价单-第2稿.docx", (2,), False),
    ("报价单（终稿）.xlsx", None, True),
    ("报价单最终稿.xlsx", None, True),
    ("报价单（定稿）.xlsx", None, True),
    ("报价单审定稿.docx", None, True),
    ("报价单 终版.docx", None, True),
    ("报价单（最终版）.xlsx", None, True),
    ("报价单 (final).xlsx", None, True),
    ("报价单 第三版（终稿）.xlsx", (3,), True),
    ("报价单 终稿 v2.xlsx", (2,), True),
    # 去掉的是别的修饰：既不是版本号，也不是终版
    ("报价单初稿.xlsx", None, False),
    ("报价单修订稿.xlsx", None, False),
    ("报价单送审稿.docx", None, False),
    ("报价单 副本 2.xlsx", None, False),
    ("报价单.xlsx", None, False),
]


@pytest.mark.parametrize(("name", "number", "final"), NAMES)
def test_the_written_forms_the_stem_rule_strips_are_the_ones_picking_reads(name, number, final):
    # 先确认这张表没写错：它们确实都在同一个词干组里
    assert derive_stem(name) == "报价单"
    assert file_mentions.version_number(name) == number
    assert bool(file_mentions.final_files([{"name": name}])) is final


def test_version_tag_keeps_the_written_form_for_the_full_name_needle():
    tag = file_mentions.version_tag
    assert tag("报价单 第三版.docx") == "第三版"
    assert tag("报价单第 3 版.docx") == "第3版"
    assert tag("报价单_第二稿.docx") == "第二稿"
    assert tag("报价单 (v2).xlsx") == "v2"
    # 名字里有好几个时取最后一个（vN 原来就是这样）
    assert tag("报价单 v2 第三版.xlsx") == "第三版"
    assert tag("报价单 第三版 v4.xlsx") == "v4"
    # 全角数字、全角括号和半角一样
    assert tag("报价单 第３版.docx") == "第3版"
    assert tag("报价单（Ｖ２）.xlsx") == "v2"
    # 没有版本号的
    assert tag("报价单（终稿）.xlsx") is None
    assert tag("第三季度报告.docx") is None


def test_numbers_beyond_twenty_are_a_tag_but_not_a_version_number():
    # 口头版本号只认 1 到 20（file_mentions.cn_number），文件名和它对齐：没有版本号，但整个写法照样当全名针
    assert file_mentions.version_tag("报价单 第一百版.docx") == "第一百版"
    assert file_mentions.version_number("报价单 第一百版.docx") is None
    assert not file_mentions.is_version("报价单 第一百版.docx", 100)


def test_is_version_matches_chinese_and_arabic_numbers_alike():
    assert file_mentions.is_version("报价单 第三版.docx", 3)
    assert file_mentions.is_version("报价单 第3版.docx", 3)
    assert file_mentions.is_version("报价单 第四稿.docx", 4)
    assert file_mentions.is_version("报价单 v3.0.docx", 3)
    assert not file_mentions.is_version("报价单 第三版.docx", 4)
    # 第 2.1 版这类带小数的不当成「第二版」
    assert not file_mentions.is_version("报价单 (v2.1).xlsx", 2)


def test_dotted_versions_are_read_as_their_own_numbers():
    """version_tag 先过 light_key 再拆数字时小数点被去掉了：V1.2 成了 12、v2.0 成了 20，口头说「第二版」认不出
    v2.0（生产里带点版本号的文件名有 72 个）。"""
    number = file_mentions.version_number
    assert (number("报价单 V1.2.docx"), number("报价单 v2.0.xlsx"), number("原型V1.0.1.png")) == (
        (1, 2),
        (2, 0),
        (1, 0, 1),
    )
    assert file_mentions.is_version("报价单 v3.0.docx", 3)
    assert file_mentions.is_version("报价单 v3.0.0.docx", 3)
    assert not file_mentions.is_version("报价单 v3.0.docx", 30)
    assert not file_mentions.is_version("报价单 V1.2.docx", 12)
    # 全名针仍按折叠后的写法比：逐字稿里说「报价单 V1.2」折叠成 报价单v12
    assert file_mentions.version_tag("报价单 V1.2.docx") == "v12"


def test_a_spoken_version_picks_the_dotted_file(tmp_path):
    db, root_id = setup(tmp_path)
    v2 = add_file(db, root_id, "报价/报价单_v2.0.xlsx", day="2026-09-05")
    add_file(db, root_id, "报价/报价单_v3.1.xlsx", day="2026-09-10")
    base = add_file(db, root_id, "报价/报价单.xlsx", day="2026-09-20")
    add_meeting(db, "second", ago=1, project_id="p", segments=said("报价单第二版价格偏高"))
    add_meeting(db, "twelfth", ago=1, project_id="p", segments=said("报价单第十二版价格偏高"))

    run(db)

    assert mentions(db, "second")["报价单"]["file_id"] == v2
    # v1.2 不是第十二版：没有这一版，按会议日期挑
    assert mentions(db, "twelfth")["报价单"]["file_id"] == base


def test_spoken_draft_numbers_are_version_numbers_too():
    spoken = file_mentions.spoken_version
    assert (spoken("第二稿"), spoken("报价单第3稿"), spoken("第十二稿"), spoken("这是第四稿")) == (
        2,
        3,
        12,
        4,
    )
    # 「第一稿件」是另一个词；第一版本、第三版仍是原来的认法
    assert spoken("第一稿件要归档") is None
    assert (spoken("第三版"), spoken("第一版本")) == (3, 1)


def test_spoken_final_draft_words_count_as_final():
    near = file_mentions.near_final
    assert near("报价单终稿发出去了", 0, 3)
    assert near("报价单最终稿发出去了", 0, 3)
    assert near("报价单终版发出去了", 0, 3)
    assert not near("报价单初稿发出去了", 0, 3)
    assert not near("报价单修订稿发出去了", 0, 3)


# 并进同一组的几份：会上说「第三版」「终稿」「v2」各挑到对应那份；没说就按会议日期挑开会前最新的（报价单.xlsx）
FILES = {
    "base": ("报价/报价单.xlsx", "2026-09-20"),
    "v2": ("报价/报价单 (v2).xlsx", "2026-09-05"),
    "third": ("报价/报价单 第三版.docx", "2026-09-10"),
    "fourth": ("报价/报价单 第4稿.docx", "2026-09-12"),
    "final": ("报价/报价单（终稿）.xlsx", "2026-09-15"),
}
SAID = {
    "third-written-out": ("报价单第三版我看过了", "third"),
    "third-with-words-between": ("报价单的第三版我们再改一下", "third"),
    "v2-written-out": ("报价单 v2 要改", "v2"),
    "second-edition-means-v2": ("报价单第二版价格偏高", "v2"),
    "final-draft": ("报价单终稿发出去了", "final"),
    "last-final-draft": ("报价单最终稿发出去了", "final"),
    "fourth-draft-in-chinese": ("报价单第四稿还没改完", "fourth"),
    "fourth-draft-written-out": ("报价单第4稿还没改完", "fourth"),
    "no-version-said": ("报价单明天发", "base"),
    "first-draft-is-not-a-version": ("报价单第一稿件要归档", "base"),
}


def test_each_spoken_version_or_final_picks_its_own_file_in_the_merged_group(tmp_path):
    db, root_id = setup(tmp_path)
    ids = {key: add_file(db, root_id, rel, day=day) for key, (rel, day) in FILES.items()}
    for meeting_id, (text, _expected) in SAID.items():
        add_meeting(db, meeting_id, ago=1, project_id="p", segments=said(text))

    run(db)

    picked = {meeting_id: mentions(db, meeting_id)["报价单"]["file_id"] for meeting_id in SAID}
    assert picked == {meeting_id: ids[expected] for meeting_id, (_text, expected) in SAID.items()}
