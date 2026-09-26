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

逐字稿每段带说话人标签（界面显示「说话人 1」；超 50 分钟的切块转写显示
「片段2·说话人1」，跨片段的同号说话人未必是同一人）。会议详情页可把标签改成真名，改名
只作用于当前标签的段落、只在本场会议内生效。说话人数据来自 FunASR 转写自带的 cam++
分离，SRT 仍是逐字稿文本权威，`funasr.json` 只按段序号对齐补说话人字段；新会议导入时
自动补标，存量由 `backend/scripts/backfill_speakers.py` 一次性回填（设计决策见
[docs/speaker-diarization.md](../docs/speaker-diarization.md)）。

## 视觉体系

2026-08-18 起前端换成深色体系，配色、字体、几何全部收在
`frontend/src/styles.css` 顶部的 `:root` 里：底色三层 `--bg #121212` → `--surface #181818`
→ `--stage #131313`，唯一 accent 是声档橙 `--signal #f0783b`，语义色分 `--ok / --warn /
--danger` 三族且各带 `-bg` / `-line` 变体。旧 token 名（`--ink` / `--paper` / `--night` /
`--teal` 等）保留为兼容层指向新值，所以全站 460 多处 `var()` 引用不用改。

**要换配色只改 `:root`**，不要在具体规则里写死颜色——之前 359 处硬编码色值让换主题要动
八个文件，现在全部走 token。canvas 里的颜色（波形、抖动图表）也是运行时读 `:root` 拿的。

**深色 / 浅色 / 跟随系统三选一**：顶栏右侧的三个图标切换，偏好只存在本机浏览器
（`localStorage` 的 `meeting-workbench:theme`），默认跟随系统。深色是 `:root` 的默认值，
浅色在 `:root[data-theme="light"]` 里覆盖同一批 token；`src/theme.ts` 解析偏好并写
`<html data-theme>`，`public/theme-init.js` 在首帧前先写一次避免闪屏（服务端 CSP 是
`script-src 'self'`，所以走外链脚本而不是内联）。组件里的半透明叠色写
`rgb(var(--fg-rgb) / α)`、阴影写 `rgb(0 0 0 / calc(α * var(--shadow-k)))`，两套主题各自
调深浅；canvas 组件把 `useTheme().resolved` 放进依赖，换主题时重绘。

## 交互约定

- 每个视图和打开的会议都有地址锚点（`#library`、`#tasks`、`#meetings/<id>` …），视图切换压入
  浏览器历史：后退键回到上一个视图，打开的会议后退即关掉，刷新不丢位置。会议详情的「← 返回」
  回到打开它之前的视图（检索结果也保留），不再一律回录音档案。
- 保存、回滚、改说话人后会议详情静默刷新，不整页闪加载，标签页、滚动与播放进度都保留；
  需求详情同理。
- 弹窗共用 `components/useDialog.ts`：焦点进出与 Tab 循环、背景不滚动、Esc 关闭（输入法组合中
  的 Esc 不算）。有输入内容的表单弹窗点背景不关；只做选择的弹窗点背景关，且只认按下和松开都在
  背景上的点击。
- 丢弃草稿、丢弃未保存修改、写回会议文件夹、删除术语、移除材料根目录这类操作统一走
  `components/ConfirmDialog.tsx` 的二次确认；需要调接口的确认，失败原因写在确认框里。
- 复制路径在 `http://<局域网 IP>` 这类非安全上下文里退回 `execCommand("copy")`（`src/clipboard.ts`）。
- 任务池、需求池、项目管理、词典的页签、查询条件和页码，以及项目详情里两张子表的页签与页码，
  离开页面再回来原样保留，刷新也不丢，关掉标签页才清空（`src/viewState.ts` 的 `usePersistentState`，
  存在内存和 `sessionStorage`）。录音档案的筛选一直由 App 持有，本来就保留。
- 操作结果提示统一用 `components/Notice.tsx`：成功（绿）5 秒后自动收起；提醒（黄，比如「请求发出后
  的本地修改仍保留，请再次保存」）和失败（红，`role="alert"`）不自动收起，点 ✕ 或下一次操作才换掉。
  复制成功这类一闪而过的确认仍用 `Toast`。

字体是 `Outfit`（拉丁与数字）+ `PingFang SC`（中文）。Outfit 走自托管
`frontend/public/fonts/outfit.woff2`（32KB 可变字体，覆盖 100~900 字重），**不要改成
Google Fonts CDN**，否则断网时字体掉回系统默认。

`frontend/src/components/charts/` 是 Bayer 8×8 有序抖动图表（`DitherArea` /
`DitherDonut` / `DitherCalendar`），用网点密度表达数值，全灰阶 + 橙点缀。工作台的两个
图表自己单独取一次 `meetings`（`limit: 400`），因为主列表只加载一页盖不住 30 天，
缺的日子会被画成「当天没有会议」。

