"""第四期 4e：产出建议（links_loop 的 L4，produced.watch）。

任务确认以后，项目文件夹里新出现的、看起来是这条任务做出来的文件，问「是这条任务的交付物吗？」。
只用 SQL 和字符串：不调 AI、不用向量、不读盘，文件名和材料文字都不离开这台 Mac。

- 看哪些任务：confirmed、in_progress，有项目，项目至少一个根目录收完过一轮文件名。done 的任务不再问新
  文件，已经在问的留到 30 天过期。
- 窗口：最近一次「进入已确认」（confirmed 事件；从 pending_confirm、expired、cancelled 改过来的
  status_changed；手动建的任务的 created）、最近一次 requirement_changed、app_state 的 links_since，
  三者取最晚，长 14 天。一条 SQL 取出全部候选任务和三个时间。
- 哪些新文件算（第 1 节问题 2 的默认）：范围 A 是任务的需求文件夹（前缀下的新增都算）；范围 B 是项目
  的所有根目录，文件名或所在文件夹名和任务标题或需求标题有共同词才算。改过的文件、成批出现的文件
  两个范围都要共同词。moved、copied、back、gone 不算；pending 等 classify 分出来。
- 跳过：已经是项目里任何任务的交付物；项目里这份文件已经有过产出行（在问的、你说不是的、收回的，
  一份文件只问一次）。上限只数在问的：每条任务 2 个、每个需求 5 个、每个项目 8 个，不挤掉已在问的。
- 收回（每轮先做）：任务取消、过期、退回待确认或离开项目；文件从别的路成了交付物；30 天没回答。
  文件断了线由 L2 收回。
- 全部经 relations.upsert_system 写，只写新行，已有的行不改，闲着一轮不写。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import file_events
from .cards import APP_FILE_NAMES
from .decisions import project_names
from .file_stems import COMMON_TWO_CHAR, STOPWORDS
from .material_graph import rel_under
from .material_rules import silent_skip
from .project_profile import light_key
from .relations import canonical, upsert_system
from .search import fold
from .task_due import BEIJING_TZ, meeting_date

WINDOW = timedelta(days=14)
EXPIRE = timedelta(days=30)
# 窗口关上以后再多看一会儿：还在等内容标识的新增最多等 60 分钟才分出来
LATE_EVENTS = timedelta(hours=2)
ROUND_TASKS = 200
ROUND_SECONDS = 1.0
CAP_TASK = 2
CAP_REQUIREMENT = 5
CAP_PROJECT = 8
# 成批出现：同一个 (root_id, dir_rel) 10 分钟内超过 20 条 added
BURST_WINDOW = timedelta(minutes=10)
BURST_COUNT = 20
PACKAGE_EXTS = frozenset({"key", "pages", "numbers"})
SKIP_EXTS = frozenset({"tmp", "part", "crdownload", "download", "partial", "swp", "lock"})
# 只由这些词拼成的汉字词不算共同词
GENERIC_WORDS = frozenset(
    {*COMMON_TWO_CHAR, "文件", "资料", "最终", "修改", "版本", "副本", "新建"}
)
# 从这些状态改到 confirmed 才算重新进入已确认（从 in_progress 退回不算）
_REENTER_BODIES = ("pending_confirm → confirmed", "expired → confirmed", "cancelled → confirmed")

SCOPE_FOLDER = "folder"
SCOPE_WORD = "word"
REF_MEETING = "meeting"
REF_CONFIRM = "confirm"


# ---------------------------------------------------------------------- 共同词


def _han(char: str) -> bool:
    return "一" <= char <= "鿿" or "㐀" <= char <= "䶿"


def _alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _runs(text: str) -> list[str]:
    """折叠后按汉字、字母数字切成段（标点、空白、下划线都是分隔）。"""
    runs: list[str] = []
    current: list[str] = []
    for char in fold(text or ""):
        if _han(char) or _alnum(char):
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _long_enough(word: str) -> bool:
    """至少 3 个汉字，或至少 4 个字母数字，或汉字和字母数字混排至少 3 个字。"""
    hans = sum(1 for char in word if _han(char))
    if hans == len(word):
        return hans >= 3
    if hans == 0:
        return len(word) >= 4
    return len(word) >= 3


def _only_generic(word: str) -> bool:
    """纯汉字、只由常见两字词和「文件」「资料」这类词拼成。"""
    if not word or not all(_han(char) for char in word):
        return False
    reachable = [True] + [False] * len(word)
    for end in range(1, len(word) + 1):
        for size in (2, 3, 4):
            start = end - size
            if start >= 0 and reachable[start] and word[start:end] in GENERIC_WORDS:
                reachable[end] = True
                break
    return reachable[-1]


def _excluded(word: str, excluded: Sequence[str]) -> bool:
    if light_key(word) in STOPWORDS or _only_generic(word):
        return True
    return any(word in fold(name) for name in excluded if name)


def _maximal(run: str, task_runs: Sequence[str]) -> list[str]:
    """文件一段里和任务一边的公共子串，只取往左往右都不能再长的（最长公共子串的候选）。"""
    found: list[str] = []
    for start in range(len(run)):
        end = start
        while end < len(run) and any(run[start : end + 1] in part for part in task_runs):
            end += 1
        if end - start < 3:
            continue
        # 往左还能长的不是它自己，是更长的那一个的尾巴
        if start > 0 and any(run[start - 1 : end] in part for part in task_runs):
            continue
        found.append(run[start:end])
    return found


def _grams(runs: Sequence[str]) -> frozenset[str]:
    """各段里的 3 字片段。共同词至少 3 个字，两边没有共同的 3 字片段就不会有共同词。"""
    return frozenset(run[index : index + 3] for run in runs for index in range(len(run) - 2))


def _shared(
    task_runs: Sequence[str], file_runs: Sequence[str], excluded: Sequence[str]
) -> str | None:
    best: str | None = None
    for run in file_runs:
        for word in _maximal(run, task_runs):
            if best is not None and len(word) <= len(best):
                continue
            if _long_enough(word) and not _excluded(word, excluded):
                best = word
    return best


def shared_word(task_text: str, file_text: str, excluded: Sequence[str] = ()) -> str | None:
    """两边按 search.fold 折叠后的最长公共子串，够长、又不是泛词、项目名、也叫和根目录名时返回它。
    task_text 是任务标题加需求标题；file_text 是去掉扩展名的文件名，或 dir_rel 的一段。公共子串只取
    不能再长的那几个：泛词拼成的词被排除以后，不退回去拿它的一截。"""
    return _Words(excluded).shared(task_text, file_text)


def _stem(name: str) -> str:
    stem, dot, ext = str(name).rpartition(".")
    return (
        stem
        if dot and stem and ext and len(ext) <= 8 and ext.isascii() and ext.isalnum()
        else str(name)
    )


class _Words:
    """一个项目一轮里的共同词：每段文字（任务一边、文件名、文件夹名）只切一次段、算一次 3 字片段，先按
    3 字片段粗筛；file_word 按 (任务文字, 文件名, dir_rel, below) 记下结果。只在内存里，这一轮用完就丢。"""

    def __init__(self, excluded: Sequence[str]) -> None:
        self.excluded = excluded
        self._sides: dict[str, tuple[list[str], frozenset[str]]] = {}
        self._found: dict[tuple[str, str, str, str], tuple[str, str | None] | None] = {}
        self._grams_of: dict[tuple[str, str], frozenset[str]] = {}
        self._last_file: Mapping[str, Any] | None = None
        self._last_grams: frozenset[str] | None = None

    def _side(self, text: str) -> tuple[list[str], frozenset[str]]:
        side = self._sides.get(text)
        if side is None:
            runs = _runs(text)
            side = self._sides[text] = (runs, _grams(runs))
        return side

    def shared(self, task_text: str, file_text: str) -> str | None:
        task_runs, task_grams = self._side(task_text)
        file_runs, file_grams = self._side(file_text)
        if not task_runs or task_grams.isdisjoint(file_grams):
            return None
        return _shared(task_runs, file_runs, self.excluded)

    def task_grams(self, task_text: str) -> frozenset[str]:
        return self._side(task_text)[1]

    def file_grams(self, file: Mapping[str, Any]) -> frozenset[str]:
        """文件名和各层文件夹名合起来的 3 字片段（按文件名和 dir_rel 记）。一条流水要和每条任务比，
        同一份文件连着问好几次，先看是不是上一次那份。"""
        if file is self._last_file and self._last_grams is not None:
            return self._last_grams
        key = (str(file["name"]), str(file.get("dir_rel") or ""))
        grams = self._grams_of.get(key)
        if grams is None:
            texts = [_stem(key[0]), *(part for part in key[1].split("/") if part)]
            grams = self._grams_of[key] = frozenset().union(
                *(self._side(text)[1] for text in texts)
            )
        self._last_file, self._last_grams = file, grams
        return grams

    def file_word(
        self, task_text: str, file: Mapping[str, Any], below: str = ""
    ) -> tuple[str, str | None] | None:
        if self._side(task_text)[1].isdisjoint(self.file_grams(file)):
            return None
        key = (task_text, str(file["name"]), str(file.get("dir_rel") or ""), below)
        if key in self._found:
            return self._found[key]
        best: tuple[str, str | None] | None = None
        word = self.shared(task_text, _stem(file["name"]))
        if word:
            best = (word, None)
        segments = [part for part in key[2].split("/") if part]
        if below:
            segments = segments[len([part for part in below.split("/") if part]) :]
        for segment in reversed(segments):
            word = self.shared(task_text, segment)
            if word and (best is None or len(word) > len(best[0])):
                best = (word, f"{segment}/")
        self._found[key] = best
        return best


def file_word(
    task_text: str, file: Mapping[str, Any], excluded: Sequence[str], below: str = ""
) -> tuple[str, str | None] | None:
    """文件名或它所在的文件夹名和任务的共同词：(词, 写有这个词的那一层文件夹「x/」或 None)。一样长时
    文件名里的优先，文件夹里的靠里的优先。below 是范围 A 的需求文件夹前缀：只看它下面的几层（需求文件夹
    自己的名字常常就是需求标题，不能拿它当共同词）。"""
    return _Words(excluded).file_word(task_text, file, below)


# ---------------------------------------------------------------------- 候选任务


_TASKS_SQL = f"""
SELECT t.id, t.title, t.status, t.origin, t.project_id, t.requirement_id, t.meeting_id,
       rq.title AS requirement_title, m.recording_date, m.created_at AS meeting_created_at,
       (SELECT MAX(e.created_at) FROM task_events e
         WHERE e.task_id = t.id
           AND (e.kind = 'confirmed'
                OR (e.kind = 'status_changed' AND e.body IN ({", ".join(f"'{body}'" for body in _REENTER_BODIES)}))
                OR (e.kind = 'created' AND t.origin = 'manual'))) AS confirmed_at,
       (SELECT MAX(e.created_at) FROM task_events e
         WHERE e.task_id = t.id AND e.kind = 'requirement_changed') AS requirement_at,
       (SELECT value FROM app_state WHERE key = 'links_since') AS links_since
  FROM tasks t
  LEFT JOIN requirements rq ON rq.id = t.requirement_id
  LEFT JOIN meetings m ON m.id = t.meeting_id
 WHERE t.status IN ('confirmed', 'in_progress') AND t.project_id IS NOT NULL AND t.project_id > ?
   AND EXISTS (SELECT 1 FROM project_material_roots pr
                 JOIN material_index_state s ON s.root_id = pr.id
                WHERE pr.project_id = t.project_id AND s.last_full_at IS NOT NULL)
 ORDER BY t.project_id, t.id"""


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def window_start(row: Mapping[str, Any]) -> datetime | None:
    """窗口起点：进入已确认、挂上需求、links_since 三者里最晚的（没有的不算）。"""
    moments = [
        moment
        for moment in (
            _parse(row.get("confirmed_at")),
            _parse(row.get("requirement_at")) if row.get("requirement_id") else None,
            _parse(row.get("links_since")),
        )
        if moment is not None
    ]
    return max(moments) if moments else None


@dataclass
class _Task:
    row: dict[str, Any]
    start: datetime
    end: datetime
    text: str
    folders: list[tuple[int, str, str]] = field(default_factory=list)  # (root_id, 前缀, 「x/」)
    grams: frozenset[str] = frozenset()  # 任务一边的 3 字片段（粗筛共同词）

    @property
    def id(self) -> str:
        return str(self.row["id"])


@dataclass
class _Pick:
    task: _Task
    event: dict[str, Any]
    file: dict[str, Any]
    scope: str
    folder: str | None
    word: str | None
    target: tuple[Any, ...]

    def strength(self) -> tuple:
        """匹配强弱：范围 A 优先，再看共同词长短。"""
        return (self.scope == SCOPE_FOLDER, len(self.word or ""))

    def order(self) -> tuple:
        """每条任务里的顺序：A 先于 B；共同词长的先；added 先于 changed；新的先。"""
        return (
            *self.strength(),
            self.event["kind"] == file_events.ADDED,
            self.event["at"],
            self.event["id"],
        )


# ---------------------------------------------------------------------- 每轮


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def retract(connection: Any, now: datetime, since: str) -> int:
    """每轮先做：在问的产出，任务取消、过期、退回待确认或离开项目，文件从别的路成了任何任务的交付物，
    或建了 30 天没回答的，改 cleared（收回的不再问）。只动 updated_at 不晚于 since 的系统行。"""
    return connection.execute(
        """UPDATE relations SET status = 'cleared', updated_at = :now
            WHERE kind = 'produced' AND status = 'suggested' AND origin != 'manual' AND updated_at <= :since
              AND (NOT EXISTS (SELECT 1 FROM tasks t
                                WHERE t.id = relations.task_id AND t.project_id = relations.project_id
                                  AND t.status IN ('confirmed', 'in_progress', 'done'))
                   OR created_at < :expire
                   OR EXISTS (SELECT 1 FROM deliverable_files df
                               WHERE (relations.content_key IS NOT NULL AND df.content_key = relations.content_key)
                                  OR (df.root_id = relations.root_id AND df.rel_path = relations.rel_path))
                   OR EXISTS (SELECT 1 FROM deliverables d
                                JOIN project_material_roots pr ON pr.id = relations.root_id
                               WHERE d.kind = 'file' AND d.url = rtrim(pr.path, '/') || '/' || relations.rel_path))""",
        {"now": _stamp(now), "since": since, "expire": _stamp(now - EXPIRE)},
    ).rowcount


def candidates(connection: Any, after: str = "") -> list[dict[str, Any]]:
    """一条 SQL：全部候选任务和三个时间（按项目、任务 id 排，after 之后的项目）。"""
    return [dict(row) for row in connection.execute(_TASKS_SQL, (after,)).fetchall()]


def watch(
    db: Any,
    now: datetime,
    budget: float = ROUND_SECONDS,
    *,
    since: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_tasks: int | None = None,
    after: str = "",
) -> dict[str, Any]:
    """L4 一轮：先收回，再按项目看候选任务（after 之后的项目），每轮最多 max_tasks 条任务或 budget 秒。
    返回 {tried, pending, written, cleared, after}：after 是下一轮从哪个项目之后接着看（到底了回 ""）。

    一个项目的任务要一起配（一份文件只给匹配最强的那条任务，上限按项目数），不能拆到两轮。所以：项目
    之间看条数和时间；项目里面每条流水之前也看时间，超了就放下这个项目（什么都不写），游标停在它前面，
    下一轮从它开始，不丢不重。每轮第一个项目不看时间，一定做完，不然一个配不完 1 秒的项目每轮都从头
    再来；共同词按段缓存、先用 3 字片段粗筛以后，一个项目 10 条任务 6000 条流水不到 0.2 秒。"""
    started = clock()
    stamp = since or _stamp(now)
    limit = ROUND_TASKS if max_tasks is None else max_tasks
    with db.transaction() as connection:
        cleared = retract(connection, now, stamp)
    with db.autocommit() as connection:
        rows = candidates(connection, after)
    by_project: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_project.setdefault(str(row["project_id"]), []).append(row)
    tried = 0
    written = 0
    next_after = ""

    def out_of_time() -> bool:
        return clock() - started >= budget

    done = 0
    for project_id, tasks in by_project.items():
        first = done == 0
        if not first and (tried + len(tasks) > limit or out_of_time()):
            next_after = _previous(by_project, project_id)
            break
        with db.transaction() as connection:
            count = _watch_project(
                connection, project_id, tasks, now, stamp, None if first else out_of_time
            )
        if count is None:
            next_after = _previous(by_project, project_id)
            break
        done += 1
        written += count
        tried += len(tasks)
    return {
        "tried": tried,
        "pending": len(rows),
        "written": written,
        "cleared": cleared,
        "after": next_after,
    }


def _previous(by_project: Mapping[str, Any], project_id: str) -> str:
    keys = list(by_project)
    index = keys.index(project_id)
    return keys[index - 1] if index > 0 else ""


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _meeting_day(row: Mapping[str, Any]) -> date | None:
    """开会的日子，北京日历（和文件流水的 day 同一套：「会后 N 天」的两边必须同一套日历）。"""
    if not row.get("meeting_id"):
        return None
    return meeting_date(
        {"recording_date": row.get("recording_date"), "created_at": row.get("meeting_created_at")}
    )


def _eligible(file: Mapping[str, Any]) -> bool:
    """活文件；normal 区，或 key、pages、numbers 包；不是系统影子文件和锁文件；不是下载一半的临时文件；
    不是声档自己写的文件（cards.APP_FILE_NAMES）；大小不为 0（包的 size 是空的）。"""
    if file.get("gone_at") is not None:
        return False
    ext = str(file.get("ext") or "").lower()
    zone = file.get("zone")
    if not (zone == "normal" or (zone == "package" and ext in PACKAGE_EXTS)):
        return False
    if silent_skip(str(file.get("name") or "")) or ext in SKIP_EXTS:
        return False
    if str(file.get("name") or "") in APP_FILE_NAMES:
        return False  # 声档自己写的（00 索引.md 在卡片区，按区已经挡住；按名字再挡一道）
    return bool(file.get("size")) or zone == "package"


def _identity(file: Mapping[str, Any]) -> tuple[str, str | None]:
    """(ident 里文件那一半, content_key)：内容标识新鲜时用它；算不出标识的（包、fig、压缩包，读不了的），
    和等过 60 分钟还没算出来的（classify 已经按文件名加大小分过），用 p:<root_id>:<rel_path>。"""
    fresh = (
        file.get("content_key")
        and file.get("content_size") == file.get("size")
        and file.get("content_mtime_ns") == file.get("mtime_ns")
    )
    if fresh:
        return str(file["content_key"]), str(file["content_key"])
    return f"p:{file['root_id']}:{file['rel_path']}", None


def _bursts(events: Iterable[Mapping[str, Any]]) -> set[int]:
    """成批出现的 added：同一个 (root_id, dir_rel) 里 10 分钟内超过 20 条。"""
    groups: dict[tuple[int, str], list[tuple[datetime, int]]] = {}
    for event in events:
        moment = _parse(event["at"])
        if moment is not None:
            groups.setdefault((int(event["root_id"]), str(event["dir_rel"])), []).append(
                (moment, int(event["id"]))
            )
    burst: set[int] = set()
    for items in groups.values():
        items.sort()
        left = 0
        for right in range(len(items)):
            while items[right][0] - items[left][0] > BURST_WINDOW:
                left += 1
            if right - left + 1 > BURST_COUNT:
                burst.update(event_id for _moment, event_id in items[left : right + 1])
    return burst


def _match(
    task: _Task, event: Mapping[str, Any], file: Mapping[str, Any], words: _Words, burst: bool
) -> tuple[str, str | None, str | None] | None:
    """(scope, folder, word) 或 None。added：范围 A 不要共同词（成批出现的要），范围 B 要；changed：都要。"""
    folder_label: str | None = None
    below = ""
    for root_id, prefix, label in task.folders:
        if int(file["root_id"]) == root_id and str(file["rel_path"]).startswith(prefix + "/"):
            folder_label, below = label, prefix
            break
    found = words.file_word(task.text, file, below)
    need_word = event["kind"] == file_events.CHANGED or burst or folder_label is None
    if need_word and found is None:
        return None
    word = found[0] if found else None
    if folder_label is not None:
        return SCOPE_FOLDER, folder_label, word
    return SCOPE_WORD, found[1] if found else None, word


def _evidence(pick: _Pick) -> dict[str, Any]:
    event = pick.event
    event_day = date.fromisoformat(str(event["day"]))
    meeting_day = _meeting_day(pick.task.row)
    if meeting_day is not None:
        ref, since_day = REF_MEETING, meeting_day
    else:
        ref, since_day = REF_CONFIRM, pick.task.start.astimezone(BEIJING_TZ).date()
    return {
        "event_id": int(event["id"]),
        "event_kind": event["kind"],
        "event_day": event_day.isoformat(),
        "scope": pick.scope,
        "folder": pick.folder,
        "words": [pick.word] if pick.word else [],
        "ref": ref,
        "days": max(0, (event_day - since_day).days),
    }


def _watch_project(
    connection: Any,
    project_id: str,
    rows: list[dict[str, Any]],
    now: datetime,
    since: str,
    out_of_time: Callable[[], bool] | None = None,
) -> int | None:
    """一个项目：算窗口、读窗口里的文件流水、配任务、按上限写新行。返回写了几行；配到一半 out_of_time
    为真时回 None，什么都没写（新行都在最后一起写）。"""
    tasks: list[_Task] = []
    for row in rows:
        start = window_start(row)
        if start is None or now - (start + WINDOW) > LATE_EVENTS:
            continue
        text = " ".join(part for part in (row.get("title"), row.get("requirement_title")) if part)
        tasks.append(_Task(row=row, start=start, end=start + WINDOW, text=text))
    if not tasks:
        return 0
    roots = [
        dict(row)
        for row in connection.execute(
            "SELECT id, path FROM project_material_roots WHERE project_id = ?", (project_id,)
        ).fetchall()
    ]
    root_ids = [int(root["id"]) for root in roots]
    if not root_ids:
        return 0
    project = connection.execute(
        "SELECT name, also_names FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    excluded = [
        *(project_names(project["name"], project["also_names"]) if project is not None else []),
        *(str(root["path"]).rstrip("/").rpartition("/")[2] for root in roots),
    ]
    words = _Words(excluded)
    requirement_ids = sorted(
        {str(task.row["requirement_id"]) for task in tasks if task.row.get("requirement_id")}
    )
    folders: dict[str, list[tuple[int, str, str]]] = {}
    if requirement_ids:
        for folder in connection.execute(
            f"SELECT requirement_id, path FROM requirement_folders WHERE requirement_id IN ({_marks(requirement_ids)})",
            requirement_ids,
        ).fetchall():
            for root in roots:
                prefix = rel_under(str(root["path"]), str(folder["path"]))
                # 文件夹就是根目录本身时按范围 B：整个根目录太宽，说不出「新增在『…/』」
                if prefix:
                    label = prefix.rstrip("/").rpartition("/")[2] + "/"
                    folders.setdefault(str(folder["requirement_id"]), []).append(
                        (int(root["id"]), prefix, label)
                    )
    for task in tasks:
        task.folders = folders.get(str(task.row.get("requirement_id") or ""), [])
        task.grams = words.task_grams(task.text)

    first = min(task.start for task in tasks)
    added = file_events.recent_added(connection, root_ids, first, None, now=now)
    # changed 按 (root_id, at) 索引直接读，也过 classify（原样回 changed）
    changed = [
        dict(row)
        for row in connection.execute(
            f"""SELECT * FROM material_file_events
                 WHERE root_id IN ({_marks(root_ids)}) AND at >= ? AND kind = 'changed'""",
            (*root_ids, file_events.at_text(first)),
        ).fetchall()
    ]
    labels = file_events.classify(connection, changed, now=now)
    events = [
        *added,
        *(event for event in changed if labels[int(event["id"])] == file_events.CHANGED),
    ]
    if not events:
        return 0
    raw_added = [
        dict(row)
        for row in connection.execute(
            f"""SELECT id, root_id, dir_rel, at FROM material_file_events
                 WHERE root_id IN ({_marks(root_ids)}) AND at >= ? AND kind = 'added'""",
            (*root_ids, file_events.at_text(first - BURST_WINDOW)),
        ).fetchall()
    ]
    burst = _bursts(raw_added)
    file_ids = sorted({int(event["file_id"]) for event in events})
    files: dict[int, dict[str, Any]] = {}
    for start in range(0, len(file_ids), 400):
        part = file_ids[start : start + 400]
        for row in connection.execute(
            f"SELECT * FROM material_files WHERE id IN ({_marks(part)}) AND gone_at IS NULL", part
        ).fetchall():
            files[int(row["id"])] = dict(row)

    # 一份文件只问一次：项目里已经有产出行的（不管什么状态）和已经是交付物的都跳过
    seen_keys: set[str] = set()
    seen_paths: set[tuple[int, str]] = set()
    open_task: dict[str, int] = {}
    open_requirement: dict[str, int] = {}
    open_project = 0
    for row in connection.execute(
        """SELECT r.task_id, r.status, r.content_key, r.root_id, r.rel_path, t.requirement_id
             FROM relations r LEFT JOIN tasks t ON t.id = r.task_id
            WHERE r.kind = 'produced' AND r.project_id = ?""",
        (project_id,),
    ).fetchall():
        if row["content_key"]:
            seen_keys.add(str(row["content_key"]))
        if row["root_id"] is not None and row["rel_path"]:
            seen_paths.add((int(row["root_id"]), str(row["rel_path"])))
        if row["status"] == "suggested":
            open_project += 1
            open_task[str(row["task_id"])] = open_task.get(str(row["task_id"]), 0) + 1
            if row["requirement_id"]:
                key = str(row["requirement_id"])
                open_requirement[key] = open_requirement.get(key, 0) + 1
    urls: set[str] = set()
    for row in connection.execute(
        """SELECT d.kind, d.url, df.content_key, df.root_id, df.rel_path FROM deliverables d
             JOIN tasks t ON t.id = d.task_id
             LEFT JOIN deliverable_files df ON df.deliverable_id = d.id
            WHERE t.project_id = ?""",
        (project_id,),
    ).fetchall():
        if row["content_key"]:
            seen_keys.add(str(row["content_key"]))
        if row["root_id"] is not None and row["rel_path"]:
            seen_paths.add((int(row["root_id"]), str(row["rel_path"])))
        if row["kind"] == "file":
            urls.add(str(row["url"]))
    root_paths = {int(root["id"]): str(root["path"]).rstrip("/") for root in roots}

    # 每份文件挑最强的那一对（任务，事件）
    best: dict[tuple[Any, ...], _Pick] = {}
    for event in events:
        if out_of_time is not None and out_of_time():
            return None
        file = files.get(int(event["file_id"]))
        if file is None or not _eligible(file):
            continue
        ident_part, content_key = _identity(file)
        path = (int(file["root_id"]), str(file["rel_path"]))
        if (content_key and content_key in seen_keys) or path in seen_paths:
            continue
        if f"{root_paths.get(path[0], '')}/{path[1]}" in urls:
            continue
        moment = _parse(event["at"])
        if moment is None:
            continue
        target = (ident_part,)
        grams = words.file_grams(file)
        for task in tasks:
            if not (task.start <= moment < task.end):
                continue
            # 不在任务的需求文件夹里（要共同词）、又没有共同的 3 字片段：配不上，不用往下算
            if not task.folders and task.grams.isdisjoint(grams):
                continue
            matched = _match(task, event, file, words, int(event["id"]) in burst)
            if matched is None:
                continue
            scope, folder, word = matched
            pick = _Pick(task, event, file, scope, folder, word, target)
            kept = best.get(target)
            if kept is None or (pick.strength(), task.start, pick.order()) > (
                kept.strength(),
                kept.task.start,
                kept.order(),
            ):
                best[target] = pick
    if not best:
        return 0

    fresh: list[dict[str, Any]] = []
    for pick in sorted(best.values(), key=lambda item: item.order(), reverse=True):
        task_id = pick.task.id
        requirement = str(pick.task.row.get("requirement_id") or "")
        if open_project >= CAP_PROJECT:
            break
        if open_task.get(task_id, 0) >= CAP_TASK:
            continue
        if requirement and open_requirement.get(requirement, 0) >= CAP_REQUIREMENT:
            continue
        open_project += 1
        open_task[task_id] = open_task.get(task_id, 0) + 1
        if requirement:
            open_requirement[requirement] = open_requirement.get(requirement, 0) + 1
        identity = _identity(pick.file)
        fresh.append(
            {
                "kind": "produced",
                "project_id": project_id,
                "ident": f"{task_id}|{identity[0]}",
                "status": "suggested",
                "origin": "rule",
                "task_id": task_id,
                "file_id": int(pick.file["id"]),
                "content_key": identity[1],
                "root_id": int(pick.file["root_id"]),
                "rel_path": str(pick.file["rel_path"]),
                "stem_key": pick.file.get("stem_key"),
                "quote": "",
                "evidence_json": canonical(_evidence(pick)),
                "score": (2.0 if pick.scope == SCOPE_FOLDER else 1.0) + len(pick.word or "") / 100,
            }
        )
    return upsert_system(connection, fresh, _stamp(now), since=since) if fresh else 0
