#!/usr/bin/env python3
"""FunASR 转写 runner（paraformer-zh + fsmn-vad + ct-punc + cam++ 说话人分离）

由 transcribe.sh 调用，不建议手动直跑（手动跑也行，产物落 outdir）：
    ~/.venvs/funasr/bin/python funasr_transcribe.py <音频> <输出目录> <stem> [prompt文件]

产物（与 whisper 版 transcribe.sh 的下游契约对齐）：
    <stem>.txt          逐字稿（每句一行，无说话人前缀）
    <stem>.srt          带时间轴字幕
    <stem>.spk.txt      带说话人分组的转写（[spkN] [mm:ss] 前缀）
    <stem>.funasr.json  原始结构化结果

约束（实跑验证得出，改动前先读）：
  - 超过 50 分钟的音频自动切块分别转写再按偏移合并——16G 内存机器上，95 分钟音频
    整段跑 cam++ 聚类曾两次被系统因内存压力杀掉，切半 + batch_size_s=60 才稳。
    切块的副作用：说话人编号跨块不连续（c1-spk0 与 c2-spk0 未必是同一人）。
  - 热词从 prompt 文件提取，上限 20 词——热词过多会造成假注入，实测无关的专有名词
    会被硬塞进完全不相干的会议里 5-6 处。实际采用的热词表打进 stdout 供审计。
  - batch_size_s 保持 60，调大能提速但内存峰值会上去，别随手改。
"""
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHUNK_SEC = 3000          # 50 分钟硬上限
CHUNK_TARGET_SEC = 2700   # 45 分钟目标长度
CHUNK_SEARCH_SEC = 90     # 在目标点前后寻找安静边界
CHUNK_OVERLAP_SEC = 2     # 无安静边界时的硬切重叠
HOTWORD_CAP = 20          # 热词上限，防假注入
BATCH_SIZE_S = 60         # 转写批大小（秒），内存峰值与它正相关


def log(msg):
    print(f"[funasr] {msg}", flush=True)


def audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def load_hotwords(prompt_file):
    """从 prompt.txt（whisper initial_prompt 词典）提取热词，去重、限量。"""
    if not prompt_file or not Path(prompt_file).is_file():
        return ""
    text = Path(prompt_file).read_text(encoding="utf-8")
    tokens, seen = [], set()
    for t in re.findall(r"[A-Za-z0-9一-鿿]{2,}", text):
        if t not in seen:
            seen.add(t)
            tokens.append(t)
    if len(tokens) > HOTWORD_CAP:
        log(f"WARN: 词典 {len(tokens)} 词超上限，截取前 {HOTWORD_CAP} 词（防假注入）")
        tokens = tokens[:HOTWORD_CAP]
    return " ".join(tokens)


def detect_silence_intervals(audio):
    """用 ffmpeg 找安静区间；检测失败时安全降级为硬切。"""
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
            "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log("WARN: 静音检测失败，使用带重叠的硬切")
        return []
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", result.stderr)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", result.stderr)]
    return list(zip(starts, ends))


def plan_chunks(duration, silence_intervals):
    """按安静边界规划切块；找不到边界时保留小重叠。"""
    duration = max(0.0, float(duration))
    if duration <= CHUNK_SEC:
        return [{"start": 0.0, "end": duration, "hard_cut": False}]
    silence_midpoints = [
        (float(start) + float(end)) / 2
        for start, end in silence_intervals
        if float(end) > float(start)
    ]
    chunks = []
    start = 0.0
    while duration - start > CHUNK_SEC:
        target = start + CHUNK_TARGET_SEC
        latest = min(start + CHUNK_SEC, duration)
        candidates = [
            point
            for point in silence_midpoints
            if target - CHUNK_SEARCH_SEC <= point <= target + CHUNK_SEARCH_SEC
            and start + 60 <= point <= latest
        ]
        if candidates:
            end = min(candidates, key=lambda point: (abs(point - target), point))
            hard_cut = False
        else:
            end = min(target, latest)
            hard_cut = True
        chunks.append({"start": round(start, 3), "end": round(end, 3), "hard_cut": hard_cut})
        next_start = end - CHUNK_OVERLAP_SEC if hard_cut else end
        if next_start <= start:
            raise RuntimeError("切块规划没有向前推进")
        start = next_start
    chunks.append({"start": round(start, 3), "end": round(duration, 3), "hard_cut": False})
    return chunks


def _normalized_overlap_text(text):
    return re.sub(r"\W+", "", str(text), flags=re.UNICODE).casefold()


def is_overlap_duplicate(candidate, existing, boundary_sec):
    """只在硬切边界附近去除文本完全相同的重叠句。"""
    boundary_ms = float(boundary_sec) * 1000
    if candidate["start"] > boundary_ms + 5000 or candidate["end"] < boundary_ms - 5000:
        return False
    normalized = _normalized_overlap_text(candidate.get("text", ""))
    if not normalized:
        return False
    for previous in reversed(existing[-12:]):
        if previous["end"] < boundary_ms - 5000:
            break
        if _normalized_overlap_text(previous.get("text", "")) == normalized:
            return True
    return False


