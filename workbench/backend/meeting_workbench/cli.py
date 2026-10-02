from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
import shutil
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

from .asr_eval import AsrEvaluationError, evaluate_asr, parse_engine_specs, run_qwen_shadow
from .backup import BackupManager
from .config import Settings
from .db import Database, read_only_uri, utc_now
from .importer import ArchiveImporter
from .integrity import AudioIntegrityError, AudioIntegrityVerifier, last_audio_integrity_result
from .gold_export import GoldExportError, export_gold_jsonl
from .main import create_app
from .ocr_engines import tools_report
from .project_linking import ProjectLinker
from .semantic import SemanticIndex, SemanticPaused, SemanticUnavailable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting-workbench")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="在 127.0.0.1:8765 启动工作台")
    subcommands.add_parser("scan", help="扫描正式归档与中转产物")
    semantic = subcommands.add_parser("semantic-index", help="构建本地语义索引")
    semantic.add_argument("--force", action="store_true")
    subcommands.add_parser("download-model", help="安装期下载固定本地语义模型")
    subcommands.add_parser("backup", help="创建在线 SQLite 备份")
    subcommands.add_parser("verify-audio", help="完整核验正式原音频哈希")
    asr_evaluate = subcommands.add_parser("asr-evaluate", help="用人工金标比较多个 ASR 引擎")
    asr_evaluate.add_argument("--gold", required=True)
    asr_evaluate.add_argument("--engine", action="append", required=True, metavar="NAME=DIR")
    asr_evaluate.add_argument("--output")
    asr_export = subcommands.add_parser("asr-export-gold", help="从工作台导出 ASR 金标 JSONL")
    asr_export.add_argument("--output", required=True)
    asr_export.add_argument("--meeting-id")
    qwen_shadow = subcommands.add_parser(
        "asr-shadow-qwen", help="使用本机缓存的 MLX Qwen3-ASR 生成影子稿"
    )
    qwen_shadow.add_argument("audio")
    qwen_shadow.add_argument("--output-dir", required=True)
    qwen_shadow.add_argument("--model", choices=["0.6B"], default="0.6B")
    subcommands.add_parser("doctor", help="检查本机运行条件")
    backfill_projects = subcommands.add_parser(
        "backfill-projects", help="为存量会议批量归类项目（字面线索 + LLM）"
    )
    backfill_projects.add_argument("--dry-run", action="store_true", help="只打印判定结果，不写库")
    backfill_projects.add_argument(
        "--evaluate",
        action="store_true",
        help="回测：拿人工归过项目的会当答案，藏起答案重判；模型高置信错 2 场以上时要求字面线索",
    )
    backfill_projects.add_argument(
        "--no-apply",
        action="store_true",
        help="和 --evaluate 一起用：只报告，不改 link_require_literal",
    )
    backfill_projects.add_argument("--limit", type=int, help="最多处理的会议数")

    materials_cmd = subcommands.add_parser(
        "materials", help="项目材料：盘点、读了多少、图片文字识别试跑和引擎"
    )
    materials_sub = materials_cmd.add_subparsers(dest="materials_command", required=True)
    walk = materials_sub.add_parser(
        "walk", help="走一遍项目材料文件夹，统计第三期要索引的量（只读，不改数据库）"
    )
    walk.add_argument("--dry-run", action="store_true", help="只盘点，不建索引（目前只支持这一种）")
    walk.add_argument("--project", help="只看这个项目（项目 id 或名字）")
    walk.add_argument(
        "--root",
        action="append",
        type=Path,
        help="只看这个文件夹，可以给多次；给了就不读数据库里挂的",
    )
    walk.add_argument("--no-probe", action="store_true", help="不读音视频时长")
    walk.add_argument("--json", type=Path, help="把完整结果另存成 JSON")
    ocr = materials_sub.add_parser(
        "ocr-trial",
        help="挑一些材料图片，分别用 Vision 和 tesseract 识别，比较用时和效果（在 Mac 上跑）",
    )
    ocr.add_argument("--project", help="只从这个项目的材料里挑（项目 id 或名字）")
    ocr.add_argument("--root", action="append", type=Path, help="只从这个文件夹里挑，可以给多次")
    ocr.add_argument("--limit", type=int, default=20, help="挑几张图，默认 20")
    ocr.add_argument(
        "--engine",
        action="append",
        choices=["vision", "tesseract"],
        help="只跑某一个，默认两个都跑",
    )
    ocr.add_argument("--out", type=Path, help="结果写到哪个文件夹，默认数据目录下的 ocr-trial/")
    status = materials_sub.add_parser("status", help="看材料读了多少、还剩多少、哪些读不了（只读）")
    status.add_argument("--project", help="只看这个项目（项目 id 或名字）")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    engine = materials_sub.add_parser(
        "ocr-engine",
        help="选图片和扫描页用哪套认字：auto（默认）、vision、tesseract、off；不用重启服务",
    )
    engine.add_argument(
        "engine",
        nargs="?",
        choices=["auto", "vision", "tesseract", "off"],
        help="不给就只看现在用的是哪套",
    )
    links_cmd = subcommands.add_parser(
        "links", help="深度关联：各步在等几个、AI 用量、现在重试、看一场会解析出的决议"
    )
    links_sub = links_cmd.add_subparsers(dest="links_command", required=True)
    links_status = links_sub.add_parser(
        "status",
        help="各步在等几个、上一轮的时间、AI 状态、今天调了几次和上限（直接查库，服务没开也能看）",
    )
    links_status.add_argument("--json", action="store_true", help="输出 JSON")
    links_sub.add_parser(
        "retry",
        help="把没做成的 AI 整理和决议对比放回队列、次数清零，清掉 AI 循环的暂停（和页面上的［现在重试］一样）",
    )
    links_decisions = links_sub.add_parser(
        "decisions",
        help="打印一场会解析出的决议：id、原文、时间点、note，和对比时会发的提示词（只读，不发送）",
    )
    links_decisions.add_argument("--meeting", required=True, help="会议 id")
    links_decisions.add_argument("--json", action="store_true", help="输出 JSON")
    # 4d：相关的校准输出（开发工具，唯一印分数的地方）
    links_related = links_sub.add_parser(
        "related", help="看一场会每段找到的相关材料：分数、门槛、共同词、被刷掉的原因"
    )
    links_related.add_argument("--meeting", help="会议 id")
    links_related.add_argument("--project", help="项目 id（和 --stats 或 --rebuild 一起用）")
    links_related.add_argument("--at", help="只看这个时刻所在的窗，比如 12:30 或 1:02:30")
    links_related.add_argument("--floor", type=float, help="临时换一个最低门槛（不改设置）")
    links_related.add_argument("--margin", type=float, help="临时换一个门槛余量（不改设置）")
    links_related.add_argument(
        "--encode", action="store_true", help="重新编码窗（不写库）；默认用存下的窗"
    )
    links_related.add_argument("--stats", action="store_true", help="这个项目的相关统计")
    links_related.add_argument(
        "--rebuild", action="store_true", help="标记重算：只把 dirty 加一，不当场算"
    )
    links_related.add_argument("--json", action="store_true", help="输出 JSON")
    # 4g：问答试跑。问题从标准输入读，不放进命令行参数（免得出现在 ps 和 shell 历史里）；从不调 AI
    links_ask = links_sub.add_parser(
        "ask", help="问答试跑：问题从标准输入读，列出会找到的原话、要发几段、发给谁；不发送"
    )
    links_ask.add_argument("--project", required=True, help="项目 id")
    # 4h：从材料里挖出的词（手工验收看准头用）
    links_words = links_sub.add_parser(
        "words", help="这个项目从材料里找到的词：排好序，每个一行，带次数和一处证据"
    )
    links_words.add_argument("--project", required=True, help="项目 id")
    links_words.add_argument(
        "--dry-run",
        action="store_true",
        help="当场给这个项目的内容挖一遍，什么都不写；不带就真做一遍项目汇总",
    )
    return parser


