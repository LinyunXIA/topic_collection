# CLI 手册 — feedkicker 9 命令详解

本手册覆盖 `feedkicker` 全部 **9 个命令**：5 个 entry point（`tc-push` / `tc-salon` / `tc-purge` / `tc-extract` / `tc-score`）+ 4 个 `python -m` 模块（`feedkicker.wiki_home` / `.bitable` / `.wiki` / `.topic`）。每命令给出用途、参数表、dev/test/prod 环境差异、三环境示例（示意输出 + 退出码 + 副作用）、`--dry-run` 输出与注意事项。

所有 flag / 默认值 / `choices` / 退出码均取自源码 argparse 与各命令 `--help` 实跑；输出片段**示意化**（结构真实，值脱敏或截断），凭据一律占位符（`<webhook>`/`<salon-app-token>`/`<space-id>`…）。**prod 示例一律 `--dry-run`**，真跑命令单列并标 ⚠️。

## 目录

- [通用约定](#通用约定)
- [tc-push](#tc-push) — 抓取 → 归档 → 推飞书摘要卡
- [tc-salon](#tc-salon) — 已选题 → 双大纲 → Wiki 归档
- [tc-purge](#tc-purge) — 365 天滚动保留清理
- [tc-extract](#tc-extract) — 近 N 天资讯 → LLM 提炼选题 → salon 表
- [tc-score](#tc-score) — 沙龙话题清单自动打分（六维加权）
- [feedkicker.wiki_home](#wiki_home) — 重建 Wiki 首页索引
- [feedkicker.bitable](#bitable) — 多维表格归档运维
- [feedkicker.wiki](#wiki) — 单篇 Wiki docx 创建（联调）
- [feedkicker.topic](#topic) — 已选题分页拉取（只读）
- [附录：错误码对照](#附录错误码对照)

---

<a id="通用约定"></a>

## 通用约定

**环境解析与覆盖顺序**（`feedkicker/config.py` `load_config`）：

```
--db  >  TC_DB  >  --env  >  TC_APP_ENV  >  prod（默认）
```

- `--env {dev,test,prod}`：决定默认配置文件 `config-{env}.yaml` 与 db 路径（覆盖环境变量 `TC_APP_ENV`）。
- `--config <path>`：显式指定配置文件路径，覆盖 `--env` 推导出的默认文件。
- `--db <path>`：显式指定 sqlite 路径，优先级最高（覆盖 `TC_DB` 与 `--env` 推导）。
- 默认 db 路径 = `data/tc-{env}.sqlite3`；默认配置 = `<repo>/config-{env}.yaml`（锚定仓库根，不随 cwd 漂移）。
- 未给任何环境参数时回落到 **prod**。

**支持 `--env` 的命令**：全部 9 个。**支持 `--config`/`--db` 的命令**：`tc-push`、`tc-salon`、`tc-purge`、`tc-extract`、`tc-score`、`feedkicker.wiki_home`、`feedkicker.bitable`（`feedkicker.wiki` 与 `feedkicker.topic` 无此二参数，靠 `--env` 或直接传 token）。

**命令形态约定**：所有命令均支持 `-h/--help`。5 个 entry point 由 `.venv/bin/tc-*` 调用（等价 `.venv/bin/python -m feedkicker.push|salon_flow|purge|extract_flow|score_flow`）；4 个模块用 `.venv/bin/python -m feedkicker.<name>`。

**脱敏规则**：真实 webhook / 签名 secret / app_token / table_id / space_id / node_token 一律写占位符，如 `<webhook>`、`<salon-app-token>`、`<wiki-space-id>`、`<node_token>`；时间戳与计数保留真实结构。文档不落任何真实凭据。

**prod 纪律**：prod 库默认禁写。本文中 prod 示例**一律 `--dry-run`**；需要真实写入的 prod 命令在各自小节「⚠️ prod 真跑」单列，执行前须人工确认。

---

<a id="tc-push"></a>

## tc-push

**用途**：抓取 RSS 订阅源 → 写入多维表格归档 → 推送飞书摘要卡；推送成功后才 `mark_pushed`。定时每日 8:30 / 16:00 由 launchd 拉起。对应 DESIGN §6（核心流程）、§7（卡片）、§8（发送）、§16（归档）、§9（调度）。

**入口**：`tc-push = feedkicker.push:main`。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--dry-run` | store_true | 关 | — | 抓取/写库照常（`download`/首跑标记），仅打印 payload 不发送、不归档 |
| `--config` | str | `None` | 覆盖 `--env` 推导的默认配置 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖 `TC_DB` 与 `--env` 推导（最高） | sqlite 路径 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 决定默认配置与 db 路径 |

### 环境差异

| 环境 | 配置 | db | 凭据 / 归档 |
|---|---|---|---|
| dev | `config-dev.yaml` | `data/tc-dev.sqlite3` | webhook/secret 为占位或留空（不推真实群）；bitable 与 dev/test 共享 Base，用「环境」列区分 |
| test | `config-test.yaml` | `data/tc-test.sqlite3` | 同上；用于联调真实归档与卡片 |
| prod | `config-prod.yaml` | `data/tc-prod.sqlite3` | 真实 webhook/secret；独立 prod Base；完整源清单、`bootstrap_days=3`（下限 1，上限 3650） |

### 示例

**dev**：

```bash
.venv/bin/tc-push --env dev --dry-run
```

示意输出（「示意」）：

```
2026-09-14 ... feedkicker.push 运行开始：环境=dev，db=/.../data/tc-dev.sqlite3
{
  "msg_type": "interactive",
  "card": {"header": {"title": {"tag": "plain_text", "content": "Feeds 汇总  10:00"}}, "elements": [...]}
}
2026-09-14 ... feedkicker.push dry-run：共 3 条待推，已打印 payload 未发送
```

退出码：`0`。副作用：RSS 抓取与 sqlite 写库照常；不发送、不写多维表格。

**test**（真实发送到测试群，需 test 配置）：

```bash
.venv/bin/tc-push --env test
```

示意输出：`运行开始：环境=test` → `多维表格已写入 N 条` → `推送成功：N 条新条目，失败源 M 个`。退出码：`0`（发送失败 `1`，配置错误 `2`）。副作用：sqlite 入库 + 多维表格归档 + 飞书发送 + `mark_pushed`。

**prod（仅 dry-run）**：

```bash
.venv/bin/tc-push --env prod --dry-run
```

退出码 `0`；只打印 payload、不发送不归档（sqlite 写库照常）。

⚠️ **prod 真跑**（影响线上群与 prod Base，人工执行）：

```bash
.venv/bin/tc-push --env prod
```

### `--dry-run` 示意输出

见上 dev 段：打印完整卡片 JSON（`msg_type: interactive`），末尾日志 `dry-run：共 N 条待推，已打印 payload 未发送`。

### 退出码

`0` = 无待推 / dry-run / 推送成功；`1` = 未捕获异常或发送失败（`run()` 返回 `0 if ok else 1`）；`2` = 配置加载失败。

### 注意 / 坑

- `--dry-run` **不是纯只读**：RSS 抓取与 sqlite 写库（首跑标记）照常，仅跳过发送与多维表格归档。
- 多维表格同步失败只 WARNING，卡片照发；失败批次保留待重试。
- 发送失败不 `mark_pushed`，下轮重发；带按钮卡片失败会去掉按钮降级重试一次。
- 连续 ≥3 次发送失败会发一条纯文本 SOS（不影响返回码）。
- 非 prod 环境若配置了非空 webhook，会打一条「将向真实群发送」WARNING。

---

<a id="tc-salon"></a>

## tc-salon

**用途**：读取「已选题」话题 → MiniMax 生成工具类 / 原理类双大纲 → 写飞书 Wiki docx → 推通知卡 → 重建 Wiki 首页。定时周五 10:00 由 launchd 拉起。对应 DESIGN §19（salon 全链路）、§22（Wiki 首页）、§21。

**入口**：`tc-salon = feedkicker.salon_flow:main`（`--help` 的 `usage` 显示 `feedkicker.salon_flow`）。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--dry-run` | store_true | 关 | — | 仅预览：不写库不落 Wiki；无 salon token 时打印 stub 数据 |
| `--config` | str | `None` | 覆盖 `--env` 推导 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖 `TC_DB` 与 `--env` 推导 | sqlite 路径 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 决定默认配置与 db 路径 |

### 环境差异

| 环境 | 配置 / db | 凭据 / 资源 |
|---|---|---|
| dev | `config-dev.yaml` / `data/tc-dev.sqlite3` | MiniMax key（`MiniMax_Key` env）可占位；salon app_token/table_id 缺失或含 `<` 占位时 dry-run 走 stub；Wiki 若未配置则跳过写入 |
| test | `config-test.yaml` / `data/tc-test.sqlite3` | 联调真实选题 Base 与 Wiki（谨慎，会产物） |
| prod | `config-prod.yaml` / `data/tc-prod.sqlite3` | 真实 salon 选题 Base、真实 MiniMax key、真实 Wiki space/parent |

### 示例

**dev**：

```bash
.venv/bin/tc-salon --env dev --dry-run
```

示意输出（「示意」）：

```
{ "tool_outline": {"title": "示例已选题话题", "slides": [...]}, "principle_outline": {...} }
# 示例已选题话题
...
https://<host>/wiki/wiki_dry_示例已选题话题
2026-09-14 ... feedkicker.salon_flow dry-run 完成，待处理 1 条，Wiki 预览 1 个
```

退出码：`0`。副作用：无（不写库、不落 Wiki、不发卡）。

**test**：

```bash
.venv/bin/tc-salon --env test
```

示意输出：`topic <record_id> Wiki 已创建: https://<host>/wiki/<node_token>` → `本轮成功 N 条` → `Wiki 首页已重建：N 篇大纲索引`。退出码 `0`（卡片发送失败 `1`，配置错误 `2`）。副作用：Wiki 新建 docx、sqlite 记录 `mark_topic_archived`、发通知卡、首页重建。

**prod（仅 dry-run）**：

```bash
.venv/bin/tc-salon --env prod --dry-run
```

⚠️ **prod 真跑**（会新建真实 Wiki 文档并发卡，人工执行）：

```bash
.venv/bin/tc-salon --env prod
```

### `--dry-run` 示意输出

打印 `{"tool_outline": ..., "principle_outline": ...}` 与合并 MD 前 3000 字符；Wiki 创建走 stub 返回 `wiki_dry_<话题>`；末尾若命中则打印 `{"wiki_urls": [...]}`。

### 退出码

`0` = 无已选题 / dry-run / 通知成功；`1` = 未捕获异常、通知卡发送失败、或全部话题失败（0 条成功建 Wiki，或 Wiki 建成而归档状态未落库）；`2` = 配置加载失败。

### 注意 / 坑

- 无 salon `app_token`/`table_id` 且**非** dry-run 时会 WARNING 后跳过（返回 `0`）。
- wiki space/parent（`wiki.space_id`/`parent_token`，回退 `salon.wiki_*`）为空或含 `<` 占位时，**非 dry-run 直接 WARNING 跳过建 Wiki 与标记、rc `0`**（不产生孤儿 docx；dry-run 仍走 stub 预览）。
- 单条话题大纲生成 / Wiki 写入失败只 WARNING，不阻断其余话题；全部失败（0 条成功建 Wiki）或 Wiki 建成但归档状态未落库时 rc `1`（#R9-27/#R10-30）。
- Wiki 首页重建失败只 WARNING，不影响返回码、不触发 SOS。
- 去重靠 `ppt_synced_at` + `ppt_last_status_{rid}` 翻转检测，已是「已选题」且已同步的会被跳过。
- dry-run 也调用首页更新（传入 `dry_run=True`，只打印预览）。

---

<a id="tc-purge"></a>

## tc-purge

**用途**：365 天滚动保留，清理超过保留期的 sqlite 与多维表格记录。**默认 dry-run**，`--apply` 才真删。launchd 每月 1 号 10:30 只跑 dry-run 巡检。对应 DESIGN §20。

**入口**：`tc-purge = feedkicker.purge:main`。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--apply` | store_true | 关（即默认 dry-run） | — | 真删；缺省仅 dry-run 巡检 |
| `--retention-days` | int | `None`（取 `config.bitable.retention_days=365`） | 覆盖配置值（`<1` 静默钳为 1；仅 `>36500` 报 rc 2） | 保留天数 |
| `--config` | str | `None` | 覆盖 `--env` 推导 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖 `TC_DB` 与 `--env` 推导 | sqlite 路径 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 决定默认配置与 db 路径 |

### 环境差异

| 环境 | 配置 / db | 归档 |
|---|---|---|
| dev | `config-dev.yaml` / `data/tc-dev.sqlite3` | bitable 与 test 共享 Base（「环境」列区分），仅删「环境」= `dev` 的超期行 |
| test | `config-test.yaml` / `data/tc-test.sqlite3` | 同上，仅删「环境」= `test` 的超期行 |
| prod | `config-prod.yaml` / `data/tc-prod.sqlite3` | 独立 prod Base；真实清理影响档案 |

### 示例

**dev**（默认 dry-run，无需 `--dry-run` flag）：

```bash
.venv/bin/tc-purge --env dev
```

示意输出（「示意」，`PurgeStats` JSON）：

```json
{
  "dry_run": true,
  "retention_days": 365,
  "cutoff_iso": "2025-09-13T16:00:00Z",
  "cutoff_date_shanghai": "2025-09-14",
  "sqlite_deleted": 0,
  "sqlite_expired_unarchived": 0,
  "sqlite_expired_archivable": 0,
  "bitable_scanned": 120,
  "bitable_expired": 0,
  "bitable_deleted": 0,
  "bitable_skipped_reason": ""
}
```

`sqlite_expired_archivable`：超期且已归档、本可删除的行数（dry-run 下仍计数，供巡检可见，#278）。

退出码：`0`。副作用：无（只计数不删）。

**test**：`--apply` 在本环境真删（须确认）：

```bash
.venv/bin/tc-purge --env test --apply --retention-days 365
```

示意输出：`sqlite_deleted` / `bitable_deleted` 为真实删除数；真删且 bitable 段整段成功时写 meta `purge_last_run_at`。退出码 `0`（异常 `1`，配置错误 `2`）。

**prod（仅巡检，默认 dry-run）**：

```bash
.venv/bin/tc-purge --env prod
```

⚠️ **prod 真删**（不可逆，必须人工确认后执行）：

```bash
.venv/bin/tc-purge --env prod --apply
```

### `--dry-run` 示意输出

即上 dev 段 `PurgeStats` JSON，`"dry_run": true`，`*_deleted` 均为 `0`，`bitable_expired`/`bitable_scanned` 反映真实扫描计数。

### 退出码

`0` = 正常（含 bitable 段被跳过）；`1` = 未捕获异常；`2` = 配置加载失败。

### 注意 / 坑

- **默认就是 dry-run**：真要删必须显式 `--apply`；调度只做巡检。
- sqlite 仅删 `pushed_at` 超期且 `bitable_synced_at` 非空（已归档）的行；超期未归档只计数 WARNING。
- 占位 token（含 `<`）或 bitable 未启用时跳过 bitable 段，原因写入 `bitable_skipped_reason`。
- dev/test 共享 Base：bitable 段仅删「环境」列等于当前 env 的过期行；prod 全表不过滤。存量无「环境」列的行不会被 dev/test 删除（保守方向）。
- 绝不调 `ensure_initialized`，不误建 Base；只操作资讯归档 Base，不碰 salon 选题 Base。
- 截止用上海日界，截止当天记录保留（保守方向）。

---

<a id="tc-extract"></a>

## tc-extract

**用途**：把最近 N 天（默认 7）的 RSS 资讯分批交给 LLM **先整合去重、再提炼**候选话题，写入 salon「沙龙话题清单」。**默认 dry-run 打印完整待写清单**，`--apply` 才写表（落实提示词「写前确认」）。对应 DESIGN §25。

**入口**：`tc-extract = feedkicker.extract_flow:main`。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--apply` | store_true | 关（即默认 dry-run） | 与 `--dry-run` 互斥 | 执行多维表格写入 |
| `--dry-run` | store_true | 关（默认行为） | 与 `--apply` 互斥 | 仅打印待写清单，零写调用 |
| `--since-days` | 正整数 | `None`（取 `extract.since_days=7`） | 覆盖配置 | 时间窗天数（边界含当天；取值 1..3650，#269） |
| `--limit` | 正整数 | `None`（不限） | — | 最多处理的 RSS 行数 |
| `--batch-size` | 正整数 | `None`（取 `extract.batch_size=30`） | 覆盖配置 | 每批条数（每批一次 LLM 调用；取值 1..200，越界 rc 2，#335） |
| `--max-calls` | 非负整数 | `None`（取 `extract.max_calls=0`） | 覆盖配置 | LLM 调用上限，`0`=不限；达限停止剩余批 |
| `--provider` | choice `{deepseek,minimax}`（`choices=sorted(PROVIDERS)`，与 `tc-extract --help` 一致；由 provider 注册表键动态生成） | `None`（取 `extract.provider`，默认 `minimax`） | 覆盖配置 | 本次运行使用的 LLM provider；`提取工具` 随之为该 provider 的 tool_label（minimax→`MMax`，deepseek→`DS`） |
| `--config` | str | `None` | 覆盖 `--env` 推导 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖 `TC_DB` 与 `--env` 推导 | sqlite 路径 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 决定默认配置与 db 路径 |

### 环境差异

| 环境 | 配置 / db | 写入目标 |
|---|---|---|
| dev | `config-dev.yaml` / `data/tc-dev.sqlite3` | `cfg.salon.app_token/table_id`（dev/test 常共享同一 salon Base，按配置而定） |
| test | `config-test.yaml` / `data/tc-test.sqlite3` | 同上 |
| prod | `config-prod.yaml` / `data/tc-prod.sqlite3` | 真实 prod salon 选题表 |

provider key：`extract.providers.<name>.api_key`，为空或占位时按 provider 取 env（`MiniMax_Key`/`MINIMAX_API_KEY`、`DEEPSEEK_API_KEY`）；占位值（`<` 开头）清空。`--provider` 可单次切换 provider（缺省取 `extract.provider`，默认 `minimax`）；未知 provider 或所选 provider 缺 key → rc 2 且不发起 HTTP 调用。提示词文件默认 `prompts/extract.md`（仓库根相对）。

### 示例

**dev**（默认 dry-run，不写表）：

```bash
.venv/bin/tc-extract --env dev
```

本次切 DeepSeek（key 走 `DEEPSEEK_API_KEY` 或 `extract.providers.deepseek.api_key`；`提取工具=DS`）：

```bash
.venv/bin/tc-extract --env dev --provider deepseek
```

示意输出（「示意」）：

```
2026-09-14 ... feedkicker.extract_flow tc-extract 运行开始：环境=dev，db=/.../data/tc-dev.sqlite3，mode=dry-run
2026-09-14 ... feedkicker.extract_flow 近 7 天 RSS 行 12 条（limit=None）→ 批大小 30，provider=minimax（提取工具=MMax）
2026-09-14 ... feedkicker.extract_flow 第 1/1 批提炼 2 个话题
待写选题 2 个（dry-run，未写表；目标表已存在跳过 0 个）：
[将写入] 1. 话题名A
{"话题名称": "话题名A", "可使用工具": "…", "相关AI原理": "…", "资讯链接": "https://…", "出处来源": "量子位", "提炼日期": "2026-09-14", "讨论状态": ["未讨论"], "提取工具": ["MMax"]}
{"mode": "dry-run", "since_days": 7, "batches": 1, "llm_calls": 1, "topics": 2, "written": 0, "pending": 2, "skipped": 0, "failed_writes": 0, "failed_batches": 0, "empty_batches": 0}
```

退出码 `0`。副作用：读 sqlite 与 salon 表（只读 `+record-list` 去重查询），不写表。

**test**（真写测试 Base，须确认）：

```bash
.venv/bin/tc-extract --env test --apply
```

如需本次走 DeepSeek：`.venv/bin/tc-extract --env test --apply --provider deepseek`（`提取工具=DS`）。

示意输出：`提炼完成：… 写入=N 待写=0 跳过=M 失败批=0` 与统计 JSON（`"mode": "apply"`）。退出码 `0`（部分批失败/部分写入失败仅汇总 WARNING，rc 不变）；`--apply` **全部写入失败**（`failed_writes>0 且 written==0`）→ `1`；配置错 `2`，其它异常 `1`。副作用：写 salon 选题表（≤200/批，`讨论状态=未讨论`、`提取工具`=所选 provider 的 tool_label）。

**prod（仅 dry-run）**：

```bash
.venv/bin/tc-extract --env prod
```

⚠️ **prod 真跑**（写线上 salon 选题表，人工确认后执行）：

```bash
.venv/bin/tc-extract --env prod --apply
```

### `--dry-run` 示意输出

见上 dev 段：逐条打印完整待写记录（人读行 + JSON 行），末尾一行统计 JSON，**零写调用**（仅对 salon 表做只读去重查询）。

### 退出码

`0` = 正常（含部分批失败跳过，结束汇总 WARNING）；`1` = 未捕获异常，或 `--apply` 全部写入失败（`failed_writes>0` 且 `written==0`，#358；部分写入失败仍 rc 0）；`2` = 配置错（config 加载失败 / `prompts/extract.md` 缺失 / `--provider` 未知（argparse choices 拒绝）/ provider 未注册 / 所选 provider 缺 key / salon token 缺失或占位 / 参数非法 / 同时给 `--apply` 与 `--dry-run`）。

### 注意 / 坑

- **默认 dry-run**：真实写入必须显式 `--apply`；重复运行按 **资讯链接 OR 话题名称** 双键去重跳过（幂等）——话题名经 NFKC 归一；链接归一 = **去 markdown 包裹 + 拆行 + 去 tracking 参数**（表内值常是 markdown 包裹且带 `utm_*` 的换行拼接串，故先取 markdown 链接目标（target）URL/按行拆，再 `canonicalize` 去 fragment/host 大小写后剥 tracking 参数，有意义 query 按名排序保留）；仅两者皆无命中才写。链接键兜底 LLM 命名非确定性（同一新闻重跑可能得到不同话题名）。
- `讨论状态` / `提取工具` 均为单选 select，按 lark-cli select CellValue 协议**一律写单元素数组**：`["未讨论"]` / `["MMax"]`（select CellValue 恒为数组，`multiple=false` 时也须数组；写字符串会被服务端拒）；取值须为**表内已有选项**（`讨论状态`：`未讨论`/`已选题`/`不选择`/`待继续评估`；`提取工具`：`MMax`/`DS`），写表外新值被拒 `800030005 Provide an existing option value`；不再读字段元数据判形态。
- 时间窗 `COALESCE(published_at, first_seen) >= now − N 天`，边界含当天；仅 RSS 行（salon 占位行 `ppt_synced_at` 非空被排除）。
- 单批 LLM 调用失败（超时/429/529/业务可重试码）重试 1 次（总 HTTP ≤2/批，单层重试）后跳过并汇总 WARNING，不阻断其余批；模型合法返回空话题列表计 `empty_batches` 不计失败，单条非法 topic 丢弃该条不丢整批；`--max-calls` 供联调限次。
- 行为由 `prompts/extract.md` 定义（用户提示词原文 + 输出 JSON schema），改提示词即改提炼口径。
- 绝不调 `ensure_initialized`，不改 salon 表结构。

---

<a id="tc-score"></a>

## tc-score

**用途**：对 salon「沙龙话题清单」**全表行**逐条自动打分——六维各 0–5（0.5 档）→ 加权总分 0–5（1 位小数）→ 写回 `MMax打分`/`MMax理由`（`--provider deepseek` 时写 `DS打分`/`DS理由`）两列，供人工复核与横向排序。**默认 dry-run**，`--apply` 才写表；**默认只补空**（`打分` 列非空的行跳过），`--force` 覆盖重算。对应 DESIGN §26。

**入口**：`tc-score = feedkicker.score_flow:main`。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--apply` | store_true | 关（即默认 dry-run） | 与 `--dry-run` 互斥 | 写回目标 provider 的 `打分`/`理由` 两列 |
| `--dry-run` | store_true | 关（默认行为） | 与 `--apply` 互斥 | 仅打印待写清单与统计，零写调用 |
| `--provider` | choice `{deepseek,minimax}`（由列映射表键生成） | `None`（取 `score.provider`，默认 `minimax`） | 覆盖配置 | 调用方与目标列：minimax→`MMax打分`/`MMax理由`，deepseek→`DS打分`/`DS理由` |
| `--limit` | 非负整数 | `0`（全部） | — | 最多处理行数；`0`=全表（无对应配置项，直接生效） |
| `--max-calls` | 非负整数 | `0`（argparse 实默；省略与显式 `0` 等价，均回落配置） | 非 0 时覆盖配置 | LLM 调用上限；省略/显式 0 → 取 `score.max_calls`（配置 0=不限），达限停止剩余批并 WARNING。对照 `tc-extract` 同 flag 实默 `None`（见上节） |
| `--force` | store_true | 关 | — | 忽略既有打分，对全部命中行重算并覆盖 |
| `--config` | str | `None` | 覆盖 `--env` 推导 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖 `TC_DB` 与 `--env` 推导 | sqlite 路径 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 决定默认配置与 db 路径 |

### 环境差异

| 环境 | 配置 / db | 目标表 |
|---|---|---|
| dev | `config-dev.yaml` / `data/tc-dev.sqlite3` | `cfg.salon.app_token/table_id`（dev/test 常共享同一 salon Base） |
| test | `config-test.yaml` / `data/tc-test.sqlite3` | 同上 |
| prod | `config-prod.yaml` / `data/tc-prod.sqlite3` | 真实 prod salon 话题清单 |

provider key：`score.providers.<name>.api_key`，为空或占位时按 provider 取 env（`MiniMax_Key`/`MINIMAX_API_KEY`、`DEEPSEEK_API_KEY`）；占位值（`<` 开头）清空。未知 provider 或所选 provider 缺 key → rc 2 且**不发起任何 lark/LLM 调用**。提示词文件默认 `prompts/score.md`（仓库根相对）；目标 provider 对应两列任一不在表内 → rc 2（先 `+field-list` 校验，**零表数据读取、不自动建列**）。

### 示例

**dev**（默认 dry-run，不写表）：

```bash
.venv/bin/tc-score --env dev
```

本次切 DeepSeek（key 走 `DEEPSEEK_API_KEY` 或 `score.providers.deepseek.api_key`）：

```bash
.venv/bin/tc-score --env dev --provider deepseek
```

示意输出（「示意」）：

```
2026-09-15 ... feedkicker.score_flow tc-score 运行开始：环境=dev，db=/.../data/tc-dev.sqlite3，mode=dry-run
2026-09-15 ... feedkicker.score_flow tc-score 运行计划：环境=dev db=... 目标表=tbl… 模板=prompts/score.md provider=minimax（MMax）总行数=85 批数=5 每批行数=[20, 20, 20, 20, 5] 待打分=85 跳过=0 横向上文=0 max_calls=0 force=False
tc-score dry-run 计划：总行数=85 批数=5 待打分=85 跳过=0
[将写入] 1. 话题名A → 3.5 ｜ 依据 可使用工具… ｜ risk=false ｜ source=false ｜ 六维：…
待写 85 / 跳过 0 / 分布校验 ok
{"mode": "dry-run", "batches": 5, "llm_calls": 5, "rows": 85, "scored": 85, "skipped": 0, "written": 0, "failed_writes": 0, "failed_batches": 0, "dropped": 0, "empty_batches": 0}
```

退出码 `0`。副作用：只读 salon 表（`+record-list`/`+field-list`）并调用 LLM，**零写调用**。

**test**（真写测试 Base，须确认）：

```bash
.venv/bin/tc-score --env test --apply
```

如需覆盖既有分：`.venv/bin/tc-score --env test --apply --force`。

示意输出：`打分完成：模式=apply 批=5 调用=5 行=85 打分=85 跳过=0 写入=85 写入失败=0 失败批=0 丢弃=0 空批=0` 与统计 JSON（`"mode": "apply"`）。退出码 `0`（部分行/批失败仅汇总 WARNING）；`--apply` **全部写入失败**（`failed_writes>0 且 written==0`）→ `1`；配置错 `2`。副作用：写目标 provider 的 2 列（`base +record-batch-update`，≤100/批）。

**prod（仅 dry-run）**：

```bash
.venv/bin/tc-score --env prod
```

⚠️ **prod 真跑**（写线上 salon 话题清单，人工确认后执行；**默认只补空**，可安全重跑）：

```bash
.venv/bin/tc-score --env prod --apply
```

### `--dry-run` 示意输出

见上 dev 段：逐行打印 `[将写入] <话题名> → <总分> ｜ <理由(截断 60 字)>`，末尾 `待写 N / 跳过 M / 分布校验 <ok|violations>` 与统计 JSON，**零写调用**。

### 统计 JSON 字段

| 字段 | 含义 |
|---|---|
| `mode` | `dry-run` / `apply` |
| `batches` | 组批数（每批 ≤100） |
| `llm_calls` | 实际 LLM 调用次数（单批「调用+解析」共享重试预算，总 HTTP ≤2/批） |
| `rows` | 表内总行数（受 `--limit` 影响） |
| `scored` | 归一后成功打分的条数 |
| `skipped` | 因 `打分` 非空而跳过（或 `plan_writes` 二次判空跳过）的条数 |
| `written` | 实际写入条数（dry-run 恒 0） |
| `failed_writes` | 写入失败条数（单批失败累加本批条数） |
| `failed_batches` | LLM 调用+解析两次仍失败的批数 |
| `dropped` | 被丢弃条数（非法返回项 / 缺返回行 / 多余行） |
| `empty_batches` | 模型合法返回空 `scores` 的批数 |

### 退出码

`2` = 配置/参数非法（config 加载失败 / `prompts/score.md` 缺失或为空 / `score.batch_size>100` / `--provider` 未知（argparse choices 拒绝）/ provider 未注册 / 所选 provider 缺 key / salon token 缺失或占位 / 目标 provider 两列缺失 / 同时给 `--apply` 与 `--dry-run`）；`1` = 未捕获异常，或 `--apply` 全部写入失败，或 `--apply` 全部 LLM 批失败（后两个 rc1 分支**仅 `--apply`** 触发：`written==0` 且 `failed_writes>0`/`failed_batches>0`/待写非空；dry-run 全部 LLM 批失败仍 rc `0`，看统计 JSON 的 `failed_batches`）；`0` = 正常（含单行/单批失败跳过并计数）。

### 注意 / 坑

- **默认 dry-run**：真实写入必须显式 `--apply`。
- **幂等只补空**：判据**仅看 `打分` 列非空**；`理由` 有值但 `打分` 空视为未完成，重算并补齐两列（自愈部分写入失败）。重复 `--apply`（无 `--force`）写入数为 0（PRD §22.10）。
- **列映射**：`--provider minimax` → `MMax打分`/`MMax理由`；`--provider deepseek` → `DS打分`/`DS理由`；两套列互不复用。`打分` 列写**纯数字字符串**（1 位小数，如 `3.5`；全维缺失写 `缺失`）；`理由` 列单行，含 `｜ risk=… ｜ source=… ｜ 六维：…`。
- **绝不触碰其它列**：每批 payload **只含目标 2 个键**（`+record-batch-update`），不 `batch-create`、不删行、不动 `飞书AI打分`/`人工打分` 等其它列；不新增「打分日期」列。
- **批大小与超时**（配置项，无 CLI flag）：`score.batch_size` 默认 **20**（上限 `MAX_SCORE_BATCH=100`），`score.timeout_seconds` 默认 **600**；单批越小越不易读超时（MiniMax M3 约 10–15s/行，批 20 约 4–5 分钟；仍超时把 `score.batch_size` 调至 10），超时按调用异常重试 1 次后计 `failed_batches`。
- 单批 LLM 调用失败（超时/429/529/业务可重试码或契约解析失败）重试 1 次（总 HTTP ≤2/批）后计 `failed_batches` 并跳过该批，不阻断其余批；模型合法返回空列表计 `empty_batches`。
- 分布校验：按 PRD §22.7 校验「`≥4.0` ≤20%」「`<2.0` ≥15%」，违反**仅 WARNING**，不自动调分。
- 不抓 `资讯链接` 指向的网页正文；理由必须引用表内字段。绝不调 `ensure_initialized`，不改表结构。

---

<a id="wiki_home"></a>

## feedkicker.wiki_home

**用途**：重建 Wiki「首页」为按月大块的沙龙大纲索引表格（整篇 overwrite）。salon 每班有新文档后自动调用；也可独立手动重建 / 存量回填。对应 DESIGN §22（并见 §21 的 node-list 兜底）。

**入口**：`.venv/bin/python -m feedkicker.wiki_home`。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--dry-run` | store_true | 关 | — | 仅打印索引 markdown 不写入 |
| `--config` | str | `None` | 覆盖 `--env` 推导 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖推导 | sqlite 路径（本命令数据源是 node-list，不依赖 sqlite） |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 运行环境 |

### 环境差异

| 环境 | 配置 | space / parent 来源 | 影响 |
|---|---|---|---|
| dev | `config-dev.yaml` | `cfg.wiki.*`，回退 `cfg.salon.wiki_*` | 若指向真实 Wiki 则**会覆盖真实首页**，联调务必 dry-run |
| test | `config-test.yaml` | 同上 | 同上 |
| prod | `config-prod.yaml` | 同上 | 真实覆盖 prod Wiki 首页 |

### 示例

**dev**：

```bash
.venv/bin/python -m feedkicker.wiki_home --env dev --dry-run
```

示意输出（「示意」）：

```
=== Wiki 首页预览（dry-run，不写入）===
# AI 沙龙双大纲归档

> 本页由 feedkicker 每周五自动重建（最近更新 2026-09-14），共 10 篇。

## 2026年9月

| 生成日期 | 文件名 | 链接 |
|---|---|---|
| 2026-09-08 | 示例话题 | [打开](https://<host>/wiki/<node_token>) |
```

退出码：`0`。副作用：无（只读 node-list + 打印）。

**test**：`--env test` 真写（会覆盖 test Wiki 首页）。

示意输出：`Wiki 首页已重建：N 篇大纲索引`。退出码 `0`（更新失败或异常 `1`，配置/space-parent 缺失 `2`）。

**prod（仅 dry-run）**：

```bash
.venv/bin/python -m feedkicker.wiki_home --env prod --dry-run
```

⚠️ **prod 真跑**（整篇覆盖 prod Wiki 首页，人工执行）：

```bash
.venv/bin/python -m feedkicker.wiki_home --env prod
```

### `--dry-run` 示意输出

以 `=== Wiki 首页预览（dry-run，不写入）===` 开头，后接完整 index markdown（标题 + 重建说明引用行 + 按月表格）。

### 退出码

`0` = 成功（含 dry-run）；`1` = 未捕获异常或更新失败（`update_homepage` 返回 False）；`2` = 配置加载失败，或 `wiki.space_id` / `parent_token`（含 salon 回退）为空或含 `<` 占位（占位时零 lark 调用直接拒绝）。

### 注意 / 坑

- 数据源是 `wiki +node-list`（即时可靠），**不读 sqlite**；`--db` 仅经 `load_config` 接收，对结果无影响。
- 整篇 overwrite **会抹去首页原模板内容**，不做局部替换。
- 仅收录 `obj_type=docx` 且标题匹配 `{话题}_{YYYY-MM-DD}_大纲` 的节点。
- 更新失败仅 WARNING、返回 `False`，不影响 salon 主流程。

---

<a id="bitable"></a>

## feedkicker.bitable

**用途**：多维表格归档运维入口——补字段/视图/分享、清空重灌、回填存量空归档日期、同步待归档记录。对应 DESIGN §16（归档）、§21.4（子模块拆分）、§20.2（占位守卫）。

**入口**：`.venv/bin/python -m feedkicker.bitable`（`--help` 的 `usage` 显示 `prog="tc-bitable"`；`pyproject` 未注册独立 entry point）。

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--config` | str | `None` | 覆盖 `--env` 推导 | 指定 `config-{env}.yaml` 路径 |
| `--db` | str | `None` | 覆盖 `TC_DB` 与 `--env` 推导 | sqlite 路径 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 环境 |
| `--init` | store_true | 关 | — | 补字段/视图/组织内只读分享 |
| `--reseed` | store_true | 关 | — | 清空本环境记录后全量重灌（dev/test 仅清「环境」匹配行，prod 全清）；与 `--backfill`/`--fix-archive-date` **互斥** |
| `--backfill` | store_true | 关 | — | 回填存量空归档日期；与 `--reseed` **互斥** |
| `--fix-archive-date` | store_true | 关 | `--backfill` 的**别名** | 同「回填存量空归档日期」；与 `--reseed` **互斥** |
| `--dry-run` | store_true | 关 | — | 只读预览：不建 Base、不写表、不清空 |

> `--reseed` / `--backfill` / `--fix-archive-date` 三者互斥（argparse mutually exclusive group），同时给出会以 usage 错误退出 rc `2`；无 flag 或仅 `--backfill`/`--fix-archive-date` 且 token 未就绪时**不自动建 Base**（见退出码）。

### 环境差异

| 环境 | 配置 / db | 归档 Base |
|---|---|---|
| dev | `config-dev.yaml` / `data/tc-dev.sqlite3` | 与 test 共享 Base（「环境」列区分）；`backfill` 请求带出「环境」字段，`env_name=dev` 只处理该环境行（环境缺失/为空的行不额外过滤，向后兼容）；`reseed` 同样按「环境」过滤——**只清 dev 行，test 行保留** |
| test | `config-test.yaml` / `data/tc-test.sqlite3` | 同上，`env_name=test` |
| prod | `config-prod.yaml` / `data/tc-prod.sqlite3` | 独立 prod Base；`env_name=None`（全表） |

### 示例

**dev**：

```bash
.venv/bin/python -m feedkicker.bitable --env dev --dry-run --init --backfill
```

示意输出（「示意」）：

```
dry-run：不建 Base、不写表；以下仅预览将执行的动作
dry-run：将补归档日期字段/「按来源」「按日期」视图/组织内只读分享（未执行）
dry-run：归档日期将回填 0 条（未写入）
dry-run：跳过 sync_env（不写记录）
```

退出码：`0`。副作用：无。

**test**：

```bash
.venv/bin/python -m feedkicker.bitable --env test --init
```

示意输出：`Base: https://<host>/base/<app_token>` → `「按来源」分组视图已设置` → `「按日期」分组视图已创建` → `分享已设为组织内只读`。退出码 `0`。副作用：补字段/视图/分享（幂等）。

**prod（仅 dry-run）**：

```bash
.venv/bin/python -m feedkicker.bitable --env prod --dry-run
```

⚠️ **prod 真跑**（写 prod Base，`--reseed` 会清空重灌，人工确认后执行）：

```bash
.venv/bin/python -m feedkicker.bitable --env prod --init
.venv/bin/python -m feedkicker.bitable --env prod --backfill
```

### `--dry-run` 示意输出

逐项打印「将执行」的动作（init/reseed/backfill 各自预览 + `跳过 sync_env`），不产生任何写入。

### 退出码

`0` = 正常 / 未启用 / dry-run；`1` = 动作段未捕获异常（`ensure_initialized`/`sync_env`/reseed 清表等，顶层 `log.exception` 后退出，R11-11；backfill 路径自身已硬化为 log.error + rc 2）；`2` = `--reseed` 但 Base 未配置或为占位 token（拒绝执行，防「先建后清」），或 `--init` 时 `app_token`/`table_id` 仍为 `<...>` 占位（#262），或**非 `--init`/`--reseed`（含无 flag 与仅 `--backfill`/`--fix-archive-date`）且 `app_token`/`table_id` 未就绪/占位**（log.error，不自动建 Base），或互斥 flag 同时给出（argparse usage 错误）；配置加载失败（`FileNotFoundError`/`ValueError`）→ `log.error` + rc 2。

### 注意 / 坑

- `bitable.enabled=false` 时直接 `return 0`（日志 `bitable 未启用`）。
- `--reseed` 要求既有且非占位 `app_token`/`table_id`，否则返回 2；执行顺序为**先 reset 本地同步标记 → 按环境清表 → sync_env**，清表任一批失败即 log.error + rc 2 中止（标记已清，下轮可自愈重灌）。
- `--backfill` 与 `--fix-archive-date` 等价；`--reseed`/`--backfill`/`--fix-archive-date` 三者互斥，同时给会以 usage 错误退出 rc `2`。
- 无 flag 或仅回填类 flag 且 token 空/占位时 log.error + rc `2`（**不自动建 Base**）；仅 `--init`/`--reseed` 允许创建/修复 Base；`--init` 遇 `<...>` 占位 token 亦 rc `2`（#262）。
- 占位 token（含 `<`）在 dry-run 下只提示「跳过预览」。

---

<a id="wiki"></a>

## feedkicker.wiki

**用途**：在指定 Wiki 父节点下创建单篇 docx 并写入 markdown，返回规范 `/wiki/<node_token>` 链接；主要用于联调。对应 DESIGN §19.5。

**入口**：`.venv/bin/python -m feedkicker.wiki`。**注意：无 `--config`/`--db` 参数。**

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--app-token` | str | `""` | 显式值为准 | 多维表 app_token（lark-cli 自行鉴权，主要作入参兼容） |
| `--space-id` | str | `""` | 显式值 > 配置 | Wiki space id；非 dry-run 且缺省时从 `cfg.wiki.space_id` 回退 |
| `--parent-token` | str | `""` | 显式值 > 配置 | 父节点 node_token；缺省时从 `cfg.wiki.parent_token` 回退 |
| `--title` | str | `"示例话题"` | — | 文档标题，生成 `{title}_{日期}_大纲` |
| `--file` | str | `None` | — | MD 文件路径，默认用 title 生成示例内容；`--dry-run` 下忽略（不读文件） |
| `--dry-run` | store_true | 关 | — | 仅打印 wiki_url 不真传 |
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 缺 token 时据此读配置 |

### 环境差异

- 未给 `--app-token`/`--space-id` 时：按 `--env`（或 `TC_APP_ENV`，默认 prod）读 `config-{env}.yaml`，取 `wiki.app_token`/`space_id`/`parent_token`（回退 `salon.app_token` 等）。
- **非 dry-run 会向真实 Wiki 写文档**：dev/test 需确保配置指向对应空间的父节点，prod 会写 prod Wiki。

### 示例

**dev（dry-run，安全）**：

```bash
.venv/bin/python -m feedkicker.wiki --env dev --title "示例话题" --dry-run
```

示意输出（「示意」）：

```
https://<host>/wiki/wiki_dry_示例话题
{
  "wiki_url": "https://<host>/wiki/wiki_dry_示例话题",
  "filename": "示例话题_2026-09-14_大纲.md"
}
```

退出码：`0`。副作用：无。

**test**（真写，确保 space/parent 指向测试空间）：

```bash
.venv/bin/python -m feedkicker.wiki --env test --title "示例话题" --file ./notes/draft.md
```

示意输出：`https://<host>/wiki/<node_token>`。退出码 `0`（创建失败抛异常，非 0）。副作用：新建 Wiki docx。

**prod（仅 dry-run）**：

```bash
.venv/bin/python -m feedkicker.wiki --env prod --title "示例话题" --dry-run
```

⚠️ **prod 真跑**（新建真实 Wiki 文档，人工执行）：

```bash
.venv/bin/python -m feedkicker.wiki --env prod --title "<话题>" --file <md_path>
```

### `--dry-run` 示意输出

先打印 stub 完整链接 `https://<host>/wiki/wiki_dry_<标题>`，再打印 `{"wiki_url": ..., "filename": ...}` JSON。

### 退出码

`0` = 成功（含 dry-run）；`2` = `--file` 读取失败（不存在 / 非 UTF-8 / 是目录），或**非 dry-run 且 space_id / parent 为空或占位**（拒绝建孤儿 docx，零 lark 调用），或创建失败（缺 lark-cli、`docs +create` 业务失败、取不到 `document_id` 等 `RuntimeError`/`OSError`，统一 `log.error` 后返回 `2`）；配置回退阶段的 `load_config` 异常被 `pass` 吞掉，最终由 token 守卫统一判定 rc `2`（不再以未捕获异常收场）。

### 注意 / 坑

- 无 `--config`/`--db`：凭据只能走 `--env` 配置或直接传 token。
- 取 `node_token` 有 node-get miss → node-list 兜底 → 最后回退 `/docx/<document_id>` 并发 WARNING（见 §19.5 / §21 的 131005 说明）。
- `--file` 缺失时用 title 生成示例内容，不读真实草稿；**`--dry-run` 忽略 `--file`**（直接打印 stub `wiki_dry_<标题>` 链接）。
- 非 dry-run 时任一 token 缺失即触发配置回退（`--app-token`/`--space-id`/`--parent-token` 只要缺一个就加载配置补全）；配置也补不齐则按上表 rc `2` 拒绝执行。

---

<a id="topic"></a>

## feedkicker.topic

**用途**：按「讨论状态 intersects 已选题」服务端过滤，分页拉取沙龙选题记录；或校验「讨论状态」字段类型。**只读**，供 salon 与人工排查。对应 DESIGN §19.2。

**入口**：`.venv/bin/python -m feedkicker.topic`。**注意：无 `--config`/`--db` 参数。**

### 参数表

| flag | 类型 | 默认 | 覆盖关系 | 说明 |
|---|---|---|---|---|
| `--env` | choice `{dev,test,prod}` | `None`（回落 prod） | 覆盖 `TC_APP_ENV` | 环境名，对应 `config-{env}.yaml` |
| `--app-token` | str | `None` | 显式值 > 配置 `salon.app_token` | 覆盖多维表 app_token |
| `--table-id` | str | `None` | 显式值 > 配置 `salon.table_id` | 覆盖表 id |
| `--limit` | int | `200` | — | 分页大小 |
| `--dry-run` | store_true | 关 | — | 仅打印；token 缺失时打印 stub，不校验远端副作用 |
| `--check-fields` | store_true | 关 | — | 校验「讨论状态」字段类型 |

### 环境差异

- app_token/table_id 未给时按 `--env` 读 `config-{env}.yaml` 的 `salon.app_token` / `salon.table_id`。
- dev/test/prod 读取各自的 salon 选题 Base（prod 与 dev/test 为不同 Base）。

### 示例

**dev（dry-run，token 缺失时打 stub）**：

```bash
.venv/bin/python -m feedkicker.topic --env dev --dry-run
```

示意输出（「示意」）：

```json
[
  { "record_id": "recStub000", "fields": { "讨论状态": ["已选题"], "话题名称": "示例已选题话题" } }
]
```

退出码：`0`。副作用：无。

**test**：

```bash
.venv/bin/python -m feedkicker.topic --env test --limit 200
```

示意输出：`[{"record_id": "rec...", "fields": {"讨论状态": ["已选题"], ...}}, ...]`，日志 `已选题 N 条`。退出码 `0`。副作用：无（只读）。

**prod（仅 dry-run，只读，但读真实 Base）**：

```bash
.venv/bin/python -m feedkicker.topic --env prod --dry-run --limit 200
```

（本命令为只读，无写入副作用；仍建议排查时用小 `--limit` 控制输出。）

### `--dry-run` 示意输出

见上 dev 段：token 缺失时打印 stub 记录（`recStub000`）；有 token 时打印真实记录的 `--dry-run` 说明为「仅打印，不校验远端副作用」。

### 退出码

`0` = 正常 / stub；`2` = token/config 类：`app_token`/`table_id` 缺失或为占位 `<...>`、配置加载失败（非 dry-run 时 `log.error` + rc 2；dry-run 回落 stub）；动作段（拉取已选题 / `--check-fields`）无 try，未捕获 `RuntimeError` 直接以 rc `1` 退出（裸 traceback；不存在 main 顶层兜底）。

### 注意 / 坑

- 无 `--config`/`--db`；token 只能走 `--env` 配置或显式参数。
- `--check-fields` 会校验「讨论状态」字段类型（select/multiSelect 等视为合法），字段缺失或类型异常仅 WARNING。
- 分页与 bitable 路径统一：页指纹熔断（lark-cli 忽略 `--offset` 时重复页即报错）+ 页数上限 `_MAX_PAGES`=1000（与 `--limit` 无关）与 20 万 offset（`_CHUNK × _MAX_PAGES`）双兜底；`has_more` 恒真 + 连续空页时第 2 页即熔断（#290），不会死循环。

---

<a id="附录错误码对照"></a>

## 附录：错误码对照

来源：`feedkicker/feishu_card.py`、`feedkicker/wiki_lark.py`、`feedkicker/wiki.py`、`AGENTS.md`。以下为代码/文档**实际存在**的错误码，未出现的不臆造。

| 码 | 场景 | 含义 / 处置 |
|---|---|---|
| `11246` | 卡片 div 内文本标签用了 `markdown` | 自定义机器人旧版卡片不支持 div 内 `markdown`，必须用 `lark_md`（`build_card` 全程用 `lark_md`） |
| `131005` | `wiki +node-get` 反查新建节点 | 新建节点秒级传播延迟导致 `not_found`；已用 `wiki +node-list` 兜底取规范 `node_token`（`wiki.py` / `wiki_home.py`） |
| `>20KB`（20000 字节） | 飞书自定义机器人请求体超限 | `build_card` 降级裁剪：先剥全部 description，仍超则从最旧条目起逐条丢弃，footer 提示「… 已截断 N 条」；不要在 send 层截断 JSON（`feishu_card._MAX_BODY_BYTES`） |

补充：签名校验失败（配置了 `feishu_secret` 但签名错误）飞书返回业务码 `19021`；`feishu.send` 对非 0 业务码记 WARNING 并返回 False，不影响已入库与首跑标记（DESIGN §8）。