微动效在 `frontend/src/components/motion/`。`ScrollReveal` 走 framer-motion 的
`whileInView` + `viewport.amount = 0`，**不要改回 IntersectionObserver 的比例阈值**：
高度超过视口的容器永远达不到 12% 可见比例，整块内容会卡在 `opacity: 0`。`FadeContent` 刻意不用 `AnimatePresence`，`mode="wait"` 要等退出动画
跑完才挂新内容，动画时钟不推进时（后台标签页、减动效、测试环境）新内容永远不出现。

改完前端必须 `cd frontend && npm run build`，后端直接服务 `frontend/dist`。

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

### 材料盘点与图片文字识别试跑（给第三期摸底，只读）

```bash
# 走一遍所有项目挂的材料文件夹，按「文档正文 / 图片文字 / 音视频转写 / 只收文件名」分层统计，
# 列出读不了的（要密码、文件损坏、格式不支持、处理超时、没有权限）；不写库、不写盘
.venv/bin/meeting-workbench materials walk --dry-run --json ~/Desktop/材料盘点.json
# 只看一个项目，或直接指定文件夹
.venv/bin/meeting-workbench materials walk --dry-run --project 云图AI
.venv/bin/meeting-workbench materials walk --dry-run --root /Volumes/资料盘/项目

# 挑 20 张材料里的图，分别用 macOS 自带的 Vision 和 tesseract 识别，比较用时和效果；
# 对照结果写到 ~/.meeting-workbench/ocr-trial/<时间>/结果.md
.venv/bin/meeting-workbench materials ocr-trial
```

Vision 需要 Xcode 命令行工具（`xcode-select --install`）；tesseract 需要
`brew install tesseract tesseract-lang`。哪个没装就只跑另一个。装了 ffprobe
（`brew install ffmpeg`）时，盘点会顺带算出音视频总时长。

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

受管会议目录（带 `workbench-manifest.json`）的校验只管**身份与音频锚点**：manifest 自身结构、
relay 库里的 job / attempt / archive_dir、原音频与输入逐字稿快照的哈希、登记文件是否齐全。这四类
对不上才隔离，`/api/health` 会分别写明原因。目录里多出来的文件（自己放的笔记、外部工具生成的
产物）当普通产物归档；manifest 登记的纪要、逐字稿等文本被改写不再隔离，而是走既有的外部变更
检测——没人工编辑过就直接更新，有草稿就开冲突让人选。早期的严格版本曾把直接改过盘上纪要、
或目录里多放了一份笔记的会议静默冻结在隔离前的状态。

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

纪要默认跑在 relay 的全局后端上。详情页“用 Claude 重写”按钮在同一个重生成链路上多带一个
`backend=claude`，只钉住这一次 attempt，不改全局配置；完成后新纪要覆盖当前版本，旧版本仍留在
纪要历史里可回滚。后端只接受 `claude` / `deepseek`，其余值由接口挡回 422。

新 attempt 使用纪要内容协议 v3：先从真实 SRT 生成确定性的 `minutes-plan.json`，按连续
8–12 分钟窗口覆盖全部 cue；超过 45 分钟的会议还必须生成逐窗口 `minutes-ledger/` 后再
多阶段合并。最终 Markdown 同时包含固定短的“一分钟摘要”和随有效议题增长的“完整会议记录”，
并生成 `minutes-evidence.json`。完成回执、人工发布和证据读取都会核对 job/attempt、来源 SRT、
plan、账本、正文时间锚及哈希；旧 v1/v2 attempt 可继续读取和发布，但不伪报为 v3 可验证证据。
manifest 的身份判定与 `whisper-ref/` 豁免在导入器和证据读取之间只有一份实现
（`minutes_evidence.manifest_meeting_id_matches` / `is_manifest_exempt_path`）：relay 写侧从不写
`meeting_id`，身份由 `vm-` / `fp-` 前缀派生；`whisper-ref/` 在 manifest 落盘后仍在追加，不参与
完整性比对；目录里未登记的文件不算证据不完整。两侧口径一旦分家，证据接口会在真实数据上整体失效。

## 数据与安全

- 数据库：`~/.meeting-workbench/workbench.sqlite3`
- 本机备份：`~/.meeting-workbench/backups/`，保留 14 份
- 外置盘镜像：正式归档根下 `.meeting-workbench-backups/`，保留 14 份
- 每次备份都会生成唯一快照并执行 SQLite 完整性、SHA-256 与落盘校验；结果写入
  `~/.meeting-workbench/backups/last-backup.json`。外置盘副本失败时保留本机快照并标记降级
