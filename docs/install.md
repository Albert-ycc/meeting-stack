# 安装与运行

## 环境要求

- macOS（Apple Silicon 上验证过；relay 的守护管理与权限处理是 macOS 专属）
- Python 3.12 以上
- Node 20 以上
- ffmpeg / ffprobe（`brew install ffmpeg`）
- pandoc，可选，用于纪要转 HTML（`brew install pandoc`）
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

## 四、运行

```bash
# 工作台（三个 tmux session：Web、每日备份、每周完整性核验）
./workbench/scripts/remote-bootstrap.sh

# 录音监听
python3 relay/quickstart/relay_watchdog.py

# 可选：Voice Memos 桥接（仅 macOS + iPhone）
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
不需要远程访问就别跑这个脚本。

---

## 维护命令

```bash
cd workbench
.venv/bin/meeting-workbench scan             # 扫描归档根，同步索引
.venv/bin/meeting-workbench semantic-index   # 重建语义索引
.venv/bin/meeting-workbench backup           # 立即备份数据库
.venv/bin/meeting-workbench verify-audio     # 核验原音频哈希
.venv/bin/meeting-workbench doctor           # 体检
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
