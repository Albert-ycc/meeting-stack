"""不需要请求体的写接口。前端的 write() 一律发 `{}`，curl 这类客户端什么都不发，两种都要照常通过；
带了别的字段一律 422。写法统一成 `_body: EmptyInput | None = None`，不再有「任意键都收的 dict」「必须带 `{}`
的必填体」「干脆不声明」三种并存。"""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .test_tasks_api import make_client, write_headers

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 带真字段（backend）但还没建模型、仍是 dict 的一处，不属于「不需要请求体」，不在这里统一
TYPED_LATER = {("POST", "/api/meetings/{meeting_id}/minutes/regenerate")}


def write_operations(app) -> list[tuple[str, str, dict[str, Any]]]:
    paths = app.openapi()["paths"]
    return [
        (method.upper(), path, operation)
        for path, item in paths.items()
        for method, operation in item.items()
        if method.upper() in WRITE_METHODS
    ]


def json_body_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    body = operation.get("requestBody") or {}
    return body.get("content", {}).get("application/json", {}).get("schema")


def referenced_models(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        found = {schema["$ref"].rsplit("/", 1)[-1]} if "$ref" in schema else set()
        return found.union(*(referenced_models(value) for value in schema.values()))
    if isinstance(schema, list):
        return set().union(*(referenced_models(value) for value in schema))
    return set()


def is_free_form(schema: Any) -> bool:
    """dict[str, Any] 在 OpenAPI 里是没有 properties、additionalProperties 不为 false 的 object。"""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" not in schema:
            return schema.get("additionalProperties", True) is not False
        return any(is_free_form(value) for value in schema.values())
    if isinstance(schema, list):
        return any(is_free_form(value) for value in schema)
    return False


def bodyless_operations(app) -> list[tuple[str, str]]:
    """没有自己的请求模型的写接口：没声明、任意键都收、或者已经是 EmptyInput。"""
    found = []
    for method, path, operation in write_operations(app):
        if (method, path) in TYPED_LATER:
            continue
        schema = json_body_schema(operation)
        if schema is None or is_free_form(schema) or "EmptyInput" in referenced_models(schema):
            found.append((method, path))
    return found


def concrete(path: str) -> str:
    # 路径里的 id 都填 1：只看请求体有没有被拒，处理函数里找不到东西回 404 也无所谓
    return re.sub(r"\{(\w+)\}", lambda match: "whisper" if match[1] == "name" else "1", path)


def body_rejected(response) -> bool:
    detail = response.json().get("detail") if response.status_code == 422 else None
    return isinstance(detail, list) and any(item["loc"][0] == "body" for item in detail)


def make_checking_client(tmp_path):
    client, _ = make_client(tmp_path)
    headers = write_headers(client)
    return TestClient(client.app, raise_server_exceptions=False, cookies=client.cookies), headers


def test_every_bodyless_write_route_declares_the_same_empty_body(tmp_path):
    client, _ = make_client(tmp_path)

    undeclared, free_form, shape = [], [], []
    for method, path, operation in write_operations(client.app):
        if (method, path) in TYPED_LATER:
            continue
        schema = json_body_schema(operation)
        if schema is None:
            undeclared.append(f"{method} {path}")
        elif is_free_form(schema):
            free_form.append(f"{method} {path}")
        elif "EmptyInput" in referenced_models(schema):
            # 不带请求体也要通过：声明成可选，类型是 EmptyInput 或 null
            assert not operation["requestBody"].get("required"), f"{method} {path}"
            options = schema.get("anyOf", [])
            assert {"$ref": "#/components/schemas/EmptyInput"} in options, f"{method} {path}"
            assert {"type": "null"} in options, f"{method} {path}"
            shape.append(f"{method} {path}")

    assert undeclared == [] and free_form == []
    # 防止 OpenAPI 的结构变了、上面什么都没扫到却照样通过
    assert len(shape) >= 40


def test_a_body_with_fields_is_rejected_on_every_bodyless_route(tmp_path):
    client, headers = make_checking_client(tmp_path)

    accepted_junk = []
    for method, path in bodyless_operations(client.app):
        response = client.request(method, concrete(path), json={"junk": 1}, headers=headers)
        if not body_rejected(response):
            accepted_junk.append(f"{method} {path} -> {response.status_code}")

    assert accepted_junk == []


@pytest.mark.parametrize("shape", ["empty_object", "no_body", "null"])
def test_empty_object_no_body_and_null_all_pass_body_validation(tmp_path, shape):
    client, headers = make_checking_client(tmp_path)
    options: dict[str, Any] = {
        "empty_object": {"json": {}},
        "no_body": {},
        "null": {"content": "null"},
    }[shape]

    refused = []
    for method, path in bodyless_operations(client.app):
        response = client.request(
            method,
            concrete(path),
            headers={**headers, "Content-Type": "application/json"},
            **options,
        )
        if body_rejected(response):
            refused.append(f"{method} {path} -> {response.json()['detail'][0]['msg']}")

    assert refused == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/uploads/nope/cancel"),
        ("POST", "/api/meetings/vm-x/publish"),
        ("POST", "/api/meetings/vm-x/project/undo"),
        ("POST", "/api/tasks/t-x/reject"),
        ("DELETE", "/api/projects/p-x"),
        ("POST", "/api/materials/files/1/open"),
    ],
)
def test_frontend_style_empty_object_and_curl_style_no_body_get_the_same_answer(
    tmp_path, method, path
):
    """每种旧写法各挑一个：必填 dict、可选 dict、没声明、EmptyInput。两种发法不仅都过校验，
    处理函数看到的也一样（不存在的 id 一律是同一个 404 之类的回答）。"""
    client, headers = make_checking_client(tmp_path)
    json_headers = {**headers, "Content-Type": "application/json"}

    with_object = client.request(method, path, json={}, headers=headers)
    without_body = client.request(method, path, headers=json_headers)

    assert not body_rejected(with_object) and not body_rejected(without_body)
    assert (with_object.status_code, with_object.content) == (
        without_body.status_code,
        without_body.content,
    )
