"""serve 的日志：访问日志去掉查询串、成功的轮询不记；可以写进自己管理的轮转文件。"""

from __future__ import annotations

import copy
import logging
import logging.config
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


def configure_serve_logging(settings: Settings) -> None:
    if settings.log_file is not None:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(build_log_config(settings))
