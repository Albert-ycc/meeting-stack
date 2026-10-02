"""asr_eval._edit_distance：位并行算法要和逐格填表的旧写法逐值相等，4000 字的样本不再要 2 秒。"""

import random
import time

from meeting_workbench import asr_eval


def table_distance(reference: str, hypothesis: str) -> int:
    """逐格填表的旧写法，原样留在这里当对拍的标准。"""
    if reference == hypothesis:
        return 0
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    if not hypothesis:
        return len(reference)
    previous = list(range(len(hypothesis) + 1))
    for row, reference_char in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_char in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_char != hypothesis_char),
                )
            )
        previous = current
    return previous[-1]


HAN = "".join(chr(0x4E00 + index) for index in range(3000))
ALPHABETS = [
    "ab",
    "abc",
    "abcdefghijklmnopqrstuvwxyz0123456789 ，。",
    HAN[:50],
    HAN,
    "a😀b𝒳",
]


def random_text(rng: random.Random, alphabet: str, length: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


def near_copy(rng: random.Random, text: str, alphabet: str) -> str:
    """在 text 上随机改、插、删几处。"""
    chars = list(text)
    for _ in range(rng.randint(0, 6)):
        operation = rng.choice("sid")
        if operation == "s" and chars:
            chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        elif operation == "i":
            chars.insert(rng.randint(0, len(chars)), rng.choice(alphabet))
        elif operation == "d" and chars:
            del chars[rng.randrange(len(chars))]
    return "".join(chars)


def test_matches_the_cell_by_cell_table_on_random_pairs():
    rng = random.Random(261002)
    pairs: list[tuple[str, str]] = []
    for _ in range(600):
        alphabet = rng.choice(ALPHABETS)
        first = random_text(rng, alphabet, rng.choice([0, 1, 2, 3, rng.randint(0, 40)]))
        kind = rng.choice(["random", "equal-length", "very-uneven", "near-copy", "empty"])
        if kind == "equal-length":
            second = random_text(rng, alphabet, len(first))
        elif kind == "very-uneven":
            second = random_text(rng, alphabet, rng.choice([0, 1, 2, 150, 300]))
        elif kind == "near-copy":
            second = near_copy(rng, first, alphabet)
        elif kind == "empty":
            second = ""
        else:
            second = random_text(rng, alphabet, rng.randint(0, 60))
        pairs.append((first, second))
    # 位宽的边界（Python 大整数每 30 位一档、机器字 64 位）上下各取几个长度
    for width in (1, 29, 30, 31, 32, 63, 64, 65, 127, 128, 129, 300):
        alphabet = rng.choice(ALPHABETS)
        base = random_text(rng, alphabet, width)
        pairs.append((base, near_copy(rng, base, alphabet)))
        pairs.append((base, random_text(rng, alphabet, width)))
        pairs.append((base, base[::-1]))
        pairs.append((base, base * 2))

    assert len(pairs) >= 600
    for first, second in pairs:
        expected = table_distance(first, second)
        assert asr_eval._edit_distance(first, second) == expected, (first, second)
        assert asr_eval._edit_distance(second, first) == expected, (second, first)


def test_edge_cases_keep_their_old_values():
    assert asr_eval._edit_distance("", "") == 0
    assert asr_eval._edit_distance("", "云图") == 2
    assert asr_eval._edit_distance("云图", "") == 2
    assert asr_eval._edit_distance("云图", "云图") == 0
    assert asr_eval._edit_distance("a", "b") == 1
    assert asr_eval._edit_distance("kitten", "sitting") == 3
    assert asr_eval._edit_distance("抗癌协会", "康癌协汇") == 2
    assert asr_eval._edit_distance("a" * 500, "a" * 500 + "b") == 1
    assert asr_eval._edit_distance("ab" * 200, "ba" * 200) == 2


def test_four_thousand_characters_finish_in_well_under_a_second():
    """改前约 1.8 秒（逐格填表）、改后约 10 毫秒；留 100 倍余量，只防退回逐格填表。"""
    rng = random.Random(261002)
    first = random_text(rng, HAN, asr_eval.MAX_SAMPLE_CHARACTERS)
    second = random_text(rng, HAN, asr_eval.MAX_SAMPLE_CHARACTERS)
    started = time.monotonic()

    distance = asr_eval._edit_distance(first, second)

    assert time.monotonic() - started < 1
    assert distance > 3900
