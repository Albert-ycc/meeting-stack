"""第四期 4h：从材料里挖词（H4）。种子、挖哪些内容、项目汇总的十步、听错的写法、不提的词、数量和去留、
循环（转写、预算、签名、开关、材料全文表补建）、写（不变不写、不用 INSERT OR REPLACE、上限、写事务）、内存。"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import tracemalloc
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_workbench import deep_links
from meeting_workbench import glossary_mining as gm
from meeting_workbench.db import utc_now
from meeting_workbench.material_fts import REBUILD_KEY
from meeting_workbench.project_names import merge_project

from .gm_world import NOW, body, mine, rows, terms, world
from .test_material_search import add_content, add_file, add_root
from .test_search import add_meeting, add_project


def seeds(text):
    return dict(gm.extract_seeds(text))


# ---------------------------------------------------------------------- 种子


def test_seeds_find_the_planted_word_and_skip_noise():
    found = seeds(body("司美格鲁肽", "驻场服务"))
    assert found["司美格鲁肽"] == 2 and found["驻场服务"] == 2
    # 被包住、次数一样的片段不要
    assert "美格鲁肽" not in found and "司美格鲁" not in found
    # 停用字打头结尾的不要
    assert not any(word[0] in "的了是在" or word[-1] in "的了是在" for word in found)
    assert gm.extract_seeds("综上所述没有。综上所述没有。") == []
    assert "天可以" not in seeds("明天可以。后天可以。")


def test_seed_boundary_keeps_verb_like_word_endings():
    """「请」是 glossary._EXPAND_STOP_CHARS 里的字，但也是很多正经词的收尾字（申请、邀请）；
    边界不能把它当虚字挡在词外，否则长一个字的整词反而卡不住，只留得下「用药申」这半截。"""
    found = seeds(body("用药申请"))
    assert found["用药申请"] == 2
    assert "用药申" not in found


def test_seeds_combine_several_longer_words_to_cover_a_short_one():
    """「组状态」只以「入组状态」「出组状态」两种写法出现，单独一个都不到 90%，合起来够——
    覆盖判断要按合起来算，不能只认某一个长词单独盖住（第四期复查后修，原来这种情况短词会留下）。"""
    text = "。".join(["入组状态确认"] * 6 + ["出组状态确认"] * 5)
    found = seeds(text)
    assert "组状态" not in found
    assert found.get("入组状态确认") == 6 and found.get("出组状态确认") == 5


def test_seeds_cover_a_word_via_a_prefix_that_fails_han_ok_itself():
    """「在」是虚字头，「在组状态」自己进不了候选词（_han_ok 挡在门外），但它仍然是「组状态」的
    真实出处——覆盖判断要用 _han_ok 过滤前的全体（frequent），只用候选词表（kept）会漏掉这条证据。"""
    text = "。".join(["在组状态确认"] * 10)
    found = seeds(text)
    assert "组状态" not in found
    assert "在组状态" not in found  # 虚字头，本来就不该是候选词


def test_seeds_keep_a_word_that_is_also_used_on_its_own():
    """「组状态」大部分时候独立出现，只是偶尔搭配「入组」「出组」——覆盖判断不能连累它，
    不能误伤。"""
    text = "。".join(["入组状态确认"] * 3 + ["出组状态确认"] * 3 + ["组状态"] * 10)
    found = seeds(text)
    assert "组状态" in found


def test_seeds_treat_guo_as_a_real_word_forming_char_not_a_stop_suffix():
    """「过」不是的、地、得、了、着这类纯语法虚字（通过、超过都以它收尾）；「次初审通过时间」
    「首次初审通过时间」「一次初审通过时间」分头出现时，「初审通过时间」这六个字的核心要留住，
    不能被切成「首次初审通」这种半截（复查后修：原来照搬 _SEED_STOP_CHARS 挡住了「过」，
    真词反而被卡在「过」前面）。"""
    text = "".join(
        [
            "系统记录次初审通过时间用于统计。", "接口返回次初审通过时间字段。", "报表展示次初审通过时间数值。",
            "首页显示首次初审通过时间提醒。", "详情页展示首次初审通过时间信息。", "列表按首次初审通过时间排序。",
            "导出包含一次初审通过时间列。", "校验一次初审通过时间格式。", "统计一次初审通过时间分布。",
            "文档写着初审通过时间的定义。", "手册解释初审通过时间的含义。",
        ]
    )
    found = seeds(text)
    assert "首次初审通" not in found
    assert found.get("初审通过时间") == 11


def test_latin_seeds():
    found = seeds("GLP-1 和 GLP-1；CRF 表、CRF；ESG ESG；PDF PDF 3f2a9c1e0b7d 3f2a9c1e0b7d v1.2 v1.2 report report")
    assert {"GLP-1", "CRF", "ESG"} <= set(found)
    assert not {"PDF", "v1.2", "report"} & set(found)
    for token in ("GLP-1", "CRF", "ESG", "OpenAI"):
        assert gm.latin_ok(token), token
    for token in ("PDF", "3f2a9c1e0b7d", "v1.2", "report", "550e8400-e29b-41d4-a716-446655440000"):
        assert not gm.latin_ok(token), token


def test_versioned_filename_is_not_a_seed():
    """「V1.4.0.txt」「V1.0.2-phase3.txt」这类版本号/阶段名带扩展名的文件名不算词：
    _VERSION 只认整串是版本号，带扩展名或「-phase3」这类后缀就漏了，扩展名本身是停用词才是关键。"""
    for token in ("V1.4.0.txt", "V1.5.3.txt", "V1.0.2-phase3.txt", "V1.1.0.txt"):
        assert not gm.latin_ok(token), token
    found = seeds(body("V1.4.0.txt", "R01"))
    assert "V1.4.0.txt" not in found
    assert found["R01"] == 2  # 需求编号（R01）这类不是文件名，照样留着


def test_only_the_first_60000_chars_are_read():
    text = "。" * gm.MINE_CHARS + body("尾巴里的词")
    assert gm.extract_seeds(text) == []
    assert "开头里的词" in seeds(body("开头里的词") + "。" * gm.MINE_CHARS)


def test_name_segments():
    assert gm.name_segments("入组标准说明_v2 司美格鲁肽方案汇总") == ["入组标准", "司美格鲁肽"]
    assert gm.name_segments("方案汇总") == []


def test_seed_memory_peak():
    random.seed(7)
    chars = [chr(0x4E00 + index) for index in range(3000)]
    text = "".join(random.choice(chars) for _ in range(200_000))
    tracemalloc.start()
    gm.extract_seeds(text)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak <= 16 * 1024 * 1024


HALVES = {
    "受试者用药记", "试者用药记录", "云图科研用药", "图科研用药平", "科研用药平台", "司美格鲁肽注", "美格鲁肽注射",
    "格鲁肽注射液", "注射液的冷链", "研用药平台需", "肽注射液的冷", "由北辰科研仓", "由项目组统一", "目组统一配置",
}


def test_real_style_seeds_have_no_halves():
    """词后面总跟着同一串字时，种子留整串（最长 8 字），不留一截截的滑窗。"""
    from .gm_real_style import P_DOCS

    found = seeds("\n".join(P_DOCS))
    assert {"受试者用药记录", "云图科研用药平台", "司美格鲁肽注射液", "北辰科研仓", "药品追溯码", "初审规则"} <= set(found)
    assert not HALVES & set(found)
    # 左邻总是同一个字的是半截；邻字是标点、虚字或一串汉字的头尾时不算
    assert "格鲁肽注射液" not in seeds("司美格鲁肽注射液。司美格鲁肽注射液的")
    assert seeds("北辰科研仓的接口。北辰科研仓的文档。")["北辰科研仓"] == 2


def test_real_style_project_top_ten(tmp_path):
    """真实风格样本：前 10 个里没有半截词，司美格鲁肽进候选并和会上听错的司美格鲁太成组。"""
    from .gm_real_style import real_style_world

    db = real_style_world(tmp_path)
    stats = mine(db)
    top = [item.term for item in stats.items[:10]]
    assert top[0] == "司美格鲁肽" and [pair.wrong for pair in stats.items[0].pairs] == ["司美格鲁太"]
    assert {"初审规则", "受试者用药记录", "药品追溯码", "北辰科研仓", "药房管理员"} <= set(top)
    assert not HALVES & {item.term for item in stats.items}
    assert ("司美格鲁肽", "司美格鲁太") in terms(db) and ("司美格鲁肽", "") in terms(db)


def test_pool_drops_a_word_one_char_shorter_than_a_frequent_one(tmp_path):
    """项目汇总：池里有多一个字、次数有它 0.9 以上的词，短的丢掉。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    for index in range(4):
        add_content(db, f"k{index}", [body("冷链温度报警", "药房管理员")])
        add_file(db, root, f"方案{index}.docx", key=f"k{index}")
    fake = {f"k{index}": [("冷链温度报", 2), ("冷链温度报警", 2), ("药房管理员", 3)] for index in range(4)}
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    kept = {item.term for item in stats.items}
    assert "冷链温度报警" in kept and "冷链温度报" not in kept and "药房管理员" in kept
    assert stats.queries["spread_materials"] == 2  # 短的在候选池里就丢了，第 3 步不再查它


