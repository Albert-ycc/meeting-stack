"""项目座次（需求池改版 260930，R03）：项目之间的先后，代表这一阶段的个人工作方向。

座次只影响需求池的排序与展示，不参与任何归属判断。库里的 seat 只当排序键（座次靠前的项目被合并、
删除后会留空位），对外一律给从 1 开始连续的名次。新建的项目默认未排座次（NULL）。
"""

from __future__ import annotations

from typing import Any

from .db import Database
from .service import NotFoundError

# 录音时间混着 -07:00 和 +00:00 两种时区写法，按字符串比会差出几个小时；比较先后一律换成儒略日。
MEETING_TIME_SQL = "COALESCE(m.recording_date, m.created_at)"


def seat_ranks(connection: Any) -> dict[str, int]:
    """{项目 id: 名次}，只含排了座次的项目，名次从 1 开始连续。"""
    rows = connection.execute(
        "SELECT id FROM projects WHERE seat IS NOT NULL ORDER BY seat, id"
    ).fetchall()
    return {row["id"]: index for index, row in enumerate(rows, start=1)}


def project_latest_meetings(connection: Any) -> dict[str, dict[str, Any]]:
    """每个项目归属的会议里录音时间最晚的一场：{项目 id: {"date": 原样的录音时间, "jd": 儒略日}}。"""
    rows = connection.execute(
        f"""SELECT m.project_id, MAX(julianday({MEETING_TIME_SQL})) AS jd,
                   {MEETING_TIME_SQL} AS latest
              FROM meetings m
             WHERE m.project_id IS NOT NULL
             GROUP BY m.project_id"""
    ).fetchall()
    # SQLite 的 MAX 聚合里，裸列 latest 取自 jd 最大的那一行。
    return {
        row["project_id"]: {"date": row["latest"], "jd": row["jd"]}
        for row in rows
        if row["jd"] is not None
    }


def set_project_seats(db: Database, project_ids: list[str]) -> dict[str, Any]:
    """「我的方向」拖动后整排保存：project_ids 是排了座次的项目从第 1 位起的先后，
    不在里面的项目回到未排座次。顺序没变就不写库。"""
    if len(set(project_ids)) != len(project_ids):
        raise ValueError("座次里有重复的项目")
    with db.transaction() as connection:
        for project_id in project_ids:
            if (
                connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"项目不存在：{project_id}")
        if list(seat_ranks(connection)) != project_ids:
            # 先整排清空再按新顺序写：座次上有唯一索引，逐个改会撞上还没挪走的旧座次。
            connection.execute("UPDATE projects SET seat=NULL WHERE seat IS NOT NULL")
            for seat, project_id in enumerate(project_ids, start=1):
                connection.execute("UPDATE projects SET seat=? WHERE id=?", (seat, project_id))
    return {"seats": [{"project_id": pid, "seat": seat} for seat, pid in enumerate(project_ids, 1)]}