- 备份不含 `embeddings`（`backup.py` 的 `DERIVED_TABLES`）：向量占主库七成体积且能从 `segments`
  重算，快照体积因此从 400 MB 级降到 120 MB 级。**从备份恢复后不需要额外操作** —— 服务启动后的
  后台循环会调 `SemanticIndex.rebuild()` 自动补齐，补齐前语义检索结果为空、全文检索不受影响。
  receipt 里的 `derived_stripped` 标明该快照是否做过这步剥离
- 所有写接口要求同源、双提交 CSRF 与 `application/json`
- 移动端界面只读，用于资料库、检索、播放和阅读；接口权限仍由 Tailnet ACL 控制，不把 UA 或屏幕尺寸当成鉴权凭据
- 大录音通过 4 MiB JSON 分块上传，仅接受 `m4a/mp3/wav`，不会在浏览器或服务端一次性展开整段 Base64
- 数据库使用 schema v12；类型化冲突、ASR 金标、Qwen 影子任务、跨进程运行租约、术语词典、会议项目归属及项目/需求/任务三层都保存在 SQLite。
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
- 精确搜索随人工编辑即时更新；语义索引由后台扫描维护，最多延迟一个扫描周期。扫描是「上一轮结束后歇 15 秒再开下一轮」，
  一轮耗时看 `/api/health` 的 `last_started_at` / `last_completed_at`；受管目录校验复用 `fingerprint_cache`（按路径 + 大小 + mtime 命中就不重算音频哈希），未发布存量增长不会让每轮重读全部音频
- `GET /api/health` 使用缓存的 Relay 探测结果，并分别报告扫描、语义索引、Relay worker、Qwen worker 与备份状态；
  `relayctl health --json` 的退出码 0/1/2 分别表示健康、降级和不可用
- 无遥测、无自动更新

正式归档根必须专用于会议资料。历史目录继续兼容没有 manifest 的 legacy 文本会议，因此不要把
普通文档目录混放到该根目录。`stop-after-stage` 是用户主动停止语义：当前阶段结束后保留产物，
任务仍以 `interrupted` 收口。

## 任务代办与项目看板（260804 新增）

会议纪要生成后，AI 会把会上拍板的事项整理成任务草稿，先进「待确认」闸门，确认后才进正式清单。
任务与来源会议、所属项目关联；项目下能看到每场会的任务与交付物链接。

- 任务状态机：待确认 → 已确认 → 进行中 → 已完成 / 已取消（驳回 = 取消）
- AI 抽取挂在工作台扫描周期内，检测到新的纪要版本自动触发；未配置模型时自动跳过、不影响主链
- 项目归属三级：AI 直接匹配既有项目 → 本地语义模型兜底 → 建议新建（确认任务时才真正建项目）
- **会议自动归属项目（260905 新增）**：流水线在「纪要生成 → 完成」之后多一步。会议有纪要且从未归属过时，
  扫描周期里自动归类，命中即停：AI 看标题 + 纪要 + 项目列表给出精确项目名（只认 `confidence=high`）→
  这场会已归属任务的多数项目（占比 ≥ 50%）→ 都没有就留空，不瞎猜。本地语义兜底（与任务归属共用同一份实现
  与阈值）只在 AI 没能作答（没配 key / 调用失败）时启用——它只看标题向量，不配推翻读过全文并明确说 low 的
  判断（首批存量里它 4 场错 2 场）。新建项目（人工建或任务确认时 AI 建议新建）后，之前判「不归属」的会议会在
  下一轮自动再判一次。结果直接落 `meetings.project_id` 并标 `project_origin='ai'`，会议页归档归属面板显示「AI 归属」；
  人工保存一次（改或清空）就变成 `manual`，之后自动归类永远不再碰这场会。每个纪要版本只归一次
  （`project_links` 表），事件表记 `meeting_project_auto_assigned` / `meeting_project_unresolved`。
  存量用 `.venv/bin/meeting-workbench backfill-projects --dry-run` 先看分布再去掉 `--dry-run` 落库；
  整体撤回：`UPDATE meetings SET project_id=NULL, project_origin=NULL WHERE project_origin='ai'`。
  一场会只保留一个主项目，跨项目的会靠各条任务自己的项目归属体现。
- 飞书通知单向触达。一场会在群里推三次：**转写完成**（relay watchdog 发，只报时长
  不带内容）→ **纪要写好**（会议名 + 录音元信息 + **纪要全文**）→ **任务待确认**
  （逐条确认/驳回）。三条都经 `notifications` 台账幂等，同一份纪要只发一次
- 纪要卡直接摊开全文，不摘要、不给「点开看全文」的跳转：服务只跑在本机，链接在手机上
  打不开，给了等于没给。正文超过一张卡装得下的长度（飞书上限 30 KB）时按段落切成多张
  卡顺序发全，标题上标 n/N，一个字都不丢

