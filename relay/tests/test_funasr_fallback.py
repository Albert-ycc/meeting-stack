"""FunASR 某一块只回整段文本、没有 sentence_info 时，回退句要有合法的非零时长。

用假模型，不加载真模型、不跑 ffmpeg；产物写临时目录。
"""

import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


RUNNER_PATH = Path(__file__).resolve().parents[2] / "transcribe" / "funasr_transcribe.py"
SRT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})")


def load_runner():
    spec = importlib.util.spec_from_file_location("funasr_transcribe_fallback_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def srt_cues(text: str) -> list[tuple[int, int, str]]:
    cues = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        match = SRT_TIME.fullmatch(lines[1])
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(value) for value in match.groups())
        start = ((h1 * 60 + m1) * 60 + s1) * 1000 + ms1
        end = ((h2 * 60 + m2) * 60 + s2) * 1000 + ms2
        cues.append((start, end, lines[2]))
    return cues


class FunAsrTextOnlyChunkTests(unittest.TestCase):
    def _run(self, chunks, results):
        module = load_runner()
        calls = iter(results)

        class FakeModel:
            def __init__(self, **kwargs):
                pass

            def generate(self, input, batch_size_s, hotword):
                return [next(calls)]

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (
                patch.dict(sys.modules, {"funasr": types.SimpleNamespace(AutoModel=FakeModel)}),
                patch.object(module, "to_wav_chunks", return_value=(chunks, len(chunks))),
                patch.object(sys, "argv", ["funasr_transcribe.py", "audio.m4a", str(out), "demo"]),
                patch.object(module, "log"),
            ):
                module.main()
            return srt_cues((out / "demo.srt").read_text(encoding="utf-8"))

    def test_text_only_chunk_spans_to_chunk_end(self):
        cues = self._run(
            [
                (Path("c0.wav"), 0.0, None, {"start": 0.0, "end": 2700.0, "hard_cut": False}),
                (Path("c1.wav"), 2700.0, None, {"start": 2700.0, "end": 3000.0, "hard_cut": False}),
            ],
            [
                {"text": "第一块", "sentence_info": [{"start": 0, "end": 1500, "text": "第一块正常的话", "spk": 0}]},
                {"text": "第二块只有整段文本没有分句"},
            ],
        )

        self.assertEqual(
            [(0, 1500, "第一块正常的话"), (2_700_000, 3_000_000, "第二块只有整段文本没有分句")],
            cues,
        )

    def test_text_only_chunk_without_usable_end_still_has_positive_duration(self):
        # 计划里的块终点不比起点大（异常输入）时，也不能写出起止相同的句子
        cues = self._run(
            [(Path("c0.wav"), 12.0, None, {"start": 12.0, "end": 12.0, "hard_cut": False})],
            [{"text": "只有整段文本"}],
        )

        self.assertEqual(1, len(cues))
        start, end, _ = cues[0]
        self.assertEqual(12_000, start)
        self.assertGreater(end, start)


if __name__ == "__main__":
    unittest.main()
