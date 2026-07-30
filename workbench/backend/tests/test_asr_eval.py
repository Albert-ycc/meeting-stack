from __future__ import annotations

import json
import time

import pytest

from meeting_workbench import asr_eval, cli
from meeting_workbench.asr_eval import AsrEvaluationError, evaluate_asr, run_qwen_shadow


def write_gold(path):
    rows = [
        {
            "id": "sample-1",
            "reference": "云图项目金额是 120 万元",
            "entities": ["云图"],
            "numbers": ["120"],
        },
        {
            "id": "sample-2",
            "reference": "ACME 需求下周确认",
            "entities": ["ACME"],
            "numbers": [],
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_evaluate_asr_reports_cer_entities_numbers_and_missing_samples(tmp_path):
    gold = tmp_path / "gold.jsonl"
    write_gold(gold)
    funasr = tmp_path / "funasr"
    qwen = tmp_path / "qwen"
    funasr.mkdir()
    qwen.mkdir()
    (funasr / "sample-1.txt").write_text("云图项目金额是120万元", encoding="utf-8")
    (funasr / "sample-2.txt").write_text("卡卡需求下周确认", encoding="utf-8")
    (qwen / "sample-1.txt").write_text("云图项目金额是120万元", encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": funasr, "qwen": qwen})

    assert report["sample_count"] == 2
    assert report["engines"]["funasr"]["missing_samples"] == 0
    assert report["engines"]["funasr"]["entity_recall"] == pytest.approx(0.5)
    assert report["engines"]["funasr"]["number_recall"] == 1.0
    assert report["engines"]["funasr"]["cer"] > 0
    assert report["engines"]["qwen"]["missing_samples"] == 1


def test_evaluate_asr_reports_sample_metadata_precision_and_tag_metrics(tmp_path):
    gold = tmp_path / "gold.jsonl"
    rows = [
        {
            "id": "sample-1",
            "reference": "云图项目金额是 120 万元",
            "entities": ["云图"],
            "numbers": ["120"],
            "tags": ["medical", "numbers"],
            "start_ms": 0,
            "end_ms": 1_000,
            "source_audio_sha256": "A" * 64,
        },
        {
            "id": "sample-2",
            "reference": "ACME 项目是 7 号",
            "entities": ["ACME"],
            "numbers": ["7"],
            "tags": ["medical"],
        },
    ]
    gold.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "sample-1.txt").write_text(
        "云图项目金额是120万元，同时提到ACME和7号", encoding="utf-8"
    )

    report = evaluate_asr(gold, {"funasr": engine})

    assert report["schema_version"] == 2
    sample = report["samples"][0]
    assert sample["tags"] == ["medical", "numbers"]
    assert sample["start_ms"] == 0
    assert sample["end_ms"] == 1_000
    assert sample["source_audio_sha256"] == "a" * 64
    detail = sample["engines"]["funasr"]
    assert detail["missing"] is False
    assert detail["hypothesis"] == "云图项目金额是120万元，同时提到ACME和7号"
    assert detail["entity_hits"] == 1
    assert detail["entity_total"] == 1
    assert detail["number_hits"] == 1
    assert detail["number_total"] == 1

    summary = report["engines"]["funasr"]
    assert summary["entity_precision"] == 0.5
    assert summary["number_precision"] == 0.5
    assert summary["tag_metrics"]["numbers"]["sample_count"] == 1
    assert summary["tag_metrics"]["medical"]["missing_samples"] == 1


def test_evaluate_asr_uses_longest_non_overlapping_number_matches(tmp_path):
    gold = tmp_path / "gold.jsonl"
    rows = [
        {"id": "long", "reference": "金额120", "numbers": ["120"]},
        {"id": "short", "reference": "编号1", "numbers": ["1"]},
        {"id": "short-wrong", "reference": "编号1", "numbers": ["1"]},
    ]
    gold.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "long.txt").write_text("金额120", encoding="utf-8")
    (engine / "short.txt").write_text("编号1", encoding="utf-8")
    (engine / "short-wrong.txt").write_text("编号120", encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})

    details = {sample["id"]: sample["engines"]["funasr"] for sample in report["samples"]}
    assert details["long"]["number_hits"] == 1
    assert details["long"]["number_predictions"] == 1
    assert details["short"]["number_hits"] == 1
    assert details["short-wrong"]["number_hits"] == 0
    assert details["short-wrong"]["number_predictions"] == 1
    assert report["engines"]["funasr"]["number_recall"] == pytest.approx(2 / 3)
    assert report["engines"]["funasr"]["number_precision"] == pytest.approx(2 / 3)


