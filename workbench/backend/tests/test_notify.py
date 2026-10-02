"""飞书通知通道与任务通知调度测试（260804 新增）。"""

import json
import os
import re
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError

import pytest

from meeting_workbench.db import Database, utc_now
from meeting_workbench.notify import (
    LarkNotifier,
    build_minutes_ready_cards,
    outline_minutes,
)
from meeting_workbench.config import Settings
from meeting_workbench.task_due import BEIJING_TZ, beijing_today
from meeting_workbench.tasks import TaskService

# 老六段式纪要：决议在「## 三、核心决议」下，逐条是三级标题。
MINUTES_SIX_SECTION = """# 需求会 · 会议纪要

## 一、参会方与指代规范

### 说话人识别来源

表格略。

## 二、会议背景

本场是安和患者端的日常需求沟通会，产品侧抛出两个用户反馈。

## 三、核心决议

### 决议 1 · 患者端支持手机号修改，设四重约束 `[00:00:36 — 00:05:19]`

正文。

### 决议 2 · 扫码加入项目弹窗按项目自定义 `[00:05:47 — 00:06:58]`

正文。

## 四、行动项清单
"""

# 新模板纪要：摘要在「## 一分钟摘要」，决议是「## 决议」下的有序列表。
MINUTES_SUMMARY_LIST = """# 数据看板评审 · 会议纪要

## 一分钟摘要

本次会议围绕数据看板展开，确定了字段精简、导出口径与两个新增需求。

---

## 完整会议记录

### 议题一：看板字段

正文。

## 决议

1. 看板顶部保留核心统计字段，删除「材料回传率」 [00:03:24]
2. 「提交时间」改为「最后一次操作时间」 [00:07:10]
3. 导出按当前筛选范围全量导出 [00:09:44]

## 行动项
"""


def draft_tasks(extraction_id=7, count=1):
    """task_draft 新签名的任务样例。"""
    return [
        {
            "id": f"task-{i}",
            "title": f"任务{i}",
            "assignee": "ai",
            "anchor_ms": 1394000 if i == 1 else None,
            "anchor_quote": f"会上原话{i}" if i == 1 else "",
            "extraction_id": extraction_id,
        }
        for i in range(1, count + 1)
    ]


class FakeResponse:
    def read(self, _size: int = -1):
        return b'{"code": 0}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def make_db(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    return db, settings


def seed_task(db, task_id, status, title="任务X", updated_days_ago=0):
    now = utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', 'ai', ?, ?, ?)""",
        (task_id, title, status, now, now, now),
    )


def test_notifier_disabled_when_no_webhook(tmp_path):
    db, _settings = make_db(tmp_path)
    notifier = LarkNotifier(db, webhook_url="", public_base_url="http://x")
    assert not notifier.enabled
    assert not notifier.task_draft(1, meeting_title="会", tasks=draft_tasks(1))
    assert not notifier._sent("draft", "1")


def test_notifier_idempotent_on_success(tmp_path, monkeypatch):
    db, _settings = make_db(tmp_path)
    calls = []

    class FakeUrlopen:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", FakeUrlopen)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    assert notifier.task_draft(7, meeting_title="需求会", tasks=draft_tasks(7, 2))
    assert len(calls) == 1
    assert notifier._sent("draft", "7")
    # 同批次不重发
    assert not notifier.task_draft(7, meeting_title="需求会", tasks=draft_tasks(7, 2))
    assert len(calls) == 1


