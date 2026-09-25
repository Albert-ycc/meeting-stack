"""纪要 md/html 配对：manifest 登记过的纪要不再按文件名关键词排除。

relay 按会议标题给纪要命名。2026-09-14「明德劳务协议签署与证据链对接」因标题里有「协议」，
纪要被当成合同类附件跳过，整个受管目录被判「纪要产物缺失」隔离，界面只剩「未命名录音」。
标题带「转写」「原文」时 artifact_kind 还会把纪要 md 归成逐字稿，同样被跳过。
"""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import (
    ArchiveImporter,
    manifest_registered_paths,
    topic_minutes_pair,
)


def _write(directory, names):
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = directory / name
        path.write_text(f"# {path.stem}", encoding="utf-8")
        paths.append(path)
    return paths


def _manifest(directory, registered):
    path = directory / "workbench-manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-x",
                "attempt": 1,
                "artifacts": [{"path": name, "bytes": 1, "sha256": "0" * 64} for name in registered],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_registered_minutes_pair_with_marker_in_title_is_accepted(tmp_path):
    names = ["明德劳务协议签署与证据链对接.md", "明德劳务协议签署与证据链对接.html"]
    files = _write(tmp_path / "meeting", names)
    files.append(_manifest(tmp_path / "meeting", names))

    pair = topic_minutes_pair(files)

    assert pair is not None
    assert [path.name for path in pair] == names


def test_unregistered_contract_document_is_still_excluded(tmp_path):
    files = _write(tmp_path / "legacy", ["服务合同.md", "服务合同.html"])

    assert topic_minutes_pair(files) is None


def test_registered_pair_wins_over_unregistered_user_notes(tmp_path):
    directory = tmp_path / "meeting"
    registered = ["转写方案评审.md", "转写方案评审.html"]
    files = _write(directory, registered + ["一些笔记.md", "一些笔记.html"])
    files.append(_manifest(directory, registered))

    pair = topic_minutes_pair(files)

    assert pair is not None
    assert pair[0].name == "转写方案评审.md"


def test_unregistered_marker_document_in_managed_directory_is_ignored(tmp_path):
    directory = tmp_path / "meeting"
    registered = ["需求沟通.md", "需求沟通.html"]
    files = _write(directory, registered + ["合同.md", "合同.html"])
    files.append(_manifest(directory, registered))

    pair = topic_minutes_pair(files)

    assert pair is not None
    assert pair[0].name == "需求沟通.md"


def test_manifest_paths_outside_directory_are_not_trusted(tmp_path):
    directory = tmp_path / "meeting"
    files = _write(directory, ["合同.md", "合同.html"])
    files.append(_manifest(directory, ["../合同.md", "/etc/合同.html"]))

    assert manifest_registered_paths(files) == set()
    assert topic_minutes_pair(files) is None


def test_malformed_manifest_falls_back_to_marker_rules(tmp_path):
    directory = tmp_path / "meeting"
    files = _write(directory, ["劳务协议.md", "劳务协议.html"])
    broken = directory / "workbench-manifest.json"
    broken.write_text("{not json", encoding="utf-8")
    files.append(broken)

    assert topic_minutes_pair(files) is None


def _managed_directory(directory, meeting_id, job_id, minutes_stem, text):
    directory.mkdir(parents=True)
    files = {
        f"{meeting_id}.m4a": b"audio",
        f"{meeting_id}.srt": f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n".encode(),
        f"{meeting_id}.txt": text.encode(),
        f"{meeting_id}.spk.txt": "SPEAKER_00\t张三".encode(),
        f"{meeting_id}.funasr.json": b"{}",
        "funasr.log": b"ok",
        f"{minutes_stem}.md": f"# {minutes_stem}\n\n## 一分钟摘要\n\n{text}\n".encode(),
        f"{minutes_stem}.html": f"<h1>{minutes_stem}</h1>".encode(),
    }
    for relative, content in files.items():
        (directory / relative).write_bytes(content)
    (directory / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "attempt": 1,
                "minutes_protocol_version": 3,
                "artifacts": [
                    {
                        "path": relative,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    for relative, content in files.items()
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


@pytest.mark.parametrize(
    "minutes_stem",
    ["明德劳务协议签署与证据链对接", "声档转写方案评审", "合同原文核对会"],
)
def test_managed_meeting_with_marker_title_is_imported_not_quarantined(tmp_path, minutes_stem):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260914-003009-1dffbbce"
    directory = _managed_directory(
        archive / f"260914 {minutes_stem}", meeting_id, "job-marker", minutes_stem, "会上正文"
    )
    relay_db = tmp_path / "relay.sqlite3"
    with closing(sqlite3.connect(relay_db)) as connection, connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-marker', 'completed_unreviewed', 1, ?)",
            (str(directory),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
        semantic_enabled=False,
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert not report.quarantine_details
    meeting = db.query_one(
        "SELECT title, canonical_dir, current_minutes_version_id FROM meetings WHERE id=?",
        (meeting_id,),
    )
    assert meeting is not None
    assert meeting["canonical_dir"] == str(directory)
    assert meeting["current_minutes_version_id"] is not None
    assert minutes_stem in meeting["title"]
