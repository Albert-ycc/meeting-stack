"""造数第二步（服务没起时跑，接在 `scan` 之后）：项目、会议归属、说话人名、待办（AI 草稿 + 已确认）、需求、
需求候选、词典词条、决议、材料文件夹、纪要版本很多的那场会。只写隔离数据目录和 $E2E_ROOT/browse。

日期都按「今天」往前推，原因见 seed_archive.py：待办草稿放满 7 天会自动过期，写死日期过几周用例就没有待确认可点了。
"""

import hashlib
import json
import os
import random
import struct
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meeting_workbench import decisions
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.rendering import render_safe_markdown
from meeting_workbench.requirement_candidates import insert_candidate

# run.py 起的是 backend_entry.py，会先关掉 .env；这里自己直接用 Settings，要同样关掉
Settings.model_config["env_file"] = None

ROOT = Path(os.environ["E2E_ROOT"])
settings = Settings()
assert str(settings.database_path).startswith(str(ROOT)), settings.database_path
db = Database(settings.database_path)
db.initialize()
seed = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))
TODAY = datetime.fromisoformat(seed["today"]).date()

PROJECTS = [
    ("project-yimi", "医米科研用药", "#2c8d83"),
    ("project-hengrui", "恒瑞健康", "#549c8a"),
    ("project-cvm", "CVM 云讲堂", "#5090ff"),
    ("project-empty", "空项目（无会议）", "#3f51b5"),
    ("project-moved", "资料盘搬走的项目", "#8c772c"),
]
ASSIGN = {
    "a1a1a1a1": "project-yimi",
    "a2a2a2a2": "project-yimi",
    "a3a3a3a3": "project-yimi",
    "a4a4a4a4": "project-yimi",
    "b1b1b1b1": "project-hengrui",
    "b2b2b2b2": "project-hengrui",
    "b3b3b3b3": "project-hengrui",
    "c1c1c1c1": "project-cvm",
    "c2c2c2c2": "project-cvm",
    "d1d1d1d1": "project-cvm",
    "d3d3d3d3": "project-yimi",
    # c3、c4、d2、e2e2e2e2、e3e3e3e3 不归项目
}

now = utc_now()
meetings = {
    row["id"][-8:]: row for row in db.query_all("SELECT id, title, duration_ms FROM meetings")
}
assert set(seed["meetings"]) <= set(meetings), set(seed["meetings"]) - set(meetings)
for pid, name, color in PROJECTS:
    db.execute(
        "INSERT OR IGNORE INTO projects(id, name, color, created_at) VALUES (?, ?, ?, ?)",
        (pid, name, color, now),
    )
for suffix, pid in ASSIGN.items():
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='manual' WHERE id=?",
        (pid, meetings[suffix]["id"]),
    )

# 说话人改名（一场会两人有名字）
m1 = meetings["a1a1a1a1"]["id"]
for label, name in (("SPEAKER_00", "张经理"), ("SPEAKER_01", "李工")):
    db.execute(
        "INSERT OR IGNORE INTO speakers(id, meeting_id, label, display_name) VALUES (?, ?, ?, ?)",
        (f"spk-{uuid.uuid4().hex[:8]}", m1, label, name),
    )

