# 安装与运行

## 环境要求

- macOS（Apple Silicon 上验证过；relay 的守护管理与权限处理是 macOS 专属）
- Python 3.12 以上
- Python 包 `watchdog`：只有录音监听 `relay_watchdog.py` 要，装法见「四、运行」
- Node 20 以上
- ffmpeg / ffprobe（`brew install ffmpeg`）
- pandoc，可选，用于纪要转 HTML（`brew install pandoc`）
- Xcode 命令行工具，可选，用于读 PDF、认图片和扫描件里的字（`xcode-select --install`）；没有时可以装
  tesseract 认图片里的字（`brew install tesseract tesseract-lang`），但 PDF 要等命令行工具装上才读
- 内存 16GB 以上。cam++ 说话人聚类在长音频上吃内存，8GB 机器跑 90 分钟以上录音会被系统杀掉

## 一、工作台

```bash
./workbench/scripts/install-local.sh
```

这个脚本会建 venv、装依赖、跑完整测试、构建前端、下载语义模型、初始化索引。
中途任何一步失败都会停下，不会留下半装状态。

装完确认：

```bash
cd workbench && .venv/bin/meeting-workbench doctor
```

## 二、转写引擎

两个引擎装在各自独立的 venv 里，互不干扰：

```bash
# FunASR：主稿 + 说话人分离
python3 -m venv ~/.venvs/funasr
~/.venvs/funasr/bin/pip install funasr modelscope torch torchaudio

# Whisper：对照稿
python3 -m venv ~/.venvs/whisper
~/.venvs/whisper/bin/pip install openai-whisper
```

首次运行会自动下载模型权重（paraformer-zh、fsmn-vad、ct-punc、cam++、whisper turbo），
之后完全离线运行。合计约 3–4 GB。

装在别的位置就用 `MEETING_RELAY_FUNASR_PYTHON` 和 `MEETING_RELAY_WHISPER_BIN` 指过去。

单独测一下转写：

```bash
bash transcribe/transcribe.sh /绝对路径/测试音频.m4a
```

## 三、配置

```bash
cp .env.example .env
```

最少需要改 `MEETING_WORKBENCH_ARCHIVE_ROOT` 和 `MEETING_RELAY_ARCHIVE_ROOT`
（两者要指向同一个目录）。该目录应专用于会议资料，不要和普通文档混放——
扫描逻辑会把里面的目录当会议处理。

谁读这份 `.env`：

- **工作台**自己读。仓库根的 `.env` 和 `workbench/.env` 都会读，两处都写了的项以 `workbench/.env` 为准，
  与从哪个目录启动无关。
- **relay**（`relay_watchdog.py`、`relayctl`）作为命令启动时也读，找法一样，并且只取 relay 自己用的键
  （`MEETING_RELAY_*`、`RELAY_*`、`TRANSCRIBE_ENGINE`）。**已经在环境变量里的值优先**，`.env` 不覆盖，
  所以 launchd / ssh 启动命令里显式写的值照旧生效。转写脚本由 watchdog 拉起，拿到的环境里已经带着这些值。
- 卡片监听**不读** `.env`，只认进程的环境变量，写进它自己的启动命令里。

`.env.example` 里的 `MEETING_RELAY_CONTROL_ENABLED=1` 必须保留，relay 启动时会读到它。不设时 watchdog 走旧同步路径：
工作台入队的任务没人领；监听目录里短于 10 分钟的音频会被当成口述指令，转写后直接派给 Agent 执行。
所以监听目录（`MEETING_RELAY_WATCH_DIR`，默认 `~/Downloads`）别用会落进不可信文件的目录，
专门建一个只放录音的目录最稳妥。

## 四、运行

### 录音监听要先装 watchdog 包

`relay_watchdog.py` 靠第三方包 `watchdog` 收监听目录的文件事件，这是 relay 里唯一要 pip 装的包
（`relayctl`、`voicememos_bridge.py` 和工作台后端都用不到它）。包要装进**启动 `relay_watchdog.py` 的那个
Python**。Homebrew 装的 Python 不让往系统环境里 `pip install`（报 `externally-managed-environment`），
建一个专用 venv 最省事，和上面转写引擎的做法一样：

```bash
python3 -m venv ~/.venvs/relay
~/.venvs/relay/bin/pip install watchdog

# 验证：用要跑 watchdog 的那个 Python 导入一次，打出版本号才算装好
~/.venvs/relay/bin/python -c "import watchdog.version as v; print(v.VERSION_STRING)"
```

