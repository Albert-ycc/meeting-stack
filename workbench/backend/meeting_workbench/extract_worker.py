"""文档正文的读取进程（第三期 3b）。

子进程这边：`python -m meeting_workbench.extract_worker` 常驻，一行一个请求 `{id, path, ext, layer}`，
一行一个回答 `{id, status, blocks: [{loc, text}], chars, pages, truncated, extractor, extractor_version}`。
status 是 ok、io_error、password、corrupt、unsupported、permission、timeout、waiting（textutil 没有），
或 relayer（看开头字节其实是 PDF，服务把这份内容换到 PDF 层）。上限都在这里执行：20 万字、回答 2MB。

服务这边：TextExtractor 按 3a 的共同规矩管这个进程，每个文件 60 秒超时，处理超时的下次用 120 秒。
"""

from __future__ import annotations

import json
import logging
import struct
import sys
import time
import traceback
import zipfile
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import extract_formats as formats
from .material_rules import LAYER_PDF, LAYER_TEXT

EXTRACTOR = "stdlib"
EXTRACTOR_VERSION = 1
ANSWER_LIMIT_BYTES = 2 * 1024 * 1024
FIRST_TIMEOUT = 60.0
RETRY_TIMEOUT = 120.0

logger = logging.getLogger(__name__)

# 解析失败：直接算文件损坏
_PARSE_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    ET.ParseError,
    formats.CFBError,
    struct.error,
    UnicodeError,
    ValueError,
    IndexError,
    KeyError,
    TypeError,
    RecursionError,
)


# ---------------------------------------------------------------------- 子进程这边


def read_document(path: Path, ext: str, out: formats.Collector) -> str | None:
    """按实际内容读一份文档。返回 None 表示读完；返回层名表示该换层（其实是 PDF）。"""
    kind = formats.sniff(path, ext)
    if kind == "pdf":
        return LAYER_PDF
    if kind == "iwork":
        raise formats.Unreadable(formats.UNSUPPORTED, "iWork")
    if kind == "binary":
        raise formats.Unreadable(formats.UNSUPPORTED, "扩展名是文字，内容是二进制")
    if kind == "plain":
        formats.read_plain(path, out)
    elif kind == "csv":
        formats.read_csv(path, out, ext=ext)
    elif kind == "ipynb":
        formats.read_ipynb(path, out)
    elif kind == "email":
        formats.read_email(path, out)
    elif kind == "html":
        formats.read_html(path, out)
    elif kind == "zip":
        formats.read_zip(path, out, ext=ext)
    elif kind == "cfb":
        formats.read_cfb(path, out, ext=ext, textutil=None)
    elif kind in {"rtf", "rtfd"}:
        formats.read_textutil(path, out, fmt=kind)
    else:
        raise formats.Unreadable(formats.UNSUPPORTED, kind)
    return None


def _status(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
        **extra,
    }


def fit_answer(answer: dict[str, Any], limit: int = ANSWER_LIMIT_BYTES) -> dict[str, Any]:
    """一个回答不超过 2MB：超了就从后往前丢块，记 truncated。"""
    size = len(json.dumps(answer, ensure_ascii=False).encode("utf-8"))
    if size <= limit:
        return answer
    blocks = list(answer.get("blocks") or [])
    while blocks and size > limit:
        dropped = blocks.pop()
        size -= len(json.dumps(dropped, ensure_ascii=False).encode("utf-8")) + 2
    return {
        **answer,
        "blocks": blocks,
        "chars": sum(len(block["text"]) for block in blocks),
        "truncated": True,
    }


