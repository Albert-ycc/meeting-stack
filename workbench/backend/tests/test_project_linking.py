"""会议自动归属项目测试（260905 新增；v13 第一期 1b 按字面线索 + LLM 的新规则重写）。"""
import json
import uuid

from meeting_workbench import project_linking as project_linking_module
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.project_profile import build_cue_table, norm_key


def make_db(tmp_path, *, with_key: bool = True) -> tuple[Database, Settings]:
    key_file = tmp_path / "api-key"
    if with_key:
        key_file.write_text("test-key", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        llm_api_key_file=key_file,
    )
    db = Database(settings.database_path)
    db.initialize()
    return db, settings


def seed_meeting(
    db: Database,
    meeting_id: str,
    title: str,
    markdown: str = "# 摘要\n拍板做 X。",
    *,
    segments: list[tuple[int, str]] | None = None,
    project_id: str | None = None,
    origin: str | None = None,
) -> str:
    now = utc_now()
    db.execute(
        "INSERT INTO meetings(id, title, status, project_id, project_origin, created_at, updated_at) "
        "VALUES (?, ?, 'completed_unreviewed', ?, ?, ?, ?)",
        (meeting_id, title, project_id, origin, now, now),
    )
    version_id = f"mv-{meeting_id}"
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES (?, ?, 1, ?, '<p></p>', 'generated', 1, ?)""",
        (version_id, meeting_id, markdown, now),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (version_id, meeting_id))
    if segments:
        transcript = db.create_transcript_version(meeting_id, "funasr", published=True)
        db.replace_segments(
            transcript,
            meeting_id,
            [
                {
                    "id": f"seg-{meeting_id}-{index}",
                    "ordinal": index,
                    "start_ms": start_ms,
                    "end_ms": start_ms + 1000,
                    "speaker_label": "SPEAKER_00",
                    "text": text,
                }
                for index, (start_ms, text) in enumerate(segments)
            ],
        )
    return version_id


def make_project(db: Database, name: str, *, also: list[str] | None = None) -> str:
    project_id = f"project-{uuid.uuid4().hex[:12]}"
    db.execute(
        "INSERT INTO projects(id, name, color, origin, also_names, created_at) "
        "VALUES (?, ?, '#2c8d83', 'manual', ?, ?)",
        (
            project_id,
            name,
            json.dumps([{"name": value, "source": "manual"} for value in also or []]),
            utc_now(),
        ),
    )
    return project_id


def make_term(db: Database, project_id: str, term: str, *, is_cue: int = 1) -> str:
    term_id = f"gt-{uuid.uuid4().hex[:8]}"
    db.execute(
        """INSERT INTO glossary_terms(id, term, scope, project_id, is_cue, created_at, updated_at)
           VALUES (?, ?, 'x', ?, ?, ?, ?)""",
        (term_id, term, project_id, is_cue, utc_now(), utc_now()),
    )
    return term_id


def make_task(db: Database, meeting_id: str, project_id: str | None, status: str = "confirmed") -> None:
    now = utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, detail, status, origin, assignee, meeting_id, project_id,
            status_changed_at, created_at, updated_at)
           VALUES (?, '任务', '', ?, 'ai', 'ai', ?, ?, ?, ?, ?)""",
        (f"task-{uuid.uuid4().hex}", status, meeting_id, project_id, now, now, now),
    )


def llm_answers(monkeypatch, *answers: str, prompts: list[str] | None = None):
    queue = list(answers)

    def fake(settings, prompt, *, system):
        if prompts is not None:
            prompts.append(prompt)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(project_linking_module, "call_llm", fake)


HIGH = '{{"project_match":"{name}","confidence":"high","reason":"标题即项目名"}}'
LOW = '{"project_match":null,"confidence":"low","reason":"内容笼统"}'


def link_row(db: Database, meeting_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM project_links WHERE meeting_id=? ORDER BY id DESC LIMIT 1", (meeting_id,)
    )
    for key in ("evidence_json", "candidates_json"):
        row[key] = json.loads(row[key]) if row[key] else None
    return row


def meeting_row(db: Database, meeting_id: str) -> dict:
    return db.query_one(
        "SELECT project_id, project_origin FROM meetings WHERE id=?", (meeting_id,)
    )


def test_llm_high_without_rival_cues_is_auto_assigned_with_evidence(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI")
    make_term(db, project_id, "初审规则")
    seed_meeting(
        db, "vm-1", "云图AI 周会", segments=[(1000, "先过一下初审规则"), (61000, "初审规则第二版")]
    )
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))

    stats = ProjectLinker(db, settings).link_pending()

    assert (stats["started"], stats["linked"]) == (1, 1)
    assert meeting_row(db, "vm-1") == {"project_id": project_id, "project_origin": "ai"}
    link = link_row(db, "vm-1")
    assert (link["status"], link["method"], link["project_id"]) == ("done", "llm_high", project_id)
    literal = [item for item in link["evidence_json"] if item["kind"] == "literal"]
    term = next(item for item in literal if item["cue"] == "初审规则")
    assert term["count"] == 2
    assert term["anchors_ms"] == [1000, 61000]
    assert term["source"] == "term"
    assert link["evidence_json"][-1] == {
        "kind": "llm", "project_id": project_id, "confidence": "high", "reason": "标题即项目名",
    }


