"""会议卡片接口（第一期 1c-2）：详情和改归属带卡片去向、重写、补写、撤下、暂停与恢复。"""
from pathlib import Path

from meeting_workbench.cards import CardWriter
from meeting_workbench.db import Database, utc_now

from .test_cards import CARDS, MEETING, _card_files, _make_historical, _meeting, _mount
from .test_project_linking import make_project
from .test_tasks_api import make_client, write_headers


def _setup(tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    client, settings = make_client(tmp_path, material_browse_root=disk)
    headers = write_headers(client)
    db = Database(settings.database_path)
    return client, settings, headers, db, disk, CardWriter(db, settings)


def _project(db, disk, name):
    project_id = make_project(db, name)
    root = disk / name
    root.mkdir()
    _mount(db, project_id, root)
    return project_id, root


def test_reassigning_moves_the_card_and_undo_brings_it_back(tmp_path):
    client, _settings, headers, db, disk, writer = _setup(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    assert client.get(f"/api/meetings/{MEETING}").json()["card"]["category"] == "ok"

    detail = client.patch(
        f"/api/meetings/{MEETING}", json={"project_id": project_b}, headers=headers
    ).json()

    assert detail["effects"]["card"] == {
        "action": "moved",
        "from": f"云图AI/{CARDS}/260926 初审规则沟通.md",
        "to": f"数据中台/{CARDS}/260926 初审规则沟通.md",
        "reason": None,
    }
    assert detail["card"]["state"] == "synced"
    assert detail["card"]["path"] == str(root_b / CARDS / "260926 初审规则沟通.md")
    assert _card_files(root_a) == []

    undone = client.post(f"/api/meetings/{MEETING}/project/undo", json={}, headers=headers).json()

    assert undone["effects"]["card"]["action"] == "moved"
    assert _card_files(root_a) == ["260926 初审规则沟通.md"]
    assert _card_files(root_b) == []


def test_unassigned_meeting_says_what_the_card_is_waiting_for(tmp_path):
    client, _settings, headers, db, disk, writer = _setup(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()

    detail = client.patch(f"/api/meetings/{MEETING}", json={"project_id": ""}, headers=headers).json()

    assert detail["effects"]["card"]["action"] == "retired"
    assert detail["card"]["reason"] == "waiting_project"
    assert detail["card"]["category"] == "waiting"
    assert _card_files(root) == []


def test_regenerating_a_deleted_card(tmp_path):
    client, _settings, headers, db, disk, writer = _setup(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    (root / CARDS / "260926 初审规则沟通.md").unlink()
    db.execute("UPDATE meeting_cards SET dirty = dirty + 1")
    writer.reconcile()
    assert client.get(f"/api/meetings/{MEETING}").json()["card"]["state"] == "missing"

    response = client.post(
        f"/api/meetings/{MEETING}/card", json={"action": "regenerate"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "synced"
    assert _card_files(root) == ["260926 初审规则沟通.md"]
    assert client.post(
        "/api/meetings/nope/card", json={"action": "rewrite"}, headers=headers
    ).status_code == 404


def test_mounting_a_folder_writes_the_backlog_and_the_board_counts_cards(tmp_path):
    client, _settings, headers, db, disk, writer = _setup(tmp_path)
    project_id = make_project(db, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    board = client.get(f"/api/projects/{project_id}/board").json()
    assert board["cards"]["waiting"] == 1
    assert board["cards"]["waiting_reason"] == "no_root"
    root = disk / "云图AI"
    root.mkdir()

    mounted = client.post(
        f"/api/projects/{project_id}/material-roots", json={"path": str(root)}, headers=headers
    ).json()

    assert mounted["cards_written"] == 1
    board = client.get(f"/api/projects/{project_id}/board").json()
    assert board["cards"]["root"] == str(root / CARDS)
    assert board["cards"]["index_path"] == str(root / CARDS / "00 索引.md")
    assert (board["cards"]["written"], board["cards"]["waiting"]) == (1, 0)


def test_offline_disk_does_not_block_renaming_the_project(tmp_path):
    client, settings, headers, db, _disk, writer = _setup(tmp_path)
    settings.material_browse_root = Path("/Volumes")
    project_id = make_project(db, "云图AI")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, "/Volumes/声档测试盘-不存在/云图AI", utc_now()),
    )
    _meeting(db, project_id)
    writer.reconcile()

    response = client.patch(
        f"/api/projects/{project_id}", json={"name": "云图AI 二期", "color": "#5090ff"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert not Path("/Volumes/声档测试盘-不存在").exists()
    card = client.get(f"/api/meetings/{MEETING}").json()["card"]
    assert (card["state"], card["reason"]) == ("blocked", "root_offline")


def test_backfill_banner_answer_and_notices(tmp_path):
    client, _settings, headers, db, disk, writer = _setup(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    _make_historical(db)

    banner = client.get("/api/cards/banner").json()
    assert banner["backfill"]["meetings"] == 1
    assert banner["notices"] == []
    assert client.post("/api/cards/backfill", json={"answer": "maybe"}, headers=headers).status_code == 422

    assert client.post("/api/cards/backfill", json={"answer": "yes"}, headers=headers).status_code == 200
    writer.reconcile()

    banner = client.get("/api/cards/banner").json()
    assert banner["backfill"] is None
    assert [notice["path"] for notice in banner["notices"]] == [str(root / CARDS)]
    client.post("/api/cards/notices/dismiss", json={"project_id": project_id}, headers=headers)
    assert client.get("/api/cards/banner").json()["notices"] == []
    # 这台机器不是 Mac，打开文件夹要说清楚
    reveal = client.post("/api/cards/reveal", json={"project_id": project_id}, headers=headers)
    assert reveal.status_code == 409


def test_pause_resume_and_retire_all(tmp_path):
    client, _settings, headers, db, disk, writer = _setup(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()

    paused = client.post(f"/api/projects/{project_id}/cards/pause", json={}, headers=headers).json()
    assert paused["retired"] == 1
    assert client.get(f"/api/projects/{project_id}/board").json()["cards"]["paused"] is True

    resumed = client.post(f"/api/projects/{project_id}/cards/resume", json={}, headers=headers).json()
    assert resumed["written"] == 1
    assert resumed["cards"]["paused"] is False
    assert _card_files(root) == ["260926 初审规则沟通.md"]

    retired = client.post("/api/cards/retire-all", json={}, headers=headers).json()
    assert (retired["retired"], retired["kept"]) == (1, [])
    assert client.get(f"/api/meetings/{MEETING}").json()["card"]["reason"] == "disabled"
    client.post("/api/cards/enable", json={}, headers=headers)
    writer.reconcile()
    assert _card_files(root) == ["260926 初审规则沟通.md"]
