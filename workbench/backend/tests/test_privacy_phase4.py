"""第四期的隐私哨兵（4a 建骨架，4b 到 4h 每步往 run_everything 里加自己的循环和出口）。

两种哨兵：
- 材料文字：一个材料片段里放「蓝鲸七号材料原文」。不出现在任何一次 AI 请求、glossary-snapshot.json、
  项目线索、segments_fts、minutes_fts、卡片、任何 relations.quote 和 evidence_json、
  meeting_window_passages.words 里；整库文本扫描只放过它本来就在的地方（material_chunks 和它的全文
  影子表、material_files）和三处白名单（glossary_mining_seeds.terms_json、glossary_candidates.term、
  glossary_candidates.wrong）。
- 文件名：根目录下面一个子文件夹（不是根目录本身：根目录名会进项目线索）里的「绝密文件名甲」文件夹
  和文件。只查往外发的地方：AI 请求、快照、项目线索、两张会议全文表、卡片、日志；存位置的列不算，
  不做整库扫描。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from pathlib import Path

import pytest

from meeting_workbench.cards import CardWriter
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.deep_links import LinksWorker
from meeting_workbench.links_llm import LinksLLMWorker
from meeting_workbench.glossary import SNAPSHOT_FILENAME, rewrite_snapshot
from meeting_workbench.material_index import MaterialIndexer
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.project_profile import build_cue_table
from meeting_workbench.tasks import TaskService

from .test_material_index import run_until_done
from .test_project_linking import make_project, seed_meeting

MATERIAL_SENTINEL = "蓝鲸七号材料原文"
NAME_SENTINEL = "绝密文件名甲"
CONTENT_KEY = "q2:" + "7" * 32

# 材料文字哨兵本来就在的表，和三处白名单
MATERIAL_TEXT_TABLES = ("material_chunks", "material_files")
MATERIAL_TEXT_PREFIXES = ("material_chunks_fts",)
MATERIAL_TEXT_COLUMNS = frozenset(
    {
        ("glossary_mining_seeds", "terms_json"),
        ("glossary_candidates", "term"),
        ("glossary_candidates", "wrong"),
    }
)

MINUTES = (
    "# 云图周会\n\n## 一分钟摘要\n\n讨论报价单和排期。\n\n## 决议\n\n"
    "- 报价单按第三版发出 [00:12:34]\n- 排期表下周定稿\n"
)


class _Reply:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, amt: int = -1) -> bytes:
        # tasks.call_llm 一次读完；llm.chat 分块读，读到空串为止
        body, self._body = self._body, b""
        return body

    def close(self) -> None:
        return None

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def fake_ai(monkeypatch) -> list[str]:
    """假 AI：记下每一次请求的地址和请求体，回一个空的 JSON。"""
    requests: list[str] = []

    def urlopen(request, *_args, **_kwargs):
        data = getattr(request, "data", None) or b""
        requests.append(f"{getattr(request, 'full_url', request)}\n{data.decode('utf-8')}")
        return _Reply({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return requests


def build_world(tmp_path: Path) -> tuple[Database, Settings, Path]:
    disk = tmp_path / "disk"
    root = disk / "云图AI"
    secret = root / "资料" / NAME_SENTINEL
    secret.mkdir(parents=True)
    (secret / f"{NAME_SENTINEL}.docx").write_text("正文", encoding="utf-8")
    (root / "报价").mkdir()
    (root / "报价" / "报价单 v3.xlsx").write_text("表格", encoding="utf-8")
    key = tmp_path / "api-key"
    key.write_text("sk-test", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        llm_api_key_file=key,
        material_browse_root=disk,
    )
    db = Database(settings.database_path)
    db.initialize()
    project_id = make_project(db, "云图AI")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, str(root), utc_now()),
    )
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    # 内容循环在测试里关着：直接写一份读好的内容
    now = utc_now()
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
           VALUES (?, 'text', 'done', 20, 1, ?, ?)""",
        (CONTENT_KEY, now, now),
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES (?, 0, ?)",
        (CONTENT_KEY, f"方案第三节：{MATERIAL_SENTINEL}，预算另议。"),
    )
    db.execute(
        """UPDATE material_files SET content_key = ?, content_size = size, content_mtime_ns = mtime_ns
            WHERE name = ?""",
        (CONTENT_KEY, f"{NAME_SENTINEL}.docx"),
    )
    segments = [(0, "大家好，开始吧"), (754_000, "报价单按第三版发出"), (900_000, "排期表下周定稿")]
    seed_meeting(
        db, "vm-20260926-143000", "云图周会", MINUTES, segments=segments,
        project_id=project_id, origin="ai",
    )
    seed_meeting(db, "vm-20260927-100000", "周会", "# 摘要\n\n讨论报价单。", segments=segments)
    return db, settings, root