def to_wav_chunks(audio, tmpdir):
    """转 16k 单声道 wav；超长时优先在静音处切块。"""
    dur = audio_duration(audio)
    silence_intervals = detect_silence_intervals(audio) if dur > CHUNK_SEC else []
    plan = plan_chunks(dur, silence_intervals)
    chunks = []
    for i, item in enumerate(plan):
        off = item["start"]
        chunk_len = item["end"] - item["start"]
        wav = Path(tmpdir) / f"chunk{i}.wav"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(audio),
               "-ac", "1", "-ar", "16000"]
        if len(plan) > 1:
            cmd += ["-ss", f"{off:.3f}", "-t", f"{chunk_len:.3f}"]
        cmd.append(str(wav))
        subprocess.run(cmd, check=True)
        overlap_boundary = plan[i - 1]["end"] if i and plan[i - 1]["hard_cut"] else None
        chunks.append((wav, off, overlap_boundary, item))
    boundaries = ", ".join(
        f"{item['start']:.1f}-{item['end']:.1f}{'*' if item['hard_cut'] else ''}"
        for item in plan
    )
    log(f"音频 {dur/60:.1f} 分钟，切为 {len(plan)} 块（* 为带重叠硬切）：{boundaries}")
    return chunks, len(plan)


def fmt_srt_ts(ms):
    s, ms = divmod(int(ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    audio, outdir, stem = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    prompt_file = sys.argv[4] if len(sys.argv) > 4 else None

    hotword = load_hotwords(prompt_file)
    log(f"热词表（{len(hotword.split()) if hotword else 0} 词）：{hotword or '（无）'}")

    from funasr import AutoModel

    t0 = time.time()
    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        spk_model="cam++",
        vad_kwargs={"max_single_segment_time": 30000},
        disable_update=True,
    )
    log(f"模型加载 {time.time()-t0:.1f}s")

    sentences, raw = [], []
    with tempfile.TemporaryDirectory() as tmpdir:
        chunks, n_chunks = to_wav_chunks(audio, tmpdir)
        for i, (wav, off, overlap_boundary, chunk_plan) in enumerate(chunks):
            t1 = time.time()
            res = model.generate(input=str(wav), batch_size_s=BATCH_SIZE_S,
                                 hotword=hotword)
            log(f"块 {i+1}/{n_chunks} 转写 {time.time()-t1:.1f}s")
            info = res[0]
            raw.append({"chunk": i, "offset_sec": off, "plan": chunk_plan, "result": info})
            prefix = f"c{i+1}-" if n_chunks > 1 else ""
            for s in info.get("sentence_info", []):
                sentence = {
                    "start": s.get("start", 0) + off * 1000,
                    "end": s.get("end", 0) + off * 1000,
                    "text": s.get("text", "").strip(),
                    "spk": f"{prefix}spk{s.get('spk', '?')}",
                }
                if overlap_boundary and is_overlap_duplicate(
                    sentence, sentences, overlap_boundary
                ):
                    continue
                sentences.append(sentence)
            if not info.get("sentence_info") and info.get("text"):
                sentences.append({"start": off * 1000, "end": off * 1000,
                                  "text": info["text"], "spk": f"{prefix}spk?"})

    sentences = [s for s in sentences if s["text"]]
    if not sentences:
        log("ERROR: 转写结果为空")
        sys.exit(1)

    # <stem>.txt：每句一行
    (outdir / f"{stem}.txt").write_text(
        "\n".join(s["text"] for s in sentences) + "\n", encoding="utf-8")

    # <stem>.srt
    srt = []
    for i, s in enumerate(sentences, 1):
        srt.append(f"{i}\n{fmt_srt_ts(s['start'])} --> {fmt_srt_ts(s['end'])}\n{s['text']}\n")
    (outdir / f"{stem}.srt").write_text("\n".join(srt), encoding="utf-8")

    # <stem>.spk.txt：同说话人连续句合并成段
    lines, cur_spk, buf, seg_start = [], None, [], 0
    for s in sentences:
        if s["spk"] != cur_spk and buf:
            t = int(seg_start // 1000)
            lines.append(f"[{cur_spk}] [{t//60:02d}:{t%60:02d}] {''.join(buf)}")
            buf = []
        if s["spk"] != cur_spk:
            cur_spk, seg_start = s["spk"], s["start"]
        buf.append(s["text"])
    if buf:
        t = int(seg_start // 1000)
        lines.append(f"[{cur_spk}] [{t//60:02d}:{t%60:02d}] {''.join(buf)}")
    header = ""
    if n_chunks > 1:
        header = (f"# 长音频已切 {n_chunks} 块分别转写，说话人编号跨块不连续"
                  f"（c1-spk0 与 c2-spk0 未必是同一人）\n")
    (outdir / f"{stem}.spk.txt").write_text(
        header + "\n".join(lines) + "\n", encoding="utf-8")

    # <stem>.funasr.json
    (outdir / f"{stem}.funasr.json").write_text(
        json.dumps(raw, ensure_ascii=False, default=str), encoding="utf-8")

    n_spk = len({s["spk"] for s in sentences})
    log(f"完成：{len(sentences)} 句，说话人 {n_spk} 个，总耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
