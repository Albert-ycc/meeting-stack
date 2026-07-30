# workbench — Web 工作台

meeting-stack 的三个组件之一，负责资料库、检索、播放与编辑。整体介绍见
[顶层 README](../README.md)，安装步骤见 [docs/install.md](../docs/install.md)。

面向单用户的本地会议资料库。正式文件始终保留在归档根（`MEETING_WORKBENCH_ARCHIVE_ROOT`，
默认 `~/MeetingArchive`），工作台只维护索引、草稿、版本和控制台账，不持有原始文件。

## 界面口径

资料库按录音日期分组，日期是找回某场会的主线索；录音编号里的时间戳按本机墙上时间
解析，界面日期与归档目录名 `<YYMMDD 标题>` 始终一致。

界面上的会议状态只有「已完成」和「失败」两种。库里的 `completed_unreviewed`、
`draft_modified`、`published` 都算已完成——它们对使用者是同一件事：转写好了、能听能搜。
流水线阶段链也止于「纪要生成中 → 已完成」，校对与发布不再是必经环节。详情页的
「写回会议文件夹」仍可把工作台里的修改同步回归档目录，原音频永不改写。

纪要生成失败（`codex_callback` / `archive_validation`）且逐字稿已就绪的任务，后台扫描会
自动重派一次纪要生成，最多两次、间隔 20 分钟。纪要没补回来之前，标题显示为
`<YYMMDD> 未命名录音` 并在列表上标注「标题待生成」，不再退成一串编号。

## 安装与启动

见 [docs/install.md](../docs/install.md)。默认监听 `127.0.0.1:8765`，端口用
`MEETING_WORKBENCH_PORT` 改。

普通局域网不开放端口。启用远程访问时，Tailscale ACL 是设备身份边界，应用不另建账号。

## 常用维护

```bash
.venv/bin/meeting-workbench scan
.venv/bin/meeting-workbench semantic-index
.venv/bin/meeting-workbench backup
.venv/bin/meeting-workbench verify-audio
.venv/bin/meeting-workbench doctor
```

## 转写质量评测与影子模型

ASR 主引擎切换前必须先用人工金标比较，不以公开榜单代替真实会议验收。金标使用 UTF-8
JSONL，每行一条：

```json
{"id":"sample-001","reference":"人工校正后的原句","entities":["云图","ACME"],"numbers":["120"],"tags":["medical","amount"],"start_ms":1000,"end_ms":4200,"source_audio_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
```

`tags`、成对出现且满足 `0 <= start_ms < end_ms` 的毫秒时间范围、以及 64 位十六进制
`source_audio_sha256` 都是可选字段；字段一旦出现就按上述类型严格校验。旧版只含
`id/reference/entities/numbers` 的金标继续有效。

每个候选引擎把同 ID 的文本放成 `<引擎目录>/sample-001.txt`，再执行：

```bash
.venv/bin/meeting-workbench asr-evaluate \
  --gold ~/.meeting-workbench/asr-eval/gold.jsonl \
  --engine funasr=~/.meeting-workbench/asr-eval/funasr \
  --engine whisper=~/.meeting-workbench/asr-eval/whisper \
  --engine qwen3=~/.meeting-workbench/asr-eval/qwen3 \
  --output ~/.meeting-workbench/asr-eval/report.json
```

版本 2 报告保留中文字符错误率、实体/数字召回和缺失样本数，并增加逐样本、精确率、标签
分组、幻觉和连续重复明细。精确率的候选数来自本批金标已知实体/数字词表；未标注的自由文本
异常由幻觉指标另行识别。词表匹配按规范化值长度降序、同长度从左到右分配非重叠区间，较短
值不会在已命中的较长值内部重复计数。幻觉要求规范化候选至少比参考多 10 字、长度达到 1.5 倍，且按编辑
距离估算的可对齐内容不超过 50%；连续重复要求 2–12 字短片段至少连续出现 4 次。

可选的 Apple Silicon Qwen3-ASR 影子适配器默认使用 `~/.venvs/mlx-qwen3-asr` 中的
`Qwen/Qwen3-ASR-0.6B`（位置可用 `MEETING_WORKBENCH_QWEN_BINARY` 覆盖），
只读取本机缓存并强制离线，不会在运行期下载模型：

```bash
.venv/bin/meeting-workbench asr-shadow-qwen "/绝对路径/会议.m4a" \
  --model 0.6B --output-dir ~/.meeting-workbench/asr-eval/qwen3
```

执行程序或模型缓存不可用时，影子任务会明确标记 `unavailable`，不影响 FunASR 主链。
工作台详情页可把最新 Whisper/Qwen 参考稿与 FunASR 主稿逐段对照，标出数字、英文术语、
文本差异和候选缺失风险；桌面端可把人工修正段保存为 ASR 金标，移动端保持只读。

新 Relay 任务可显式绑定最多 20 个本场高置信热词；词表会去重后快照，来源文件后续变化
不会污染任务：

```bash
../relay/quickstart/relayctl enqueue "/绝对路径/会议.m4a" \
  --hotwords "/绝对路径/本项目热词.txt" --json
```

不要把跨项目大词典作为默认热词。未显式绑定时继续使用原有可选默认词典逻辑。

开机自恢复通过 `ssh localhost → tmux` 启动，避免外置盘 TCC 权限差异：

```bash
./scripts/install-launch-agent.sh
```

Web 服务与 `meeting-relay` watchdog 是两个独立进程；停止工作台不会停止录音发现和转写。
`verify-audio` 每 7 天由独立 tmux session 完整核验一次原音频；发现缺失或哈希变化时只报警并
标记冲突，不会接受新文件或改写原始基准哈希。

