import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = REPO_ROOT / "quickstart" / "relay_control.py"


def load_control_module():
    spec = importlib.util.spec_from_file_location(
        "relay_control_minutes_v3_under_test", CONTROL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_hour_srt(path: Path) -> None:
    def timestamp(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},000"

    blocks = []
    for index in range(60):
        blocks.append(
            f"{index + 1}\n"
            f"{timestamp(index * 60)} --> {timestamp((index + 1) * 60)}\n"
            f"第{index + 1}段内容"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def make_valid_v3_evidence(plan: dict) -> dict:
    first_window = plan["windows"][0]
    first_cue = first_window["cues"][0]
    return {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "strategy": "multi_stage",
        "topics": [
            {
                "topic_id": "T01",
                "title": "测试议题",
                "start_sec": first_cue["source_start_sec"],
                "end_sec": first_cue["source_end_sec"],
                "items": [
                    {
                        "item_id": "D01",
                        "kind": "decision",
                        "text": "测试结论",
                        "source_start_sec": first_cue["source_start_sec"],
                        "source_end_sec": first_cue["source_end_sec"],
                        "source_text_sha256": first_cue["source_text_sha256"],
                        "source_window_id": first_window["window_id"],
                        "minutes_anchor": "[00:00:00]",
                        "status": "included",
                    }
                ],
            }
        ],
        "coverage": {
            "total_items": 1,
            "included_items": 1,
            "omitted_items": 0,
        },
    }


def write_ledgers(
    root: Path,
    plan: dict,
    *,
    omit_last: bool = False,
    all_empty: bool = False,
) -> None:
    ledger_root = root / "minutes-ledger"
    ledger_root.mkdir()
    windows = plan["windows"][:-1] if omit_last else plan["windows"]
    for index, window in enumerate(windows):
        first_cue = window["cues"][0]
        items = []
        empty_reason = "本窗口无需要进入最终纪要的重要事项"
        if index == 0 and not all_empty:
            items = [
                {
                    "item_id": "D01",
                    "kind": "decision",
                    "text": "测试结论",
                    "source_start_sec": first_cue["source_start_sec"],
                    "source_end_sec": first_cue["source_end_sec"],
                    "source_text_sha256": first_cue["source_text_sha256"],
                }
            ]
            empty_reason = None
        (ledger_root / f"{window['window_id']}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "minutes_protocol_version": 3,
                    "window_id": window["window_id"],
                    "items": items,
                    "empty_reason": empty_reason,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


class MinutesProtocolV3Tests(unittest.TestCase):
    def setUp(self):
        self.module = load_control_module()

    def test_current_protocol_is_v3_but_v2_evidence_still_validates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:01]", encoding="utf-8")
            evidence = root / "minutes-evidence.json"
            evidence.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "minutes_protocol_version": 2,
                        "strategy": "single_pass",
                        "topics": [
                            {
                                "topic_id": "T01",
                                "title": "议题",
                                "start_sec": 0,
                                "end_sec": 2,
                                "items": [
                                    {
                                        "item_id": "D01",
                                        "kind": "decision",
                                        "text": "测试结论",
                                        "source_start_sec": 1,
                                        "status": "included",
                                        "minutes_anchor": "[00:00:01]",
                                    }
                                ],
                            }
                        ],
                        "coverage": {
                            "total_items": 1,
                            "included_items": 1,
                            "omitted_items": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence, minutes, protocol_version=2
            )

        self.assertEqual(3, self.module.CURRENT_MINUTES_PROTOCOL_VERSION)
        self.assertEqual([], errors)

    def test_minutes_plan_has_ordered_non_overlapping_8_to_12_minute_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            source_digest = hashlib.sha256(srt.read_bytes()).hexdigest()

            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )

        self.assertEqual(3, plan["minutes_protocol_version"])
        self.assertEqual(source_digest, plan["source_srt_sha256"])
        self.assertEqual(60, plan["cue_count"])
        self.assertEqual(list(range(1, 61)), [
            cue["cue_index"]
            for window in plan["windows"]
            for cue in window["cues"]
        ])
        previous_end = 0
        for window in plan["windows"]:
            self.assertGreaterEqual(window["start_sec"], previous_end)
            self.assertGreaterEqual(window["end_sec"] - window["start_sec"], 480)
            self.assertLessEqual(window["end_sec"] - window["start_sec"], 720)
            previous_end = window["end_sec"]

    def test_new_v3_minutes_enqueue_rejects_txt_before_creating_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "archive"
            archive.mkdir()
            audio = root / "vm-20260714-120000-v3txt.m4a"
            audio.write_bytes(b"audio")
            transcript = root / "edited.txt"
            transcript.write_text("工作台编辑稿", encoding="utf-8")
            control = self.module.RelayControl(
                root / "jobs.sqlite3", archive_root=archive
            )

            with self.assertRaisesRegex(self.module.RelayControlError, "SRT"):
                control.enqueue(
                    audio,
                    requested_stage="minutes_generating",
                    transcript_path=transcript,
                )

            self.assertEqual([], control.list_jobs())

    def test_new_v3_minutes_retry_rejects_txt_without_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "archive"
            archive.mkdir()
            audio = root / "vm-20260714-120001-v3txt.m4a"
            audio.write_bytes(b"audio")
            transcript = root / "edited.txt"
            transcript.write_text("工作台编辑稿", encoding="utf-8")
            control = self.module.RelayControl(
                root / "jobs.sqlite3", archive_root=archive
            )
            job_id = control.enqueue(audio)
            claim = control.claim_next(worker_id="worker-v3txt")
            control.fail(
                job_id,
                "transcribing",
                "fixture",
                expected_attempt=1,
                expected_worker=claim["worker_id"],
            )

            with self.assertRaisesRegex(self.module.RelayControlError, "SRT"):
                control.retry(
                    job_id,
                    "minutes_generating",
                    transcript_path=transcript,
                )

            self.assertEqual(1, control.status(job_id)["current_attempt"])

    def test_prepare_attempt_draft_creates_safe_dir_for_real_enqueued_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "archive"
            archive.mkdir()
            audio = root / "vm-20260714-120002-v3plan.m4a"
            audio.write_bytes(b"audio")
            control = self.module.RelayControl(
                root / "jobs.sqlite3", archive_root=archive
            )
            job_id = control.enqueue(audio)
            claim = control.claim_next(worker_id="worker-v3plan")
            expected = archive / ".workbench-drafts" / job_id / "attempt-1"
            self.assertFalse(expected.exists())

            prepared = control.prepare_attempt_draft(
                job_id,
                attempt_no=1,
                expected_worker=claim["worker_id"],
            )

            self.assertEqual(expected, prepared)
            self.assertTrue(prepared.is_dir())

    def test_v3_rejects_bad_anchor_wrong_window_hash_time_and_missing_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan, omit_last=True)
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [12:99:00]", encoding="utf-8")
            evidence = make_valid_v3_evidence(plan)
            item = evidence["topics"][0]["items"][0]
            item["minutes_anchor"] = "[12:99:00]"
            item["source_window_id"] = plan["windows"][1]["window_id"]
            item["source_text_sha256"] = "f" * 64
            item["source_end_sec"] = 999999
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_evidence_anchor:D01", errors)
        self.assertIn("minutes_evidence_source:D01", errors)
        self.assertIn("minutes_ledger_missing:W006", errors)

    def test_v3_accepts_valid_evidence_and_one_explicit_empty_ledger_per_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan)
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertEqual([], errors)

    def test_v3_rejects_plan_or_cue_hash_not_matching_actual_srt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan)
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )
            srt.write_text(
                srt.read_text(encoding="utf-8").replace("第1段内容", "被修改的内容", 1),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_plan_source_mismatch", errors)

    def test_sparse_srt_fails_deterministically_instead_of_creating_empty_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "sparse.srt"
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n唯一一句\n", encoding="utf-8"
            )

            with self.assertRaises(self.module.RelayControlError) as caught:
                self.module.create_minutes_plan(
                    srt, root / "minutes-plan.json", total_duration_sec=3600
                )

        self.assertEqual("minutes_plan_window_duration", str(caught.exception))

    def test_plan_clamps_overlapping_diarization_cues_instead_of_failing(self):
        # 260721/260715 线上事故：FunASR 说话人分句输出重叠时间戳（抢话、
        # 取整），旧解析器按损坏 SRT 一票否决导致 minutes_plan_invalid。
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            srt.write_text(
                "1\n00:00:00,000 --> 00:03:20,010\n第一段\n\n"
                # 与前一条重叠 10ms（取整误差）
                "2\n00:03:20,000 --> 00:07:40,000\n第二段\n\n"
                # 完全被前一条覆盖的插话（抢话）
                "3\n00:05:00,000 --> 00:05:00,240\n对，\n\n"
                "4\n00:07:40,000 --> 00:11:40,000\n第三段\n\n"
                "5\n00:11:40,000 --> 00:15:19,000\n第四段\n",
                encoding="utf-8",
            )

            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=919
            )
            _, errors = self.module._minutes_plan_context(
                plan_path, source_srt_path=srt
            )

        self.assertEqual([], errors)
        self.assertEqual(5, plan["cue_count"])
        cues = [
            cue for window in plan["windows"] for cue in window["cues"]
        ]
        previous_end = 0.0
        for cue in cues:
            self.assertGreaterEqual(cue["source_start_sec"], previous_end)
            self.assertGreater(cue["source_end_sec"], cue["source_start_sec"])
            previous_end = cue["source_end_sec"]

    def test_plan_still_rejects_negative_duration_cue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            srt.write_text(
                "1\n00:00:10,000 --> 00:00:05,000\n终点早于起点\n",
                encoding="utf-8",
            )

            with self.assertRaises(self.module.RelayControlError) as caught:
                self.module.create_minutes_plan(
                    srt, root / "minutes-plan.json", total_duration_sec=919
                )

        self.assertEqual("minutes_plan_srt_cue", str(caught.exception))

    def test_plan_balances_twelve_to_sixteen_minute_meeting_into_two_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            srt.write_text(
                "1\n00:00:00,000 --> 00:03:20,000\n第一段\n\n"
                "2\n00:03:20,000 --> 00:07:40,000\n第二段\n\n"
                "3\n00:07:40,000 --> 00:11:40,000\n第三段\n\n"
                "4\n00:11:40,000 --> 00:15:19,000\n第四段\n",
                encoding="utf-8",
            )

            plan = self.module.create_minutes_plan(
                srt, root / "minutes-plan.json", total_duration_sec=919
            )

        self.assertEqual(2, len(plan["windows"]))
        self.assertEqual(919, plan["total_duration_sec"])
        self.assertTrue(
            all(
                360 <= window["end_sec"] - window["start_sec"] <= 720
                for window in plan["windows"]
            )
        )

    def test_v3_rejects_evidence_item_missing_from_its_nonempty_window_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan, all_empty=True)
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_evidence_ledger:D01", errors)

    def test_v3_rejects_topic_outside_plan_or_item_outside_topic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            write_hour_srt(srt)
            plan_path = root / "minutes-plan.json"
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            minutes = root / "minutes.md"
            minutes.write_text("结论 [00:00:00]", encoding="utf-8")
            write_ledgers(root, plan)
            ledgers = root / "minutes-ledger"
            evidence_path = root / "minutes-evidence.json"

            evidence = make_valid_v3_evidence(plan)
            evidence["topics"][0]["end_sec"] = plan["total_duration_sec"] + 1
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            outside_plan = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=ledgers,
                source_srt_path=srt,
            )
            self.assertIn("minutes_evidence_topic", outside_plan)

            evidence = make_valid_v3_evidence(plan)
            evidence["topics"][0]["start_sec"] = evidence["topics"][0][
                "end_sec"
            ]
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            outside_topic = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=ledgers,
                source_srt_path=srt,
            )
            self.assertIn("minutes_evidence_source:D01", outside_topic)

    def test_v3_rejects_nonempty_ledger_item_with_wrong_source_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan)
            ledger_path = root / "minutes-ledger" / "W001.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["items"][0]["source_text_sha256"] = "f" * 64
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_ledger_item:W001", errors)
        self.assertIn("minutes_evidence_ledger:D01", errors)

    def test_v3_rejects_window_gap_or_boundary_tampering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            plan["windows"][1]["start_sec"] += 1
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            write_ledgers(root, plan)
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_plan_window", errors)

    def test_v3_rejects_ledger_text_change_and_unaccounted_ledger_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan)
            ledger_path = root / "minutes-ledger" / "W001.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["items"][0]["text"] = "被篡改的结论"
            second_cue = plan["windows"][0]["cues"][1]
            ledger["items"].append(
                {
                    "item_id": "D02",
                    "kind": "fact",
                    "text": "未进入最终 evidence 的事实",
                    "source_start_sec": second_cue["source_start_sec"],
                    "source_end_sec": second_cue["source_end_sec"],
                    "source_text_sha256": second_cue["source_text_sha256"],
                }
            )
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_ledger_mismatch:D01", errors)
        self.assertIn("minutes_ledger_unaccounted:D02", errors)

    def test_v3_rejects_ledger_time_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            srt = root / "meeting.srt"
            plan_path = root / "minutes-plan.json"
            write_hour_srt(srt)
            plan = self.module.create_minutes_plan(
                srt, plan_path, total_duration_sec=3600
            )
            write_ledgers(root, plan)
            ledger_path = root / "minutes-ledger" / "W001.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["items"][0]["source_start_sec"] += 1
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            minutes = root / "minutes.md"
            minutes.write_text("测试结论 [00:00:00]", encoding="utf-8")
            evidence_path = root / "minutes-evidence.json"
            evidence_path.write_text(
                json.dumps(make_valid_v3_evidence(plan), ensure_ascii=False),
                encoding="utf-8",
            )

            errors = self.module._minutes_evidence_errors(
                evidence_path,
                minutes,
                protocol_version=3,
                plan_path=plan_path,
                ledger_root=root / "minutes-ledger",
                source_srt_path=srt,
            )

        self.assertIn("minutes_ledger_item:W001", errors)
        self.assertIn("minutes_evidence_ledger:D01", errors)


