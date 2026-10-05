# meeting-stack 开发约定

给在这个仓库里写代码的人和 AI 看。功能说明在 `README.md` 和 `workbench/README.md`，这里只写提交前必须做的事。

## 提交前

改了 `workbench/backend`：

```bash
cd workbench
.venv/bin/ruff format backend        # 必跑：install-local.sh 会用 ruff format --check 卡住没格式化的代码
.venv/bin/ruff check backend
.venv/bin/pytest backend/tests -o tmp_path_retention_policy=failed   # 不加的话每轮把全部用例的临时目录留下（约 1.7GB），盘小的机器会被写满
```

改了 `workbench/frontend`：

```bash
cd workbench/frontend
npx vitest run
npm run typecheck
```

改了路由、浏览器历史、盖在页面上的二级页、滚动位置这一类：vitest（jsdom）测不出来，提交前要在真浏览器里点一遍。
`workbench/scripts/e2e/run.py` 一条命令建隔离环境、造数、跑全部实点用例（先 `npm run build`，用法见 `workbench/README.md`），不会碰 8765。
2026-10-01 需求池收尾时就有两处用例全绿、真浏览器里却是错的：React 换了一层结构，把会议页卸掉重建了；浏览器后退时按历史记录恢复滚动，
盖掉了代码放回去的位置。另外，Playwright 自带的 Chromium 解不了 m4a 里的 AAC，测播放只能看 `paused`，看不到时间往前走。

改了 `relay/`（`glossary/`、`transcribe/`、`task-notify/`、`workbench/card_listener.py` 也一样，ruff 规则和后端同一套）：

```bash
cd workbench
.venv/bin/ruff format card_listener.py ../relay ../glossary ../transcribe ../task-notify   # install-local.sh 同样会卡格式
.venv/bin/ruff check card_listener.py ../relay ../glossary ../transcribe ../task-notify
cd ../relay
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests   # 要用装了 watchdog 的 Python，见 relay/README.md「Python 依赖」
```

`card_listener.py` 的用例在后端用例里（`backend/tests/test_card_listener.py`）。

## 用例不能依赖跑它的机器

声档实际跑在用户的 Mac 上（macOS、太平洋时区、装着 Homebrew 的 tesseract），开发常在 Linux、UTC 的环境里。
260928 升级时有十来个用例只在开发环境能过，都是下面这几种：

- **时区**：用例里的时间按 `+08:00` 写、按日期断言的，要钉时区（后端复用 `tests/test_timeline.py` 的 `shanghai`
  fixture，前端 `vite.config.ts` 已钉 `TZ=Asia/Shanghai`）。产品代码里的「哪一天」按北京日历（`task_due.BEIJING_TZ`、
  `beijing_today()`；用户 2026-10-03 定的全站口径，会都在北京白天开），不要用 UTC，也不要按本机时区。这类逻辑的用例
  要在非上海时区（比如 `America/Los_Angeles`）下再跑一遍：进程时区钉成上海时，偷用本机时区的代码照样是绿的。
- **真实当前时间**：别让用例的结果取决于今天几点（比如假时钟加 31 天，却拿真实时间记下的时间戳去比）。
- **平台**：`sys.platform` 相关的分支要在用例里钉住，不然在 Mac 上会真去开访达。
- **本机装的程序**：找程序的逻辑会去看 Homebrew 目录，用例要把这条路也挡掉。

## 用例里的库少写盘

全量一轮曾经逻辑写盘 20GiB（每个新库建表要提交三百多次，用例里 db.execute 每条语句开关一次连接、每次都把 -wal 整个写回主文件）。
`backend/tests/conftest.py` 里自动生效的 `_cheap_databases` 让用例的库从模板克隆、页大小 1024、每条连接 temp_store=MEMORY、
initialize() 之后常驻一个空闲连接；语句的结果和库里的内容不变（`test_cheap_databases.py` 拿克隆出来的库和真从零建的逐行对比）。

- 用例要看库文件本身、-wal / -shm 的生命周期（最后一个连接关掉时做检查点、删边车文件）、备份读到的 WAL 状态，模块或用例上标
  `pytest.mark.real_database_files`，它们不给常驻连接。新写这类用例别忘了标，不然常驻的连接会让它看到的和生产不一样。
- `initialize()` 里新加带时间或随机 id 的种子行，要在 `_schema_template` 里和 app_state 一样处理（清掉，让克隆之后那一遍真 initialize()
  重写），否则所有克隆共用模板建成那一刻的值；`test_cheap_databases.py` 会拦住。
- 量写盘：`~/bin/pytest-writes.py --python <venv python> --profile out.jsonl -- backend/tests -o tmp_path_retention_policy=failed`，
  再用 `~/bin/pytest_writes_report.py out.jsonl` 按文件、按用例看（读的是 proc_pid_rusage 的 ri_logical_writes）。

## 其他

- 全量格式化的提交记在 `.git-blame-ignore-revs`；本地跑一次
  `git config blame.ignoreRevsFile .git-blame-ignore-revs`，`git blame` 就会跳过它。
