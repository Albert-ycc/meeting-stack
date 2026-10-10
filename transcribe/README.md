# transcribe — 本地转写引擎

把音频转成逐字稿、字幕和说话人分组，全部在本机完成，不调用任何外部服务。

## 用法

```bash
# 完整流程：建文件夹、移入音频、转写
bash transcribe.sh /绝对路径/会议.m4a

# 指定热词词典（默认读音频同级的 prompt.txt）
bash transcribe.sh /绝对路径/会议.m4a /绝对路径/热词.txt

# 只把已有的 会议纪要.md 转成 HTML
bash transcribe.sh /绝对路径/会议文件夹/
```

## 产物

```
<录音名>/
  ├── <录音名>.m4a          原始音频
  ├── <录音名>.txt          逐字稿（每句一行）
  ├── <录音名>.srt          带时间轴字幕
  ├── <录音名>.spk.txt      带说话人分组（[spkN] [mm:ss] 前缀）
  ├── <录音名>.funasr.json  结构化结果
  ├── funasr.log            转写日志，含实际采用的热词表，可审计
  └── whisper-ref/          Whisper 对照稿（后台生成）
```

## 双引擎与为什么

`TRANSCRIBE_ENGINE` 三个取值：

| 值 | 行为 |
|---|---|
| `observe`（默认） | FunASR 加 Qwen3 精转出主稿，Whisper 转后台出对照稿 |
| `funasr` | 只跑 FunASR，仅供单独手工跑 `transcribe.sh` |
| `whisper` | 只跑 Whisper，仅供单独手工跑 `transcribe.sh` |

**relay 受控模式只支持 `observe`。** 交接时要 FunASR 的 `.spk.txt`、`.funasr.json`，发布时要
`whisper-ref/` 对照稿，单引擎产物不全，任务会在转写或发布环节失败，所以别在 relay 的环境里设
`funasr` / `whisper`。FunASR 环境缺失时脚本会大声回落成纯 Whisper，在受控模式下同样会失败，
要先把 FunASR 装好。

默认双跑是因为两个引擎的强弱互补。实测对比中，FunASR 错字更少、数字与金额更准，
非自回归结构也不会出现整段幻觉；弱点是字母类术语（缩写、英文产品名）不如 Whisper。
对照稿留在 `whisper-ref/`，工作台详情页可以逐段比对，标出数字、英文术语和文本差异。

Whisper 侧必须用带温度回退的实现（`openai-whisper`）。无温度回退的实现在长音频上会整段幻觉，
而且不报错——曾把一段 90 分钟录音转成完全虚构的内容。

## 模型

FunASR 侧组合四个模型：

| 模型 | 作用 |
|---|---|
| `paraformer-zh` | 中文 ASR 主模型 |
| `fsmn-vad` | 语音活动检测 |
| `ct-punc` | 标点恢复 |
| `cam++` | 说话人分离 |

Whisper 侧用 `turbo`。首次运行自动下载权重，之后离线加载。

FunASR 跑完后，文字再交给 Qwen3-ASR-0.6B（`mlx-qwen3-asr`，Apple Silicon 上跑）重转一遍，见下一节。

## Qwen3 文字精转

主稿默认分两遍出：

1. FunASR 照旧跑完，给出切块、说话人（cam++）和一份初稿。
2. 拿初稿按 `glossary/injection.py` 挑本场词典（和出纪要同一套规则：先认项目，再挑最多 50 条），
   作为 `--context` 交给 Qwen3-ASR 带字级时间戳重转。句子按 Qwen 的标点切，起止取字级时间戳，
   说话人取时间上重叠最多的那句 FunASR 的编号，切块会议的 `c1-spk0` / `c2-spk0` 规则不变。

四件套的格式不变：`.txt` / `.srt` / `.spk.txt` 写合并后的句子；`.funasr.json` 每块的 `result`
换成合并后的句子（带 `text_engine: qwen3-asr`），FunASR 的原始结果留在同一块的 `paraformer` 字段。

为什么分两遍：FunASR 的热词在转写前就要定，那时还不知道是哪个项目的会，只能用通用模板，项目专名
进不去；第二遍有了初稿才认得出项目。拿一场 39 分钟的真实项目会对照：线上稿里一个高频项目专名只写对 6 处；
同一份本场词典喂给 FunASR 热词能拉到 17 处，但会把热词硬塞进无关句子（「我建议」写成「问诊建于」）；
Qwen3 带同一份词典同样 17 处，另外改对了词典里没有的同音词（「氪金→客服」「解放→结算」这一类）。

每个 Qwen 分段（约 30 秒）单独体检，下面任一条成立这一段就退回 FunASR 原文，原因写进 `funasr.log`：
被截断；单字连着 8 次以上或 2–8 字的片段连着 5 遍以上；字数和同一时间窗的 FunASR 差太多；冒出 5 条
以上 FunASR 里没有的词典词（Qwen 偶尔把 context 原样吐出来）。字级时间戳和文字对不上、Qwen 没装、
超时（音频时长的 1.5 倍，至少 15 分钟）或报错时，整场用 FunASR 原文。

| 变量 | 作用 |
|---|---|
| `TRANSCRIBE_TEXT_ENGINE=paraformer` | 关掉第二遍，主稿就是 FunASR 原文 |
| `MEETING_RELAY_QWEN_ASR_BIN` | Qwen3-ASR 命令行位置，默认 `~/.venvs/mlx-qwen3-asr/bin/mlx-qwen3-asr` |
| `MEETING_RELAY_GLOSSARY_SNAPSHOT` | 词典快照，默认 `~/.meeting-workbench/glossary-snapshot.json`，和出纪要共用 |

耗时：0.6B 带字级时间戳约为音频时长的 0.26 倍（39 分钟的会约 10 分钟），加在 FunASR 之后。

## 热词

热词从 prompt 文件提取，**上限 20 词**。这个上限是硬约束：热词过多会造成假注入，
实测无关的专有名词会被硬塞进完全不相干的会议里 5-6 处，反而降低准确率。

实际采用的热词表会打进 `funasr.log` 供事后审计。

## 长音频

超过 50 分钟的音频自动切块转写再按偏移合并。16GB 内存机器上，95 分钟音频整段跑 cam++
聚类会被系统因内存压力杀掉，切半加 `batch_size_s=60` 才稳定。

切块的副作用：**说话人编号跨块不连续**，第一块的 `spk0` 和第二块的 `spk0` 未必是同一个人，
需要人工对齐。

`batch_size_s` 保持 60。调大能提速，但内存峰值会上去。

## 依赖

```bash
python3 -m venv ~/.venvs/funasr
~/.venvs/funasr/bin/pip install funasr modelscope torch torchaudio

python3 -m venv ~/.venvs/whisper
~/.venvs/whisper/bin/pip install openai-whisper

python3 -m venv ~/.venvs/mlx-qwen3-asr
~/.venvs/mlx-qwen3-asr/bin/pip install mlx-qwen3-asr   # 首次运行自动下载 Qwen3-ASR-0.6B 与对齐模型
```

装在别处就用 `MEETING_RELAY_FUNASR_PYTHON`、`MEETING_RELAY_WHISPER_BIN`、`MEETING_RELAY_QWEN_ASR_BIN` 指过去。
另需 ffmpeg（音频解码）与可选的 pandoc（纪要转 HTML）。