VERDICT_LABELS = {
    "auto_right": "自动·对",
    "auto_wrong": "自动·错",
    "review_hit": "待你选·含答案",
    "review_miss": "待你选·不含",
    "unresolved": "没认出",
}


def _print_evaluation(result: dict) -> int:
    if not result["llm_ready"]:
        print("未配置 LLM API key：本次回测只按字面线索判断", file=sys.stderr)
    print(f"{'会议标题':<30}{'答案':<16}{'判成':<16}{'结果':<14}原因")
    for item in result["results"]:
        guess = item["project_name"]
        if not guess and item["candidates"]:
            guess = " / ".join(
                str(candidate.get("project_name") or "") for candidate in item["candidates"]
            )
        print(
            f"{(item['meeting_title'] or ''):<30}"
            f"{(item['answer_project_name'] or ''):<16}"
            f"{(guess or '—'):<16}"
            f"{VERDICT_LABELS[item['verdict']]:<14}"
            f"{item['reason']}"
        )
    counts = result["counts"]
    print(
        f"共 {result['evaluated']} 场：自动对 {counts['auto_right']}，自动错 {counts['auto_wrong']}"
        f"（其中模型高置信错 {counts['llm_high_wrong']}），待你选含答案 {counts['review_hit']}，"
        f"不含 {counts['review_miss']}，没认出 {counts['unresolved']}"
    )
    hints = result.get("requirement_hints")
    if hints:
        print(f"有新需求提示的会 {hints['meetings']} / {hints['total']}")
    if result.get("skipped_untrusted"):
        print(
            f"另有 {result['skipped_untrusted']} 场人工归属没算进来（先被 AI 归、后来只是确认，或没有纪要）"
        )
    guard = result["literal_guard"]
    print(
        f"要求字面线索后：能拦下 {guard['wrong_blocked']} 场模型归错的，"
        f"也会让 {guard['right_demoted']} 场模型归对的改成待你选"
    )
    if result["require_literal_enabled"]:
        print("模型高置信归错 2 场以上，已打开 link_require_literal")
    elif result["require_literal_before"]:
        print("link_require_literal 之前已经打开")
    return 0


