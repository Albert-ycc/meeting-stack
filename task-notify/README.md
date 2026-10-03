# task-notify：飞书通知与任务确认闭环（参考实现）

1.1 起承载任务确认闭环：会议纪要生成后，本地 AI 提取任务草稿，推送飞书「会后任务确认」卡片，
用户在飞书里逐条点「确认 / 驳回」，回调经长连接回到本机，任务状态落库，卡片原地更新。

1.2 起补上通知的中间一环：纪要写好时先推一张「纪要写好」卡，把摘要和决议直接摊在群里。
一场会在群里的节奏是三条消息——转写完成、纪要写好、任务待确认。

本目录是**脱敏参考实现**，聚焦两个文件：

| 文件 | 作用 |
|---|---|
| [`cards.py`](cards.py) | 任务卡构造：每条任务灰底信息块（标题/建议/原话）+ 确认/驳回按钮，确认后重建状态卡 |
| [`minutes_card.py`](minutes_card.py) | 纪要写好卡构造：把纪要压成摘要 + 决议 + 下一步，抽不到就留空 |

长连接回调监听不在这里，它是工作台的一部分：实际跑的是
[`../workbench/card_listener.py`](../workbench/card_listener.py)（收 card.action → 校验操作者 → 落库 → 原地刷新卡片）。

完整逻辑与设计决策见 [../docs/task-notification-push.md](../docs/task-notification-push.md) 与
[../docs/notification-triple-touch.md](../docs/notification-triple-touch.md)。

## 运行回调监听

```bash
# 1. 在飞书开放平台配置应用机器人：开启回调=长连接，订阅 card.action.trigger
# 2. 把应用机器人加进目标群，在 workbench/.env 里配好
#    MEETING_WORKBENCH_LARK_CHAT_ID（目标群）和 MEETING_WORKBENCH_LARK_OWNER_OPEN_ID（只认这个人点的按钮）
# 3. 在 GUI 会话的 tmux 里运行（lark-cli 凭证走 keychain），用工作台的 venv 起
workbench/.venv/bin/python workbench/card_listener.py
```

这两项缺一项，监听进程就说明白缺什么然后退出（退出码 78），不会带着空配置跑起来。
配置的读法、日志位置和轮转见 [../workbench/README.md](../workbench/README.md) 的「卡片回调监听」一节。

## 数据边界

外发的只是任务摘要（标题、来源会议名、一段原话）与确认按钮；会议全文留本机。
回调走长连接，不开放任何公网端口。
