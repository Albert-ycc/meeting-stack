"""造数第一步：在隔离归档根里摆会议文件夹（ffmpeg 生成的音频 + SRT 逐字稿 + 会议纪要.md），
之后由 run.py 用 `scan` 让导入器扫进来。只写 $E2E_ROOT/archive 和 $E2E_ROOT/seed.json。

会议日期按「今天」往前推（北京日历），不写死某年某月：关系图的时间窗（7 / 28 / 90 天）、待办的草稿过期都按当前日期算，
写死了的日期过几周就会让用例没有可点的东西。录音编号 vm-YYYYMMDD-HHMMSS-<后缀> 里带日期，所以编号也跟着变，
用例按后缀（a1a1a1a1 这种）从 seed.json 里取编号，不要写死。
"""

import json
import os
import random
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ["E2E_ROOT"])
ARCHIVE = ROOT / "archive"
FFMPEG = os.environ.get("E2E_FFMPEG", "ffmpeg")
TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()

# (往前几天, 时分秒, 目录标题, 后缀, 时长秒, 说话人数, 主题词)
MEETINGS = [
    (31, "100000", "医米京东科研仓对接", "a1a1a1a1", 120, 3, "京东科研仓"),
    (29, "143000", "医米赠药横跳拦截规则", "a2a2a2a2", 90, 2, "横跳拦截"),
    (27, "093000", "EDC 系统选型", "a3a3a3a3", 100, 3, "EDC 选型"),
    (24, "160000", "新患者注册五个问题前置", "a4a4a4a4", 80, 2, "注册流程"),
    (22, "110000", "恒瑞黑卡亲友积分", "b1b1b1b1", 110, 2, "黑卡积分"),
    (20, "150000", "黑卡分享注销口径", "b2b2b2b2", 95, 3, "分享注销"),
    (17, "103000", "恒瑞健康商城后台字段", "b3b3b3b3", 130, 4, "后台字段"),
    (14, "190000", "云课堂直播运营问题对齐", "c1c1c1c1", 140, 3, "直播运营"),
    (12, "090000", "项目复制与名单一键转移", "c2c2c2c2", 85, 2, "名单转移"),
    (10, "140000", "口服药到店领取配置", "c3c3c3c3", 75, 2, "到店领取"),
    (7, "170000", "医生资质AI审核规则", "c4c4c4c4", 105, 3, "资质审核"),
    (5, "120000", '特殊字符 "引号" & <b>标签</b> 50%', "d1d1d1d1", 70, 2, "特殊字符"),
    (3, "100000", "周会 (第 39 周) - 进度*同步", "d2d2d2d2", 150, 4, "周会进度"),
    (2, "200000", "需求池改版验收", "d3d3d3d3", 115, 3, "需求池"),
]

PHRASES = [
    "这个{t}的事情我们今天先过一下",
    "入库单需要从系统里推过去，不然库房那边对不上",
    "我觉得这里还是要加一个审批节点",
    "上线时间先定在十月中旬，具体看测试进度",
    "导出 excel 这个功能现在是没有的",
    "那就先这样，会后我把接口清单发给大家",
    "积分一个抵一块钱，这个口径不变",
    "这块需要产品再确认一下{t}的边界",
    "测试那边说有八条用例没过",
    "我们下周三之前把方案定下来",
    "这个字段在后台是看不到的，需要加",
    "患者要手持身份证跟药盒拍照上传",
    "好的，那{t}这件事就交给你来跟",
    "注意冷链温度要求是二到八度",
    "这个先不做，放到二期",
]


