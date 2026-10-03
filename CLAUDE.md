# meeting-stack 开发约定

给在这个仓库里写代码的人和 AI 看。功能说明在 `README.md` 和 `workbench/README.md`，这里只写提交前必须做的事。

## 提交前

改了 `workbench/backend`：

```bash
cd workbench
.venv/bin/ruff format backend        # 必跑：install-local.sh 会用 ruff format --check 卡住没格式化的代码
.venv/bin/ruff check backend
.venv/bin/pytest backend/tests
```

改了 `workbench/frontend`：

```bash
cd workbench/frontend
npx vitest run
npm run typecheck
```

改了路由、浏览器历史、盖在页面上的二级页、滚动位置这一类：vitest（jsdom）测不出来，提交前要在真浏览器里点一遍。
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
  fixture，前端 `vite.config.ts` 已钉 `TZ=Asia/Shanghai`）。产品代码里「今天」「昨天」按本地日算，不要用 UTC 日期。
- **真实当前时间**：别让用例的结果取决于今天几点（比如假时钟加 31 天，却拿真实时间记下的时间戳去比）。
- **平台**：`sys.platform` 相关的分支要在用例里钉住，不然在 Mac 上会真去开访达。
- **本机装的程序**：找程序的逻辑会去看 Homebrew 目录，用例要把这条路也挡掉。

## 其他

- 全量格式化的提交记在 `.git-blame-ignore-revs`；本地跑一次
  `git config blame.ignoreRevsFile .git-blame-ignore-revs`，`git blame` 就会跳过它。
