#!/usr/bin/env python3
"""材料录音转写程序（声档第三期 3d）：常驻，一行一个请求、一行一个回答。

由声档服务的材料转写循环起，不给中转用，也不要手动直跑（会一直等 stdin）：
    ~/.venvs/funasr/bin/python funasr_material.py

请求：{"id": ..., "wav": "<16k 单声道 wav>", "offset_ms": <这段在原文件里的起点>}
回答：{"id": ..., "ok": true, "sentences": [{"start_ms", "end_ms", "text"}]}
出错：{"id": ..., "ok": false, "error": "..."}

和会议转写（funasr_transcribe.py）的区别，改动前先读：
  - 不加载说话人模型 cam++：材料不分说话人，省下的内存留给会议转写。不加载 cam++ 时不显式
    要 sentence_timestamp 就拿不到 sentence_info。
  - 不用热词：材料里什么都有，热词只会假注入。
  - 线程数固定 4：服务用 taskpolicy -b 起它，只跑能效核，线程开多了互相抢。
  - 原来的 stdout 只写回答；库里的 print 都转到 stderr（服务把它接到日志文件）。
  - stdin 读到 EOF 就退出：服务没了它也不会留着。
"""
import json
import os
import sys
import time

BATCH_SIZE_S = 60  # 同会议转写，内存峰值与它正相关，别随手改
THREADS = 4


def log(message):
    print(f"[funasr-material] {message}", file=sys.stderr, flush=True)


def sentences_from(result, offset_ms):
    """generate 的结果换成带原文件时间的句子；空结果就是没有句子，不算出错。"""
    info = result[0] if result else {}
    sentences = []
    for item in info.get("sentence_info") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        sentences.append({
            "start_ms": int(item.get("start", 0)) + offset_ms,
            "end_ms": int(item.get("end", 0)) + offset_ms,
            "text": text,
        })
    if not sentences and str(info.get("text") or "").strip():
        # 没有句子时间戳时整段算一句，起止都记在这段的起点
        sentences.append({"start_ms": offset_ms, "end_ms": offset_ms, "text": str(info["text"]).strip()})
    return sentences


def main():
    # 在 import funasr 之前：留一份原来的 fd 1 专门写回答，再把 fd 1 指到 stderr
    answers = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    os.environ["OMP_NUM_THREADS"] = str(THREADS)

    import torch
    from funasr import AutoModel

    torch.set_num_threads(THREADS)
    started = time.time()
    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        vad_kwargs={"max_single_segment_time": 30000},
        disable_update=True,
        disable_pbar=True,
    )
    log(f"模型加载 {time.time() - started:.1f}s")

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            log(f"不是 JSON 的请求，丢掉：{raw[:200]}")
            continue
        if not isinstance(request, dict):
            continue
        request_id = request.get("id")
        try:
            wav = str(request["wav"])
            offset_ms = int(request.get("offset_ms") or 0)
            began = time.time()
            result = model.generate(input=wav, batch_size_s=BATCH_SIZE_S, sentence_timestamp=True)
            sentences = sentences_from(result, offset_ms)
            log(f"{os.path.basename(wav)} 起点 {offset_ms // 1000}s：{len(sentences)} 句，{time.time() - began:.1f}s")
            answer = {"id": request_id, "ok": True, "sentences": sentences}
        except Exception as error:  # noqa: BLE001
            log(f"转写失败：{type(error).__name__}: {error}")
            answer = {"id": request_id, "ok": False, "error": f"{type(error).__name__}: {error}"}
        answers.write(json.dumps(answer, ensure_ascii=False) + "\n")
        answers.flush()


if __name__ == "__main__":
    main()
