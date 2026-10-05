"""load_json_file 的嵌套深度校验按文件缓存：同一份没变的文件只扫一遍。

_validate_json_depth 是纯 Python 逐字符扫描，实测一份 20MB 的转写 JSON 要 0.3～0.5 秒；
导入同一份文件时会被加载好几次（登记检查、取会议 id、拆目录、核对清单……），
每次都重扫一遍。缓存只记「已经校验通过」，键是（路径）加（大小、mtime、ctime、inode），文件变了就重扫；\n刚写完不到 MEMO_SETTLE_NS 的文件不记。
"""

import json
import os
from pathlib import Path

import pytest

from meeting_workbench import importer, parsers
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter

from .test_importer import write_meeting


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    # 用例里的文件都是刚写的：不把「刚写完不记」关掉，下面这些用例就什么都记不住。
    # 这条规则有专门的用例（test_a_file_written_a_moment_ago_is_not_remembered）。
    monkeypatch.setattr(parsers, "MEMO_SETTLE_NS", 0)
    parsers._depth_checked.clear()
    yield
    parsers._depth_checked.clear()


@pytest.fixture
def scans(monkeypatch):
    """记下深度扫描真正跑了几次。"""
    calls = []
    real = parsers._validate_json_depth

    def counting(content):
        calls.append(len(content))
        return real(content)

    monkeypatch.setattr(parsers, "_validate_json_depth", counting)
    return calls


def _write(path: Path, payload, mtime_ns: int | None = None) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _nested(levels: int):
    value: object = 1
    for _ in range(levels):
        value = [value]
    return value


def test_unchanged_file_is_depth_checked_once_however_often_it_is_loaded(tmp_path, scans):
    path = _write(tmp_path / "a.json", {"sentence_info": [{"text": "你好"}] * 50})

    results = [parsers.load_json_file(path) for _ in range(5)]

    assert all(result == results[0] for result in results)
    assert results[0]["sentence_info"][0] == {"text": "你好"}
    assert len(scans) == 1


def test_a_rewritten_file_is_checked_again(tmp_path, scans):
    path = _write(tmp_path / "a.json", {"v": 1}, mtime_ns=1_700_000_000_000_000_000)
    parsers.load_json_file(path)

    # 大小一样、内容不同，只有修改时间变了：也要重扫
    _write(path, {"v": 2}, mtime_ns=1_700_000_001_000_000_000)
    assert parsers.load_json_file(path) == {"v": 2}

    # 大小变了、修改时间没变：同样要重扫
    _write(path, {"v": 22}, mtime_ns=1_700_000_001_000_000_000)
    assert parsers.load_json_file(path) == {"v": 22}

    assert len(scans) == 3


def test_a_deep_file_swapped_in_after_a_good_load_is_still_rejected(tmp_path, scans):
    path = _write(tmp_path / "a.json", _nested(3), mtime_ns=1_700_000_000_000_000_000)
    parsers.load_json_file(path)

    _write(path, _nested(parsers.MAX_JSON_DEPTH + 1), mtime_ns=1_700_000_001_000_000_000)

    with pytest.raises(ValueError, match="nesting"):
        parsers.load_json_file(path)


def test_a_file_that_failed_the_check_is_not_remembered(tmp_path, scans):
    path = _write(tmp_path / "deep.json", _nested(parsers.MAX_JSON_DEPTH + 1))

    for _ in range(2):
        with pytest.raises(ValueError, match="nesting"):
            parsers.load_json_file(path)

    assert len(scans) == 2


def test_the_remembered_files_are_bounded_and_the_oldest_is_forgotten(tmp_path, scans, monkeypatch):
    monkeypatch.setattr(parsers, "_DEPTH_CHECKED_LIMIT", 3)
    paths = [_write(tmp_path / f"{index}.json", {"i": index}) for index in range(5)]
    for path in paths:
        parsers.load_json_file(path)

    assert len(parsers._depth_checked) == 3
    assert len(scans) == 5
    parsers.load_json_file(paths[-1])  # 最近的还记着
    assert len(scans) == 5
    parsers.load_json_file(paths[0])  # 最早的已经挤掉，要重扫
    assert len(scans) == 6


def test_a_file_written_a_moment_ago_is_not_remembered(tmp_path, scans, monkeypatch):
    monkeypatch.setattr(parsers, "MEMO_SETTLE_NS", 3_000_000_000)
    path = _write(tmp_path / "fresh.json", {"v": 1})

    for _ in range(3):
        parsers.load_json_file(path)

    # 时间戳精度粗的盘上，同一个刻度里再写一次改不动 mtime：刚写完的文件每次都重扫
    assert len(scans) == 3
    assert len(parsers._depth_checked) == 0


def test_the_same_size_and_mtime_but_another_inode_is_checked_again(tmp_path, scans):
    path = _write(tmp_path / "a.json", {"v": 1}, mtime_ns=1_700_000_000_000_000_000)
    parsers.load_json_file(path)

    # 整份替换：大小、内容长度、mtime 都和原来一样，inode 不同
    replacement = _write(
        tmp_path / "replacement.json", {"v": 2}, mtime_ns=1_700_000_000_000_000_000
    )
    os.replace(replacement, path)
    assert parsers.load_json_file(path) == {"v": 2}

    assert len(scans) == 2


def test_an_in_place_rewrite_with_the_mtime_put_back_is_checked_again(tmp_path, scans):
    path = _write(tmp_path / "a.json", _nested(3), mtime_ns=1_700_000_000_000_000_000)
    parsers.load_json_file(path)

    # 原地写进同样长度的内容，再把 mtime 拨回去：改动会刷新 ctime
    path.write_text(json.dumps([[[2]]]), encoding="utf-8")
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    assert len(path.read_bytes()) == len(json.dumps(_nested(3)))
    assert parsers.load_json_file(path) == [[[2]]]

    assert len(scans) == 2


def test_one_round_over_more_files_than_the_old_limit_is_scanned_once_per_file(tmp_path, scans):
    """一轮扫描轮流碰的 JSON 比以前的上限 512 多时，第二轮也要全部命中（原来每轮都是零命中）。"""
    paths = [_write(tmp_path / f"{index}.json", {"i": index}) for index in range(700)]
    for path in paths:
        parsers.load_json_file(path)
    first_round = len(scans)

    for path in paths:
        parsers.load_json_file(path)

    assert first_round == 700
    assert len(scans) == 700


def test_oversized_and_missing_files_still_fail_the_same_way(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "MAX_JSON_BYTES", 10)
    big = _write(tmp_path / "big.json", {"k": "x" * 50})

    with pytest.raises(ValueError, match="64 MiB"):
        parsers.load_json_file(big)
    with pytest.raises(FileNotFoundError):
        parsers.load_json_file(tmp_path / "missing.json")


def test_one_import_round_scans_each_json_file_once_although_it_loads_them_many_times(
    tmp_path, monkeypatch, scans
):
    archive = tmp_path / "archive"
    write_meeting(archive, "正式会议", official=True, transcript_text="正式稿内容")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    loads = []
    real_load = importer.load_json_file
    monkeypatch.setattr(
        importer, "load_json_file", lambda path: (loads.append(str(path)), real_load(path))[1]
    )

    report = ArchiveImporter(db, settings).scan()

    assert report.errors == 0
    distinct = set(loads)
    # 这一轮同一份清单被加载了不止一次，否则这条用例证明不了什么
    assert len(loads) > len(distinct)
    assert len(scans) == len(distinct)
