from meeting_workbench.quality import align_transcript_segments


def test_alignment_flags_number_and_latin_term_disagreements():
    primary = [
        {
            "id": "main-1",
            "start_ms": 1_000,
            "end_ms": 4_000,
            "text": "云图项目预算是一百二十万，接入SCRM",
        }
    ]
    candidate = [
        {
            "id": "ref-1",
            "start_ms": 1_100,
            "end_ms": 4_100,
            "text": "云图项目预算是一百二十五万，接入CRM",
        }
    ]

    rows = align_transcript_segments(primary, candidate)

    assert len(rows) == 1
    assert rows[0]["candidate_segment_ids"] == ["ref-1"]
    assert rows[0]["risk_kinds"] == ["latin_term", "number"]


def test_alignment_compares_repeated_numbers_as_multiset():
    rows = align_transcript_segments(
        [{"id": "primary", "start_ms": 0, "end_ms": 1000, "text": "金额120和120"}],
        [{"id": "candidate", "start_ms": 0, "end_ms": 1000, "text": "金额120"}],
    )

    assert rows[0]["risk_kinds"] == ["number"]
    assert rows[0]["primary_text"] != rows[0]["candidate_text"]


def test_alignment_marks_missing_candidate_without_fabricating_text():
    primary = [
        {
            "id": "main-1",
            "start_ms": 10_000,
            "end_ms": 12_000,
            "text": "这是主稿内容",
        }
    ]

    rows = align_transcript_segments(primary, [])

    assert rows == [
        {
            "primary_segment_id": "main-1",
            "candidate_segment_ids": [],
            "start_ms": 10_000,
            "end_ms": 12_000,
            "primary_text": "这是主稿内容",
            "candidate_text": "",
            "risk_kinds": ["missing_candidate"],
            "similarity": 0.0,
        }
    ]
