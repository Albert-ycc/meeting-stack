import importlib.util
from pathlib import Path
import unittest


RUNNER_PATH = Path(__file__).resolve().parents[2] / "transcribe" / "funasr_transcribe.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("funasr_transcribe_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FunAsrChunkPlanningTests(unittest.TestCase):
    def test_long_audio_prefers_nearby_silence_without_overlap(self):
        module = load_runner()

        chunks = module.plan_chunks(
            5_500.0,
            silence_intervals=[(2_680.0, 2_682.0)],
        )

        self.assertEqual(
            [
                {"start": 0.0, "end": 2_681.0, "hard_cut": False},
                {"start": 2_681.0, "end": 5_500.0, "hard_cut": False},
            ],
            chunks,
        )

    def test_long_audio_uses_small_overlap_when_no_silence_exists(self):
        module = load_runner()

        chunks = module.plan_chunks(5_500.0, silence_intervals=[])

        self.assertEqual(0.0, chunks[0]["start"])
        self.assertEqual(2_700.0, chunks[0]["end"])
        self.assertTrue(chunks[0]["hard_cut"])
        self.assertEqual(2_698.0, chunks[1]["start"])
        self.assertEqual(5_500.0, chunks[1]["end"])
        self.assertFalse(chunks[1]["hard_cut"])

    def test_overlap_dedup_only_removes_same_text_near_boundary(self):
        module = load_runner()
        existing = [
            {"start": 2_696_000, "end": 2_700_000, "text": "下一步确认预算", "spk": "c1-spk0"}
        ]
        repeated = {
            "start": 2_698_100,
            "end": 2_701_000,
            "text": "下一步确认预算",
            "spk": "c2-spk1",
        }
        different = {**repeated, "text": "下一步确认范围"}

        self.assertTrue(module.is_overlap_duplicate(repeated, existing, 2_700.0))
        self.assertFalse(module.is_overlap_duplicate(different, existing, 2_700.0))


if __name__ == "__main__":
    unittest.main()