def _grouped_state_world(tmp_path, segments=()):
    """「组状态」只以「入组状态」「出组状态」两种写法出现（第 6 步「包含」的合起来算场景），
    单独一个都不到 95%，合起来够；可选传入会上说过的话。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    for index in range(4):
        add_content(db, f"k{index}", [body("入组状态", "出组状态")])
        add_file(db, root, f"方案{index}.docx", key=f"k{index}")
    if segments:
        add_meeting(db, "m1", date="2026-09-20T10:00:00", project_id="p", segments=list(segments))
    return db


def _grouped_state_seeds():
    return {f"k{index}": [("组状态", 20), ("入组状态", 11), ("出组状态", 9)] for index in range(4)}


def test_project_covering_combines_several_longer_words(tmp_path):
    """第 6 步「包含」：「入组状态」占 11/20、「出组状态」占 9/20，单独一个都不到 95%，
    合起来 20/20 够——不能只认某一个长词单独盖住短词（复查后修）。"""
    db = _grouped_state_world(tmp_path)
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=_grouped_state_seeds())
    kept = {item.term for item in stats.items}
    assert "组状态" not in kept
    assert "入组状态" in kept and "出组状态" in kept


def test_covering_does_not_drop_a_word_that_was_actually_spoken(tmp_path):
    """会上真的独立说了「组状态」（前后都不是覆盖它的长词的紧邻字），就不能被第 6 步的
    「包含」当成别的词的一截收掉——「说过」是硬证据，比写在材料里的次数更可信。"""
    db = _grouped_state_world(tmp_path, segments=["这个组状态字段还没定"])
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=_grouped_state_seeds())
    assert "组状态" in {item.term for item in stats.items}


def test_covering_still_drops_a_word_only_ever_spoken_as_part_of_the_longer_one(tmp_path):
    """会上说的其实是「入组状态」，「组状态」只是子串命中算出来的「说过」——不是真独立说的，
    不能靠这个逃过「包含」（反例：证明「说过」保护不会被子串命中滥用）。"""
    db = _grouped_state_world(tmp_path, segments=["受试者的入组状态确认一下"])
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=_grouped_state_seeds())
    assert "组状态" not in {item.term for item in stats.items}


# ---------------------------------------------------------------------- 挖哪些内容


def test_only_text_and_pdf_layers_of_normal_documents_are_mined(tmp_path):
    db = world(tmp_path)[0]
    root = db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'p'")["id"]
    cases = {
        "k-img": ("图片.png", "image", "normal"),
        "k-media": ("录音.m4a", "media", "normal"),
        "k-py": ("脚本.py", "text", "normal"),
        "k-json": ("数据.json", "text", "normal"),
        "k-srt": ("字幕.srt", "text", "normal"),
        "k-cards": ("卡片.md", "text", "cards"),
        "k-code": ("代码.md", "text", "code"),
        "k-pkg": ("包.md", "text", "package"),
        "k-md": ("笔记.md", "text", "normal"),
        "k-csv": ("表.csv", "text", "normal"),
        "k-docx": ("文档.docx", "text", "normal"),
    }
    for key, (name, layer, zone) in cases.items():
        add_content(db, key, [body("看得见的词")], layer=layer)
        add_file(db, root, f"挖/{name}", key=key, zone=zone)
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=NOW)
    mined = {row["content_key"] for row in db.query_all("SELECT content_key FROM glossary_mining_seeds")}
    assert {"k-md", "k-csv", "k-docx"} <= mined
    assert not {"k-img", "k-media", "k-py", "k-json", "k-srt", "k-cards", "k-code", "k-pkg"} & mined


# ---------------------------------------------------------------------- 项目汇总


def test_project_pass_keeps_what_belongs_to_the_project(tmp_path):
    db, _roots = world(tmp_path)

    stats = mine(db)

    pending = terms(db)
    assert ("司美格鲁肽", "") in pending  # 说过、只在这个项目里
    assert ("驻场服务", "") in pending  # 没说过，3 个文件
    words = {term for term, _wrong in pending}
    assert "质量控制" not in words  # 在 2 个别的项目的材料里有
    assert "甲状腺髓样癌" not in words  # 在 2 个别的项目的会里说过
    assert "入组标准" not in words  # 没说过，只有 2 个文件
    assert "能耗看板" not in words  # 只在文件名里
    # 说过的排在没说过的前面
    order = [item.term for item in stats.items]
    assert order.index("司美格鲁肽") < order.index("驻场服务")


def test_unspoken_word_needs_three_files(tmp_path):
    db, roots = world(tmp_path)
    add_content(db, "k4", [body("入组标准")])
    add_file(db, roots["p"], "方案4.docx", key="k4")
    mine(db)
    assert ("入组标准", "") in terms(db)


# ---------------------------------------------------------------------- 法规原文噪声


def test_law_clause_terms_are_dropped_when_never_spoken(tmp_path):
    """没说过、命中的片段几乎全带「第……条」编号的，是法规原文摘出来的噪声，不是业务词。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    law_chunks = [
        "第一条 为规范违规经营行为，制定本办法。",
        "第二条 违规经营受托代理机构罚没款项，处一万元以上五万元以下罚款。",
        "第三条 由监督管理部门负责解释。",
    ]
    for index in range(3):
        add_content(db, f"k{index}", law_chunks)
        add_file(db, root, f"法规{index}.docx", key=f"k{index}")
    fake = {f"k{index}": [("受托代理机构", 6)] for index in range(3)}
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    assert "受托代理机构" not in {item.term for item in stats.items}


