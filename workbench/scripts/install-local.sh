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
.venv/bin/ruff check backend
.venv/bin/ruff format --check backend
.venv/bin/pytest backend/tests
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
