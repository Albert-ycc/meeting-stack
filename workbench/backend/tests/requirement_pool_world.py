"""需求池改版（v17）测试用的项目、会议和逐字稿。

会议 id、会名、录音时间、时长、所属项目和时间锚前后的几句逐字稿，都照生产库原样抄（260930 只读取出），
原话和时间锚不编。需求和候选的标题、说明用《口径书》的示意数据（由真实会议纪要改写）。
"""

from __future__ import annotations

from typing import Any

from meeting_workbench.db import Database, utc_now

PROJECTS = {
    "yimi": ("project-59314319a40c43ea", "医米科研用药", "#2c8d83"),
    "hengrui": ("project-587f318a77c84d46", "恒瑞健康", "#549c8a"),
    "huaxia": ("project-2de1cdc1e9364813", "华夏基金会科普同行", "#2c8d83"),
    "cvm": ("project-592ef4b19a60442a", "CVM 云讲堂", "#5090ff"),
    "anxin": ("project-02500313fb494b2d", "安心四季", "#2c8d83"),
    "copy": ("project-036a983d51b849f1", "项目复制与名单一键转移", "#2c8d83"),
    "oral": ("project-a9eab7580a614e5b", "口服药到店领取配置方案", "#2c8d83"),
    # 库里有、但一场会都没归进来的项目
    "pager": ("project-e12e757109954848", "寻呼随访项目", "#3f51b5"),
}

# (会议 id, 会名, 录音时间, 时长毫秒, 项目键或 None, [(开始毫秒, 说话人, 原句)])
MEETINGS: dict[str, tuple[str, str, str, int, str | None, list[tuple[int, str, str]]]] = {
    "jd": (
        "vm-20260916-190150-2eebb406",
        "260916 医米京东科研仓系统对接",
        "2026-09-16T19:01:50-07:00",
        3033387,
        "yimi",
        [
            (825270, "C1_SPEAKER_10", "就是这个入库单的这个单据，"),
            (827350, "C1_SPEAKER_10", "你得需要从你们的一米这个系统里面给我们这个库房推过来。"),
            (1909360, "C1_SPEAKER_00", "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，"),
            (1915910, "C1_SPEAKER_00", "然后还要传这个随货通行单，"),
        ],
    ),
    "hengtiao": (
        "vm-20260910-182248-fe38a3cb",
        "医米赠药横跳拦截规则",
        "2026-09-10T18:22:48-07:00",
        194525,
        "yimi",
        [],
    ),
    "edc": (
        "vm-20260928-183726-93ac203c",
        "EDC 系统选型与产研对接决策",
        "2026-09-28T18:37:26-07:00",
        113475,
        "yimi",
        [],
    ),
    "five": (
        "vm-20260921-030550-4975623f",
        "新患者注册流程五个问题前置",
        "2026-09-21T03:05:50-07:00",
        78655,
        "yimi",
        [],
    ),
    "family": (
        "vm-20260921-191503-d0cfb758",
        "恒瑞黑卡亲友积分与后台字段",
        "2026-09-21T19:15:03-07:00",
        152885,
        "hengrui",
        [(78260, "SPEAKER_00", "没东西啊，"), (78860, "SPEAKER_00", "在我这里面要去加。")],
    ),
    "blackcard": (
        "vm-20260921-192149-f9d22c68",
        "黑卡分享注销与积分限制口径",
        "2026-09-21T19:21:49-07:00",
        105665,
        "hengrui",
        [],
    ),
    "huaxia": (
        "vm-20260914-003009-1dffbbce",
        "华夏劳务协议签署与证据链对接",
        "2026-09-14T00:30:09-07:00",
        3942349,
        "huaxia",
        [
            (1345560, "C1_SPEAKER_07", "我们就告诉你成功没匹配上的，"),
            (1347540, "C1_SPEAKER_07", "让你再确认嗯怎么确认这个交互。"),
        ],
    ),
    "cvm": (
        "vm-20260929-192637-f3947874",
        "260929 云课堂直播运营问题对齐",
        "2026-09-29T19:26:37-07:00",
        747000,
        "cvm",
        [
            (568390, "SPEAKER_03", "预约审核查看。"),
            (576900, "SPEAKER_01", "那我有办法导出 excel 吗？"),
            (581000, "SPEAKER_01", "是没有办法，"),
            (582060, "SPEAKER_01", "我看到导出是一个 OKOK。"),
        ],
    ),
    "aitan": (
        "vm-20260920-021944-87458591",
        "艾坦联合艾瑞卡申领与出组核对",
        "2026-09-20T02:19:44-07:00",
        130185,
        "anxin",
        [],
    ),
    "copy": (
        "vm-20260919-235421-d359efe7",
        "260919 项目复制与名单一键转移",
        "2026-09-19T23:54:21-07:00",
        276265,
        "copy",
        [],
    ),
    "oral": (
        "vm-20260916-215500-ba46d327",
        "口服药到店领取保留与药房配置",
        "2026-09-16T21:55:00-07:00",
        154155,
        "oral",
        [],
    ),
    # 库里这场会没归项目：它抽出来的候选是「未归项目」
    "doctor": (
        "vm-20260913-180145-6a743f14",
        "医生资质AI审核规则沟通",
        "2026-09-13T18:01:45-07:00",
        775770,
        None,
        [(124800, "SPEAKER_00", "然后这个门口或者你把前面这个也都不要嘛统一掉嘛。")],
    ),
}