def test_law_clause_terms_survive_when_spoken(tmp_path):
    """同一个词、同样命中带条款编号的片段，只要会上真说过，就不该被法规噪声判断收掉——
    「说过」优先于法规信号，不能一刀切（反例：证明法规过滤不会误伤真被讨论过的词）。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    law_chunks = [
        "第一条 为规范违规经营行为，制定本办法。",
        "第二条 违规经营受托代理机构罚没款项，处一万元以上五万元以下罚款。",
        "第三条 由监督管理部门负责解释。",
    ]
    for index in range(3):
        add_content(db, f"k{index}", law_chunks)
        add_file(db, root, f"法规{index}.docx", key=f"k{index}")
    add_meeting(db, "m1", date="2026-09-20T10:00:00", project_id="p", segments=["受托代理机构这边要不要改"])
    fake = {f"k{index}": [("受托代理机构", 6)] for index in range(3)}
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    assert "受托代理机构" in {item.term for item in stats.items}


def test_business_term_in_a_mostly_business_file_is_not_dropped(tmp_path):
    """业务文档里偶尔引用一句法规，不能连累同一份材料里的真业务词——命中片段本身不带编号、
    整份材料也不是法规页面（反例：证明法规过滤不会因为材料里有一句引用就整份牵连）。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    chunks = ["科研用药项目需要药房管理员登记入库单，签收后交给药师核对。"] * 8 + [
        "第一条 引用了一句相关法规作为背景说明。"
    ]
    for index in range(3):
        add_content(db, f"k{index}", chunks)
        add_file(db, root, f"业务{index}.docx", key=f"k{index}")
    fake = {f"k{index}": [("药房管理员", 5)] for index in range(3)}
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    assert "药房管理员" in {item.term for item in stats.items}


