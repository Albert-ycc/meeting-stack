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
- 4b：候选词（glossary_candidates.term）里的「候选词哨兵丁」不出现在任何提示词里；会名里的「会名哨兵乙」
  不出现在 4b 发出的请求里（4c、4g 和项目归属本来就发会名）；假 AI 给 4b 回一条说法，L5 真的写出放宽行，
  材料文字哨兵不出现在它的 quote、evidence_json 里。
- 4c：决议对比只发决议原文、会名和需求名（<this>、<others>、<reqs> 三个标签），材料文字、文件名、逐字稿和
  纪要其余部分的哨兵都不在里面。
- 4d：相关（H3）真的跑出片段和相关行：只在材料文字里的哨兵不进 meeting_window_passages.words、relations 的
  quote 和 evidence_json；两边都有的词（文件词干「接口文档」）进了 words，但不进词条、候选词、项目线索、
  segments_fts、minutes_fts（行数不变）和任何提示词；相关的计算不发 AI。
- 4e：产出（L4）和可能过时（H2、L3）真的写出行：材料文字哨兵和文件名哨兵不出现在它们的 quote、
  evidence_json 里（影响的 quote 是决议原文，材料一端只存段号）；4e 不调 AI，单独再跑一遍 L3、L4、H2，
  记下的请求一条都不多。
- 4g：问答是唯一的例外。prepare 不调 AI；with_materials=false 的提示词里两个哨兵都没有；with_materials=true
  的提示词里材料文字哨兵正好一次、文件名哨兵零次；之后跑项目归属、任务抽取、两个循环、快照和线索，都没有这两个
  哨兵；假 AI 回答里的「回答专用标记乙」整库一处都没有；问答日志里没有问题、原文和回答。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meeting_workbench import affects, produced
from meeting_workbench.cards import CardWriter
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.decision_pairs import DecisionPairTask
from meeting_workbench.deep_links import LinksWorker
from meeting_workbench.links_llm import LinksLLMWorker, ordered
from meeting_workbench.loose_mentions import TASK_BACKFILL, TASK_RECENT, LooseMentionTask
from meeting_workbench.glossary import SNAPSHOT_FILENAME, rewrite_snapshot
from meeting_workbench.material_index import MaterialIndexer
from meeting_workbench.material_vectors import MaterialVectors
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.project_profile import build_cue_table
from meeting_workbench.tasks import TaskService

from .test_material_index import run_until_done
from .test_project_linking import make_project, seed_meeting
from .test_related import MODEL, TOPIC_A, FakeSemantic, embed_all, segments_for