def test_evaluate_asr_requires_unicode_numeric_boundaries(tmp_path):
    gold = tmp_path / "gold.jsonl"
    rows = [
        {"id": "inside-longer", "reference": "编号1", "numbers": ["1"]},
        {"id": "unicode-longer", "reference": "编号1", "numbers": ["1"]},
        {"id": "independent-one", "reference": "编号1", "numbers": ["1"]},
        {"id": "independent-120", "reference": "金额120", "numbers": ["120"]},
    ]
    gold.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "inside-longer.txt").write_text("10 和 1.5%", encoding="utf-8")
    (engine / "unicode-longer.txt").write_text("1٢", encoding="utf-8")
    (engine / "independent-one.txt").write_text("编号1号", encoding="utf-8")
    (engine / "independent-120.txt").write_text("金额120元", encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})

    details = {sample["id"]: sample["engines"]["funasr"] for sample in report["samples"]}
    assert details["inside-longer"]["number_hits"] == 0
    assert details["inside-longer"]["number_predictions"] == 0
    assert details["unicode-longer"]["number_hits"] == 0
    assert details["independent-one"]["number_hits"] == 1
    assert details["independent-120"]["number_hits"] == 1


def test_evaluate_asr_preserves_numeric_semantic_symbols_end_to_end(tmp_path):
    gold = tmp_path / "gold.jsonl"
    rows = [
        {"id": "decimal-correct", "reference": "数值1.5", "numbers": ["1.5"]},
        {"id": "decimal-wrong", "reference": "数值1.5", "numbers": ["1.5"]},
        {"id": "negative-correct", "reference": "温度-2", "numbers": ["-2"]},
        {"id": "negative-wrong", "reference": "温度-2", "numbers": ["-2"]},
        {"id": "unsigned-wrong", "reference": "温度2", "numbers": ["2"]},
        {"id": "percent", "reference": "比例1%", "numbers": ["1%"]},
        {"id": "per-mille", "reference": "比例1‰", "numbers": ["1‰"]},
    ]
    gold.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    hypotheses = {
        "decimal-correct": "数值１．５",
        "decimal-wrong": "数值15",
        "negative-correct": "温度－２度",
        "negative-wrong": "温度2度",
        "unsigned-wrong": "温度-2度",
        "percent": "比例１％",
        "per-mille": "比例1‰",
    }
    for sample_id, hypothesis in hypotheses.items():
        (engine / f"{sample_id}.txt").write_text(hypothesis, encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})

    details = {sample["id"]: sample["engines"]["funasr"] for sample in report["samples"]}
    assert details["decimal-correct"]["number_hits"] == 1
    assert details["decimal-wrong"]["number_hits"] == 0
    assert details["negative-correct"]["number_hits"] == 1
    assert details["negative-wrong"]["number_hits"] == 0
    assert details["unsigned-wrong"]["number_hits"] == 0
    assert details["percent"]["number_hits"] == 1
    assert details["per-mille"]["number_hits"] == 1


def test_evaluate_asr_uses_longest_non_overlapping_entity_matches(tmp_path):
    gold = tmp_path / "gold.jsonl"
    rows = [
        {
            "id": "long",
            "reference": "云图科研用药",
            "entities": ["云图科研用药"],
        },
        {"id": "short", "reference": "云图", "entities": ["云图"]},
        {"id": "short-wrong", "reference": "云图", "entities": ["云图"]},
    ]
    gold.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "long.txt").write_text("云图科研用药", encoding="utf-8")
    (engine / "short.txt").write_text("云图", encoding="utf-8")
    (engine / "short-wrong.txt").write_text("云图科研用药", encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})

    details = {sample["id"]: sample["engines"]["funasr"] for sample in report["samples"]}
    assert details["long"]["entity_hits"] == 1
    assert details["long"]["entity_predictions"] == 1
    assert details["short"]["entity_hits"] == 1
    assert details["short-wrong"]["entity_hits"] == 0
    assert details["short-wrong"]["entity_predictions"] == 1
    assert report["engines"]["funasr"]["entity_recall"] == pytest.approx(2 / 3)
    assert report["engines"]["funasr"]["entity_precision"] == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    "metadata",
    [
        {"tags": "medical"},
        {"tags": [""]},
        {"start_ms": True, "end_ms": 1},
        {"start_ms": -1, "end_ms": 1},
        {"start_ms": 0},
        {"start_ms": 1, "end_ms": 1},
        {"source_audio_sha256": None},
        {"source_audio_sha256": "not-a-sha256"},
    ],
)
def test_evaluate_asr_strictly_validates_optional_gold_metadata(tmp_path, metadata):
    gold = tmp_path / "gold.jsonl"
    row = {"id": "sample", "reference": "有效参考", **metadata}
    gold.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(AsrEvaluationError):
        evaluate_asr(gold, {"funasr": tmp_path})


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("entities", ["ACME", "acme"]),
        ("entities", ["ＡＣＭＥ", "ACME"]),
        ("numbers", ["１２０", "120"]),
        ("tags", ["Medical", "medical"]),
    ],
)
def test_evaluate_asr_rejects_duplicate_normalized_annotations(tmp_path, field, values):
    gold = tmp_path / "gold.jsonl"
    row = {"id": "sample", "reference": "有效参考", field: values}
    gold.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(AsrEvaluationError, match="重复"):
        evaluate_asr(gold, {"funasr": tmp_path})