def test_file_level_regulation_density_catches_boilerplate(tmp_path):
    """网页页脚、导航条这类抓下来的边角文字（「中国示例网」「网站标识码」）本身不挨着条款编号，
    但整份材料条款编号总数够多、密度也够高（是法规页面本身），也该按法规噪声收掉。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    nums = [
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四", "十五",
        "十六", "十七", "十八", "十九", "二十", "二十一",
    ]
    clause_chunks = [f"第{n}条 违规经营处一万元以上罚款，由监督管理部门解释。" for n in nums]
    footer_chunk = "版权所有：中国示例网运行中心 网站标识码bm0999999"
    for index in range(3):
        add_content(db, f"k{index}", clause_chunks + [footer_chunk])
        add_file(db, root, f"法规{index}.docx", key=f"k{index}")
    fake = {f"k{index}": [("中国示例网", 6)] for index in range(3)}
    with db.autocommit() as conn:
        assert gm._file_is_regulation(conn, "k0") is True
        stats = gm.compute(conn, "p", seeds=fake)
    assert "中国示例网" not in {item.term for item in stats.items}


def test_file_level_density_does_not_flag_a_mostly_business_file(tmp_path):
    """同样凑够了 LAW_CLAUSE_FILE_MIN 个条款编号，但整份材料绝大部分是业务正文——密度不够，
    不该被判成法规页面（反例：证明「总数够」不等于「整份都是法规」）。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    nums = [
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四", "十五",
        "十六", "十七", "十八", "十九", "二十", "二十一",
    ]
    clause_chunks = [f"第{n}条 附录引用。" for n in nums]
    biz_chunks = ["科研用药项目里药品追溯码要扫码校验，登记入库单和签收信息，核对批号有效期。" * 10] * 80
    for index in range(3):
        add_content(db, f"k{index}", biz_chunks + clause_chunks)
        add_file(db, root, f"业务{index}.docx", key=f"k{index}")
    fake = {f"k{index}": [("追溯码", 5)] for index in range(3)}
    with db.autocommit() as conn:
        assert gm._file_is_regulation(conn, "k0") is False
        stats = gm.compute(conn, "p", seeds=fake)
    assert "追溯码" in {item.term for item in stats.items}


def test_misheard_writing_forms_a_pair(tmp_path):
    db, _roots = world(tmp_path)
    mine(db)
    assert ("司美格鲁肽", "司美格鲁太") in terms(db)
    row = next(row for row in rows(db) if row["wrong"] == "司美格鲁太")
    assert row["spoken"] == 2
    evidence = json.loads(row["evidence_json"])
    assert {ref["m"] for ref in evidence["heard"]} == {"m1", "m2"}
    assert "司美格鲁" not in row["evidence_json"].replace("司美格鲁肽", "").replace("司美格鲁太", "")


def test_pairs_need_two_hearings_and_no_material(tmp_path):
    db, roots = world(tmp_path)
    add_meeting(db, "m3", date="2026-09-27T10:00:00", project_id="p", segments=["驻场服剂再说", "驻场服剂"])
    add_meeting(db, "m4", date="2026-09-27T11:00:00", project_id="p", segments=["驻扬服务只一次"])
    mine(db)
    wrongs = {wrong for _term, wrong in terms(db) if wrong}
    assert "驻扬服务" not in wrongs  # 只听到 1 次
    assert "驻场服剂" in wrongs
    # 写法在任何材料里出现过就不成
    add_content(db, "k-x", ["驻场服剂在这份材料里有"])
    add_file(db, roots["q"], "别的.docx", key="k-x")
    db.execute("UPDATE glossary_mining_scan SET mined_at = '2000-01-01T00:00:00+00:00'")
    mine(db)
    assert "驻场服剂" not in {wrong for _term, wrong in terms(db) if wrong}


def test_head_of_a_long_word_pairs_only_with_a_real_misheard_form(tmp_path):
    """长词的头几个字（至少 5 字）也拿来找听错的写法；换的是的、了这类虚字时不算。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    for index in range(3):
        add_content(db, f"k{index}", [body("司美格鲁肽注射液", "受试者用药记录")])
        add_file(db, root, f"方案{index}.docx", key=f"k{index}")
    add_meeting(db, "m1", date="2026-09-20T10:00:00", project_id="p",
                segments=["司美格鲁太的剂量", "受试者用的东西", "司美格鲁太再看", "受试者用的那个"])
    stats = mine(db)
    paired = {item.term: [pair.wrong for pair in item.pairs] for item in stats.items if item.pairs}
    assert paired == {"司美格鲁肽": ["司美格鲁太"]}
    assert ("司美格鲁肽", "司美格鲁太") in terms(db) and ("司美格鲁肽注射液", "") in terms(db)


def test_sub_base_pair_rejects_particle_wrong_even_after_a_real_one(tmp_path):
    """同一个头几个字的 base 配出好几种写法：头一个不是的、了这类虚字收尾、后面几个是的时候，
    后面几个也要照样挡掉——不能因为这个 base 已经被头一个立住了，就不再查后面的。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    for index in range(3):
        add_content(db, f"k{index}", [body("受试者用药记录")])
        add_file(db, root, f"方案{index}.docx", key=f"k{index}")
    add_meeting(
        db, "m1", date="2026-09-20T10:00:00", project_id="p",
        segments=[
            "受试者用一次就够", "受试者用一遍看看",
            "受试者用了才知道", "受试者用了没反应",
            "受试者用的时候要注意", "受试者用的效果不错",
        ],
    )
    stats = mine(db)
    paired = {item.term: sorted(pair.wrong for pair in item.pairs) for item in stats.items if item.pairs}
    assert paired == {"受试者用药": ["受试者用一"]}


