"""第四期 4d：相关（会上某一段和项目材料里某一段说的是一回事，并且两边都出现了同一个专有的词）。

- 切窗：当前逐字稿每 45 秒一个窗、每窗 90 秒宽（窗 k 覆盖 [k×45 秒，k×45 秒 + 90 秒)），start_ms 落在
  窗里的段用换行连起来，最多 480 字；汉字、字母、数字少于 60 个的窗跳过。text_sha = sha1(模型名 + 换行 +
  文字)，重算时只重新编码 text_sha 变了的窗。只用逐字稿，不用纪要。
- H3（links_loop 的重活，RelatedPass）：打开过、到期的会排在 H2 前面（一场会可以用完 15 秒）；材料一侧的
  增量（每轮 4,096 个新片段向量）和到期的会（3 场会或 8 秒）排在 H2 后面。编码一律走
  semantic.encode_texts(…, background=True)，每批 16 个窗拿一次 encode_lock；每场会之前、每批编码之间、
  每块打分之间看忙信号和停止标记，忙了最多一批以内停下，已经算的不写半截（只有编好的窗向量按批先存下，
  它们是派生数据，下一轮不用再编码）。
- 打分：范围是这场会所在项目的根目录里的活文件（不在「声档会议记录/」里）；vectors.snapshot() 每 16,384 行
  一块，只取本项目的行转 float32；矩阵里没有的从 material_chunk_vectors 补，最多 6 万行，超了跳过最旧的
  内容、记 partial。每窗一个门槛 bar = max(related_floor, p95 + related_margin)，p95 是这个窗对项目背景
  样本（最多 4,096 段、每份内容最多 4 段、按 sha1(content_key, ordinal) 取）的第 95 百分位；样本少于 200 段
  或 20 份内容时 bar = related_floor + 0.04。
- 候选依次过：得分不低于 bar；片段至少 40 字、汉字和字母至少占 30%；这份内容在项目里有活文件；同一个窗里
  每份内容只留一段；至少一个共同词。每窗存 3 个（栏里显示 2 个）。
- 共同词：本项目已确认词条（别名只在逐字稿一侧认，显示本名）、通用的已确认词条、能用的文件词干、项目线索词
  （去掉项目名和也叫）、两边动态找出的共同片段（3 到 12 个字）。动态片段和通用词条另过四项检查，按
  （项目，词）缓存 2 万条。共同词都在逐字稿里出现过，只进 meeting_window_passages.words 和
  relations.evidence_json，不进词条、候选词、项目线索、全文索引，也不发给任何大模型。
- 副本（这场会自己导出到资料盘的逐字稿）记进 copies_json；到处都相关的内容（至少 6 场会、超过已算过的
  会的一半）读的时候去掉。连成「相关」：至少 2 个窗，或 1 个窗但至少 2 个共同词；每场会最多 5 条；只写
  relations（kind='related'），只让 related_rev 加一。
- 分数只用来排序和过门槛，任何接口都不返回；只有命令行 links related 印出来（开发工具）。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import math
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import relations as relations_module
from .file_stems import COMMON_TWO_CHAR, STEM_YES, STOPWORDS, stem_usability
from .material_index import MATCH_ZONES
from .project_profile import (
    GENERIC_PROJECT_SPREAD,
    _spread_to_other_projects,
    also_name_list,
    build_cue_table,
    light_key,
)
from .semantic import SemanticUnavailable
from .text_scan import FormScanner, fold_with_map

logger = logging.getLogger(__name__)

RELATED_VERSION = 1
WINDOW_MS = 90_000
STEP_MS = 45_000
WINDOW_CHARS = 480
MIN_WINDOW_CHARS = 60
ENCODE_BATCH = 16
BLOCK_ROWS = 16_384
EXTRA_BLOCK = 4_096
EXTRA_MAX = 60_000
TOP_SCORED = 6
KEEP_PER_WINDOW = 3
SAMPLE_MAX = 4_096
SAMPLE_PER_CONTENT = 4
SAMPLE_MIN_CHUNKS = 200
SAMPLE_MIN_CONTENTS = 20
SMALL_SAMPLE_BUMP = 0.04
MIN_PASSAGE_CHARS = 40
MIN_LETTER_SHARE = 0.3
COPY_MIN_WINDOWS = 4
COPY_WINDOW_SHARE = 0.3
COPY_MIN_SCORE = 0.85
HUB_MIN_MEETINGS = 6
LINK_MIN_WINDOWS = 2
LINK_MIN_WORDS = 2
LINKS_PER_MEETING = 5
MAX_WORDS = 3
QUOTE_CHARS = 80
DYNAMIC_MIN = 3
DYNAMIC_MAX = 12
WORD_CACHE = 20_000
RARE_MIN = 3
RARE_SHARE = 0.05
GENERIC_MIN_MEETINGS = 4
# 预算：打开过的会可以用完整个重活预算；其余的会 3 场或 8 秒；增量每轮 4,096 个新片段，每块 1,024
REST_MEETINGS = 3
REST_SECONDS = 8.0
INCREMENT_CHUNKS = 4_096
INCREMENT_BLOCK = 1_024
# 增量打分时每次最多拿这么多个窗和一块片段相乘：2,048 窗 × 1,024 段的得分 8 MiB
WINDOW_SLICE = 2_048
WINDOW_FETCH = 1_024
PARAM_BATCH = 400
CHUNK_MARK_KEY = "related_chunk_mark"
DEFAULT_FLOOR = 0.60
DEFAULT_MARGIN = 0.05

# 共同词的来源，显示时按这个顺序
KIND_TERM = 0
KIND_STEM = 1
KIND_CUE = 2
KIND_DYNAMIC = 3
# 动态片段头尾去掉的虚字
_EDGE_FILLERS = frozenset(
    "的了是在我你他她它们这那和与及就也都还又把被给让对从向吗呢吧啊呀嘛哦嗯么个"
)
_LIVE_FILE = "f.gone_at IS NULL AND f.zone != 'cards' AND f.content_key IS NOT NULL"

# 命令行的刷掉原因
REASON_BELOW_BAR = "below_bar"
REASON_QUALITY = "quality"
REASON_NO_WORD = "no_word"
REASON_HUB = "hub"
REASON_COPY = "copy"
REASON_REJECTED = "rejected"
REASON_DEAD_KEY = "dead_key"
REASON_SAME_CONTENT = "same_content"
REASON_FULL = "full"


class Stop(Exception):
    """这一场会在半路停下：status 是 busy、stopping、budget 或 off。什么结果都不写。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


# ---------------------------------------------------------------------- 切窗


@dataclass
class Window:
    start_ms: int
    end_ms: int
    text: str
    chars: int
    # 每段在 text 里的 [起点, 终点)、开始时间、原文
    parts: list[tuple[int, int, int, str]] = field(default_factory=list)

    def segment_at(self, position: int) -> tuple[int, str]:
        """text 里这个位置落在哪一段：(开始时间, 原文)。"""
        for begin, end, start_ms, text in self.parts:
            if begin <= position < end:
                return start_ms, text
        start_ms, text = self.parts[-1][2], self.parts[-1][3]
        return start_ms, text


def meaningful(text: str) -> int:
    """汉字、字母、数字的个数。"""
    return sum(1 for char in text if char.isalnum())