def test_evaluate_asr_detects_explainable_hallucination_and_repetition(tmp_path):
    gold = tmp_path / "gold.jsonl"
    rows = [
        {"id": "hallucination", "reference": "患者今天状态平稳"},
        {"id": "repeat-three", "reference": "正常文本"},
        {"id": "repeat-four", "reference": "正常文本"},
    ]
    gold.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "hallucination.txt").write_text(
        "完全无关的超长候选内容这里没有任何可以和原文对齐的信息",
        encoding="utf-8",
    )
    repeated = "你好"
    (engine / "repeat-three.txt").write_text(
        repeated * (asr_eval.REPETITION_MIN_CONSECUTIVE - 1), encoding="utf-8"
    )
    (engine / "repeat-four.txt").write_text(
        repeated * asr_eval.REPETITION_MIN_CONSECUTIVE, encoding="utf-8"
    )

    report = evaluate_asr(gold, {"funasr": engine})

    details = {sample["id"]: sample["engines"]["funasr"] for sample in report["samples"]}
    assert details["hallucination"]["hallucination"] is True
    assert details["repeat-three"]["repetition"] is False
    assert details["repeat-four"]["repetition"] is True
    assert report["engines"]["funasr"]["hallucination_samples"] == 1
    assert report["engines"]["funasr"]["repetition_samples"] == 1


def test_hallucination_extra_character_threshold_boundary():
    assert asr_eval.HALLUCINATION_MIN_EXTRA_CHARS == 10
    assert asr_eval._is_hallucination("a" * 10, "x" * 20, 20) is True
    assert asr_eval._is_hallucination("a" * 10, "x" * 19, 19) is False


def test_hallucination_length_ratio_threshold_boundary():
    assert asr_eval.HALLUCINATION_LENGTH_RATIO == 1.5
    assert asr_eval._is_hallucination("a" * 30, "x" * 45, 45) is True
    assert asr_eval._is_hallucination("a" * 30, "x" * 44, 44) is False


def test_hallucination_alignment_threshold_boundary():
    assert asr_eval.HALLUCINATION_MAX_ALIGNED_RATIO == 0.5
    reference = "abcdefghijklmnopqrst"
    assert asr_eval._is_hallucination(reference, reference[:15] + "z" * 15, 15) is True
    assert asr_eval._is_hallucination(reference, reference[:16] + "z" * 14, 14) is False


