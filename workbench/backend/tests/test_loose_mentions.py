"""第四期 4b：放宽的提到（loose_mentions）——只发逐字稿、分段、截断、按段认领、本机校验、重新定位和重抽、
T1 到 T4、按时间提示挑文件、和字面行一起、L5 的写和回答。"""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from meeting_workbench import file_mentions, links_llm, loose_mentions, relations
from meeting_workbench.db import utc_now
from meeting_workbench.file_mentions import ProjectContext
from meeting_workbench.links_llm import LinksLLMWorker
from meeting_workbench.llm import ChatReply, LLMError
from meeting_workbench.loose_mentions import Line, LooseMentionTask

from .test_file_mentions import add_file, ns
from .test_file_mentions import setup as fm_setup
from .test_graph import add_meeting
from .test_relations import rev

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
FILLER = "我们今天把排期再过一遍，确认下周的节奏安排和人手"


def settings(tmp_path, **overrides):
    key = tmp_path / "api-key"
    key.write_text("sk-test", encoding="utf-8")
    values = {
        "links_enabled": True,
        "links_llm_enabled": True,
        "links_llm_daily_calls": 200,
        "links_backfill_days": 180,
        "qa_daily_questions": 100,
        "llm_api_key_file": key,
        "llm_api_base": "http://127.0.0.1:9/v1",
        "llm_model": "test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def talk(*special, filler=20):
    """逐字稿：前面 filler 行垫话（凑够 300 字），后面是要测的几句，每句隔一分钟。"""
    texts = [f"{FILLER}{index}" for index in range(filler)] + list(special)
    return [(index * 60_000, text) for index, text in enumerate(texts)]


def at(index, filler=20):
    return (filler + index) * 60_000


def hms(ms):
    return loose_mentions._hms(ms)


class FakeChat:
    """假 AI：按 replies 依次回（dict 转成 JSON；str 原样；异常抛出），记下每一次的提示词。"""

    def __init__(self, *replies, finish="stop"):
        self.replies = list(replies)
        self.finish = finish
        self.calls: list[dict] = []

    def __call__(self, settings, *, system, user, json_mode, max_tokens, timeout):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
        )
        reply = (
            self.replies.pop(0)
            if len(self.replies) > 1
            else (self.replies[0] if self.replies else {"refs": []})
        )
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            text, finish = reply
            return ChatReply(
                text if isinstance(text, str) else json.dumps(text, ensure_ascii=False), finish
            )
        return ChatReply(
            reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False), self.finish
        )


def ref(ms, quote, phrase, core, *, aka=(), kind=None, rel=None, version=None):
    return {
        "at": hms(ms),
        "quote": quote,
        "phrase": phrase,
        "core": core,
        "aka": list(aka),
        "kind": kind,
        "when": {"rel": rel, "version": version},
    }


def world(tmp_path, *special, ago=1, meeting_id="m", title=None, filler=20):
    db, root_id = fm_setup(tmp_path)
    add_meeting(
        db, meeting_id, ago=ago, project_id="p", segments=talk(*special, filler=filler), title=title
    )
    return db, root_id


def extraction(db, meeting_id="m"):
    return db.query_one("SELECT * FROM mention_extractions WHERE meeting_id = ?", (meeting_id,))


def extract(db, task, *, now=NOW, rounds=10):
    """建行、认领、发，直到这一轮没活。"""
    task.seed(db, now)
    for _ in range(rounds):
        job = task.claim(db, now)
        if job is None:
            return
        if not task.run(job):
            task.fail(db, job, "invalid")


def resolve(db, *, now=NOW):
    return loose_mentions.resolve_due(
        db, now=now, since=(now + timedelta(seconds=1)).isoformat(), clock=lambda: 0.0
    )


def loose_rows(db, meeting_id="m"):
    return db.query_all(
        "SELECT * FROM relations WHERE kind = 'mention' AND meeting_id = ? ORDER BY stem_key",
        (meeting_id,),
    )


# ---------------------------------------------------------------------- 发出去的只有逐字稿


