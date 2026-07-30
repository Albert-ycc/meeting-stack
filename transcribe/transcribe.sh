#!/usr/bin/env bash
# 会议音频转写脚本 v4（2026-07-08：FunASR 双跑观察期，引擎开关见下方 ENGINE 段）
#
# 用法：
#   bash transcribe.sh <音频文件>            # 完整流程：建文件夹 + 移音频 + 转写
#   bash transcribe.sh <已有文件夹>          # 只跑 HTML 转换（适合事后写完纪要再跑一次）
#   bash transcribe.sh <音频> <prompt 文件>  # 指定自定义词典（默认用同级 prompt.txt）
#   TRANSCRIBE_ENGINE=whisper bash transcribe.sh <音频>   # 回切纯 whisper（v3 行为）
#
# 规则：每个录音的所有产出（音频/逐字稿/字幕/JSON/纪要/HTML）统一放在一个以
# 录音名命名的文件夹里，避免多场会议产物混在一起。
#
# 完整流程产出（默认 observe 模式）：
#   <录音名>/
#     ├── <录音名>.m4a         原始音频
#     ├── <录音名>.txt         逐字稿（FunASR 主稿，每句一行）
#     ├── <录音名>.srt         带时间轴字幕（FunASR）
#     ├── <录音名>.spk.txt     带说话人分组的转写（[spkN] [mm:ss] 前缀，cam++）
#     ├── <录音名>.funasr.json 结构化数据
#     ├── funasr.log           FunASR 转写日志（含实际采用的热词表，可审计）
#     ├── whisper-ref/         whisper 对照稿（后台生成，交叉核字母类术语用）
#     ├── 会议纪要.md          ← 后期人工/AI 写
#     └── 会议纪要.html        ← 写完后再跑一次脚本自动生成

set -eo pipefail

INPUT="$1"
PROMPT_FILE="$2"

if [ -z "$INPUT" ]; then
  cat <<'EOF' >&2
Usage:
  bash transcribe.sh <audio_file>       # 完整流程
  bash transcribe.sh <folder>           # 只转 HTML
  bash transcribe.sh <audio> <prompt>   # 指定词典
EOF
  exit 1
fi

PANDOC="$(command -v pandoc || echo /opt/homebrew/bin/pandoc)"
PY="${MEETING_RELAY_PYTHON:-$(command -v python3)}"

# GitHub 风格 CSS（移动端可读、表格清楚、引用块明显）
CSS="body{font-family:-apple-system,'PingFang SC',sans-serif;max-width:880px;margin:40px auto;padding:0 24px;line-height:1.7;color:#222}h1,h2,h3{border-bottom:1px solid #eee;padding-bottom:8px}table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #ddd;padding:8px 12px;text-align:left}th{background:#f6f8fa}code{background:#f6f8fa;padding:2px 6px;border-radius:3px;font-family:Menlo,monospace;font-size:90%}blockquote{border-left:4px solid #ddd;color:#666;padding:0 16px;margin:16px 0}"

# 把会议纪要 .md 转 HTML 的函数
gen_html() {
  local folder="$1"
  local md="$folder/会议纪要.md"
  local html="$folder/会议纪要.html"
  local name="$(basename "$folder")"

  if [ ! -f "$md" ]; then
    echo "No 会议纪要.md found in $folder, skipping HTML generation."
    return 0
  fi

  if [ ! -x "$PANDOC" ]; then
    echo "WARN: pandoc not found, skipping HTML generation."
    return 0
  fi

  "$PANDOC" "$md" \
    --from=gfm \
    --to=html5 \
    --standalone \
    --metadata title="$name" \
    --css="data:text/css,$CSS" \
    -o "$html"
  echo "HTML generated: $html"
}

# === 模式 1：传入的是文件夹 → 只跑 HTML 转换 ===
if [ -d "$INPUT" ]; then
  gen_html "$INPUT"
  exit 0
fi

# === 模式 2：传入的是音频文件 → 完整流程 ===
if [ ! -f "$INPUT" ]; then
  echo "File not found: $INPUT" >&2
  exit 1
fi

AUDIO="$INPUT"
AUDIO_DIR="$(dirname "$AUDIO")"
AUDIO_BASE="$(basename "$AUDIO")"
AUDIO_NAME="${AUDIO_BASE%.*}"

# prompt 默认位置：音频同级目录的 prompt.txt
if [ -z "$PROMPT_FILE" ]; then
  PROMPT_FILE="$AUDIO_DIR/prompt.txt"
fi

# 建专属文件夹（用录音名命名），把音频搬进去
WORK_DIR="$AUDIO_DIR/$AUDIO_NAME"
mkdir -p "$WORK_DIR"

