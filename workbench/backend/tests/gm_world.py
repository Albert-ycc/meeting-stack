"""4h 挖词测试共用的样本：三个项目，项目 p 的材料里埋了几个专属词。"""

from datetime import UTC, datetime

from meeting_workbench import glossary_mining as gm
from meeting_workbench.db import Database

from .test_material_search import add_content, add_file, add_root
from .test_search import add_meeting, add_project

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def body(*words, times=2):
    """每个词各出现 times 次，用标点和数字隔开（数字和标点会切断汉字串）。"""
    return "".join(f"{word}，第{index}条。" for index, word in enumerate(words * times))


def make_db(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    return db


def world(tmp_path, db=None):
    db = db or make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "北辰仓")
    add_project(db, "r", "数据中台")
    roots = {
        pid: add_root(db, pid, tmp_path / name)
        for pid, name in (("p", "云图目录"), ("q", "北辰目录"), ("r", "中台目录"))
    }
    texts = {
        "k1": body("司美格鲁肽", "驻场服务", "入组标准", "质量控制", "甲状腺髓样癌"),
        "k2": body("司美格鲁肽", "驻场服务", "入组标准", "质量控制", "甲状腺髓样癌"),
        "k3": body("驻场服务", "质量控制"),
    }
    for index, (key, text) in enumerate(texts.items(), start=1):
        add_content(db, key, [text])
        add_file(db, roots["p"], f"方案{index}.docx", key=key)
    add_file(db, roots["p"], "能耗看板说明.docx", key=None)
    add_file(db, roots["p"], "能耗看板汇总.docx", key=None)
    for pid, key in (("q", "kq"), ("r", "kr")):
        add_content(db, key, [body("质量控制")])
        add_file(db, roots[pid], "规范.docx", key=key)
    add_meeting(
        db,
        "m1",
        date="2026-09-20T10:00:00",
        project_id="p",
        segments=["这次司美格鲁太的剂量先按", "司美格鲁肽要再确认一次", "甲状腺髓样癌病史排除"],
    )
    add_meeting(
        db,
        "m2",
        date="2026-09-26T10:00:00",
        project_id="p",
        segments=["司美格鲁太再看一下", "司美格鲁肽的供货"],
    )
    add_meeting(
        db, "mq", date="2026-09-21T10:00:00", project_id="q", segments=["甲状腺髓样癌的病例"]
    )
    add_meeting(
        db, "mr", date="2026-09-22T10:00:00", project_id="r", segments=["甲状腺髓样癌也要看"]
    )
    return db, roots


def mine(db, project_id="p", now=NOW):
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=now)
        return gm.project_pass(conn, project_id, now)


def rows(db, project_id="p", status=None):
    sql = "SELECT * FROM glossary_candidates WHERE project_id = ?"
    params = [project_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return db.query_all(sql + " ORDER BY term_key, wrong", params)


def terms(db, project_id="p", status="pending"):
    return {(row["term"], row["wrong"]) for row in rows(db, project_id, status)}