MATERIAL_SENTINEL = "蓝鲸七号材料原文"
NAME_SENTINEL = "绝密文件名甲"
TITLE_SENTINEL = "会名哨兵乙"
CANDIDATE_SENTINEL = "候选词哨兵丁"
# 4g：假 AI 给问答的回答里带它，整库一处都不许有
ANSWER_SENTINEL = "回答专用标记乙"
# 4g 的请求：user 消息里有 <sources> 标签
QA_MARK = "<sources>"
LOOSE_MEETING = "vm-20260926-160000"
# 4b 的请求：user 消息里有 <transcript> 标签
LOOSE_MARK = "<transcript>"
# 4c 的请求：user 消息里有 <others> 标签
PAIRS_MARK = "<others>"
PAIRS_MEETING = "vm-20260925-100000"
MINUTES_SENTINEL = "纪要摘要哨兵丙"
TRANSCRIPT_SENTINEL = "逐字稿哨兵戊"
CONTENT_KEY = "q2:" + "7" * 32
# 4d：一份和会上说的是一回事的材料（里面也有材料文字哨兵），文件名的词干「接口文档」两边都有
RELATED_KEY = "q2:" + "8" * 32
RELATED_MEETING = "vm-20260926-090000"
SHARED_STEM = "接口文档"

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
        body = data.decode("utf-8")
        requests.append(f"{getattr(request, 'full_url', request)}\n{body}")
        content = "{}"
        if LOOSE_MARK in body:
            # 4b：回一条会上真说过的说法，L5 才有东西可对
            content = json.dumps(
                {"refs": [{"at": "00:01:00", "quote": "上周那版报价单再看一下", "phrase": "上周那版报价单",
                           "core": "报价单", "aka": [], "kind": "表格", "when": {"rel": "last_week", "version": None}}]},
                ensure_ascii=False,
            )
        elif QA_MARK in body:
            # 4g：问答的回答（纯文本，带出处）
            content = f"{ANSWER_SENTINEL}：预算另议[M1][T1]。"
        return _Reply({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

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
    (root / "需求").mkdir()
    (root / "需求" / f"{SHARED_STEM}.docx").write_text("正文", encoding="utf-8")
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
    # 4d：材料一侧（带向量，假编码器按文字给）
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
           VALUES (?, 'text', 'done', 200, 1, ?, ?)""",
        (RELATED_KEY, now, now),
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES (?, 0, ?)",
        (RELATED_KEY, "。".join(TOPIC_A) + f"。{MATERIAL_SENTINEL}"),
    )
    db.execute(
        """UPDATE material_files SET content_key = ?, content_size = size, content_mtime_ns = mtime_ns
            WHERE name = ?""",
        (RELATED_KEY, f"{SHARED_STEM}.docx"),
    )
    embed_all(db)
    seed_meeting(
        db, RELATED_MEETING, "驻场沟通", "# 摘要\n\n排期。", segments=segments_for(TOPIC_A),
        project_id=project_id, origin="manual",
    )
    segments = [(0, "大家好，开始吧"), (754_000, "报价单按第三版发出"), (900_000, "排期表下周定稿")]
    seed_meeting(
        db, "vm-20260926-143000", "云图周会", MINUTES, segments=segments,
        project_id=project_id, origin="ai",
    )
    seed_meeting(db, "vm-20260927-100000", "周会", "# 摘要\n\n讨论报价单。", segments=segments)
    # 4c：同项目另一场会也定了报价单的事，初筛能留下，对比真的发出去
    seed_meeting(
        db, PAIRS_MEETING, "报价沟通",
        f"# 报价沟通\n\n## 一分钟摘要\n\n{MINUTES_SENTINEL}\n\n## 决议\n\n- 报价单按第二版发出 [00:03:00]\n",
        segments=[(0, TRANSCRIPT_SENTINEL), (180_000, "报价单按第二版发出")],
        project_id=project_id, origin="manual",
    )
    # 4b：一场逐字稿够长的会（会名里有哨兵），会上说了「上周那版报价单」
    long_talk = [(0, "开始吧")] + [
        (60_000, "上周那版报价单再看一下"),
        *[((index + 2) * 60_000, f"我们今天把排期再过一遍，确认下周的节奏安排和人手{index}") for index in range(20)],
    ]
    seed_meeting(
        db, LOOSE_MEETING, f"{TITLE_SENTINEL}周会", "# 摘要\n\n排期。", segments=long_talk,
        project_id=project_id, origin="manual",
    )
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (project_id, CANDIDATE_SENTINEL, CANDIDATE_SENTINEL, now, now),
    )
    return db, settings, root


def run_everything(db: Database, settings: Settings) -> None:
    """项目归属、任务抽取、卡片、快照，以及开着 links_enabled 跑的 links_loop 一整轮（L1 到 L5、H2 到
    H4、清理）和 links_llm_loop 一次。4b 起各步往两个循环里填的活、索引和需求背景也就跟着跑到了
    （glossary_mining_enabled 开着）。"""
    ProjectLinker(db, settings).link_pending()
    TaskService(db, settings).extract_pending()
    links_settings = settings.model_copy(
        update={
            "links_enabled": True,
            "glossary_mining_enabled": True,
            # 4d：相关要语义索引和材料正文读取都开着（假编码器，材料向量已经写好）
            "semantic_enabled": True,
            "material_content_enabled": True,
            "semantic_model": MODEL,
        }
    )
    semantic = FakeSemantic()
    vectors = MaterialVectors(db, links_settings, semantic)
    vectors.refresh()
    llm_worker = LinksLLMWorker(
        db,
        links_settings,
        tasks=ordered(
            [
                LooseMentionTask(links_settings, TASK_RECENT),
                DecisionPairTask(links_settings),
                LooseMentionTask(links_settings, TASK_BACKFILL),
            ]
        ),
    )
    links = LinksWorker(db, links_settings, llm=llm_worker, semantic=semantic, vectors=vectors)
    links.run_round()
    # AI 循环跑到没活（4b 一段一次、4c 一场会一次），再跑一轮本机循环让 L5 把说法对到文件
    for _ in range(20):
        if not llm_worker.tick()["called"]:
            break
    links.run_round()
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
        ).fetchone()[0] == 2  # 4a 的一段、4d 的一段
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


def test_loose_mentions_send_only_the_transcript(tmp_path, fake_ai):
    db, settings, root = build_world(tmp_path)

    run_everything(db, settings)

    loose_requests = [text for text in fake_ai if LOOSE_MARK in text]
    assert loose_requests, "4b 一次都没发，这个测试什么都没验证"
    for text in loose_requests:
        for sentinel in (TITLE_SENTINEL, NAME_SENTINEL, MATERIAL_SENTINEL, CANDIDATE_SENTINEL, "云图AI"):
            assert sentinel not in text, sentinel
        assert "上周那版报价单再看一下" in text
    # 候选词、文件名、材料文字不在任何一次请求里
    for text in fake_ai:
        for sentinel in (CANDIDATE_SENTINEL, NAME_SENTINEL, MATERIAL_SENTINEL):
            assert sentinel not in text, sentinel
    # L5 真的写出了放宽行（上面的材料文字断言才有意义）
    rows = db.query_all("SELECT quote, evidence_json FROM relations WHERE kind = 'mention' AND origin = 'llm'")
    assert rows and rows[0]["quote"] == "上周那版报价单再看一下"
    for row in rows:
        assert MATERIAL_SENTINEL not in row["quote"] + row["evidence_json"]
        assert NAME_SENTINEL not in row["quote"] + row["evidence_json"]


def test_decision_pairs_send_only_decision_text(tmp_path, fake_ai):
    db, settings, root = build_world(tmp_path)

    run_everything(db, settings)

    pair_requests = [text for text in fake_ai if PAIRS_MARK in text]
    assert pair_requests, "4c 一次都没发，这个测试什么都没验证"
    for text in pair_requests:
        for sentinel in (MATERIAL_SENTINEL, NAME_SENTINEL, CANDIDATE_SENTINEL, MINUTES_SENTINEL, TRANSCRIPT_SENTINEL,
                         "报价单 v3.xlsx", str(root)):
            assert sentinel not in text, sentinel
        # 发的是决议原文和会名
        assert "报价单按第二版发出" in text and "报价单按第三版发出" in text and "报价沟通" in text


def test_related_keeps_material_text_out_and_shared_words_local(tmp_path, fake_ai):
    db, settings, root = build_world(tmp_path)
    with db.autocommit() as connection:
        fts_before = [
            connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (f'"{SHARED_STEM}"',)).fetchone()[0]
            for table in ("segments_fts", "minutes_fts")
        ]

    run_everything(db, settings)

    # H3 真的跑出了片段和相关行（下面的断言才有意义）
    rows = db.query_all("SELECT words FROM meeting_window_passages WHERE meeting_id = ?", (RELATED_MEETING,))
    assert rows and all(SHARED_STEM in json.loads(row["words"]) for row in rows)
    links = db.query_all("SELECT quote, evidence_json FROM relations WHERE kind = 'related'")
    assert links
    for row in links:
        assert MATERIAL_SENTINEL not in row["quote"] + row["evidence_json"]
        assert NAME_SENTINEL not in row["quote"] + row["evidence_json"]
    for row in db.query_all("SELECT words FROM meeting_window_passages"):
        assert MATERIAL_SENTINEL not in row["words"]
    # 共同词不进词条、候选词、项目线索；两张会议全文表的行数不变
    with db.autocommit() as connection:
        assert SHARED_STEM not in repr(build_cue_table(connection))
        places = database_leaks(connection, SHARED_STEM)
        assert "meeting_window_passages.words" in places
        assert not [place for place in places if place.startswith(("glossary_", "project_"))], places
        fts_after = [
            connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (f'"{SHARED_STEM}"',)).fetchone()[0]
            for table in ("segments_fts", "minutes_fts")
        ]
    assert fts_after == fts_before
    # 文件词干和材料文字不在任何提示词里；这场会的逐字稿本身也没有发出去（相关的计算不发 AI）
    for text in fake_ai:
        assert SHARED_STEM not in text and MATERIAL_SENTINEL not in text
        assert TOPIC_A[1] not in text


def test_produced_and_affects_keep_material_text_out(tmp_path, fake_ai):
    """4e：L4 和 H2 在 run_everything 里真的写出产出和影响；哨兵不进它们的 quote、evidence_json；
    单独再跑 L3、L4、H2 不发任何 AI 请求。"""
    db, settings, root = build_world(tmp_path)
    project_id = db.query_one("SELECT id FROM projects WHERE name = '云图AI'")["id"]
    root_id = db.query_one("SELECT id FROM project_material_roots")["id"]
    now = datetime.now(UTC)
    # 产出：一条一小时前确认的任务，之后新出现的一份名字对得上的文件（算不出内容标识的格式）
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, project_id, status_changed_at, created_at, updated_at)
           VALUES ('t-4e', '整理报价单明细', 'confirmed', 'ai', ?, 'x', 'x', 'x')""",
        (project_id,),
    )
    db.execute(
        "INSERT INTO task_events(task_id, kind, body, created_at) VALUES ('t-4e', 'confirmed', '任务已确认', ?)",
        ((now - timedelta(hours=1)).isoformat(),),
    )
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone, seen_at)
           VALUES (?, '报价/报价单明细.key', '报价', '报价单明细.key', '报价单明细', '报价单明细', 'key', 10, 1, 'normal', 'x')""",
        (root_id,),
    )
    # 影响：一场定了「总价下调 5%」的会；一份上个月的文件还写着下调 3%，同一段里有材料文字哨兵
    seed_meeting(
        db, "vm-20260926-170000", "定价会", "# 定价会\n\n## 决议\n\n- 总价下调 5% [00:01:00]\n",
        project_id=project_id, origin="manual",
    )
    price_key = "q2:" + "9" * 32
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
           VALUES (?, 'text', 'done', 40, 1, 'x', 'x')""",
        (price_key,),
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES (?, 0, ?)",
        (price_key, f"报价说明：总价下调 3%，{MATERIAL_SENTINEL}"),
    )
    old_ns = int((now - timedelta(days=30)).timestamp() * 1_000_000_000)
    db.execute(
        """UPDATE material_files SET content_key = ?, content_size = size, mtime_ns = ?, content_mtime_ns = ?
            WHERE name = '报价单 v3.xlsx'""",
        (price_key, old_ns, old_ns),
    )

    run_everything(db, settings)

    rows = db.query_all("SELECT kind, quote, evidence_json FROM relations WHERE kind IN ('produced', 'affects')")
    assert {row["kind"] for row in rows} == {"produced", "affects"}, "4e 一行都没写，这个测试什么都没验证"
    for row in rows:
        for sentinel in (MATERIAL_SENTINEL, NAME_SENTINEL, "报价说明"):
            assert sentinel not in row["quote"] + row["evidence_json"], sentinel
    assert db.query_one("SELECT quote FROM relations WHERE kind = 'affects'")["quote"] == "总价下调 5%"
    for text in fake_ai:
        assert MATERIAL_SENTINEL not in text and "报价说明" not in text
    # 4e 不调 AI：单独再跑一遍 L3、L4、H2，记下的请求一条都不多
    before = len(fake_ai)
    later = datetime.now(UTC) + timedelta(minutes=1)
    stamp = later.isoformat()
    affects.auto_clear(db, later, stamp)
    produced.watch(db, later, 5.0, since=stamp)
    db.execute("UPDATE decision_scan SET affects_hash = NULL")
    assert affects.match_due(db, lambda: False, 5.0, now=later, since=stamp)["tried"] >= 1
    assert len(fake_ai) == before


