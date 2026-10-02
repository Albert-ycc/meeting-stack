"""放行音频的目录：归档根、中转产物根、上传落盘的目录（数据目录下的 uploads）。
数据目录里还有数据库、备份、波形缓存，不能整个放行，不然只剩 kind='audio' 和扩展名在挡。"""

import base64

import pytest

from meeting_workbench.db import Database, utc_now

from .test_jobs_api import make_client, write_headers

# 都在数据目录里、都不在 uploads 里（相对数据目录）
OUTSIDE_UPLOADS = [
    pytest.param("backups/meeting.m4a", id="备份目录"),
    pytest.param("waveform-peaks/meeting.m4a", id="波形缓存目录"),
    pytest.param("meeting.m4a", id="数据目录根"),
    # 名字以 uploads 开头的兄弟目录不是 uploads
    pytest.param("uploads-other/meeting.m4a", id="名字相近的兄弟目录"),
    pytest.param("workbench-copy.sqlite3", id="数据库文件"),
]


def audio_artifact(settings, path) -> int:
    """直接往库里写一行指向 path 的音频记录（kind='audio'），模拟库里的记录被改过。"""
    db = Database(settings.database_path)
    db.execute(
        "INSERT OR IGNORE INTO meetings(id, title, status) "
        "VALUES ('vm-roots', 'Roots', 'completed_unreviewed')"
    )
    return db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('vm-roots', 'audio', 'source', 'archive', ?, 10, 0, ?)""",
        (str(path), utc_now()),
    )


def write(path, content=b"0123456789"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("relative", OUTSIDE_UPLOADS)
def test_media_does_not_serve_files_in_the_data_dir_outside_uploads(tmp_path, relative):
    client, _ = make_client(tmp_path)
    settings = client.app.state.settings
    artifact_id = audio_artifact(settings, write(settings.data_dir / relative))

    assert client.get(f"/api/media/{artifact_id}").status_code == 404
    assert client.get(f"/api/media/{artifact_id}/peaks").status_code == 404


@pytest.mark.parametrize("relative", OUTSIDE_UPLOADS)
def test_manual_enqueue_rejects_files_in_the_data_dir_outside_uploads(tmp_path, relative):
    client, relay = make_client(tmp_path)
    target = write(client.app.state.settings.data_dir / relative)

    response = client.post(
        "/api/jobs/enqueue", json={"audio_path": str(target)}, headers=write_headers(client)
    )

    assert response.status_code == 400
    assert relay.enqueued == []


def test_a_symlink_in_uploads_cannot_lead_to_the_database(tmp_path):
    client, relay = make_client(tmp_path)
    settings = client.app.state.settings
    secret = write(settings.data_dir / "workbench-copy.sqlite3", b"SQLite format 3\0")
    link = settings.data_dir / "uploads" / "innocent.m4a"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    artifact_id = audio_artifact(settings, link)

    assert client.get(f"/api/media/{artifact_id}").status_code == 404
    refused = client.post(
        "/api/jobs/enqueue", json={"audio_path": str(link)}, headers=write_headers(client)
    )
    assert refused.status_code == 400 and relay.enqueued == []


@pytest.mark.parametrize("root", ["archive", "staging", "data/uploads"])
def test_the_three_managed_roots_still_work(tmp_path, root):
    client, relay = make_client(tmp_path)
    settings = client.app.state.settings
    audio = write(tmp_path / root / "meeting" / "audio.m4a")
    artifact_id = audio_artifact(settings, audio)

    media = client.get(f"/api/media/{artifact_id}", headers={"Range": "bytes=2-5"})
    enqueue = client.post(
        "/api/jobs/enqueue", json={"audio_path": str(audio)}, headers=write_headers(client)
    )

    assert media.status_code == 206 and media.content == b"2345"
    assert enqueue.status_code == 200
    assert relay.enqueued == [str(audio.resolve())]


def test_an_uploaded_file_can_be_played_and_enqueued_again_by_path(tmp_path):
    """上传落盘的目录和放行的目录是同一个：刚上传完的文件，库里有它的记录时能播，手填路径也能再入队。"""
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    settings = client.app.state.settings
    payload = b"audio-data"
    upload_id = client.post(
        "/api/uploads/start",
        json={"filename": "meeting.m4a", "size_bytes": len(payload)},
        headers=headers,
    ).json()["upload_id"]
    client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        json={"content_base64": base64.b64encode(payload).decode()},
        headers=headers,
    )
    done = client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers).json()

    assert done["path"].startswith(str(settings.data_dir / "uploads") + "/")
    artifact_id = audio_artifact(settings, done["path"])
    assert client.get(f"/api/media/{artifact_id}").content == payload
    again = client.post("/api/jobs/enqueue", json={"audio_path": done["path"]}, headers=headers)
    assert again.status_code == 200
    assert relay.enqueued[-1] == done["path"]
