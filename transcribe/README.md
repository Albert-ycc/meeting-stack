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
| `observe`（默认） | FunASR 出主稿，Whisper 转后台出对照稿 |
| `funasr` | 只跑 FunASR |
| `whisper` | 只跑 Whisper |

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
```

装在别处就用 `MEETING_RELAY_FUNASR_PYTHON` 和 `MEETING_RELAY_WHISPER_BIN` 指过去。
另需 ffmpeg（音频解码）与可选的 pandoc（纪要转 HTML）。