class RelayHotwordRetryTests(unittest.TestCase):
    def test_retry_cli_accepts_replacement_hotword_file(self):
        module = load_control_module()

        parsed = module.build_parser().parse_args(
            [
                "retry",
                "job-test",
                "--stage",
                "transcribing",
                "--hotwords",
                "/tmp/replacement-hotwords.txt",
            ]
        )

        self.assertEqual("/tmp/replacement-hotwords.txt", parsed.hotwords)

    def test_retry_replaces_hotword_snapshot_only_when_explicitly_requested(self):
        module = self.module = load_control_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_root = root / "archive"
            archive_root.mkdir()
            audio = root / "vm-20260710-120000-HOTWORDS.m4a"
            audio.write_bytes(b"audio")
            old = root / "old.txt"
            old.write_text("旧词条\n", encoding="utf-8")
            replacement = root / "replacement.txt"
            replacement.write_text("新词条\n", encoding="utf-8")
            control = module.RelayControl(
                root / "jobs.sqlite3",
                archive_root=archive_root,
                archive_lock_path=root / "archive.lock",
                auto_pending_archive=False,
            )
            job_id = control.enqueue(audio, hotword_prompt_path=old)
            control.record_stage(job_id, "transcribing")
            control.fail(job_id, "transcribing", "engine")

            control.retry(
                job_id, "transcribing", hotword_prompt_path=replacement
            )
            claim = control.claim_next(worker_id="worker-hotword-retry")
            snapshot_text = Path(claim["hotword_prompt_path"]).read_text(
                encoding="utf-8"
            )

        self.assertEqual("新词条\n", snapshot_text)
        self.assertEqual(
            hashlib.sha256("新词条\n".encode()).hexdigest(),
            claim["hotword_prompt_sha256"],
        )

    def test_retry_without_replacement_keeps_original_hotword_snapshot(self):
        module = load_control_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive_root = root / "archive"
            archive_root.mkdir()
            audio = root / "vm-20260710-120000-HOTWORDS.m4a"
            audio.write_bytes(b"audio")
            hotwords = root / "hotwords.txt"
            hotwords.write_text("原始词条\n", encoding="utf-8")
            control = module.RelayControl(
                root / "jobs.sqlite3",
                archive_root=archive_root,
                archive_lock_path=root / "archive.lock",
                auto_pending_archive=False,
            )
            job_id = control.enqueue(audio, hotword_prompt_path=hotwords)
            original_sha = control.status(job_id)["hotword_prompt_sha256"]
            control.record_stage(job_id, "transcribing")
            control.fail(job_id, "transcribing", "engine")

            control.retry(job_id, "transcribing")
            claim = control.claim_next(worker_id="worker-hotword-inherit")

        self.assertEqual(original_sha, claim["hotword_prompt_sha256"])


if __name__ == "__main__":
    unittest.main()