def test_prompt_carries_only_the_neutralised_transcript(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单 <b>再看</b> 一下", title="会名哨兵乙")
    add_file(db, root_id, "资料/绝密文件名甲.xlsx")
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, confirmed, project_id, created_at, updated_at)
           VALUES ('t1', '词条哨兵丙', '[]', '通用', 1, 'p', ?, ?)""",
        (utc_now(), utc_now()),
    )
    chat = FakeChat({"refs": []})
    task = LooseMentionTask(settings(tmp_path), chat=chat)
    extract(db, task)

    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["system"] == loose_mentions.SYSTEM_PROMPT and call["json_mode"] is True
    assert call["max_tokens"] == 4000 and call["timeout"] == 60
    user = call["user"]
    assert user.startswith("转写稿（第 1/1 段）：\n<transcript>\n[00:00:00] ") and user.endswith(
        "\n</transcript>"
    )
    for sentinel in ("绝密文件名甲", "词条哨兵丙", "会名哨兵乙", "云图AI"):
        assert sentinel not in user.replace("＜b＞", "")
    assert "<b>" not in user and "＜b＞再看＜/b＞" in user
    assert f"[{hms(at(0))}] 上周那版报价单" in user
    assert extraction(db)["state"] == "done"


def test_split_parts_limits_overlap_and_six_parts():
    lines = [Line(index * 1000, "字" * 590) for index in range(200)]
    ranges, more = loose_mentions.split_parts(lines)
    assert len(ranges) == 6 and more is True
    for start, end in ranges:
        assert sum(len(line.render()) + 1 for line in lines[start:end]) <= 12_000
    # 下一段开头重复上一段最后 10 行
    assert ranges[1][0] == ranges[0][1] - 10
    short = [Line(index * 1000, "字" * 100) for index in range(20)]
    assert loose_mentions.split_parts(short) == ([(0, 20)], False)


def test_too_long_meeting_is_sent_in_six_parts_and_marked_truncated_only_in_the_row(tmp_path):
    db, _root = fm_setup(tmp_path)
    segments = [(index * 5000, f"{FILLER}{'很长的话' * 140}{index}") for index in range(160)]
    add_meeting(db, "m", ago=1, project_id="p", segments=segments)
    chat = FakeChat({"refs": []})
    extract(db, LooseMentionTask(settings(tmp_path), chat=chat))
    row = extraction(db)
    assert (
        len(chat.calls) == 6
        and row["parts"] == 6
        and row["parts_done"] == 6
        and row["state"] == "done"
    )
    assert chat.calls[5]["user"].startswith("转写稿（第 6/6 段）")
    assert row["error"] == "truncated"


def test_truncated_survives_a_restart_a_recall_and_a_failed_try(tmp_path):
    db, _root = fm_setup(tmp_path)
    segments = [(index * 5000, f"{FILLER}{'很长的话' * 140}{index}") for index in range(160)]
    add_meeting(db, "m", ago=1, project_id="p", segments=segments)
    task = LooseMentionTask(settings(tmp_path), chat=FakeChat("不是 JSON"))
    task.seed(db, NOW)
    assert extraction(db)["error"] == "truncated"
    # 一次不合格记 invalid；之后这一段做成了，回到 truncated（多的那些还是没发）
    job = task.claim(db, NOW)
    assert task.run(job) is False
    task.fail(db, job, "invalid")
    assert extraction(db)["error"] == "invalid"
    task._chat = FakeChat({"refs": []})
    assert task.run(task.claim(db, NOW)) is True
    assert extraction(db)["error"] == "truncated"
    # 抽到一半原地改了字：从头来，照样记 truncated
    db.execute("UPDATE segments SET text = text || '改' WHERE ordinal = 0")
    assert task.run(task.claim(db, NOW)) is True
    row = extraction(db)
    assert row["parts_done"] == 1 and row["error"] == "truncated"
    # 做完以后重新转写成完全不同的长稿：清零重来，也记 truncated
    extract(db, task)
    assert extraction(db)["state"] == "done"
    new_version(
        db,
        "m",
        "generated",
        [(index * 5000, f"完全不同{'别的话题' * 140}{index}") for index in range(160)],
    )
    assert resolve(db)["recalled"] == 1
    row = extraction(db)
    assert (row["state"], row["error"]) == ("pending", "truncated")


def test_a_part_done_later_clears_the_old_invalid(tmp_path):
    db, _root = world(tmp_path, "上周那版报价单再看一下")
    task = LooseMentionTask(settings(tmp_path), chat=FakeChat("不是 JSON"))
    task.seed(db, NOW)
    job = task.claim(db, NOW)
    assert task.run(job) is False
    task.fail(db, job, "invalid")
    assert extraction(db)["error"] == "invalid"
    task._chat = FakeChat({"refs": []})
    extract(db, task)
    row = extraction(db)
    assert (row["state"], row["error"], row["attempts"]) == ("done", None, 1)
    # truncated_output 是这一段自己的记号：留着
    db.execute(
        "UPDATE mention_extractions SET state = 'pending', parts_done = 0, error = 'truncated_output'"
    )
    extract(db, task)
    assert extraction(db)["error"] == "truncated_output"


# ---------------------------------------------------------------------- 截断


def test_truncated_reply_keeps_complete_items(tmp_path):
    db, _root = world(tmp_path, "上周那版报价单再看一下")
    broken = json.dumps(
        {
            "refs": [
                ref(at(0), "上周那版报价单再看一下", "上周那版报价单", "报价单", rel="last_week")
            ]
        },
        ensure_ascii=False,
    )
    broken = broken[:-2] + ', {"at": "00:2'
    chat = FakeChat((broken, "length"))
    extract(db, LooseMentionTask(settings(tmp_path), chat=chat))
    row = extraction(db)
    assert row["state"] == "done" and row["error"] == "truncated_output" and row["attempts"] == 0
    [phrase] = json.loads(row["phrases_json"])
    assert phrase["core"] == "报价单" and phrase["when"] == {"rel": "last_week", "version": None}


def test_truncated_reply_without_items_splits_the_part_and_does_not_count(tmp_path):
    db, _root = fm_setup(tmp_path)
    segments = [(index * 5000, f"{FILLER}{'很长的话' * 140}{index}") for index in range(30)]
    add_meeting(db, "m", ago=1, project_id="p", segments=segments)
    chat = FakeChat(('{"refs": [{"at": "00:0', "length"), {"refs": []})
    task = LooseMentionTask(settings(tmp_path), chat=chat)
    task.seed(db, NOW)
    before = extraction(db)["parts"]
    job = task.claim(db, NOW)
    assert task.run(job) is True
    row = extraction(db)
    assert row["parts"] == before + 1 and row["parts_done"] == 0 and row["attempts"] == 0
    assert row["state"] == "pending"
    extract(db, task)
    row = extraction(db)
    assert row["state"] == "done" and row["parts_done"] == row["parts"] == before + 1
    assert len(chat.calls) == before + 2
    # 切细以后每一行都发到过
    sent = "\n".join(call["user"] for call in chat.calls[1:])
    for index in range(30):
        assert f"{'很长的话' * 140}{index}\n" in sent + "\n"


# ---------------------------------------------------------------------- 按段认领


def test_parts_resume_the_next_day_and_claims_survive_recovery(tmp_path):
    db, _root = fm_setup(tmp_path)
    segments = [(index * 5000, f"{FILLER}{'很长的话' * 140}{index}") for index in range(45)]
    add_meeting(db, "m", ago=1, project_id="p", segments=segments)
    chat = FakeChat({"refs": []})
    task = LooseMentionTask(settings(tmp_path), chat=chat)
    day = {"value": date(2026, 9, 26)}
    worker = LinksLLMWorker(
        db,
        settings(tmp_path, links_llm_daily_calls=1),
        tasks=[task],
        now=lambda: NOW,
        today=lambda: day["value"],
    )
    assert worker.tick()["state"] == "ok"
    row = extraction(db)
    parts = row["parts"]
    assert parts >= 3 and row["parts_done"] == 1 and row["state"] == "pending"
    assert worker.tick()["state"] == "capped"
    day["value"] = date(2026, 9, 27)
    assert worker.tick()["state"] == "ok"
    row = extraction(db)
    assert row["parts_done"] == 2 and row["attempts"] == 0
    assert chat.calls[1]["user"].startswith(f"转写稿（第 2/{parts} 段）")

    # 认领超过 10 分钟收回：段进度和已抽出的说法都在
    db.execute('UPDATE mention_extractions SET phrases_json = \'[{"at_ms": 1, "core": "报价单"}]\'')
    job = task.claim(db, NOW - timedelta(minutes=30))
    assert job is not None
    with db.transaction() as connection:
        assert links_llm.recover_claims(connection, NOW) == 1
    row = extraction(db)
    assert row["state"] == "pending" and row["parts_done"] == 2 and "报价单" in row["phrases_json"]
    # 收回之后，旧的认领写不进来
    assert task.run(job) is True
    assert extraction(db)["parts_done"] == 2


def test_invalid_content_three_times_fails_and_network_does_not_count(tmp_path):
    db, _root = world(tmp_path, "上周那版报价单再看一下")
    task = LooseMentionTask(settings(tmp_path), chat=FakeChat(LLMError("network", sent=False)))
    worker = LinksLLMWorker(db, settings(tmp_path), tasks=[task], now=lambda: NOW)
    assert worker.tick()["state"] == "network"
    assert extraction(db)["attempts"] == 0 and extraction(db)["state"] == "pending"

    task._chat = FakeChat("不是 JSON")
    for expected in (1, 2):
        worker.clear_pause()
        assert worker.tick()["state"] == "invalid"
        assert extraction(db)["attempts"] == expected and extraction(db)["state"] == "pending"
    worker.clear_pause()
    assert worker.tick()["state"] == "invalid"
    assert extraction(db)["state"] == "failed"

    # 条目全被丢掉也算不合格；一个都没有（refs 为空）是合格的
    db.execute("UPDATE mention_extractions SET state = 'pending', attempts = 0")
    task._chat = FakeChat({"refs": [ref(at(0), "编造的原话", "编造", "编造的")]})
    assert worker.tick()["state"] == "invalid"
    task._chat = FakeChat({"refs": []})
    assert worker.tick()["state"] == "ok" and extraction(db)["state"] == "done"


@pytest.mark.parametrize(("status", "code"), [(400, "bad_request"), (401, "auth"), (403, "auth")])
def test_client_errors_are_not_retried(tmp_path, status, code):
    db, _root = world(tmp_path, "上周那版报价单再看一下")
    chat = FakeChat(LLMError(code, status=status))
    task = LooseMentionTask(settings(tmp_path), chat=chat)
    worker = LinksLLMWorker(db, settings(tmp_path), tasks=[task], now=lambda: NOW)
    assert worker.tick()["state"] == code
    assert len(chat.calls) == 1
    assert extraction(db)["attempts"] == (1 if code == "bad_request" else 0)


# ---------------------------------------------------------------------- 校验


def test_validate_drops_only_the_bad_items():
    lines = [
        Line(60_000, "上周那版报价单我们再看一下"),
        Line(120_000, "能耗看板那个PPT"),
        Line(180_000, "也发一下"),
    ]
    good = ref(60_000, "上周那版报价单我们再看一下", "上周那版报价单", "报价单", rel="last_week")
    items = [
        good,
        ref(600_000, "上周那版报价单", "报价单", "报价单"),  # 时间不在这一段
        ref(60_000, "这句话没说过", "没说过", "说过"),  # 编造的原话
        ref(60_000, "上周那版报价单", "能耗看板", "看板"),  # phrase 不在 quote 里
        {
            **ref(120_000, "能耗看板那个PPT", "能耗看板那个PPT", "能耗看板", kind="视频"),
            "aka": ["看板", "不存在的叫法"],
        },
        ref(120_000, "PPT也发一下", "PPT", "PPT"),  # 跨了两段的一句话
        ref(60_000, "报价单", "报价单", "报"),  # core 不到 2 个字
        {"at": 3},
    ]
    found = loose_mentions.validate(items, lines)
    assert [item["core"] for item in found] == ["报价单", "能耗看板", "PPT"]
    assert found[1]["kind"] is None and found[1]["aka"] == ["看板"]
    assert found[0]["when"] == {"rel": "last_week", "version": None}
    weird = {**good, "kind": "表格", "when": {"rel": "someday", "version": 300}}
    [item] = loose_mentions.validate([weird], lines)
    assert item["kind"] == "表格" and item["when"] == {"rel": None, "version": None}
    # 每段最多 40 条
    many = [
        ref(60_000 + index, f"第{index}份", f"第{index}份", f"第{index}份") for index in range(50)
    ]
    lines = [Line(60_000, "".join(f"第{index}份" for index in range(50)))]
    assert len(loose_mentions.validate(many, lines)) == 40


def test_parse_refs_takes_complete_items_from_a_cut_reply():
    assert loose_mentions.parse_refs('{"refs": []}') == []
    assert loose_mentions.parse_refs("oops") is None
    assert loose_mentions.parse_refs('{"x": 1}') is None
    cut = '```json\n{"refs": [{"at": "00:00:01", "core": "a"}, {"at": "00:0'
    assert loose_mentions.parse_refs(cut, partial=True) == [{"at": "00:00:01", "core": "a"}]
    assert loose_mentions.parse_refs('{"refs": [{"at', partial=True) == []


# ---------------------------------------------------------------------- 重新定位和重抽


def test_relocate_follows_moved_lines_and_drops_deleted_ones():
    phrases = [
        {"at_ms": 60_000, "quote": "上周那版报价单再看一下", "core": "报价单"},
        {"at_ms": 120_000, "quote": "能耗看板那个PPT", "core": "能耗看板"},
        {"at_ms": 180_000, "quote": "预算表发群里", "core": "预算表"},
    ]
    segments = [
        {"start_ms": 75_000, "text": "上周那版报价单，再看一下"},  # 挪了 15 秒，标点改了
        {"start_ms": 900_000, "text": "能耗看板那个 PPT"},  # 挪到很远，但全文只有一处
    ]
    moved = loose_mentions.relocate(phrases, segments)
    assert [(item["core"], item["at_ms"]) for item in moved] == [
        ("报价单", 75_000),
        ("能耗看板", 900_000),
    ]


def test_recall_needed_only_for_real_retranscription():
    text = "".join(f"{FILLER}{index}" for index in range(40))
    typo = text.replace("节奏", "结奏", 1)
    assert loose_mentions.recall_needed(text, typo, 9, 10) is False
    other = "".join(f"完全不同的一场会讨论的是别的事情{index}" for index in range(40))
    assert loose_mentions.recall_needed(text, other, 9, 10) is True
    assert loose_mentions.recall_needed(text, typo, 7, 10) is True
    assert loose_mentions.recall_needed(None, other, 9, 10) is False
    assert loose_mentions.jaccard(text, text) == 1.0


def done_with(db, meeting_id, phrases, version_id=None):
    row = db.query_one(
        "SELECT current_transcript_version_id AS v FROM meetings WHERE id = ?", (meeting_id,)
    )
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, parts, parts_done, phrases_json,
               finished_at, created_at, updated_at)
           VALUES (?, ?, 'sha', 'done', 1, 1, ?, ?, ?, ?)
           ON CONFLICT(meeting_id) DO UPDATE SET phrases_json = excluded.phrases_json, state = 'done',
               version_id = excluded.version_id""",
        (
            meeting_id,
            version_id or row["v"],
            json.dumps(phrases, ensure_ascii=False),
            utc_now(),
            utc_now(),
            utc_now(),
        ),
    )


