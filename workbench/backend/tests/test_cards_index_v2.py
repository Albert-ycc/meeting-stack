"""00 索引.md（第四期 4h）：先修四个毛病，再是 v2 的几节。

四个毛病：全部撤下后下一轮又写回来；暂停的项目索引还在更新；根目录最后一张卡片走了以后索引冻住；
合并或移走根目录后留下孤儿。
"""
import json

from meeting_workbench.cards import INDEX_PATHS_KEY, CardWriter
from meeting_workbench.project_names import merge_project

from .test_cards import CARDS, MEETING, _card_files, _env, _meeting, _mount, _project, _task
from .test_project_linking import make_project

INDEX = "00 索引.md"
WEEKLY = "vm-20260927-090000"


def _index(root):
    return root / CARDS / INDEX


def _retired_indexes(settings):
    folder = settings.data_dir / "card-retired"
    return sorted(
        p.parent.name.rsplit("-", 1)[-1] + "/" + p.name
        for p in folder.rglob(INDEX)
    ) if folder.is_dir() else []


def _known(db):
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (INDEX_PATHS_KEY,))
    return json.loads(row["value"]) if row else None


def _edit(path, old="讨论了", new="我改了"):
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _unmount(db, root):
    db.execute("DELETE FROM project_material_roots WHERE path=?", (str(root),))


# ---------------------------------------------------------------------- 四个毛病


def test_retire_all_takes_every_index_and_reconcile_writes_none(tmp_path):
    """毛病 1：全部撤下以后，留在原处的改过的卡片不再让索引下一轮又长出来。"""
    db, settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    _meeting(db, project_b, meeting_id=WEEKLY, title="周会", when="2026-09-27T09:00:00")
    writer.reconcile()
    assert _index(root_a).is_file() and _index(root_b).is_file()
    assert _known(db) == {str(_index(root_a)): project_a, str(_index(root_b)): project_b}
    _edit(root_a / CARDS / "260926 初审规则沟通.md")

    result = writer.retire_all()

    assert result["retired"] == 1  # 索引不计数
    assert _card_files(root_a) == ["260926 初审规则沟通.md"]
    assert not _index(root_a).exists() and not _index(root_b).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md", "index/00 索引.md"]
    assert _known(db) == {}
    _task(db, project_a, "in_progress", "关掉以后又加的任务")
    writer.reconcile()
    writer.reconcile()
    assert not _index(root_a).exists() and not _index(root_b).exists()

    writer.enable()
    writer.reconcile()
    assert "关掉以后又加的任务" in _index(root_a).read_text(encoding="utf-8")
    assert _index(root_b).is_file()


