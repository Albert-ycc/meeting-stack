"""跨站读取：任何网页都能让浏览器往 127.0.0.1:8765 发 GET（图片、fetch、iframe……）。
读不到返回内容，但 /api/media/N/peaks 会真的起 ffmpeg，是盲打本机资源。浏览器发这类请求时带
Sec-Fetch-* 头，服务据此拒绝；整页导航（飞书卡片、书签）、同源页面、curl 这类不带头的客户端不受影响。"""

import pytest
from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.main import create_app


def make_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    return TestClient(create_app(settings)), settings


def seed_audio(settings) -> int:
    audio = settings.archive_root / "meeting" / "audio.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"0123456789")
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-cross', 'Cross', 'completed_unreviewed')"
    )
    return db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('vm-cross', 'audio', 'source', 'archive', ?, 10, ?, ?)""",
        (str(audio), audio.stat().st_mtime_ns, utc_now()),
    )


def fetch_headers(site: str, mode: str, dest: str) -> dict[str, str]:
    return {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}


REFUSED = [
    pytest.param(fetch_headers("cross-site", "no-cors", "image"), id="跨站的 <img>"),
    pytest.param(fetch_headers("cross-site", "cors", "empty"), id="跨站的 fetch"),
    pytest.param(fetch_headers("cross-site", "no-cors", "audio"), id="跨站的 <audio>"),
    pytest.param(fetch_headers("same-site", "no-cors", "image"), id="同站不同源的 <img>"),
    pytest.param(fetch_headers("same-site", "cors", "empty"), id="同站不同源的 fetch"),
    # iframe、embed、object 加载页面时 Sec-Fetch-Mode 也是 navigate，但页面看不到结果，服务照样会干活
    pytest.param(fetch_headers("cross-site", "navigate", "iframe"), id="跨站的 <iframe>"),
    pytest.param(fetch_headers("cross-site", "navigate", "frame"), id="跨站的 <frame>"),
    pytest.param(fetch_headers("cross-site", "navigate", "embed"), id="跨站的 <embed>"),
    pytest.param(fetch_headers("cross-site", "navigate", "object"), id="跨站的 <object>"),
    pytest.param(fetch_headers("same-site", "navigate", "iframe"), id="同站的 <iframe>"),
    # 浏览器后台预取：模式是 navigate、dest 是 document，只有 Sec-Purpose 说明不是用户在点
    pytest.param(
        {**fetch_headers("cross-site", "navigate", "document"), "Sec-Purpose": "prefetch"},
        id="跨站的预取（speculation rules）",
    ),
    pytest.param(
        {
            **fetch_headers("cross-site", "navigate", "document"),
            "Sec-Purpose": "prefetch;prerender",
        },
        id="跨站的预渲染",
    ),
    pytest.param(
        {**fetch_headers("same-site", "navigate", "document"), "Purpose": "prefetch"},
        id="同站的旧式 Purpose: prefetch",
    ),
    pytest.param({"Sec-Fetch-Site": "cross-site"}, id="只有 Site、没有 Mode"),
    pytest.param(
        {"Sec-Fetch-Site": " Cross-Site ", "Sec-Fetch-Mode": "No-Cors"}, id="大小写和空白不影响判断"
    ),
]

ALLOWED = [
    pytest.param(fetch_headers("cross-site", "navigate", "document"), id="飞书卡片：跨站整页导航"),
    pytest.param(fetch_headers("same-site", "navigate", "document"), id="同站整页导航"),
    pytest.param(fetch_headers("none", "navigate", "document"), id="地址栏和书签"),
    pytest.param(fetch_headers("same-origin", "no-cors", "audio"), id="同源的 <audio>"),
    pytest.param(fetch_headers("same-origin", "no-cors", "image"), id="同源的 <img>"),
    pytest.param(fetch_headers("same-origin", "cors", "empty"), id="同源的 fetch"),
    pytest.param(fetch_headers("same-origin", "navigate", "iframe"), id="同源的 <iframe>"),
    pytest.param(
        {**fetch_headers("same-origin", "navigate", "document"), "Sec-Purpose": "prefetch"},
        id="本站自己的预取",
    ),
    pytest.param({}, id="curl、老浏览器：不带 Sec-Fetch-*"),
]


@pytest.mark.parametrize("headers", REFUSED)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_foreign_subresource_reads_are_refused_before_the_handler_runs(
    tmp_path, monkeypatch, method, headers
):
    client, settings = make_client(tmp_path)
    artifact_id = seed_audio(settings)
    started = []
    monkeypatch.setattr(
        client.app.state.waveforms, "get", lambda *args, **kwargs: started.append(args) or {}
    )

    response = client.request(method, f"/api/media/{artifact_id}/peaks", headers=headers)

    assert response.status_code == 403
    # 403 不是处理完才回的：峰值计算（ffmpeg）一次都没起
    assert started == []
    if method == "GET":
        assert response.json() == {"detail": "跨站读取已拒绝"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("headers", ALLOWED)
def test_navigations_same_origin_pages_and_plain_clients_still_read(tmp_path, monkeypatch, headers):
    client, settings = make_client(tmp_path)
    artifact_id = seed_audio(settings)
    started = []
    monkeypatch.setattr(
        client.app.state.waveforms,
        "get",
        lambda *args, **kwargs: started.append(args) or {"peaks": [0.5]},
    )

    peaks = client.get(f"/api/media/{artifact_id}/peaks", headers=headers)
    health = client.get("/api/health", headers=headers)

    assert peaks.status_code == 200 and peaks.json() == {"peaks": [0.5]}
    assert len(started) == 1
    assert health.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/api/bootstrap", "/api/meetings", "/api/materials/files/1/thumb", "/", "/index.html"],
)
def test_the_rule_covers_every_get_path_not_just_media(tmp_path, path):
    client, _ = make_client(tmp_path)

    refused = client.get(path, headers=fetch_headers("cross-site", "cors", "empty"))
    plain = client.get(path)

    assert refused.status_code == 403
    assert plain.status_code != 403


def test_same_origin_audio_range_request_is_not_affected(tmp_path):
    """播放器的 Range 请求是同源的 <audio>：Sec-Fetch-Site 为 same-origin，不能被拦。"""
    client, settings = make_client(tmp_path)
    artifact_id = seed_audio(settings)

    response = client.get(
        f"/api/media/{artifact_id}",
        headers={**fetch_headers("same-origin", "no-cors", "audio"), "Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"


def test_a_foreign_page_cannot_read_the_csrf_token_or_even_trigger_the_download(tmp_path):
    client, settings = make_client(tmp_path)
    artifact_id = seed_audio(settings)
    foreign = fetch_headers("cross-site", "no-cors", "audio")

    assert client.get("/api/bootstrap", headers=foreign).status_code == 403
    assert client.get(f"/api/media/{artifact_id}", headers=foreign).status_code == 403
