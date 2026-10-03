"""只读连接的 URI 不能靠字符串拼路径。

`file:{path}?mode=ro` 里路径的 ?、# 会被 SQLite 当成 URI 的语法，% 会被当成转义，
结果打开别的文件、或者报「打不开」，更糟的是把一份坏备份「校验」成完好（打开的是别的文件）。
这里每个只读入口都放进带 ?、#、%、空格、中文的目录下，要求打开的是它自己。
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_workbench import cli
from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import SCHEMA_VERSION, Database, read_only_uri
from meeting_workbench.importer import ArchiveImporter

from .test_importer import write_managed_unreviewed_bundle

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_speakers  # 只能在 sys.path 调整之后导入

# 第二项是「按字符串拼 URI 时会被打开的那个路径」：? 处被截断、%41 被解码成 A
WEIRD_DIRS = {
    "问号井号空格中文": ("库 a?b#c 空格", "库 a"),
    "百分号转义": ("p%41q", "pAq"),
}


@pytest.fixture(params=sorted(WEIRD_DIRS))
def weird(request, tmp_path):
    """(带特殊字符的目录, 拼错 URI 时会误打开的路径)。"""
    name, decoy = WEIRD_DIRS[request.param]
    directory = tmp_path / name
    directory.mkdir()
    return directory, tmp_path / decoy


def _settings(directory: Path, **extra) -> Settings:
    return Settings(
        data_dir=directory / "data",
        database_path=directory / "workbench.sqlite3",
        archive_root=directory / "archive",
        staging_root=directory / "staging",
        semantic_enabled=False,
        **extra,
    )


def test_read_only_uri_opens_the_given_file_and_refuses_writes(weird):
    directory, _decoy = weird
    path = directory / "x.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t(v)")
        connection.execute("INSERT INTO t VALUES ('right-file')")
    connection = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        assert connection.execute("SELECT v FROM t").fetchone() == ("right-file",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO t VALUES ('x')")
    finally:
        connection.close()


def test_user_version_reads_the_database_it_belongs_to(weird):
    directory, decoy = weird
    # 误打开的路径上放一份别的版本号的库：以前读到的是它，或者干脆打不开
    decoy.parent.mkdir(exist_ok=True)
    with sqlite3.connect(decoy) as connection:
        connection.execute("PRAGMA user_version=7")
    db = Database(directory / "workbench.sqlite3")

    db.initialize()
    db.initialize()

    assert db.user_version() == SCHEMA_VERSION


def test_backup_verification_checks_the_file_it_was_given(weird):
    directory, decoy = weird
    decoy.parent.mkdir(exist_ok=True)
    with sqlite3.connect(decoy) as connection:
        connection.execute("CREATE TABLE healthy(v)")
    broken = directory / "workbench-broken.sqlite3"
    broken.write_bytes(b"this is not a database" * 400)
    good = directory / "workbench-good.sqlite3"
    with sqlite3.connect(good) as connection:
        connection.execute("CREATE TABLE healthy(v)")

    BackupManager._verify_database(good)
    # 误打开别的文件时这份坏备份会被判成完好
    with pytest.raises(sqlite3.DatabaseError):
        BackupManager._verify_database(broken)


def test_importer_reads_the_relay_jobs_database_it_was_configured_with(weird):
    directory, decoy = weird
    archive = directory / "archive"
    attempt = write_managed_unreviewed_bundle(
        archive / "待校对" / "260712 第一场",
        "vm-20260712-181605-0b445e55",
        "job-first",
        text="第一场正文",
    )
    relay_db = directory / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER,"
            " archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-first', 'completed_unreviewed', 1, ?)",
            (str(attempt),),
        )
    settings = _settings(directory, relay_jobs_db=relay_db)
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    meeting = db.query_one("SELECT source_job_id, source_priority FROM meetings")
    assert report.errors == 0
    assert meeting == {"source_job_id": "job-first", "source_priority": 51}
    # ? 之后的 mode=ro 会落进被忽略的片段，「只读」连接其实可写，还会在这里新建一个空库
    assert not decoy.exists()


def test_cli_read_only_entries_open_the_workbench_database(weird, capsys):
    directory, decoy = weird
    settings = _settings(directory)
    db = Database(settings.database_path)
    db.initialize()
    # 误打开的路径上放一份挂了材料文件夹的库：以前这几个命令会把它当成真库
    decoy.parent.mkdir(exist_ok=True)
    decoy_db = Database(decoy)
    decoy_db.initialize()
    decoy_db.execute(
        "INSERT INTO projects(id, name, color, created_at) VALUES ('p', '别处的项目', '#123456', 'x')"
    )
    decoy_db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/decoy', 'x')"
    )

    connection = cli._read_only(settings)
    try:
        assert connection.execute("SELECT COUNT(*) FROM project_material_roots").fetchone()[0] == 0
    finally:
        connection.close()
    with pytest.raises(SystemExit, match="还没有挂材料文件夹"):
        cli._material_roots(settings, None, None)
    assert cli._materials_status(SimpleNamespace(project=None, json=True), settings) == 0
    assert '"roots": []' in capsys.readouterr().out


def test_backfill_script_reads_the_relay_jobs_database_it_was_configured_with(weird):
    directory, decoy = weird
    relay_db = directory / "relay.sqlite3"
    for path, status in ((relay_db, "transcribing"), (decoy, "completed_unreviewed")):
        path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT)")
            connection.execute("INSERT INTO jobs VALUES ('job-1', ?)", (status,))
    settings = _settings(directory, relay_jobs_db=relay_db)

    assert backfill_speakers._relay_in_flight_jobs(settings) == ["job-1:transcribing"]
