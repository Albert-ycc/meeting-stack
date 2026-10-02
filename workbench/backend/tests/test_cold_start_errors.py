"""冷启动弱归属复评：固定抛错的条目抛满 REEVAL_MAX_ERRORS 次后按「保持原样」收口，cold_start_done 才写得上。"""

from meeting_workbench import cold_start
from meeting_workbench.project_linking import ProjectLinker

from .test_cold_start import _app_state, _link, _weak
from .test_project_linking import HIGH, llm_answers, make_db, make_project, seed_meeting


def _broken(linker, monkeypatch, broken):
    """让 broken 里的会每次都抛错，别的照常判。"""
    classify = linker._classify
    calls: list[str] = []

    def fails_for_broken(**kwargs):
        calls.append(kwargs["meeting_id"])
        if kwargs["meeting_id"] in broken:
            raise RuntimeError("坏数据")
        return classify(**kwargs)

    monkeypatch.setattr(linker, "_classify", fails_for_broken)
    return calls


def test_a_link_that_always_fails_is_kept_after_the_last_allowed_error(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    seed_meeting(db, "vm-0", "云图科研用药周会")
    _weak(db, "vm-0", project_id)
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))
    linker = ProjectLinker(db, settings)
    _broken(linker, monkeypatch, {"vm-0"})

    for failed in range(1, cold_start.REEVAL_MAX_ERRORS):
        stats = cold_start.run(db, settings, linker)
        # 没抛满：下一轮照样重试，复评还是待做，done 写不上
        link = _link(db, "vm-0")
        assert (link["method"], link["attempts"]) == ("semantic", failed)
        assert stats["done"] is False and _app_state(db, "cold_start_done") is None

    stats = cold_start.run(db, settings, linker)

    link = _link(db, "vm-0")
    assert (link["status"], link["method"], link["attempts"]) == (
        "done",
        "reeval_kept",
        cold_start.REEVAL_MAX_ERRORS,
    )
    assert stats["done"] is True
    assert _app_state(db, "reeval_weak_ai") == "done"
    assert _app_state(db, "cold_start_done") == "1"
    # 会议归属原样不动
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-0'")
    assert (meeting["project_id"], meeting["project_origin"]) == (project_id, "ai")


def test_five_failing_links_among_normal_ones_all_close_and_done_is_written(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    broken = {f"vm-{index}" for index in range(cold_start.REEVAL_PER_ROUND)}
    for index in range(cold_start.REEVAL_PER_ROUND + 4):
        seed_meeting(db, f"vm-{index}", "云图科研用药周会")
        _weak(db, f"vm-{index}", project_id)
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))
    linker = ProjectLinker(db, settings)
    _broken(linker, monkeypatch, broken)

    rounds = 0
    while not cold_start.run(db, settings, linker)["done"]:
        rounds += 1
        assert rounds < 10, "固定抛错的条目一直占着名额，复评收不了口"

    for index in range(cold_start.REEVAL_PER_ROUND + 4):
        expected = "reeval_kept" if f"vm-{index}" in broken else "llm_high"
        assert _link(db, f"vm-{index}")["method"] == expected
    assert _app_state(db, "cold_start_done") == "1"


def test_errors_below_the_limit_are_retried_and_a_later_success_counts(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    seed_meeting(db, "vm-0", "云图科研用药周会")
    _weak(db, "vm-0", project_id)
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))
    linker = ProjectLinker(db, settings)
    classify = linker._classify
    errors = {"left": cold_start.REEVAL_MAX_ERRORS - 1}

    def flaky(**kwargs):
        if errors["left"] > 0:
            errors["left"] -= 1
            raise RuntimeError("偶发错误")
        return classify(**kwargs)

    monkeypatch.setattr(linker, "_classify", flaky)

    for _ in range(cold_start.REEVAL_MAX_ERRORS - 1):
        assert cold_start.run(db, settings, linker)["done"] is False
    assert _link(db, "vm-0")["method"] == "semantic"
    assert cold_start.run(db, settings, linker)["done"] is True

    link = _link(db, "vm-0")
    assert (link["status"], link["method"]) == ("done", "llm_high")
    assert _app_state(db, "cold_start_done") == "1"


def test_kept_after_errors_is_the_same_state_as_kept_without_minutes(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    for meeting_id in ("vm-broken", "vm-bare"):
        seed_meeting(db, meeting_id, "云图科研用药周会")
        _weak(db, meeting_id, project_id)
    db.execute("UPDATE meetings SET current_minutes_version_id=NULL WHERE id='vm-bare'")
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))
    linker = ProjectLinker(db, settings)
    _broken(linker, monkeypatch, {"vm-broken"})

    for _ in range(cold_start.REEVAL_MAX_ERRORS):
        cold_start.run(db, settings, linker)

    broken, bare = _link(db, "vm-broken"), _link(db, "vm-bare")
    assert (broken["status"], broken["method"]) == (bare["status"], bare["method"])
    assert (bare["status"], bare["method"]) == ("done", "reeval_kept")
    # 都不再算进待复评，也都没有凭空多出证据或候选
    assert broken["evidence_json"] is None and broken["candidates_json"] is None


def test_the_limit_is_a_named_constant():
    assert cold_start.REEVAL_MAX_ERRORS == 3
