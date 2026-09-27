"""回测命令 backfill-projects --evaluate：藏起人工答案重判（v13 第一期 1b-4）。"""
import json

from meeting_workbench.cli import main as cli_main
from meeting_workbench.db import utc_now
from meeting_workbench.project_linking import ProjectLinker

from .test_project_linking import HIGH, llm_answers, make_db, make_project, make_term, seed_meeting


def _require_literal(db):
    row = db.query_one("SELECT value FROM app_state WHERE key='link_require_literal'")
    return row["value"] if row else None


def _verdicts(result):
    return {item["meeting_id"]: item["verdict"] for item in result["results"]}


def test_turning_off_a_cue_word_stops_the_wrong_auto_attribution(tmp_path):
    """① 点「以后不再用这个词判断」后，含这个词的会不再自动归到 A。"""
    db, settings = make_db(tmp_path, with_key=False)
    project_a = make_project(db, "云图科研用药")
    project_b = make_project(db, "数据中台")
    term_id = make_term(db, project_a, "灰度方案")
    seed_meeting(
        db, "vm-1", "周二评审", "# 摘要\n灰度方案先上一半，灰度方案的回滚预案下周定。",
        project_id=project_b, origin="manual",
    )
    linker = ProjectLinker(db, settings)

    before = linker.evaluate()
    assert _verdicts(before) == {"vm-1": "auto_wrong"}
    assert before["results"][0]["project_id"] == project_a

    db.execute("UPDATE glossary_terms SET is_cue=0 WHERE id=?", (term_id,))
    after = linker.evaluate()
    assert after["results"][0]["decision"] != "auto" or after["results"][0]["project_id"] != project_a
    assert after["counts"]["auto_wrong"] == 0


def test_adding_an_also_name_makes_the_meeting_land_in_the_right_project(tmp_path):
    """② 给 B 加一个叫法后，含这个叫法的会结果是 B。"""
    db, settings = make_db(tmp_path, with_key=False)
    make_project(db, "云图科研用药")
    project_b = make_project(db, "数据中台")
    seed_meeting(
        db, "vm-2", "周三评审", "# 摘要\n统一指标平台的口径先对齐，统一指标平台下月上线。",
        project_id=project_b, origin="manual",
    )
    linker = ProjectLinker(db, settings)
    assert _verdicts(linker.evaluate()) == {"vm-2": "unresolved"}

    db.execute(
        "UPDATE projects SET also_names=? WHERE id=?",
        (json.dumps([{"name": "统一指标平台", "source": "manual"}], ensure_ascii=False), project_b),
    )
    result = linker.evaluate()
    assert _verdicts(result) == {"vm-2": "auto_right"}
    assert result["results"][0]["project_id"] == project_b


def test_the_answer_is_hidden_from_the_prompt(tmp_path, monkeypatch):
    """回测时这场会自己不出现在项目画像的「最近人工归入的会」里；平时的提示词里有（③）。"""
    db, settings = make_db(tmp_path)
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "vm-3", "指标口径评审", project_id=project_b, origin="manual")
    prompts: list[str] = []
    llm_answers(monkeypatch, HIGH.format(name="数据中台"), prompts=prompts)
    linker = ProjectLinker(db, settings)

    assert "最近人工归入的会 指标口径评审" in linker._build_prompt(title="x", minutes="y")
    result = linker.evaluate()
    assert "指标口径评审）" not in prompts[0]
    assert _verdicts(result) == {"vm-3": "auto_right"}


def test_confirmed_ai_attributions_are_not_used_as_answers(tmp_path, monkeypatch):
    """先被 AI 归、后来只是确认成同一个项目的会不算答案；AI 归错后被改过的算。"""
    db, settings = make_db(tmp_path)
    project_a = make_project(db, "云图科研用药")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "vm-confirmed", "例会一", project_id=project_b, origin="manual")
    seed_meeting(db, "vm-corrected", "例会二", project_id=project_b, origin="manual")
    for meeting_id, picked in (("vm-confirmed", project_b), ("vm-corrected", project_a)):
        db.execute(
            """INSERT INTO project_links(meeting_id, minutes_version_id, status, project_id, method, created_at)
               VALUES (?, ?, 'done', ?, 'llm_high', ?)""",
            (meeting_id, f"mv-{meeting_id}", picked, utc_now()),
        )
    llm_answers(monkeypatch, HIGH.format(name="数据中台"))

    result = ProjectLinker(db, settings).evaluate()

    assert [item["meeting_id"] for item in result["results"]] == ["vm-corrected"]
    assert result["skipped_untrusted"] == 1


def test_two_confident_mistakes_turn_on_the_literal_requirement(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    make_project(db, "云图科研用药")
    project_b = make_project(db, "数据中台")
    for meeting_id in ("vm-4", "vm-5"):
        seed_meeting(db, meeting_id, f"周会 {meeting_id}", project_id=project_b, origin="manual")
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))
    linker = ProjectLinker(db, settings)

    report_only = linker.evaluate(apply=False)
    assert report_only["counts"]["llm_high_wrong"] == 2
    # 两场都没有「云图科研用药」自己的线索：要求字面线索就能拦下
    assert report_only["literal_guard"] == {"wrong_blocked": 2, "right_demoted": 0}
    assert _require_literal(db) is None

    applied = linker.evaluate()
    assert applied["require_literal_enabled"] is True
    assert _require_literal(db) == "1"

    # 打开之后再回测，模型高置信但没有字面线索的不再自动归
    again = linker.evaluate()
    assert again["counts"]["auto_wrong"] == 0
    assert again["require_literal_before"] is True


def test_evaluate_changes_no_meeting(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    make_project(db, "云图科研用药")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "vm-6", "周会", project_id=project_b, origin="manual")
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))

    ProjectLinker(db, settings).evaluate()

    row = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-6'")
    assert (row["project_id"], row["project_origin"]) == (project_b, "manual")
    assert db.query_one("SELECT COUNT(*) AS count FROM project_links")["count"] == 0


def test_cli_evaluate_prints_a_summary(tmp_path, monkeypatch, capsys):
    db, settings = make_db(tmp_path, with_key=False)
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "vm-7", "数据中台周会", project_id=project_b, origin="manual")
    for key, value in {
        "DATA_DIR": settings.data_dir,
        "DATABASE_PATH": settings.database_path,
        "ARCHIVE_ROOT": settings.archive_root,
        "STAGING_ROOT": settings.staging_root,
        "LLM_API_KEY_FILE": settings.llm_api_key_file,
        "SEMANTIC_ENABLED": "false",
    }.items():
        monkeypatch.setenv(f"MEETING_WORKBENCH_{key}", str(value))

    assert cli_main(["backfill-projects", "--evaluate", "--no-apply"]) == 0

    out = capsys.readouterr().out
    assert "数据中台周会" in out
    assert "共 1 场" in out
