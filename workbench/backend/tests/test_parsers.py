import json

import pytest

from meeting_workbench.parsers import parse_funasr_json, parse_srt, parse_txt, parse_whisper_json


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


def test_parse_funasr_single_block_wrapped_in_list_keeps_bare_label(tmp_path):
    # 真实单块会议的 .funasr.json 顶层仍是长度为 1 的列表（不是裸 dict），
    # 必须和历史裸 dict 格式一样不带块前缀，否则所有单块会议的标签都会变。
    path = tmp_path / "single.funasr.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk": 0,
                    "offset_sec": 0,
                    "result": {
                        "sentence_info": [
                            {"start": 300, "end": 900, "spk": 0, "text": "第一句"},
                            {"start": 1000, "end": 1600, "spk": 1, "text": "第二句"},
                        ]
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    segments = parse_funasr_json(path)

    assert [segment["speaker_label"] for segment in segments] == ["SPEAKER_00", "SPEAKER_01"]


def test_parse_funasr_multichunk_labels_get_chunk_prefix_and_dont_collide(tmp_path):
    # 两块各自的 spk 都从 0 重编号，块前缀是区分两个不同人的唯一手段——
    # 不加前缀会让 chunk0 的 SPEAKER_00 和 chunk1 的 SPEAKER_00 被判定成同一个人。
    path = tmp_path / "multi-collide.funasr.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chunk": 0,
                    "offset_sec": 0,
                    "result": {
                        "sentence_info": [
                            {"start": 100, "end": 800, "spk": 0, "text": "块一说话人0"}
                        ]
                    },
                },
                {
                    "chunk": 1,
                    "offset_sec": 2000,
                    "result": {
                        "sentence_info": [
                            {"start": 200, "end": 900, "spk": 0, "text": "块二说话人0"}
                        ]
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    segments = parse_funasr_json(path)

    assert [segment["speaker_label"] for segment in segments] == ["C1_SPEAKER_00", "C2_SPEAKER_00"]
    assert segments[0]["speaker_label"] != segments[1]["speaker_label"]


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


BAD_TIMESTAMPS = [
    "1e400",
    "-1e400",
    "Infinity",
    "NaN",
    "100000000000000000000000",
    pytest.param("1" + "0" * 400, id="10**400"),
]


@pytest.mark.parametrize("value", BAD_TIMESTAMPS)
def test_whisper_json_rejects_non_finite_or_out_of_range_timestamps(tmp_path, value):
    path = tmp_path / "whisper.json"
    path.write_text('{"segments":[{"start": %s, "end": 1, "text": "x"}]}' % value, "utf-8")

    with pytest.raises(ValueError, match="invalid timestamp"):
        parse_whisper_json(path)


@pytest.mark.parametrize("value", BAD_TIMESTAMPS)
def test_funasr_json_rejects_non_finite_or_out_of_range_timestamps(tmp_path, value):
    path = tmp_path / "meeting.funasr.json"
    path.write_text('{"sentence_info":[{"start": 0, "end": %s, "text": "x"}]}' % value, "utf-8")

    with pytest.raises(ValueError, match="invalid timestamp"):
        parse_funasr_json(path)


def test_funasr_chunk_offset_that_pushes_past_the_limit_is_rejected(tmp_path):
    path = tmp_path / "meeting.funasr.json"
    path.write_text(
        json.dumps(
            [
                {"chunk": 0, "offset_sec": 0, "result": {"sentence_info": []}},
                {
                    "chunk": 1,
                    "offset_sec": 100 * 3600 - 1,
                    "result": {"sentence_info": [{"start": 5000, "end": 6000, "text": "x"}]},
                },
            ]
        ),
        "utf-8",
    )

    with pytest.raises(ValueError, match="invalid timestamp"):
        parse_funasr_json(path)


def test_timestamps_up_to_the_srt_limit_still_parse(tmp_path):
    whisper = tmp_path / "whisper.json"
    whisper.write_text(
        json.dumps({"segments": [{"start": 359_999.0, "end": 359_999.999, "text": "最后一句"}]}),
        "utf-8",
    )
    funasr = tmp_path / "meeting.funasr.json"
    funasr.write_text(
        json.dumps({"sentence_info": [{"start": -10, "end": 359_999_999, "text": "末尾"}]}),
        "utf-8",
    )

    assert parse_whisper_json(whisper)[0]["end_ms"] == 359_999_999
    # 负数照旧放行（入库时会被夹到 0），只拒绝越界和非有限值。
    assert [(s["start_ms"], s["end_ms"]) for s in parse_funasr_json(funasr)] == [(-10, 359_999_999)]


def _srt(tmp_path, data: bytes):
    path = tmp_path / "meeting.srt"
    path.write_bytes(data)
    return [(s["start_ms"], s["end_ms"], s["text"]) for s in parse_srt(path)]


def test_srt_cues_without_a_blank_line_between_them_stay_separate(tmp_path):
    data = "1\n00:00:01,000 --> 00:00:02,000\n第一句\n2\n00:00:03,000 --> 00:00:04,000\n第二句\n"
    assert _srt(tmp_path, data.encode()) == [(1000, 2000, "第一句"), (3000, 4000, "第二句")]


def test_srt_text_that_merely_contains_an_arrow_is_still_text(tmp_path):
    data = "1\n00:00:01,000 --> 00:00:02,000\n方案 A --> 方案 B\n100\n\n2\n00:00:03,000 --> 00:00:04,000\n好\n"
    assert _srt(tmp_path, data.encode()) == [
        (1000, 2000, "方案 A --> 方案 B 100"),
        (3000, 4000, "好"),
    ]


@pytest.mark.parametrize(
    "timing",
    ["00:00:02,000 -->", "00:00:02,000 -->   ", " --> 00:00:04,000", "00:00:0 --> 00:00:04,000"],
)
def test_srt_truncated_timing_line_is_a_value_error(tmp_path, timing):
    # 写到一半被截断的 SRT：统一按不合规时间码报 ValueError，不能漏出 IndexError。
    with pytest.raises(ValueError):
        _srt(tmp_path, f"1\n{timing}\n正文\n".encode())


def test_gbk_transcripts_are_decoded_instead_of_turning_into_replacement_characters(tmp_path):
    data = "1\n00:00:01,000 --> 00:00:02,000\n中文内容\n".encode("gbk")
    assert _srt(tmp_path, data) == [(1000, 2000, "中文内容")]
    txt = tmp_path / "meeting.txt"
    txt.write_bytes("历史逐字稿".encode("gbk"))
    assert parse_txt(txt)[0]["text"] == "历史逐字稿"


def test_utf8_with_a_truncated_tail_is_still_read_as_utf8(tmp_path):
    data = "1\n00:00:01,000 --> 00:00:02,000\n这一句是完整的中文内容\n".encode() + "半".encode()[:2]
    assert _srt(tmp_path, data) == [(1000, 2000, "这一句是完整的中文内容 \ufffd")]


def test_undecodable_transcript_is_a_value_error(tmp_path):
    with pytest.raises(ValueError):
        _srt(tmp_path, b"1\n00:00:01,000 --> 00:00:02,000\n" + b"\xff\x80" * 50 + b"\n")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(
            "\ufeff1\r\n00:00:01,000 --> 00:00:02,000\r\n你好\r\n\r\n"
            "2\r\n00:00:03,000 --> 00:00:04,000\r\n再见\r\n".encode(),
            [(1000, 2000, "你好"), (3000, 4000, "再见")],
            id="bom-crlf",
        ),
        pytest.param(
            "1\r00:00:01,000 --> 00:00:02,000\r甲\r\r2\r00:00:03,000 --> 00:00:04,000\r乙\r".encode(),
            [(1000, 2000, "甲"), (3000, 4000, "乙")],
            id="cr-only",
        ),
        pytest.param(b"", [], id="empty"),
        pytest.param(
            "1\n00:00:05,000 --> 00:00:01,000\n倒着\n\n2\n00:00:00,500 --> 00:00:09,000\n重叠\n".encode(),
            [(5000, 1000, "倒着"), (500, 9000, "重叠")],
            id="reversed-and-overlapping",
        ),
        pytest.param(
            ("1\n00:00:01,000 --> 00:00:02,000\n" + "长" * 200_000 + "\n").encode(),
            [(1000, 2000, "长" * 200_000)],
            id="very-long-line",
        ),
    ],
)
def test_srt_inputs_that_already_parsed_correctly_keep_their_result(tmp_path, data, expected):
    assert _srt(tmp_path, data) == expected