def cut_windows(segments: Sequence[tuple[int, str]]) -> list[Window]:
    """当前逐字稿的段 (start_ms, text) 切成窗；不到 60 个汉字字母数字的窗不要。"""
    rows = sorted(
        ((int(start), str(text or "")) for start, text in segments), key=lambda item: item[0]
    )
    if not rows:
        return []
    last = rows[-1][0]
    windows: list[Window] = []
    for k in range(last // STEP_MS + 1):
        start = k * STEP_MS
        end = start + WINDOW_MS
        pieces = [
            (start_ms, text) for start_ms, text in rows if start <= start_ms < end and text.strip()
        ]
        if not pieces:
            continue
        parts: list[tuple[int, int, int, str]] = []
        buffer = ""
        for start_ms, text in pieces:
            if len(buffer) >= WINDOW_CHARS:
                break
            if buffer:
                buffer += "\n"
            begin = len(buffer)
            buffer += text
            parts.append((begin, min(len(buffer), WINDOW_CHARS), start_ms, text))
        buffer = buffer[:WINDOW_CHARS]
        parts = [part for part in parts if part[0] < len(buffer)]
        count = meaningful(buffer)
        if count < MIN_WINDOW_CHARS:
            continue
        windows.append(Window(start, end, buffer, count, parts))
    return windows


def text_sha(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode()).hexdigest()


def good_passage(text: str) -> bool:
    """片段至少 40 字，并且汉字和字母至少占 30%（去掉纯数字的表格）。"""
    body = "".join(str(text or "").split())
    if len(body) < MIN_PASSAGE_CHARS:
        return False
    letters = sum(1 for char in body if char.isalpha())
    return letters >= MIN_LETTER_SHARE * len(body)


def _to_f16(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float16).tobytes()


def _from_f16(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _batches(values: Sequence[Any], size: int = PARAM_BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _setting(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, default)
    return default if value is None else value


def model_of(settings: Any) -> str:
    return str(_setting(settings, "semantic_model", ""))


def enabled(settings: Any) -> bool:
    """三个开关都开着。"""
    return bool(
        _setting(settings, "links_enabled", False)
        and _setting(settings, "semantic_enabled", False)
        and _setting(settings, "material_content_enabled", False)
    )


# ---------------------------------------------------------------------- 签名和到期


def project_sigs(connection: Any, settings: Any) -> dict[str, str]:
    """每个项目的签名：项目 id、根目录 id、本项目已确认词条的条数和最后修改时间、项目名和也叫、两个门槛、
    模型名、RELATED_VERSION。三条语句算全部项目。"""
    roots: dict[str, list[int]] = {}
    for row in connection.execute(
        "SELECT project_id, id FROM project_material_roots ORDER BY id"
    ).fetchall():
        roots.setdefault(str(row["project_id"]), []).append(int(row["id"]))
    terms = {
        str(row["project_id"]): (int(row["n"]), str(row["latest"] or ""))
        for row in connection.execute(
            """SELECT project_id, COUNT(*) AS n, MAX(updated_at) AS latest FROM glossary_terms
                WHERE confirmed = 1 AND project_id IS NOT NULL GROUP BY project_id"""
        ).fetchall()
    }
    floor = float(_setting(settings, "related_floor", DEFAULT_FLOOR))
    margin = float(_setting(settings, "related_margin", DEFAULT_MARGIN))
    model = model_of(settings)
    sigs: dict[str, str] = {}
    for row in connection.execute("SELECT id, name, also_names FROM projects").fetchall():
        project_id = str(row["id"])
        parts = [
            project_id,
            ",".join(str(root) for root in roots.get(project_id, [])),
            "%d|%s" % terms.get(project_id, (0, "")),
            str(row["name"]),
            "|".join(also_name_list(row["also_names"])),
            f"{floor:.4f}",
            f"{margin:.4f}",
            model,
            str(RELATED_VERSION),
        ]
        sigs[project_id] = hashlib.sha1("\n".join(parts).encode()).hexdigest()[:24]
    return sigs


_DUE_WHERE = """(
    (m.project_id IS NOT NULL AND m.current_transcript_version_id IS NOT NULL
     AND (s.meeting_id IS NULL OR s.dirty > 0
          OR s.sig IS NOT (SELECT value FROM json_each(:sigs) WHERE key = m.project_id)))
    OR s.dirty > 0)"""


def due_meetings(
    connection: Any, sigs: dict[str, str], *, meeting_id: str | None = None
) -> list[dict[str, Any]]:
    """到期的会：有项目、有当前逐字稿，并且没有台账行、dirty > 0 或签名对不上；另有离开项目、没了逐字稿
    而 dirty > 0 的（要收拾旧结果）。一条语句，按录音日期从新到旧。"""
    extra = " AND m.id = :meeting" if meeting_id is not None else ""
    rows = connection.execute(
        f"""SELECT m.id, m.project_id, m.current_transcript_version_id AS version_id,
                   COALESCE(s.dirty, 0) AS dirty, s.meeting_id IS NOT NULL AS has_scan
              FROM meetings m LEFT JOIN meeting_related_scan s ON s.meeting_id = m.id
             WHERE {_DUE_WHERE}{extra}
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id""",
        {"sigs": json.dumps(sigs), "meeting": meeting_id},
    ).fetchall()
    return [dict(row) for row in rows]


def due_count(connection: Any, settings: Any) -> int:
    """健康检查的 waiting.related：到期的会数。"""
    sigs = project_sigs(connection, settings)
    row = connection.execute(
        f"""SELECT COUNT(*) FROM meetings m LEFT JOIN meeting_related_scan s ON s.meeting_id = m.id
             WHERE {_DUE_WHERE}""",
        {"sigs": json.dumps(sigs)},
    ).fetchone()
    return int(row[0])


def is_due(connection: Any, settings: Any, meeting_id: str) -> bool:
    return bool(due_meetings(connection, project_sigs(connection, settings), meeting_id=meeting_id))


def mark_dirty(
    connection: Any, *, meeting_id: str | None = None, project_id: str | None = None
) -> int:
    """links related --rebuild：只把 dirty 加一，不当场算。"""
    if meeting_id is not None:
        ids = [meeting_id]
    else:
        ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM meetings WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
    for part in _batches(ids):
        connection.execute(
            f"""INSERT INTO meeting_related_scan(meeting_id, dirty)
                SELECT id, 1 FROM meetings WHERE id IN ({_marks(part)})
                ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1""",
            list(part),
        )
    return len(ids)


# ---------------------------------------------------------------------- 项目的范围


@dataclass
class Scope:
    """一个项目的材料范围：内容标识到代表文件（修改时间最新的活文件）。"""

    project_id: str
    name: str
    also: list[str]
    root_ids: list[int]
    files: dict[str, dict[str, Any]]
    mtime: dict[str, int]

    @property
    def keys(self) -> set[str]:
        return set(self.files)


def load_scope(connection: Any, project_id: str) -> Scope | None:
    project = connection.execute(
        "SELECT id, name, also_names FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        return None
    root_ids = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM project_material_roots WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()
    ]
    files: dict[str, dict[str, Any]] = {}
    mtime: dict[str, int] = {}
    for row in connection.execute(
        f"""SELECT f.id, f.content_key, f.root_id, f.rel_path, f.name, f.ext, f.mtime_ns
              FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
             WHERE r.project_id = ? AND {_LIVE_FILE}""",
        (project_id,),
    ).fetchall():
        key = str(row["content_key"])
        current = files.get(key)
        rank = (int(row["mtime_ns"] or 0), int(row["id"]))
        if current is None or rank > (int(current["mtime_ns"] or 0), int(current["id"])):
            files[key] = dict(row)
        mtime[key] = max(mtime.get(key, 0), int(row["mtime_ns"] or 0))
    return Scope(
        project_id,
        str(project["name"]),
        also_name_list(project["also_names"]),
        root_ids,
        files,
        mtime,
    )


# ---------------------------------------------------------------------- 共同词


def _fold(text: str) -> tuple[str, list[int]]:
    return fold_with_map(text)


def _strip_fillers(text: str) -> str:
    start, end = 0, len(text)
    while start < end and text[start] in _EDGE_FILLERS:
        start += 1
    while end > start and text[end - 1] in _EDGE_FILLERS:
        end -= 1
    return text[start:end]


def common_pieces(left: str, right: str) -> list[str]:
    """两边动态找出的共同片段：3 字片段取交集，扩成最长的连续串，去掉头尾的虚字；3 到 12 个字、不全是
    数字、不在 STOPWORDS 里。按在 left 里出现的顺序，折叠后的写法。"""
    a, _ = _fold(left)
    b, _ = _fold(right)
    if len(a) < DYNAMIC_MIN or len(b) < DYNAMIC_MIN:
        return []
    grams = {b[index : index + DYNAMIC_MIN] for index in range(len(b) - DYNAMIC_MIN + 1)}
    found: list[str] = []
    seen: set[str] = set()
    index = 0
    while index <= len(a) - DYNAMIC_MIN:
        if a[index : index + DYNAMIC_MIN] not in grams:
            index += 1
            continue
        end = index + DYNAMIC_MIN
        while end < len(a) and a[index : end + 1] in b:
            end += 1
        piece = _strip_fillers(a[index:end])
        if (
            DYNAMIC_MIN <= len(piece) <= DYNAMIC_MAX
            and not piece.isdigit()
            and light_key(piece) not in STOPWORDS
            and piece not in seen
        ):
            seen.add(piece)
            found.append(piece)
        index = end
    return found


@dataclass
class Found:
    word: str
    kind: int
    position: int


class Words:
    """一个项目的共同词：逐字稿一侧和材料一侧各一个 FormScanner，加上动态片段和四项检查。"""

    def __init__(self, connection: Any, scope: Scope, checks: WordChecks):
        self.connection = connection
        self.scope = scope
        self.checks = checks
        self.names = {light_key(scope.name), *(light_key(name) for name in scope.also)}
        spoken: dict[str, tuple[str, int, bool]] = {}  # 写法 -> (显示, 来源, 要检查)
        written: dict[str, tuple[str, int, bool]] = {}

        def add(
            table: dict[str, tuple[str, int, bool]], form: str, shown: str, kind: int, check: bool
        ) -> None:
            form = (form or "").strip()
            if not form or light_key(form) in self.names or light_key(shown) in self.names:
                return
            table.setdefault(form, (shown, kind, check))

        for row in connection.execute(
            """SELECT term, aliases, also, project_id FROM glossary_terms
                WHERE confirmed = 1 AND (project_id = ? OR project_id IS NULL)
                ORDER BY project_id IS NULL, term""",
            (scope.project_id,),
        ).fetchall():
            term = str(row["term"] or "").strip()
            generic = row["project_id"] is None
            add(spoken, term, term, KIND_TERM, generic)
            add(written, term, term, KIND_TERM, generic)
            for alias in (*_json_list(row["aliases"]), *_json_list(row["also"])):
                if isinstance(alias, str):
                    add(spoken, alias, term, KIND_TERM, generic)
        if scope.root_ids:
            zones = ", ".join(f"'{zone}'" for zone in MATCH_ZONES)
            for row in connection.execute(
                f"""SELECT DISTINCT f.stem, f.stem_key FROM material_files f
                      JOIN project_material_roots r ON r.id = f.root_id
                     WHERE r.project_id = ? AND f.gone_at IS NULL AND f.zone IN ({zones}) AND f.stem_key != ''""",
                (scope.project_id,),
            ).fetchall():
                key = str(row["stem_key"])
                if stem_usability(key) != STEM_YES or key in STOPWORDS or key in COMMON_TWO_CHAR:
                    continue
                stem = str(row["stem"]).strip()
                add(spoken, stem, stem, KIND_STEM, False)
                add(written, stem, stem, KIND_STEM, False)
        for cue in checks.cues(scope.project_id):
            add(spoken, cue, cue, KIND_CUE, False)
            add(written, cue, cue, KIND_CUE, False)
        self.spoken = spoken
        self.written = written
        self.spoken_scanner = FormScanner(spoken)
        self.written_scanner = FormScanner(written)
        self._written_cache: dict[int, set[str]] = {}

    def in_window(self, window: Window) -> dict[str, Found]:
        """窗里出现的词（显示的写法 -> 第一次出现）。"""
        found: dict[str, Found] = {}
        for form, start, _end in self.spoken_scanner.matches(window.text):
            shown, kind, _check = self.spoken[form]
            if shown not in found:
                found[shown] = Found(shown, kind, start)
        return found

    def in_passage(self, chunk_id: int, text: str) -> set[str]:
        cached = self._written_cache.get(chunk_id)
        if cached is None:
            cached = {self.written[form][0] for form, _s, _e in self.written_scanner.matches(text)}
            self._written_cache[chunk_id] = cached
        return cached

    def shared(
        self, window: Window, spoken: dict[str, Found], chunk_id: int, text: str
    ) -> list[Found]:
        """这个窗和这个片段的共同词，最多 3 个：词条、词干、线索词、动态片段，同类按在窗里第一次出现的位置。"""
        written = self.in_passage(chunk_id, text)
        result: list[Found] = []
        for shown, hit in spoken.items():
            if shown not in written:
                continue
            check = self.spoken.get(shown, (shown, hit.kind, False))[2]
            if check and not self.checks.ok(self.connection, self.scope, shown):
                continue
            result.append(hit)
        taken = [light_key(item.word) for item in result]
        folded_window, positions = _fold(window.text)
        for piece in common_pieces(window.text, text):
            key = light_key(piece)
            if not key or key in self.names or any(key in other or other in key for other in taken):
                continue
            if not self.checks.ok(self.connection, self.scope, piece):
                continue
            at = folded_window.find(piece)
            result.append(
                Found(piece, KIND_DYNAMIC, positions[at] if 0 <= at < len(positions) else 0)
            )
            taken.append(key)
        result.sort(key=lambda item: (item.kind, item.position))
        return result[:MAX_WORDS]


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


class WordChecks:
    """动态片段和通用词条的四项检查，按（项目，词）在内存里缓存，最多 2 万条；线索词按项目缓存到下一轮。"""

    def __init__(self, limit: int = WORD_CACHE):
        self.limit = limit
        self._cache: OrderedDict[tuple[str, str], bool] = OrderedDict()
        self._cues: dict[str, list[str]] | None = None
        self._totals: dict[str, tuple[int, int]] = {}

    def new_round(self) -> None:
        self._cues = None
        self._totals = {}

    def cues(self, project_id: str) -> list[str]:
        """项目的线索词：已确认的线索词条和 3 字以上的文件夹名，去掉项目名和也叫（每场会都说）。"""
        if self._cues is None:
            return []
        return self._cues.get(project_id, [])

    def load_cues(self, connection: Any) -> None:
        if self._cues is not None:
            return
        table = build_cue_table(connection)
        self._cues = {
            project_id: [cue.text for cue in cues if cue.kind in ("folder", "term")]
            for project_id, cues in table.items()
        }

    def _project_totals(self, connection: Any, project_id: str) -> tuple[int, int]:
        """(项目内容份数, 项目的会数)，每轮一次。"""
        if project_id not in self._totals:
            contents = connection.execute(
                f"""SELECT COUNT(DISTINCT f.content_key) FROM material_files f
                      JOIN project_material_roots r ON r.id = f.root_id
                     WHERE r.project_id = ? AND {_LIVE_FILE}""",
                (project_id,),
            ).fetchone()[0]
            meetings = connection.execute(
                "SELECT COUNT(*) FROM meetings WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            self._totals[project_id] = (int(contents), int(meetings))
        return self._totals[project_id]

    def ok(self, connection: Any, scope: Scope, word: str) -> bool:
        key = (scope.project_id, word)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        verdict = self._check(connection, scope, word)
        self._cache[key] = verdict
        while len(self._cache) > self.limit:
            self._cache.popitem(last=False)
        return verdict

    def _check(self, connection: Any, scope: Scope, word: str) -> bool:
        # 不是项目名或也叫
        if light_key(word) in {light_key(scope.name), *(light_key(name) for name in scope.also)}:
            return False
        contents, meetings = self._project_totals(connection, scope.project_id)
        # 在项目里少见：含这个词的内容份数（数到门槛加一为止）不超过 max(3, 项目内容份数的 5%)
        limit = max(RARE_MIN, math.floor(contents * RARE_SHARE))
        if self._contents_with(connection, scope.project_id, word, limit + 1) > limit:
            return False
        # 在别的项目里不常见
        if _spread_to_other_projects(connection, word, scope.project_id) >= GENERIC_PROJECT_SPREAD:
            return False
        # 在项目的会里不泛滥：不能既出现在至少 4 场会、又超过一半的会里
        spoken = self._meetings_with(connection, scope.project_id, word)
        return not (spoken >= GENERIC_MIN_MEETINGS and spoken * 2 > meetings)

    @staticmethod
    def _contents_with(connection: Any, project_id: str, word: str, cap: int) -> int:
        live = f"""EXISTS (SELECT 1 FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                           WHERE f.content_key = c.content_key AND r.project_id = ? AND {_LIVE_FILE})"""
        if len(word) >= 3:
            sql = f"""SELECT COUNT(*) FROM (SELECT DISTINCT c.content_key FROM material_chunks_fts
                        JOIN material_chunks c ON c.id = material_chunks_fts.rowid
                       WHERE material_chunks_fts MATCH ? AND {live} LIMIT ?)"""
            params: tuple[Any, ...] = (_fts_phrase(word), project_id, cap)
        else:
            sql = f"""SELECT COUNT(*) FROM (SELECT DISTINCT c.content_key FROM material_chunks c
                       WHERE instr(c.text, ?) > 0 AND {live} LIMIT ?)"""
            params = (word, project_id, cap)
        return int(connection.execute(sql, params).fetchone()[0])

    @staticmethod
    def _meetings_with(connection: Any, project_id: str, word: str) -> int:
        if len(word) >= 3:
            row = connection.execute(
                """SELECT COUNT(DISTINCT m.id) FROM segments_fts
                     JOIN meetings m ON m.id = segments_fts.meeting_id
                          AND m.current_transcript_version_id = segments_fts.version_id
                    WHERE segments_fts MATCH ? AND m.project_id = ?""",
                (_fts_phrase(word), project_id),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT COUNT(DISTINCT m.id) FROM segments s
                     JOIN meetings m ON m.current_transcript_version_id = s.version_id
                    WHERE m.project_id = ? AND instr(s.text, ?) > 0""",
                (project_id, word),
            ).fetchone()
        return int(row[0])


# ---------------------------------------------------------------------- 打分


@dataclass
class Scored:
    """每个窗的前 6 个 (片段 id, 得分)，以及打分时看到的情况。"""

    ids: np.ndarray  # (窗数, 6)
    scores: np.ndarray  # (窗数, 6)
    bars: np.ndarray  # (窗数,)
    partial: bool
    mark: int


def _merge_top(
    best_ids: np.ndarray, best_scores: np.ndarray, ids: np.ndarray, scores: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """把一块的 (窗 × 行) 得分并进每个窗的前 6。"""
    if scores.size == 0:
        return best_ids, best_scores
    all_scores = np.concatenate([best_scores, scores], axis=1)
    all_ids = np.concatenate([best_ids, np.broadcast_to(ids, scores.shape)], axis=1)
    keep = min(TOP_SCORED, all_scores.shape[1])
    top = np.argpartition(-all_scores, keep - 1, axis=1)[:, :keep]
    return np.take_along_axis(all_ids, top, axis=1), np.take_along_axis(all_scores, top, axis=1)


def _sample_key(content_key: str, ordinal: int) -> str:
    return hashlib.sha1(f"{content_key}\n{ordinal}".encode()).hexdigest()


def pick_sample(rows: Iterable[tuple[int, str, int]]) -> list[int]:
    """背景样本：每份内容按 sha1(content_key, ordinal) 取最多 4 段，全部再按同一个值取最多 4,096 段。"""
    by_content: dict[str, list[tuple[str, int]]] = {}
    for chunk_id, content_key, ordinal in rows:
        # 每份内容边读边只留最小的 4 个（有序），不把 14 万段的 sha1 都攒着
        items = by_content.setdefault(content_key, [])
        entry = (_sample_key(content_key, ordinal), chunk_id)
        if len(items) < SAMPLE_PER_CONTENT:
            bisect.insort(items, entry)
        elif entry < items[-1]:
            bisect.insort(items, entry)
            items.pop()
    picked = [entry for items in by_content.values() for entry in items]
    picked.sort()
    return [chunk_id for _key, chunk_id in picked[:SAMPLE_MAX]]


def window_bars(
    windows: np.ndarray, sample: np.ndarray | None, contents: int, floor: float, margin: float
) -> np.ndarray:
    """每个窗的门槛：max(floor, p95 + margin)；样本少于 200 段或 20 份内容时 floor + 0.04。"""
    count = 0 if sample is None else sample.shape[0]
    if count < SAMPLE_MIN_CHUNKS or contents < SAMPLE_MIN_CONTENTS or sample is None:
        return np.full(windows.shape[0], floor + SMALL_SAMPLE_BUMP, dtype=np.float32)
    scores = windows @ sample.T
    p95 = np.percentile(scores, 95, axis=1)
    return np.maximum(floor, p95 + margin).astype(np.float32)


def _read_vectors(
    connection: Any, chunk_ids: Sequence[int], model: str
) -> tuple[np.ndarray, np.ndarray]:
    """按片段 id 从 material_chunk_vectors 读向量（float16 存，读成 float32）。"""
    ids: list[int] = []
    blobs: list[bytes] = []
    for part in _batches(list(chunk_ids)):
        for row in connection.execute(
            f"SELECT chunk_id, vector FROM material_chunk_vectors WHERE model = ? AND chunk_id IN ({_marks(part)})",
            [model, *part],
        ).fetchall():
            ids.append(int(row[0]))
            blobs.append(row[1])
    if not ids:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
    width = len(blobs[0])
    keep = [index for index, blob in enumerate(blobs) if len(blob) == width]
    vectors = np.frombuffer(b"".join(blobs[index] for index in keep), dtype=np.float16).reshape(
        len(keep), width // 2
    )
    return np.asarray([ids[index] for index in keep], dtype=np.int64), vectors.astype(np.float32)


_SCOPE_ROW = np.dtype([("id", np.int64), ("key", np.int32), ("ordinal", np.int64)])


def scope_chunks(connection: Any, keys: Sequence[str], model: str) -> np.ndarray:
    """本项目每个有向量的片段：(id, keys 里的下标, ordinal)，直接读成 numpy 结构数组，不建 Python 元组。"""
    order = {key: index for index, key in enumerate(keys)}
    parts: list[np.ndarray] = []
    for part in _batches(list(keys)):
        cursor = connection.execute(
            f"""SELECT v.chunk_id, c.content_key, c.ordinal FROM material_chunk_vectors v
                  JOIN material_chunks c ON c.id = v.chunk_id
                 WHERE v.model = ? AND c.content_key IN ({_marks(part)})""",
            [model, *part],
        )
        parts.append(
            np.fromiter(((row[0], order[row[1]], row[2]) for row in cursor), dtype=_SCOPE_ROW)
        )
    return np.concatenate(parts) if parts else np.zeros(0, dtype=_SCOPE_ROW)


def _scope_tuples(rows: np.ndarray, keys: Sequence[str]) -> Iterable[tuple[int, str, int]]:
    """给 pick_sample 用：按 4,096 行一段转回 (id, content_key, ordinal)，边转边用。"""
    for start in range(0, rows.shape[0], EXTRA_BLOCK):
        part = rows[start : start + EXTRA_BLOCK]
        for chunk_id, index, ordinal in zip(
            part["id"].tolist(), part["key"].tolist(), part["ordinal"].tolist(), strict=True
        ):
            yield chunk_id, keys[index], ordinal


@dataclass
class ProjectWindows:
    """增量用：一个项目里已算过的会的窗（一轮 increment 里各块之间共用，每个项目只读一次）。"""

    meeting_ids: list[str]
    meeting: np.ndarray  # (窗数,) int32，meeting_ids 的下标
    start_ms: np.ndarray  # (窗数,) int64
    bars: np.ndarray  # (窗数,) float32
    vectors: np.ndarray  # (窗数, 维数) float16，按原样存，打分时按切片转 float32


def load_project_windows(
    connection: Any, model: str, meeting_ids: Sequence[str]
) -> ProjectWindows | None:
    """先数窗，再用 fetchmany 边读边放进预先分好的数组（np.frombuffer 直接转，不建 Python float 列表）。"""
    ids = sorted(meeting_ids)
    total = 0
    for part in _batches(ids):
        total += int(
            connection.execute(
                f"SELECT COUNT(*) FROM meeting_windows WHERE model = ? AND meeting_id IN ({_marks(part)})",
                [model, *part],
            ).fetchone()[0]
        )
    if not total:
        return None
    index = {meeting_id: position for position, meeting_id in enumerate(ids)}
    meeting = np.empty(total, dtype=np.int32)
    starts = np.empty(total, dtype=np.int64)
    bars = np.empty(total, dtype=np.float32)
    vectors: np.ndarray | None = None
    filled = 0
    for part in _batches(ids):
        cursor = connection.execute(
            f"""SELECT meeting_id, start_ms, bar, vector FROM meeting_windows
                 WHERE model = ? AND meeting_id IN ({_marks(part)})""",
            [model, *part],
        )
        while filled < total:
            rows = cursor.fetchmany(WINDOW_FETCH)
            if not rows:
                break
            for row in rows:
                blob = row[3]
                if vectors is None:
                    vectors = np.empty((total, len(blob) // 2), dtype=np.float16)
                if filled >= total or len(blob) != vectors.shape[1] * 2:
                    continue
                vectors[filled] = np.frombuffer(blob, dtype=np.float16)
                meeting[filled] = index[str(row[0])]
                starts[filled] = int(row[1])
                bars[filled] = float(row[2])
                filled += 1
    if vectors is None or not filled:
        return None
    return ProjectWindows(ids, meeting[:filled], starts[:filled], bars[:filled], vectors[:filled])


def increment_hits(
    cached: ProjectWindows,
    window_marks: np.ndarray,
    matrix: np.ndarray,
    chunk_ids: np.ndarray,
    rows: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """窗 × 这块新片段：得分不低于窗的 bar、并且片段 id 大于这场会的 chunk_mark 的 (窗下标, 片段下标, 得分)。
    每次最多 WINDOW_SLICE 个窗转 float32 相乘，峰值和项目有多少窗无关。"""
    step = int(rows or WINDOW_SLICE)
    found_windows: list[np.ndarray] = []
    found_chunks: list[np.ndarray] = []
    found_scores: list[np.ndarray] = []
    for start in range(0, cached.vectors.shape[0], step):
        end = min(cached.vectors.shape[0], start + step)
        part = cached.vectors[start:end].astype(np.float32)
        scores = part @ matrix.T
        del part
        window_index, chunk_index = np.nonzero(scores >= cached.bars[start:end, None])
        keep = chunk_ids[chunk_index] > window_marks[start + window_index]
        window_index, chunk_index = window_index[keep], chunk_index[keep]
        found_windows.append(window_index + start)
        found_chunks.append(chunk_index)
        found_scores.append(scores[window_index, chunk_index])
        del scores
    if not found_windows:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, np.zeros(0, dtype=np.float32)
    return np.concatenate(found_windows), np.concatenate(found_chunks), np.concatenate(found_scores)


# ---------------------------------------------------------------------- 候选


@dataclass
class Passage:
    start_ms: int
    chunk_id: int
    content_key: str
    ordinal: int
    loc: str | None
    score: float
    words: list[str]
    seg_ms: int


def judge(
    window: Window,
    candidates: Sequence[tuple[int, float]],
    chunks: dict[int, dict[str, Any]],
    bar: float,
    scope: Scope,
    words: Words,
) -> tuple[list[Passage], list[dict[str, Any]]]:
    """一个窗的候选依次过：得分不低于 bar；片段质量；有活文件；每份内容一段；至少一个共同词。
    返回（留下的前 3 个，每个候选的去向——给命令行）。"""
    spoken = words.in_window(window)
    kept: list[Passage] = []
    verdicts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk_id, score in sorted(candidates, key=lambda item: -item[1]):
        chunk = chunks.get(int(chunk_id))
        verdict: dict[str, Any] = {
            "chunk_id": int(chunk_id),
            "score": float(score),
            "words": [],
            "reason": None,
        }
        verdicts.append(verdict)
        if chunk is None:
            verdict["reason"] = REASON_DEAD_KEY
            continue
        verdict["content_key"] = chunk["content_key"]
        verdict["ordinal"] = chunk["ordinal"]
        if score < bar:
            verdict["reason"] = REASON_BELOW_BAR
            continue
        if not good_passage(chunk["text"]):
            verdict["reason"] = REASON_QUALITY
            continue
        if chunk["content_key"] not in scope.files:
            verdict["reason"] = REASON_DEAD_KEY
            continue
        if chunk["content_key"] in seen:
            verdict["reason"] = REASON_SAME_CONTENT
            continue
        found = words.shared(window, spoken, int(chunk_id), chunk["text"])
        verdict["words"] = [item.word for item in found]
        if not found:
            verdict["reason"] = REASON_NO_WORD
            continue
        if len(kept) >= KEEP_PER_WINDOW:
            verdict["reason"] = REASON_FULL
            continue
        seen.add(chunk["content_key"])
        seg_ms, _text = window.segment_at(found[0].position)
        kept.append(
            Passage(
                window.start_ms,
                int(chunk_id),
                str(chunk["content_key"]),
                int(chunk["ordinal"]),
                chunk.get("loc"),
                float(score),
                [item.word for item in found],
                int(seg_ms),
            )
        )
    return kept, verdicts


def copies_of(windows: int, tops: Iterable[tuple[str, float]]) -> list[str]:
    """这场会自己的副本：一份内容在至少 max(4, 30%) 个有效窗里排第一，并且这些第一的平均得分至少 0.85。"""
    firsts: dict[str, list[float]] = {}
    for content_key, score in tops:
        firsts.setdefault(content_key, []).append(score)
    need = max(COPY_MIN_WINDOWS, math.ceil(windows * COPY_WINDOW_SHARE))
    return sorted(
        key
        for key, scores in firsts.items()
        if len(scores) >= need and sum(scores) / len(scores) >= COPY_MIN_SCORE
    )


def hub_keys(
    connection: Any, project_id: str, content_keys: Iterable[str], *, extra_scanned: int = 0
) -> set[str]:
    """到处都相关：出现在至少 6 场会的片段里，并且超过本项目已算过的会的一半（按
    idx_meeting_window_passages_content 现查）。"""
    keys = sorted(set(content_keys))
    if not keys:
        return set()
    scanned = (
        int(
            connection.execute(
                """SELECT COUNT(*) FROM meeting_related_scan s JOIN meetings m ON m.id = s.meeting_id
                WHERE m.project_id = ? AND s.scanned_at IS NOT NULL""",
                (project_id,),
            ).fetchone()[0]
        )
        + extra_scanned
    )
    hubs: set[str] = set()
    for part in _batches(keys):
        for row in connection.execute(
            f"""SELECT p.content_key, COUNT(DISTINCT p.meeting_id) AS n FROM meeting_window_passages p
                  JOIN meetings m ON m.id = p.meeting_id AND m.project_id = ?
                 WHERE p.content_key IN ({_marks(part)}) GROUP BY p.content_key""",
            [project_id, *part],
        ).fetchall():
            count = int(row["n"])
            if count >= HUB_MIN_MEETINGS and count * 2 > scanned:
                hubs.add(str(row["content_key"]))
    return hubs


def rejected_filter(
    connection: Any, meeting_id: str, project_id: str
) -> tuple[set[str], set[tuple[int, str]]]:
    """你标过不相关的：按 content_key 挡（别处的同内容副本也挡），或按 (root_id, rel_path) 挡（原地改过也挡）。"""
    keys: set[str] = set()
    places: set[tuple[int, str]] = set()
    for row in connection.execute(
        """SELECT content_key, root_id, rel_path FROM relations
            WHERE kind = 'related' AND meeting_id = ? AND project_id = ? AND status = 'rejected'""",
        (meeting_id, project_id),
    ).fetchall():
        if row["content_key"]:
            keys.add(str(row["content_key"]))
        if row["root_id"] is not None and row["rel_path"]:
            places.add((int(row["root_id"]), str(row["rel_path"])))
    return keys, places


def is_blocked(
    content_key: str, file: dict[str, Any] | None, rejected: tuple[set[str], set[tuple[int, str]]]
) -> bool:
    keys, places = rejected
    if content_key in keys:
        return True
    return bool(file and (int(file["root_id"]), str(file["rel_path"])) in places)


# ---------------------------------------------------------------------- 连成「相关」


def _quote_around(text: str, word: str) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= QUOTE_CHARS:
        return body
    at = body.find(word) if word else -1
    if at < 0:
        return body[: QUOTE_CHARS - 1] + "…"
    start = max(0, at - QUOTE_CHARS // 3)
    end = min(len(body), start + QUOTE_CHARS)
    start = max(0, end - QUOTE_CHARS)
    piece = body[start:end]
    if start:
        piece = "…" + piece[1:]
    if end < len(body):
        piece = piece[:-1] + "…"
    return piece


def link_rows(
    meeting_id: str,
    scope: Scope,
    passages: Sequence[dict[str, Any]],
    *,
    skip: set[str],
    segments: dict[int, str],
) -> list[dict[str, Any]]:
    """每份内容汇总这场会的片段（副本、到处相关的除外）：至少 2 个窗，或 1 个窗但至少 2 个共同词，才连；
    每场会最多 5 条，按（窗数，共同词数，得分）从大到小。passages 是表里的行（带 loc）。"""
    groups: dict[str, dict[str, Any]] = {}
    for row in passages:
        key = str(row["content_key"])
        if key in skip or key not in scope.files:
            continue
        words = json.loads(row["words"]) if isinstance(row["words"], str) else list(row["words"])
        group = groups.setdefault(key, {"windows": set(), "words": [], "best": None})
        group["windows"].add(int(row["start_ms"]))
        for word in words:
            if word not in group["words"]:
                group["words"].append(word)
        if group["best"] is None or float(row["score"]) > float(group["best"]["score"]):
            group["best"] = {**row, "words": words}
    ranked = []
    for key, group in groups.items():
        count = len(group["windows"])
        if count < LINK_MIN_WINDOWS and len(group["words"]) < LINK_MIN_WORDS:
            continue
        best = group["best"]
        ranked.append((count, len(group["words"]), float(best["score"]), key, group))
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    rows = []
    for count, _n, score, key, group in ranked[:LINKS_PER_MEETING]:
        best = group["best"]
        # 最好的那个窗的共同词在前，别的窗的补在后面
        words = list(best["words"])
        for word in group["words"]:
            if word not in words:
                words.append(word)
        words = words[:MAX_WORDS]
        seg_ms = int(best["seg_ms"])
        quote = _quote_around(segments.get(seg_ms, ""), best["words"][0] if best["words"] else "")
        file = scope.files[key]
        rows.append(
            {
                "kind": "related",
                "project_id": scope.project_id,
                "ident": f"{meeting_id}|{key}",
                "status": "shown",
                "origin": "vector",
                "meeting_id": meeting_id,
                "at_ms": seg_ms,
                "content_key": key,
                "root_id": int(file["root_id"]),
                "rel_path": str(file["rel_path"]),
                "file_id": int(file["id"]),
                "quote": quote,
                "score": round(score, 4),
                "evidence": {
                    "words": words,
                    "windows": count,
                    "meeting": {"at_ms": seg_ms, "quote": quote},
                    "material": {
                        "content_key": key,
                        "ordinal": int(best["ordinal"]),
                        "loc": best.get("loc"),
                    },
                },
            }
        )
    return rows


def _segments(connection: Any, version_id: str | None) -> list[tuple[int, str]]:
    if not version_id:
        return []
    return [
        (int(row[0]), str(row[1] or ""))
        for row in connection.execute(
            "SELECT start_ms, text FROM segments WHERE version_id = ? ORDER BY start_ms, ordinal",
            (version_id,),
        ).fetchall()
    ]


def _segment_texts(segments: Sequence[tuple[int, str]]) -> dict[int, str]:
    texts: dict[int, str] = {}
    for start_ms, text in segments:
        texts.setdefault(int(start_ms), text)
    return texts


def stored_passages(connection: Any, meeting_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """SELECT p.start_ms, p.rank, p.chunk_id, p.content_key, p.ordinal, p.score, p.words, p.seg_ms, c.loc
                 FROM meeting_window_passages p LEFT JOIN material_chunks c ON c.id = p.chunk_id
                WHERE p.meeting_id = ? ORDER BY p.start_ms, p.rank""",
            (meeting_id,),
        ).fetchall()
    ]


def write_links(
    connection: Any,
    meeting_id: str,
    scope: Scope,
    *,
    copies: Sequence[str],
    segments: dict[int, str],
    now: str,
    since: str,
    extra_scanned: int = 0,
) -> int:
    """按表里的片段重新汇总这场会的「相关」：upsert_system 写，再 clear_missing。只动 related_rev。"""
    passages = stored_passages(connection, meeting_id)
    hubs = hub_keys(
        connection,
        scope.project_id,
        [row["content_key"] for row in passages],
        extra_scanned=extra_scanned,
    )
    rows = link_rows(meeting_id, scope, passages, skip=set(copies) | hubs, segments=segments)
    written = relations_module.upsert_system(connection, rows, now, since=since)
    written += relations_module.clear_missing(
        connection,
        "related",
        scope.project_id,
        {"meeting_id": meeting_id},
        [row["ident"] for row in rows],
        now,
        since=since,
    )
    return written


# ---------------------------------------------------------------------- H3


def _stamp_now() -> str:
    from .db import utc_now

    return utc_now()


class RelatedPass:
    """links_loop 的 H3。状态（检查过的词、背景样本、上次看到的矩阵）留在内存里，跨轮复用。"""

    def __init__(self) -> None:
        self.checks = WordChecks()
        self.model_missing = False
        self._sample: tuple[tuple[Any, ...], np.ndarray | None, int] | None = None
        self._seen_matrix: Any = None
        self._round: str | None = None
        self._sigs: dict[str, str] = {}

    # ------------------------------------------------------------ 开跑前

    def _begin(self, ctx: Any) -> tuple[str | None, Any]:
        """开跑条件；过了返回 (None, 快照)，否则 (状态, None)。"""
        if not enabled(ctx.settings) or ctx.semantic is None or ctx.vectors is None:
            return "off", None
        if ctx.stopping():
            return "stopping", None
        if ctx.busy_now():
            return "busy", None
        snap = ctx.vectors.snapshot()
        if snap is None:
            return "waiting", None
        if self._round != ctx.since:
            self._round = ctx.since
            self.checks.new_round()
            with ctx.db.autocommit() as connection:
                self._sigs = project_sigs(connection, ctx.settings)
        # 矩阵重建完成（拿到的是新建的矩阵）：partial 的会 dirty 加一，重算一次
        if self._seen_matrix is not None and snap.code_of is not self._seen_matrix:
            with ctx.db.transaction() as connection:
                connection.execute(
                    "UPDATE meeting_related_scan SET dirty = dirty + 1 WHERE partial = 1"
                )
        self._seen_matrix = snap.code_of
        self._ensure_mark(ctx)
        return None, snap

    def _ensure_mark(self, ctx: Any) -> dict[str, Any]:
        """related_chunk_mark：第一次 H3 时建成当时的模型名和最大片段向量 id；模型变了或键不在就按那时
        新模型已有的最大片段向量 id 重设。"""
        model = model_of(ctx.settings)
        with ctx.db.autocommit() as connection:
            mark = read_mark(connection)
        if mark is not None and mark.get("model") == model:
            return mark
        with ctx.db.transaction() as connection:
            top = connection.execute(
                "SELECT COALESCE(MAX(chunk_id), 0) FROM material_chunk_vectors WHERE model = ?",
                (model,),
            ).fetchone()[0]
            fresh = {"model": model, "id": int(top)}
            connection.execute(
                """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (CHUNK_MARK_KEY, json.dumps(fresh), _stamp_now()),
            )
        return fresh

    # ------------------------------------------------------------ 两段

    def opened(self, ctx: Any, done: Callable[[str], None]) -> str:
        """打开过、到期的会（新的在前），一场可以用完整个重活预算；算完调 done。"""
        if not ctx.priorities:
            return "done"
        status, snap = self._begin(ctx)
        if status is not None:
            return status
        try:
            for meeting_id in ctx.priorities:
                halt = self._halt(ctx, ctx.deadline)
                if halt:
                    return halt
                with ctx.db.autocommit() as connection:
                    rows = due_meetings(connection, self._sigs, meeting_id=meeting_id)
                if not rows:
                    done(meeting_id)
                    continue
                result = self._compute_or_stop(ctx, snap, rows[0], ctx.deadline)
                if result != "done":
                    return result
                ctx.work += 1
                done(meeting_id)
            return "done"
        finally:
            del snap  # 每段重活结束就放掉快照

    def rest(self, ctx: Any) -> str:
        """材料一侧的增量（每轮最多 4,096 个新片段），再按会议新的在前算到期的会（3 场或 8 秒）。"""
        status, snap = self._begin(ctx)
        if status is not None:
            return status
        try:
            result = self.increment(ctx)
            if result not in ("done", "budget"):
                return result
            more = result == "budget"
            deadline = min(ctx.deadline, ctx.clock() + REST_SECONDS)
            with ctx.db.autocommit() as connection:
                rows = due_meetings(connection, self._sigs)
            for index, row in enumerate(rows):
                if index >= REST_MEETINGS:
                    return "budget"
                halt = self._halt(ctx, deadline)
                if halt:
                    return halt
                result = self._compute_or_stop(ctx, snap, row, deadline)
                if result != "done":
                    return result
                ctx.work += 1
            return "budget" if more else "done"
        finally:
            del snap

    @staticmethod
    def _halt(ctx: Any, deadline: float) -> str | None:
        if ctx.stopping():
            return "stopping"
        if ctx.busy_now():
            return "busy"
        if ctx.clock() >= deadline:
            return "budget"
        return None

    def _compute_or_stop(self, ctx: Any, snap: Any, row: dict[str, Any], deadline: float) -> str:
        try:
            self.compute(ctx, snap, row, deadline)
        except Stop as stop:
            return stop.status
        except SemanticUnavailable:
            self.model_missing = True
            logger.info("本地语义模型没装好，相关材料先不找")
            return "off"
        self.model_missing = False
        return "done"

    # ------------------------------------------------------------ 一场会

    def compute(self, ctx: Any, snap: Any, row: dict[str, Any], deadline: float) -> None:
        """一场会的完整计算。半路停下（忙、停止、预算）时抛 Stop，除了编好的窗向量什么都不写。"""
        meeting_id = str(row["id"])
        dirty = int(row["dirty"] or 0)
        model = model_of(ctx.settings)
        now = ctx.now.isoformat()
        project_id = row.get("project_id")
        with ctx.db.autocommit() as connection:
            scope = load_scope(connection, str(project_id)) if project_id else None
            segments = _segments(connection, row.get("version_id"))
        if scope is None or not row.get("version_id") or not scope.root_ids:
            note = (
                "no_project"
                if scope is None
                else ("no_transcript" if not row.get("version_id") else "no_roots")
            )
            self._write_empty(ctx, meeting_id, scope, dirty, note, now)
            return
        windows = cut_windows(segments)
        vectors = self._window_vectors(ctx, meeting_id, windows, model, deadline)
        with ctx.db.autocommit() as connection:
            self.checks.load_cues(connection)
            scored = (
                self.score(ctx, connection, snap, scope, vectors, deadline) if windows else None
            )
            chunk_rows = self._chunks(connection, scored)
            words = Words(connection, scope, self.checks)
            kept: list[Passage] = []
            tops: list[tuple[str, float]] = []
            for index, window in enumerate(windows):
                assert scored is not None
                candidates = [
                    (int(chunk_id), float(score))
                    for chunk_id, score in zip(scored.ids[index], scored.scores[index], strict=True)
                    if np.isfinite(score)
                ]
                top = next(
                    (
                        (chunk_rows[chunk_id]["content_key"], score)
                        for chunk_id, score in sorted(candidates, key=lambda item: -item[1])
                        if chunk_id in chunk_rows and good_passage(chunk_rows[chunk_id]["text"])
                    ),
                    None,
                )
                if top is not None:
                    tops.append(top)
                passages, _verdicts = judge(
                    window, candidates, chunk_rows, float(scored.bars[index]), scope, words
                )
                kept.extend(passages)
        copies = copies_of(len(windows), tops)
        seg_texts = _segment_texts(segments)
        with ctx.db.transaction() as connection:
            starts = [window.start_ms for window in windows]
            existing = {
                int(r[0])
                for r in connection.execute(
                    "SELECT start_ms FROM meeting_windows WHERE meeting_id = ? AND model = ?",
                    (meeting_id, model),
                ).fetchall()
            }
            for start in existing - set(starts):
                connection.execute(
                    "DELETE FROM meeting_windows WHERE meeting_id = ? AND model = ? AND start_ms = ?",
                    (meeting_id, model, start),
                )
            if scored is not None:
                for window, bar in zip(windows, scored.bars, strict=True):
                    connection.execute(
                        "UPDATE meeting_windows SET bar = ? WHERE meeting_id = ? AND model = ? AND start_ms = ?",
                        (round(float(bar), 4), meeting_id, model, window.start_ms),
                    )
            self._replace_passages(connection, meeting_id, kept)
            scanned_before = connection.execute(
                "SELECT scanned_at IS NOT NULL FROM meeting_related_scan WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            extra = 0 if scanned_before is not None and scanned_before[0] else 1
            write_links(
                connection,
                meeting_id,
                scope,
                copies=copies,
                segments=seg_texts,
                now=now,
                since=ctx.since,
                extra_scanned=extra,
            )
            self._write_scan(
                connection,
                meeting_id,
                sig=self._sigs.get(scope.project_id, ""),
                dirty=dirty,
                mark=scored.mark if scored is not None else 0,
                windows=len(windows),
                passages=len(kept),
                copies=copies,
                partial=bool(scored and scored.partial),
                note=None,
                now=now,
            )

    def _write_empty(
        self, ctx: Any, meeting_id: str, scope: Scope | None, dirty: int, note: str, now: str
    ) -> None:
        """没项目、没逐字稿、项目没挂根目录：删片段、留窗（文字没变），记 note。"""
        with ctx.db.transaction() as connection:
            connection.execute(
                "DELETE FROM meeting_window_passages WHERE meeting_id = ?", (meeting_id,)
            )
            if scope is not None:
                relations_module.clear_missing(
                    connection,
                    "related",
                    scope.project_id,
                    {"meeting_id": meeting_id},
                    [],
                    now,
                    since=ctx.since,
                )
            self._write_scan(
                connection,
                meeting_id,
                sig=self._sigs.get(scope.project_id, "") if scope else "",
                dirty=dirty,
                mark=0,
                windows=0,
                passages=0,
                copies=[],
                partial=False,
                note=note,
                now=now,
            )
        ctx.work += 1

    @staticmethod
    def _write_scan(
        connection: Any,
        meeting_id: str,
        *,
        sig: str,
        dirty: int,
        mark: int,
        windows: int,
        passages: int,
        copies: Sequence[str],
        partial: bool,
        note: str | None,
        now: str,
    ) -> None:
        """算完按 dirty 原值写：算的时候又改过（dirty 比读到的大），这场会仍是脏的。"""
        connection.execute(
            """INSERT INTO meeting_related_scan(meeting_id, sig, dirty, chunk_mark, windows, passages, copies_json,
                   partial, note, scanned_at)
               SELECT :meeting, :sig, 0, :mark, :windows, :passages, :copies, :partial, :note, :now
                WHERE EXISTS (SELECT 1 FROM meetings WHERE id = :meeting)
               ON CONFLICT(meeting_id) DO UPDATE SET sig = excluded.sig, chunk_mark = excluded.chunk_mark,
                   windows = excluded.windows, passages = excluded.passages, copies_json = excluded.copies_json,
                   partial = excluded.partial, note = excluded.note, scanned_at = excluded.scanned_at,
                   dirty = MAX(0, meeting_related_scan.dirty - :dirty)""",
            {
                "meeting": meeting_id,
                "sig": sig,
                "mark": int(mark),
                "windows": windows,
                "passages": passages,
                "copies": json.dumps(list(copies), ensure_ascii=False),
                "partial": int(partial),
                "note": note,
                "now": now,
                "dirty": int(dirty),
            },
        )

    @staticmethod
    def _replace_passages(connection: Any, meeting_id: str, kept: Sequence[Passage]) -> None:
        connection.execute(
            "DELETE FROM meeting_window_passages WHERE meeting_id = ?", (meeting_id,)
        )
        ranks: dict[int, int] = {}
        for passage in kept:
            rank = ranks.get(passage.start_ms, 0)
            ranks[passage.start_ms] = rank + 1
            connection.execute(
                """INSERT INTO meeting_window_passages(meeting_id, start_ms, rank, chunk_id, content_key, ordinal,
                       score, words, seg_ms)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM material_chunks WHERE id = ?)""",
                (
                    meeting_id,
                    passage.start_ms,
                    rank,
                    passage.chunk_id,
                    passage.content_key,
                    passage.ordinal,
                    round(passage.score, 4),
                    json.dumps(passage.words, ensure_ascii=False),
                    passage.seg_ms,
                    passage.chunk_id,
                ),
            )

    def _window_vectors(
        self, ctx: Any, meeting_id: str, windows: Sequence[Window], model: str, deadline: float
    ) -> np.ndarray:
        """窗的向量：text_sha 没变的用存下的，变了的每 16 个一批编码（每批拿一次编码锁），编好一批先存一批。"""
        with ctx.db.autocommit() as connection:
            stored = {
                int(row["start_ms"]): (str(row["text_sha"]), row["vector"], float(row["bar"]))
                for row in connection.execute(
                    "SELECT start_ms, text_sha, vector, bar FROM meeting_windows WHERE meeting_id = ? AND model = ?",
                    (meeting_id, model),
                ).fetchall()
            }
        floor = float(_setting(ctx.settings, "related_floor", DEFAULT_FLOOR))
        result: dict[int, np.ndarray] = {}
        todo: list[tuple[Window, str]] = []
        for window in windows:
            sha = text_sha(model, window.text)
            old = stored.get(window.start_ms)
            if old is not None and old[0] == sha:
                result[window.start_ms] = _from_f16(old[1])
            else:
                todo.append((window, sha))
        for start in range(0, len(todo), ENCODE_BATCH):
            halt = self._halt(ctx, deadline)
            if halt:
                raise Stop(halt)
            batch = todo[start : start + ENCODE_BATCH]
            encoded = np.asarray(
                ctx.semantic.encode_texts([window.text for window, _sha in batch], background=True),
                dtype=np.float32,
            )
            with ctx.db.transaction() as connection:
                for (window, sha), vector in zip(batch, encoded, strict=True):
                    old = stored.get(window.start_ms)
                    connection.execute(
                        """INSERT INTO meeting_windows(meeting_id, model, start_ms, end_ms, text_sha, chars, bar, vector)
                           SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM meetings WHERE id = ?)
                           ON CONFLICT(meeting_id, model, start_ms) DO UPDATE SET end_ms = excluded.end_ms,
                               text_sha = excluded.text_sha, chars = excluded.chars, vector = excluded.vector""",
                        (
                            meeting_id,
                            model,
                            window.start_ms,
                            window.end_ms,
                            sha,
                            window.chars,
                            old[2] if old is not None else floor,
                            _to_f16(vector),
                            meeting_id,
                        ),
                    )
                    result[window.start_ms] = _from_f16(_to_f16(vector))
        if not windows:
            return np.zeros((0, 0), dtype=np.float32)
        return np.vstack([result[window.start_ms] for window in windows])

    # ------------------------------------------------------------ 打分

    def score(
        self,
        ctx: Any,
        connection: Any,
        snap: Any,
        scope: Scope,
        windows: np.ndarray,
        deadline: float,
    ) -> Scored:
        """窗 × 本项目片段：快照里的每 16,384 行一块，矩阵外的从库里补（最多 6 万行）。每窗留前 6。"""
        model = model_of(ctx.settings)
        count = windows.shape[0]
        best_ids = np.full((count, 0), -1, dtype=np.int64)
        best_scores = np.full((count, 0), -np.inf, dtype=np.float32)
        top = connection.execute(
            "SELECT COALESCE(MAX(chunk_id), 0) FROM material_chunk_vectors WHERE model = ?",
            (model,),
        ).fetchone()[0]
        allowed_codes = np.fromiter(
            (snap.code_of[key] for key in scope.files if key in snap.code_of), dtype=np.int32
        )
        # 片段 id 一律放 numpy 数组（14.3 万段约 1 MiB），不建 Python 集合或元组
        in_matrix: list[np.ndarray] = []
        n = int(snap.n)
        if allowed_codes.size and n and snap.dim == windows.shape[1]:
            for start in range(0, n, BLOCK_ROWS):
                halt = self._halt(ctx, deadline)
                if halt:
                    raise Stop(halt)
                end = min(n, start + BLOCK_ROWS)
                mask = snap.valid[start:end] & np.isin(snap.codes[start:end], allowed_codes)
                if not mask.any():
                    continue
                picked = np.flatnonzero(mask) + start
                in_matrix.append(snap.ids[picked])
                # 一块里选中的行再按 4,096 行转 float32，不同时留一块的 float16 副本和 float32 副本
                for offset in range(0, picked.size, EXTRA_BLOCK):
                    rows_at = picked[offset : offset + EXTRA_BLOCK]
                    rows = snap.vectors[rows_at].astype(np.float32)
                    best_ids, best_scores = _merge_top(
                        best_ids, best_scores, snap.ids[rows_at], windows @ rows.T
                    )
                    del rows
        # 矩阵外的行：只读 id（不带向量），按内容的修改时间从新到旧，最多 6 万行
        keys = sorted(scope.files, key=lambda key: -scope.mtime.get(key, 0))
        everything = scope_chunks(connection, keys, model)
        all_ids = everything["id"]
        matrix_ids = np.concatenate(in_matrix) if in_matrix else np.zeros(0, dtype=np.int64)
        outside = np.isin(all_ids, matrix_ids, invert=True)
        missing_ids, missing_keys = all_ids[outside], everything["key"][outside]
        missing = missing_ids[np.lexsort((missing_ids, missing_keys))]
        del outside, missing_ids, missing_keys, matrix_ids
        partial = missing.size > EXTRA_MAX
        missing = missing[:EXTRA_MAX]
        for start in range(0, missing.size, EXTRA_BLOCK):
            halt = self._halt(ctx, deadline)
            if halt:
                raise Stop(halt)
            ids, rows = _read_vectors(
                connection, missing[start : start + EXTRA_BLOCK].tolist(), model
            )
            if ids.size and rows.shape[1] == windows.shape[1]:
                best_ids, best_scores = _merge_top(best_ids, best_scores, ids, windows @ rows.T)
        mark = max(int(top), int(all_ids.max())) if all_ids.size else int(top)
        sample = self._background(connection, scope, everything, keys, model)
        floor = float(_setting(ctx.settings, "related_floor", DEFAULT_FLOOR))
        margin = float(_setting(ctx.settings, "related_margin", DEFAULT_MARGIN))
        contents = int(np.unique(everything["key"]).size)
        if sample is not None and sample.shape[1] != windows.shape[1]:
            sample = None
        bars = window_bars(windows, sample, contents, floor, margin)
        return Scored(best_ids, best_scores, bars, partial, mark)

    def _background(
        self, connection: Any, scope: Scope, everything: np.ndarray, keys: Sequence[str], model: str
    ) -> np.ndarray | None:
        """项目的背景样本（只留最近用的一个项目，约 8MB）。everything 是 scope_chunks 的结构数组。"""
        count = int(everything.shape[0])
        key = (scope.project_id, model, count, int(everything["id"].max()) if count else 0)
        if self._sample is not None and self._sample[0] == key:
            return self._sample[1]
        picked = pick_sample(_scope_tuples(everything, keys))
        vectors: np.ndarray | None = None
        if picked:
            _ids, rows = _read_vectors(connection, picked, model)
            vectors = rows if rows.size else None
        self._sample = (key, vectors, len(picked))
        return vectors

    @staticmethod
    def _chunks(connection: Any, scored: Scored | None) -> dict[int, dict[str, Any]]:
        if scored is None:
            return {}
        ids = sorted({int(value) for value in scored.ids.ravel().tolist() if value >= 0})
        rows: dict[int, dict[str, Any]] = {}
        for part in _batches(ids):
            for row in connection.execute(
                f"SELECT id, content_key, ordinal, loc, start_ms, text FROM material_chunks WHERE id IN ({_marks(part)})",
                list(part),
            ).fetchall():
                rows[int(row["id"])] = dict(row)
        return rows

    # ------------------------------------------------------------ 增量

    def increment(self, ctx: Any) -> str:
        """材料一侧的增量：读 id 大于 related_chunk_mark 的片段向量（每轮最多 4,096 个，每块 1,024），按活文件
        映射到项目，对这些项目里已算过、干净的会的窗打分；只对 chunk_mark 小于这个片段 id 的会、过了那个窗的
        bar 的才算。每块一个事务，标记和结果一起写。"""
        model = model_of(ctx.settings)
        with ctx.db.autocommit() as connection:
            mark = read_mark(connection) or {"model": model, "id": 0}
            rows = connection.execute(
                """SELECT v.chunk_id, v.vector, c.content_key, c.ordinal, c.loc, c.start_ms, c.text
                     FROM material_chunk_vectors v JOIN material_chunks c ON c.id = v.chunk_id
                    WHERE v.model = ? AND v.chunk_id > ? ORDER BY v.chunk_id LIMIT ?""",
                (model, int(mark["id"]), INCREMENT_CHUNKS),
            ).fetchall()
        if not rows:
            return "done"
        # 本轮各块之间共用：每个项目的窗只读一次
        windows: dict[str, ProjectWindows | None] = {}
        for start in range(0, len(rows), INCREMENT_BLOCK):
            halt = self._halt(ctx, ctx.deadline)
            if halt:
                return halt
            block = [dict(row) for row in rows[start : start + INCREMENT_BLOCK]]
            self._increment_block(ctx, block, model, windows)
            ctx.work += 1
        return "budget" if len(rows) >= INCREMENT_CHUNKS else "done"

    def _increment_block(
        self,
        ctx: Any,
        block: list[dict[str, Any]],
        model: str,
        windows: dict[str, ProjectWindows | None] | None = None,
    ) -> None:
        if windows is None:
            windows = {}
        now = ctx.now.isoformat()
        last = int(block[-1]["chunk_id"])
        keys = sorted({str(row["content_key"]) for row in block})
        with ctx.db.autocommit() as connection:
            projects: dict[str, set[str]] = {}
            for part in _batches(keys):
                for row in connection.execute(
                    f"""SELECT DISTINCT r.project_id, f.content_key FROM material_files f
                          JOIN project_material_roots r ON r.id = f.root_id
                         WHERE f.content_key IN ({_marks(part)}) AND {_LIVE_FILE}""",
                    list(part),
                ).fetchall():
                    projects.setdefault(str(row[0]), set()).add(str(row[1]))
            self.checks.load_cues(connection)
            plans: list[tuple[Scope, str, list[Passage], dict[str, Any]]] = []
            for project_id, contents in projects.items():
                sig = self._sigs.get(project_id)
                scope = load_scope(connection, project_id)
                if sig is None or scope is None:
                    continue
                chunks = [row for row in block if row["content_key"] in contents]
                plans.extend(
                    self._increment_project(ctx, connection, scope, sig, chunks, model, windows)
                )
        with ctx.db.transaction() as connection:
            for scope, meeting_id, passages, info in plans:
                self._merge_passages(connection, meeting_id, passages)
                write_links(
                    connection,
                    meeting_id,
                    scope,
                    copies=info["copies"],
                    segments=info["segments"],
                    now=now,
                    since=ctx.since,
                )
                connection.execute(
                    "UPDATE meeting_related_scan SET passages = (SELECT COUNT(*) FROM meeting_window_passages "
                    "WHERE meeting_id = ?) WHERE meeting_id = ?",
                    (meeting_id, meeting_id),
                )
            connection.execute(
                """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (CHUNK_MARK_KEY, json.dumps({"model": model, "id": last}), now),
            )

    def _increment_project(
        self,
        ctx: Any,
        connection: Any,
        scope: Scope,
        sig: str,
        chunks: list[dict[str, Any]],
        model: str,
        windows: dict[str, ProjectWindows | None],
    ) -> list[tuple[Scope, str, list[Passage], dict[str, Any]]]:
        meetings = {
            str(row["meeting_id"]): dict(row)
            for row in connection.execute(
                """SELECT s.meeting_id, s.chunk_mark, s.copies_json, m.current_transcript_version_id AS version_id
                     FROM meeting_related_scan s JOIN meetings m ON m.id = s.meeting_id
                    WHERE m.project_id = ? AND s.dirty = 0 AND s.sig = ? AND s.note IS NULL""",
                (scope.project_id, sig),
            ).fetchall()
        }
        if not meetings or not chunks:
            return []
        width = len(chunks[0]["vector"])
        chunks = [row for row in chunks if len(row["vector"]) == width]
        matrix = (
            np.frombuffer(b"".join(row["vector"] for row in chunks), dtype=np.float16)
            .reshape(len(chunks), width // 2)
            .astype(np.float32)
        )
        chunk_ids = np.asarray([int(row["chunk_id"]) for row in chunks], dtype=np.int64)
        by_id = {int(row["chunk_id"]): row for row in chunks}
        hits: dict[str, dict[int, list[tuple[int, float]]]] = {}
        if scope.project_id not in windows:
            windows[scope.project_id] = load_project_windows(connection, model, list(meetings))
        cached = windows[scope.project_id]
        if cached is not None and cached.vectors.shape[1] == matrix.shape[1]:
            # 本轮读窗以后才变干净的会不在缓存里，下次完整计算会补上；已经不干净的会 mark 取最大，不算
            never = np.iinfo(np.int64).max
            per_meeting = np.fromiter(
                (
                    int(meetings[meeting_id]["chunk_mark"]) if meeting_id in meetings else never
                    for meeting_id in cached.meeting_ids
                ),
                dtype=np.int64,
                count=len(cached.meeting_ids),
            )
            found = increment_hits(cached, per_meeting[cached.meeting], matrix, chunk_ids)
            for window_index, chunk_index, score in zip(
                *(part.tolist() for part in found), strict=True
            ):
                meeting_id = cached.meeting_ids[cached.meeting[window_index]]
                bucket = hits.setdefault(meeting_id, {}).setdefault(
                    int(cached.start_ms[window_index]), []
                )
                bucket.append((int(chunk_ids[chunk_index]), float(score)))
        plans = []
        words = Words(connection, scope, self.checks) if hits else None
        for meeting_id, by_window in hits.items():
            assert words is not None
            info = meetings[meeting_id]
            segments = _segments(connection, info["version_id"])
            cut = {window.start_ms: window for window in cut_windows(segments)}
            old = stored_passages(connection, meeting_id)
            chunk_rows = {int(chunk_id): {**by_id[chunk_id], "id": chunk_id} for chunk_id in by_id}
            texts = self._chunks_by_id(connection, [int(row["chunk_id"]) for row in old])
            chunk_rows.update(texts)
            bars = {
                int(row[0]): float(row[1])
                for row in connection.execute(
                    "SELECT start_ms, bar FROM meeting_windows WHERE meeting_id = ? AND model = ?",
                    (meeting_id, model),
                ).fetchall()
            }
            merged: list[Passage] = []
            for start_ms, fresh in by_window.items():
                window = cut.get(start_ms)
                if window is None:
                    continue
                candidates = [
                    (int(row["chunk_id"]), float(row["score"]))
                    for row in old
                    if int(row["start_ms"]) == start_ms
                ]
                candidates += fresh
                kept, _verdicts = judge(
                    window, candidates, chunk_rows, bars.get(start_ms, 1.0), scope, words
                )
                merged.extend(kept)
                if not kept:
                    merged.append(Passage(start_ms, -1, "", 0, None, 0.0, [], 0))  # 这个窗清空
            plans.append(
                (
                    scope,
                    meeting_id,
                    merged,
                    {
                        "copies": _json_list(info["copies_json"]),
                        "segments": _segment_texts(segments),
                    },
                )
            )
        return plans

    @staticmethod
    def _chunks_by_id(connection: Any, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        for part in _batches(sorted(set(ids))):
            for row in connection.execute(
                f"SELECT id, content_key, ordinal, loc, start_ms, text FROM material_chunks WHERE id IN ({_marks(part)})",
                list(part),
            ).fetchall():
                rows[int(row["id"])] = dict(row)
        return rows

    @staticmethod
    def _merge_passages(connection: Any, meeting_id: str, passages: Sequence[Passage]) -> None:
        """增量：只重写有新候选的窗。"""
        for start in sorted({passage.start_ms for passage in passages}):
            connection.execute(
                "DELETE FROM meeting_window_passages WHERE meeting_id = ? AND start_ms = ?",
                (meeting_id, start),
            )
            rank = 0
            for passage in passages:
                if passage.start_ms != start or passage.chunk_id < 0:
                    continue
                connection.execute(
                    """INSERT INTO meeting_window_passages(meeting_id, start_ms, rank, chunk_id, content_key, ordinal,
                           score, words, seg_ms)
                       SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM material_chunks WHERE id = ?)""",
                    (
                        meeting_id,
                        start,
                        rank,
                        passage.chunk_id,
                        passage.content_key,
                        passage.ordinal,
                        round(passage.score, 4),
                        json.dumps(passage.words, ensure_ascii=False),
                        passage.seg_ms,
                        passage.chunk_id,
                    ),
                )
                rank += 1


def read_mark(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT value FROM app_state WHERE key = ?", (CHUNK_MARK_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or "id" not in value:
        return None
    return value


# ---------------------------------------------------------------------- 命令行（开发工具，印分数）


def _clock_ms(text: str) -> int:
    parts = [int(part) for part in text.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds * 1000


def explain_meeting(
    connection: Any,
    settings: Any,
    meeting_id: str,
    *,
    at: str | None = None,
    floor: float | None = None,
    margin: float | None = None,
    encode: Callable[[list[str]], np.ndarray] | None = None,
) -> dict[str, Any]:
    """links related --meeting：每个窗的文字开头、bar、前 6 个的得分、共同词和刷掉的原因。用存下的窗
    （--encode 时重新编码但不写库）；片段向量从库里流式读，不用内存矩阵，能和服务同时跑。只读。"""
    meeting = connection.execute(
        "SELECT id, title, project_id, current_transcript_version_id AS version_id FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if meeting is None:
        raise LookupError(meeting_id)
    model = model_of(settings)
    result: dict[str, Any] = {
        "meeting_id": meeting_id,
        "title": meeting["title"],
        "windows": [],
        "copies": [],
    }
    scan = connection.execute(
        "SELECT * FROM meeting_related_scan WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    result["scan"] = dict(scan) if scan is not None else None
    if not meeting["project_id"]:
        result["note"] = "no_project"
        return result
    scope = load_scope(connection, str(meeting["project_id"]))
    if scope is None:
        result["note"] = "no_project"
        return result
    windows = cut_windows(_segments(connection, meeting["version_id"]))
    if at:
        moment = _clock_ms(at)
        windows = [window for window in windows if window.start_ms <= moment < window.end_ms]
    stored = {
        int(row["start_ms"]): (row["vector"], float(row["bar"]))
        for row in connection.execute(
            "SELECT start_ms, vector, bar FROM meeting_windows WHERE meeting_id = ? AND model = ?",
            (meeting_id, model),
        ).fetchall()
    }
    if encode is not None and windows:
        vectors = np.asarray(encode([window.text for window in windows]), dtype=np.float32)
    else:
        windows = [window for window in windows if window.start_ms in stored]
        vectors = (
            np.vstack([_from_f16(stored[window.start_ms][0]) for window in windows])
            if windows
            else None
        )
    if not windows or vectors is None:
        return result
    best_ids = np.full((len(windows), 0), -1, dtype=np.int64)
    best_scores = np.full((len(windows), 0), -np.inf, dtype=np.float32)
    everything: list[tuple[int, str, int]] = []
    keys = sorted(scope.files)
    for part in _batches(keys):
        cursor = connection.execute(
            f"""SELECT v.chunk_id, v.vector, c.content_key, c.ordinal FROM material_chunk_vectors v
                  JOIN material_chunks c ON c.id = v.chunk_id
                 WHERE v.model = ? AND c.content_key IN ({_marks(part)})""",
            [model, *part],
        )
        while True:
            rows = cursor.fetchmany(EXTRA_BLOCK)
            if not rows:
                break
            width = vectors.shape[1] * 2
            rows = [row for row in rows if len(row[1]) == width]
            if not rows:
                continue
            everything.extend((int(row[0]), str(row[2]), int(row[3])) for row in rows)
            ids = np.asarray([int(row[0]) for row in rows], dtype=np.int64)
            matrix = np.frombuffer(b"".join(row[1] for row in rows), dtype=np.float16).reshape(
                len(rows), -1
            )
            best_ids, best_scores = _merge_top(
                best_ids, best_scores, ids, vectors @ matrix.astype(np.float32).T
            )
    floor_value = float(
        floor if floor is not None else _setting(settings, "related_floor", DEFAULT_FLOOR)
    )
    margin_value = float(
        margin if margin is not None else _setting(settings, "related_margin", DEFAULT_MARGIN)
    )
    picked = pick_sample(everything)
    _ids, sample = _read_vectors(connection, picked, model) if picked else (None, None)
    bars = window_bars(
        vectors,
        sample if sample is not None and sample.size else None,
        len({item[1] for item in everything}),
        floor_value,
        margin_value,
    )
    chunk_rows = RelatedPass._chunks_by_id(
        connection, [int(value) for value in best_ids.ravel() if value >= 0]
    )
    checks = WordChecks()
    checks.load_cues(connection)
    words = Words(connection, scope, checks)
    copies = set(_json_list(scan["copies_json"])) if scan is not None else set()
    passages = connection.execute(
        "SELECT DISTINCT content_key FROM meeting_window_passages WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()
    hubs = hub_keys(connection, scope.project_id, [row[0] for row in passages])
    rejected = rejected_filter(connection, meeting_id, scope.project_id)
    result["copies"] = sorted(copies)
    result["hubs"] = sorted(hubs)
    for index, window in enumerate(windows):
        candidates = [
            (int(chunk_id), float(score))
            for chunk_id, score in zip(best_ids[index], best_scores[index], strict=True)
            if np.isfinite(score)
        ]
        _kept, verdicts = judge(window, candidates, chunk_rows, float(bars[index]), scope, words)
        for verdict in verdicts:
            key = verdict.get("content_key")
            if verdict["reason"] is None and key is not None:
                if key in copies:
                    verdict["reason"] = REASON_COPY
                elif key in hubs:
                    verdict["reason"] = REASON_HUB
                elif is_blocked(key, scope.files.get(key), rejected):
                    verdict["reason"] = REASON_REJECTED
        result["windows"].append(
            {
                "start_ms": window.start_ms,
                "text": window.text[:40],
                "bar": round(float(bars[index]), 4),
                "candidates": [
                    {**verdict, "score": round(verdict["score"], 4)} for verdict in verdicts
                ],
            }
        )
    return result


def project_stats(connection: Any, settings: Any, project_id: str) -> dict[str, Any]:
    """links related --project --stats：窗数、每窗片段数、线数、到处相关的文件、副本、bar 的分布。"""
    model = model_of(settings)
    bars = sorted(
        float(row[0])
        for row in connection.execute(
            """SELECT w.bar FROM meeting_windows w JOIN meetings m ON m.id = w.meeting_id
                WHERE m.project_id = ? AND w.model = ?""",
            (project_id, model),
        ).fetchall()
    )

    def percentile(share: float) -> float | None:
        if not bars:
            return None
        return round(bars[min(len(bars) - 1, int(share * (len(bars) - 1)))], 4)

    windows = len(bars)
    passages = connection.execute(
        """SELECT COUNT(*) FROM meeting_window_passages p JOIN meetings m ON m.id = p.meeting_id
            WHERE m.project_id = ?""",
        (project_id,),
    ).fetchone()[0]
    links = connection.execute(
        "SELECT status, COUNT(*) AS n FROM relations WHERE kind = 'related' AND project_id = ? GROUP BY status",
        (project_id,),
    ).fetchall()
    keys = [
        row[0]
        for row in connection.execute(
            """SELECT DISTINCT p.content_key FROM meeting_window_passages p JOIN meetings m ON m.id = p.meeting_id
                WHERE m.project_id = ?""",
            (project_id,),
        ).fetchall()
    ]
    scope = load_scope(connection, project_id)
    hubs = hub_keys(connection, project_id, keys)
    copies: set[str] = set()
    partial = 0
    for row in connection.execute(
        """SELECT s.copies_json, s.partial FROM meeting_related_scan s JOIN meetings m ON m.id = s.meeting_id
            WHERE m.project_id = ?""",
        (project_id,),
    ).fetchall():
        copies.update(_json_list(row["copies_json"]))
        partial += int(row["partial"] or 0)

    def name(key: str) -> str:
        file = scope.files.get(key) if scope else None
        return str(file["name"]) if file else key

    return {
        "windows": windows,
        "passages_per_window": round(passages / windows, 2) if windows else 0,
        "links": {str(row["status"]): int(row["n"]) for row in links},
        "hubs": sorted(name(key) for key in hubs),
        "copies": sorted(name(key) for key in copies),
        "partial_meetings": partial,
        "bar": {"p10": percentile(0.1), "p50": percentile(0.5), "p90": percentile(0.9)},
    }