def phrase(ms, quote, core, *, phrase_text=None, aka=(), kind=None, rel=None, version=None):
    return {
        "at_ms": ms,
        "quote": quote,
        "phrase": phrase_text or quote,
        "core": core,
        "aka": list(aka),
        "kind": kind,
        "when": {"rel": rel, "version": version},
    }


def new_version(db, meeting_id, kind, segments):
    version = db.create_transcript_version(meeting_id, kind, published=True)
    db.replace_segments(
        version,
        meeting_id,
        [
            {
                "id": f"{version}-s{index}",
                "ordinal": index,
                "start_ms": start,
                "end_ms": start + 4000,
                "text": text,
            }
            for index, (start, text) in enumerate(segments)
        ],
    )
    return version


def test_drafts_never_reset_and_close_retranscription_is_kept(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单再看一下")
    add_file(db, root_id, "报价单.xlsx")
    done_with(db, "m", [phrase(at(0), "上周那版报价单再看一下", "报价单")])
    resolve(db)
    assert len(loose_rows(db)) == 1

    # 存草稿、发布：换了 current_transcript_version_id，只重新定位，不重置
    segments = talk("上周那版报价单，再看一下吧")
    new_version(db, "m", "draft", segments)
    assert resolve(db)["recalled"] == 0
    assert extraction(db)["state"] == "done"
    [row] = loose_rows(db)
    assert row["status"] == "shown" and row["at_ms"] == at(0)

    # 重新转写，文字几乎一样：留着
    new_version(db, "m", "generated", talk("上周那版报价单，再看一下吧"))
    assert resolve(db)["recalled"] == 0 and extraction(db)["state"] == "done"

    # 重新转写，那句话没了：清零重来
    new_version(
        db,
        "m",
        "generated",
        [(index * 60_000, f"完全不同的内容{index}" * 3) for index in range(30)],
    )
    assert resolve(db)["recalled"] == 1
    row = extraction(db)
    assert (row["state"], row["parts_done"], row["attempts"], row["phrases_json"]) == (
        "pending",
        0,
        0,
        "[]",
    )


def unique_lines(count, *, offset=0, special=None):
    """每行字都不一样的逐字稿（4 字片段互不重叠），好算 Jaccard；special 放在第 0 行。"""
    lines = [
        (index * 60_000, "".join(chr(0x4E00 + offset + index * 40 + k) for k in range(30)))
        for index in range(count)
    ]
    if special:
        lines[0] = (0, special)
    return lines


def test_kept_retranscription_moves_the_row_to_the_new_version(tmp_path):
    db, root_id = fm_setup(tmp_path)
    first = unique_lines(30, special="上周那版报价单再看一下")
    add_meeting(db, "m", ago=1, project_id="p", segments=first)
    add_file(db, root_id, "报价单.xlsx")
    done_with(db, "m", [phrase(0, "上周那版报价单再看一下", "报价单", rel="last_week")])
    resolve(db)
    sent_id = extraction(db)["version_id"]

    # 重新转写，改了两行：留着，version_id、text_sha 换到新版
    second = list(first)
    second[10], second[11] = unique_lines(2, offset=5000)
    generated = new_version(db, "m", "generated", second)
    assert resolve(db)["recalled"] == 0
    row = extraction(db)
    with db.autocommit() as connection:
        sha = loose_mentions.text_sha(loose_mentions.transcript_lines(connection, generated))
    assert (row["version_id"], row["text_sha"], row["state"]) == (
        generated,
        sha,
        "done",
    ) and generated != sent_id
    assert json.loads(row["phrases_json"])[0]["at_ms"] == 0
    # 签名跟着重算：下一轮不再重做
    assert resolve(db)["tried"] == 0

    # 再存草稿改另外两行：和新版比很近，和最早发出去那一版已经差得多，也不重抽
    third = list(second)
    third[20], third[21] = unique_lines(2, offset=9000)
    fold = lambda lines: "".join(text for _ms, text in lines)  # noqa: E731
    assert (
        loose_mentions.jaccard(fold(first), fold(third))
        < 0.85
        <= loose_mentions.jaccard(fold(second), fold(third))
    )
    new_version(db, "m", "draft", third)
    assert resolve(db)["recalled"] == 0 and extraction(db)["state"] == "done"
    assert [row["status"] for row in loose_rows(db)] == ["shown"]


def test_seven_mentions_read_as_seven_places(tmp_path):
    from meeting_workbench import graph, relation_read

    from .test_graph import TODAY

    said = [f"能耗看板那个PPT再过一遍{index}" for index in range(7)]
    db, root_id = world(tmp_path, *said)
    plan = add_file(db, root_id, "能耗看板方案.pptx")
    done_with(
        db,
        "m",
        [
            phrase(at(index), text, "能耗看板", phrase_text="能耗看板那个PPT")
            for index, text in enumerate(said)
        ],
    )
    resolve(db)
    [row] = loose_rows(db)
    evidence = json.loads(row["evidence_json"])
    assert len(evidence["hits"]) == 3 and evidence["count"] == 7
    with db.autocommit() as connection:
        body = graph.project_graph(connection, "p", today=TODAY)
    [edge] = [edge for edge in body["edges"] if edge["kind"] == "mentioned"]
    assert edge["label"] == "会上说『能耗看板那个PPT』等 7 处 · 00:20:00"
    db.execute("UPDATE relations SET status = 'rejected' WHERE id = ?", (row["id"],))
    with db.autocommit() as connection:
        [rejected] = relation_read.rejected_loose_mentions(connection, plan)
    assert rejected["count"] == 7


def test_this_or_last_version_is_not_version_one(tmp_path):
    db, root_id = fm_setup(tmp_path)
    add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-15")
    add_file(db, root_id, "报价单 v3.xlsx", day="2026-09-20")

    def picked(*args, **kwargs):
        return matched(db, phrase(1, *args, **kwargs))["报价单"][0]

    assert picked("上一版报价单", "报价单", rel="previous") == "报价单 v2.xlsx"
    # AI 同时给了 rel 和一个原话里没有的版本号：按 rel
    assert picked("上一版报价单", "报价单", rel="previous", version=1) == "报价单 v2.xlsx"
    assert picked("这一版报价单", "报价单") == "报价单 v3.xlsx"
    assert picked("下一版报价单", "报价单") == "报价单 v3.xlsx"
    assert picked("这两版报价单", "报价单") == "报价单 v3.xlsx"
    # 明说了第几版的照认
    assert picked("第一版报价单", "报价单") == "报价单 v1.xlsx"
    assert picked("那个报价单", "报价单", version=2) == "报价单 v2.xlsx"


# ---------------------------------------------------------------------- T1 到 T4、kind、在组里挑


def context_of(db):
    with db.autocommit() as connection:
        return ProjectContext(connection, "p")


def matched(db, *phrases_):
    return {
        stem: (item["file"]["name"], item["via"], item["tier"])
        for stem, item in loose_mentions.resolve_phrases(
            context_of(db), list(phrases_), ns("2026-09-25"), None
        ).items()
    }


def test_tiers_one_to_four(tmp_path):
    db, root_id = fm_setup(tmp_path)
    add_file(db, root_id, "报价单.xlsx")
    add_file(db, root_id, "能耗看板方案.pptx")
    add_file(db, root_id, "设备采购清单.xlsx")
    add_file(db, root_id, "资料/能源管理平台.docx")
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, also, scope, confirmed, project_id, created_at, updated_at)
           VALUES ('t1', '设备采购清单', '[]', '["采购表"]', '通用', 1, 'p', ?, ?)""",
        (utc_now(), utc_now()),
    )
    assert matched(db, phrase(1, "上周那版报价单", "报价单")) == {
        "报价单": ("报价单.xlsx", "stem", 1)
    }
    assert matched(db, phrase(1, "那个能耗看板方案", "那个能耗看板方案")) == {
        "能耗看板方案": ("能耗看板方案.pptx", "stem", 2)
    }
    assert matched(db, phrase(1, "能耗看板那个PPT", "能耗看板")) == {
        "能耗看板方案": ("能耗看板方案.pptx", "stem", 2)
    }
    assert matched(db, phrase(1, "采购表发一下", "采购表")) == {
        "设备采购清单": ("设备采购清单.xlsx", "alias", 3)
    }
    assert matched(db, phrase(1, "能原管理平台", "能原管理平台")) == {
        "能源管理平台": ("能源管理平台.docx", "stem", 4)
    }
    # aka 只走 T1、T2
    assert matched(db, phrase(1, "那个东西", "那个东西", aka=["报价单"])) == {
        "报价单": ("报价单.xlsx", "alias", 1)
    }
    # 太短、常用词不算包含
    assert matched(db, phrase(1, "看板", "看板")) == {}


def test_two_groups_or_wrong_kind_do_not_link(tmp_path):
    db, root_id = fm_setup(tmp_path)
    add_file(db, root_id, "能耗看板方案.pptx")
    add_file(db, root_id, "能耗看板数据.xlsx")
    # 「能耗看板」对到两个词干组：不连、不猜
    assert matched(db, phrase(1, "能耗看板", "能耗看板")) == {}
    # 说了是表格：只剩一个组
    assert matched(db, phrase(1, "能耗看板那个表", "能耗看板", kind="表格")) == {
        "能耗看板数据": ("能耗看板数据.xlsx", "stem", 2)
    }
    # 说了是图片：一个都不剩
    assert matched(db, phrase(1, "能耗看板方案的图", "能耗看板方案", kind="图片")) == {}


def calendar(tmp_path):
    """一份固定日历（北京时间）：会是 2026-09-28 周一 10:00；文件分布在上周、这周、昨天（周日）、今天。"""
    db, root_id = fm_setup(tmp_path)
    ids = {}
    for label, when in (
        ("v1", "2026-09-19T12:00:00"),  # 上上周六
        ("v2", "2026-09-21T09:00:00"),  # 上周一
        ("v3", "2026-09-27T20:00:00"),  # 昨天（周日，上周的最后一天）
        ("v4", "2026-09-28T08:00:00"),  # 今天、开会之前
        ("v5", "2026-09-28T18:00:00"),  # 今天、开会之后
    ):
        ids[label] = add_file(db, root_id, f"报价单 {label}.xlsx")
        stamp = int(datetime.fromisoformat(when + "+08:00").timestamp() * 1_000_000_000)
        db.execute("UPDATE material_files SET mtime_ns = ? WHERE id = ?", (stamp, ids[label]))
    group = context_of(db).groups["报价单"]
    meeting = int(datetime.fromisoformat("2026-09-28T10:00:00+08:00").timestamp() * 1_000_000_000)
    return group, meeting, ids


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("last_week", "v3"),  # 上一个周一到周日：周日晚上那份
        ("this_week", "v4"),  # 周一开会：这周只有今天早上那份
        ("yesterday", "v3"),
        ("today", "v4"),
        ("latest", "v4"),
        ("previous", "v3"),
        ("last_meeting", "v2"),
    ],
)
def test_when_rel_on_a_fixed_calendar(tmp_path, rel, expected):
    group, meeting, ids = calendar(tmp_path)
    previous = int(datetime.fromisoformat("2026-09-22T10:00:00+08:00").timestamp() * 1_000_000_000)
    chosen = file_mentions.pick_by_hint(group, {"rel": rel}, meeting, previous)
    assert chosen["id"] == ids[expected]
    assert file_mentions.pick_by_hint(group, {"version": 2}, meeting)["id"] == ids["v2"]
    assert file_mentions.pick_by_hint(group, {"version": 9}, meeting) is None


def test_sunday_meeting_counts_its_own_week(tmp_path):
    group, _meeting, ids = calendar(tmp_path)
    sunday = int(datetime.fromisoformat("2026-09-27T21:00:00+08:00").timestamp() * 1_000_000_000)
    # 周日开会：「这周」从 9/21 周一算起，取开会前最新的那份（周日晚上 8 点）
    assert file_mentions.pick_by_hint(group, {"rel": "this_week"}, sunday)["id"] == ids["v3"]
    # 「上周」是 9/14 到 9/20：上上周六那份
    assert file_mentions.pick_by_hint(group, {"rel": "last_week"}, sunday)["id"] == ids["v1"]


# ---------------------------------------------------------------------- L5：和字面行一起写


def test_l5_writes_a_loose_row_when_there_is_no_literal_one(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单再看一下", "报价单还要改")
    old = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    new = add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-18")
    add_file(db, root_id, "报价单 v3.xlsx", day="2026-09-25")
    db.execute("UPDATE material_files SET mtime_ns = ? WHERE id = ?", (ns("2026-09-26"), old + 2))
    done_with(
        db,
        "m",
        [
            phrase(
                at(0),
                "上周那版报价单再看一下",
                "报价单",
                phrase_text="上周那版报价单",
                rel="last_week",
            ),
            phrase(at(1), "报价单还要改", "报价单"),
        ],
    )
    graph_before = rev(db)
    assert resolve(db)["written"] == 1
    [row] = loose_rows(db)
    evidence = json.loads(row["evidence_json"])
    assert (row["status"], row["origin"], row["ident"], row["file_id"]) == (
        "shown",
        "llm",
        "m|报价单",
        new,
    )
    assert row["at_ms"] == at(0) and row["quote"] == "上周那版报价单再看一下"
    assert evidence == {
        "phrase": "上周那版报价单",
        "via": "time_hint",
        "hits": [
            {"at_ms": at(0), "quote": "上周那版报价单再看一下"},
            {"at_ms": at(1), "quote": "报价单还要改"},
        ],
        "count": 2,
    }
    assert rev(db) == graph_before + 1

    # 签名没变：整轮不写，graph_rev 不动
    stamp = row["updated_at"]
    assert resolve(db) == {
        "pending": 0,
        "tried": 0,
        "written": 0,
        "unchanged": 0,
        "recalled": 0,
        "skipped": 0,
    }
    assert loose_rows(db)[0]["updated_at"] == stamp and rev(db) == graph_before + 1

    # 说法没了：放宽行收回
    done_with(db, "m", [])
    resolve(db)
    assert loose_rows(db)[0]["status"] == "cleared"


def test_l5_with_literal_rows(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单再看一下", "预算表也看一下", "排期表再说")
    add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    v2 = add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-18")
    budget = add_file(db, root_id, "预算表.xlsx")
    add_file(db, root_id, "排期表.xlsx")
    for stem, file_id, status, picked in (
        ("报价单", v2 - 1, "active", 0),
        ("预算表", budget, "rejected", 0),
        ("排期表", budget + 1, "active", 1),
    ):
        db.execute(
            """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
                   anchors_json, minutes_count, source, status, picked, updated_at)
               VALUES ('m', 'p', ?, ?, ?, 1, 0, '[0]', 0, 'transcript', ?, ?, ?)""",
            (stem, file_id, stem, status, picked, utc_now()),
        )
    db.execute(
        """INSERT INTO meeting_file_scan(meeting_id, stems_sig, dirty) VALUES ('m', 'x', 0)
           ON CONFLICT(meeting_id) DO UPDATE SET dirty = 0"""
    )
    done_with(
        db,
        "m",
        [
            phrase(at(0), "上周那版报价单再看一下", "报价单", rel="last_week"),
            phrase(at(1), "预算表也看一下", "预算表", rel="latest"),
            phrase(at(2), "排期表再说", "排期表", rel="latest"),
        ],
    )
    resolve(db)
    # 有效、没换过的字面行：只写提示，2d 下一轮重挑；rejected 和你换过的什么都不写
    assert loose_rows(db) == []
    assert json.loads(extraction(db)["hints_json"]) == {"报价单": {"rel": "last_week"}}
    assert db.query_one("SELECT dirty FROM meeting_file_scan WHERE meeting_id = 'm'") == {
        "dirty": 1
    }
    # 2d 按提示挪到上周那一版
    file_mentions.match_pending(db, clock=lambda: 0.0)
    literal = db.query_one(
        "SELECT file_id FROM meeting_file_mentions WHERE meeting_id = 'm' AND stem_key = '报价单'"
    )
    assert literal == {"file_id": v2}
    # 提示没变：不再加 dirty
    resolve(db)
    assert db.query_one("SELECT dirty FROM meeting_file_scan WHERE meeting_id = 'm'") == {
        "dirty": 0
    }


def test_at_most_ten_rows_per_meeting(tmp_path):
    names = [f"{word}清单" for word in "甲乙丙丁戊己庚辛壬癸子丑"]
    db, root_id = world(tmp_path, *[f"那个{name}看一下" for name in names])
    for name in names:
        add_file(db, root_id, f"{name}.xlsx")
    done_with(
        db, "m", [phrase(at(index), f"那个{name}看一下", name) for index, name in enumerate(names)]
    )
    resolve(db)
    rows = loose_rows(db)
    assert len(rows) == 10


def test_pick_and_undo_on_a_loose_row(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单再看一下")
    first = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    second = add_file(db, root_id, "报价/报价单 v2.xlsx", day="2026-09-11")
    done_with(db, "m", [phrase(at(0), "上周那版报价单再看一下", "报价单")])
    resolve(db)
    [row] = loose_rows(db)
    assert row["file_id"] == second
    now = utc_now()
    with db.transaction() as connection:
        result = relations.answer(connection, row["id"], {"answer": "pick", "file_id": first}, now)
    fresh = loose_rows(db)[0]
    assert (fresh["status"], fresh["origin"], fresh["file_id"]) == ("shown", "manual", first)
    assert json.loads(fresh["prev_json"])["file_id"] == second and result["undo_until"]
    # 只能换成本项目同一词干的活文件
    other = add_file(db, root_id, "预算表.xlsx")
    with db.transaction() as connection, pytest.raises(relations.RelationError) as error:
        relations.answer(connection, row["id"], {"answer": "pick", "file_id": other}, now)
    assert error.value.status in (409, 422)
    # 以后的轮次不挪它，说法没了也不收回
    done_with(db, "m", [])
    resolve(db)
    assert loose_rows(db)[0]["file_id"] == first and loose_rows(db)[0]["status"] == "shown"
    # 后来出现字面行：换过的放宽行照样显示（压过字面行）
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
               anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('m', 'p', '报价单', ?, '报价单', 1, 0, '[0]', 0, 'transcript', 'active', 0, ?)""",
        (second, utc_now()),
    )
    with db.autocommit() as connection:
        from meeting_workbench import relation_read

        rows = relation_read.meeting_mentions(connection, "m", "p")
    assert [(item["via"], item["file_id"]) for item in rows] == [("manual", first)]
    # 撤销换回原文件
    with db.transaction() as connection:
        relations.undo(connection, row["id"], now)
    fresh = loose_rows(db)[0]
    assert (fresh["origin"], fresh["file_id"]) == ("llm", second)