def test_ask_sends_material_text_only_after_confirm(tmp_path, fake_ai, caplog):
    """4g：材料原文只在 with_materials=true 的那一次请求里、只有段落文字；回答不落库；日志里什么都没有。"""
    from meeting_workbench import asks
    from meeting_workbench.materials import ROOT_ONLINE

    caplog.set_level(logging.DEBUG)
    db, settings, root = build_world(tmp_path)
    project_id = db.query_one("SELECT id FROM projects WHERE name = '云图AI'")["id"]
    service = asks.AskService(
        db,
        settings,
        worker=LinksLLMWorker(db, settings),
        registry=asks.AskRegistry(spawn=lambda fn: fn()),
        state_of=lambda _path: ROOT_ONLINE,
    )
    question = "排期表和预算另议"
    plan = service.prepare(project_id, question)
    assert fake_ai == [], "prepare 不调 AI"
    assert plan["counts"]["materials"] == 1 and plan["counts"]["meetings"] >= 1
    assert plan["confirm"] is not None
    # 出处小块的名字是本机拼的，只给页面
    assert any(NAME_SENTINEL in (source.get("name") or "") for source in plan["sources"])

    service.ask(project_id, plan["plan_id"], False)
    service.ask(project_id, plan["plan_id"], True)
    assert len(fake_ai) == 2
    meetings_only, with_materials = fake_ai
    assert QA_MARK in meetings_only and QA_MARK in with_materials
    assert MATERIAL_SENTINEL not in meetings_only and NAME_SENTINEL not in meetings_only
    assert with_materials.count(MATERIAL_SENTINEL) == 1
    assert NAME_SENTINEL not in with_materials
    assert str(root) not in with_materials and "报价单 v3.xlsx" not in with_materials

    run_everything(db, settings)

    later = fake_ai[2:]
    assert later, "之后的循环一次都没调 AI，这个测试什么都没验证"
    for text in later:
        assert MATERIAL_SENTINEL not in text and NAME_SENTINEL not in text and ANSWER_SENTINEL not in text
    places = outgoing(db, settings, root, later)
    for place, text in places.items():
        assert MATERIAL_SENTINEL not in text, place
        assert NAME_SENTINEL not in text, place
        assert ANSWER_SENTINEL not in text, place
    with db.autocommit() as connection:
        assert database_leaks(connection, MATERIAL_SENTINEL) == []
        # 回答哨兵：整库每张表的文本列（连材料表一起）都找不到
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                for value in row:
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", "ignore")
                    assert not (isinstance(value, str) and ANSWER_SENTINEL in value), table
    asks_log = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "meeting_workbench.asks"
    )
    assert "问答回来" in asks_log
    for secret in (question, "预算另议", MATERIAL_SENTINEL, NAME_SENTINEL, ANSWER_SENTINEL):
        assert secret not in asks_log
    assert ANSWER_SENTINEL not in caplog.text and MATERIAL_SENTINEL not in caplog.text
