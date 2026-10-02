"""扫描清理失效记录的比例保护。

某个根这一轮要清掉的记录超过它现有记录的一半、且超过 20 条，这一轮不删，记进 report 的
mass_cleanup_skipped、打一条 warning（同一侧持续这样只打一次）。盘上真删了一半以上的归档时，
命令行 scan --allow-mass-cleanup 让这一次照删。原来只有「这个根一个文件都没发现」那一道兜底，
被正常清空的暂存根、被误删大半的归档都挡不住或清不掉。
"""

import json
import logging
import shutil
from datetime import UTC, datetime

import pytest

from meeting_workbench import cli
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter

from .test_importer import _write_plain_meeting


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        semantic_enabled=False,
    )


def _world(tmp_path, *, archive_meetings=0, staging_meetings=0):
    settings = _settings(tmp_path)
    settings.archive_root.mkdir(exist_ok=True)
    settings.staging_root.mkdir(exist_ok=True)
    archive = [
        _write_plain_meeting(
            settings.archive_root, f"归档{i:02d}", f"vm-20260101-{i:06d}", f"归档正文{i:02d}"
        )
        for i in range(archive_meetings)
    ]
    staging = [
        _write_plain_meeting(
            settings.staging_root, f"暂存{i:02d}", f"vm-20260201-{i:06d}", f"暂存正文{i:02d}"
        )
        for i in range(staging_meetings)
    ]
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    assert importer.scan().errors == 0
    return db, settings, importer, archive, staging


def _count(db, *source_roots):
    marks = ",".join("?" for _ in source_roots)
    return db.query_one(
        f"SELECT COUNT(*) AS n FROM artifacts WHERE source_root IN ({marks})", source_roots
    )["n"]


