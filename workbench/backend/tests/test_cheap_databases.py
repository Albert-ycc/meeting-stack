"""conftest 的 _cheap_databases 让新库从模板克隆，再跑真 initialize()。克隆出来的库必须和真从零建出来的一样：
表结构、全部行（含 rowid）、user_version，带时间的种子行写的是 initialize() 那一刻的时间，不是模板建成的时间。
以后 initialize() 里再加带时间、带随机 id 的种子行，模板里的值会被所有克隆共用，这条用例会拦住。"""

from __future__ import annotations

import sqlite3

from meeting_workbench import db as db_module
from meeting_workbench.db import Database

from .conftest import REAL_INITIALIZE, TEST_PAGE_SIZE

FROZEN = "2031-01-02T03:04:05.678901+00:00"


def rows(connection: sqlite3.Connection, table: str) -> list:
    """带 rowid 的表连 rowid 一起取（rowid 也要一样）；WITHOUT ROWID 的表没有它，按内容排序。"""
    try:
        return connection.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid').fetchall()
    except sqlite3.OperationalError:
        return sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)


def dump(path) -> dict:
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        names = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {name: rows(connection, name) for (name,) in names}
        return {
            "schema": schema,
            "tables": tables,
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_keys": connection.execute("PRAGMA foreign_key_check").fetchall(),
        }
    finally:
        connection.close()


def test_a_cloned_database_equals_one_built_from_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "utc_now", lambda: FROZEN)
    scratch = Database(tmp_path / "scratch.sqlite3")
    REAL_INITIALIZE(scratch)
    cloned = Database(tmp_path / "cloned.sqlite3")
    cloned.initialize()

    with sqlite3.connect(cloned.path) as connection:
        assert connection.execute("PRAGMA page_size").fetchone()[0] == TEST_PAGE_SIZE
    assert dump(cloned.path) == dump(scratch.path)
    seeds = dict(
        sqlite3.connect(cloned.path).execute("SELECT key, value FROM app_state").fetchall()
    )
    assert seeds["links_since"] == FROZEN
    assert seeds["cards_since"] == FROZEN
    assert seeds["requirement_candidates_since"] == FROZEN
    assert len(seeds) == 6


def test_a_second_initialize_on_a_cloned_database_changes_nothing(tmp_path, monkeypatch):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    before = dump(db.path)
    monkeypatch.setattr(db_module, "utc_now", lambda: FROZEN)
    db.initialize()
    assert dump(db.path) == before
