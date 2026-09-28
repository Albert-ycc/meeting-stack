# 备份瘦身：剥离可从源数据重建的衍生表（1.2）

## 要解决的问题

工作台的 SQLite 主库越滚越大，其中最大的一块是**语义索引的向量表**（embeddings）——它占了主库
约七成体积。而备份机制是每天一份、按 retention 份数轮转，等于同一批向量被原样复制了 retention 份。

实测：单份备份 407M，其中 embeddings 独占 283M；剥掉之后降到 118M，本地与外置盘镜像各省下一大截。

问题不在于「向量表大」，而在于**它在备份里是冗余的**：向量是正文的分词结果，随时能从留存数据重新算出来。
备份里留着它，只是把「能重建的东西」复制了很多份。

## 方案：派生表白名单剥离

备份时不再原样整库拷贝，而是拷完后清掉一类表——**能从留存数据重建的派生表**，再 `VACUUM` 回收页面。

```
主库（含 embeddings）
  → sqlite online backup 到临时文件
  → 清空 DERIVED_TABLES 白名单里的表（DELETE + VACUUM）
  → integrity_check 通过 → 原子替换为正式备份
  → 本地 + 外置盘镜像 各留一份
```

实现上就是一个白名单常量，加表只需往列表里加一个名字：

```python
# 可从 segments 重算的派生数据，备份时清空。
DERIVED_TABLES: tuple[str, ...] = ("embeddings",)
```

## 安全判据：什么表能剥、什么表不能

判断一张表能不能进白名单，只看一条——**它的重建入口是否在服务启动后自动跑**。

- embeddings 的重建挂在服务后台的扫描循环里：用 `LEFT JOIN embeddings … WHERE e.segment_id IS NULL`
  做增量重算，空表即全量重算，恢复后无需人工干预。
- 所以「备份里没有 embeddings」这个状态，服务一启动就会自己补齐，语义检索会有一段空窗（全文检索不受影响），
  之后自动恢复。

反过来，**能重算但需要人工手动触发的表不算**。那种表剥了之后，恢复会变成一个带隐藏步骤的操作——
谁恢复谁踩坑。白名单只收「重启就能自动补齐」的表。

## 失败降级：宁可备份大，不可备份没做成

剥离发生在备份链路里，任何一步失败都不能拖垮备份本身：

- 清空表失败（`DELETE` 报错）→ 回滚，**保留完整副本**，返回「未剥离」。
- `VACUUM` 失败 → 不抛错，只是没回收到页面，备份内容已经是干净的。

原则是：剥得掉是省空间，剥不掉也得把备份做成。**省空间是加分项，备份成不成功是底线。** 两者冲突时
保底线。

## 回执留痕

每份备份的 receipt 里记录 `derived_stripped` 与 `derived_tables` 两个字段，一眼能看出这份备份
剥了哪些表、是不是完整副本。出问题排查时先看这个，再决定要不要怀疑是剥离导致的差异。
`derived_tables` 只列这份备份里实际清空了的表（副本里没有的表不列）。

## 项目材料的全文表和向量（第三期）

schema v15 起白名单多了两张表：

```python
DERIVED_TABLES: tuple[str, ...] = ("embeddings", "material_chunks_fts", "material_chunk_vectors")
```

- `material_chunk_vectors`：材料片段的向量，和 embeddings 一样由后台的向量循环按「还没有向量的片段」增量补，
  空表就全量补。补完之前「意思相近的」材料结果少一些。
- `material_chunks_fts`：材料片段的全文表，是外部内容表（内容在 `material_chunks` 里）。这张表不能用
  `DELETE` 清（实测备份反而变大），改用 `INSERT INTO material_chunks_fts(material_chunks_fts) VALUES('delete-all')`；
  同一个事务里在副本的 `app_state` 写 `material_fts_rebuild`。用这份备份恢复后，服务启动时的后台任务看到
  这个键就分批把全文表补回来（断点续补，补完跑 integrity-check 再删键），补完之前读内容、转写的循环不写不删片段。
  副本里没有这张表时不写这个键。

材料读出的文字本身（`material_contents`、`material_chunks`）和转写断点（`material_media_jobs`）**不剥**：
它们能重算，但要重新认字、重新转写几个小时，还要资料盘插着，不符合「重启就能自动补齐」这条判据。

## 深度关联（第四期）

schema v16 起白名单再多三张表，都用普通 `DELETE` 清：

```python
DERIVED_TABLES: tuple[str, ...] = (
    "embeddings", "material_chunks_fts", "material_chunk_vectors",
    "meeting_windows", "meeting_window_passages", "meeting_related_scan",
)
```

- `meeting_windows`：逐字稿每个 90 秒窗口的向量（约 40MB），由关联整理的后台循环从逐字稿重算。
- `meeting_window_passages`：每个窗口对得上的材料段落，从窗口向量和材料片段重算。
- `meeting_related_scan`：「相关」的台账。和上面两张一起清空，恢复后每场会都算「该算了」，后台循环自己补。
- 同一个事务里删掉 `app_state` 的 `related_chunk_mark`（「相关」已经看过的最大材料向量 id）：材料向量也被清掉了，
  要边补边算。恢复后第一次算「相关」时按那时已有的最大向量 id 重新记这个键，之后补回来的向量 id 更大，
  走增量补上。副本里没有这三张表时不删这个键。

第四期别的新表都**不剥**：`relations` 和 `glossary_candidates` 里有你的回答；`decisions`、`decision_scan`
是决议的历史和归需求的决定；`mention_extractions` 是花钱调 AI 得来的；`material_file_events` 是过去的文件动静，
没法重建；`glossary_mining_scan`、`glossary_mining_seeds` 很小，清掉的话每次恢复都要全部重挖。
