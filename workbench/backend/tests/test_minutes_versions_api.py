"""会议详情只带当前纪要版本的正文，历史版本只给元数据。
原来详情每次都带回全部历史版本的 markdown 和 html：20 个版本、每版 30KB 的会，一次详情 1.2MB，
保存后的静默刷新、轮询 Qwen 状态都要再拉一遍，而页面上从来没有用到过历史版本的正文
（版本下拉只用来选回滚的目标）。"""

import hashlib

from meeting_workbench.db import Database, utc_now

from .test_tasks_api import make_client as make_app_client
from .test_tasks_api import write_headers

BODY_FIELDS = {"markdown", "html"}


def make_client(tmp_path):
    client, settings = make_app_client(tmp_path)
    return client, Database(settings.database_path)


def add_meeting(db: Database, meeting_id: str, *, versions: int, size: int = 200) -> list[str]:
    """造一场有 versions 个纪要版本的会：最新的一版是当前版本。返回按 version_no 升序的版本号 id。"""
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, ?, 'completed_unreviewed')",
        (meeting_id, f"会 {meeting_id}"),
    )
    ids = []
    for number in range(1, versions + 1):
        version_id = f"mv-{meeting_id}-{number}"
        markdown = f"# {meeting_id} 第 {number} 版\n\n" + f"第{number}版正文。" * size
        db.execute(
            """INSERT INTO minutes_versions
               (id, meeting_id, version_no, based_on_id, markdown, html, kind, published,
                content_sha256, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
            (
                version_id,
                meeting_id,
                number,
                ids[-1] if ids else None,
                markdown,
                f"<h1>{meeting_id} 第 {number} 版</h1>",
                int(number == 1),
                hashlib.sha256(markdown.encode()).hexdigest(),
                utc_now(),
            ),
        )
        ids.append(version_id)
    db.execute("UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (ids[-1], meeting_id))
    return ids


def detail(client, meeting_id: str) -> dict:
    response = client.get(f"/api/meetings/{meeting_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_only_the_current_version_carries_its_body(tmp_path):
    client, db = make_client(tmp_path)
    ids = add_meeting(db, "vm-a", versions=4)
    versions = {item["id"]: item for item in detail(client, "vm-a")["minutes_versions"]}

    assert list(versions) == [ids[3], ids[2], ids[1], ids[0]]  # 仍按 version_no 倒序
    current = versions[ids[3]]
    assert current["markdown"].startswith("# vm-a 第 4 版")
    assert current["html"] == "<h1>vm-a 第 4 版</h1>"
    for history_id in ids[:3]:
        assert BODY_FIELDS.isdisjoint(versions[history_id]), "历史版本不该带 markdown、html"


def test_history_versions_keep_all_other_columns(tmp_path):
    """元数据照旧：以后表里新增了列，历史版本也要带着，只有两个正文列被拿掉。"""
    client, db = make_client(tmp_path)
    ids = add_meeting(db, "vm-a", versions=3)
    columns = {row["name"] for row in db.query_all("PRAGMA table_info(minutes_versions)")}

    versions = {item["id"]: item for item in detail(client, "vm-a")["minutes_versions"]}

    assert set(versions[ids[0]]) == columns - BODY_FIELDS
    assert set(versions[ids[2]]) == columns
    first = versions[ids[0]]
    assert (first["version_no"], first["kind"], first["published"]) == (1, "draft", 1)
    assert first["meeting_id"] == "vm-a" and first["created_at"]
    assert versions[ids[1]]["based_on_id"] == ids[0]
    assert (
        first["content_sha256"]
        == hashlib.sha256(("# vm-a 第 1 版\n\n" + "第1版正文。" * 200).encode()).hexdigest()
    )


def test_the_body_goes_with_the_current_pointer_not_with_the_newest_version(tmp_path):
    """丢弃草稿、采用外部版本之后，当前版本可能是较早的一版：正文跟着 current_minutes_version_id 走。"""
    client, db = make_client(tmp_path)
    ids = add_meeting(db, "vm-a", versions=3)
    db.execute("UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (ids[0], "vm-a"))

    versions = {item["id"]: item for item in detail(client, "vm-a")["minutes_versions"]}

    assert BODY_FIELDS <= set(versions[ids[0]])
    assert BODY_FIELDS.isdisjoint(versions[ids[2]])
    assert BODY_FIELDS.isdisjoint(versions[ids[1]])


def test_without_a_current_pointer_the_newest_version_carries_the_body(tmp_path):
    """没有当前版本指针（或指针指到不在这场会里的版本）时，页面拿最新的一版当当前版本显示，正文也给它。"""
    client, db = make_client(tmp_path)
    ids = add_meeting(db, "vm-a", versions=3)
    for pointer in (None, "mv-not-here"):
        db.execute("UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (pointer, "vm-a"))
        versions = {item["id"]: item for item in detail(client, "vm-a")["minutes_versions"]}
        assert BODY_FIELDS <= set(versions[ids[2]]), pointer
        assert BODY_FIELDS.isdisjoint(versions[ids[1]]), pointer
        assert BODY_FIELDS.isdisjoint(versions[ids[0]]), pointer


def test_a_meeting_without_minutes_has_an_empty_list(tmp_path):
    client, db = make_client(tmp_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-none', '没纪要', 'completed_unreviewed')"
    )

    assert detail(client, "vm-none")["minutes_versions"] == []


def test_detail_size_does_not_grow_with_the_history(tmp_path):
    """20 个版本、每版约 30KB：详情只比 1 个版本的会多 19 份元数据，不再多 19 份正文。"""
    client, db = make_client(tmp_path)
    add_meeting(db, "vm-one", versions=1, size=2500)
    add_meeting(db, "vm-many", versions=20, size=2500)
    one = client.get("/api/meetings/vm-one")
    many = client.get("/api/meetings/vm-many")

    body_bytes = len(("第20版正文。" * 2500).encode())
    assert body_bytes > 30_000  # 造的数据确实是每版 30KB 的量级
    assert len(many.content) < len(one.content) + 19 * 1_500  # 每多一版只多一份元数据
    assert len(many.content) < 2 * body_bytes
    assert "第1版正文" not in many.text and "第19版正文" not in many.text


def test_rolling_back_still_shows_the_rolled_back_text_as_current(tmp_path):
    """回滚会建一个新版本、设成当前：详情里它带正文，被回滚的老版本和其余历史版本只剩元数据。"""
    client, db = make_client(tmp_path)
    ids = add_meeting(db, "vm-a", versions=3, size=20)

    rolled = client.post(
        "/api/meetings/vm-a/rollback/minutes",
        json={"version_id": ids[0]},
        headers=write_headers(client),
    )
    assert rolled.status_code == 200, rolled.text

    after = detail(client, "vm-a")
    versions = after["minutes_versions"]
    assert [item["version_no"] for item in versions] == [4, 3, 2, 1]
    assert versions[0]["id"] == after["current_minutes_version_id"]
    assert versions[0]["markdown"] == "# vm-a 第 1 版\n\n" + "第1版正文。" * 20
    assert all(BODY_FIELDS.isdisjoint(item) for item in versions[1:])
    assert versions[0]["based_on_id"] == ids[0]