### 新增配置（环境变量，前缀 `MEETING_WORKBENCH_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY_FILE` | `~/.config/ds/api-key` | 任务抽取用的 DeepSeek API key 文件路径；文件内容即 key，留空则抽取自动跳过 |
| `LLM_MODEL` | `deepseek-chat` | 抽取模型 |
| `LARK_WEBHOOK_URL` | 空 | 飞书群自定义机器人 webhook；留空 = 通知整体关闭 |
| `LARK_CHAT_ID` | 空 | 应用机器人发到的群；非空时优先于 webhook |
| `LARK_APP_ID` | 空 | 自建应用直连通道的 app_id；与下面的 secret 文件同时配上才生效 |
| `LARK_APP_SECRET_FILE` | 空 | 自建应用 secret 的本机文件路径（`chmod 600`）。配上后直接调开放平台发卡片，不经 lark-cli、不依赖 GUI 会话的 keychain；多实例部署时各实例各走各的应用 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8765` | 本机地址（纪要卡不再放跳转链接，此项只留给其他通知） |
| `TASK_STALL_AFTER_DAYS` | `3` | 停滞督办阈值（天） |
| `MATERIAL_BROWSE_ROOT` | `~`（用户主目录） | 项目材料目录可浏览/挂靠的范围（260915 新增），见下一节 |

### 重启生效

```bash
./scripts/stop-web.sh && ./scripts/start-via-ssh.sh
```

`card_listener.py` 是单独的进程，确认/驳回后重建卡片用的是它启动时加载的 `notify.py`。
改过任务卡片（例如这一版卡片上多了「项目：…」一行）后也要重启它，不然点完按钮刷新出来的
还是旧样子的卡片。

## 项目 → 需求 → 任务三层（260915 新增）

任务池之上多两层：项目挂「材料根目录」（外置盘上的目录，可挂多个）；需求归属项目，挂「材料
文件夹」（所属项目某个材料根目录下的一级子文件夹），可关联多场会议。优先级 P0–P3 只挂在需求
上，任务本身不设优先级，展示时从所属需求只读派生；任务挂需求后项目跟着需求走——设需求时任务
项目自动改成需求项目，只改任务项目且与原需求项目不一致时自动移出需求，需求换项目时名下任务的
项目在同一事务里跟着改，全部改动写入任务的讨论轨迹。需求不做删除，不要的需求置为「已搁置」。

材料目录浏览与统计限制在 `MATERIAL_BROWSE_ROOT` 之下（realpath 判定，拒绝 `..` 与指向外部的
符号链接），跳过隐藏项、`node_modules`、`__MACOSX`，不跟随符号链接，单文件夹最多数到 2000 个
文件；外置盘 exFAT 递归慢，项目材料子文件夹统计按根目录短 TTL 缓存。相关代码在
`materials.py`（文件系统）与 `requirements.py`（需求业务）。

## 数据库迁移

首次启动会自动备份并把数据库迁移到当前 schema v12（v7 曾新增 tasks / task_events /
deliverables / task_extractions / notifications 五张表及 projects.origin 列；v8 新增
术语词典 glossary_terms / glossary_suggestions 两张表，快照导出到
`~/.meeting-workbench/glossary-snapshot.json` 供转写侧消费；v9 新增 `meetings.project_origin` 与
`project_links` 表；v10 新增 `glossary_terms.project_id`，并把 `scope` 与项目同名的术语自动挂上项目；v11 新增 `job_acknowledgements`，记录资料库「需要处理」里确认归档过的失败任务，任务之后又有变化会重新出现；v12 新增项目 → 需求 → 任务三层——`project_material_roots`、`requirements`、`requirement_folders`、`requirement_meetings` 四张表及 `tasks.requirement_id` 列，只加不改）。

## 术语词典（260821 新增，260905 与项目打通）

词典有两个用途：转写侧（relay）生成纪要前把「通用」术语 + 转写稿命中的范围术语注入 prompt 防错字；
工作台侧编辑纪要时按 diff 捕获疑似错字更正进「待确认」队列，确认后反写别名并重导快照。

**范围 = 项目，单一来源是 `glossary_terms.project_id`**。挂了项目的术语，`scope` 列派生为项目名（改项目名时
同步）；没挂项目的术语，`scope` 是自由桶——默认「通用」，也允许「互联网医院」这类不是项目的领域桶。快照里的
`scope` 字段名与语义不变，relay 侧「命中任一术语即注入该 scope」的逻辑照常，只是 scope 现在与项目名对齐。

界面：筛选器是一排 chips（全部 · 通用 · 各项目 · 其他桶，带计数），「全部」按分组平铺所有范围；术语是紧凑
卡片网格，一屏能看二三十张；术语弹窗的「归属」三选一（通用 / 项目 / 其他范围）；项目详情页有「词典」块，
可一键跳到词典并预选该项目。