def _face_whitelist_world(tmp_path, segments):
    """「人脸识别白名单」是完整词，「人脸识别白」是切出来找听错写法用的 sub；不同 segments
    验证「只有听错写法」和「原词也真独立说过」两种情况。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    for index in range(4):
        add_content(db, f"k{index}", [body("人脸识别白名单")])
        add_file(db, root, f"方案{index}.docx", key=f"k{index}")
    add_meeting(db, "m1", date="2026-09-20T10:00:00", project_id="p", segments=segments)
    return db


def test_sub_base_drops_when_covered_and_multiple_wrong_forms(tmp_path):
    """「人脸识别白名单」材料里从不单独出现「人脸识别白」；会上「人脸识别一」「人脸识别应」两种
    写法都被听到 2 次以上——不是同一个词被听错成一种写法，是「人脸识别」后面接哪个字都算，这是
    位置本身不稳定（sub 就是截断的伪影）的信号，「人脸识别白」不该冒出来当候选词（D6 原始样例）。"""
    db = _face_whitelist_world(
        tmp_path,
        ["人脸识别一开关", "人脸识别一还没配", "人脸识别应该怎么弄", "人脸识别应用范围"],
    )
    stats = mine(db)
    terms_found = {item.term: [pair.wrong for pair in item.pairs] for item in stats.items}
    assert "人脸识别白" not in terms_found
    assert "人脸识别白名单" in terms_found


def test_sub_base_survives_when_the_original_word_was_independently_spoken(tmp_path):
    """同样两种听错写法都出现，但会上也真独立说过一次「人脸识别白」（后面跟的不是「名」）——
    这次不是伪影，是真有这个说法，不能被截断判断收掉（反例：证明独立说过优先于「多种写法」信号）。"""
    db = _face_whitelist_world(
        tmp_path,
        [
            "人脸识别一开关", "人脸识别一还没配", "人脸识别应该怎么弄", "人脸识别应用范围",
            "这个人脸识别白要不要单独测",
        ],
    )
    stats = mine(db)
    terms_found = {item.term: sorted(pair.wrong for pair in item.pairs) for item in stats.items}
    assert terms_found.get("人脸识别白") == ["人脸识别一", "人脸识别应"]


def test_three_char_words_get_no_pairs():
    pairs = gm.find_pairs(["张伟明"], "张伟民说了\n张伟民又说", lambda _text: False)
    assert pairs == []
    pairs = gm.find_pairs(["能耗看板"], "能耗看版上线\n能耗看版再看", lambda _text: False)
    assert [(pair.base, pair.wrong, pair.heard) for pair in pairs] == [("能耗看板", "能耗看版", 2)]


def test_pair_on_an_existing_project_term_names_that_term(tmp_path):
    db, _roots = world(tmp_path)
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, project_id,
               created_at, updated_at) VALUES ('gt-1', '能耗看板', '[]', '云图AI', '其他', 'manual', 1, 'p', ?, ?)""",
        (utc_now(), utc_now()),
    )
    add_meeting(db, "m5", date="2026-09-27T10:00:00", project_id="p", segments=["能耗看版上线", "能耗看版再看"])
    mine(db)
    row = next(row for row in rows(db) if row["wrong"] == "能耗看版")
    assert row["term"] == "能耗看板" and row["status"] == "pending"
    assert not any(row["term"] == "能耗看板" and row["wrong"] == "" for row in rows(db))


@pytest.mark.parametrize(
    "setup",
    [
        "other_alias", "project_also", "folder", "generic_folder", "ignored_name", "suggestion", "rejected",
    ],
)
def test_words_that_are_never_proposed(tmp_path, setup):
    db, roots = world(tmp_path)
    now = utc_now()
    word = "驻场服务"
    if setup == "other_alias":
        db.execute(
            """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, project_id,
                   created_at, updated_at) VALUES ('gt-q', '驻场支持', ?, '北辰仓', '其他', 'manual', 1, 'q', ?, ?)""",
            (json.dumps([word], ensure_ascii=False), now, now),
        )
    elif setup == "project_also":
        db.execute("UPDATE projects SET also_names = ? WHERE id = 'q'", (json.dumps([{"name": word, "source": "manual"}], ensure_ascii=False),))
    elif setup == "folder":
        add_root(db, "r", tmp_path / word)
    elif setup == "generic_folder":
        word = "新建文件夹"
        add_content(db, "k-g", [body(word, times=2) + body(word)])
        for index in range(3):
            add_content(db, f"k-g{index}", [body(word)])
            add_file(db, roots["p"], f"g{index}.docx", key=f"k-g{index}")
    elif setup == "ignored_name":
        db.execute(
            "INSERT INTO name_decisions(norm_key, name, decision, decided_at) VALUES (?, ?, 'ignored', ?)",
            (word, word, now),
        )
    elif setup == "suggestion":
        db.execute(
            """INSERT INTO glossary_suggestions(id, wrong, correct, status, created_at, updated_at)
               VALUES ('gs-1', '驻场服物', ?, 'pending', ?, ?)""",
            (word, now, now),
        )
    elif setup == "rejected":
        db.execute(
            """INSERT INTO glossary_candidates(project_id, term, term_key, status, created_at, updated_at)
               VALUES ('p', ?, ?, 'rejected', ?, ?)""",
            (word, word, now, now),
        )
    mine(db)
    assert (word, "") not in terms(db)
    assert ("司美格鲁肽", "") in terms(db)  # 别的照旧


# ---------------------------------------------------------------------- 数量和去留


def many_words_world(tmp_path, count=35):
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    # 用生僻的字造 35 个 4 字词，每个词在 3 份内容里各出现 2 次
    alphabet = [chr(code) for code in range(0x9A00, 0x9B00)]
    words = sorted({"".join(alphabet[(index * 4 + offset) % len(alphabet)] for offset in range(4)) for index in range(count)})
    contents = 12
    for number in range(contents):
        mine_words = [word for index, word in enumerate(words) if number in {index % contents, (index + 1) % contents, (index + 2) % contents}]
        add_content(db, f"k{number:02d}", [body(*mine_words)])
        add_file(db, root, f"文{number:02d}.docx", key=f"k{number:02d}")
    return db, words


def gm_db(tmp_path):
    from .gm_world import make_db

    return make_db(tmp_path)