# 需求
reqs = [
    ("req-jd", "project-yimi", "京东科研仓对接", "P0", "active", ["a1a1a1a1", "d3d3d3d3"]),
    ("req-ht", "project-yimi", "赠药横跳拦截", "P0", "active", ["a2a2a2a2"]),
    ("req-edc", "project-yimi", "EDC 系统选型", "P1", "shelved", ["a3a3a3a3"]),
    ("req-five", "project-yimi", "新患者注册：五个问题前置", "P2", "done", ["a4a4a4a4"]),
    ("req-jf", "project-hengrui", "亲友积分入口", "P1", "active", ["b1b1b1b1", "b2b2b2b2"]),
    ("req-field", "project-hengrui", "后台字段补齐", "P2", "active", ["b3b3b3b3"]),
    ("req-live", "project-cvm", "直播画面比例 95%", "P1", "active", ["c1c1c1c1"]),
    (
        "req-xss",
        "project-cvm",
        '<img src=x onerror=alert(1)> "引号" 需求',
        "P3",
        "active",
        ["d1d1d1d1"],
    ),
]
for rid, pid, title, prio, status, ms in reqs:
    db.execute(
        """INSERT OR IGNORE INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rid, pid, title, prio, status, now, now),
    )
    for suffix in ms:
        db.execute(
            "INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
            (rid, meetings[suffix]["id"], now),
        )

# 待办：每场会 3~5 条，部分 AI 草稿待确认，部分已确认/进行中/已完成，带时间锚
statuses = ["pending_confirm", "pending_confirm", "confirmed", "in_progress", "done"]
titles = [
    "会后发出接口清单",
    "下周三前定方案",
    "确认导出 excel 是否放二期",
    "和测试对齐八条失败用例",
    "补齐后台字段说明",
]
count = 0
base = datetime.now(UTC) - timedelta(days=3)
for index, (suffix, row) in enumerate(sorted(meetings.items())):
    pid = ASSIGN.get(suffix)
    for k in range(3 + index % 3):
        count += 1
        status = statuses[(index + k) % len(statuses)]
        created = (base + timedelta(hours=count)).isoformat()
        due = (TODAY + timedelta(days=count % 9 - 4)).isoformat() if k % 2 else None
        db.execute(
            """INSERT OR IGNORE INTO tasks(id, title, status, origin, assignee, meeting_id, project_id,
                   anchor_ms, anchor_quote, due_date, status_changed_at, created_at, updated_at)
               VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"task-{count:03d}",
                f"{titles[k % len(titles)]}（{row['title'][:10]}）",
                status,
                "me" if k % 3 else "张经理",
                row["id"],
                pid,
                5000 + k * 7000,
                "那就先这样，会后我把接口清单发给大家",
                due,
                created,
                created,
                created,
            ),
        )
db.execute(
    "UPDATE tasks SET requirement_id='req-jd' WHERE meeting_id=? AND status!='pending_confirm'",
    (m1,),
)

# 需求候选（待认领）
cands = [
    ("a1a1a1a1", "京东仓签收凭证", "患者手持身份证跟药盒拍照上传作为签收凭证", "患者要手持身份证跟药盒拍照上传", 9000),
    ("a3a3a3a3", "EDC 供应商报价比对", "三家供应商报价对比后再定", "我们下周三之前把方案定下来", 12000),
    ("b3b3b3b3", "后台增加亲友积分列", "后台列表里看不到亲友积分", "这个字段在后台是看不到的，需要加", 15000),
    ("c1c1c1c1", "预约审核导出 Excel", "预约审核页不支持导出", "导出 excel 这个功能现在是没有的", 20000),
    ("c2c2c2c2", "名单一键转移", "项目复制时名单一并转移", "这个先不做，放到二期", 8000),
    ("c4c4c4c4", "医生资质 AI 审核提示统一", "未归项目的会抽出来的候选", "我觉得这里还是要加一个审批节点", 6000),
    ("d1d1d1d1", '<script>alert("候选")</script> 候选', "XSS 探针", "这个先不做，放到二期", 3000),
]  # fmt: skip
with db.autocommit() as conn:
    have = conn.execute("SELECT COUNT(*) FROM requirement_candidates").fetchone()[0]
    if not have:
        conn.execute("BEGIN")
        for suffix, title, summary, quote, anchor in cands:
            insert_candidate(
                conn,
                meeting_id=meetings[suffix]["id"],
                title=title,
                summary=summary,
                quote=quote,
                anchor_ms=anchor,
                similar_requirement_id="req-jd" if suffix == "a1a1a1a1" else None,
            )
        conn.execute("COMMIT")