if [ "$(cd "$AUDIO_DIR" && pwd)" != "$(cd "$WORK_DIR" && pwd)" ]; then
  echo "Moving audio into: $WORK_DIR/"
  mv "$AUDIO" "$WORK_DIR/"
  AUDIO="$WORK_DIR/$AUDIO_BASE"
fi

# 读 prompt
if [ -f "$PROMPT_FILE" ]; then
  PROMPT_CONTENT="$(cat "$PROMPT_FILE")"
  echo "Using prompt: $PROMPT_FILE"
else
  PROMPT_CONTENT=""
  echo "WARN: no prompt file, recognition quality will be lower"
fi

echo "Transcribing: $AUDIO_BASE"
echo "Output to:    $WORK_DIR/"

# === 转写引擎（2026-07-08 起进入 FunASR 双跑观察期） ===
# TRANSCRIBE_ENGINE 三个值：
#   observe（默认）= FunASR 出主稿（<名>.txt/.srt/.spk.txt），whisper 转入后台
#                    出对照稿到 whisper-ref/ 子目录（指代对齐时交叉核字母类术语）
#   funasr          = 只跑 FunASR
#   whisper         = 只跑 openai-whisper，行为与 v3 完全一致（一行回切开关）
#
# 依据 2026-07-08 三场真实会议 PoC 盲评（产物归档 funasr-poc-260708/）：
#   FunASR 错字少约 31%、数字/金额轴系统性更准、非自回归无整段水印幻觉
#   （whisper 曾在 260708 场吞掉约 4 分钟真实讨论）、cam++ 说话人分离主干可用、
#   速度约 5 倍实时。已知弱点：字母类术语（LV2/SCRM/UMU）不如 whisper，
#   靠 whisper-ref 对照稿交叉核。
# whisper 侧沿用 2026-06-29 教训：openai-whisper 带温度回退，长音频不幻觉；
# mlx 单温度无 fallback 已两次翻车（备份 transcribe.sh.mlx-bak-260702）。
ENGINE="${TRANSCRIBE_ENGINE:-observe}"
# 两个引擎的可执行文件位置：优先环境变量，其次 PATH，最后回退到常见安装位置。
WHISPER="${MEETING_RELAY_WHISPER_BIN:-$(command -v whisper || echo "$HOME/.venvs/whisper/bin/whisper")}"
FUNASR_PY="${MEETING_RELAY_FUNASR_PYTHON:-$HOME/.venvs/funasr/bin/python}"
FUNASR_RUNNER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/funasr_transcribe.py"
[ -f "$FUNASR_RUNNER" ] || FUNASR_RUNNER="${MEETING_RELAY_FUNASR_RUNNER:-$HOME/MeetingArchive/funasr_transcribe.py}"

# FunASR 环境缺失时大声回落 whisper，绝不静默失败
if [ "$ENGINE" != "whisper" ] && { [ ! -x "$FUNASR_PY" ] || [ ! -f "$FUNASR_RUNNER" ]; }; then
  echo "WARN: FunASR 环境缺失（$FUNASR_PY / $FUNASR_RUNNER），回落 whisper 引擎" | tee "$WORK_DIR/funasr.log"
  ENGINE="whisper"
fi

run_whisper() {
  local outdir="$1"
  mkdir -p "$outdir"
  "$WHISPER" "$AUDIO" \
    --model turbo \
    --language zh \
    --output_format all \
    --output_dir "$outdir" \
    --verbose False \
    --initial_prompt "$PROMPT_CONTENT" \
    > "$outdir/whisper.log" 2>&1
}

run_funasr() {
  "$FUNASR_PY" "$FUNASR_RUNNER" "$AUDIO" "$WORK_DIR" "$AUDIO_NAME" "$PROMPT_FILE" \
    > "$WORK_DIR/funasr.log" 2>&1
}

case "$ENGINE" in
  whisper)
    run_whisper "$WORK_DIR"
    ;;
  funasr)
    run_funasr
    ;;
  observe)
    run_funasr
    # whisper 对照稿转后台，不阻塞主稿交付；完成与否看 whisper-ref/whisper.log
    ( run_whisper "$WORK_DIR/whisper-ref" ) &
    disown
    echo "whisper 对照稿后台生成中 -> $WORK_DIR/whisper-ref/"
    ;;
  *)
    echo "Unknown TRANSCRIBE_ENGINE: $ENGINE（可选 observe/funasr/whisper）" >&2
    exit 1
    ;;
esac

echo "Transcription done. (engine=$ENGINE)"

# 如果用户已经预先放了 会议纪要.md，顺手转 HTML
gen_html "$WORK_DIR"

echo
echo "All files in: $WORK_DIR/"
ls -lh "$WORK_DIR/"

echo
echo "Next steps:"
echo "  1. 写会议纪要到：$WORK_DIR/会议纪要.md"
echo "  2. 写完后再跑一次：bash $0 \"$WORK_DIR\"  # 自动转 HTML"
