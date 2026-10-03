#!/bin/zsh
# 一键本地安装：建 venv、装依赖、跑测试、下载语义模型、初始化索引。
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT="${SCRIPT_DIR:h}"          # <repo>/workbench
cd "$ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --requirement requirements.lock
.venv/bin/python -m pip install --no-deps -e .
# 卡片监听和仓库根下这几个目录跟后端用同一套 ruff 规则（各目录的 ruff.toml 指向 workbench/pyproject.toml）
LINT_PATHS=(backend card_listener.py ../relay ../glossary ../transcribe ../task-notify)
.venv/bin/ruff check "${LINT_PATHS[@]}"
.venv/bin/ruff format --check "${LINT_PATHS[@]}"
.venv/bin/pytest backend/tests
# relay 的用例要用装了 watchdog 的 Python 跑（relay/README.md「Python 依赖」）：没指定 RELAY_PYTHON 时用
# docs/install.md 里建的 ~/.venvs/relay，还没建就用 python3。录音监听排在工作台后面装，导入不了 watchdog 就先跳过、说一声。
if [[ -z "${RELAY_PYTHON:-}" ]]; then
  RELAY_PYTHON="$HOME/.venvs/relay/bin/python"
  [[ -x "$RELAY_PYTHON" ]] || RELAY_PYTHON=python3
fi
if "$RELAY_PYTHON" -c "import watchdog" 2>/dev/null; then
  (cd ../relay && PYTHONDONTWRITEBYTECODE=1 "$RELAY_PYTHON" -m unittest discover -s tests)
else
  print -u2 "跳过 relay 用例：$RELAY_PYTHON 导入不了 watchdog。按 docs/install.md「四、运行」装好后重跑本脚本，或用 RELAY_PYTHON 指定装了它的 Python"
fi
cd frontend
npm ci
npm run test
npm run typecheck
npm run build
cd "$ROOT"
.venv/bin/meeting-workbench download-model
if [[ -f "${MEETING_WORKBENCH_DATABASE_PATH:-$HOME/.meeting-workbench/workbench.sqlite3}" ]]; then
  .venv/bin/meeting-workbench backup
fi
.venv/bin/meeting-workbench scan
.venv/bin/meeting-workbench semantic-index
.venv/bin/meeting-workbench doctor