# 词典
terms = [
    ("司美格鲁肽", ["司美格鲁太"], "通用", "药品"),
    ("京东科研仓", ["京东科研藏"], "医米科研用药", "其他"),
    ("白介素十七", ["IL 杠十七 a"], "CVM 云讲堂", "医学"),
    ("EDC", ["一地西"], "通用", "其他"),
    ('<b>粗体</b>"术语"', [], "通用", "其他"),
]
for term, aliases, scope, cat in terms:
    db.execute(
        """INSERT OR IGNORE INTO glossary_terms(id, term, aliases, scope, category, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"term-{uuid.uuid4().hex[:10]}",
            term,
            json.dumps(aliases, ensure_ascii=False),
            scope,
            cat,
            now,
            now,
        ),
    )

# 决议：从纪要「决议」段解析入库
print("decisions", decisions.ingest_pending(db, backfill_days=3650))


def tiny_png(path: Path) -> None:
    """64×64 的纯色 PNG，只为材料预览图那条请求有东西可取"""
    rows = b"".join(b"\x00" + bytes((44, 141, 131)) * 64 for _ in range(64))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# 材料文件夹：医米、恒瑞的都在；「资料盘搬走的项目」登记了一个已经不存在的文件夹（根目录显示「找不到」，
# 关系图取径器的用例要用）。它不挂会议，免得「根目录找不到」这个状态混进别的项目的卡片和关系图。
browse = settings.material_browse_root
yimi = browse / "医米材料"
yimi.mkdir(parents=True, exist_ok=True)
(yimi / "需求说明书.md").write_text("# 需求说明书\n京东科研仓对接", encoding="utf-8")
(yimi / "接口清单.txt").write_text("入库单接口\n出库单接口", encoding="utf-8")
tiny_png(yimi / "示意图.png")
hengrui = browse / "恒瑞材料"
hengrui.mkdir(parents=True, exist_ok=True)
(hengrui / "积分规则.md").write_text("# 积分规则\n一个积分抵一块钱", encoding="utf-8")
for pid, folder in (
    ("project-yimi", yimi),
    ("project-hengrui", hengrui),
    ("project-moved", browse / "已经搬走的资料"),
):
    db.execute(
        "INSERT OR IGNORE INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (pid, str(folder), now),
    )

# 纪要版本很多的会：补到 20 个版本，每版 markdown 约 30KB。走和 service.save_minutes 一样的写法
# （html 用 render_safe_markdown 渲，kind=draft，content_sha256 照算）
MID = seed["meetings"]["e3e3e3e3"]["id"]
SENTENCES = [
    "本场围绕接口对接的边界做了逐条确认，入库单需要从系统里推过去。",
    "审批节点放在导出之前，导出 excel 放到二期，口径不变。",
    "冷链温度要求是二到八度，库房那边需要补一个温度记录的字段。",
    "积分一个抵一块钱，会后由产品把口径写进需求文档并发给大家。",
    "测试反馈有八条用例没过，开发会在周三之前修完再提测。",
]


def version_body(version_no: int, rng: random.Random) -> str:
    lines = [f"# 纪要版本很多（第 {version_no} 版）", "", "## 一分钟摘要", ""]
    while sum(len(line.encode("utf-8")) + 1 for line in lines) < 30_000:
        lines.append(
            f"- [{version_no:02d}] {rng.choice(SENTENCES)}（编号 {rng.randrange(10**6):06d}）"
        )
    return "\n".join(lines) + "\n"


rng = random.Random(11)
current = db.query_one("SELECT current_minutes_version_id AS id FROM meetings WHERE id=?", (MID,))[
    "id"
]
existing = db.query_all("SELECT version_no FROM minutes_versions WHERE meeting_id=?", (MID,))
start = max(row["version_no"] for row in existing) + 1 if existing else 1
for version_no in range(start, 21):
    markdown = version_body(version_no, rng)
    version_id = f"mv-{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, based_on_id, markdown, html, kind, published, content_sha256, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?)""",
        (
            version_id,
            MID,
            version_no,
            current,
            markdown,
            render_safe_markdown(markdown),
            hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            utc_now(),
        ),
    )
    current = version_id
db.execute(
    "UPDATE meetings SET current_minutes_version_id=?, status='draft_modified' WHERE id=?",
    (current, MID),
)

for table in (
    "projects",
    "meetings",
    "tasks",
    "requirements",
    "requirement_candidates",
    "glossary_terms",
    "decisions",
    "minutes_versions",
):
    print(table, db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