def _material_roots(
    settings: Settings, project: str | None, roots: list[Path] | None
) -> list[dict[str, Any]]:
    """--root 给了就只用它；否则只读打开数据库，列出（某个项目或全部项目）挂的材料文件夹。"""
    if roots:
        return [{"path": str(root.expanduser())} for root in roots]
    assert settings.database_path is not None
    if not settings.database_path.exists():
        raise SystemExit(
            f"找不到声档数据库：{settings.database_path}；可以用 --root 直接指定文件夹"
        )
    connection = sqlite3.connect(read_only_uri(settings.database_path), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        chosen = _find_project(connection, project)
        rows = connection.execute(
            """SELECT r.path, r.project_id, p.name AS project_name
                 FROM project_material_roots r JOIN projects p ON p.id = r.project_id
                WHERE ? IS NULL OR r.project_id = ?
                ORDER BY p.name, r.created_at, r.id""",
            (chosen["id"] if chosen else None, chosen["id"] if chosen else None),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise SystemExit("还没有挂材料文件夹；可以用 --root 直接指定文件夹")
    return [dict(row) for row in rows]


def _find_project(connection: sqlite3.Connection, project: str | None) -> dict[str, Any] | None:
    """--project 按 id 或名字（不分全半角、大小写）找项目；找不到就列出现有项目退出。"""
    if not project:
        return None
    projects = [
        dict(row) for row in connection.execute("SELECT id, name FROM projects ORDER BY name")
    ]
    wanted = unicodedata.normalize("NFKC", project).casefold().strip()
    chosen = next(
        (
            item
            for item in projects
            if item["id"] == project
            or unicodedata.normalize("NFKC", item["name"]).casefold().strip() == wanted
        ),
        None,
    )
    if chosen is None:
        names = "、".join(item["name"] for item in projects) or "（还没有项目）"
        raise SystemExit(f"没有叫「{project}」的项目。现有项目：{names}")
    return chosen


class _ReadOnlyDb:
    """只读连接包一层 query_one，给 OcrEngines 查认字引擎的设置用。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None


def _materials_status(args: argparse.Namespace, settings: Settings) -> int:
    """materials status：直接查库（读不到服务内存，所以不写「转写会议时先停」）。"""
    from .material_status import coverage, render_status
    from .ocr_engines import OcrEngines

    assert settings.database_path is not None
    if not settings.database_path.exists():
        raise SystemExit(f"找不到声档数据库：{settings.database_path}")
    connection = sqlite3.connect(read_only_uri(settings.database_path), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        chosen = _find_project(connection, args.project)
        engines = OcrEngines(_ReadOnlyDb(connection), settings)  # type: ignore[arg-type]
        roots = coverage(connection, chosen["id"] if chosen else None, engines=engines)
        names = {
            row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM projects")
        }
    finally:
        connection.close()
    if args.json:
        print(json.dumps({"roots": roots}, ensure_ascii=False, indent=2))
    else:
        print(render_status(roots, names))
    return 0


ENGINE_LABELS = {
    "vision": "Vision（macOS 自带）",
    "tesseract": "tesseract",
    "off": "不认字（只读 PDF 文字层）",
}


def _ocr_engine(args: argparse.Namespace, settings: Settings) -> int:
    from .ocr_engines import OcrEngines

    engines = OcrEngines(_database(settings), settings)
    reread = engines.set_engine(args.engine)["reread"] if args.engine else 0
    print(
        "正在检查认字程序（第一次要编译 Vision 程序，可能要一两分钟）…", file=sys.stderr, flush=True
    )
    engines.refresh(force=True)
    current = engines.image_engine()
    print(
        f"设置：{engines.setting()}；图片和扫描页现在用：{ENGINE_LABELS.get(current or '', '没有能用的（先装一套）')}"
    )
    tools = engines.tools()
    print(f"Vision：{engines.build.describe() if sys.platform == 'darwin' else '只能在 Mac 上用'}")
    if not tools.tesseract:
        print("tesseract：没装（brew install tesseract tesseract-lang）")
    elif not tools.tesseract_chinese:
        print("tesseract：没有中文语言包（brew install tesseract-lang）")
    else:
        print(f"tesseract：能用（{tools.tesseract_version}）")
    print("PDF 的文字层：" + ("能读" if engines.build.ready() else "要等 Vision 程序编译好"))
    if reread:
        print(f"关着认字时跳过的 {reread} 份图片和扫描件会重新认字")
    return 0


def _materials(args: argparse.Namespace, settings: Settings) -> int:
    from . import material_walk, ocr_trial

    if args.materials_command == "ocr-engine":
        return _ocr_engine(args, settings)
    if args.materials_command == "status":
        return _materials_status(args, settings)
    roots = _material_roots(settings, args.project, args.root)
    if args.materials_command == "walk":
        if not args.dry_run:
            print("请加 --dry-run：材料索引由服务在后台自动建，这个命令只做盘点。", file=sys.stderr)
            return 2
        report = material_walk.walk_materials(
            roots, probe_media=not args.no_probe, progress=material_walk.stderr_progress
        )
        print(material_walk.render_report(report))
        if args.json:
            args.json.expanduser().write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"完整结果已存到 {args.json}")
        return 0

    from .materials import ROOT_ONLINE, volume_state

    online = [Path(root["path"]) for root in roots if volume_state(root["path"]) == ROOT_ONLINE]
    if not online:
        print("挂的材料文件夹现在都不在（盘没插或文件夹没了）", file=sys.stderr)
        return 1
    images = ocr_trial.pick_images(ocr_trial.collect_images(online), online, args.limit)
    if not images:
        print("材料里没找到大于 20 KB 的图片", file=sys.stderr)
        return 1
    out_dir = args.out.expanduser() if args.out else ocr_trial.default_out_dir(settings.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = ocr_trial.scratch_dir()
    try:
        engines, notes = ocr_trial.prepare_engines(args.engine or ["vision", "tesseract"], workdir)
        for note in notes:
            print(note, file=sys.stderr)
        if not engines:
            return 1
        report = ocr_trial.run_trial(
            images,
            engines,
            progress=lambda done, total: print(
                f"已识别 {done}/{total} 张…", file=sys.stderr, flush=True
            ),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    report["machine"] = ocr_trial.machine_summary()
    (out_dir / "结果.md").write_text(ocr_trial.render_markdown(report, notes), encoding="utf-8")
    (out_dir / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(ocr_trial.render_summary(report, out_dir))
    return 0


def _read_only(settings: Settings) -> sqlite3.Connection:
    assert settings.database_path is not None
    if not settings.database_path.exists():
        raise SystemExit(f"找不到声档数据库：{settings.database_path}")
    connection = sqlite3.connect(read_only_uri(settings.database_path), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _server_url(settings: Settings, path: str) -> str:
    return f"http://127.0.0.1:{settings.port}{path}"


def _server_json(settings: Settings, path: str, *, post: bool = False) -> dict[str, Any] | None:
    """服务开着时问它（GET，或带 CSRF 的 POST）；连不上回 None。只连本机。"""
    import urllib.error
    import urllib.request

    try:
        if not post:
            with urllib.request.urlopen(_server_url(settings, path), timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(_server_url(settings, "/api/bootstrap"), timeout=3) as response:
            token = json.loads(response.read().decode("utf-8"))["csrf_token"]
        request = urllib.request.Request(
            _server_url(settings, path),
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{settings.port}",
                "X-CSRF-Token": token,
                "Cookie": f"{settings.csrf_cookie_name}={token}",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # 服务开着、但拒绝了这次请求：照实报，不能当成「服务没开」再去直接写库
        error.close()
        return {"http_error": int(error.code)} if post else None
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        return None


LINKS_PHASE_LABELS = {
    "decisions": "决议入库",
    "resolve": "重找文件",
    "stale": "影响自动收回",
    "produced": "产出建议",
    "mentions": "放宽的提到",
    "affects": "影响匹配",
    "related": "相关",
    "terms": "挖词",
}
# deep_links.PHASE_STATES 的人话
LINKS_PHASE_STATES = {
    "done": "做完了",
    "budget": "这轮时间到了，下轮接着做",
    "busy": "会议在转写，先停",
    "stopping": "服务在停，没做完",
    "locked": "数据库忙，下轮再做",
    "off": "关着",
    "waiting": "在等前一步",
    "error": "出错了，下轮再试（详见服务日志）",
}
LINKS_LLM_LABELS = {
    "ok": "正常",
    "off": "关着",
    "no_key": "没配置 key",
    "auth": "key 不对，停下了（换了 key 或 links retry 后恢复）",
    "balance": "账户余额不足，停下了（充值后 links retry）",
    "capped": "今天的用量到上限了",
    "backoff": "连不上，在退避，会自己再试",
    "failing": "连续几次连不上",
}


def _local_time(value: str | None) -> str | None:
    """库里和健康检查里的 UTC 时间（带 Z 或 +00:00）按本机时区写成「2026-09-28 06:30」。"""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _links_status(args: argparse.Namespace, settings: Settings) -> int:
    """links status：各步在等几个、今天的用量直接查库；上一轮的时间和 AI 的暂停只在服务内存里，服务开着
    时顺便问一下它的健康检查。"""
    from . import decisions
    from .links_llm import read_usage
    from .llm import destination, llm_ready

    connection = _read_only(settings)
    try:
        today = datetime.now().date().isoformat()
        waiting = {
            "decisions": decisions.pending_count(connection),
            "mentions": connection.execute(
                "SELECT COUNT(*) FROM mention_extractions WHERE state IN ('pending', 'running')"
            ).fetchone()[0],
            "pairs": connection.execute(
                "SELECT COUNT(*) FROM decision_scan WHERE pair_state IN ('pending', 'running')"
            ).fetchone()[0],
        }
        # 4d：到期的会，另数材料太多、没比全的会（只在这里计数）
        from . import related

        waiting["related"] = related.due_count(connection, settings)
        related_partial = connection.execute(
            "SELECT COUNT(*) FROM meeting_related_scan WHERE partial = 1"
        ).fetchone()[0]
        # 4e：可能过时（H2）到期的会
        from . import affects

        waiting["affects"] = affects.due_count(connection, datetime.now(UTC))
        failed = {
            "mentions": connection.execute(
                "SELECT COUNT(*) FROM mention_extractions WHERE state = 'failed'"
            ).fetchone()[0],
            "pairs": connection.execute(
                "SELECT COUNT(*) FROM decision_scan WHERE pair_state = 'failed'"
            ).fetchone()[0],
        }
        opened = {
            kind: connection.execute(
                "SELECT COUNT(*) FROM relations WHERE kind = ? AND status = 'suggested'", (kind,)
            ).fetchone()[0]
            for kind in ("produced", "affects")
        }
        usage = read_usage(connection, today)
        housekeeping = connection.execute(
            "SELECT value FROM app_state WHERE key = 'links_housekeeping_at'"
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise SystemExit(f"数据库还没升到 v16（先启动一次服务）：{error}") from None
    finally:
        connection.close()
    health = _server_json(settings, "/api/health")
    live = ((health or {}).get("details") or {}).get("links") if health else None
    if live and "llm" in live:
        llm_state = str(live["llm"])
    elif (
        not (settings.links_enabled and settings.links_llm_enabled)
        or settings.links_llm_daily_calls <= 0
    ):
        llm_state = "off"
    elif not llm_ready(settings):
        llm_state = "no_key"
    elif usage["background"] >= settings.links_llm_daily_calls:
        llm_state = "capped"
    else:
        llm_state = "ok"
    result = {
        "enabled": settings.links_enabled,
        "server": live is not None,
        "last_round_at": (live or {}).get("last_round_at"),
        "paused": (live or {}).get("paused"),
        "phases": (live or {}).get("phases") or {},
        "waiting": waiting,
        "related_partial": related_partial,
        "failed": failed,
        "open": opened,
        "housekeeping_at": housekeeping["value"] if housekeeping else None,
        "llm": llm_state,
        "llm_host": destination(settings).host,
        "calls_today": {"background": usage["background"], "qa": usage["qa"]},
        "limits": {"background": settings.links_llm_daily_calls, "qa": settings.qa_daily_questions},
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(
        "关联整理："
        + ("开着" if settings.links_enabled else "关着（MEETING_WORKBENCH_LINKS_ENABLED）")
    )
    if live is None:
        print("服务没开（或连不上）：上一轮的时间和各步的情况要服务开着才看得到")
    else:
        print(
            f"上一轮：{_local_time(result['last_round_at']) or '还没跑过'}"
            + ("，会议在转写，重活先停" if result["paused"] == "busy" else "")
        )
        for name, status in result["phases"].items():
            print(
                f"  {LINKS_PHASE_LABELS.get(name, name)}：{LINKS_PHASE_STATES.get(status, status)}"
            )
    print(
        f"在等：决议入库 {waiting['decisions']} 场，放宽的提到 {waiting['mentions']} 场"
        f"（没做成 {failed['mentions']} 场），决议对比 {waiting['pairs']} 场（没对比成 {failed['pairs']} 场）"
    )
    print(f"相关：{waiting['related']} 场会到期，{related_partial} 场材料太多、没比全")
    print(
        f"在问你：产出 {opened['produced']} 条，可能过时 {opened['affects']} 条"
        f"（可能过时还有 {waiting['affects']} 场会到期没配）"
    )
    print(f"上次清理：{_local_time(result['housekeeping_at']) or '还没清理过'}")
    print(f"AI（{result['llm_host']}）：{LINKS_LLM_LABELS.get(llm_state, llm_state)}")
    print(
        f"今天调了：后台 {usage['background']} / {settings.links_llm_daily_calls} 次，"
        f"问答 {usage['qa']} / {settings.qa_daily_questions} 次"
    )
    return 0


def _links_retry(settings: Settings) -> int:
    """links retry：服务开着时走 POST /api/links/retry（连 AI 循环内存里的暂停一起清）；没开时直接改库
    （暂停只在服务内存里，下次启动本来就没有）。"""
    from .links_llm import requeue_failed

    answer = _server_json(settings, "/api/links/retry", post=True)
    if answer is not None and "http_error" in answer:
        print(f"服务开着，但没接受这次重试（HTTP {answer['http_error']}），什么都没改")
        return 1
    if answer is not None and "requeued" in answer:
        print(f"已放回 {answer['requeued']} 场，AI 循环的暂停和退避已清掉")
        return 0
    db = _database(settings)
    with db.transaction() as connection:
        requeued = requeue_failed(connection)
    print(f"已放回 {requeued} 场（服务没开，下次启动时接着做）")
    return 0


def _hhmmss(ms: int | None) -> str:
    if ms is None:
        return "--:--:--"
    seconds = int(ms) // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _links_decisions(args: argparse.Namespace, settings: Settings) -> int:
    """links decisions --meeting：台账跟上当前纪要时从表里读（带 id）；落后时当场解析（id 为空）。只读。
    4c 起再打印对比时会发的提示词，只打印，从不发送。"""
    from . import decisions

    connection = _read_only(settings)
    try:
        meeting = connection.execute(
            "SELECT id, title, current_minutes_version_id FROM meetings WHERE id = ?",
            (args.meeting,),
        ).fetchone()
        if meeting is None:
            raise SystemExit(f"没有这场会：{args.meeting}")
        try:
            ledger = decisions.ledger_decisions(
                connection, meeting["id"], meeting["current_minutes_version_id"]
            )
        except sqlite3.OperationalError:
            ledger = None
        if ledger is not None:
            items, note = ledger
            source = "table"
        else:
            row = connection.execute(
                "SELECT markdown FROM minutes_versions WHERE id = ?",
                (meeting["current_minutes_version_id"],),
            ).fetchone()
            parsed = decisions.parse_safely(row["markdown"] if row else None)
            items = [
                {
                    "id": None,
                    "text": item.text,
                    "detail": item.detail,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                }
                for item in parsed.items
            ]
            note = parsed.note
            source = "parsed"
        # 4c：对比时会发的提示词（只读、只打印，从不发送）；台账落后或库还是旧版本时没有
        prompt = None
        if source == "table":
            from .decision_pairs import prompt_preview

            try:
                prompt = prompt_preview(connection, meeting["id"])
            except sqlite3.OperationalError:
                prompt = None
    finally:
        connection.close()
    if args.json:
        print(
            json.dumps(
                {
                    "meeting_id": meeting["id"],
                    "source": source,
                    "note": note,
                    "decisions": items,
                    "prompt": prompt,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"{meeting['title'] or meeting['id']}（{meeting['id']}）")
    if source == "parsed":
        print("台账还没跟上这版纪要：下面是当场解析的，还没有 id")
    if note:
        print(f"note：{note}（{decisions.NOTE_TEXT.get(note, note)}）")
    for item in items:
        span = _hhmmss(item["start_ms"])
        if item.get("end_ms") is not None:
            span += f"-{_hhmmss(item['end_ms'])}"
        print(f"{item['id'] or '（还没有 id）'}  [{span}]  {item['text']}")
        if item.get("detail"):
            for line in str(item["detail"]).splitlines():
                print(f"    {line}")
    if prompt is not None:
        print()
        if prompt["needs_call"]:
            print(
                f"对比时会发的提示词（这一场 {prompt['this']} 条、之前的 {prompt['others']} 条；只打印，不发送）："
            )
        else:
            print(
                "初筛后没有要对比的决议，也没有要放的决议：这场会不用调用 AI。下面是按现在的数据拼出的提示词（不发送）："
            )
        print("---- system ----")
        print(prompt["system"])
        print("---- user ----")
        print(prompt["user"])
    return 0


def _links_related(args: argparse.Namespace, settings: Settings) -> int:
    """links related：--meeting 印每个窗的 bar、前 6 个的得分、共同词和刷掉的原因（用存下的窗，片段向量从
    库里流式读，不用内存矩阵）；--project --stats 印项目的统计；--rebuild 只把 dirty 加一。"""
    from . import related

    if args.rebuild:
        if not (args.meeting or args.project):
            raise SystemExit("--rebuild 要配 --meeting 或 --project")
        db = _database(settings)
        with db.transaction() as connection:
            count = related.mark_dirty(connection, meeting_id=args.meeting, project_id=args.project)
        print(f"已标记 {count} 场会重算（服务开着时下一轮 H3 接着算，会议在转写时照样让路）")
        return 0
    connection = _read_only(settings)
    try:
        if args.stats:
            if not args.project:
                raise SystemExit("--stats 要配 --project")
            stats = related.project_stats(connection, settings, args.project)
            if args.json:
                print(json.dumps(stats, ensure_ascii=False, indent=2))
                return 0
            print(f"窗 {stats['windows']} 个，每窗 {stats['passages_per_window']} 段")
            print("相关线：" + ("、".join(f"{k} {v}" for k, v in stats["links"].items()) or "没有"))
            print("到处都相关：" + ("、".join(stats["hubs"]) or "没有"))
            print("副本：" + ("、".join(stats["copies"]) or "没有"))
            print(f"材料太多、没比全的会：{stats['partial_meetings']} 场")
            bar = stats["bar"]
            print(f"bar：p10 {bar['p10']}，p50 {bar['p50']}，p90 {bar['p90']}")
            return 0
        if not args.meeting:
            raise SystemExit("要给 --meeting（或 --project --stats、--rebuild）")
        encode = None
        if args.encode:
            from .semantic import SemanticIndex

            index = SemanticIndex(None, settings, busy_check=lambda: False)  # type: ignore[arg-type]
            encode = lambda texts: index.encode_texts(texts, background=False)  # noqa: E731
        try:
            result = related.explain_meeting(
                connection,
                settings,
                args.meeting,
                at=args.at,
                floor=args.floor,
                margin=args.margin,
                encode=encode,
            )
        except LookupError:
            raise SystemExit(f"没有这场会：{args.meeting}") from None
    except sqlite3.OperationalError as error:
        raise SystemExit(f"数据库还没升到 v16（先启动一次服务）：{error}") from None
    finally:
        connection.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"{result['title'] or result['meeting_id']}（{result['meeting_id']}）")
    if result.get("note"):
        print(f"note：{result['note']}")
    if result.get("copies"):
        print("这场会的副本：" + "、".join(result["copies"]))
    if result.get("hubs"):
        print("到处都相关：" + "、".join(result["hubs"]))
    if not result["windows"]:
        print("没有存下的窗（还没算过，或加 --encode 当场编码）")
    for window in result["windows"]:
        print(f"[{_hhmmss(window['start_ms'])}] bar {window['bar']:.3f}  {window['text']}")
        for item in window["candidates"]:
            words = "、".join(item.get("words") or []) or "-"
            reason = item.get("reason") or "留下"
            print(
                f"    {item['score']:.3f}  {item.get('content_key', '?')}#{item.get('ordinal', '?')}  {words}  {reason}"
            )
    return 0


def _links_ask(args: argparse.Namespace, settings: Settings) -> int:
    """links ask --project：问题从标准输入读，跑一遍 retrieve，每个来源打印类、会名或文件名、时间或位置和前
    60 个字；只读，从不调 AI。"""
    from . import ask_retrieval, llm
    from .asks import NOTE_TEXTS

    question = sys.stdin.read()
    try:
        text = ask_retrieval.clean_question(question)
    except ask_retrieval.QuestionError as error:
        print(error.text, file=sys.stderr)
        return 2
    semantic = vectors = None
    if settings.semantic_enabled:
        try:
            from .material_vectors import MaterialVectors
            from .semantic import SemanticIndex

            db = Database(settings.database_path)
            semantic = SemanticIndex(db, settings)
            # 先把模型加载好：命令行是冷启动，加载要好几秒，不先加载会吃光 2.5 秒的检索预算（页面上服务早已加载）
            semantic.warm()
            vectors = MaterialVectors(db, settings, semantic)
            vectors.refresh()
        except Exception as error:  # noqa: BLE001  模型没装好时只按原词找
            print(f"这次只按原词找（{type(error).__name__}）")
            semantic = vectors = None
    connection = _read_only(settings)
    try:
        plan = ask_retrieval.retrieve(
            connection, args.project, text, settings=settings, semantic=semantic, vectors=vectors
        )
    except ask_retrieval.ProjectMissing:
        raise SystemExit(f"没有这个项目：{args.project}") from None
    finally:
        connection.close()
    counts = plan.counts
    print(f"取的词：{'、'.join(plan.terms.phrases + plan.terms.needles) or '（没有）'}")
    print(f"找到会议里的 {counts['meetings']} 段、材料里的 {counts['materials']} 段")
    if counts["materials"]:
        print(
            f"发送时会写：将发送 {counts['materials']} 段材料原文给 {llm.destination(settings).host}"
        )
    for kind in plan.notes:
        print(NOTE_TEXTS.get(kind, kind))
    if plan.unattributed:
        print(f"另有 {plan.unattributed} 场没归项目的会也说到这些词，这次没用上")
    for source in plan.sources:
        if source["kind"] == "material":
            name, where = source["name"], source.get("loc") or ""
        else:
            name = f"{source.get('date', '')} {source.get('title') or ''}".strip()
            where = ask_retrieval.clock_text(source.get("start_ms"))
        label = ask_retrieval.KIND_NAMES[source["id"][0]]
        print(f"[{source['id']}] {label} · {name}{' · ' + where if where else ''}")
        print(f"    {source['text'][:60]}")
    return 0


def _links_words(args: argparse.Namespace, settings: Settings) -> int:
    """links words --project：打印这个项目排好序的词。--dry-run 当场挖、只读；不带就真做一遍项目汇总。"""
    from datetime import UTC, datetime

    from . import glossary_mining

    if args.dry_run:
        connection = _read_only(settings)  # 只读打开：什么都写不进去
        try:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (args.project,)
                ).fetchone()
                is None
            ):
                raise SystemExit(f"没有这个项目：{args.project}")
            stats = glossary_mining.dry_run(connection, args.project)
        finally:
            connection.close()
    else:
        db = Database(settings.database_path)
        with db.autocommit() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (args.project,)
                ).fetchone()
                is None
            ):
                raise SystemExit(f"没有这个项目：{args.project}")
            stats = glossary_mining.project_pass(connection, args.project, datetime.now(UTC))
    if stats.stale:
        print("算的时候库变了，这次没写；再跑一次")
    for rank, item in enumerate(stats.items, start=1):
        mark = "" if rank <= glossary_mining.PENDING_CAP else "（排在 30 以后，不显示）"
        print(f"{rank:>3}. {glossary_mining.describe(item)}{mark}")
    if not stats.items:
        print("这个项目还没找到词")
    return 0


def _links(args: argparse.Namespace, settings: Settings) -> int:
    if args.links_command == "words":
        return _links_words(args, settings)
    if args.links_command == "ask":
        return _links_ask(args, settings)
    if args.links_command == "status":
        return _links_status(args, settings)
    if args.links_command == "retry":
        return _links_retry(settings)
    if args.links_command == "related":
        return _links_related(args, settings)
    return _links_decisions(args, settings)


def _links_doctor(db: Database, settings: Settings) -> dict[str, Any]:
    """doctor 的 links 一项：开没开、key 文件的路径、key 能不能用、AI 的主机名。只报告，不进 required。
    路径只在 doctor 和 README 里出现，页面上不写。"""
    from .links_llm import read_usage
    from .llm import destination, llm_ready

    ready = llm_ready(settings)
    try:
        with db.autocommit() as connection:
            usage = read_usage(connection, datetime.now().date().isoformat())
    except sqlite3.Error:
        usage = {"background": None, "qa": None}
    if not settings.links_enabled:
        note = "关联整理关着（MEETING_WORKBENCH_LINKS_ENABLED=0），两个循环都不启动"
    elif not settings.links_llm_enabled:
        note = "后台不调 AI（MEETING_WORKBENCH_LINKS_LLM_ENABLED=0），只跑本机的活"
    elif not ready:
        note = f"没有 key：把 key 写进 {settings.llm_api_key_file.expanduser()}（只放 key 一行）"
    else:
        note = "ok"
    return {
        "enabled": settings.links_enabled,
        "llm_enabled": settings.links_llm_enabled,
        "key_file": str(settings.llm_api_key_file.expanduser()),
        "key_ready": ready,
        "host": destination(settings).host,
        "daily_calls": settings.links_llm_daily_calls,
        "calls_today": {"background": usage["background"], "qa": usage["qa"]},
        "note": note,
    }


def _database(settings: Settings) -> Database:
    assert settings.database_path is not None
    db = Database(settings.database_path)
    db.initialize(before_migrate=lambda: BackupManager(db, settings).create())
    return db


def _raise_open_file_limit(target: int = 4096) -> None:
    # macOS 进程默认软上限只有 256，扫描高峰叠加页面请求会 EMFILE；抬到 target（不超过硬上限）兜底。
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (ValueError, OSError):
            pass


def _material_fts_check(db: Database) -> str:
    from .material_fts import integrity_ok, rebuild_pending

    if rebuild_pending(db):
        return "rebuilding"
    with db.transaction() as connection:
        return "ok" if integrity_ok(connection) else "failed"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.command == "materials":
        return _materials(args, settings)
    if args.command == "links":
        return _links(args, settings)
    if args.command == "serve":
        _raise_open_file_limit()
        if settings.host not in {"127.0.0.1", "::1", "localhost"}:
            print("拒绝启动：工作台只允许监听 loopback", file=sys.stderr)
            return 2
        import logging

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        import uvicorn

        uvicorn.run(
            create_app(settings),
            host="127.0.0.1",
            port=settings.port,
            access_log=True,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
        return 0
    if args.command == "download-model":
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            print("请先安装 semantic 依赖：pip install -e '.[semantic]'", file=sys.stderr)
            return 1
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        SentenceTransformer(settings.semantic_model, device="cpu")
        print(json.dumps({"model": settings.semantic_model, "status": "installed"}))
        return 0
    assert settings.database_path is not None
    if args.command == "asr-evaluate":
        try:
            report = evaluate_asr(args.gold, parse_engine_specs(args.engine))
        except AsrEvaluationError as error:
            print(str(error), file=sys.stderr)
            return 2
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            output = os.path.abspath(os.path.expanduser(args.output))
            with open(output, "w", encoding="utf-8") as destination:
                destination.write(payload + "\n")
        print(payload)
        return 0
    if args.command == "asr-shadow-qwen":
        try:
            transcript = run_qwen_shadow(args.audio, args.output_dir, model=args.model)
        except AsrEvaluationError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps({"transcript": str(transcript)}, ensure_ascii=False))
        return 0
    if args.command == "asr-export-gold":
        db = _database(settings)
        try:
            count = export_gold_jsonl(db, Path(args.output), meeting_id=args.meeting_id)
        except (GoldExportError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps({"status": "exported", "samples": count}, ensure_ascii=False))
        return 0
    if args.command == "backup":
        if not settings.database_path.is_file():
            print("数据库尚不存在，无法备份", file=sys.stderr)
            return 1
        db = Database(settings.database_path)
        result = BackupManager(db, settings).create()
        print(
            json.dumps(
                {
                    "local_path": str(result.local_path),
                    "mirror_path": str(result.mirror_path) if result.mirror_path else None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "verify-audio":
        if not settings.database_path.is_file():
            print("数据库尚不存在，无法核验原音频", file=sys.stderr)
            return 2
        db: Database | None = None
        try:
            db = _database(settings)
            result = AudioIntegrityVerifier(db, settings).verify()
        except (AudioIntegrityError, OSError, sqlite3.Error) as error:
            if db is not None:
                try:
                    # 外置盘没插时巡检每 5 分钟被拉起一次：上一条已经是同一原因的失败就不再记，
                    # 成功执行过一次、或原因变了才再记。
                    last = last_audio_integrity_result(db) or {}
                    if (
                        last.get("status") != "failed"
                        or last.get("error_type") != type(error).__name__
                        or last.get("detail") != str(error)
                    ):
                        db.add_event(
                            "audio_integrity_failed",
                            actor="system",
                            payload={
                                "status": "failed",
                                "error_type": type(error).__name__,
                                "detail": str(error),
                                "completed_at": utc_now(),
                            },
                        )
                except (OSError, sqlite3.Error):
                    pass
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0 if result.issues == 0 else 1
    db = _database(settings)
    if args.command == "scan":
        report = ArchiveImporter(db, settings).scan()
        print(
            json.dumps(
                {name: getattr(report, name) for name in report.__dataclass_fields__},
                ensure_ascii=False,
            )
        )
        return 0 if report.errors == 0 else 1
    if args.command == "semantic-index":
        try:
            indexed = SemanticIndex(db, settings).rebuild(force=args.force)
        except (SemanticPaused, SemanticUnavailable) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps({"indexed": indexed}, ensure_ascii=False))
        return 0
    if args.command == "doctor":
        checks = {
            "database": db.query_one("PRAGMA integrity_check")["integrity_check"] == "ok",
            "archive": settings.archive_root.is_dir(),
            "staging": settings.staging_root.is_dir(),
            "loopback": settings.host == "127.0.0.1",
            "last_audio_verification": last_audio_integrity_result(db),
            # 第三期：材料读取用到的程序，只报告、不进 required（装机脚本最后会跑 doctor）
            "materials": tools_report(settings),
            # 3f：材料全文表和片段对得上（恢复备份后还没补完时写 rebuilding）
            "material_fts": _material_fts_check(db),
            # 第四期：关联整理开没开、key 在哪、能不能用、AI 的主机名（只报告，不进 required）
            "links": _links_doctor(db, settings),
        }
        print(json.dumps(checks, ensure_ascii=False))
        required = (checks["database"], checks["archive"], checks["staging"], checks["loopback"])
        return 0 if all(required) else 1
    if args.command == "backfill-projects" and args.evaluate:
        return _print_evaluation(
            ProjectLinker(db, settings).evaluate(limit=args.limit, apply=not args.no_apply)
        )
    if args.command == "backfill-projects":
        linker = ProjectLinker(db, settings)
        result = linker.backfill(dry_run=args.dry_run, limit=args.limit)
        if not result["dry_run"] and not result.get("llm_ready"):
            print(
                "未配置 LLM API key：只按字面线索判断；没认出的会议记为「没认出」，"
                "key 配好后下一轮扫描会让 LLM 再判一次",
                file=sys.stderr,
            )
        print(f"{'会议标题':<30}{'项目':<20}{'方法':<14}原因")
        for item in result["results"]:
            project = item["project_name"]
            if not project and item.get("candidates"):
                project = "待你选：" + " / ".join(
                    str(candidate.get("project_name") or "") for candidate in item["candidates"]
                )
            if not project and item.get("new_project_name"):
                project = f"像新项目：{item['new_project_name']}"
            if item.get("new_requirement_name"):
                project = f"{project or ''}（像新需求：{item['new_requirement_name']}）"
            print(
                f"{(item['meeting_title'] or ''):<30}"
                f"{(project or '（未归类）'):<20}"
                f"{(item['method'] or '-'):<14}"
                f"{item['reason']}"
            )
        if not result["dry_run"]:
            summary = ", ".join(f"{k}:{v}" for k, v in result["method_counts"].items()) or "无"
            print(
                f"汇总：{summary}；待你选:{result['needs_review']}；没认出:{result['unresolved']}"
            )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
