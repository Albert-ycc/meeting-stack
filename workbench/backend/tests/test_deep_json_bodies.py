"""写接口的请求体嵌套过深：写保护中间件读完请求体就回 400，不进路由，也不进 FastAPI 的校验。

原来校验失败回 422 时，FastAPI 会把被拒的请求体逐层递归编码进响应，嵌到八九百层就撞上 Python 的递归上限，
异常出在异常处理里，落成 500，服务日志里每次一段 traceback；任意键都收的 dict 请求体（出纪要重写那个接口）
还会原样交给处理函数。"""

import json
import logging
from typing import Any

import pytest

from .test_api_security_media import make_client, write_headers

# security.MAX_JSON_DEPTH。前端发的最深的请求体是保存逐字稿，3 层
LIMIT = 64


def arrays(depth: int) -> str:
    return "[" * depth + "]" * depth


def objects(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


@pytest.fixture
def probe(tmp_path):
    """建好应用后挂一条任意键都收的 POST 探针路由，记下处理函数收到了什么。"""
    client, _ = make_client(tmp_path)
    calls: list[dict[str, Any]] = []

    def endpoint(body: dict[str, Any]):
        calls.append(body)
        return {"ok": True}

    client.app.post("/api/_probe")(endpoint)
    # 本机 build 过前端时静态文件挂在 /，挂载之后加的路由匹配不到，插到最前面
    client.app.router.routes.insert(0, client.app.router.routes.pop())
    return client, write_headers(client), calls


def post(client, headers, path: str, raw: str):
    return client.post(
        path, content=raw.encode(), headers={**headers, "Content-Type": "application/json"}
    )


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"x":' + arrays(LIMIT) + "}", id="比上限多一层"),
        pytest.param(arrays(995), id="995 层数组"),
        pytest.param(objects(1000), id="1000 层对象"),
        pytest.param(arrays(100_000), id="十万层"),
    ],
)
def test_too_deep_body_is_refused_before_the_handler_runs(probe, caplog, raw):
    client, headers, calls = probe

    with caplog.at_level(logging.INFO):
        response = post(client, headers, "/api/_probe", raw)

    assert response.status_code == 400
    assert response.json()["detail"] == f"请求体嵌套过深（超过 {LIMIT} 层）"
    assert calls == []
    # 中间件直接回话，没有异常：TestClient 默认把服务端的异常原样抛出来，生产里那就是日志里的一段 traceback
    assert [r for r in caplog.records if r.exc_info or r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize("path", ["/api/cards/enable", "/api/projects", "/api/glossary/terms"])
def test_typed_write_endpoints_answer_deep_bodies_with_400_instead_of_500(probe, path):
    client, headers, _ = probe

    response = post(client, headers, path, arrays(995))

    assert response.status_code == 400


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"x":' + arrays(LIMIT - 1) + "}", id="正好到上限"),
        pytest.param(
            json.dumps({"segments": [{"start_ms": n, "text": "好"} for n in range(5000)]}),
            id="括号上万个但只有三层",
        ),
        pytest.param(
            json.dumps({"text": '\\"' + "[{" * 200, "[[[": "{{{" * 100}),
            id="字符串里的括号和转义的引号不算",
        ),
    ],
)
def test_bodies_within_the_limit_still_reach_the_handler(probe, raw):
    client, headers, calls = probe

    response = post(client, headers, "/api/_probe", raw)

    assert response.status_code == 200
    assert calls == [json.loads(raw)]


def test_ordinary_typed_write_is_unaffected(probe):
    client, headers, _ = probe

    response = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["name"] == "云图"
