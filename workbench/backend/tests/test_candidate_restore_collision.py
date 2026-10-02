"""撤销丢掉的候选（restore_candidate）时，同项目已经有同名的待认领候选：不建第二条，提示用户。

同项目同名的待认领候选只该有一条（抽取时并进那条、会议改归属时 reconcile_moved 并进那条）。撤销丢掉绕过了这两道：
丢掉之后同项目又出了一条同名的（比如撤销合并把一条候选放回了待认领），撤回来墙上就有两条同名。
并进已有那条做不到可逆（候选没了，丢掉再撤销回不到原来两条各自的样子），也会丢掉被撤回那条自己的说明和同一场会的
第二句原话，所以选了提示：被丢掉的那条原样留在「已丢掉」里，30 天内处理完那一条再撤销。
"""

from __future__ import annotations

from meeting_workbench.db import utc_now
from meeting_workbench.requirement_candidates import insert_candidate

from .requirement_pool_world import meeting_id, project_id
from .test_requirement_candidates import post
from .test_requirement_pool import candidate, make_world

TITLE = "京东仓签收凭证"


def same_name_pair(db):
    """医米项目里两场会（京东对接、EDC 选型）各抽出一条同名候选，并在京东那条上挂一条任务。"""
    first = candidate(db, "receipt", TITLE)
    with db.transaction() as connection:
        second = insert_candidate(connection, meeting_id=meeting_id("edc"), title=TITLE)
    now = utc_now()
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, assignee, meeting_id, project_id,
                             candidate_id, status_changed_at, created_at, updated_at)
           VALUES ('task-receipt', '跟京东确认签收凭证怎么回传', 'pending_confirm', 'ai', 'me', ?, ?, ?,
                   ?, ?, ?)""",
        (meeting_id("jd"), project_id("yimi"), first, now, now, now),
    )
    return first, second


def test_restore_is_refused_while_the_project_has_a_same_name_pending_candidate(tmp_path):
    client, headers, db = make_world(tmp_path)
    first, second = same_name_pair(db)
    post(client, headers, f"/api/requirement-candidates/{first}/drop")
    dropped = db.query_one(
        "SELECT status, dropped_at, drop_undo FROM requirement_candidates WHERE id=?", (first,)
    )

    refused = post(client, headers, f"/api/requirement-candidates/{first}/restore")

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "同项目里已经有一条同名的待认领候选「京东仓签收凭证」，先认领、合并或丢掉那一条，再撤销这条"
    )
    # 被丢掉的那条原样留着，摘下来的任务没挂回去，已有的那条没动
    assert (
        db.query_one(
            "SELECT status, dropped_at, drop_undo FROM requirement_candidates WHERE id=?", (first,)
        )
        == dropped
    )
    assert db.query_one("SELECT candidate_id FROM tasks WHERE id='task-receipt'") == {
        "candidate_id": None
    }
    assert db.query_one("SELECT status FROM requirement_candidates WHERE id=?", (second,)) == {
        "status": "pending"
    }

    # 把那一条处理掉（丢掉）以后，撤销照常：任务挂回来
    post(client, headers, f"/api/requirement-candidates/{second}/drop")
    restored = post(client, headers, f"/api/requirement-candidates/{first}/restore")
    assert restored.status_code == 200 and restored.json()["status"] == "pending"
    assert db.query_one("SELECT candidate_id FROM tasks WHERE id='task-receipt'") == {
        "candidate_id": first
    }


def test_same_name_in_another_project_or_without_project_does_not_block_the_restore(tmp_path):
    client, headers, db = make_world(tmp_path)
    first = candidate(db, "receipt", TITLE)
    with db.transaction() as connection:
        insert_candidate(connection, meeting_id=meeting_id("family"), title=TITLE)  # 恒瑞健康
    post(client, headers, f"/api/requirement-candidates/{first}/drop")

    assert post(client, headers, f"/api/requirement-candidates/{first}/restore").status_code == 200

    # 会议没归项目：候选不按名字去重（和抽取时 _pending_same_name 一致）
    post(client, headers, f"/api/requirement-candidates/{first}/drop")
    with db.transaction() as connection:
        insert_candidate(connection, meeting_id=meeting_id("edc"), title=TITLE)
    db.execute(
        "UPDATE meetings SET project_id=NULL WHERE id IN (?, ?)",
        (meeting_id("jd"), meeting_id("edc")),
    )
    assert post(client, headers, f"/api/requirement-candidates/{first}/restore").status_code == 200
