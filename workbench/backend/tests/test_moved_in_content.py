"""早就读过的材料挪进（或复制进）另一个项目的资料盘：内容标识不变、片段 id 都不大于这个项目已算过的
记号。相关（H3）和可能过时（H2）都要把这份「新进本项目的旧内容」补比对一次，不整项目重算。"""

from __future__ import annotations

from meeting_workbench.db import utc_now

from . import test_affects as ta
from .test_material_search import add_file
from .test_related import KEY_A, TOPIC_A, add_content, build, related_rows, worker_for


def _related_with_other_project(tmp_path):
    w = build(tmp_path, chunk_a=False)
    other = tmp_path / "别的项目资料"
    other.mkdir()
    w.db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('q', ?, ?)",
        (str(other), utc_now()),
    )
    q_root = w.db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'q'")["id"]
    add_content(w.db, KEY_A, ["。".join(TOPIC_A)])
    add_file(w.db, q_root, "接口文档.docx", key=KEY_A, mtime=5)
    w.vectors.refresh()
    for _ in range(3):
        worker_for(w).run_round()
    assert related_rows(w.db) == []  # 还在别的项目里，本项目当然没有
    return w, q_root


def test_related_content_moved_in_from_another_project_is_compared(tmp_path):
    w, q_root = _related_with_other_project(tmp_path)
    scan = w.db.query_one(
        "SELECT dirty, chunk_mark FROM meeting_related_scan WHERE meeting_id = 'm'"
    )
    chunk = w.db.query_one(
        "SELECT MAX(id) AS id FROM material_chunks WHERE content_key = ?", (KEY_A,)
    )
    assert scan["dirty"] == 0 and chunk["id"] <= scan["chunk_mark"]

    w.db.execute("UPDATE material_files SET gone_at = ? WHERE root_id = ?", (utc_now(), q_root))
    add_file(w.db, w.root, "需求/接口文档.docx", key=KEY_A, mtime=5)
    w.vectors.refresh()
    for _ in range(3):
        worker_for(w).run_round()

    assert [row["ident"] for row in related_rows(w.db)] == [f"m|{KEY_A}"]
    # 补比对是增量：会没被整场重算（台账的 dirty 没加过、签名没变）
    after = w.db.query_one(
        "SELECT dirty, chunk_mark FROM meeting_related_scan WHERE meeting_id = 'm'"
    )
    assert dict(after) == dict(scan)


def test_related_content_copied_into_a_second_project_is_compared(tmp_path):
    w, _q_root = _related_with_other_project(tmp_path)
    add_file(w.db, w.root, "需求/接口文档.docx", key=KEY_A, mtime=5)
    w.vectors.refresh()
    for _ in range(3):
        worker_for(w).run_round()
    assert [row["ident"] for row in related_rows(w.db)] == [f"m|{KEY_A}"]


def test_affects_content_moved_in_from_another_project_is_matched(tmp_path):
    w = ta.world(tmp_path)
    w.db.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '别的项目', 'x')")
    q_root = w.db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('q', '/材料/别的', 'x')"
    )
    ta.meeting(w.db, "m", "总价下调 5%")
    # 报价单先在别的项目里读好；本项目随后又进了一份（片段 id 更大），会的记号越过了报价单的片段
    quote = ta.material(
        w.db, q_root, "报价/报价单 v3.xlsx", "封面", "报价说明：总价在原基础上下调 3%，含税"
    )
    ta.material(w.db, w.root, "周报/周报.docx", "这周没什么事")
    ta.run(w.db)
    assert ta.open_rows(w.db) == []
    scan = w.db.query_one("SELECT affects_chunk_mark FROM decision_scan WHERE meeting_id = 'm'")
    top = w.db.query_one(
        "SELECT MAX(id) AS id FROM material_chunks WHERE content_key = ?", (quote["content_key"],)
    )
    assert top["id"] <= scan["affects_chunk_mark"]

    w.db.execute("UPDATE material_files SET gone_at = 'x' WHERE root_id = ?", (q_root,))
    ta.material(
        w.db,
        w.root,
        "报价/报价单 v3.xlsx",
        "封面",
        "报价说明：总价在原基础上下调 3%，含税",
        key=quote["content_key"],
    )
    ta.run(w.db, now=ta.at(0.01))

    assert ta.names(w.db) == ["报价单 v3.xlsx"]
    assert (
        w.db.query_one("SELECT affects_chunk_mark FROM decision_scan WHERE meeting_id = 'm'")[
            "affects_chunk_mark"
        ]
        == scan["affects_chunk_mark"]
    )
    # 补过一次就记住了：再跑一轮什么都不做
    assert ta.run(w.db, now=ta.at(0.02))["tried"] == 0