def test_wrong_pick_is_422_with_the_phase_two_sentence(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单再看一下")
    add_file(db, root_id, "报价单.xlsx")
    other = add_file(db, root_id, "预算表.xlsx")
    done_with(db, "m", [phrase(at(0), "上周那版报价单再看一下", "报价单")])
    resolve(db)
    [row] = loose_rows(db)
    with db.transaction() as connection, pytest.raises(relations.RelationError) as error:
        relations.answer(connection, row["id"], {"answer": "pick", "file_id": other}, utc_now())
    assert (
        error.value.status == 422 and str(error.value) == "只能换成这个项目文件夹里同名的另一份文件"
    )


# ---------------------------------------------------------------------- 状态句


class Snapshot:
    def __init__(self, **values):
        self.values = {"enabled": True, "llm": "ok", **values}

    def snapshot(self):
        return self.values


def test_brief_state_sentences(tmp_path):
    db, root_id = world(tmp_path, "上周那版报价单再看一下")
    add_file(db, root_id, "报价单.xlsx")
    on = settings(tmp_path)

    def state(worker, row_state=None, project="p", cfg=on):
        if row_state is None:
            db.execute("DELETE FROM mention_extractions")
        else:
            done_with(db, "m", [])
            db.execute("UPDATE mention_extractions SET state = ?", (row_state,))
        with db.autocommit() as connection:
            return loose_mentions.brief_state(connection, "m", project, worker, cfg)

    seen = [
        state(Snapshot(), "pending"),
        state(Snapshot(llm="no_key"), "pending"),
        state(Snapshot(llm="auth"), "running"),
        state(Snapshot(llm="capped"), "pending"),
        state(Snapshot(llm="balance"), "pending"),
        state(Snapshot(llm="backoff"), "pending"),
        state(Snapshot(), "failed"),
    ]
    assert [item["text"] for item in seen] == list(loose_mentions.LOOSE_SENTENCES)
    assert [item["action"] for item in seen].count({"kind": "retry", "label": "现在重试"}) == 1
    assert all(sum(1 for item in [entry["action"]] if item) <= 1 for entry in seen)
    # 抽完了、没有行、关着、AI 这一层关着、会议在转写（AI 循环照跑）
    assert state(Snapshot(), "done") is None
    assert state(Snapshot()) is None
    assert state(Snapshot(llm="off"), "pending") is None
    assert state(Snapshot(), "pending", cfg=settings(tmp_path, links_enabled=False)) is None
    assert state(Snapshot(paused="busy"), "pending")["text"] == loose_mentions.LOOSE_SENTENCES[0]
    # 没归项目、项目没挂根目录
    assert state(Snapshot(), "pending", project=None) is None
    db.execute("DELETE FROM project_material_roots")
    assert state(Snapshot(), "pending") is None


def test_seed_does_nothing_when_the_ai_layer_is_off(tmp_path):
    db, _root = world(tmp_path, "上周那版报价单再看一下")
    for off in (
        {"links_llm_enabled": False},
        {"links_llm_daily_calls": 0},
        {"links_enabled": False},
    ):
        assert loose_mentions.seed(db, settings(tmp_path, **off), NOW) == 0
    assert extraction(db) is None
    assert loose_mentions.seed(db, settings(tmp_path), NOW) == 1


# ---------------------------------------------------------------------- 接口（都是读，GET 不写库）


def test_endpoints_carry_the_loose_rows_and_answers_work(tmp_path):
    from fastapi.testclient import TestClient

    from meeting_workbench.config import Settings
    from meeting_workbench.db import Database
    from meeting_workbench.main import create_app

    from .test_copy_vocabulary import collect_copy, problems
    from .test_material_index import add_root
    from .test_tasks_api import FakeRelayClient, write_headers

    key = tmp_path / "api-key"
    key.write_text("sk-test", encoding="utf-8")
    cfg = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        lark_webhook_url="",
        llm_api_key_file=key,
        material_browse_root=tmp_path / "browse",
        links_enabled=True,
    )
    client = TestClient(create_app(cfg, FakeRelayClient()))
    headers = write_headers(client)
    db = Database(cfg.database_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    root_id = add_root(db, tmp_path / "云图AI")
    db.execute(
        "INSERT INTO material_index_state(root_id, state, stems_rev, stems_hash) VALUES (?, 'done', 1, 'h')",
        (root_id,),
    )
    first = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    second = add_file(db, root_id, "报价/报价单 v2.xlsx", day="2026-09-18")
    add_meeting(
        db, "m", ago=1, project_id="p", segments=talk("上周那版报价单再看一下", "那版报价单还要改")
    )
    done_with(
        db,
        "m",
        [
            phrase(at(0), "上周那版报价单再看一下", "报价单", phrase_text="上周那版报价单"),
            phrase(at(1), "那版报价单还要改", "报价单"),
        ],
    )
    resolve(db)
    [row] = loose_rows(db)
    graph_rev = rev(db)

    brief = client.get("/api/meetings/m/brief").json()
    [item] = brief["files"]
    assert (item["relation_id"], item["phrase"], item["via"], item["count"], item["first_ms"]) == (
        row["id"],
        "上周那版报价单",
        "stem",
        2,
        at(0),
    )
    assert brief["loose_state"] is None  # 抽完了：不写
    db.execute("UPDATE mention_extractions SET state = 'failed'")
    state = client.get("/api/meetings/m/brief").json()["loose_state"]
    assert state == {
        "kind": "stopped",
        "text": "这场会的 AI 整理没做成",
        "action": {"kind": "retry", "label": "现在重试"},
    }
    detail = client.get(f"/api/graph/files/{second}").json()
    [meeting] = detail["meetings"]
    assert (meeting["relation_id"], meeting["phrase"], meeting["via"], meeting["status"]) == (
        row["id"],
        "上周那版报价单",
        "stem",
        "active",
    )
    assert detail["active_meetings"] == 1
    preview = client.get(f"/api/materials/files/{second}/preview").json()
    assert [(item["relation_id"], item["phrase"], item["via"]) for item in preview["mentions"]] == [
        (row["id"], "上周那版报价单", "stem")
    ]
    graph = client.get("/api/graph/projects/p").json()
    [edge] = [edge for edge in graph["edges"] if edge["kind"] == "mentioned"]
    assert (
        edge["id"] == f"e:file:{second}:m"
        and edge["label"] == "会上说『上周那版报价单』等 2 处 · 00:20:00"
    )
    # GET 不写库
    assert rev(db) == graph_rev
    for payload in (brief, state, detail, preview, graph):
        assert [text for text in collect_copy(payload) if problems(text)] == []

    # ［不是这份文件］、过了撤销期从「你标过…」那一行 restore；［换成这份］
    answered = client.post(
        f"/api/relations/{row['id']}/answer", json={"answer": "no"}, headers=headers
    ).json()
    assert answered["relation"]["status"] == "rejected" and answered["undo_until"]
    rejected = client.get(f"/api/graph/files/{second}").json()["meetings"]
    assert [(item["status"], item["relation_id"]) for item in rejected] == [("rejected", row["id"])]
    restored = client.post(
        f"/api/relations/{row['id']}/answer", json={"answer": "restore"}, headers=headers
    )
    assert restored.json()["relation"]["status"] == "shown"
    wrong = client.post(
        f"/api/relations/{row['id']}/answer",
        json={"answer": "pick", "file_id": 9999},
        headers=headers,
    )
    assert (
        wrong.status_code == 422
        and wrong.json()["detail"] == "只能换成这个项目文件夹里同名的另一份文件"
    )
    picked = client.post(
        f"/api/relations/{row['id']}/answer",
        json={"answer": "pick", "file_id": first},
        headers=headers,
    ).json()
    assert picked["relation"]["file"]["id"] == first and picked["relation"]["status"] == "shown"
    assert (
        client.post(f"/api/relations/{row['id']}/undo", json={}, headers=headers).status_code == 200
    )
    assert loose_rows(db)[0]["file_id"] == second
