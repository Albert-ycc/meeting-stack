"""relay 读工作台那套 .env。

watchdog、relayctl 作为命令启动时调 load_env_files()，把仓库根和 workbench/ 下的 .env 里 relay 自己
用的键补进进程的环境变量。规矩：

- 找法和工作台 config.py 的 ENV_FILES 一致：仓库根一份、workbench/ 下一份，两份都有时 workbench/ 的优先；
- 已经在环境变量里的值优先，.env 不覆盖：启动命令里显式给的值、工作台起 relayctl 时传的值照旧生效；
- 只补 relay 自己读的键（MEETING_RELAY_*、RELAY_*、TRANSCRIBE_ENGINE）。.env 是工作台那套，里头有飞书
  webhook、AI key 文件路径这些，relay 和它起的子进程（转写、tmux、lark-cli）不该白得一份；
- MEETING_RELAY_PROJECT_HINT 永远不从文件取：它是工作台每次入队、重试时按场次传的值，还特意先摘掉继承来的；
- 不引入依赖，写法只认常用的一小撮：KEY=VALUE、# 注释、export 前缀、单双引号、行尾 # 注释。不加引号且以 ~
  开头的值会展开（原来文档让用 source ./.env 导入，shell 会展开；转写脚本拿到环境变量里的 ~ 不会再展开）；
  不展开 $变量；空值等于没写（导出空串，float("") 会让 watchdog 一启动就抛）。

只在入口调：被 import、被用例直接调 main() 时都不读，生产 checkout 里的真 .env 带不进用例。
"""

import os
import re
import sys
from pathlib import Path
from typing import Iterable, MutableMapping

# 仓库根：本文件位于 <repo>/relay/quickstart/
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = (REPO_ROOT / ".env", REPO_ROOT / "workbench" / ".env")

RELAY_KEY_PREFIXES = ("MEETING_RELAY_", "RELAY_")
RELAY_KEY_NAMES = frozenset({"TRANSCRIBE_ENGINE"})
NEVER_FROM_FILE = frozenset({"MEETING_RELAY_PROJECT_HINT"})

_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")


def _is_relay_key(name: str) -> bool:
    if name in NEVER_FROM_FILE:
        return False
    return name.startswith(RELAY_KEY_PREFIXES) or name in RELAY_KEY_NAMES


def _parse_value(raw: str) -> str | None:
    """None 表示这行解析不了（引号没闭合），按没写处理。"""
    stripped = raw.strip()
    if stripped[:1] in ("'", '"'):
        end = stripped.find(stripped[0], 1)
        return None if end == -1 else stripped[1:end]
    value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if value == "~" or value.startswith("~/"):
        value = os.path.expanduser(value)
    return value


def _merge_file(path: Path, merged: dict[str, str]) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as error:
        # 只说是哪个文件、什么错，不带内容：.env 里有 key
        print(
            f"relay：读不了 {path.parent.name}/{path.name}（{type(error).__name__}），已跳过",
            file=sys.stderr,
        )
        return
    for line in text.splitlines():
        # 带 NUL 的值写进 os.environ 会抛 ValueError，一份坏掉的 .env 不该让守护起不来
        match = None if "\x00" in line else _ASSIGNMENT.fullmatch(line)
        if match is None:
            continue
        value = _parse_value(match.group(2))
        if value:
            merged[match.group(1)] = value
        elif value is not None:
            merged.pop(match.group(1), None)


def load_env_files(
    files: Iterable[Path] = ENV_FILES,
    environ: MutableMapping[str, str] = os.environ,
) -> list[str]:
    """把 .env 里 relay 自己读、且环境变量里还没有的键补进 environ，返回补进去的键名（不含值）。"""
    merged: dict[str, str] = {}
    for path in files:
        _merge_file(path, merged)
    applied = []
    for name, value in merged.items():
        if _is_relay_key(name) and name not in environ:
            environ[name] = value
            applied.append(name)
    return sorted(applied)