# 逐字稿里选中的原话：跨句时原话取选中的文字，时间锚取第一句的开始时间（R01 异常与边界）。
QUOTES = {
    "inbound": (
        "jd",
        "就是这个入库单的这个单据，你得需要从你们的一米这个系统里面给我们这个库房推过来。",
        825270,
    ),
    "receipt": (
        "jd",
        "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，然后还要传这个随货通行单，",
        1909360,
    ),
    "family": ("family", "没东西啊，在我这里面要去加。", 78260),
    "export": ("cvm", "那我有办法导出 excel 吗？是没有办法，", 576900),
    "hospital": ("huaxia", "我们就告诉你成功没匹配上的，让你再确认嗯怎么确认这个交互。", 1345560),
    "doctor": ("doctor", "然后这个门口或者你把前面这个也都不要嘛统一掉嘛。", 124800),
}


def project_id(key: str) -> str:
    return PROJECTS[key][0]


def meeting_id(key: str) -> str:
    return MEETINGS[key][0]


def source(quote_key: str) -> dict[str, Any]:
    """接口入参里的来源：会议、原话、时间锚。"""
    meeting_key, quote, anchor_ms = QUOTES[quote_key]
    return {"meeting_id": meeting_id(meeting_key), "quote": quote, "anchor_ms": anchor_ms}


def seed_world(db: Database, *, projects: tuple[str, ...] = tuple(PROJECTS)) -> None:
    """建项目和会议（含逐字稿），会议归属照库里原样；只建 projects 里列到的项目，没建的项目下的会不建。"""
    now = utc_now()
    for key in projects:
        pid, name, color = PROJECTS[key]
        db.execute(
            "INSERT INTO projects(id, name, color, created_at) VALUES (?, ?, ?, ?)",
            (pid, name, color, now),
        )
    for _key, (mid, title, recorded, duration, project_key, segments) in MEETINGS.items():
        if project_key is not None and project_key not in projects:
            continue
        db.execute(
            """INSERT INTO meetings
                   (id, title, recording_date, duration_ms, status, project_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'published', ?, ?, ?)""",
            (
                mid,
                title,
                recorded,
                duration,
                project_id(project_key) if project_key else None,
                now,
                now,
            ),
        )
        if segments:
            version = db.create_transcript_version(mid, "funasr", published=True)
            db.replace_segments(
                version,
                mid,
                [
                    {
                        "id": f"{mid}-{start}",
                        "ordinal": ordinal,
                        "start_ms": start,
                        "end_ms": start + 1000,
                        "speaker_label": speaker,
                        "text": text,
                    }
                    for ordinal, (start, speaker, text) in enumerate(segments)
                ],
            )
