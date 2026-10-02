"""备份保留份数：MEETING_WORKBENCH_BACKUP_RETENTION，默认 14，最小 1，本机和外置盘镜像共用这一个值。

以前写死在 BackupManager(retention=14)，本机在系统盘上每份几百 MB，想少留几份得改代码。
"""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from meeting_workbench import cli
from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database

START = datetime(2026, 1, 1, tzinfo=UTC)
ENV_NAME = "MEETING_WORKBENCH_BACKUP_RETENTION"
BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]


def test_default_is_fourteen_and_the_environment_variable_changes_it(monkeypatch):
    assert Settings(semantic_enabled=False).backup_retention == 14

    monkeypatch.setenv(ENV_NAME, "7")
    assert Settings(semantic_enabled=False).backup_retention == 7
    monkeypatch.setenv(ENV_NAME, " 1 ")
    assert Settings(semantic_enabled=False).backup_retention == 1


@pytest.mark.parametrize("value", ["0", "-3", "abc", "7.5", "", "  ", "十四"])
def test_invalid_values_fail_at_startup_with_a_plain_message(monkeypatch, value):
    monkeypatch.setenv(ENV_NAME, value)

    with pytest.raises(ValidationError) as error:
        Settings(semantic_enabled=False)

    message = str(error.value)
    assert ENV_NAME in message and "不小于 1 的整数" in message
    assert repr(value) in message  # 现在填的是什么，一眼能看到


@pytest.mark.parametrize("value", [0, -1, 7.5, True])
def test_invalid_values_are_rejected_when_passed_in_code_too(value):
    with pytest.raises(ValidationError, match="不小于 1 的整数"):
        Settings(semantic_enabled=False, backup_retention=value)


def test_command_line_startup_reports_the_bad_value_in_plain_words(tmp_path):
    environment = {
        **os.environ,
        ENV_NAME: "0",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(BACKEND),  # 测的是这个工作目录里的代码，不是装在虚拟环境里的那份
    }

    result = subprocess.run(
        [sys.executable, "-c", "from meeting_workbench.cli import main; main(['doctor'])"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert ENV_NAME in result.stderr and "不小于 1 的整数" in result.stderr
    assert not (tmp_path / ".meeting-workbench").exists()  # 配置不对就不往下走，没建任何东西


def test_in_process_startup_fails_before_touching_anything(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_NAME, "0")
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(ValidationError, match=ENV_NAME):
        cli.main(["scan"])

    assert not (tmp_path / "data").exists()


def _manager(tmp_path, **settings_overrides):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        **settings_overrides,
    )
    db = Database(settings.database_path)
    db.initialize()
    return settings, db


def test_backup_manager_keeps_that_many_copies_in_both_the_local_and_the_mirror_directory(tmp_path):
    settings, db = _manager(tmp_path, backup_retention=3)
    manager = BackupManager(db, settings)
    for day in range(5):
        result = manager.create(now=START + timedelta(days=day))

    assert manager.retention == 3
    assert len(list(settings.backup_dir.glob("workbench-*.sqlite3"))) == 3
    assert len(list(result.mirror_path.parent.glob("workbench-*.sqlite3"))) == 3


def test_default_manager_still_keeps_fourteen_and_an_explicit_argument_overrides(tmp_path):
    settings, db = _manager(tmp_path)

    assert BackupManager(db, settings).retention == 14
    assert BackupManager(db, settings, retention=2).retention == 2
    # 0 份会把刚生成的新副本也轮转掉，不能让它进到轮转里
    with pytest.raises(ValueError, match="不能小于 1"):
        BackupManager(db, settings, retention=0)


def test_env_example_documents_the_setting_in_one_commented_out_line(tmp_path):
    lines = [
        line
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if ENV_NAME in line
    ]

    assert len(lines) == 1 and lines[0].startswith("# ")
    # 取消注释就能用：行尾的说明不会被当成值的一部分，默认值和代码里的默认值一致
    uncommented = lines[0][2:]
    assert uncommented.startswith(f"{ENV_NAME}=")
    env_file = tmp_path / ".env"
    env_file.write_text(uncommented + "\n", encoding="utf-8")
    parsed = Settings(_env_file=env_file, semantic_enabled=False)
    assert parsed.backup_retention == Settings(semantic_enabled=False).backup_retention == 14