def test_notifier_does_not_record_on_failure(tmp_path, monkeypatch):
    db, _settings = make_db(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("webhook 挂了")

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", boom)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    assert not notifier.task_draft(9, meeting_title="会", tasks=draft_tasks(9))
    assert not notifier._sent("draft", "9")


def test_stall_candidates_respect_threshold(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    seed_task(db, "t-old", "in_progress", updated_days_ago=0)
    # 停滞阈值设为 3 天：手工把 status_changed_at 拨到 5 天前
    db.execute(
        "UPDATE tasks SET status_changed_at=? WHERE id='t-old'",
        (utc_now(),),
    )
    # 模拟 5 天前
    import datetime as dt

    five_days_ago = datetime.now().astimezone() - dt.timedelta(days=5)
    db.execute(
        "UPDATE tasks SET status_changed_at=? WHERE id='t-old'",
        (five_days_ago.isoformat(),),
    )
    seed_task(db, "t-fresh", "in_progress")
    db.execute(
        "UPDATE tasks SET status_changed_at=? WHERE id='t-fresh'",
        (datetime.now().astimezone().isoformat(),),
    )

    sent = []
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")

    def fake_stall(task):
        sent.append(task["id"])
        return True

    monkeypatch.setattr(notifier, "stall_reminder", fake_stall)
    monkeypatch.setattr(notifier, "daily_digest", lambda stats: False)
    service = TaskService(db, settings, notifier=notifier)
    service.run_notifications()
    # 只点名停滞的任务
    assert sent == ["t-old"]


def make_service(db, settings):
    return TaskService(db, settings)


def test_daily_digest_sends_once_per_day(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    seed_task(db, "t-p", "pending_confirm")
    seed_task(db, "t-d", "done")

    calls = []
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")

    def fake_urlopen(*args, **kwargs):
        calls.append(args)
        return FakeResponse()

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", fake_urlopen)
    service = make_service(db, settings)
    stats = service._digest_stats()
    assert stats["total"] >= 1
    assert notifier.daily_digest(stats)
    assert len(calls) == 1
    assert notifier._sent("digest", beijing_today().isoformat())
    # 当日不重发
    assert not notifier.daily_digest(service._digest_stats())
    assert len(calls) == 1


@pytest.fixture
def local_tz(request):
    """把本机时区钉成参数给的那个，用完还原（同 test_timeline 的 shanghai）。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("local_tz", ["America/Los_Angeles", "Asia/Shanghai"], indirect=True)
def test_daily_digest_follows_the_beijing_calendar(tmp_path, monkeypatch, local_tz):
    """晨报的触发（09:00 以后）、去重键、标题日期都按北京日历：本机在太平洋还是北京，结果一样——
    北京每天 09:00 以后发一次。原来按本机 09:00 触发、按 UTC 日期去重，太平洋时区同一天发两次。"""
    from zoneinfo import ZoneInfo

    from meeting_workbench import task_due, tasks

    class Clock(datetime):
        moment = None

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(tasks, "datetime", Clock)
    monkeypatch.setattr(task_due, "datetime", Clock)
    db, settings = make_db(tmp_path)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    titles = []
    monkeypatch.setattr(
        notifier,
        "_post",
        lambda payload: titles.append(payload["card"]["header"]["title"]["content"]) or True,
    )
    service = make_service(db, settings)
    stats = {
        "total": 1,
        "pending": 1,
        "pending_sources": [],
        "stalled": 0,
        "stalled_titles": [],
        "stalled_days": [],
        "done_yesterday": [],
        "auto_assigned_yesterday": 0,
        "needs_review": 0,
    }
    beijing = ZoneInfo("Asia/Shanghai")
    sent = []
    for moment in (
        datetime(2026, 10, 1, 8, 30, tzinfo=beijing),
        datetime(2026, 10, 1, 9, 5, tzinfo=beijing),
        datetime(2026, 10, 1, 17, 30, tzinfo=beijing),  # 太平洋 10-01 02:30
        datetime(2026, 10, 2, 8, 30, tzinfo=beijing),  # 太平洋 10-01 17:30，UTC 已是 10-02
        datetime(2026, 10, 2, 9, 10, tzinfo=beijing),
        datetime(2026, 10, 2, 23, 50, tzinfo=beijing),
    ):
        Clock.moment = moment
        if service._digest_due() and notifier.daily_digest(stats):
            sent.append(moment.isoformat())

    assert sent == ["2026-10-01T09:05:00+08:00", "2026-10-02T09:10:00+08:00"]
    assert titles == ["今日任务晨报 · 2026-10-01", "今日任务晨报 · 2026-10-02"]


def test_webhook_cards_escape_titles_from_meetings(tmp_path, monkeypatch):
    """webhook 通道（lark_md）的任务卡、停滞提醒、晨报：任务标题、会名、项目名来自会上内容，和应用机器人
    卡片一样转义，「[点我](http://evil.example)」不能在卡片里变成可点的链接。"""
    db, _settings = make_db(tmp_path)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    contents = []
    monkeypatch.setattr(
        notifier,
        "_post",
        lambda payload: contents.append(payload["card"]["elements"][0]["text"]["content"]) or True,
    )
    evil = "[点我](http://evil.example)"

    notifier.task_draft(
        1, meeting_title=evil, tasks=[{"id": "t", "title": evil}], project_name=evil
    )
    notifier.stall_reminder(
        {
            "id": "t",
            "title": evil,
            "stall_days": 3,
            "stall_since": "2026-09-28T00:00:00+00:00",
            "project_name": evil,
            "assignee": "me",
        }
    )
    notifier.daily_digest(
        {
            "total": 2,
            "pending": 1,
            "pending_sources": [evil],
            "stalled": 1,
            "stalled_titles": [evil],
            "stalled_days": [3],
            "done_yesterday": [evil],
            "auto_assigned_yesterday": 0,
            "needs_review": 0,
        }
    )

    assert len(contents) == 3
    for content in contents:
        assert evil not in content
        assert "\\[点我\\]" in content


@pytest.mark.parametrize(
    "pattern, old",
    [
        ("_SUBHEADING", (r"^###\s+(.+?)\s*$", re.MULTILINE)),
        ("_LIST_ITEM", (r"^\s*(?:\d+[.、)]|[-*+])\s+(.+?)\s*$", re.MULTILINE)),
        ("_ANCHOR", (r"\s*`?\[\d{2}:\d{2}(?::\d{2})?[^\]]*\]`?", 0)),
    ],
)
def test_rewritten_outline_patterns_parse_like_the_old_ones(pattern, old):
    """纪要摘要的几条正则改成不回溯的写法后，对仓库全部用例文本取到的东西和原来逐条一致。"""
    from meeting_workbench import notify

    lazy, greedy = re.compile(*old), getattr(notify, pattern)
    samples = [
        "### 决议 1 · 手机号修改 `[00:00:36 — 00:05:19]`  \t",
        "- a   b  \n1. 第一条\r\n2、 第二条　\n-   \n",
        "结论 `[00:01:02 — 00:03:04]`   [12:30 张三] 后半句",
    ]
    texts = [
        path.read_text(encoding="utf-8") for path in sorted(Path(__file__).parent.glob("*.py"))
    ]
    for text in texts + samples:
        if pattern == "_ANCHOR":
            assert lazy.sub("", text) == greedy.sub("", text)
        else:
            assert lazy.findall(text) == greedy.findall(text)


def test_outline_minutes_is_not_quadratic_on_a_long_run_of_spaces():
    """决议段里一行夹 2 万个空格：原来 outline_minutes 要好几秒，现在一眨眼。上界给得很宽，不卡机器速度。"""
    spaces = " " * 20000
    markdown = f"# 会\n\n## 一分钟摘要\n\n摘要{spaces}结尾\n\n## 决议\n\n- a{spaces}b\n"

    started = time.perf_counter()
    outline = outline_minutes(markdown)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert outline["items"][0].startswith("a")


def _log_path_for(kind, ref_key):
    return Path.home() / ".meeting-workbench" / "logs" / f"card-send-{kind}-{ref_key}.log"


def test_cli_channel_enabled_and_sends(tmp_path, monkeypatch):
    """应用机器人通道：chat_id 非空即启用，经 tmux 转发 lark-cli 发卡片 2.0。"""
    db, _settings = make_db(tmp_path)
    calls = []
    log_path = _log_path_for("draft", "11")

    def fake_tmux(_self, cmd, **kwargs):
        calls.append(cmd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"code": 0, "data": {"message_id": "om_x"}}', encoding="utf-8")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(LarkNotifier, "_tmux_run", fake_tmux)
    notifier = LarkNotifier(
        db,
        webhook_url="",
        public_base_url="http://x",
        chat_id="oc_test123",
        lark_cli_bin="lark-cli",
    )
    assert notifier.enabled
    tasks = draft_tasks(11)
    assert notifier.task_draft(11, meeting_title="需求会", tasks=tasks)
    assert notifier._sent("draft", "11")
    # 同批次不重发
    assert not notifier.task_draft(11, meeting_title="需求会", tasks=tasks)
    assert len(calls) == 1
    # 命令里带 POST messages + 卡片载荷
    cmd = calls[0]
    assert "POST /open-apis/im/v1/messages" in cmd
    args = shlex.split(cmd)
    body = json.loads(args[args.index("--data") + 1])
    assert body["receive_id"] == "oc_test123"
    assert body["msg_type"] == "interactive"
    card = json.loads(body["content"])
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"].startswith("会后任务待确认")
    flat = json.dumps(card, ensure_ascii=False)
    # 卡片里带逐条确认按钮与 task_id 载荷
    assert '"action": "confirm"' in flat and "task-1" in flat
    # 去掉「全部确认」：不得出现 confirm_all
    assert "confirm_all" not in flat and "全部确认" not in flat
    # 每条任务带「会上原话」辅助决策
    assert "会上原话" in flat and "会上原话1" in flat
    log_path.unlink(missing_ok=True)


def test_cli_channel_failure_not_recorded(tmp_path, monkeypatch):
    """lark-cli 返回业务失败码或 tmux 调度失败时，不落台账，下轮可补发。"""
    db, _settings = make_db(tmp_path)
    log_path = _log_path_for("draft", "12")

    def fail_tmux(_self, cmd, **kwargs):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"code": 99991663, "msg": "token invalid"}', encoding="utf-8")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(LarkNotifier, "_tmux_run", fail_tmux)
    notifier = LarkNotifier(
        db,
        webhook_url="",
        public_base_url="http://x",
        chat_id="oc_test123",
    )
    assert not notifier.task_draft(12, meeting_title="会", tasks=draft_tasks(12))
    assert not notifier._sent("draft", "12")

    def raising_tmux(_self, cmd, **kw):
        raise OSError("tmux 不可用")

    monkeypatch.setattr(LarkNotifier, "_tmux_run", raising_tmux)
    assert not notifier.task_draft(12, meeting_title="会", tasks=draft_tasks(12))
    assert not notifier._sent("draft", "12")
    log_path.unlink(missing_ok=True)


# ---------------------------------------------------------- 自建应用直连（第二实例）


class FakeAppResponse:
    """`_tenant_token` / `_post_via_app` 共用的 urlopen 响应桩。"""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self, _size: int = -1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def make_direct_app_notifier(db):
    return LarkNotifier(
        db,
        webhook_url="",
        public_base_url="http://x",
        chat_id="oc_direct",
        app_id="cli_direct",
        app_secret="secret_direct",
    )


def test_direct_app_channel_sends_minutes_and_task_draft_via_open_api_not_cli(
    tmp_path, monkeypatch
):
    """配了 app_id+secret+chat_id 时走自建应用直连开放平台，不再经 lark-cli/tmux。"""
    db, _settings = make_db(tmp_path)
    notifier = make_direct_app_notifier(db)
    assert notifier.direct_app

    def forbidden_tmux(*_args, **_kwargs):
        raise AssertionError("direct_app 模式不该再走 lark-cli/tmux")

    monkeypatch.setattr(LarkNotifier, "_tmux_run", forbidden_tmux)

    urls = []

    def fake_urlopen(request, timeout=None):
        urls.append(request.full_url)
        if "tenant_access_token" in request.full_url:
            return FakeAppResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        return FakeAppResponse({"code": 0})

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", fake_urlopen)

    assert notifier.minutes_ready(
        "mv-direct", meeting_title="需求会", markdown="# 摘要\n\n定了 X。"
    )
    assert notifier.task_draft(21, meeting_title="需求会", tasks=draft_tasks(21))

    assert notifier._sent("minutes", "mv-direct")
    assert notifier._sent("draft", "21")
    assert sum("open-apis/im/v1/messages" in url for url in urls) == 2
    assert sum("tenant_access_token" in url for url in urls) == 1  # 缓存命中，第二次没有重新换取


def test_tenant_token_is_cached_and_refetched_only_after_expiry(tmp_path, monkeypatch):
    """tenant_access_token 按返回的有效期缓存，命中不重复换取，过期后重新换取。"""
    db, _settings = make_db(tmp_path)
    notifier = make_direct_app_notifier(db)
    issued = []

    def fake_urlopen(request, timeout=None):
        issued.append(1)
        return FakeAppResponse(
            {"code": 0, "tenant_access_token": f"t-{len(issued)}", "expire": 7200}
        )

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", fake_urlopen)

    assert notifier._tenant_token() == "t-1"
    assert notifier._tenant_token() == "t-1"  # 缓存命中
    assert len(issued) == 1

    notifier._app_token_expires_at = time.time() - 1  # 模拟已过期
    assert notifier._tenant_token() == "t-2"
    assert len(issued) == 2


def test_direct_app_long_minutes_first_card_failure_not_recorded(tmp_path, monkeypatch):
    """长纪要拆成多张卡：首卡发送失败整批不记账，下轮可重发。"""
    db, _settings = make_db(tmp_path)
    notifier = make_direct_app_notifier(db)
    section = "### 议题 {n}\n\n" + "会议正文内容，反复展开细节。" * 40 + "\n"
    long_markdown = "# 超长会\n\n" + "\n".join(section.format(n=i) for i in range(1, 60))

    def fake_urlopen(request, timeout=None):
        if "tenant_access_token" in request.full_url:
            return FakeAppResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        raise URLError("模拟首卡发送失败")

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", fake_urlopen)

    assert (
        notifier.minutes_ready("mv-fail-first", meeting_title="超长会", markdown=long_markdown)
        is False
    )
    assert not notifier._sent("minutes", "mv-fail-first")


def test_direct_app_long_minutes_continuation_failure_keeps_first_card_recorded(
    tmp_path, monkeypatch
):
    """长纪要首卡发出去之后就已经记账；续篇发送失败只丢续篇，不重刷、也不误判失败。"""
    db, _settings = make_db(tmp_path)
    notifier = make_direct_app_notifier(db)
    section = "### 议题 {n}\n\n" + "会议正文内容，反复展开细节。" * 40 + "\n"
    long_markdown = "# 超长会\n\n" + "\n".join(section.format(n=i) for i in range(1, 60))

    sent_cards = 0

    def fake_urlopen(request, timeout=None):
        nonlocal sent_cards
        if "tenant_access_token" in request.full_url:
            return FakeAppResponse({"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        sent_cards += 1
        if sent_cards == 1:
            return FakeAppResponse({"code": 0})
        raise URLError("模拟续篇发送失败")

    monkeypatch.setattr("meeting_workbench.notify.urllib_request.urlopen", fake_urlopen)

    result = notifier.minutes_ready(
        "mv-fail-continuation", meeting_title="超长会", markdown=long_markdown
    )
    assert result is True
    assert notifier._sent("minutes", "mv-fail-continuation")
    assert sent_cards >= 2  # 首卡 + 至少一张续篇尝试


# ---------------------------------------------------------------- 纪要写好通知


def test_outline_reads_six_section_template():
    """老六段式：决议来自「核心决议」段的三级标题，摘要退回「会议背景」。"""
    outline = outline_minutes(MINUTES_SIX_SECTION)
    assert outline["kind"] == "decision"
    assert outline["items"] == [
        "患者端支持手机号修改，设四重约束",
        "扫码加入项目弹窗按项目自定义",
    ]
    assert outline["total"] == 2
    assert outline["summary"].startswith("本场是安和患者端")
    # 时间锚点与「决议 N ·」编号前缀都不该出现在群消息里
    assert "[00:00:36" not in outline["items"][0]
    assert not outline["items"][0].startswith("决议")


def test_outline_reads_summary_and_list_template():
    """新模板：摘要来自「一分钟摘要」，决议来自「## 决议」下的有序列表。"""
    outline = outline_minutes(MINUTES_SUMMARY_LIST)
    assert outline["kind"] == "decision"
    assert outline["total"] == 3
    assert outline["items"][0].startswith("看板顶部保留核心统计字段")
    assert "[00:03:24]" not in outline["items"][0]
    assert outline["summary"].startswith("本次会议围绕数据看板")
    # 「## 完整会议记录」下的议题标题不能混进决议清单
    assert all("议题" not in item for item in outline["items"])


def test_outline_falls_back_to_topics_without_decision_section():
    """没有决议段时退回议题标题，措辞也跟着从「定了」换成「聊了」。"""
    markdown = "## 一分钟摘要\n\n摘要正文。\n\n## 完整会议记录\n\n### 议题一：证件校验\n\n正文。\n"
    outline = outline_minutes(markdown)
    assert outline["kind"] == "topic"
    assert outline["items"] == ["证件校验"]
    cards = build_minutes_ready_cards(
        meeting_title="沟通会",
        recording_date="2026-08-06T00:56:16-07:00",
        duration_ms=423765,
        markdown=markdown,
    )
    # 卡片直接摊开纪要全文：议题标题原样在里面，不再摘成「定了这几件事」清单。
    flat = json.dumps(cards, ensure_ascii=False)
    assert len(cards) == 1
    assert "### 议题一：证件校验" in flat
    assert "这场会聊了这几件事" not in flat


def test_outline_empty_for_unstructured_minutes():
    """纪要没结构时宁可少一块，也不拿别处内容凑数。"""
    outline = outline_minutes("就是一段没有任何标题的白话纪要。")
    assert outline["items"] == [] and outline["kind"] == ""


def test_long_minutes_split_into_multiple_cards():
    """超长纪要拆成多张卡发全：每张都在飞书 30 KB 上限内，一个字都不丢。"""
    section = "### 议题 {n}\n\n" + "会议正文内容，反复展开细节。" * 40 + "\n"
    markdown = "# 超长会\n\n" + "\n".join(section.format(n=i) for i in range(1, 60))
    assert len(markdown.encode("utf-8")) > 30_000

    cards = build_minutes_ready_cards(
        meeting_title="超长会",
        recording_date=None,
        duration_ms=None,
        markdown=markdown,
    )
    assert len(cards) > 1
    for index, card in enumerate(cards, 1):
        assert len(json.dumps(card, ensure_ascii=False).encode("utf-8")) < 30_000
        assert card["header"]["title"]["content"] == f"纪要写好了 · 全文 {index}/{len(cards)}"
    # 正文逐段拼回来必须与原文一致，分片不能吃掉任何内容
    body = "\n\n".join(
        element["content"]
        for card in cards
        for element in card["body"]["elements"]
        if element["tag"] == "markdown"
    )
    for line in markdown.strip().split("\n"):
        if line.strip():
            assert line.strip() in body


def test_minutes_card_has_no_external_link():
    """本机地址在手机上打不开，纪要卡里不许再出现任何跳转按钮或链接。"""
    cards = build_minutes_ready_cards(
        meeting_title="需求会",
        recording_date="2026-08-06T00:56:16-07:00",
        duration_ms=423765,
        markdown=MINUTES_SIX_SECTION,
    )
    flat = json.dumps(cards, ensure_ascii=False)
    assert "http" not in flat and "看全文" not in flat
    assert all(
        element["tag"] in {"markdown", "hr"}
        for card in cards
        for element in card["body"]["elements"]
    )


def test_task_draft_card_escapes_markdown_link_syntax_in_title_and_quote():
    """任务标题/会上原话来自会议内容不受信任；混进链接语法后不能变成卡片里可点击的链接。"""
    from meeting_workbench.notify import build_task_draft_card

    malicious_title = "[点我](http://evil.example/pwn)"
    malicious_quote = "会上有人说：[立即转账](http://evil.example/2)"
    card = build_task_draft_card(
        meeting_title="需求会",
        tasks=[
            {
                "id": "task-1",
                "title": malicious_title,
                "assignee": "ai",
                "anchor_ms": None,
                "anchor_quote": malicious_quote,
                "extraction_id": 1,
            }
        ],
        interactive=False,
    )
    task_block = card["body"]["elements"][3]  # 0=导语 1=项目 2=hr 3=任务信息块
    texts = [item["content"] for item in task_block["columns"][0]["elements"]]
    joined = "\n".join(texts)
    assert malicious_title not in joined
    assert malicious_quote not in joined
    assert r"\[点我\]\(http://evil.example/pwn\)" in joined
    assert r"\[立即转账\]\(http://evil.example/2\)" in joined


def test_minutes_ready_card_content_and_idempotency(tmp_path, monkeypatch):
    """纪要卡走 应用机器人通道：带会议名/日期/时长/决议/下一步，同版本只发一次。"""
    db, _settings = make_db(tmp_path)
    calls = []
    log_path = _log_path_for("minutes", "mv-1")

    def fake_tmux(_self, cmd, **kwargs):
        calls.append(cmd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"code": 0}', encoding="utf-8")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(LarkNotifier, "_tmux_run", fake_tmux)
    notifier = LarkNotifier(
        db,
        webhook_url="",
        public_base_url="http://x",
        chat_id="oc_test123",
    )
    sent = notifier.minutes_ready(
        "mv-1",
        meeting_title="需求会 ·",
        recording_date="2026-08-06T00:56:16-07:00",
        duration_ms=423765,
        markdown=MINUTES_SIX_SECTION,
    )
    assert sent and notifier._sent("minutes", "mv-1")
    body = json.loads(shlex.split(calls[0])[shlex.split(calls[0]).index("--data") + 1])
    card = json.loads(body["content"])
    flat = json.dumps(card, ensure_ascii=False)
    assert card["header"]["title"]["content"] == "纪要写好了"
    # 标题尾巴上的孤立分隔符要清掉（正文里的 H1 原样保留，不算数）
    assert card["body"]["elements"][0]["content"] == "**需求会**"
    assert "8 月 6 日录的会，时长 7 分钟" in flat
    # 纪要全文原样进卡片：正文每一段都在，不摘要、不给「看全文」的跳转链接
    assert MINUTES_SIX_SECTION.strip() in flat.replace("\\n", "\n")
    assert "挑完发一张确认卡过来" in flat
    assert "看全文" not in flat
    # 同一份纪要不重复打扰
    assert not notifier.minutes_ready("mv-1", meeting_title="需求会", markdown="")
    assert len(calls) == 1
    log_path.unlink(missing_ok=True)


def seed_minutes(db, meeting_id, version_id, *, kind="generated", extraction="pending"):
    now = utc_now()
    db.execute(
        """INSERT INTO meetings (id, title, recording_date, duration_ms, status,
                                 current_minutes_version_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'completed_unreviewed', ?, ?, ?)""",
        (meeting_id, f"会议{meeting_id}", now, 600000, version_id, now, now),
    )
    db.execute(
        """INSERT INTO minutes_versions (id, meeting_id, version_no, markdown, kind, created_at)
           VALUES (?, ?, 1, ?, ?, ?)""",
        (version_id, meeting_id, MINUTES_SUMMARY_LIST, kind, now),
    )
    db.execute(
        """INSERT INTO task_extractions (meeting_id, minutes_version_id, status, created_at)
           VALUES (?, ?, ?, ?)""",
        (meeting_id, version_id, extraction, now),
    )


def test_minutes_notification_skips_backlog_and_non_generated(tmp_path, monkeypatch):
    """只通知这轮 AI 新写的纪要：存量 skipped 与导入的历史纪要都不惊动群里。"""
    db, settings = make_db(tmp_path)
    seed_minutes(db, "m-new", "mv-new")
    seed_minutes(db, "m-old", "mv-old", extraction="skipped")
    seed_minutes(db, "m-imported", "mv-imported", kind="imported")

    notified = []
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    monkeypatch.setattr(
        notifier, "minutes_ready", lambda version_id, **kw: notified.append(version_id) or True
    )
    service = TaskService(db, settings, notifier=notifier)
    assert service._notify_minutes_ready() == 1
    assert notified == ["mv-new"]


def test_task_cards_show_read_only_project_line():
    """任务卡和确认后重建的卡都有一行只读项目信息；没项目时说还没定。"""
    from meeting_workbench.notify import build_task_draft_card, build_task_status_card

    task = {
        "id": "task-1",
        "title": "对齐接口",
        "assignee": "ai",
        "anchor_ms": None,
        "extraction_id": 1,
    }
    draft = build_task_draft_card(meeting_title="需求会", tasks=[task], project_name="云图科研用药")
    assert draft["body"]["elements"][1] == {"tag": "markdown", "content": "项目：云图科研用药"}
    unknown = build_task_draft_card(meeting_title="需求会", tasks=[task])
    assert unknown["body"]["elements"][1]["content"] == "项目：还没定，到声档里选"

    # 重建卡从任务上取项目（任务跟着会议走）
    rebuilt = build_task_status_card(
        meeting_title="需求会",
        tasks=[{**task, "status": "confirmed", "project_name": "数据中台"}],
        all_done=True,
    )
    assert rebuilt["body"]["elements"][1]["content"] == "项目：数据中台"


def test_daily_digest_has_attribution_line(tmp_path, monkeypatch):
    """晨报加一行：昨天自动归属几场、几场等你选项目；没有任务时只要有归属信息也发。"""
    db, _settings = make_db(tmp_path)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    sent = []
    monkeypatch.setattr(
        notifier, "_send", lambda kind, ref, title, text, **kw: sent.append(text) or True
    )
    stats = {
        "total": 0,
        "pending": 0,
        "pending_sources": [],
        "in_progress": 0,
        "stalled": 0,
        "stalled_titles": [],
        "stalled_days": [],
        "done_yesterday": [],
        "auto_assigned_yesterday": 5,
        "needs_review": 2,
    }
    assert notifier.daily_digest(stats)
    assert sent[-1] == "· 昨天自动归属 5 场，2 场等你选项目。"
    assert not notifier.daily_digest({**stats, "auto_assigned_yesterday": 0, "needs_review": 0})


def test_digest_stats_count_yesterdays_auto_assignments(tmp_path, monkeypatch):
    """昨天（北京日历）自动归属的会按会议去重计数；今天的不算；待你选按当前状态数。时钟钉死，不取真实当前时间。"""
    from datetime import timedelta

    from meeting_workbench import tasks as tasks_module

    from .task_views_world import NOW, FrozenClock
    from .test_project_linking import seed_meeting

    monkeypatch.setattr(tasks_module, "datetime", FrozenClock)
    db, settings = make_db(tmp_path)
    beijing_now = NOW.astimezone(BEIJING_TZ)
    today_start = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_noon = today_start - timedelta(hours=12)
    for meeting_id in ("m1", "m2", "m3", "m4"):
        seed_meeting(db, meeting_id, f"会 {meeting_id}")

    def auto_event(meeting_id, when):
        db.execute(
            """INSERT INTO events(meeting_id, event_type, actor, payload_json, created_at)
               VALUES (?, 'meeting_project_auto_assigned', 'system', '{}', ?)""",
            (meeting_id, when.astimezone(UTC).isoformat()),  # 和 add_event 一样写 UTC
        )

    auto_event("m1", yesterday_noon)
    auto_event("m1", yesterday_noon + timedelta(minutes=5))  # 同一场会只算一次
    auto_event("m2", yesterday_noon)
    auto_event("m3", beijing_now)  # 今天的不算
    db.execute(
        """INSERT INTO project_links(meeting_id, minutes_version_id, status, candidates_json, created_at)
           VALUES ('m4', 'mv-m4', 'needs_review', '[]', ?)""",
        (utc_now(),),
    )

    stats = TaskService(db, settings)._digest_stats()
    assert stats["auto_assigned_yesterday"] == 2
    assert stats["needs_review"] == 1
