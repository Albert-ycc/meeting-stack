import json

import pytest

from meeting_workbench.parsers import parse_funasr_json, parse_srt, parse_whisper_json


def test_parse_srt_keeps_speaker_and_millisecond_anchor(tmp_path):
    path = tmp_path / "meeting.srt"
    path.write_text(
        "1\n00:00:01,250 --> 00:00:03,900\n[SPEAKER_01] 你好，我们开始吧。\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\n继续讨论。\n",
        encoding="utf-8",
    )

    segments = parse_srt(path)

    assert segments[0]["start_ms"] == 1250
    assert segments[0]["end_ms"] == 3900
    assert segments[0]["speaker_label"] == "SPEAKER_01"
    assert segments[0]["text"] == "你好，我们开始吧。"


def test_parse_old_whisper_json(tmp_path):
    path = tmp_path / "whisper.json"
    path.write_text(
        json.dumps({"segments": [{"start": 1.5, "end": 2.75, "text": " 对照文本 "}]}),
        encoding="utf-8",
    )

    segments = parse_whisper_json(path)

    assert segments == [
        {
            "ordinal": 0,
            "start_ms": 1500,
            "end_ms": 2750,
            "speaker_label": None,
            "speaker_name": None,
            "text": "对照文本",
        }
    ]


def test_parse_funasr_sentence_info(tmp_path):
    path = tmp_path / "funasr.json"
    path.write_text(
        json.dumps(
            {
                "sentence_info": [
                    {"start": 300, "end": 900, "spk": 2, "text": "第一句"},
                    {"start": 1000, "end": 1600, "spk": 1, "text": "第二句"},
                ]
            }
        ),
        encoding="utf-8",
    )

    segments = parse_funasr_json(path)

    assert [segment["speaker_label"] for segment in segments] == ["SPEAKER_02", "SPEAKER_01"]
    assert segments[1]["start_ms"] == 1000


def test_parse_funasr_multichunk_applies_offset_seconds(tmp_path):
    path = tmp_path / "multi.funasr.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk": 0,
                    "offset_sec": 0,
                    "result": {
                        "sentence_info": [{"start": 100, "end": 800, "spk": 0, "text": "前半场"}]
                    },
                },
                {
                    "chunk": 1,
                    "offset_sec": 2488.232,
                    "result": {
                        "sentence_info": [{"start": 200, "end": 900, "spk": 1, "text": "后半场"}]
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    segments = parse_funasr_json(path)

    assert segments[0]["start_ms"] == 100
    assert segments[1]["start_ms"] == 2_488_432
    assert segments[1]["end_ms"] == 2_489_132


def test_json_source_larger_than_64_mib_is_rejected_before_parsing(tmp_path):
    path = tmp_path / "oversized.funasr.json"
    with path.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(ValueError, match="64 MiB"):
        parse_funasr_json(path)


def test_json_source_deeper_than_128_levels_is_rejected(tmp_path):
    path = tmp_path / "deep.funasr.json"
    path.write_text("[" * 129 + "0" + "]" * 129, encoding="utf-8")

    with pytest.raises(ValueError, match="128"):
        parse_funasr_json(path)
