# meeting-stack

本地会议录音工作流：录音落地即自动转写、归档、建立索引，产出的文本和你的项目文件躺在同一台机器上，
供 AI Agent 直接读取。

音频与逐字稿全程不出本机，转写由本地模型完成，没有按分钟计费的 API。

---

## 为什么会有这个东西

市面上的会议软件都能转写，也都能生成纪要。它们共同的问题是纪要生成完就结束了——文本躺在某个
SaaS 的数据库里，你的代码、需求文档、项目记录躺在自己电脑上，两边隔着一次导出和一次复制粘贴。

真正需要这些信息的时刻，是你在处理某个具体任务的时候：改一个接口，写一版方案，回一封邮件。
这时你需要的上下文有两个来源，一半在项目文件里（代码、文档、历史记录），另一半在会上说过的话里
（谁提的、为什么这么定、当时有什么顾虑）。前者 AI Agent 读得到，后者读不到。

这套工作流的做法是让两者以同构的形式共存：会议经本地转写变成纯文本，按日期归档到本地目录，
和项目文件一样是普通文件。于是给 Agent 补上下文的动作变成一次路径复制——工作台里点「复制文件夹路径」，
粘进 Claude Code，那场会的逐字稿、纪要、说话人分组就都进了 Agent 的可读范围，和项目代码在同一个会话里。

会议信息因此不再是一份读完就归档的记录，而成为项目上下文的一部分。这是整套东西真正的价值所在，
转写和纪要只是达成它的手段。

---

## 数据流与隐私边界

这条边界值得写清楚，方便你判断适不适合自己的保密要求：

```
录音文件 ──────────────────────► 永不离开本机
   │
   ├─► FunASR（paraformer-zh + fsmn-vad + ct-punc + cam++）
   │     逐字稿、字幕、说话人分组          ← 本地模型，离线运行
   │
   ├─► Whisper turbo 对照稿                ← 本地模型，离线运行
   │
   ├─► bge-small-zh-v1.5 语义索引          ← 本地模型，离线运行
   │
   └─► 纪要生成：把文本逐字稿交给你配置的 AI Agent
                                            ↑
                        ██ 这一步文本会离开本机 ██
                        （除非你把 Agent 换成本地 LLM）
```

音频、逐字稿、说话人分离、语义检索全部在本机完成，不依赖任何外部服务，断网可用。

唯一出本机的是纪要生成环节：文本逐字稿会递给你配置的 Agent（默认 Claude Code，可切 Codex）。
录音本身从不上传。如果你的会议连文本都不能外传，可以把派单目标改成本地 LLM，
需要自行扩展 `relay/quickstart/relay_watchdog.py` 里的派单适配——当前内置的两个目标都是外部服务。

无遥测，无自动更新，不回传任何统计信息。

---

## 特点

**转写零成本。** 三类本地模型跑在自己机器上：FunASR 出主稿（含 cam++ 说话人分离）、
Whisper turbo 出对照稿交叉核对字母类术语、bge-small-zh 建语义索引。没有按分钟计费的 API，
录多久都一样。Apple Silicon 上 FunASR 约 5 倍实时速度。

**纪要样式随你定。** 纪要生成是把逐字稿交给外部 Agent 完成的，写成什么样取决于你给的 prompt。
换个 prompt 就换套模板，不必迁就某个 SaaS 内置的固定格式。

**和本地 AI 生态打通。** 工作台每个会议都有「复制文件夹路径」，粘进 Claude Code 或任何能读本地文件的
Agent，那场会的全部文本立刻可用。这是前面说的方法论的落地方式。

**适合不能外传的会议。** 音频与逐字稿留在本机，只有纪要生成一步涉及外部服务，边界清楚可控。

---

## 架构

