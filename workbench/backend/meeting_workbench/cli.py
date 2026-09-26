from __future__ import annotations

import argparse
from dataclasses import asdict
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
from .db import Database, utc_now
from .importer import ArchiveImporter
from .integrity import AudioIntegrityError, AudioIntegrityVerifier, last_audio_integrity_result
from .gold_export import GoldExportError, export_gold_jsonl
from .main import create_app
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
    asr_evaluate = subcommands.add_parser(
        "asr-evaluate", help="用人工金标比较多个 ASR 引擎"
    )
    asr_evaluate.add_argument("--gold", required=True)
    asr_evaluate.add_argument(
        "--engine", action="append", required=True, metavar="NAME=DIR"
    )
    asr_evaluate.add_argument("--output")
    asr_export = subcommands.add_parser(
        "asr-export-gold", help="从工作台导出 ASR 金标 JSONL"
    )
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
    backfill_projects.add_argument(
        "--dry-run", action="store_true", help="只打印判定结果，不写库"
    )
    backfill_projects.add_argument(
        "--evaluate",
        action="store_true",
        help="回测：拿人工归过项目的会当答案，藏起答案重判；模型高置信错 2 场以上时要求字面线索",
    )
    backfill_projects.add_argument(
        "--no-apply", action="store_true", help="和 --evaluate 一起用：只报告，不改 link_require_literal"
    )
    backfill_projects.add_argument("--limit", type=int, help="最多处理的会议数")

    materials_cmd = subcommands.add_parser("materials", help="项目材料：盘点、图片文字识别试跑（只读）")
    materials_sub = materials_cmd.add_subparsers(dest="materials_command", required=True)
    walk = materials_sub.add_parser(
        "walk", help="走一遍项目材料文件夹，统计第三期要索引的量（只读，不改数据库）"
    )
    walk.add_argument("--dry-run", action="store_true", help="只盘点，不建索引（目前只支持这一种）")
    walk.add_argument("--project", help="只看这个项目（项目 id 或名字）")
    walk.add_argument(
        "--root", action="append", type=Path, help="只看这个文件夹，可以给多次；给了就不读数据库里挂的"
    )
    walk.add_argument("--no-probe", action="store_true", help="不读音视频时长")
    walk.add_argument("--json", type=Path, help="把完整结果另存成 JSON")
    ocr = materials_sub.add_parser(
        "ocr-trial", help="挑一些材料图片，分别用 Vision 和 tesseract 识别，比较用时和效果（在 Mac 上跑）"
    )
    ocr.add_argument("--project", help="只从这个项目的材料里挑（项目 id 或名字）")
    ocr.add_argument("--root", action="append", type=Path, help="只从这个文件夹里挑，可以给多次")
    ocr.add_argument("--limit", type=int, default=20, help="挑几张图，默认 20")
    ocr.add_argument(
        "--engine", action="append", choices=["vision", "tesseract"], help="只跑某一个，默认两个都跑"
    )
    ocr.add_argument("--out", type=Path, help="结果写到哪个文件夹，默认数据目录下的 ocr-trial/")
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
            guess = " / ".join(str(candidate.get("project_name") or "") for candidate in item["candidates"])
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
    if result.get("skipped_untrusted"):
        print(f"另有 {result['skipped_untrusted']} 场人工归属没算进来（先被 AI 归、后来只是确认，或没有纪要）")
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
        raise SystemExit(f"找不到声档数据库：{settings.database_path}；可以用 --root 直接指定文件夹")
    connection = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        projects = [dict(row) for row in connection.execute("SELECT id, name FROM projects ORDER BY name")]
        chosen = None
        if project:
            wanted = unicodedata.normalize("NFKC", project).casefold().strip()
            chosen = next(
                (
                    item for item in projects
                    if item["id"] == project
                    or unicodedata.normalize("NFKC", item["name"]).casefold().strip() == wanted
                ),
                None,
            )
            if chosen is None:
                names = "、".join(item["name"] for item in projects) or "（还没有项目）"
                raise SystemExit(f"没有叫「{project}」的项目。现有项目：{names}")
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


def _materials(args: argparse.Namespace, settings: Settings) -> int:
    from . import material_walk, ocr_trial

    roots = _material_roots(settings, args.project, args.root)
    if args.materials_command == "walk":
        if not args.dry_run:
            print("现在只做盘点：请加 --dry-run。真正建材料索引是第三期的事。", file=sys.stderr)
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
    out_dir = (args.out.expanduser() if args.out else ocr_trial.default_out_dir(settings.data_dir))
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
            progress=lambda done, total: print(f"已识别 {done}/{total} 张…", file=sys.stderr, flush=True),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    (out_dir / "结果.md").write_text(ocr_trial.render_markdown(report, notes), encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(ocr_trial.render_summary(report, out_dir))
    return 0


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.command == "materials":
        return _materials(args, settings)
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
            transcript = run_qwen_shadow(
                args.audio, args.output_dir, model=args.model
            )
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
