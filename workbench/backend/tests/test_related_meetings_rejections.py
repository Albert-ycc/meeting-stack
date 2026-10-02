"""related_read.related_meetings：一场会一场会挡驳回改成一条语句取完，挡的规矩（按内容、按位置、原地改过也挡）不变。"""

import random
from datetime import date, timedelta

import pytest

from meeting_workbench import related, related_read
from meeting_workbench.db import Database
from meeting_workbench.relation_read import AUDIO_ID_SQL

from .helpers import count_reads

NOW = "2026-09-27T00:00:00+00:00"
KEY = "q2:" + "a" * 32
PLACE = "需求/接口文档.docx"
COPY_PLACE = "别处/副本.docx"


def old_related_meetings(connection, file_id, limit=related_read.RELATED_MEETINGS):
    """改前的写法，原样留在这里当对拍的标准：每一行会各查一次 rejected_filter。"""
    file = connection.execute(
        """SELECT f.content_key, f.root_id, f.rel_path, pr.project_id FROM material_files f
             JOIN project_material_roots pr ON pr.id = f.root_id WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if file is None or not file["content_key"]:
        return []
    project_id, key = str(file["project_id"]), str(file["content_key"])
    if key in related.hub_keys(connection, project_id, [key]):
        return []
    place = {"root_id": file["root_id"], "rel_path": file["rel_path"]}
    found = connection.execute(
        f"""SELECT r.id, r.meeting_id, r.at_ms, r.quote, r.evidence_json, m.title, m.recording_date, m.created_at,
                   {AUDIO_ID_SQL.format(meeting="m.id")} AS audio_id
              FROM relations r
              JOIN meetings m ON m.id = r.meeting_id AND m.project_id = r.project_id
             WHERE r.project_id = ? AND r.content_key = ? AND r.kind = 'related' AND r.status = 'shown'
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id""",
        (project_id, key),
    )
    rows = []
    for row in found:
        if related.is_blocked(
            key, place, related.rejected_filter(connection, str(row["meeting_id"]), project_id)
        ):
            continue
        rows.append(row)
        if len(rows) >= int(limit):
            break
    return [
        {
            "meeting_id": row["meeting_id"],
            "title": row["title"],
            "date": str(row["recording_date"] or row["created_at"] or "")[:10],
            "at_ms": row["at_ms"],
            "quote": row["quote"] or "",
            "words": [
                str(word) for word in related._json_obj(row["evidence_json"]).get("words") or []
            ],
            "audio_url": f"/api/media/{row['audio_id']}" if row["audio_id"] else None,
            "relation_id": int(row["id"]),
        }
        for row in rows
    ]


def build_world(tmp_path, meetings, rejections):
    """项目 p 里 meetings 场会，每场都有一条「相关」线连到同一份文件（内容 KEY）。
    rejections[i] 是第 i 场会（i 越小越新）要写的驳回行：(状态, kind, 内容, 根目录, 路径, 项目) 的列表。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "w.sqlite3")
    db.initialize()
    root_path = tmp_path / "资料"
    root_path.mkdir()
    with db.transaction() as c:
        c.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (NOW,))
        c.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '别的项目', ?)", (NOW,))
        c.execute(
            "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
            (str(root_path), NOW),
        )
        root = c.execute("SELECT id FROM project_material_roots").fetchone()[0]
        for rel_path in (PLACE, COPY_PLACE):
            name = rel_path.rsplit("/", 1)[-1]
            c.execute(
                """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns,
                                              zone, seen_at, content_key)
                   VALUES (?, ?, ?, ?, ?, ?, 'docx', 10, 1, 'normal', ?, ?)""",
                (root, rel_path, rel_path.rsplit("/", 1)[0], name, name[:-5], name[:-5], NOW, KEY),
            )
        files = {
            row["rel_path"]: row["id"]
            for row in c.execute("SELECT id, rel_path FROM material_files")
        }
        first_day = date(2026, 6, 1)
        for index in range(meetings):
            # index 越小越新；每个日子里再按时刻错开，排序没有并列
            day = first_day + timedelta(days=(meetings - index) // 3)
            moment = f"{day.isoformat()}T{10 + (meetings - index) % 3}:00:00"
            c.execute(
                """INSERT INTO meetings(id, title, recording_date, status, project_id, project_origin,
                                        created_at, updated_at)
                   VALUES (?, ?, ?, 'completed_unreviewed', 'p', 'ai', ?, ?)""",
                (f"m{index:04d}", f"会 {index}", moment, NOW, NOW),
            )
            c.execute(
                """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, at_ms, content_key,
                                         root_id, rel_path, file_id, evidence_json, created_at, updated_at)
                   VALUES ('related', 'p', ?, 'shown', 'vector', ?, 2000, ?, ?, ?, ?, '{"words": ["驻场服务"]}', ?, ?)""",
                (f"m{index:04d}|{KEY}", f"m{index:04d}", KEY, root, PLACE, files[PLACE], NOW, NOW),
            )
        serial = 0
        for index, rows in rejections.items():
            for status, kind, content_key, root_id, rel_path, project_id in rows:
                serial += 1
                c.execute(
                    """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, content_key,
                                             root_id, rel_path, evidence_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'manual', ?, ?, ?, ?, '{}', ?, ?)""",
                    (
                        kind,
                        project_id,
                        f"m{index:04d}|rej{serial}",
                        status,
                        f"m{index:04d}",
                        content_key,
                        root if root_id == "root" else root_id,
                        rel_path,
                        NOW,
                        NOW,
                    ),
                )
    return db, files


# 一场会上可能留下的驳回行。前四种能挡住 PLACE 上的 KEY，后面的都是不该挡的干扰项。
BY_PLACE = [("rejected", "related", "q2:" + "0" * 32, "root", PLACE, "p")]  # 内容换过了，位置还在
BY_CONTENT = [("rejected", "related", KEY, "root", COPY_PLACE, "p")]  # 同内容的别处副本上驳回的
BY_CONTENT_NO_PLACE = [("rejected", "related", KEY, None, None, "p")]  # 没记位置，只记了内容
BY_PLACE_NO_CONTENT = [("rejected", "related", None, "root", PLACE, "p")]  # 没记内容，只记了位置
DECOYS = [
    [
        ("rejected", "related", "q2:" + "9" * 32, "root", "别的/不相干.docx", "p")
    ],  # 别的内容别的位置
    [("suggested", "related", KEY, "root", PLACE, "p")],  # 没驳回
    [("cleared", "related", KEY, "root", PLACE, "p")],  # 收回过的系统行
    [("rejected", "mention", KEY, "root", PLACE, "p")],  # 不是「相关」
    [("rejected", "related", KEY, "root", PLACE, "q")],  # 别的项目里的驳回
    [("rejected", "related", "q2:" + "8" * 32, "root", "", "p")],  # 路径是空串，对不上位置
    [("rejected", "related", "", None, None, "p")],  # 内容和位置都没记
]


def random_rejections(seed, meetings):
    rng = random.Random(seed)
    shapes = [BY_PLACE, BY_CONTENT, BY_CONTENT_NO_PLACE, BY_PLACE_NO_CONTENT, *DECOYS]
    rejections = {}
    for index in range(meetings):
        if rng.random() < 0.55:
            rows = []
            for _ in range(rng.randint(1, 3)):
                rows += rng.choice(shapes)
            rejections[index] = rows
    return rejections


def read_both(db, file_id, limit):
    with db.autocommit() as connection:
        return (
            related_read.related_meetings(connection, file_id, limit),
            old_related_meetings(connection, file_id, limit),
        )


@pytest.mark.parametrize("seed", range(6))
def test_random_rejections_give_the_same_meetings_as_the_per_meeting_filter(tmp_path, seed):
    meetings = 70
    db, files = build_world(tmp_path, meetings, random_rejections(seed, meetings))
    for file_id in files.values():
        for limit in (1, 3, 5, 12, 200):
            new, old = read_both(db, file_id, limit)
            assert new == old
    # 这个世界里确实有被挡的、也确实有放行的，不是两边都空
    with db.autocommit() as connection:
        shown = related_read.related_meetings(connection, files[PLACE], 200)
    assert 0 < len(shown) < meetings


def test_every_blocking_shape_blocks_and_every_decoy_lets_through(tmp_path):
    shapes = {
        "按位置": (BY_PLACE, True),
        "按内容": (BY_CONTENT, True),
        "只记了内容": (BY_CONTENT_NO_PLACE, True),
        "只记了位置": (BY_PLACE_NO_CONTENT, True),
        **{f"干扰项{index}": (rows, False) for index, rows in enumerate(DECOYS)},
    }
    rejections = {index: rows for index, (rows, _blocks) in enumerate(shapes.values())}
    db, files = build_world(tmp_path, len(shapes) + 3, rejections)

    with db.autocommit() as connection:
        shown = {
            row["meeting_id"]
            for row in related_read.related_meetings(connection, files[PLACE], 100)
        }

    for index, (label, (_rows, blocks)) in enumerate(shapes.items()):
        assert (f"m{index:04d}" not in shown) is blocks, label
    # 没写过驳回的三场会都在
    assert {f"m{index:04d}" for index in range(len(shapes), len(shapes) + 3)} <= shown


def test_blocked_meetings_match_is_blocked_meeting_by_meeting(tmp_path):
    meetings = 60
    db, files = build_world(tmp_path, meetings, random_rejections(99, meetings))
    with db.autocommit() as connection:
        for rel_path, file_id in files.items():
            row = connection.execute(
                "SELECT content_key, root_id, rel_path FROM material_files WHERE id = ?", (file_id,)
            ).fetchone()
            place = {"root_id": row["root_id"], "rel_path": row["rel_path"]}
            expected = {
                f"m{index:04d}"
                for index in range(meetings)
                if related.is_blocked(
                    KEY, place, related.rejected_filter(connection, f"m{index:04d}", "p")
                )
            }
            assert expected, rel_path
            assert related.blocked_meetings(connection, "p", KEY, place) == expected


def test_statement_count_does_not_grow_with_the_rows_scanned(tmp_path):
    """最新的 150 场会都驳回过这份内容：改前要扫过 150 多行才凑得出 5 条，每行一条 rejected_filter。"""
    quiet_db, quiet_files = build_world(tmp_path / "quiet", 160, {})
    blocked_db, blocked_files = build_world(
        tmp_path / "blocked", 160, {index: BY_PLACE for index in range(150)}
    )

    quiet = count_reads(quiet_db, lambda c: related_read.related_meetings(c, quiet_files[PLACE]))
    blocked = count_reads(
        blocked_db, lambda c: related_read.related_meetings(c, blocked_files[PLACE])
    )

    assert quiet == blocked
    assert blocked <= 6
