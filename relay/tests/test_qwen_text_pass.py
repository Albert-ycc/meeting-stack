"""Qwen3 文字精转（transcribe/qwen_text_pass.py）与它在 FunASR runner 里的接入。

不跑真模型、不跑 ffmpeg，不读本机的词典快照；Qwen 的输出用手写的 JSON 代替。
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

TRANSCRIBE_DIR = Path(__file__).resolve().parents[2] / "transcribe"
SRT_TEXT = re.compile(r"^\d+\n[^\n]+\n(.*)$", re.S)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TRANSCRIBE_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qwen_json(chunks):
    """chunks: [(start_sec, end_sec, text, [(字, start_sec, end_sec), ...]), ...]"""
    segments, out_chunks = [], []
    for start, end, text, tokens in chunks:
        out_chunks.append({"text": text, "start": start, "end": end, "truncated": False})
        segments.extend({"text": t, "start": a, "end": b} for t, a, b in tokens)
    return {"text": "".join(c[2] for c in chunks), "segments": segments, "chunks": out_chunks}


def even_tokens(text, start, end):
    """给 text 里每个字母汉字均分时间戳（模拟对齐器）。"""
    chars = [ch for ch in text if ch.isalnum()]
    step = (end - start) / max(1, len(chars))
    return [(ch, start + i * step, start + (i + 1) * step) for i, ch in enumerate(chars)]


def funasr_sentence(start_ms, end_ms, text, spk, chunk=0, chunks=1):
    prefix = f"c{chunk + 1}-" if chunks > 1 else ""
    return {
        "start": start_ms,
        "end": end_ms,
        "text": text,
        "spk": f"{prefix}spk{spk}",
        "spk_raw": spk,
        "chunk": chunk,
    }


class SplitSentencesTests(unittest.TestCase):
    def setUp(self):
        self.q = load("qwen_text_pass_split", "qwen_text_pass.py")

    def test_cuts_on_punctuation_and_takes_token_times(self):
        text = "喂，张总。“医米”OK了吗？"
        tokens = [
            ("喂", 1.0, 1.2),
            ("张", 1.3, 1.4),
            ("总", 1.4, 1.6),
            ("医", 2.0, 2.1),
            ("米", 2.1, 2.2),
            ("OK", 2.3, 2.5),
            ("了", 2.5, 2.6),
            ("吗", 2.6, 2.8),
        ]
        sentences = self.q.split_sentences(qwen_json([(0.0, 3.0, text, tokens)]))
        self.assertEqual(["喂，", "张总。", "“医米”OK了吗？"], [s["text"] for s in sentences])
        self.assertEqual((1300, 1600), (sentences[1]["start"], sentences[1]["end"]))
        self.assertEqual((2000, 2800), (sentences[2]["start"], sentences[2]["end"]))

    def test_zero_length_sentence_gets_minimum_duration(self):
        sentences = self.q.split_sentences(qwen_json([(0.0, 1.0, "嗯。", [("嗯", 0.5, 0.5)])]))
        self.assertEqual((500, 510), (sentences[0]["start"], sentences[0]["end"]))

    def test_sentence_never_spans_two_qwen_chunks(self):
        qwen = qwen_json(
            [
                (0.0, 2.0, "我们这边", even_tokens("我们这边", 0.0, 2.0)),
                (2.0, 4.0, "的订单。", even_tokens("的订单", 2.0, 4.0)),
            ]
        )
        sentences = self.q.split_sentences(qwen)
        self.assertEqual(["我们这边", "的订单。"], [s["text"] for s in sentences])
        self.assertEqual([0, 1], [s["qwen_chunk"] for s in sentences])

    def test_text_and_timestamps_disagree_raises(self):
        with self.assertRaises(self.q.QwenPassError):
            self.q.split_sentences(
                qwen_json([(0.0, 1.0, "医米", [("一", 0.0, 0.5), ("米", 0.5, 1.0)])])
            )
        with self.assertRaises(self.q.QwenPassError):
            self.q.split_sentences(
                qwen_json([(0.0, 1.0, "医", [("医", 0.0, 0.5), ("米", 0.5, 1.0)])])
            )
        with self.assertRaises(self.q.QwenPassError):
            self.q.split_sentences(qwen_json([(0.0, 1.0, "医米", [("医", 0.0, 0.5)])]))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.q = load("qwen_text_pass_merge", "qwen_text_pass.py")

    def test_speaker_follows_largest_time_overlap(self):
        funasr = [
            funasr_sentence(0, 2000, "一米这块，", 0),
            funasr_sentence(2000, 4000, "智妍那边。", 1),
        ]
        text = "医米这块，智研那边。"
        tokens = [
            ("医", 0.1, 0.5),
            ("米", 0.5, 0.9),
            ("这", 0.9, 1.3),
            ("块", 1.3, 1.9),
            ("智", 2.1, 2.5),
            ("研", 2.5, 2.9),
            ("那", 2.9, 3.3),
            ("边", 3.3, 3.9),
        ]
        merged, stats = self.q.merge(funasr, qwen_json([(0.0, 4.0, text, tokens)]), [0], [])
        self.assertEqual(["医米这块，", "智研那边。"], [s["text"] for s in merged])
        self.assertEqual(["spk0", "spk1"], [s["spk"] for s in merged])
        self.assertEqual([], stats["rejected"])

    def test_no_overlap_takes_nearest_speaker(self):
        funasr = [funasr_sentence(0, 1000, "好的", 0), funasr_sentence(5000, 6000, "可以", 1)]
        text = "好的。嗯。可以。"
        tokens = [
            ("好", 0.1, 0.5),
            ("的", 0.5, 0.9),
            ("嗯", 4.0, 4.0),
            ("可", 5.1, 5.5),
            ("以", 5.5, 5.9),
        ]
        merged, _ = self.q.merge(funasr, qwen_json([(0.0, 6.0, text, tokens)]), [0], [])
        self.assertEqual(["spk0", "spk1", "spk1"], [s["spk"] for s in merged])

    def test_long_meeting_keeps_chunk_prefix_by_time(self):
        funasr = [
            funasr_sentence(0, 2000, "第一块", 0, chunk=0, chunks=2),
            funasr_sentence(3000, 5000, "第二块", 0, chunk=1, chunks=2),
        ]
        qwen = qwen_json(
            [
                (0.0, 2.5, "第一块。", even_tokens("第一块", 0.0, 2.0)),
                (2.5, 5.0, "第二块。", even_tokens("第二块", 3.0, 5.0)),
            ]
        )
        merged, _ = self.q.merge(funasr, qwen, [0, 2500], [])
        self.assertEqual(["c1-spk0", "c2-spk0"], [s["spk"] for s in merged])
        self.assertEqual([0, 1], [s["chunk"] for s in merged])

    def _one_chunk(self, funasr_text, qwen_text, terms=(), truncated=False):
        funasr = [funasr_sentence(0, 10_000, funasr_text, 0)]
        qwen = qwen_json([(0.0, 10.0, qwen_text, even_tokens(qwen_text, 0.0, 10.0))])
        qwen["chunks"][0]["truncated"] = truncated
        return self.q.merge(funasr, qwen, [0], list(terms))

    def test_bad_chunk_falls_back_to_funasr_text(self):
        funasr_text = "我们今天主要聊一下订单创建完之后物流那边的后续流程。"
        cases = {
            "截断": ("我们今天主要聊一下订单创建完之后物流那边的后续流程。", (), True),
            "连续重复": ("我们可以我们可以我们可以我们可以我们可以我们可以。", (), False),
            "字数偏差": ("我们。", (), False),
            "词典外溢": (
                "我们今天主要聊订单。医米 智研 追溯码 随货同行单 济佰世 关爱端",
                ("医米", "智研", "追溯码", "随货同行单", "济佰世", "关爱端"),
                False,
            ),
        }
        for reason, (qwen_text, terms, truncated) in cases.items():
            with self.subTest(reason=reason):
                merged, stats = self._one_chunk(funasr_text, qwen_text, terms, truncated)
                self.assertEqual([funasr_text], [s["text"] for s in merged])
                self.assertEqual(1, len(stats["rejected"]))
                self.assertIn(reason, stats["rejected"][0][1])

    def test_ordinary_repetition_in_speech_is_kept(self):
        funasr_text = "对对对对对，谢谢谢谢，谢谢关注，可以可以可以可以。"
        qwen_text = "对对对，对对。谢谢谢谢，谢谢关注。可以可以可以可以。"
        merged, stats = self._one_chunk(funasr_text, qwen_text)
        self.assertEqual([], stats["rejected"])
        self.assertEqual("对对对，", merged[0]["text"])

    def test_corrected_terms_are_not_mistaken_for_leaks(self):
        merged, stats = self._one_chunk(
            "一米和智妍那边的追踪码。",
            "医米和智研那边的追溯码。",
            ("医米", "智研", "追溯码", "随货同行单", "济佰世"),
        )
        self.assertEqual([], stats["rejected"])
        self.assertEqual("医米和智研那边的追溯码。", merged[0]["text"])


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.q = load("qwen_text_pass_refine", "qwen_text_pass.py")
        self.funasr = [funasr_sentence(0, 2000, "一米这块。", 0)]
        self.logs = []

    def _refine(self, runner):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(self.q, "select_context", return_value=(["医米"], "医米科研用药")),
            patch.object(self.q, "_duration_sec", return_value=2.0),
            patch.object(self.q.subprocess, "run"),
        ):
            return self.q.refine("a.m4a", self.funasr, [0], tmp, self.logs.append, runner=runner)

    def test_switch_off_keeps_funasr(self):
        def runner(*args):
            raise AssertionError("关掉时不该跑 Qwen")

        with patch.dict(os.environ, {"TRANSCRIBE_TEXT_ENGINE": "paraformer"}):
            self.assertIsNone(self._refine(runner))

    def test_failure_keeps_funasr_and_says_why(self):
        def runner(*args):
            raise self.q.QwenPassError("没装 Qwen3-ASR")

        with patch.dict(os.environ, {"TRANSCRIBE_TEXT_ENGINE": ""}):
            self.assertIsNone(self._refine(runner))
        self.assertTrue(any("没装 Qwen3-ASR" in line for line in self.logs))

    def test_success_passes_selected_terms_as_context(self):
        seen = {}

        def runner(wav, terms, workdir, timeout):
            seen["terms"] = terms
            seen["timeout"] = timeout
            return qwen_json([(0.0, 2.0, "医米这块。", even_tokens("医米这块", 0.0, 2.0))])

        with patch.dict(os.environ, {"TRANSCRIBE_TEXT_ENGINE": ""}):
            merged = self._refine(runner)
        self.assertEqual(["医米这块。"], [s["text"] for s in merged])
        self.assertEqual(["医米"], seen["terms"])
        self.assertGreaterEqual(seen["timeout"], self.q.MIN_TIMEOUT_SEC)


class SelectContextTests(unittest.TestCase):
    def test_project_terms_and_also_names_become_context(self):
        q = load("qwen_text_pass_context", "qwen_text_pass.py")
        snapshot = {
            "schema_version": 1,
            "projects": [{"id": "p-yimi", "name": "医米科研用药", "cues": ["医米", "科研用药"]}],
            "terms": [
                {"term": "济佰世", "aliases": ["纪百事"], "scope": "医米", "project_id": "p-yimi"},
                {"term": "吉士医", "also": ["吉士医小程序"], "scope": "通用"},
                {"term": "萝卜坑", "scope": "泰邦", "project_id": "p-taibang"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary-snapshot.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            terms, project = q.select_context("医米这边的科研用药，医米和纪百事那边。", path)
        self.assertEqual("医米科研用药", project)
        self.assertEqual(["济佰世", "吉士医", "吉士医小程序"], terms)


class RunnerIntegrationTests(unittest.TestCase):
    """runner 用 Qwen 的句子写四件套：srt 与 funasr.json 逐句一致（说话人补标靠这一条对齐）。"""

    def test_outputs_use_refined_sentences_and_keep_paraformer(self):
        runner = load("funasr_transcribe_qwen_runner", "funasr_transcribe.py")
        results = iter(
            [
                {"sentence_info": [{"start": 0, "end": 2000, "text": "一米这块。", "spk": 0}]},
                {"sentence_info": [{"start": 500, "end": 2500, "text": "智妍那边。", "spk": 0}]},
            ]
        )

        class FakeModel:
            def __init__(self, **kwargs):
                pass

            def generate(self, input, batch_size_s, hotword):
                return [next(results)]

        chunks = [
            (Path("c0.wav"), 0.0, None, {"start": 0.0, "end": 3.0, "hard_cut": False}),
            (Path("c1.wav"), 3.0, None, {"start": 3.0, "end": 6.0, "hard_cut": False}),
        ]
        refined = [
            {
                "start": 0,
                "end": 2000,
                "text": "医米这块。",
                "spk": "c1-spk0",
                "spk_raw": 0,
                "chunk": 0,
            },
            {
                "start": 3500,
                "end": 5500,
                "text": "智研那边。",
                "spk": "c2-spk0",
                "spk_raw": 0,
                "chunk": 1,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (
                patch.dict(sys.modules, {"funasr": types.SimpleNamespace(AutoModel=FakeModel)}),
                patch.object(runner, "to_wav_chunks", return_value=(chunks, 2)),
                patch.object(sys, "argv", ["funasr_transcribe.py", "a.m4a", str(out), "demo"]),
                patch.object(runner, "log"),
                patch.object(runner.qwen_text_pass, "refine", return_value=refined) as refine,
            ):
                runner.main()
            handed = refine.call_args.args
            blocks = json.loads((out / "demo.funasr.json").read_text(encoding="utf-8"))
            srt = (out / "demo.srt").read_text(encoding="utf-8")
            txt = (out / "demo.txt").read_text(encoding="utf-8")
            spk = (out / "demo.spk.txt").read_text(encoding="utf-8")

        # 交给第二遍的 FunASR 句子带着整场时间、原始说话人编号和所在块
        self.assertEqual(
            [(0, 2000, 0, 0, "c1-spk0"), (3500, 5500, 0, 1, "c2-spk0")],
            [(x["start"], x["end"], x["spk_raw"], x["chunk"], x["spk"]) for x in handed[1]],
        )
        self.assertEqual([0.0, 3000.0], handed[2])
        self.assertEqual("一米这块。", blocks[0]["paraformer"]["sentence_info"][0]["text"])
        self.assertEqual("qwen3-asr", blocks[1]["result"]["text_engine"])
        second = blocks[1]["result"]["sentence_info"][0]
        self.assertEqual((500, 2500, 0), (second["start"], second["end"], second["spk"]))
        json_texts = [s["text"] for b in blocks for s in b["result"]["sentence_info"]]
        srt_texts = [SRT_TEXT.match(cue).group(1) for cue in srt.strip().split("\n\n")]
        self.assertEqual(json_texts, srt_texts)
        self.assertEqual("医米这块。\n智研那边。\n", txt)
        self.assertIn("[c2-spk0] [00:03] 智研那边。", spk)


if __name__ == "__main__":
    unittest.main()