def test_evaluate_asr_rejects_duplicate_gold_ids(tmp_path):
    gold = tmp_path / "gold.jsonl"
    row = {"id": "same", "reference": "文本", "entities": [], "numbers": []}
    gold.write_text(
        json.dumps(row, ensure_ascii=False) + "\n" + json.dumps(row, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(AsrEvaluationError, match="重复"):
        evaluate_asr(gold, {"funasr": tmp_path})


def test_evaluate_asr_preserves_safe_id_and_engine_boundaries(tmp_path):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps({"id": "../escape", "reference": "文本"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(AsrEvaluationError, match="id 无效"):
        evaluate_asr(gold, {"funasr": tmp_path})

    gold.write_text(
        json.dumps({"id": "safe-id", "reference": "文本"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(AsrEvaluationError, match="引擎名称无效"):
        evaluate_asr(gold, {"../escape": tmp_path})


def test_evaluate_asr_rejects_non_utf8_gold_and_hypothesis(tmp_path):
    gold = tmp_path / "gold.jsonl"
    gold.write_bytes(b"\xff")
    with pytest.raises(AsrEvaluationError, match="UTF-8 金标"):
        evaluate_asr(gold, {"funasr": tmp_path})

    gold.write_text(
        json.dumps({"id": "sample", "reference": "文本"}, ensure_ascii=False),
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "sample.txt").write_bytes(b"\xff")
    with pytest.raises(AsrEvaluationError, match="UTF-8 候选"):
        evaluate_asr(gold, {"funasr": engine})


def test_evaluate_asr_rejects_gold_and_hypothesis_over_64_mib(tmp_path):
    gold = tmp_path / "gold.jsonl"
    with gold.open("wb") as destination:
        destination.truncate(asr_eval.MAX_GOLD_BYTES + 1)
    with pytest.raises(AsrEvaluationError, match="金标文件超过 64 MiB"):
        evaluate_asr(gold, {"funasr": tmp_path})

    gold.write_text(
        json.dumps({"id": "sample", "reference": "文本"}, ensure_ascii=False),
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    with (engine / "sample.txt").open("wb") as destination:
        destination.truncate(asr_eval.MAX_TRANSCRIPT_BYTES + 1)
    with pytest.raises(AsrEvaluationError, match="候选逐字稿超过 64 MiB"):
        evaluate_asr(gold, {"funasr": engine})


def test_evaluate_asr_counts_repeated_predictions_as_multiset(tmp_path):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps(
            {
                "id": "sample",
                "reference": "云图金额120",
                "entities": ["云图"],
                "numbers": ["120"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "sample.txt").write_text("云图云图金额120和120", encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})
    detail = report["samples"][0]["engines"]["funasr"]

    assert detail["entity_hits"] == 1
    assert detail["entity_predictions"] == 2
    assert detail["number_hits"] == 1
    assert detail["number_predictions"] == 2


def test_evaluate_asr_does_not_match_chinese_one_inside_one_hundred(tmp_path):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps({"id": "sample", "reference": "一个", "numbers": ["一"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "sample.txt").write_text("一百个", encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})

    assert report["samples"][0]["engines"]["funasr"]["number_hits"] == 0
    assert report["samples"][0]["engines"]["funasr"]["number_predictions"] == 0


def test_evaluate_asr_rejects_single_sample_over_reasonable_character_limit(tmp_path):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps({"id": "sample", "reference": "a" * (asr_eval.MAX_SAMPLE_CHARACTERS + 1)}),
        encoding="utf-8",
    )

    with pytest.raises(AsrEvaluationError, match="单样本"):
        evaluate_asr(gold, {"funasr": tmp_path})


def test_evaluate_asr_accepts_single_sample_at_character_limit(tmp_path):
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps({"id": "sample", "reference": "a" * asr_eval.MAX_SAMPLE_CHARACTERS}),
        encoding="utf-8",
    )
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "sample.txt").write_text("a" * asr_eval.MAX_SAMPLE_CHARACTERS, encoding="utf-8")

    report = evaluate_asr(gold, {"funasr": engine})

    assert report["engines"]["funasr"]["cer"] == 0


def test_edit_distance_at_character_cap_finishes_within_loose_budget():
    started = time.monotonic()

    assert (
        asr_eval._edit_distance(
            "a" * asr_eval.MAX_SAMPLE_CHARACTERS,
            "b" * asr_eval.MAX_SAMPLE_CHARACTERS,
        )
        == asr_eval.MAX_SAMPLE_CHARACTERS
    )

    assert time.monotonic() - started < 8


def test_edit_distance_uses_exact_linear_memory_algorithm():
    assert asr_eval._edit_distance("kitten", "sitting") == 3
    assert asr_eval._edit_distance("", "abc") == 3


def test_asr_evaluate_cli_prints_json_report(tmp_path, capsys):
    gold = tmp_path / "gold.jsonl"
    write_gold(gold)
    engine = tmp_path / "funasr"
    engine.mkdir()
    (engine / "sample-1.txt").write_text("云图项目金额是120万元", encoding="utf-8")
    (engine / "sample-2.txt").write_text("ACME需求下周确认", encoding="utf-8")

    exit_code = cli.main(["asr-evaluate", "--gold", str(gold), "--engine", f"funasr={engine}"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["engines"]["funasr"]["sample_count"] == 2


def test_qwen_shadow_runs_offline_and_returns_generated_txt(tmp_path, monkeypatch):
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"audio")
    output = tmp_path / "qwen"
    calls = []

    monkeypatch.setattr(
        "meeting_workbench.asr_eval.shutil.which",
        lambda _name: "/opt/homebrew/bin/mlx-qwen3-asr",
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output.mkdir(parents=True, exist_ok=True)
        (output / "meeting.txt").write_text("影子稿", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("meeting_workbench.asr_eval.subprocess.run", fake_run)

    result = run_qwen_shadow(audio, output, model="0.6B")

    assert result == output / "meeting.txt"
    assert "Qwen/Qwen3-ASR-0.6B" in calls[0][0]
    assert calls[0][1]["env"]["HF_HUB_OFFLINE"] == "1"
    assert calls[0][1]["env"]["TRANSFORMERS_OFFLINE"] == "1"


def test_qwen_shadow_rejects_1_7b_in_adapter_and_cli(tmp_path, monkeypatch):
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"audio")

    with pytest.raises(AsrEvaluationError, match="固定使用 0.6B"):
        run_qwen_shadow(audio, tmp_path / "output", model="1.7B")

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "asr-shadow-qwen",
                str(audio),
                "--output-dir",
                str(tmp_path / "output"),
                "--model",
                "1.7B",
            ]
        )