def _add_ghost_records(db, settings, count, *, source_root="archive"):
    """库里登记着、盘上已经没有的文件记录（挂在已有的一场会上）。"""
    meeting_id = db.query_one("SELECT id FROM meetings ORDER BY id LIMIT 1")["id"]
    root = settings.archive_root if source_root == "archive" else settings.staging_root
    for index in range(count):
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
               VALUES (?, 'srt', 'source', ?, ?, 1, 1, ?)""",
            (
                meeting_id,
                source_root,
                str(root / "已删除的会议" / f"ghost-{index:03d}.srt"),
                datetime.now(UTC).isoformat(),
            ),
        )


def test_a_round_that_would_delete_most_of_a_root_is_skipped_and_reported(tmp_path, caplog):
    db, _settings_, importer, archive, _ = _world(tmp_path, archive_meetings=15)
    total = _count(db, "archive")
    for directory in archive[:13]:
        shutil.rmtree(directory)

    with caplog.at_level(logging.WARNING, logger="meeting_workbench.importer"):
        reports = [importer.scan() for _ in range(2)]

    assert [report.mass_cleanup_skipped for report in reports] == [1, 1]
    assert [report.errors for report in reports] == [0, 0]
    assert _count(db, "archive") == total
    # 持续这样每 15 秒扫一轮，warning 只在开始跳过时记一次，并且告诉人怎么放行
    warnings = [r.getMessage() for r in caplog.records if "--allow-mass-cleanup" in r.getMessage()]
    assert len(warnings) == 1


def test_a_smaller_cleanup_goes_ahead(tmp_path):
    db, _settings_, importer, archive, _ = _world(tmp_path, archive_meetings=15)
    total = _count(db, "archive")
    for directory in archive[:5]:
        shutil.rmtree(directory)

    report = importer.scan()

    assert report.mass_cleanup_skipped == 0
    assert _count(db, "archive") == total - 5 * (total // 15)


@pytest.mark.parametrize(
    ("ghosts", "real_meetings", "skipped"),
    [
        (20, 1, False),  # 不超过 20 条：哪怕全是失效的也照清
        (21, 1, True),  # 超过 20 条、且远多于一半
        (21, 11, False),  # 失效的 21 条、现有 43 条左右，不到一半
        (22, 11, False),  # 恰好一半（现有 44 条，失效 22 条）：不算「超过一半」
        (23, 11, True),  # 刚过一半
    ],
)
def test_the_threshold_is_more_than_twenty_records_and_more_than_half(
    tmp_path, ghosts, real_meetings, skipped
):
    db, settings, importer, _archive, _ = _world(tmp_path, archive_meetings=real_meetings)
    real = _count(db, "archive")
    assert real == 2 * real_meetings
    _add_ghost_records(db, settings, ghosts)

    report = importer.scan()

    assert (report.mass_cleanup_skipped == 1) is skipped
    assert _count(db, "archive") == (real + ghosts if skipped else real)


def test_allow_mass_cleanup_deletes_them_this_one_time(tmp_path):
    db, _settings_, importer, archive, _ = _world(tmp_path, archive_meetings=15)
    total = _count(db, "archive")
    for directory in archive[:13]:
        shutil.rmtree(directory)
    assert importer.scan().mass_cleanup_skipped == 1

    report = importer.scan(allow_mass_cleanup=True)

    assert report.mass_cleanup_skipped == 0
    assert _count(db, "archive") == total - 13 * (total // 15)
    assert importer.scan().mass_cleanup_skipped == 0


def test_each_root_is_judged_on_its_own_numbers(tmp_path):
    db, _settings_, importer, archive, staging = _world(
        tmp_path, archive_meetings=15, staging_meetings=15
    )
    archive_total, staging_total = _count(db, "archive"), _count(db, "staging")
    for directory in staging[:13]:
        shutil.rmtree(directory)  # 暂存根被清掉大半：跳过
    for directory in archive[:2]:
        shutil.rmtree(directory)  # 归档根只少了两场：照清

    report = importer.scan()

    assert report.mass_cleanup_skipped == 1
    assert _count(db, "staging") == staging_total
    assert _count(db, "archive") == archive_total - 2 * (archive_total // 15)


def test_the_warning_comes_back_after_the_root_recovers_and_is_hit_again(tmp_path, caplog):
    db, settings, importer, _archive, _ = _world(tmp_path, archive_meetings=1)
    _add_ghost_records(db, settings, 30)

    def mass_warnings():
        return [r for r in caplog.records if "--allow-mass-cleanup" in r.getMessage()]

    with caplog.at_level(logging.WARNING, logger="meeting_workbench.importer"):
        importer.scan()
        importer.scan()
        assert len(mass_warnings()) == 1
        importer.scan(allow_mass_cleanup=True)  # 放行清掉之后恢复正常
        importer.scan()
        _add_ghost_records(db, settings, 30)
        importer.scan()
    assert len(mass_warnings()) == 2


def test_allow_mass_cleanup_also_clears_a_root_that_was_emptied(tmp_path):
    db, settings, importer, _archive, staging = _world(
        tmp_path, archive_meetings=2, staging_meetings=3
    )
    for directory in staging:
        shutil.rmtree(directory)  # 暂存根被 relay 正常清空：一个文件都发现不了
    assert importer.scan().stale_cleanup_skipped == 1
    assert _count(db, "staging") > 0

    report = importer.scan(allow_mass_cleanup=True)

    assert report.stale_cleanup_skipped == 0
    assert _count(db, "staging") == 0


def test_cli_scan_only_cleans_a_mass_of_records_with_the_explicit_flag(
    tmp_path, monkeypatch, capsys
):
    db, settings, _importer, archive, _ = _world(tmp_path, archive_meetings=15)
    total = _count(db, "archive")
    for directory in archive[:13]:
        shutil.rmtree(directory)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert cli.main(["scan"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["mass_cleanup_skipped"] == 1
    assert _count(db, "archive") == total

    assert cli.main(["scan", "--allow-mass-cleanup"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["mass_cleanup_skipped"] == 0
    assert _count(db, "archive") == total - 13 * (total // 15)