def test_llm_high_is_overruled_by_a_stronger_rival_and_goes_to_review(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(
        db, "vm-2", "周会", "数据中台 口径；数据中台 指标；数据中台 排期",
    )
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))

    stats = ProjectLinker(db, settings).link_pending()

    assert stats["needs_review"] == 1
    assert meeting_row(db, "vm-2") == {"project_id": None, "project_origin": None}
    link = link_row(db, "vm-2")
    assert link["status"] == "needs_review"
    assert [item["project_id"] for item in link["candidates_json"]] == [project_a, project_b]
    assert link["candidates_json"][0]["llm"] is True
    assert link["candidates_json"][1]["count"] == 3


def test_low_confidence_never_auto_assigns_even_when_all_tasks_point_to_one_project(
    tmp_path, monkeypatch
):
    db, settings = make_db(tmp_path)
    project_a = make_project(db, "A项目")
    seed_meeting(db, "vm-3", "跨项目周会")
    make_task(db, "vm-3", project_a)
    make_task(db, "vm-3", project_a, status="in_progress")
    llm_answers(monkeypatch, LOW)

    stats = ProjectLinker(db, settings).link_pending()

    assert stats["linked"] == 0
    assert meeting_row(db, "vm-3") == {"project_id": None, "project_origin": None}
    assert link_row(db, "vm-3")["status"] in ("needs_review", "unresolved")


def test_without_key_only_a_single_strong_literal_project_is_auto_assigned(tmp_path):
    db, settings = make_db(tmp_path, with_key=False)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "vm-4", "云图AI 评审", "云图AI 的标注平台")
    seed_meeting(db, "vm-5", "云图AI 和数据中台对齐", "云图AI；数据中台")

    stats = ProjectLinker(db, settings).link_pending()

    assert meeting_row(db, "vm-4") == {"project_id": project_a, "project_origin": "ai"}
    assert link_row(db, "vm-4")["method"] == "literal"
    assert meeting_row(db, "vm-5") == {"project_id": None, "project_origin": None}
    review = link_row(db, "vm-5")
    assert review["status"] == "needs_review"
    assert {item["project_id"] for item in review["candidates_json"]} == {project_a, project_b}
    assert stats["linked"] == 1 and stats["needs_review"] == 1


def test_two_character_also_name_counts_only_when_repeated(tmp_path):
    db, settings = make_db(tmp_path, with_key=False)
    project_id = make_project(db, "云图科研用药", also=["云图"])
    seed_meeting(db, "vm-6", "周会", "提到一次云图")
    seed_meeting(db, "vm-7", "周会", "云图的排期；云图的验收")

    ProjectLinker(db, settings).link_pending()

    assert link_row(db, "vm-6")["status"] == "unresolved"
    assert meeting_row(db, "vm-7") == {"project_id": project_id, "project_origin": "ai"}


def test_no_key_and_no_signal_is_unresolved_then_reopened_once_key_exists(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path, with_key=False)
    seed_meeting(db, "vm-8", "无从判断的会")
    linker = ProjectLinker(db, settings)

    stats = linker.link_pending()

    assert stats["unresolved"] == 1
    link = link_row(db, "vm-8")
    assert (link["status"], link["method"]) == ("unresolved", "no_llm")
    assert linker.link_pending()["started"] == 0

    (tmp_path / "api-key").write_text("test-key", encoding="utf-8")
    llm_answers(monkeypatch, LOW)
    linker.seed()
    assert link_row(db, "vm-8")["status"] == "pending"


def test_llm_failure_retries_before_falling_back_to_literal(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI")
    seed_meeting(db, "vm-9", "云图AI 评审", "云图AI 的标注平台")
    llm_answers(monkeypatch, RuntimeError("LLM 调用失败：timeout"))
    linker = ProjectLinker(db, settings)

    first = linker.link_pending()

    assert first["retry"] == 1
    assert meeting_row(db, "vm-9") == {"project_id": None, "project_origin": None}
    link = link_row(db, "vm-9")
    assert (link["status"], link["attempts"]) == ("pending", 1)

    linker.link_pending()
    linker.link_pending()

    assert meeting_row(db, "vm-9") == {"project_id": project_id, "project_origin": "ai"}
    assert link_row(db, "vm-9")["method"] == "literal"


def test_require_literal_switch_holds_back_llm_only_answers(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI")
    seed_meeting(db, "vm-10", "周会", "讨论了标注效率")
    db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES ('link_require_literal', '1', ?)",
        (utc_now(),),
    )
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))

    ProjectLinker(db, settings).link_pending()

    link = link_row(db, "vm-10")
    assert link["status"] == "needs_review"
    assert link["candidates_json"][0]["project_id"] == project_id


