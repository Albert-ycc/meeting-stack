"""glossary_mining 第 8 步：法规页判定的缓存按「一轮」算，150 个词共用一批材料时每份材料只读一次。"""

import pytest

from meeting_workbench import glossary_mining as gm

from .gm_world import make_db
from .test_material_search import add_content, add_file, add_root
from .test_search import add_project

CLAUSE_NUMBERS = [
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
    "二十一",
    "二十二",
]
BUSINESS_MATERIALS = 6
LAW_PAGES = 3


def word(index):
    """互不相干的三字词（相邻码位拼的，不会在别的词里再出现）。"""
    return "".join(chr(0x4E00 + 3 * index + step) for step in range(3))


BUSINESS = [word(index) for index in range(120)]
LAW = [word(200 + index) for index in range(15)]  # 只出现在法规页的条款片段里
FOOTER = [word(300 + index) for index in range(15)]  # 只出现在法规页没有编号的页脚片段里


def build_world(tmp_path):
    """业务材料 6 份：120 个业务词每份各出现一次，片段里没有条款编号。法规页 3 份：22 条条款
    （整份算法规页面），15 个法条词夹在条款片段里，15 个页脚词在没编号的页脚片段里。"""
    db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图目录")
    seeds = {}
    for number in range(BUSINESS_MATERIALS):
        key = f"kb{number}"
        chunks = [
            "业务说明：" + "、".join(BUSINESS[start : start + 10]) + "，由药房管理员登记入库单。"
            for start in range(0, len(BUSINESS), 10)
        ]
        add_content(db, key, chunks)
        add_file(db, root, f"doc{number}.txt", key=key)
        seeds[key] = [(term, 3) for term in BUSINESS]
    for number in range(LAW_PAGES):
        key = f"kr{number}"
        chunks = [
            f"第{clause}条 违规经营处一万元以上罚款。{LAW[index] if index < len(LAW) else ''}"
            for index, clause in enumerate(CLAUSE_NUMBERS)
        ]
        chunks += [
            "版权所有：" + "、".join(FOOTER[start : start + 3]) + " 网站标识码bm0999999"
            for start in range(0, len(FOOTER), 3)
        ]
        add_content(db, key, chunks)
        add_file(db, root, f"law{number}.txt", key=key)
        seeds[key] = [(term, 3) for term in LAW + FOOTER]
    return db, seeds


def old_law_ratio(conn, term):
    """改前的写法，原样留在这里当对拍的标准：判定缓存只在这一个词的这一次调用里有效。"""
    rows = conn.execute(
        """SELECT c.content_key, c.text FROM material_chunks_fts
             JOIN material_chunks c ON c.id = material_chunks_fts.rowid
            WHERE material_chunks_fts MATCH ? LIMIT ?""",
        (gm._fts_phrase(term), gm.LAW_CLAUSE_SAMPLE),
    ).fetchall()
    if not rows:
        return 0.0
    file_is_regulation: dict[str, bool] = {}
    hits = 0
    for row in rows:
        if gm._LAW_CLAUSE.search(row["text"] or ""):
            hits += 1
            continue
        key = row["content_key"]
        is_regulation = file_is_regulation.get(key)
        if is_regulation is None:
            is_regulation = gm._file_is_regulation(conn, key)
            file_is_regulation[key] = is_regulation
        if is_regulation:
            hits += 1
    return hits / len(rows)


def snapshot(stats):
    return [
        (item.term, item.df, item.total, item.names, item.spoken, item.meetings, item.text)
        for item in stats.items
    ]


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def test_each_material_is_read_once_however_many_terms_hit_it(world, monkeypatch):
    """改前：120 个业务词各把 6 份业务材料读一遍、15 个页脚词各把 3 份法规页读一遍，共 765 次。"""
    db, seeds = world
    reads = []
    real = gm._file_is_regulation

    def spy(conn, content_key):
        reads.append(content_key)
        return real(conn, content_key)

    monkeypatch.setattr(gm, "_file_is_regulation", spy)
    with db.autocommit() as conn:
        stats = gm.compute(conn, "p", seeds=seeds)

    assert stats.queries["law_ratio"] == len(BUSINESS) + len(LAW) + len(FOOTER)
    assert len(reads) == len(set(reads)) == BUSINESS_MATERIALS + LAW_PAGES


def test_kept_and_dropped_terms_are_the_same_as_with_the_per_term_cache(world, monkeypatch):
    db, seeds = world
    with db.autocommit() as conn:
        new = gm.compute(conn, "p", seeds=seeds)
    monkeypatch.setattr(gm, "_law_ratio", lambda conn, term, *_cache: old_law_ratio(conn, term))
    with db.autocommit() as conn:
        old = gm.compute(conn, "p", seeds=seeds)

    assert snapshot(new) == snapshot(old)
    # 业务词全留下，法条词和页脚词都当法规噪声丢掉，不是两边都空或都满
    assert {item.term for item in new.items} == set(BUSINESS)


def test_law_ratio_values_do_not_change(world):
    db, _seeds = world
    with db.autocommit() as conn:
        for term in (BUSINESS[0], BUSINESS[119], LAW[0], LAW[14], FOOTER[0], FOOTER[14]):
            assert gm._law_ratio(conn, term, {}) == old_law_ratio(conn, term), term
        assert gm._law_ratio(conn, BUSINESS[0], {}) == 0.0
        assert gm._law_ratio(conn, LAW[0], {}) == 1.0
        assert gm._law_ratio(conn, FOOTER[0], {}) == 1.0