def test_thirty_pending_and_the_rest_dropped_then_revived(tmp_path):
    db, words = many_words_world(tmp_path)
    mine(db)
    assert len(rows(db, status="pending")) == gm.PENDING_CAP
    assert len(rows(db, status="dropped")) == len(words) - gm.PENDING_CAP
    first_dropped = sorted(row["term"] for row in rows(db, status="dropped"))[0]
    gm.reject(db, "p", sorted(row["term_key"] for row in rows(db, status="pending"))[0], now=NOW)
    mine(db, now=NOW + timedelta(hours=7))
    assert len(rows(db, status="pending")) == gm.PENDING_CAP
    assert first_dropped in {row["term"] for row in rows(db, status="pending")}


def test_evidence_gone_drops_and_deleted_terms_become_rejected(tmp_path):
    db, _roots = world(tmp_path)
    mine(db)
    db.execute("UPDATE material_files SET gone_at = ? WHERE content_key = 'k3'", (utc_now(),))
    mine(db, now=NOW + timedelta(hours=7))
    assert ("驻场服务", "") in terms(db, status="dropped")  # 只剩 2 个文件、没说过
    result = gm.accept(db, "p", "司美格鲁肽", [], now=NOW + timedelta(hours=7))
    db.execute("DELETE FROM glossary_terms WHERE id = ?", (result["term"]["id"],))
    mine(db, now=NOW + timedelta(hours=14))
    assert ("司美格鲁肽", "") in terms(db, status="rejected")
    assert ("司美格鲁肽", "") not in terms(db)