工作台启动后会周期性扫描已通过 relay 完成回执校验的草稿与正式归档；残缺目录、失败 attempt
和伪造的中转 `published` 状态不会进入成功资料库。Whisper 对照稿与语义索引使用独立子状态，
不阻塞 FunASR 主稿和纪要完成。

进行中的 attempt 仅放在隐藏的
`<归档根>/.workbench-drafts/<job_id>/attempt-<n>`；完成回执校验后自动成为 Finder 可见的
归档根一级目录 `<归档根>/<YYMMDD 标题>`。文件系统不区分待校对与已校对，状态只在工作台
数据库和界面中维护；人工发布会在同一会议目录内原子更新规范文件，原音频保持不变。
历史已完成但仍在隐藏目录的产物可用 `relayctl migrate-pending [job_id ...] --json`
幂等迁移；旧版 `<归档根>/待校对/<标题>` 也会兼容迁入一级目录。工作台会把纯目录搬迁
识别为路径更新，不会覆盖已有人工草稿。
只有合成验收夹具可以通过 Relay 的 `archive-policy=hidden_fixture` 显式审计策略留在
hidden attempt；归档产物本身无权改变该策略。真实录音完成后必须进入归档根一级目录，
不得长期停留在隐藏目录或旧版 `待校对` 分层。

“重新生成纪要”会先把数据库中的当前逐字稿原子固化为一次性快照，再交给 relay/Codex；回执
必须带回同一份快照的 SHA-256。工作台只导入匹配快照的新纪要，不会把快照误当逐字稿，也不会
让生成期间产生的后续人工修改被旧结果覆盖。快照格式随纪要协议走：v1/v2 是
`input-transcript.txt`，v3 起是 `input-transcript.srt`，校验端按 manifest 里的
`minutes_protocol_version` 选文件名并同时比对两种渲染的哈希，重生成的纪要不会被误判为过期。
重生成成功后，新归档目录会接管规范位置，标题也从新纪要的一级标题取回。
完整“重新转写”仍走 FunASR 主链和 Whisper 旁路。

新 attempt 使用纪要内容协议 v3：先从真实 SRT 生成确定性的 `minutes-plan.json`，按连续
8–12 分钟窗口覆盖全部 cue；超过 45 分钟的会议还必须生成逐窗口 `minutes-ledger/` 后再
多阶段合并。最终 Markdown 同时包含固定短的“一分钟摘要”和随有效议题增长的“完整会议记录”，
并生成 `minutes-evidence.json`。完成回执、人工发布和证据读取都会核对 job/attempt、来源 SRT、
plan、账本、正文时间锚及哈希；旧 v1/v2 attempt 可继续读取和发布，但不伪报为 v3 可验证证据。

## 数据与安全

- 数据库：`~/.meeting-workbench/workbench.sqlite3`
- 本机备份：`~/.meeting-workbench/backups/`，保留 14 份
- 外置盘镜像：正式归档根下 `.meeting-workbench-backups/`，保留 14 份
- 每次备份都会生成唯一快照并执行 SQLite 完整性、SHA-256 与落盘校验；结果写入
  `~/.meeting-workbench/backups/last-backup.json`。外置盘副本失败时保留本机快照并标记降级
- 所有写接口要求同源、双提交 CSRF 与 `application/json`
- 移动端界面只读，用于资料库、检索、播放和阅读；接口权限仍由 Tailnet ACL 控制，不把 UA 或屏幕尺寸当成鉴权凭据
- 大录音通过 4 MiB JSON 分块上传，仅接受 `m4a/mp3/wav`，不会在浏览器或服务端一次性展开整段 Base64
- 数据库使用 schema v6；类型化冲突、ASR 金标、Qwen 影子任务及跨进程运行租约都保存在 SQLite。
  外部文件与数据库草稿冲突时，必须明确选择保留草稿、
  采用外部版本或丢弃草稿；音频完整性及发布恢复冲突只能由对应复验流程关闭，解决一种冲突不会清除其他冲突
- 逐字稿和纪要保存携带页面打开时的基础版本；遇到并发变化返回 409，并保留浏览器中的未保存文字
- 人工修改过的会议标题优先于目录推断标题，后续自动扫描不会把它改回文件名
- 前端不执行历史 HTML，不加载外部资源
- 发布采用独立副本和持久化 schema v3 日志；APFS/HFS+ 使用原子目录交换，exFAT 使用带旧目录备份的 staged 协议并保证崩溃可恢复，但不宣称文件系统级原子交换
- 新归档及完整重转写必须通过完整主产物、Whisper 对照稿和原音频哈希校验；交换后、提交前、提交后及异常恢复清理前都会复核文件集合与哈希，不确定时保留恢复副本并标记冲突
- 扫描始终复核字幕、逐字稿、纪要和 manifest 等文本产物哈希；即使外部工具保留了文件尺寸与修改时间，也不会绕过草稿冲突检测
- 已有历史正式归档仅重生成纪要时保留其来源豁免，但必须核对 relay attempt、逐字稿快照哈希和原音频哈希
- 语义模型固定为本地 `BAAI/bge-small-zh-v1.5`；运行时离线加载
- 精确搜索随人工编辑即时更新；语义索引由 15 秒后台扫描维护，最多延迟一个扫描周期
- `GET /api/health` 使用缓存的 Relay 探测结果，并分别报告扫描、语义索引、Relay worker、Qwen worker 与备份状态；
  `relayctl health --json` 的退出码 0/1/2 分别表示健康、降级和不可用
- 无遥测、无自动更新

正式归档根必须专用于会议资料。历史目录继续兼容没有 manifest 的 legacy 文本会议，因此不要把
普通文档目录混放到该根目录。`stop-after-stage` 是用户主动停止语义：当前阶段结束后保留产物，
任务仍以 `interrupted` 收口。
