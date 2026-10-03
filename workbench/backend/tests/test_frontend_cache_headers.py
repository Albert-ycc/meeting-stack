"""前端静态文件的缓存头：assets/ 下文件名带内容哈希，浏览器一直缓存；index.html（含访问 / 时回落到的那份）每次回来问一声；
别的静态文件、404 和接口原来的缓存头不动。用临时目录造一份假的构建产物，不看本机有没有 build 过前端。"""

import pytest

from meeting_workbench import main

from .test_api_security_media import make_client

IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "fonts").mkdir()
    (dist / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/index-AbC123.js"></script>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-AbC123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "assets" / "index-AbC123.css").write_text("body{}", encoding="utf-8")
    (dist / "theme-init.js").write_text("void 0", encoding="utf-8")
    (dist / "fonts" / "outfit.woff2").write_bytes(b"wOF2")
    monkeypatch.setattr(main, "FRONTEND_DIST", dist)
    client, _settings = make_client(tmp_path)
    return client


def test_hashed_assets_are_cached_for_a_year(client):
    for path in ("/assets/index-AbC123.js", "/assets/index-AbC123.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == IMMUTABLE


def test_index_html_and_the_root_fallback_are_revalidated_every_time(client):
    for path in ("/", "/index.html"):
        response = client.get(path)
        assert response.status_code == 200
        assert "/assets/index-AbC123.js" in response.text
        assert response.headers["cache-control"] == "no-cache"
    # 回来问的时候没变就是 304，也带着 no-cache
    etag = client.get("/").headers["etag"]
    revalidated = client.get("/", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == "no-cache"


def test_other_static_files_404_and_api_keep_their_headers(client):
    for path in ("/theme-init.js", "/fonts/outfit.woff2"):
        response = client.get(path)
        assert response.status_code == 200
        assert "cache-control" not in response.headers
    missing = client.get("/assets/index-Gone99.js")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")
    assert client.get("/api/bootstrap").headers["cache-control"] == "no-store"