def srt_time(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", *args], check=True)


def write_srt(path: Path, blocks: list[tuple[int, int, int, str]]) -> None:
    out = [
        f"{index}\n{srt_time(start)} --> {srt_time(end)}\nSPEAKER_{speaker:02d}: {text}\n"
        for index, (start, end, speaker, text) in enumerate(blocks, start=1)
    ]
    path.write_text("\n".join(out), encoding="utf-8")


def meeting_folder(days_ago: int, hhmmss: str, title: str, suffix: str) -> tuple[Path, str, str]:
    day = TODAY - timedelta(days=days_ago)
    meeting_id = f"vm-{day:%Y%m%d}-{hhmmss}-{suffix}"
    folder = ARCHIVE / f"{day:%y%m%d} {title}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder, meeting_id, day.isoformat()


def main() -> None:
    rng = random.Random(42)
    seeds: dict[str, dict] = {}
    for index, (days_ago, hhmmss, title, suffix, seconds, speakers, topic) in enumerate(MEETINGS):
        folder, meeting_id, day = meeting_folder(days_ago, hhmmss, title, suffix)
        audio = folder / f"{meeting_id}.m4a"
        if not audio.exists():
            freq = 220 + index * 37
            ffmpeg(
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                "-c:a", "aac", "-b:a", "48k", str(audio),
            )  # fmt: skip
        blocks = []
        start = 500
        n = 0
        while start < seconds * 1000 - 1500:
            n += 1
            length = rng.randint(1100, 2600)
            spk = rng.randrange(speakers)
            text = rng.choice(PHRASES).format(t=topic)
            if n % 9 == 0:
                text += f"，第{n}句补充说明一下{topic}的细节，避免后面又返工。"
            blocks.append((start, start + length, spk, text))
            start += length + rng.randint(50, 400)
        write_srt(folder / f"{meeting_id}.srt", blocks)
        (folder / "会议纪要.md").write_text(
            f"# {title}\n\n## 一分钟摘要\n\n本场围绕{topic}讨论了约 {seconds // 60} 分钟，"
            f"确定了上线时间与导出口径 [00:00:05]。\n\n## 决议\n\n"
            f"1. {topic}先按现有流程执行 [00:00:10]\n2. 导出 excel 放到二期 [00:00:20]\n\n"
            f"## 待办\n\n- 会后发出{topic}接口清单\n- 下周三前定方案\n",
            encoding="utf-8",
        )
        seeds[suffix] = {"id": meeting_id, "title": folder.name, "date": day}

    # 播放验证：180 秒 wav（Playwright 自带的 Chromium 不一定解得了 m4a 里的 AAC，wav 一定能解，currentTime 才稳妥地看得到往前走）。
    # 每 5 秒一段，01:00 正好是一段的开头；60 秒和 120 秒的段里带关键词「播放验证词」，检索得到。
    folder, meeting_id, day = meeting_folder(1, "110000", "播放验证", "e2e2e2e2")
    wav = folder / f"{meeting_id}.wav"
    if not wav.exists():
        ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=440:duration=180:sample_rate=8000",
            "-ac", "1", "-c:a", "pcm_s16le", str(wav),
        )  # fmt: skip
    blocks = []
    for index in range(36):
        start = index * 5000
        if start in (60_000, 120_000):
            text = f"这一句里有播放验证词，在 {start // 1000} 秒。"
        elif index == 0:
            text = "开场，先说两句。"
        else:
            text = f"第{index + 1}段：{PHRASES[index % len(PHRASES)]}。"
        blocks.append((start, start + 4800, index % 2, text))
    write_srt(folder / f"{meeting_id}.srt", blocks)
    (folder / "会议纪要.md").write_text(
        "# 播放验证\n\n## 一分钟摘要\n\n播放验证词在一分钟处 [00:01:00]。\n", encoding="utf-8"
    )
    seeds["e2e2e2e2"] = {"id": meeting_id, "title": folder.name, "date": day}

    # 纪要版本很多：30 秒 mp3 + 6 段逐字稿 + 第一版纪要，后面 19 版由 seed_db.py 补
    folder, meeting_id, day = meeting_folder(1, "120000", "纪要版本很多", "e3e3e3e3")
    mp3 = folder / f"{meeting_id}.mp3"
    if not mp3.exists():
        ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=550:duration=30:sample_rate=8000",
            "-ac", "1", "-c:a", "libmp3lame", "-b:a", "8k", str(mp3),
        )  # fmt: skip
    write_srt(
        folder / f"{meeting_id}.srt",
        [(i * 5000, i * 5000 + 4800, i % 2, f"第{i + 1}段：{PHRASES[i]}。") for i in range(6)],
    )
    (folder / "会议纪要.md").write_text(
        "# 纪要版本很多\n\n## 一分钟摘要\n\n第一版纪要。\n", encoding="utf-8"
    )
    seeds["e3e3e3e3"] = {"id": meeting_id, "title": folder.name, "date": day}

    (ROOT / "seed.json").write_text(
        json.dumps({"today": TODAY.isoformat(), "meetings": seeds}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {len(seeds)} meetings under {ARCHIVE}")


if __name__ == "__main__":
    main()
