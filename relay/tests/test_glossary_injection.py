import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INJECTION_PATH = REPO_ROOT / "glossary" / "injection.py"


def load_injection_module():
    spec = importlib.util.spec_from_file_location("glossary_injection", INJECTION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


injection = load_injection_module()


def term(text, aliases=(), *, project_id=None, scope="通用", also=(), category="术语"):
    entry = {"term": text, "aliases": list(aliases), "scope": scope, "category": category}
    if project_id:
        entry["project_id"] = project_id
    if also:
        entry["also"] = list(also)
    return entry


def project(project_id, name, cues, also=()):
    return {
        "id": project_id,
        "name": name,
        "also": list(also),
        "cues": [{"text": cue, "kind": "name"} for cue in cues],
    }


SNAPSHOT = {
    "schema_version": 1,
    "updated_at": "2026-09-26T10:00:00+00:00",
    "terms": [
        term("数理协会", ["树立协会"], project_id="p-yt", scope="云图AI"),
        term("初审规则", ["出审规则"], project_id="p-yt", scope="云图AI", also=["初审口径"]),
        term("主数据", ["珠数据"], project_id="p-zt", scope="数据中台"),
        term("随访", ["随方"]),
        term("病例报告表", ["病历报告表"], also=["CRF"]),
        term("白名单", ["百名单"]),
        term("儿保科", ["儿宝科"], scope="儿科"),
    ],
    "projects": [
        project("p-yt", "云图AI", ["云图AI", "云图"], also=["云图"]),
        project("p-zt", "数据中台", ["数据中台", "中台"]),
    ],
}


class SelectProjectTests(unittest.TestCase):
    def test_hint_by_id_or_name_picks_that_project(self):
        by_id = injection.select_injection(SNAPSHOT, "今天聊主数据", "p-yt")
        by_name = injection.select_injection(SNAPSHOT, "今天聊主数据", "云图 ai")
        for receipt in (by_id, by_name):
            self.assertEqual(receipt["project"]["id"], "p-yt")
            self.assertEqual(receipt["project"]["source"], "hint")
            terms = [entry["term"] for entry in receipt["terms"]]
            self.assertIn("数理协会", terms)
            self.assertNotIn("主数据", terms)

    def test_transcript_scoring_takes_the_leading_project(self):
        transcript = "云图AI 这期先把初审规则定下来。云图AI 的数理协会那边下周回复。"
        receipt = injection.select_injection(SNAPSHOT, transcript)
        self.assertEqual(receipt["project"]["id"], "p-yt")
        self.assertEqual(receipt["project"]["source"], "transcript")
        self.assertEqual(receipt["ranking"][0]["id"], "p-yt")

    def test_two_char_cue_needs_two_mentions(self):
        once = injection.select_injection(SNAPSHOT, "中台那边还没回，数据先放着")
        self.assertIsNone(once["project"])
        twice = injection.select_injection(SNAPSHOT, "中台那边还没回，中台的人下周来")
        self.assertEqual(twice["project"]["id"], "p-zt")

    def test_tie_or_low_score_falls_back_to_public_terms(self):
        tie = injection.select_injection(SNAPSHOT, "云图AI 和 数据中台 都要对一下")
        self.assertIsNone(tie["project"])
        self.assertEqual(len(tie["ranking"]), 2)
        self.assertTrue(all("project_id" not in entry for entry in tie["terms"]))
        low = injection.select_injection(SNAPSHOT, "云图AI 提了一句")
        self.assertIsNone(low["project"])

    def test_unknown_hint_uses_public_terms_only(self):
        receipt = injection.select_injection(SNAPSHOT, "", "p-gone")
        self.assertIsNone(receipt["project"])
        self.assertEqual(receipt["project_hint"], "p-gone")
        self.assertTrue(all("project_id" not in entry for entry in receipt["terms"]))

    def test_old_snapshot_without_projects_still_honours_hint(self):
        old = {key: value for key, value in SNAPSHOT.items() if key != "projects"}
        receipt = injection.select_injection(old, "", "p-zt")
        self.assertEqual(receipt["project"], {"id": "p-zt", "name": None, "source": "hint", "score": None})
        self.assertIn("主数据", [entry["term"] for entry in receipt["terms"]])

    def test_legacy_bucket_terms_are_never_injected(self):
        receipt = injection.select_injection(SNAPSHOT, "儿宝科")
        self.assertNotIn("儿保科", [entry["term"] for entry in receipt["terms"]])


class TierTests(unittest.TestCase):
    def test_tiers_order_and_project_terms_first_within_a_tier(self):
        transcript = "随方的时候看了病历报告表，白名单也要更新，出审规则再议"
        receipt = injection.select_injection(SNAPSHOT, transcript, "p-yt")
        order = [(entry["term"], entry["tier"]) for entry in receipt["terms"]]
        self.assertEqual(
            order,
            [
                ("初审规则", "A"),
                ("随访", "A"),
                ("病例报告表", "A"),
                ("白名单", "B"),
                ("数理协会", "C"),
            ],
        )
        self.assertEqual(receipt["terms"][0]["transcript_hits"], {"wrong": 1, "correct": 0})
        self.assertEqual(receipt["counts"]["project"], 2)
        self.assertEqual(receipt["counts"]["public"], 3)

    def test_also_counts_as_correct_mention(self):
        receipt = injection.select_injection(SNAPSHOT, "CRF 要改版", None)
        by_term = {entry["term"]: entry for entry in receipt["terms"]}
        self.assertEqual(by_term["病例报告表"]["tier"], "B")
        self.assertEqual(by_term["病例报告表"]["also"], ["CRF"])

    def test_limit_only_cuts_tier_c(self):
        terms = [term(f"词条{index:02d}", [f"错写{index:02d}"]) for index in range(60)]
        snapshot = {"schema_version": 1, "terms": terms}
        transcript = "".join(f"错写{index:02d}" for index in range(55))
        receipt = injection.select_injection(snapshot, transcript, limit=50)
        self.assertEqual(receipt["counts"]["tier_a"], 55)
        self.assertEqual(receipt["counts"]["tier_c"], 0)
        self.assertEqual(receipt["counts"]["tier_c_dropped"], 5)
        self.assertEqual(len(receipt["terms"]), 55)

        quiet = injection.select_injection(snapshot, "错写00", limit=50)
        self.assertEqual(len(quiet["terms"]), 50)
        self.assertEqual(quiet["counts"]["tier_c"], 49)
        self.assertEqual(quiet["counts"]["tier_c_dropped"], 10)

    def test_public_alias_equal_to_project_term_is_dropped(self):
        snapshot = {
            "schema_version": 1,
            "terms": [
                term("数理", ["树立", "竖立"]),
                term("树立", project_id="p-edu"),
                term("方舟", ["方州"], project_id="p-edu", also=["竖立"]),
            ],
            "projects": [project("p-edu", "教育", ["教育平台"])],
        }
        receipt = injection.select_injection(snapshot, "树立一个标杆", "p-edu")
        public = next(entry for entry in receipt["terms"] if entry["term"] == "数理")
        self.assertEqual(public["aliases"], [])
        self.assertEqual(
            receipt["dropped_aliases"],
            [{"term": "数理", "alias": "树立"}, {"term": "数理", "alias": "竖立"}],
        )
        # 不选这个项目时照常用
        plain = injection.select_injection(snapshot, "树立一个标杆")
        self.assertEqual(plain["terms"][0]["aliases"], ["树立", "竖立"])

    def test_matching_ignores_width_and_case(self):
        snapshot = {"schema_version": 1, "terms": [term("ADHD", ["ＡＤＨＤ症"])]}
        receipt = injection.select_injection(snapshot, "adhd症 的随访")
        self.assertEqual(receipt["terms"][0]["tier"], "A")


class ReceiptTests(unittest.TestCase):
    def test_missing_or_bad_snapshot_gives_empty_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "glossary-snapshot.json"
            self.assertIsNone(injection.load_snapshot(path))
            path.write_text("{", encoding="utf-8")
            self.assertIsNone(injection.load_snapshot(path))
            path.write_text(json.dumps({"schema_version": 2, "terms": []}), encoding="utf-8")
            self.assertIsNone(injection.load_snapshot(path))
            path.write_text(json.dumps(SNAPSHOT, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(injection.load_snapshot(path)["updated_at"], SNAPSHOT["updated_at"])
        receipt = injection.select_injection(None, "随方")
        self.assertTrue(receipt["snapshot_missing"])
        self.assertEqual(receipt["terms"], [])
        self.assertEqual(injection.render_table(receipt), "")

    def test_render_table_escapes_pipes_and_names_the_project(self):
        snapshot = {
            "schema_version": 1,
            "terms": [term("A|B 方案", ["AB方案"], project_id="p-yt")],
            "projects": [project("p-yt", "云图AI", ["云图AI"])],
        }
        table = injection.render_table(injection.select_injection(snapshot, "", "p-yt"))
        self.assertIn("## 本场术语对照表（以此为准）", table)
        self.assertIn("本场按「云图AI」项目挑了 1 条词（项目词 1 条、公共词 0 条）", table)
        self.assertIn("| A／B 方案 | AB方案 |  |", table)
        self.assertIn("不要再读 glossary-snapshot.json", table)
        public_only = injection.render_table(injection.select_injection(SNAPSHOT, ""))
        self.assertIn("本场没有认出项目，只用了", public_only)

    def test_write_receipt_is_json_roundtrip(self):
        receipt = injection.select_injection(SNAPSHOT, "随方", "p-yt")
        receipt["job_id"] = "job-1"
        with tempfile.TemporaryDirectory() as directory:
            path = injection.write_receipt(receipt, directory)
            self.assertEqual(path.name, "glossary-injection.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), receipt)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["glossary-injection.json"])


if __name__ == "__main__":
    unittest.main()
