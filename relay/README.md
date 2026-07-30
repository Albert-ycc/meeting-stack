# relay — 录音发现、转写调度与派单

relay 是整条链路的自动化中枢：发现新录音 → 调用本地转写 → 按时长分流 → 把结果交给 AI Agent →
归档到资料库。工作台（`../workbench`）负责之后的检索、播放与编辑。

## 链路

```
录音来源
  ├─ voicememos_bridge.py   （可选，仅 macOS + iPhone）轮询 Voice Memos 容器，
  │                          新录音统一改名 vm-<时间戳>-<UUID>.m4a 移到监听目录
  └─ 任何方式放入监听目录的音频（手动拷贝、录音笔导入、其他脚本）
        ↓
relay_watchdog.py   监听目录，用 ffprobe 探测时长后分流：
    < 10 分钟   即时指令：转写文本直接派给 AI Agent 执行
    ≥ 10 分钟   会议纪要：转写后派 Agent 生成纪要，归档为 <YYMMDD 主题>/
        ↓
../transcribe/transcribe.sh   本地转写（FunASR 主稿 + Whisper 对照稿）
        ↓
归档根 <YYMMDD 主题>/    音频、逐字稿、字幕、说话人分组、纪要
        ↓
可选通知（未配置则静默跳过）
```

监听目录默认是 `~/Downloads`，用 `MEETING_RELAY_WATCH_DIR` 覆盖。**不需要 iPhone**——
Voice Memos 桥接只是众多入口之一，任何来源的音频文件落进监听目录都会被处理。

## 配置

全部通过环境变量，无配置文件：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEETING_RELAY_ARCHIVE_ROOT` | `~/MeetingArchive` | 正式归档根 |
| `MEETING_RELAY_PRODUCTS_ROOT` | `~/Movies/meeting-relay-products` | 转写产物工作目录 |
| `MEETING_RELAY_JOBS_DB` | `~/.meeting-relay/workbench-jobs.sqlite3` | 任务队列库 |
| `MEETING_RELAY_AGENT` | `claude` | 派单目标，`claude` 或 `codex` |
| `MEETING_RELAY_CLAUDE_BIN` | 自动探测 | Claude Code 可执行文件路径 |
| `MEETING_RELAY_CODEX_BIN` | 自动探测 | Codex 可执行文件路径 |
| `MEETING_RELAY_WHISPER_BIN` | 自动探测 | whisper 可执行文件路径 |
| `RELAY_LARK_USER_ID` | 空 | 飞书通知 open_id，留空则不通知 |

状态文件都在 `~/.meeting-relay/`：

- `processed.txt` 已处理录音清单（防重复处理）
- `last_meeting.json` 最近一次派单记录（同场会连续分段合并检测用）
- `prompt-default.txt` 转写词典模板（作为 ASR 的 initial_prompt，可持续迭代）
- `hotword-prompts/<job_id>.txt` 单场任务的热词快照（最多 20 词）
- `prompts/` 派给 Agent 的任务 prompt 存档

## 热词

热词显著影响专有名词的识别准确率，但**必须按场次显式传入**，不要把跨项目的大词典当默认值——
无关热词会诱导 ASR 把发音相近的普通词错认成词典里的专有名词，反而降低准确率。

```bash
quickstart/relayctl enqueue "/绝对路径/会议.m4a" \
  --hotwords "/绝对路径/本场热词.txt" --json
```

词表会去重后快照进任务，之后源文件再变不影响已入队的任务。

## 纪要内容协议 v3

长会议直接丢给 LLM 会丢失中段内容。v3 用确定性切窗解决：

1. 从真实 SRT 生成 `minutes-plan.json`，按连续 8–12 分钟窗口覆盖全部字幕条目
2. 超过 45 分钟的会议额外生成逐窗口 `minutes-ledger/`，再多阶段合并
3. 最终 Markdown 同时包含固定短的「一分钟摘要」和随议题增长的「完整会议记录」
4. 生成 `minutes-evidence.json` 记录证据链

`complete-minutes` 与 `mark-published` 会校验输入逐字稿、窗口计划、账本、覆盖计数及正文时间锚
的哈希，**不匹配就拒绝导入**，防止生成期间的人工修改被旧结果覆盖。旧协议的任务继续可读，
不会因升级中断。

## 归档策略

完成回执校验通过后，产物从隐藏的 `<归档根>/.workbench-drafts/<job_id>/attempt-<n>`
提升为归档根下的一级目录 `<YYMMDD 主题>/`。文件系统不区分待校对与已校对，状态只在
工作台数据库里维护。

人工发布会在同一目录内原子更新规范产物，**原音频永不改名、永不改写**。

历史草稿可用 `quickstart/relayctl migrate-pending [job_id ...] --json` 幂等迁移。

测试夹具不得进入真实资料库。入队后用
`quickstart/relayctl archive-policy <job_id> --policy hidden_fixture --json`
显式标记，它才会保留在隐藏目录；真实录音必须保持默认 `visible`。

## 运行

```bash
# 监听守护（前台）
python3 quickstart/relay_watchdog.py

# 可选：Voice Memos 桥接（macOS + iPhone）
python3 quickstart/voicememos_bridge.py

# 手动入队
quickstart/relayctl enqueue "/绝对路径/会议.m4a" --json

# 查看健康状态（退出码 0/1/2 = 健康/降级/不可用）
quickstart/relayctl health --json
```

macOS 上以守护方式常驻时的 TCC 权限问题见 [../docs/install.md](../docs/install.md)。
