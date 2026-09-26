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