def run_everything(db: Database, settings: Settings) -> None:
    """项目归属、任务抽取、卡片、快照，以及开着 links_enabled 跑的 links_loop 一整轮（L1 到 L5、H2 到
    H4、清理）和 links_llm_loop 一次。4b 起各步往两个循环里填的活、索引和需求背景也就跟着跑到了
    （glossary_mining_enabled 开着）。"""
    ProjectLinker(db, settings).link_pending()
    TaskService(db, settings).extract_pending()
    links_settings = settings.model_copy(update={"links_enabled": True, "glossary_mining_enabled": True})
    llm_worker = LinksLLMWorker(db, links_settings)
    LinksWorker(db, links_settings, llm=llm_worker).run_round()
    llm_worker.tick()
    CardWriter(db, settings).reconcile()
    rewrite_snapshot(db, settings.data_dir / SNAPSHOT_FILENAME)


def outgoing(db: Database, settings: Settings, root: Path, requests: list[str]) -> dict[str, str]:
    """往外发的地方，各拼成一段文字。"""
    with db.autocommit() as connection:
        cues = repr(build_cue_table(connection))
        segments = json.dumps(
            [list(row) for row in connection.execute("SELECT * FROM segments_fts")], ensure_ascii=False
        )
        minutes = json.dumps(
            [list(row) for row in connection.execute("SELECT * FROM minutes_fts")], ensure_ascii=False
        )
    cards = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.md")) if path.is_file()
    )
    return {
        "AI 请求": "\n".join(requests),
        "词典快照": (settings.data_dir / SNAPSHOT_FILENAME).read_text(encoding="utf-8"),
        "项目线索": cues,
        "segments_fts": segments,
        "minutes_fts": minutes,
        "卡片": cards,
    }


def database_leaks(connection: sqlite3.Connection, needle: str) -> list[str]:
    """整库文本扫描：哪些表的哪些列里有 needle（哨兵本来就在的表和白名单列不算）。"""
    found: set[str] = set()
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table in MATERIAL_TEXT_TABLES or table.startswith(MATERIAL_TEXT_PREFIXES):
            continue
        cursor = connection.execute(f'SELECT * FROM "{table}"')
        columns = [column[0] for column in cursor.description]
        for row in cursor:
            for column, value in zip(columns, row):
                if (table, column) in MATERIAL_TEXT_COLUMNS:
                    continue
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "ignore")
                if isinstance(value, str) and needle in value:
                    found.add(f"{table}.{column}")
    return sorted(found)


def test_material_text_stays_in_the_material_tables(tmp_path, fake_ai):
    db, settings, root = build_world(tmp_path)

    run_everything(db, settings)

    assert fake_ai, "假 AI 一次都没被调，这个测试什么都没验证"
    for place, text in outgoing(db, settings, root, fake_ai).items():
        assert MATERIAL_SENTINEL not in text, place
    for row in db.query_all("SELECT quote, evidence_json FROM relations"):
        assert MATERIAL_SENTINEL not in row["quote"] + row["evidence_json"]
    for row in db.query_all("SELECT words FROM meeting_window_passages"):
        assert MATERIAL_SENTINEL not in row["words"]
    with db.autocommit() as connection:
        # 哨兵确实在库里，扫描才有意义
        assert connection.execute(
            "SELECT COUNT(*) FROM material_chunks WHERE instr(text, ?) > 0", (MATERIAL_SENTINEL,)
        ).fetchone()[0] == 1
        assert database_leaks(connection, MATERIAL_SENTINEL) == []


def test_file_name_stays_in_location_columns(tmp_path, fake_ai, caplog):
    caplog.set_level(logging.DEBUG)
    db, settings, root = build_world(tmp_path)

    run_everything(db, settings)

    # 哨兵确实在存位置的列里：文件行和文件夹行
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM material_files WHERE instr(rel_path, ?) > 0", (NAME_SENTINEL,)
    ) == {"n": 1}
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM material_dirs WHERE instr(dir_rel, ?) > 0", (NAME_SENTINEL,)
    ) == {"n": 1}
    places = outgoing(db, settings, root, fake_ai)
    places["日志"] = caplog.text
    for place, text in places.items():
        assert NAME_SENTINEL not in text, place