def handle(request: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(request.get("path") or ""))
    ext = str(request.get("ext") or "").lower()
    layer = str(request.get("layer") or LAYER_TEXT)
    out = formats.Collector()
    started = time.monotonic()
    try:
        relayer = read_document(path, ext, out)
        if relayer and relayer != layer:
            return _status("relayer", layer=relayer)
        if relayer:
            raise formats.Unreadable(formats.UNSUPPORTED, "这一层读不了")
    except formats.Full:
        pass
    except formats.Unreadable as error:
        print(f"{path}: {error.reason} {error}", file=sys.stderr)
        return _status(error.reason)
    except formats.EngineMissing as error:
        return _status("waiting", what=error.what)
    except TimeoutError:
        return _status("timeout")
    except PermissionError:
        return _status("permission")
    except RuntimeError as error:
        # zipfile 遇到加密成员抛 RuntimeError；别的 RuntimeError 当解析失败
        if "encrypted" in str(error).lower() or "password" in str(error).lower():
            return _status(formats.PASSWORD)
        traceback.print_exc(file=sys.stderr)
        return _status(formats.CORRUPT)
    except NotImplementedError:
        return _status(formats.UNSUPPORTED)  # 压缩包用了不支持的压缩方式
    except OSError as error:
        print(f"{path}: io_error {error!r}", file=sys.stderr)
        return _status("io_error")
    except _PARSE_ERRORS:
        traceback.print_exc(file=sys.stderr)
        return _status(formats.CORRUPT)
    return fit_answer(
        _status(
            "ok",
            blocks=out.blocks,
            chars=out.chars,
            pages=out.pages,
            truncated=out.truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )


def main() -> None:
    from .material_helpers import serve_json_lines

    sys.setrecursionlimit(5000)
    serve_json_lines(handle)


# ---------------------------------------------------------------------- 服务这边


class TextExtractor:
    """服务这边的文档正文读取器：MaterialContent 的 extractors["text"]。"""

    name = EXTRACTOR
    version = EXTRACTOR_VERSION

    def __init__(
        self,
        data_dir: Path,
        *,
        stop: Any = None,
        python: str = sys.executable,
        helper_factory: Any = None,
        first_timeout: float = FIRST_TIMEOUT,
        retry_timeout: float = RETRY_TIMEOUT,
    ):
        self.data_dir = Path(data_dir)
        self.stop = stop
        self.python = python
        self.first_timeout = first_timeout
        self.retry_timeout = retry_timeout
        self._factory = helper_factory or self._default_helper
        self._helper: Any = None

    def _default_helper(self) -> Any:
        from .material_helpers import HelperProcess

        package_root = str(Path(__file__).resolve().parent.parent)
        env = {"PYTHONPATH": package_root, "PYTHONDONTWRITEBYTECODE": "1"}
        return HelperProcess(
            "extract",
            [self.python, "-m", "meeting_workbench.extract_worker"],
            data_dir=self.data_dir,
            env=env,
            stop=self.stop,
        )

    def timeout_for(self, row: dict[str, Any]) -> float:
        return self.retry_timeout if row.get("reason") == "timeout" else self.first_timeout

    def __call__(self, path: Path, layer: str, row: dict[str, Any]) -> Any:
        from .material_content import ExtractResult
        from .material_helpers import HelperCrashed, HelperTimeout

        if self._helper is None:
            self._helper = self._factory()
        started = time.monotonic()
        try:
            answer = self._helper.request(
                {"path": str(path), "ext": row.get("ext") or "", "layer": layer},
                timeout=self.timeout_for(row),
            )
        except (HelperTimeout, HelperCrashed) as error:
            logger.info("material text extract timed out or crashed for %s: %s", path, error)
            return ExtractResult(
                status="timeout", extractor=self.name, extractor_version=self.version
            )
        status = str(answer.get("status") or "corrupt")
        if status == "error":
            logger.warning("material text extract failed for %s: %s", path, answer.get("error"))
            status = "corrupt"
        blocks = [
            {"loc": block.get("loc"), "text": str(block.get("text") or "")}
            for block in answer.get("blocks") or []
            if isinstance(block, dict)
        ]
        return ExtractResult(
            status=status,
            blocks=blocks,
            chars=int(answer.get("chars") or 0),
            pages=answer.get("pages"),
            duration_ms=int(answer.get("duration_ms") or (time.monotonic() - started) * 1000),
            truncated=bool(answer.get("truncated")),
            extractor=str(answer.get("extractor") or self.name),
            extractor_version=self.version,
            what=answer.get("what"),
            layer=answer.get("layer"),
        )

    def close(self) -> None:
        if self._helper is not None:
            self._helper.close()
            self._helper = None


if __name__ == "__main__":
    main()
