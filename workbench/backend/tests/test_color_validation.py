"""项目和标签的颜色：前端色块和 <input type="color"> 发出来的都是 #rrggbb，
库里的颜色会被前端直接当 CSS 值用，所以创建和修改都只收这一种格式。"""

import pytest

from meeting_workbench.db import Database

from .test_tasks_api import make_client, write_headers

BAD_COLORS = [
    "red",
    "red;background:url(x)",
    "#fff",
    "#12345",
    "#1234567",
    "#gggggg",
    "667085",
    " #667085",
    "#667085 ",
    "#667085\n",
    "#66708é",
    "rgb(1,2,3)",
    "",
]


def project_colors(settings) -> dict[str, str]:
    rows = Database(settings.database_path).query_all("SELECT name, color FROM projects")
    return {row["name"]: row["color"] for row in rows}


@pytest.mark.parametrize("color", ["#3ecf8e", "#ABCDEF", "#abcdef", "#000000"])
def test_project_accepts_six_digit_hex_in_either_case(tmp_path, color):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)

    response = client.post("/api/projects", json={"name": "云图", "color": color}, headers=headers)

    assert response.status_code == 200, response.text
    assert project_colors(settings) == {"云图": color}


@pytest.mark.parametrize("color", BAD_COLORS)
def test_project_create_rejects_dirty_colors_and_stores_nothing(tmp_path, color):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)

    response = client.post("/api/projects", json={"name": "云图", "color": color}, headers=headers)

    assert response.status_code == 422
    assert project_colors(settings) == {}


@pytest.mark.parametrize("color", BAD_COLORS)
def test_project_update_rejects_dirty_colors_and_keeps_the_old_one(tmp_path, color):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    created = client.post(
        "/api/projects", json={"name": "云图", "color": "#3f51b5"}, headers=headers
    ).json()

    response = client.patch(
        f"/api/projects/{created['id']}", json={"color": color}, headers=headers
    )

    assert response.status_code == 422
    assert project_colors(settings) == {"云图": "#3f51b5"}


def test_project_update_changes_color_and_leaves_it_alone_when_not_sent(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    created = client.post("/api/projects", json={"name": "云图"}, headers=headers).json()
    assert project_colors(settings) == {"云图": "#667085"}

    changed = client.patch(
        f"/api/projects/{created['id']}", json={"color": "#7C3AED"}, headers=headers
    )
    renamed = client.patch(
        f"/api/projects/{created['id']}", json={"name": "云图 AI"}, headers=headers
    )

    assert changed.status_code == 200 and renamed.status_code == 200
    assert project_colors(settings) == {"云图 AI": "#7C3AED"}


@pytest.mark.parametrize("color", BAD_COLORS)
def test_tag_create_rejects_dirty_colors_and_stores_nothing(tmp_path, color):
    client, _ = make_client(tmp_path)
    headers = write_headers(client)

    response = client.post("/api/tags", json={"name": "待跟进", "color": color}, headers=headers)

    assert response.status_code == 422
    assert client.get("/api/tags").json() == []


@pytest.mark.parametrize(
    ("body", "stored"),
    [
        ({"name": "待跟进"}, "#667085"),
        ({"name": "待跟进", "color": "#f0783b"}, "#f0783b"),
        ({"name": "待跟进", "color": "#F0783B"}, "#F0783B"),
    ],
)
def test_tag_create_stores_the_default_or_the_given_hex(tmp_path, body, stored):
    client, _ = make_client(tmp_path)
    headers = write_headers(client)

    response = client.post("/api/tags", json=body, headers=headers)

    assert response.status_code == 200, response.text
    assert [tag["color"] for tag in client.get("/api/tags").json()] == [stored]