```
录音来源
  ├─ iPhone 语音备忘录 →（可选桥接）→ 监听目录
  └─ 任何方式放进监听目录的音频文件
        │
        ▼
   relay/  监听 → ffprobe 探时长 → 分流
        │         < 10 分钟：转写文本当即时指令派给 Agent
        │         ≥ 10 分钟：走会议纪要流程
        ▼
   transcribe/  本地转写（FunASR 主稿 + Whisper 对照稿）
        │
        ▼
   归档根 <YYMMDD 主题>/   音频、逐字稿、字幕、说话人分组、纪要
        │
        ▼
   workbench/  资料库、全文与语义检索、播放、逐字稿编辑、复制路径给 Agent
```

三个组件各自独立运行，停掉工作台不影响录音发现和转写。

| 目录 | 作用 | 说明 |
|---|---|---|
| [`workbench/`](workbench/) | Web 工作台 | FastAPI + React，默认 `127.0.0.1:8765` |
| [`relay/`](relay/README.md) | 录音发现与派单 | 监听目录、时长分流、任务队列 |
| [`transcribe/`](transcribe/) | 本地转写引擎 | FunASR + Whisper 双跑 |

**不需要 iPhone。** Voice Memos 桥接只是可选入口之一，任何来源的音频文件放进监听目录都会被处理。

---

## 快速开始

需要 macOS（TCC 权限与 launchd 相关脚本是 macOS 专属）、Python 3.12+、Node 20+、
ffmpeg，以及 16GB 以上内存（cam++ 说话人聚类在长音频上吃内存）。

```bash
git clone <本仓库地址> meeting-stack
cd meeting-stack

# 1. 装工作台（建 venv、装依赖、跑测试、下载语义模型）
./workbench/scripts/install-local.sh

# 2. 装转写引擎依赖
python3 -m venv ~/.venvs/funasr
~/.venvs/funasr/bin/pip install funasr modelscope torch torchaudio
python3 -m venv ~/.venvs/whisper
~/.venvs/whisper/bin/pip install openai-whisper

# 3. 配置
cp .env.example .env    # 至少改 MEETING_ARCHIVE_ROOT
```

启动：

```bash
./workbench/scripts/remote-bootstrap.sh          # 工作台
python3 relay/quickstart/relay_watchdog.py       # 录音监听
```

打开 http://127.0.0.1:8765 ，把一个音频文件丢进 `~/Downloads` 就会自动进入流程。

完整安装说明与 macOS 权限问题见 [docs/install.md](docs/install.md)。

---

## 配置

所有配置走环境变量，工作台读 `.env`。常用的几个：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEETING_WORKBENCH_ARCHIVE_ROOT` | `~/MeetingArchive` | 归档根，指向哪都行（外置盘、NAS 挂载点） |
| `MEETING_WORKBENCH_PORT` | `8765` | Web 端口 |
| `MEETING_RELAY_WATCH_DIR` | `~/Downloads` | 监听目录 |
| `MEETING_RELAY_AGENT` | `claude` | 派单目标，`claude` 或 `codex` |
| `TRANSCRIBE_ENGINE` | `observe` | `observe`=双跑、`funasr`=只主稿、`whisper`=只对照稿 |
| `RELAY_LARK_USER_ID` | 空 | 飞书通知，留空则不通知 |

完整清单见 [.env.example](.env.example) 与各组件 README。

---

## 已知限制

这是为单用户单机场景写的工具，没有多租户、没有账号体系。远程访问依赖 Tailscale ACL 做设备身份边界，
应用层不另建鉴权，局域网端口不开放。

macOS 之外没有验证过。转写引擎本身跨平台，但 relay 的守护进程管理、TCC 权限处理、launchd
脚本都是 macOS 专属。

说话人分离在超过 50 分钟的音频上会切块处理，切块之间说话人编号不连续（第一块的 spk0 和第二块的 spk0
未必是同一个人），需要人工对齐。

FunASR 在字母类术语（缩写、英文产品名）上不如 Whisper，这也是默认双跑的原因——工作台详情页可以把两份
稿子逐段对照，标出数字、英文术语和文本差异。

---

## License

MIT，见 [LICENSE](LICENSE)。
