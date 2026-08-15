# task-notify：飞书通知与任务确认闭环（参考实现）

1.1 起承载任务确认闭环：会议纪要生成后，本地 AI 提取任务草稿，推送飞书「会后任务确认」卡片，
用户在飞书里逐条点「确认 / 驳回」，回调经长连接回到本机，任务状态落库，卡片原地更新。

1.2 起补上通知的中间一环：纪要写好时先推一张「纪要写好」卡，把摘要和决议直接摊在群里。
一场会在群里的节奏是三条消息——转写完成、纪要写好、任务待确认。

本目录是**脱敏参考实现**，聚焦三个文件：

| 文件 | 作用 |
|---|---|
| [`cards.py`](cards.py) | 任务卡构造：每条任务灰底信息块（标题/建议/原话）+ 确认/驳回按钮，确认后重建状态卡 |
| [`minutes_card.py`](minutes_card.py) | 纪要写好卡构造：把纪要压成摘要 + 决议 + 下一步，抽不到就留空 |
| [`card_listener.py`](card_listener.py) | 长连接回调监听：收 card.action → 校验操作者 → 落库 → 原地刷新卡片 |

完整逻辑与设计决策见 [../docs/task-notification-push.md](../docs/task-notification-push.md) 与
[../docs/notification-triple-touch.md](../docs/notification-triple-touch.md)。

## 运行

```bash
# 1. 在飞书开放平台配置应用机器人：开启回调=长连接，订阅 card.action.trigger
# 2. 把应用机器人加进目标群，配好 LARK_CHAT_ID / OWNER_OPEN_ID / WORKBENCH_BASE
# 3. 建议在 GUI 会话的 tmux 里运行（lark-cli 凭证走 keychain）
python3 card_listener.py
```

## 数据边界

外发的只是任务摘要（标题、来源会议名、一段原话）与确认按钮；会议全文留本机。
回调走长连接，不开放任何公网端口。
