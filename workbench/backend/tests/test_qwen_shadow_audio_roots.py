"""影子转写（Qwen）取音频：路径来自库里 kind='audio' 的记录，放行的根和播放、手填路径入队是同一套
（Settings.audio_roots：归档根、中转产物根、上传落盘的 uploads）。库里的记录被改成指向数据目录里的
数据库、备份时，不能去算它的哈希，更不能把它交给 Qwen 程序。"""

import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting_workbench.main import create_app
from meeting_workbench.qwen_shadow import QwenShadowError

from .test_qwen_shadow_service import Relay
from .test_qwen_shadow_service import setup as make_service

# 都在数据目录里、都不在 uploads 里（相对数据目录）；第一项是测试用的数据库文件本身
OUTSIDE_UPLOADS = [
    pytest.param("db.sqlite3", id="数据库文件"),
    pytest.param("backups/meeting.m4a", id="备份目录"),
    pytest.param("waveform-peaks/meeting.m4a", id="波形缓存目录"),
    pytest.param("meeting.m4a", id="数据目录根"),
    # 名字以 uploads 开头的兄弟目录不是 uploads
    pytest.param("uploads-other/meeting.m4a", id="名字相近的兄弟目录"),
]


def point_audio_at(db, path) -> None:
    db.execute(
        "UPDATE artifacts SET path=? WHERE meeting_id='vm-qwen' AND kind='audio'", (str(path),)
    )


def put(path, content=b"not-audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
    return path


@pytest.mark.parametrize("relative", OUTSIDE_UPLOADS)
def test_request_refuses_audio_records_pointing_into_the_data_dir_outside_uploads(
    tmp_path, monkeypatch, relative
):
    settings, db, _audio, _version, service = make_service(tmp_path)
    point_audio_at(db, put(settings.data_dir / relative))
    hashed = []
    monkeypatch.setattr(
        "meeting_workbench.qwen_shadow._sha256", lambda path: hashed.append(path) or "unused"
    )

    with pytest.raises(QwenShadowError, match="audio_outside_allowed_roots"):
        service.request("vm-qwen")

    # 内容一个字节都没读（没算哈希），也没有留下任何一条转写记录
    assert hashed == []
    assert db.query_all("SELECT id FROM asr_shadow_runs") == []


def test_a_queued_run_whose_audio_record_was_redirected_never_reaches_the_qwen_binary(
    tmp_path, monkeypatch
):
    """排上队之后记录才被改：这时新位置的内容和排队时记下的哈希一致（拷贝了同一份字节），
    哈希校验拦不住，只有放行根能拦住。"""
    settings, db, audio, _version, service = make_service(tmp_path)
    started = []
    monkeypatch.setattr(
        "meeting_workbench.qwen_shadow.subprocess.run",
        lambda command, **_kwargs: (
            started.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    run = service.request("vm-qwen")
    copy = put(settings.data_dir / "backups" / "copy.m4a", audio.read_bytes())
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == run["audio_sha256"]
    point_audio_at(db, copy)

    assert service.run_once() is True

    saved = db.query_one("SELECT state, error FROM asr_shadow_runs WHERE id=?", (run["id"],))
    assert (saved["state"], saved["error"]) == ("failed", "Qwen 离线转写失败")
    assert started == []


def test_a_symlinked_directory_inside_uploads_cannot_lead_to_the_database(tmp_path):
    settings, db, _audio, _version, service = make_service(tmp_path)
    link = settings.uploads_dir / "innocent"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(settings.data_dir, target_is_directory=True)
    point_audio_at(db, link / "db.sqlite3")

    with pytest.raises(QwenShadowError, match="audio_outside_allowed_roots"):
        service.request("vm-qwen")

    assert db.query_all("SELECT id FROM asr_shadow_runs") == []


def test_api_answers_409_and_creates_no_run_for_a_redirected_audio_record(tmp_path):
    settings, db, _audio, _version, _service = make_service(tmp_path, binary=False)
    point_audio_at(db, settings.database_path)
    client = TestClient(create_app(settings, Relay()))
    token = client.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": token, "Origin": "http://testserver"}

    response = client.post("/api/meetings/vm-qwen/asr-shadow/qwen", json={}, headers=headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "audio_outside_allowed_roots"}
    assert db.query_all("SELECT id FROM asr_shadow_runs") == []


@pytest.mark.parametrize("root", ["archive_root", "staging_root", "uploads_dir"])
def test_the_three_managed_roots_still_get_a_run(tmp_path, root):
    settings, db, _audio, _version, service = make_service(tmp_path)
    audio = put(getattr(settings, root) / "meeting" / "audio.m4a", b"audio-in-" + root.encode())
    point_audio_at(db, audio)

    run = service.request("vm-qwen")

    assert run["state"] == "queued"
    assert run["audio_sha256"] == hashlib.sha256(audio.read_bytes()).hexdigest()


def test_settings_in_audio_roots_is_the_one_rule_for_every_caller(tmp_path):
    settings, *_ = make_service(tmp_path)
    inside = [
        settings.archive_root / "a.m4a",
        settings.staging_root / "a.m4a",
        settings.uploads_dir / "a.m4a",
    ]
    outside = [
        settings.data_dir / "a.m4a",
        settings.data_dir / "backups" / "a.m4a",
        settings.data_dir / "uploads-other" / "a.m4a",
        settings.database_path,
        tmp_path / "elsewhere" / "a.m4a",
    ]

    assert all(settings.in_audio_roots(path.resolve()) for path in inside)
    assert not any(settings.in_audio_roots(path.resolve()) for path in outside)