def test_indexes_written_before_the_upgrade_are_claimed(tmp_path):
    """升级前写的索引只记在内存里：第一次用到时认领，全部撤下照样收走。"""
    db, settings, _writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    CardWriter(db, settings).reconcile()
    db.execute("DELETE FROM app_state WHERE key=?", (INDEX_PATHS_KEY,))
    _edit(root / CARDS / "260926 初审规则沟通.md")

    CardWriter(db, settings).retire_all()

    assert not _index(root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    assert _known(db) == {}


def test_paused_project_index_is_retired_and_rewritten_on_resume(tmp_path):
    """毛病 2：暂停撤下索引（不计进 retired），暂停期间不更新，恢复时写回来。"""
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    _edit(root / CARDS / "260926 初审规则沟通.md")

    result = writer.pause_project(project_id)

    assert result["retired"] == 0  # 改过的卡片留在原处，索引不算数
    assert [item["meeting_id"] for item in result["kept"]] == [MEETING]
    assert not _index(root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    _task(db, project_id, "in_progress", "暂停时加的任务")
    writer.reconcile()
    writer.reconcile_project(project_id)
    assert not _index(root).exists()

    writer.resume_project(project_id)

    assert "暂停时加的任务" in _index(root).read_text(encoding="utf-8")
    assert _known(db) == {str(_index(root)): project_id}


def test_the_index_stays_live_after_the_last_card_leaves(tmp_path):
    """毛病 3：根目录最后一张卡片改走以后，后来加的任务照样写进它的索引。"""
    db, _settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    db.execute("UPDATE meetings SET project_id=?, project_origin='manual' WHERE id=?", (project_b, MEETING))
    writer.sync_meeting(MEETING)
    assert _card_files(root_a) == []

    _task(db, project_a, "in_progress", "后来加的任务", meeting_id=None)
    writer.reconcile()

    text = _index(root_a).read_text(encoding="utf-8")
    assert "- 后来加的任务 · 我 · 进行中" in text
    assert "这个项目还没有会议卡片。" in text
    assert "初审规则沟通" in _index(root_b).read_text(encoding="utf-8")


def test_a_shared_folder_gets_one_index_for_its_owner(tmp_path):
    """同一个文件夹挂在两个项目下：只有最早挂上的项目写索引；项目的第二个根目录不写。"""
    db, _settings, writer, disk = _env(tmp_path)
    first, root = _project(db, disk, "云图AI")
    second = make_project(db, "云图看板")
    _mount(db, second, root)
    second_root = disk / "看板"
    (second_root / CARDS).mkdir(parents=True)
    _mount(db, first, second_root)
    _meeting(db, first)
    _meeting(db, second, meeting_id=WEEKLY, title="看板评审", when="2026-09-27T09:00:00")

    writer.reconcile()

    assert _index(root).read_text(encoding="utf-8").startswith("---\nproject: 云图AI\n")
    assert not _index(second_root).exists()
    assert _known(db) == {str(_index(root)): first}


def test_moving_the_root_retires_the_old_index(tmp_path):
    """毛病 4：移走根目录以后，旧文件夹里的索引移进 card-retired/。"""
    db, settings, writer, disk = _env(tmp_path)
    project_id, old_root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    _edit(old_root / CARDS / "260926 初审规则沟通.md")  # 改过的卡片留在原处
    new_root = disk / "云图AI-新"
    new_root.mkdir()
    _unmount(db, old_root)
    _mount(db, project_id, new_root)

    writer.reconcile()

    assert not _index(old_root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    assert _known(db) == {str(_index(new_root)): project_id}


def test_merging_projects_retires_the_index_of_the_folder_left_behind(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    target, target_root = _project(db, disk, "数据中台")
    source, source_root = _project(db, disk, "云图AI")
    _meeting(db, source)
    _meeting(db, target, meeting_id=WEEKLY, title="周会", when="2026-09-27T09:00:00")
    writer.reconcile()
    assert _index(source_root).is_file()

    with db.transaction() as connection:
        merge_project(connection, source, target)
    writer.reconcile()

    assert not _index(source_root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    text = _index(target_root).read_text(encoding="utf-8")
    assert text.startswith("---\nproject: 数据中台\n") and 'project: ""' not in text
    assert _known(db) == {str(_index(target_root)): target}


def test_an_offline_root_keeps_its_index_until_it_comes_back(tmp_path, monkeypatch):
    db, settings, writer, disk = _env(tmp_path)
    project_id, old_root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    new_root = disk / "云图AI-新"
    new_root.mkdir()
    _unmount(db, old_root)
    _mount(db, project_id, new_root)
    original = writer._root_state
    monkeypatch.setattr(
        writer,
        "_root_state",
        lambda root, round_: "root_offline" if root == str(old_root) else original(root, round_),
    )

    writer.reconcile()

    assert _index(old_root).is_file()
    assert str(_index(old_root)) in _known(db)
    assert _retired_indexes(settings) == []

    monkeypatch.undo()
    writer.reconcile()

    assert not _index(old_root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    assert _known(db) == {str(_index(new_root)): project_id}


def test_hand_edits_are_overwritten_unless_the_marker_line_is_gone(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    index = _index(root)

    index.write_text(index.read_text(encoding="utf-8") + "\n我手改的一行\n", encoding="utf-8")
    _task(db, project_id, "in_progress", "第一条任务")
    writer.reconcile()
    assert "我手改的一行" not in index.read_text(encoding="utf-8")

    mine = index.read_text(encoding="utf-8").replace("generated_by: shengdang\n", "") + "我的索引\n"
    index.write_text(mine, encoding="utf-8")
    _task(db, project_id, "in_progress", "第二条任务")
    writer.reconcile()
    assert index.read_text(encoding="utf-8") == mine
    assert _known(db) == {}

    # 归你的索引：移走根目录、全部撤下都不碰
    other = disk / "别处"
    other.mkdir()
    _unmount(db, root)
    _mount(db, project_id, other)
    writer.reconcile()
    writer.retire_all()
    writer.reconcile()
    assert index.read_text(encoding="utf-8") == mine
    # 收走的只有新文件夹里声档写的那一份
    assert not _index(other).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
