"""需求标题「相近」的判定：和项目名同一套（project_names.names_similar），比的是轻键。

原来是子序列规则，字散在几处也算：AI 提的新需求名「数据导出」会被当成已有需求「数据权限与导出管理」的相近标题，
默默丢掉、会议页上也不提示；项目名那边早已收紧（智慧医院 / 智慧医疗院区）。三处共用 similar_title：
分类时过滤 AI 的新需求名、会议页的提示、候选名里「名字相近的文件夹」。
"""

import pytest

from meeting_workbench.name_hints import similar_title
from meeting_workbench.project_linking import ProjectLinker

from .test_name_hints import (
    _answer,
    _api,
    _hint,
    _link,
    _requirement,
    _requirement_hint_meeting,
)
from .test_project_linking import llm_answers, make_db, make_project, seed_meeting

# (一个标题, 另一个标题)：相近
SIMILAR = [
    ("数据看板", "数据 看板"),  # 轻键相同：空白、标点、大小写不算
    ("云图AI", "云图ai"),
    ("数据看板", "数据看板！"),
    ("数据看板", "云图数据看板"),  # 原样出现在另一个里面，多出来的在头或尾
    ("数据看板一期", "数据看板"),
    ("用户权限管理", "用户角色与权限管理"),  # 只是中间多了连着的一段，头尾都对得上
    ("权限组", "权限管理组"),
]
# 不相近
NOT_SIMILAR = [
    ("数据导出", "数据权限与导出管理"),  # 字散在几处：数据…导出，头对得上、尾对不上
    ("智慧医院", "智慧医疗院区"),  # 和项目名那边的反例一样
    ("看板", "数据看板"),  # 较短一方不到 3 个字
    ("云图二期", "云图三期"),  # 轻键不去「二期」，是两回事
    ("报价单v2", "报价单v3"),
    ("", "数据看板"),
]


@pytest.mark.parametrize(("first", "second"), SIMILAR)
def test_similar_titles_stay_similar_in_either_order(first, second):
    assert similar_title(first, second)
    assert similar_title(second, first)


@pytest.mark.parametrize(("first", "second"), NOT_SIMILAR)
def test_titles_whose_characters_are_only_scattered_are_not_similar(first, second):
    assert not similar_title(first, second)
    assert not similar_title(second, first)


# (已有的需求标题, AI 提的新需求名, 会不会被当成已有需求而丢掉)
CASES = {
    "m-substring": ("云图数据看板", "数据看板", True),
    "m-middle": ("用户角色与权限管理", "用户权限管理", True),
    "m-scattered": ("数据权限与导出管理", "数据导出", False),
}


def test_ai_new_requirement_name_is_dropped_only_for_a_similar_title(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI")
    for title, _name, _dropped in CASES.values():
        _requirement(db, project_id, title)
    for meeting_id, (_title, name, _dropped) in CASES.items():
        seed_meeting(
            db, meeting_id, "云图AI 周会", f"# 摘要\n{name}", segments=[(0, name), (1, name)]
        )
    llm_answers(
        monkeypatch, *[_answer("云图AI", new_requirement=name) for _t, name, _d in CASES.values()]
    )

    ProjectLinker(db, settings).link_pending(max_batches=10)

    kept = {meeting_id: _link(db, meeting_id)["new_requirement_name"] for meeting_id in CASES}
    assert kept == {
        meeting_id: None if dropped else name for meeting_id, (_t, name, dropped) in CASES.items()
    }


def test_meeting_page_hint_follows_the_same_rule(tmp_path):
    client, _settings, _headers, db, _root = _api(tmp_path)
    project_id = make_project(db, "云图AI")
    for title, _name, _dropped in CASES.values():
        _requirement(db, project_id, title)
    for meeting_id, (_title, name, _dropped) in CASES.items():
        _requirement_hint_meeting(db, meeting_id, project_id, name=name, segments=[(0, name)])

    shown = {meeting_id: _hint(client, meeting_id) is not None for meeting_id in CASES}

    assert shown == {meeting_id: not dropped for meeting_id, (_t, _n, dropped) in CASES.items()}


def test_similar_folder_candidates_for_a_requirement_follow_the_same_rule(tmp_path):
    client, _settings, headers, db, browse = _api(tmp_path)
    root = browse / "云图AI"
    for folder in ("数据权限与导出管理", "数据导出工具", "数据导出"):
        (root / folder).mkdir(parents=True)
    project = client.post(
        "/api/projects", json={"name": "云图AI", "material_roots": [str(root)]}, headers=headers
    ).json()
    _requirement_hint_meeting(
        db, "m-1", project["id"], name="数据导出", segments=[(0, "数据导出"), (1, "数据导出")]
    )
    client.app.state.roots_cache.refresh()

    body = client.get("/api/meetings/m-1/name-candidates").json()

    assert [
        (item["name"], bool(item["folder_path"]), bool(item["similar_folder_path"]))
        for item in body["candidates"]
    ] == [
        ("数据导出", True, False),  # 同名文件夹
        ("数据导出工具", False, True),  # 原样出现在里面：相近
        # 「数据权限与导出管理」只是字散开，不在候选里
    ]