def test_merge_keeps_your_answer_and_the_target_pending_yields(tmp_path):
    db, roots = world(tmp_path)
    mine(db)
    gm.reject(db, "p", "驻场服务", now=NOW)
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, status, created_at, updated_at)
           VALUES ('q', '驻场服务', '驻场服务', 'pending', ?, ?)""",
        (utc_now(), utc_now()),
    )
    with db.transaction() as connection:
        merge_project(connection, "p", "q")
    assert terms(db, "q", status="rejected") >= {("驻场服务", "")}
    assert ("驻场服务", "") not in terms(db, "q")


# ---------------------------------------------------------------------- 循环


def settings(**overrides):
    values = {"links_enabled": True, "glossary_mining_enabled": True, "links_backfill_days": 180,
              "semantic_model": "m"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_nothing_happens_while_transcribing(tmp_path):
    db, _roots = world(tmp_path)
    result = gm.mine_round(db, lambda: True, now=NOW)
    assert result == {"seeded": 0, "projects": 0, "stopped": "busy"}
    assert db.query_one("SELECT COUNT(*) AS n FROM glossary_mining_seeds")["n"] == 0


def test_busy_midway_stops_before_the_next_content(tmp_path):
    db, _roots = world(tmp_path)
    calls = {"n": 0}

    def busy():
        calls["n"] += 1
        return calls["n"] > 2  # 第一份之前不忙，第二份之前开始转写

    result = gm.mine_round(db, busy, now=NOW)
    assert result["seeded"] == 1 and result["stopped"] == "busy" and result["projects"] == 0


def test_budget_with_a_fake_clock(tmp_path):
    db, _roots = world(tmp_path)
    clock = {"t": 0.0}
    original = gm.extract_seeds

    def slow(text):
        clock["t"] += 1.5
        return original(text)

    gm_extract = gm.extract_seeds
    try:
        gm.extract_seeds = slow
        result = gm.mine_round(db, lambda: False, clock=lambda: clock["t"], now=NOW)
    finally:
        gm.extract_seeds = gm_extract
    # 种子最多 4 秒：挖了 3 份（0、1.5、3.0 时开始），之后还有到期的项目，至少做一个
    assert result["seeded"] == 3 and result["projects"] >= 1


def test_signature_interval_and_first_time(tmp_path):
    db, roots = world(tmp_path)
    first = gm.mine_round(db, lambda: False, now=NOW)
    assert first["projects"] == 3  # 第一次立刻做（三个项目）
    again = gm.mine_round(db, lambda: False, now=NOW + timedelta(minutes=5))
    assert again["projects"] == 0  # 签名没变
    add_meeting(db, "m9", date="2026-09-27T10:00:00", project_id="p", segments=["驻场服务要续签"])
    assert gm.mine_round(db, lambda: False, now=NOW + timedelta(hours=1))["projects"] == 0  # 6 小时内不做
    later = gm.mine_round(db, lambda: False, now=NOW + timedelta(hours=7))
    assert later["projects"] == 1


def test_miner_version_bump_remines(tmp_path, monkeypatch):
    db, _roots = world(tmp_path)
    gm.mine_round(db, lambda: False, now=NOW)
    with db.autocommit() as conn:
        assert gm.seeds_due(conn) == 0
    # _DUE_SEEDS 是模块加载时就拼好的字符串，MINER_VERSION 已经烤进去了；这里把它模拟成「又出了新版本」
    bumped = gm.MINER_VERSION + 1
    monkeypatch.setattr(
        gm, "_DUE_SEEDS", gm._DUE_SEEDS.replace(f"s.miner != {gm.MINER_VERSION}", f"s.miner != {bumped}")
    )
    monkeypatch.setattr(gm, "MINER_VERSION", bumped)
    with db.autocommit() as conn:
        assert gm.seeds_due(conn) == 5


def test_switched_off_does_nothing(tmp_path):
    db, _roots = world(tmp_path)
    w = deep_links.LinksWorker(db, settings(glossary_mining_enabled=False), now=lambda: NOW)
    ctx = SimpleNamespace(settings=w.settings)
    assert w.h4_terms(ctx) == "off"
    assert db.query_one("SELECT COUNT(*) AS n FROM glossary_mining_seeds")["n"] == 0


def test_material_fts_rebuild_only_seeds(tmp_path, monkeypatch):
    db, _roots = world(tmp_path)
    db.execute("INSERT INTO app_state(key, value, updated_at) VALUES (?, '1', ?)", (REBUILD_KEY, utc_now()))
    called = []
    monkeypatch.setattr(gm, "project_pass", lambda *args, **kwargs: called.append(args))
    result = gm.mine_round(db, lambda: False, now=NOW)
    assert result["seeded"] == 5 and called == []


def test_worker_runs_h4_in_the_heavy_phase(tmp_path):
    db, _roots = world(tmp_path)
    w = deep_links.LinksWorker(db, settings(), now=lambda: NOW)
    phases = w.run_round()["phases"]
    assert phases["terms"] == "done"
    assert ("司美格鲁肽", "") in terms(db)
    assert w.snapshot()["waiting"]["terms"] == 0


# ---------------------------------------------------------------------- 写


def test_unchanged_pass_writes_nothing(tmp_path):
    """一遍没有变化时主库一条写都不发（只写 temp 库里的 _gm_keys）。"""
    db, _roots = world(tmp_path)
    mine(db)
    writes: list[str] = []

    def trace(sql):
        head = sql.strip().upper()
        if head.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) and "TEMP._GM_KEYS" not in head:
            writes.append(sql)

    with db.autocommit() as conn:
        conn.set_trace_callback(trace)
        gm.project_pass(conn, "p", NOW + timedelta(hours=7))
        conn.set_trace_callback(None)
    assert writes == []


def test_no_insert_or_replace_on_candidates():
    package = Path(gm.__file__).parent
    offenders = [
        path.name
        for path in package.glob("*.py")
        if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+glossary_candidates", path.read_text(encoding="utf-8"), re.I)
    ]
    assert offenders == []


def test_caps_on_the_expensive_steps(tmp_path):
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_root(db, "p", tmp_path / "云图目录")
    alphabet = [chr(code) for code in range(0x9A00, 0x9C00)]
    words = ["".join(random.Random(index).sample(alphabet, 4)) for index in range(gm.AGG_LIMIT + 100)]
    fake = {f"k{index}": [(word, 2) for word in words] for index in range(2)}
    add_meeting(db, "m1", date="2026-09-20T10:00:00", project_id="p", segments=["，".join(words[:300])])
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    assert stats.queries["spread_materials"] <= gm.POOL_QUERY_CAP
    assert stats.queries["spread_meetings"] <= gm.SPREAD_CAP
    assert stats.queries["pair_bases"] <= gm.PAIR_BASES_CAP


def test_step_three_walks_the_ranked_pool_until_150_are_kept(tmp_path, monkeypatch):
    """第 3 步不先截到 150：按排好的顺序逐个查，别的项目也有的丢掉，直到留满 POOL_CAP 个（或查满上限）。"""
    monkeypatch.setattr(gm, "POOL_CAP", 5)
    db = gm_db(tmp_path)
    for pid, name in (("p", "云图AI"), ("q", "北辰仓"), ("r", "数据中台")):
        add_project(db, pid, name)
    roots = {pid: add_root(db, pid, tmp_path / pid) for pid in ("p", "q", "r")}
    words = [chr(0x9A00 + index) + "鹏鹤鹦" for index in range(10)]  # 按字排：前 3 个别的项目也有
    for index in range(3):
        add_content(db, f"k{index}", [body(*words)])
        add_file(db, roots["p"], f"方案{index}.docx", key=f"k{index}")
    for pid in ("q", "r"):
        add_content(db, f"k{pid}", [body(*words[:3])])
        add_file(db, roots[pid], "规范.docx", key=f"k{pid}")
    fake = {f"k{index}": [(word, 2) for word in words] for index in range(3)}
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    assert [item.term for item in stats.items] == sorted(words[3:8])
    assert stats.queries["spread_materials"] == 8
    monkeypatch.setattr(gm, "POOL_QUERY_CAP", 6)
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=fake)
    assert [item.term for item in stats.items] == sorted(words[3:6]) and stats.queries["spread_materials"] == 6


def test_write_transaction_is_short(tmp_path):
    db, _roots = world(tmp_path)
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=NOW)
    marks: dict[str, float] = {}

    def trace(sql):
        if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
            marks["begin"] = time.perf_counter()
        elif sql.strip().upper() == "COMMIT":
            marks["commit"] = time.perf_counter()

    with db.autocommit() as conn:
        conn.set_trace_callback(trace)
        gm.project_pass(conn, "p", NOW)
        conn.set_trace_callback(None)
    assert marks["commit"] - marks["begin"] < 0.05


def test_signature_change_between_compute_and_write_discards(tmp_path, monkeypatch):
    db, _roots = world(tmp_path)
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=NOW)
    original = gm.compute

    def racing(conn, project_id, **kwargs):
        result = original(conn, project_id, **kwargs)
        threading.Thread(
            target=lambda: add_meeting(db, "m-new", date="2026-09-27T10:00:00", project_id="p", segments=["新的会"])
        ).start()
        time.sleep(0.2)
        return result

    monkeypatch.setattr(gm, "compute", racing)
    with db.autocommit() as conn:
        stats = gm.project_pass(conn, "p", NOW)
    assert stats.stale
    assert rows(db) == []


def test_aggregation_memory(tmp_path):
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    now = utc_now()
    alphabet = [chr(code) for code in range(0x9A00, 0x9E00)]
    rng = random.Random(3)
    vocab = ["".join(rng.sample(alphabet, 4)) for _ in range(3000)]
    with db.transaction() as conn:
        for index in range(5000):
            key = f"k{index:05d}"
            conn.execute(
                "INSERT INTO material_contents(content_key, layer, state, created_at, updated_at) VALUES (?, 'text', 'done', ?, ?)",
                (key, now, now),
            )
            conn.execute(
                """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, zone, seen_at,
                       content_key) VALUES (?, ?, '', ?, ?, ?, 'docx', 'normal', ?, ?)""",
                (root, f"{key}.docx", f"{key}.docx", key, key, now, key),
            )
            picked = rng.sample(vocab, 24)
            conn.execute(
                "INSERT INTO glossary_mining_seeds(content_key, miner, source_sig, terms_json, mined_at) VALUES (?, 1, 'x', ?, ?)",
                (key, json.dumps([[word, 2] for word in picked], ensure_ascii=False), now),
            )
    with db.autocommit() as conn:
        tracemalloc.start()
        conn.execute(
            f"""WITH keys AS ({gm._project_keys_sql()})
                SELECT json_extract(j.value, '$[0]') AS term, COUNT(*) AS df FROM glossary_mining_seeds s
                  JOIN keys k ON k.content_key = s.content_key, json_each(s.terms_json) j
                 GROUP BY 1 ORDER BY df DESC LIMIT {gm.AGG_LIMIT}""",
            {"pid": "p"},
        ).fetchall()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert peak <= 10 * 1024 * 1024


def test_transcript_memory(tmp_path):
    """拼逐字稿：文字每字 2 字节，对照表每段约 20 字节（array），会议 id 每场只存一次；照这个比例，
    400 万字、12 万段常驻约 11MB，峰值约 21MB（H4 一轮峰值 30MB 以内）。"""
    db = gm_db(tmp_path)
    add_project(db, "p", "云图AI")
    rng = random.Random(5)
    alphabet = [chr(0x4E00 + index) for index in range(2000)]
    for meeting in range(10):
        add_meeting(db, f"m{meeting:02d}", date="2026-09-20T10:00:00", project_id="p",
                    segments=["".join(rng.choice(alphabet) for _ in range(60)) for _ in range(1000)])
    with db.autocommit() as conn:
        tracemalloc.start()
        transcript = gm.load_transcript(conn, "p")
        held, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    chars, segments = len(transcript.text), len(transcript.starts)
    assert (chars, segments, len(transcript.meetings)) == (10 * 1000 * 61 - 1, 10_000, 10)
    assert held <= 2.2 * chars + 48 * segments
    assert peak <= 4.5 * chars + 100 * segments
    position = transcript.text.index(transcript.text[61 * 1500 : 61 * 1500 + 8])
    assert transcript.where(position) == ("m01", 500_000)
    assert transcript.meeting_count([0, 5, position]) == 2


def test_pass_gives_up_when_busy_or_past_the_deadline_and_writes_no_scan_row(tmp_path):
    """汇总算到一半忙了或超了时间：这一遍放弃，候选词和 scan 行都不写，下一轮再做。"""
    db, _roots = world(tmp_path)
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=NOW)
    clock = {"t": 0.0}
    original = gm.load_transcript

    def slow(conn, project_id):
        clock["t"] += 10.0  # 读逐字稿用了 10 秒
        return original(conn, project_id)

    gm.load_transcript = slow
    try:
        with db.autocommit() as conn:
            stats = gm.project_pass(conn, "p", NOW, deadline=5.0, clock=lambda: clock["t"])
    finally:
        gm.load_transcript = original
    assert stats.abandoned == "budget"
    assert rows(db) == [] and db.query_all("SELECT * FROM glossary_mining_scan") == []
    calls = {"n": 0}

    def busy():
        calls["n"] += 1
        return calls["n"] > 2  # 第 3 步前不忙，第 6 步前开始转写

    with db.autocommit() as conn:
        stats = gm.project_pass(conn, "p", NOW, busy=busy)
    assert stats.abandoned == "busy" and rows(db) == []
    assert db.query_all("SELECT * FROM glossary_mining_scan") == []
    # 下一轮不忙就照常做完
    with db.autocommit() as conn:
        assert gm.project_pass(conn, "p", NOW).abandoned is None
    assert ("司美格鲁肽", "") in terms(db)
    assert db.query_one("SELECT COUNT(*) AS n FROM glossary_mining_scan")["n"] == 1


def test_mine_round_stops_when_a_pass_is_given_up(tmp_path):
    db, _roots = world(tmp_path)
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=NOW)
    state = {"busy": False}
    original = gm.load_transcript

    def turns_busy(conn, project_id):
        state["busy"] = True  # 读逐字稿的时候开始转写
        return original(conn, project_id)

    gm.load_transcript = turns_busy
    try:
        result = gm.mine_round(db, lambda: state["busy"], now=NOW)
    finally:
        gm.load_transcript = original
    assert result["projects"] == 0 and result["stopped"] == "busy"
    assert db.query_all("SELECT * FROM glossary_mining_scan") == []


# ---------------------------------------------------------------------- 命令行


def test_cli_links_words(tmp_path, monkeypatch, capsys):
    from meeting_workbench import cli

    db, _roots = world(tmp_path)
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(tmp_path / "workbench.sqlite3"))
    with db.autocommit() as conn:
        before = conn.execute("SELECT COUNT(*) FROM glossary_mining_seeds").fetchone()[0]
    assert cli.main(["links", "words", "--project", "p", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "司美格鲁肽（会上听成司美格鲁太）· 听到 2 次" in out
    assert "驻场服务 · 3 个文件" in out
    assert db.query_one("SELECT COUNT(*) AS n FROM glossary_mining_seeds")["n"] == before
    assert rows(db) == []  # --dry-run 什么都不写
    with db.autocommit() as conn:
        gm.seed_round(conn, float("inf"), lambda: False, now=NOW)
    assert cli.main(["links", "words", "--project", "p"]) == 0
    assert ("司美格鲁肽", "") in terms(db)
