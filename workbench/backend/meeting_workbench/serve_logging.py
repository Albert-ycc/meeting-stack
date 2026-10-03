"""serve 的日志：访问日志去掉查询串、成功的轮询不记；可以写进自己管理的轮转文件。"""

from __future__ import annotations

import copy
import logging
import logging.config
from pathlib import Path
from typing import Any

from uvicorn.config import LOGGING_CONFIG

from .config import Settings

# 前端每个开着的标签页每 5 秒（不在任务页、也没有进行中的任务时 15 秒）轮询这五个接口（App.tsx 的 refresh）。
# 一个标签页一天就是几万行访问日志，把真正要看的行淹没了。成功的不记，出错（非 2xx、304）照记。
# 只按路径认：/api/tasks、/api/glossary/suggestions 在别的页面也会被读，那几条成功的同样不记。
QUIET_POLL_PATHS = frozenset(
    {
        "/api/health",
        "/api/attention",
        "/api/jobs",
        "/api/tasks",
        "/api/glossary/suggestions",
    }
)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class AccessLogFilter(logging.Filter):
    """挂在 uvicorn.access 上。它发出的记录参数是（客户端、方法、带查询串的路径、HTTP 版本、状态码）：
    查询串里有检索词、文件路径，不能进日志；成功的轮询整条不记。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        client, method, target, version, status = args
        path = str(target).partition("?")[0]
        succeeded = 200 <= int(status) < 300 or int(status) == 304
        if method == "GET" and path in QUIET_POLL_PATHS and succeeded:
            return False
        record.args = (client, method, path, version, status)
        return True


def build_log_config(settings: Settings) -> dict[str, Any]:
    """在 uvicorn 自带的日志配置上改：没配 log_file 时和原来一样（应用日志走标准错误，访问日志走标准输出），
    配了就让 root、uvicorn、uvicorn.access 都写进同一个按大小轮转的文件。"""
    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {"access_log": {"()": AccessLogFilter}}
    config["formatters"]["app"] = {"format": LOG_FORMAT}
    config["loggers"]["uvicorn.access"]["filters"] = ["access_log"]
    if settings.log_file is None:
        config["handlers"]["app"] = {
            "class": "logging.StreamHandler",
            "formatter": "app",
            "stream": "ext://sys.stderr",
        }
        config["root"] = {"level": "INFO", "handlers": ["app"]}
        return config
    config["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "app",
        "filename": str(settings.log_file),
        "maxBytes": settings.log_max_bytes,
        "backupCount": settings.log_backup_count,
        "encoding": "utf-8",
    }
    config["root"] = {"level": "INFO", "handlers": ["file"]}
    for name in ("uvicorn", "uvicorn.access"):
        config["loggers"][name]["handlers"] = ["file"]
    return config


class LogFileError(Exception):
    """MEETING_WORKBENCH_LOG_FILE 指的地方写不了。消息是给人看的：哪项配置、指到了什么、怎么改。"""


def _open_log_file(path: Path) -> None:
    """照轮转文件的方式先打开一次。指到目录、没有写权限时 dictConfig 只报「Unable to configure handler
    'file'」和一大段 traceback，看不出是哪项配置、哪里不对；这里换成一句人话，照旧起不来。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as error:
        if path.is_dir():
            problem = f"是一个目录。改成目录里的一个文件，比如 {path / 'web.log'}"
        elif isinstance(error, PermissionError):
            problem = f"写不进去：当前用户没有权限写 {error.filename}。换一个能写的位置，或者改那个目录的权限"
        elif isinstance(error, (FileExistsError, NotADirectoryError)):
            problem = f"用不了：路径中间的 {error.filename} 不是目录。换一个路径"
        else:
            problem = f"打不开：{error.strerror or error}"
        raise LogFileError(
            f"日志文件 MEETING_WORKBENCH_LOG_FILE={path} {problem}"
            "（在环境变量或 .env 里改；这一项删掉的话，日志记到标准错误）"
        ) from None


def configure_serve_logging(settings: Settings) -> None:
    if settings.log_file is not None:
        _open_log_file(settings.log_file)
    logging.config.dictConfig(build_log_config(settings))
