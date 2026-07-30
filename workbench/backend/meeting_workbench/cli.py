from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import sqlite3
import sys
from pathlib import Path

from .asr_eval import AsrEvaluationError, evaluate_asr, parse_engine_specs, run_qwen_shadow
from .backup import BackupManager
from .config import Settings
from .db import Database, utc_now
from .importer import ArchiveImporter
from .integrity import AudioIntegrityError, AudioIntegrityVerifier, last_audio_integrity_result
from .gold_export import GoldExportError, export_gold_jsonl
from .main import create_app
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
    return parser


def _database(settings: Settings) -> Database:
    assert settings.database_path is not None
    db = Database(settings.database_path)
    db.initialize(before_migrate=lambda: BackupManager(db, settings).create())
    return db


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.command == "serve":
        if settings.host not in {"127.0.0.1", "::1", "localhost"}:
            print("拒绝启动：工作台只允许监听 loopback", file=sys.stderr)
            return 2
        import uvicorn

        uvicorn.run(
            create_app(settings),
            host="127.0.0.1",
            port=settings.port,
            access_log=False,
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