def test_llm_new_project_name_is_kept_unless_already_decided(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    make_project(db, "云图AI")
    seed_meeting(db, "vm-11", "看板需求沟通")
    seed_meeting(db, "vm-12", "看板需求沟通 2")
    db.execute(
        "INSERT INTO name_decisions(norm_key, name, decision, decided_at) VALUES (?, ?, 'ignored', ?)",
        (norm_key("市场周报"), "市场周报", utc_now()),
    )
    llm_answers(
        monkeypatch,
        '{"project_match":null,"confidence":"low","new_project_name":"云图看板","reason":"新主题"}',
        '{"project_match":null,"confidence":"low","new_project_name":"市场周报项目","reason":"x"}',
    )

    ProjectLinker(db, settings).link_pending()

    assert link_row(db, "vm-11")["new_project_name"] == "云图看板"
    assert link_row(db, "vm-12")["new_project_name"] is None


def test_folder_names_seen_in_other_projects_are_not_cues(tmp_path):
    db, settings = make_db(tmp_path, with_key=False)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    project_c = make_project(db, "智慧园区")
    for project_id, folder in ((project_a, "标注平台"), (project_a, "云图资料库")):
        db.execute(
            "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
            (project_id, f"/Volumes/资料盘/{folder}", utc_now()),
        )
    seed_meeting(db, "vm-b", "中台周会", "标注平台对接", project_id=project_b, origin="manual")
    seed_meeting(db, "vm-c", "园区周会", "标注平台选型", project_id=project_c, origin="manual")

    with db.autocommit() as connection:
        cues = {cue.text for cue in build_cue_table(connection)[project_a]}

    assert "云图资料库" in cues
    assert "标注平台" not in cues


def test_prompt_describes_each_project_profile(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药", also=["云图"])
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, "/Volumes/资料盘/项目/云图科研", utc_now()),
    )
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, created_at, updated_at)
           VALUES ('r-1', ?, '登录改版', 'P1', ?, ?)""",
        (project_id, utc_now(), utc_now()),
    )
    seed_meeting(db, "vm-old", "随访方案评审", project_id=project_id, origin="manual")
    seed_meeting(db, "vm-13", "周会")
    prompts: list[str] = []
    llm_answers(monkeypatch, LOW, prompts=prompts)

    ProjectLinker(db, settings).link_pending()

    assert (
        "- 云图科研用药（又称 云图；文件夹 云图科研；在做的需求 登录改版；"
        "最近人工归入的会 随访方案评审）"
    ) in prompts[0]
    assert "任务的项目分布" not in prompts[0]


def test_manual_assignment_never_reseeded_or_overwritten(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-14", "云图周会", project_id=project_id, origin="manual")
    llm_answers(monkeypatch, HIGH.format(name="云图"))

    linker = ProjectLinker(db, settings)
    assert linker.seed() == 0
    assert linker.link_pending()["started"] == 0
    assert meeting_row(db, "vm-14") == {"project_id": project_id, "project_origin": "manual"}


def test_unresolved_same_minutes_version_not_reseeded_new_version_reseeded(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    seed_meeting(db, "vm-15", "无法判断的会")
    llm_answers(monkeypatch, LOW)
    linker = ProjectLinker(db, settings)
    linker.link_pending()
    assert link_row(db, "vm-15")["status"] == "unresolved"

    assert linker.seed() == 0

    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-vm-15-v2', 'vm-15', 2, '# 新版摘要', '<p></p>', 'generated', 1, ?)""",
        (utc_now(),),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-vm-15-v2' WHERE id='vm-15'")
    assert linker.seed() == 1


def test_backfill_dry_run_does_not_write(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-16", "云图周会")
    llm_answers(monkeypatch, HIGH.format(name="云图"))

    result = ProjectLinker(db, settings).backfill(dry_run=True)

    assert result["dry_run"] is True
    [item] = result["results"]
    assert (item["decision"], item["project_id"], item["method"]) == ("auto", project_id, "llm_high")
    assert meeting_row(db, "vm-16")["project_id"] is None
    assert db.query_one("SELECT COUNT(*) AS n FROM project_links")["n"] == 0


def test_backfill_writes_and_summarizes(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-17", "云图周会")
    llm_answers(monkeypatch, HIGH.format(name="云图"))

    result = ProjectLinker(db, settings).backfill(dry_run=False)

    assert result["method_counts"] == {"llm_high": 1}
    assert (result["needs_review"], result["unresolved"]) == (0, 0)
    assert meeting_row(db, "vm-17") == {"project_id": project_id, "project_origin": "ai"}


def test_norm_key_folds_spacing_case_and_suffixes():
    assert norm_key("云图 AI 项目") == norm_key("云图ai") == "云图ai"
    assert norm_key("智慧园区二期") == "智慧园区"
    assert norm_key("CRM v2") == "crm"
    assert norm_key("项目") == "项目"