没装时 `relay_watchdog.py` 一启动就停在 `ModuleNotFoundError: No module named 'watchdog'`，任务不会被领、
监听目录里的新录音不会入队。已经常驻在跑的守护用的是哪个 Python，看
`ps -axo command | grep relay_watchdog` 的第一段，拿它再跑一遍上面的验证命令。

下面启动 watchdog 的命令用的是上面这个 venv，换了别的 Python 就改成那个。

### 启动

```bash
# 工作台（三个 tmux session：Web、每日备份、每周完整性核验）
./workbench/scripts/remote-bootstrap.sh

# 录音监听：relay 自己读 .env（值里有空格要加引号），不用再 source
~/.venvs/relay/bin/python relay/quickstart/relay_watchdog.py

# 可选：Voice Memos 桥接（仅 macOS + iPhone，只用标准库，不用装包）
python3 relay/quickstart/voicememos_bridge.py
```

打开 http://127.0.0.1:8765 ，往监听目录丢个音频文件试试。

### 开机自恢复

```bash
./workbench/scripts/install-launch-agent.sh
```

装一个 LaunchAgent，每 5 分钟确认工作台在跑，不在就拉起。

---

## macOS 权限：这里最容易卡住

macOS 的 TCC（透明度、同意与控制）按**进程链**授予文件访问权限，不是按用户。这带来一个反直觉的结果：

**launchd 直接拉起的进程（包括它启动的 tmux server）常常读不到 `~/Downloads`、外置卷、
Voice Memos 容器**，即使你在系统设置里给终端授予了完全磁盘访问。因为拿到授权的是终端，
不是 launchd 的子进程。

绕开的办法是让守护经 `ssh localhost` 启动：sshd 通常已在「完全磁盘访问」名单内，
经它启动的进程能继承该授权。这就是 `start-via-ssh.sh` 存在的原因，也是
`install-launch-agent.sh` 装的 LaunchAgent 调用它而非直接调用服务的原因。

前提是打开「系统设置 → 通用 → 共享 → 远程登录」，并给 `sshd-keygen-wrapper`
授予完全磁盘访问。

### ssh 链的副作用：PATH 是裸的

`ssh localhost '<cmd>'` 走的是非交互 shell，PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin`，
Homebrew 装的 ffmpeg / ffprobe 全都不在里面。

症状很隐蔽：长录音在 ffprobe 探测时长的地方抛 `FileNotFoundError`，任务卡住但不报错，
可能几小时后你才发现那场会没转写。

`relay_watchdog.py` 和 `remote-bootstrap.sh` 启动时都会自愈补 `/opt/homebrew/bin`。
新增外部命令依赖时记得这个前提。

---

## 可选：远程访问

```bash
./workbench/scripts/configure-tailscale.sh
```

把工作台暴露到自己的 Tailscale 网络，手机等设备可访问（移动端界面只读）。

设备身份边界完全由 Tailscale ACL 控制，应用层不另建账号体系。普通局域网端口始终不开放。
远程访问的域名要写进 `.env` 的 `MEETING_WORKBENCH_PUBLIC_BASE_URL`（如 `https://<本机>.ts.net`），
服务只放行本机回环地址和这个域名；别的名字要放行时写进 `MEETING_WORKBENCH_ALLOWED_HOSTS`（逗号分隔，精确匹配）。
不需要远程访问就别跑这个脚本。

---

## 维护命令

```bash
cd workbench
.venv/bin/meeting-workbench scan             # 扫描归档根，同步索引
.venv/bin/meeting-workbench semantic-index   # 重建语义索引
.venv/bin/meeting-workbench backup           # 立即备份数据库
.venv/bin/meeting-workbench verify-audio     # 核验原音频哈希
.venv/bin/meeting-workbench doctor           # 体检（含读材料用到的程序）
.venv/bin/meeting-workbench materials status # 材料读了多少、哪些读不了
```

备份保留 14 份，本机在 `~/.meeting-workbench/backups/`，归档根下另有一份镜像。
每次备份都会做 SQLite 完整性检查与 SHA-256 校验。

## 故障排查

**录音丢进去没反应**：确认 watchdog 在跑，确认监听目录配置正确，看
`~/Library/Logs/meeting-relay-watchdog.log`。

**转写卡住不动**：多半是 ffprobe/ffmpeg 不在 PATH，见上面的 ssh 链副作用。

**长音频转写被杀**：内存不够。FunASR 对超过 50 分钟的音频会自动切块，
但 16GB 以下机器仍可能在 cam++ 聚类阶段被系统杀掉。

**工作台看不到已完成的会议**：跑一次 `meeting-workbench scan`。
残缺目录和未通过回执校验的任务不会进入资料库，这是有意的。
