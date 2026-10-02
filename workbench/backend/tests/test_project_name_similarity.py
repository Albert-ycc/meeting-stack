"""项目名「相近」的判定：新建项目问「是不是它」、新项目回扫、同名文件夹三处共用 project_names.names_similar。

较短一方（归一化后）至少 3 个字，并且满足下面任一条才算相近：
- 原样出现在较长一方里（多出来的字只在开头或结尾）；
- 较长一方只是在较短一方中间多了一段连着的字，头尾都对得上（「云图用药」和「云图科研用药」）。
字都在、但散在几处的不算（「智慧医院」和「智慧医疗院区」：中间多了「疗」、结尾又多了「区」）。
"""

import pytest

from meeting_workbench.project_names import _match_kind, names_similar
from meeting_workbench.project_profile import norm_key

from .test_attribution_state import _add_link
from .test_project_linking import seed_meeting
from .test_project_names import _create, _project, _setup, _state

SIMILAR = [
    # 多出来的字在结尾、开头、两头
    ("云图科研", "云图科研用药"),
    ("云图AI", "云图AI资料"),
    ("北辰仓储", "北辰仓储资料"),
    ("用药管理", "云图用药管理"),
    ("科研用药", "云图科研用药平台"),
    ("吉士医", "CACA内容征集吉士医执行三期"),
    # 中间多了或少了一段连着的字（缩写、错一个字）
    ("云图科研用药", "云图用药"),
    ("云图科研用药", "云图科研药"),
    ("云图科研用药", "云图科研用要药"),
    ("智慧医院", "智慧医疗院"),
    ("会议预约需求", "会议预约数据汇总报表需求"),
]

NOT_SIMILAR = [
    # 字都在、但散在几处：不是缩写，是两个名字
    ("智慧医院", "智慧医疗院区"),
    ("数据中台", "数据治理中心平台"),
    ("会议预约", "会议室预约系统"),
    ("营养管理", "营养师管理平台"),
    ("云图科研", "云科图研用药"),
    # 不够 3 个字
    ("云药", "云图科研用药"),
    ("云图", "云图科研"),
    # 同样长但不一样
    ("云图科研", "云图科技"),
    # 完全相同的不是「相近」，是「同名」（find_similar_project 先走同名那一轮）
    ("云图科研", "云图科研"),
]


@pytest.mark.parametrize(("short", "long"), SIMILAR)
def test_similar_names(short, long):
    assert names_similar(norm_key(short), norm_key(long))
    assert names_similar(norm_key(long), norm_key(short))


@pytest.mark.parametrize(("left", "right"), NOT_SIMILAR)
def test_names_that_only_share_scattered_characters_are_not_similar(left, right):
    assert not names_similar(norm_key(left), norm_key(right))
    assert not names_similar(norm_key(right), norm_key(left))


def test_creating_a_project_does_not_ask_about_scattered_names(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    existing = _project(client, headers, "智慧医疗院区")

    # 只差一个字的照样问（先问它：下面的「智慧医院」一建成，这里就会同时和两个项目相近）
    near = _create(client, headers, "智慧医疗院")
    scattered = _create(client, headers, "智慧医院")

    assert near.status_code == 409
    assert near.json()["suggestion"]["project_id"] == existing["id"]
    assert near.json()["suggestion"]["match"] == "similar"
    assert scattered.status_code == 200, scattered.text


def test_folder_match_kind_uses_the_same_rule():
    assert _match_kind("云图AI资料", ["云图AI"]) == "similar"
    assert _match_kind("云图AI", ["云图 ai"]) == "exact"
    assert _match_kind("会议预约-数据汇总报表需求", ["会议预约需求"]) == "similar"
    assert _match_kind("智慧医院", ["智慧医疗院区"]) is None
    assert _match_kind("智慧医疗院区", ["智慧医院"]) is None


def test_new_project_rescan_does_not_flag_meetings_named_with_a_scattered_name(
    tmp_path, monkeypatch
):
    from .test_project_names import _no_llm

    client, _settings, headers, db, _root = _setup(tmp_path)
    _no_llm(monkeypatch)
    seed_meeting(db, "m-scattered", "现场踏勘")
    _add_link(db, "m-scattered", "unresolved", new_project_name="智慧医院")
    seed_meeting(db, "m-near", "现场踏勘")
    _add_link(db, "m-near", "unresolved", new_project_name="智慧医疗院")

    created = _project(client, headers, "智慧医疗院区")

    assert created["needs_review_meeting_ids"] == ["m-near"]
    assert _state(client, "m-near")["state"] == "needs_review"
    assert _state(client, "m-scattered")["state"] == "new_project"
