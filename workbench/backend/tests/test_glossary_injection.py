"""1d-2 按这场会挑词：快照里的项目线索、relay 回执入库、纪要体检。"""
import importlib.util
import json
from pathlib import Path

from meeting_workbench.db import Database, utc_now
from meeting_workbench.glossary import create_term, read_snapshot, rewrite_snapshot

from .test_tasks_api import make_client, write_headers

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_injection():
    """relay 用的挑词模块（glossary/injection.py，标准库 only），这里当跨进程契约来测。"""
    spec = importlib.util.spec_from_file_location(
        "glossary_injection_contract", REPO_ROOT / "glossary" / "injection.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_db(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    return db


def add_project(db, project_id, name, also=()):
    db.execute(
        """INSERT INTO projects (id, name, color, origin, also_names, created_at)
           VALUES (?, ?, '#667085', 'manual', ?, ?)""",
        (
            project_id,
            name,
            json.dumps([{"name": item, "source": "manual"} for item in also], ensure_ascii=False),
            utc_now(),
        ),
    )


# —— 快照里的项目线索 ——


def test_snapshot_exports_projects_with_recognition_cues(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图AI", also=["云图"])
    add_project(db, "p-empty", "数据中台")
    db.execute(
        """INSERT INTO project_material_roots (project_id, path, created_at)
           VALUES ('p-yt', '/Volumes/work/云图AI 资料', ?)""",
        (utc_now(),),
    )
    create_term(db, term="初审规则", aliases=["出审规则"], project_id="p-yt", scope="云图AI", is_cue=True)
    create_term(
        db, term="数理协会", aliases=["树立协会"], project_id="p-yt", scope="云图AI", is_cue=False
    )
    snapshot = tmp_path / "snap.json"
    rewrite_snapshot(db, snapshot)

    payload = read_snapshot(snapshot)
    assert payload["schema_version"] == 1
    projects = {item["id"]: item for item in payload["projects"]}
    assert projects["p-empty"] == {
        "id": "p-empty",
        "name": "数据中台",
        "also": [],
        "cues": [{"text": "数据中台", "kind": "name"}],
    }
    assert projects["p-yt"]["also"] == ["云图"]
    assert projects["p-yt"]["cues"] == [
        {"text": "云图AI", "kind": "name"},
        {"text": "云图", "kind": "also"},
        {"text": "云图AI 资料", "kind": "folder"},
        {"text": "初审规则", "kind": "term"},
        {"text": "出审规则", "kind": "term"},
    ]


def test_workbench_snapshot_drives_relay_selection(tmp_path):
    """工作台写的快照交给 relay 的挑词模块：认出项目、项目词在前、公共错写避让项目词。"""
    injection = load_injection()
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图AI", also=["云图"])
    add_project(db, "p-zt", "数据中台")
    create_term(db, term="数理协会", aliases=["树立协会"], project_id="p-yt", scope="云图AI")
    create_term(db, term="树立", project_id="p-yt", scope="云图AI")
    create_term(db, term="数理", aliases=["树立"])
    create_term(db, term="主数据", aliases=["珠数据"], project_id="p-zt", scope="数据中台")
    snapshot_path = tmp_path / "snap.json"
    rewrite_snapshot(db, snapshot_path)

    snapshot = injection.load_snapshot(snapshot_path)
    transcript = "云图AI 这边，树立协会下周回复；云图 的初审先不动。"
    receipt = injection.select_injection(snapshot, transcript)
    assert receipt["project"]["id"] == "p-yt"
    assert receipt["project"]["name"] == "云图AI"
    assert [entry["term"] for entry in receipt["terms"]] == ["数理协会", "树立", "数理"]
    assert receipt["dropped_aliases"] == [{"term": "数理", "alias": "树立"}]


def test_project_changes_rewrite_the_snapshot(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    snapshot = settings.data_dir / "glossary-snapshot.json"

    created = client.post("/api/projects", json={"name": "云图AI"}, headers=headers)
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    assert [item["name"] for item in read_snapshot(snapshot)["projects"]] == ["云图AI"]

    renamed = client.patch(
        f"/api/projects/{project_id}",
        json={"name": "云图智能", "also_names": ["云图"]},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    entry = read_snapshot(snapshot)["projects"][0]
    assert entry["name"] == "云图智能"
    assert "云图" in entry["also"]

    deleted = client.delete(
        f"/api/projects/{project_id}",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert deleted.status_code == 200, deleted.text
    assert read_snapshot(snapshot)["projects"] == []


# —— relay 回执入库 ——


def add_meeting(db, meeting_id, *, project_id=None, status="completed_unreviewed"):
    db.execute(
        """INSERT INTO meetings (id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES (?, '周会', '2026-09-26', ?, ?, ?, ?)""",
        (meeting_id, status, project_id, utc_now(), utc_now()),
    )


def add_minutes(db, meeting_id, version_id, markdown, *, kind="generated", job_id=None, attempt=None, version_no=1):
    db.execute(
        """INSERT INTO minutes_versions
               (id, meeting_id, version_no, markdown, html, kind, published,
                source_job_id, source_attempt, created_at)
           VALUES (?, ?, ?, ?, '', ?, 0, ?, ?, ?)""",
        (version_id, meeting_id, version_no, markdown, kind, job_id, attempt, utc_now()),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (version_id, meeting_id))


def add_artifact(db, meeting_id, path):
    import hashlib

    db.execute(
        """INSERT INTO artifacts (meeting_id, kind, role, source_root, path, sha256, size_bytes, created_at)
           VALUES (?, 'glossary_injection', 'source', 'archive', ?, ?, ?, ?)""",
        (meeting_id, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, utc_now()),
    )


def test_artifact_kind_recognises_the_receipt():
    from meeting_workbench.importer import artifact_kind

    assert artifact_kind(Path("/a/260926 周会/glossary-injection.json")) == "glossary_injection"
    assert artifact_kind(Path("/a/260926 周会/minutes-plan.json")) == "minutes_plan"


def test_receipts_are_ingested_once_and_matched_to_the_minutes_version(tmp_path):
    from meeting_workbench import glossary_checkup

    injection = load_injection()
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图AI")
    add_meeting(db, "vm-1")
    add_minutes(db, "vm-1", "mv-1", "# 纪要", job_id="job-1", attempt=2)
    snapshot = {
        "schema_version": 1,
        "terms": [
            {"term": "数理协会", "aliases": ["树立协会"], "scope": "云图AI", "category": "机构", "project_id": "p-yt"},
            {"term": "随访", "aliases": ["随方"], "scope": "通用", "category": "术语"},
        ],
        "projects": [{"id": "p-yt", "name": "云图AI", "also": [], "cues": [{"text": "云图AI", "kind": "name"}]}],
    }
    receipt = injection.select_injection(snapshot, "云图AI 的树立协会，云图AI 随方", None)
    receipt.update(job_id="job-1", attempt=2)
    attempt_dir = tmp_path / "attempt-2"
    attempt_dir.mkdir()
    add_artifact(db, "vm-1", injection.write_receipt(receipt, attempt_dir))
    broken = tmp_path / "broken" / "glossary-injection.json"
    broken.parent.mkdir()
    broken.write_text("{", encoding="utf-8")
    add_artifact(db, "vm-1", broken)

    assert glossary_checkup.ingest_receipts(db) == 1
    assert glossary_checkup.ingest_receipts(db) == 0

    with db.autocommit() as connection:
        minutes = dict(connection.execute("SELECT * FROM minutes_versions WHERE id='mv-1'").fetchone())
        found = glossary_checkup.receipt_for_minutes(connection, "vm-1", minutes)
        other = glossary_checkup.receipt_for_minutes(
            connection, "vm-1", {**minutes, "source_attempt": 1}
        )
    assert other is None
    summary = glossary_checkup.summarize_receipt(found)
    assert summary["project_id"] == "p-yt"
    assert summary["project_name"] == "云图AI"
    assert summary["project_source"] == "transcript"
    assert (summary["term_count"], summary["project_terms"], summary["public_terms"]) == (2, 1, 1)


# —— 纪要体检 ——


def add_transcript(db, meeting_id, lines):
    version_id = db.create_transcript_version(meeting_id, "funasr", published=True)
    db.replace_segments(
        version_id,
        meeting_id,
        [
            {
                "id": f"seg-{meeting_id}-{index}",
                "ordinal": index,
                "start_ms": index * 1000,
                "end_ms": index * 1000 + 900,
                "speaker_label": "SPEAKER_00",
                "text": text,
            }
            for index, text in enumerate(lines)
        ],
    )


def add_receipt(db, meeting_id, *, job_id, attempt, project_id, project_name, source="hint", cues=()):
    import hashlib

    payload = {
        "schema_version": 1,
        "project": {"id": project_id, "name": project_name, "source": source, "score": 3, "cues": list(cues)},
        "counts": {"project": 1, "public": 1},
        "terms": [{"term": "数理协会"}, {"term": "随访"}],
        "job_id": job_id,
        "attempt": attempt,
    }
    raw = json.dumps(payload, ensure_ascii=False)
    db.execute(
        """INSERT INTO meeting_glossary_receipts
               (meeting_id, job_id, attempt, sha256, project_id, project_name, project_source,
                term_count, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 2, ?, ?)""",
        (meeting_id, job_id, attempt, hashlib.sha256(raw.encode()).hexdigest(), project_id,
         project_name, source, raw, utc_now()),
    )


def seed_glossary(db):
    add_project(db, "p-yt", "云图AI")
    add_project(db, "p-zt", "数据中台")
    create_term(db, term="数理协会", aliases=["树立协会"], project_id="p-yt", scope="云图AI")
    create_term(db, term="云图AI", aliases=["云图"], project_id="p-yt", scope="云图AI", is_cue=False)
    create_term(db, term="主数据", aliases=["珠数据"], project_id="p-zt", scope="数据中台")
    create_term(db, term="随访", aliases=["随方"])
    create_term(db, term="儿保科", aliases=["儿宝科"], scope="儿科")


def test_dictionary_terms_take_project_and_public_only(tmp_path):
    from meeting_workbench import glossary_checkup

    db = make_db(tmp_path)
    seed_glossary(db)
    create_term(db, term="数理", aliases=["树立"])
    create_term(db, term="树立", project_id="p-yt", scope="云图AI")
    with db.autocommit() as connection:
        yt = glossary_checkup.dictionary_terms(connection, "p-yt")
        public = glossary_checkup.dictionary_terms(connection, None)
    assert [t["term"] for t in yt] == ["云图AI", "数理协会", "树立", "数理", "随访"]
    assert next(t for t in yt if t["term"] == "数理")["aliases"] == []
    assert [t["term"] for t in public] == ["数理", "随访"]
    assert next(t for t in public if t["term"] == "数理")["aliases"] == ["树立"]


def test_compute_hits_and_replacement_rules():
    from meeting_workbench import glossary_checkup

    terms = [
        {"term": "云图AI", "aliases": ["云图"], "also": [], "project_id": "p-yt"},
        {"term": "数理协会", "aliases": ["树立协会", "树立"], "also": [], "project_id": "p-yt"},
        {"term": "随访", "aliases": ["随方"], "also": ["回访"], "project_id": None},
    ]
    transcript = "云图的树立协会下周随方"
    minutes = "云图AI 项目：数理协会下周回复。云图 那边要树立协会的名单；随访照常。"
    hits = glossary_checkup.compute_hits(terms, transcript, minutes)
    assert [(h["wrong"], h["minutes_count"]) for h in hits["missed"]] == [("云图", 1), ("树立协会", 1)]
    assert [(h["wrong"], h["transcript_count"]) for h in hits["corrected"]] == [("随方", 1)]

    fixed, count = glossary_checkup.replace_missed(
        minutes, terms, [("云图", "云图AI"), ("树立", "数理协会"), ("树立协会", "数理协会")]
    )
    assert count == 2
    assert fixed == "云图AI 项目：数理协会下周回复。云图AI 那边要数理协会的名单；随访照常。"


def test_check_basis_follows_receipt_then_meeting_project_then_public(tmp_path):
    from meeting_workbench import glossary_checkup

    db = make_db(tmp_path)
    seed_glossary(db)
    add_meeting(db, "vm-1", project_id="p-zt")
    add_transcript(db, "vm-1", ["珠数据和树立协会"])
    add_minutes(db, "vm-1", "mv-1", "珠数据；树立协会", job_id="job-1", attempt=1)

    result = glossary_checkup.check_meeting(db, "vm-1")
    assert (result["basis"], result["project_id"]) == ("meeting", "p-zt")
    assert [h["wrong"] for h in result["missed"]] == ["珠数据"]

    add_receipt(db, "vm-1", job_id="job-1", attempt=1, project_id="p-yt", project_name="云图AI")
    stats = glossary_checkup.run_pending(db, object())
    assert stats["checked"] == 1
    view = glossary_checkup.meeting_glossary(db, "vm-1")
    assert view["basis"] == "receipt"
    assert view["project"]["name"] == "云图AI"
    assert view["meeting_project"]["name"] == "数据中台"
    assert view["mismatch"] is True
    assert view["receipt"]["term_count"] == 2
    assert [h["wrong"] for h in view["missed"]] == ["树立协会"]

    glossary_checkup.check_meeting(db, "vm-1", project_id="p-zt")
    add_minutes(db, "vm-1", "mv-2", "珠数据", kind="draft", job_id="job-1", attempt=1, version_no=2)
    glossary_checkup.run_pending(db, object())
    view = glossary_checkup.meeting_glossary(db, "vm-1")
    assert (view["basis"], view["project"]["id"], view["mismatch"]) == ("chosen", "p-zt", False)
    glossary_checkup.check_meeting(db, "vm-1", project_id=None)
    assert glossary_checkup.meeting_glossary(db, "vm-1")["basis"] == "receipt"

    db.execute("UPDATE meetings SET project_id=NULL WHERE id='vm-1'")
    db.execute("DELETE FROM meeting_glossary_receipts")
    glossary_checkup.check_meeting(db, "vm-1")
    view = glossary_checkup.meeting_glossary(db, "vm-1")
    assert (view["basis"], view["project"], view["mismatch"]) == ("public", None, False)


def _auto_setup(tmp_path, *, status="completed_unreviewed", with_receipt=True):
    from meeting_workbench.service import MeetingService

    db = make_db(tmp_path)
    seed_glossary(db)
    add_meeting(db, "vm-1", project_id="p-yt", status=status)
    add_transcript(db, "vm-1", ["树立协会下周随方"])
    add_minutes(db, "vm-1", "mv-1", "# 纪要\n树立协会下周回复，随访照常。", job_id="job-1", attempt=1)
    if with_receipt:
        add_receipt(db, "vm-1", job_id="job-1", attempt=1, project_id="p-yt", project_name="云图AI")
    return db, MeetingService(db)


def test_unreviewed_minutes_from_the_new_relay_are_fixed_automatically_and_can_be_undone(tmp_path):
    from meeting_workbench import glossary_checkup

    db, service = _auto_setup(tmp_path)
    changed = []
    stats = glossary_checkup.run_pending(db, service, on_minutes_changed=changed.append)
    assert (stats["checked"], stats["auto_applied"]) == (1, 1)
    assert changed == ["vm-1"]
    meeting = db.query_one("SELECT status, current_minutes_version_id FROM meetings WHERE id='vm-1'")
    assert meeting["status"] == "draft_modified"
    current = db.query_one("SELECT markdown, kind FROM minutes_versions WHERE id=?", (meeting["current_minutes_version_id"],))
    assert current["markdown"] == "# 纪要\n数理协会下周回复，随访照常。"
    assert current["kind"] == "draft"
    assert db.query_one("SELECT COUNT(*) AS n FROM glossary_suggestions")["n"] == 0
    view = glossary_checkup.meeting_glossary(db, "vm-1")
    assert view["missed"] == []
    assert [h["wrong"] for h in view["corrected"]] == ["树立协会", "随方"]
    assert view["applied"] == {"by": "auto", "count": 1, "at": view["applied"]["at"], "can_undo": True}

    assert glossary_checkup.run_pending(db, service)["auto_applied"] == 0

    glossary_checkup.undo_applied(db, service, "vm-1")
    meeting = db.query_one("SELECT current_minutes_version_id FROM meetings WHERE id='vm-1'")
    restored = db.query_one("SELECT markdown FROM minutes_versions WHERE id=?", (meeting["current_minutes_version_id"],))
    assert restored["markdown"] == "# 纪要\n树立协会下周回复，随访照常。"
    view = glossary_checkup.meeting_glossary(db, "vm-1")
    assert view["applied"] is None
    assert [h["wrong"] for h in view["missed"]] == ["树立协会"]
    assert glossary_checkup.run_pending(db, service)["auto_applied"] == 0


def test_no_automatic_fix_without_receipt_or_once_reviewed(tmp_path):
    from meeting_workbench import glossary_checkup

    for index, (status, with_receipt) in enumerate(
        [("completed_unreviewed", False), ("published", True), ("draft_modified", True)]
    ):
        db, service = _auto_setup(tmp_path / str(index), status=status, with_receipt=with_receipt)
        stats = glossary_checkup.run_pending(db, service)
        assert stats["auto_applied"] == 0, status
        view = glossary_checkup.meeting_glossary(db, "vm-1")
        assert [h["wrong"] for h in view["missed"]] == ["树立协会"]
        assert view["applied"] is None


def test_new_term_rechecks_but_never_auto_fixes_an_already_checked_version(tmp_path):
    from meeting_workbench import glossary_checkup

    db, service = _auto_setup(tmp_path)
    db.execute("DELETE FROM glossary_terms WHERE term='数理协会'")
    assert glossary_checkup.run_pending(db, service)["auto_applied"] == 0
    create_term(db, term="数理协会", aliases=["树立协会"], project_id="p-yt", scope="云图AI")
    stats = glossary_checkup.run_pending(db, service)
    assert (stats["checked"], stats["auto_applied"]) == (1, 0)
    assert [h["wrong"] for h in glossary_checkup.meeting_glossary(db, "vm-1")["missed"]] == ["树立协会"]


def test_meeting_glossary_api_check_apply_and_undo(tmp_path):
    client, settings = make_client(tmp_path)
    headers = {**write_headers(client), "Content-Type": "application/json"}
    db = Database(settings.database_path)
    seed_glossary(db)
    add_meeting(db, "vm-1", project_id="p-zt", status="draft_modified")
    add_transcript(db, "vm-1", ["树立协会和珠数据"])
    add_minutes(db, "vm-1", "mv-1", "树立协会和珠数据", kind="draft")

    assert client.get("/api/meetings/vm-1/glossary").json() == {"glossary": None}
    checked = client.post("/api/meetings/vm-1/glossary/check", json={}, headers=headers)
    assert checked.status_code == 200, checked.text
    assert [h["wrong"] for h in checked.json()["glossary"]["missed"]] == ["珠数据"]
    other = client.post("/api/meetings/vm-1/glossary/check", json={"project_id": "p-yt"}, headers=headers)
    glossary = other.json()["glossary"]
    assert (glossary["basis"], glossary["project"]["name"], glossary["mismatch"]) == ("chosen", "云图AI", True)
    assert [h["wrong"] for h in glossary["missed"]] == ["树立协会"]
    assert client.post(
        "/api/meetings/vm-1/glossary/check", json={"project_id": "p-gone"}, headers=headers
    ).status_code == 404

    stale = client.post("/api/meetings/vm-1/glossary/apply", json={"base_version_id": "mv-0"}, headers=headers)
    assert stale.status_code == 409
    applied = client.post("/api/meetings/vm-1/glossary/apply", json={"base_version_id": "mv-1"}, headers=headers)
    assert applied.status_code == 200, applied.text
    assert applied.json()["replaced"] == 1
    assert applied.json()["glossary"]["applied"]["by"] == "user"
    detail = client.get("/api/meetings/vm-1").json()
    assert detail["glossary"]["applied"]["can_undo"] is True
    current = next(
        v for v in detail["minutes_versions"] if v["id"] == detail["current_minutes_version_id"]
    )
    assert current["markdown"] == "数理协会和珠数据"

    undone = client.post("/api/meetings/vm-1/glossary/undo", json={}, headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["glossary"]["applied"] is None
    assert client.post("/api/meetings/vm-1/glossary/undo", json={}, headers=headers).status_code == 409


def test_relay_selected_project_counts_as_literal_evidence(tmp_path):
    from meeting_workbench.config import Settings
    from meeting_workbench.project_linking import ProjectLinker

    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        llm_api_key_file=tmp_path / "missing-key",
    )
    db = Database(settings.database_path)
    db.initialize()
    add_project(db, "p-yt", "云图智能")
    add_meeting(db, "vm-1")
    add_minutes(db, "vm-1", "mv-1", "# 纪要\n聊了进度。", job_id="job-1", attempt=1)
    add_receipt(
        db, "vm-1", job_id="job-1", attempt=1, project_id="p-yt", project_name="云图智能",
        source="transcript", cues=["云图AI"],
    )
    result = ProjectLinker(db, settings)._classify(meeting_id="vm-1", title="周会", minutes_markdown="聊了进度。")
    assert result["literal"] == {"p-yt": 1}
    entry = next(e for e in result["evidence"] if e.get("source") == "injection")
    assert (entry["cue"], entry["count"], entry["where"]["transcript"]) == ("云图AI", 3, 3)
    assert result["decision"] == "needs_review"

    db.execute("UPDATE meeting_glossary_receipts SET project_source='hint'")
    hinted = ProjectLinker(db, settings)._classify(meeting_id="vm-1", title="周会", minutes_markdown="聊了进度。")
    assert hinted["literal"] == {}
