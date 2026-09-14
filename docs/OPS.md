# 运维手册 — feedkicker

面向本机（macOS）运维：如何配置与持凭据、定时任务怎么跑、飞书推送有哪些坑、出问题怎么排查、以及环境分级纪律。逐命令参数与分环境示例见 [`CLI.md`](CLI.md)；需求与设计见 [`PRD.md`](PRD.md) / [`DESIGN.md`](DESIGN.md)（引用 §4/§8/§9/§16/§20）与 [`AGENTS.md`](../AGENTS.md)。

## 目录

- [1. 配置](#1-配置)
- [2. 凭据与环境变量](#2-凭据与环境变量)
- [3. 定时（launchd）](#3-定时launchd)
- [4. 飞书三坑](#4-飞书三坑)
- [5. 排障（症状 → 排查 → 处置）](#5-排障症状--排查--处置)
- [6. 环境分级纪律](#6-环境分级纪律)

---

## 1. 配置

### 1.1 三份配置文件

`config-dev.yaml` / `config-test.yaml` / `config-prod.yaml` **全部 gitignored**，真实凭据只存本地文件或环境变量。三份各有 `.example` 模板（`config-{dev,test,prod}.yaml.example`，入库）可复制起步。

未指定 `--config` 时按环境推导默认文件；默认配置锚定仓库根，不随调用方 cwd 漂移（`config_path_for`，DESIGN §4）。

### 1.2 覆盖顺序

```
--db  >  TC_DB  >  --env  >  TC_APP_ENV  >  prod（默认）
```

- `--env` 覆盖 `TC_APP_ENV`，决定默认 `config-{env}.yaml` 与 db 推导。
- `--config` 显式路径覆盖 `--env` 的默认文件选择。
- db：`--db` > `TC_DB` > 环境推导；都不给则 `data/tc-{env}.sqlite3`。
- 三环境各一库，互不污染；launchd 生产任务显式注入 `TC_APP_ENV=prod`。

### 1.3 字段面（对齐 `config_models.py` dataclass）

顶层 `Config`（13 字段）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `app_env` | str | `"prod"` | 运行环境 |
| `feishu_webhook` | str | `""` | 飞书群机器人 webhook |
| `feishu_secret` | str | `""` | 签名密钥（未开启签名校验则留空） |
| `bootstrap_days` | int | `3` | 冷启动窗口：新源首跑最多推最近 N 天 |
| `http` | HttpConf | `{timeout_seconds: 20.0, user_agent: "rss2feishu/0.1 (+local cron; private)"}` | HTTP 参数 |
| `feeds` | list[Feed] | `[]` | 订阅源 `{name, url}`（name 唯一） |
| `db_path` | Path | `data/tc-prod.sqlite3` | sqlite 路径 |
| `site` | SiteConf | `{top_n: 5}` | 摘要卡每源条数（`enabled` 已移除，不再被读取） |
| `bitable` | BitableConf | 见下 | 多维表格归档 |
| `salon` | SalonConf | 见下 | AI 沙龙 |
| `minimax` | MinimaxConf | 见下 | 大纲生成 |
| `wiki` | WikiConf | 见下 | Wiki 归档 |
| `extract` | ExtractConf | 见下 | 资讯→选题 LLM 提炼（`tc-extract`） |

嵌套段：

| 段 | 字段（默认） |
|---|---|
| `bitable` | `enabled=False`、`app_token=""`、`table_id=""`、`url=""`、`retention_days=365` |
| `salon` | `enabled=False`、`app_token=""`、`table_id=""`、`wiki_space_id=""`、`wiki_parent_token=""`、`trigger_weekday=4`、`trigger_hour=10`、`trigger_minute=0` |
| `minimax` | `api_key=""`、`model="MiniMax-M3"`、`base_url="https://api.minimaxi.com"` |
| `wiki` | `space_id=""`、`parent_token=""`、`app_token=""`（未配置时回退 `salon.wiki_space_id`/`salon.wiki_parent_token`） |
| `extract` | `enabled=False`、`since_days=7`、`batch_size=30`、`provider="minimax"`、`prompt_file="prompts/extract.md"`、`max_calls=0`（0=不限）、`providers=dict[str, ProviderConf]`（`base_url`/`model`/`api_key`/`tool_label`） |

以 `feedkicker/config_models.py` 与 `feedkicker/config.py` 的 `load_config` 为准；未文档化别名（`salon.wiki_space`、`wiki.wiki_space_id` 等）已移除。

`extract` 段补充：提示词文件默认 `prompts/extract.md`（仓库根相对路径，可配 `extract.prompt_file`）；provider 注册表默认值在 `feedkicker/extract_llm.py`（minimax：`https://api.minimaxi.com/v1` / `MiniMax-M3` / `MMX（MiniMax）`；deepseek：`https://api.deepseek.com/v1` / `deepseek-chat` / `DS（DeepSeek）`），yaml `providers.<name>` 非空字段覆盖默认。

### 1.4 db 分流

| 环境 | 默认 db | 默认配置 |
|---|---|---|
| dev | `data/tc-dev.sqlite3` | `config-dev.yaml` |
| test | `data/tc-test.sqlite3` | `config-test.yaml` |
| prod | `data/tc-prod.sqlite3` | `config-prod.yaml` |

---

## 2. 凭据与环境变量

### 2.1 环境变量（覆盖 yaml，`config.py`）

| 变量 | 覆盖 | 说明 |
|---|---|---|
| `FEISHU_WEBHOOK` | `feishu_webhook` | 凭据可不落文件 |
| `FEISHU_SECRET` | `feishu_secret` | 签名密钥 |
| `MiniMax_Key`（或 `MINIMAX_API_KEY`） | `minimax.api_key`、`extract.providers.minimax.api_key` | 大纲生成 / 选题提炼；占位值（以 `<` 开头）会被清空 |
| `DEEPSEEK_API_KEY` | `extract.providers.deepseek.api_key` | 选题提炼（DeepSeek provider）；占位值清空 |
| `TC_SALON_TOKEN` | `salon.app_token` | 沙龙选题 Base |
| `TC_FEISHU_HOST` | —（`feishu_host.feishu_host()`） | 飞书租户域名，默认 `web91vfvm7.feishu.cn`；换租户/测试注入 |
| `TC_APP_ENV` | 运行环境 | `dev\|test\|prod`，默认 `prod` |
| `TC_DB` | db 路径 | 显式指定时高于环境推导 |

### 2.2 凭据持有规则

- `config-{dev,test,prod}.yaml` 及真实 webhook / secret **不入库**，只存本地文件或环境变量。
- `.example` 中的值一律是占位符（`<dev-token>`、`<prod-secret>`…），不能直接当真实凭据用。
- 文档与 issue 中不落真实 token / webhook / 签名 secret。
- 历史提交曾含 salon `app_token`/`table_id`（标识符，不授予访问权，#225）：轮换该 Base 的 app_token 或书面记录「已接受风险」；工作树已占位化。
- 历史提交曾含资讯 Base（prod）record_id（标识符，不授予访问权，#236）：仅建议知悉；tracked 已移除，`tests/test_hygiene.py` 以 sha256 canary 防回归（明文不入库）。

### 2.3 双 Base 拓扑

- **prod 与 dev/test 是不同 Base**（prod 独立 Base；dev/test 共享一个文件，用「环境」列区分行）。
- `--backfill` 在 dev/test 下按 `env_name`（`dev`/`test`）只处理对应环境行；prod 下 `env_name=None` 处理全表。
- 多维度表格是**唯一在线档案**；本地 sqlite 是待推状态与去重索引。

---

## 3. 定时（launchd）

无常驻进程，由 macOS launchd 在 `StartCalendarInterval` 触发，睡眠错过触发点后唤醒补跑一次。三个 plist 均在 `~/Library/LaunchAgents/`，均注入 `TC_APP_ENV=prod`：push 每日 8:30/16:00、salon 周五 10:00、purge 每月 1 号 10:30 **仅 dry-run 巡检**（DESIGN §9 / §20.3）。

| plist | Label | 调度 | ProgramArguments | 日志 |
|---|---|---|---|---|
| `com.feedkicker.push.plist` | `com.feedkicker.push` | 每日 8:30 与 16:00 | `.venv/bin/python -m feedkicker.push` | `logs/push.log` |
| `com.feedkicker.salon.plist` | `com.feedkicker.salon` | `Weekday=5`（周五）10:00 | `.venv/bin/python -m feedkicker.salon_flow` | `logs/salon.log` |
| `com.feedkicker.purge.plist` | `com.feedkicker.purge` | 每月 1 号 10:30 | `.venv/bin/python -m feedkicker.purge`（**不带 `--apply`**） | `logs/purge.log` |

### 3.1 加载 / 重载

改 plist 或首次加载后：

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.feedkicker.push.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.feedkicker.push.plist
```

（salon / purge 同理替换文件名。）校验：

```bash
plutil -lint ~/Library/LaunchAgents/com.feedkicker.purge.plist
launchctl print gui/$UID/com.feedkicker.purge | grep -i calendar   # 应含 day = 1, hour = 10, minute = 30
```

### 3.2 purge 的巡检语义

- ProgramArguments **不带 `--apply`**：调度只做 dry-run 巡检，`PurgeStats` 落 `logs/purge.log`。
- 真删由人工看过日志后执行（见 §6）。

---

## 4. 飞书三坑

1. **签名算法**：`string_to_sign = f"{timestamp}\n{secret}"` 作为 HMAC-SHA256 的 **key**，对**空串**求摘要，再 Base64（`feishu.gen_sign`）。仅在配置了 `feishu_secret` 时注入 `timestamp` + `sign`；签名错误飞书返回业务码 `19021`。
2. **卡片 div 文本标签只能是 `lark_md`**：写 `markdown` 会被拒（业务码 `11246`）。`build_card` 全程用 `lark_md`。
3. **请求体 ≤20KB**：超限由 `build_card` 降级裁剪（先剥全部 description → 仍超则丢最旧条目，footer 提示截断），`_MAX_BODY_BYTES=20000`；**不要在 send 层截断 JSON**（会变残缺 JSON 被拒、条目永久 pending）。

---

## 5. 排障（症状 → 排查 → 处置）

| 症状 | 排查 | 处置 |
|---|---|---|
| 卡片没到群 | 看 `logs/push.log`：`跳过推送：webhook 为空` / `HTTP 非 200` / 业务码非 0 / 签名失败 | 检查对应环境 `feishu_webhook`、`FEISHU_SECRET` 是否与群机器人设置一致；非 prod 配置了真实 webhook 会打 WARNING |
| 卡片被拒（业务码 `11246`） | 卡片 div 用了 `markdown` 标签 | 改回 `lark_md`；`build_card` 已是 `lark_md`，勿在别处手拼卡片 |
| 请求体超限 | 日志无显式错误，但卡片偶发失败 | 确认走 `build_card`（自动降级 ≤20KB）；不要在 send 层截断 JSON |
| Wiki 链接偶发 `/docx/` 回退 | 日志 `wiki +node-get/node-list 均未拿到 node_token` | 新建节点有秒级 131005 传播延迟；node-list 兜底已实现（#140/#133）；稍后重跑即可 |
| 首页索引缺最新文档 | `wiki_home` 失败仅 WARNING | 手动 `python -m feedkicker.wiki_home --env prod` 重建；确认标题匹配 `{话题}_{YYYY-MM-DD}_大纲` |
| 归档没进多维表格 | `logs/push.log` 的 `多维表格同步未完成（不影响推送…）` WARNING | 表格是常驻档案，卡片照发；排查 Base token/权限后重跑，未归档批次会重试 |
| 条目重复推送 | 上次发送成功但进程在 `mark_pushed` 前崩溃 | 已知权衡（webhook 不保证不重发）；接受或人工核对 |
| 连续推送失败 | 群内出现 SOS「连续 N 次推送失败」 | 检查机器人状态/网络；`push_fail_streak` 达 3 次触发 SOS 后清零 |
| bitable 命令拒绝执行 | 返回码 2：`拒绝 --reseed：… 占位 token` | 确认 `bitable.app_token`/`table_id` 非空且非占位（不含 `<`） |
| purge 没删记录 | `bitable_skipped_reason` 非空 | 未启用 / token 缺失 / 占位 token 时跳过 bitable 段；补齐配置或人工确认后重跑 |

**时序要点**：`mark_pushed` 只在**发送成功后**执行，失败条目保留待推、下轮重发；归档同步失败只 WARNING，卡片照发；`tc-purge` 的 meta `purge_last_run_at` 仅在真删且 bitable 段整段成功时写入。

---

## 6. 环境分级纪律

- **prod 库默认禁写**：docs 中的 prod 示例一律 `--dry-run`；真跑命令在 CLI.md 单列并标 ⚠️，执行前人工确认。
- **`tc-purge --apply` 必须人工**：launchd 只做每月 1 号 10:30 的 dry-run 巡检，真删由人工看过 `logs/purge.log` 后手动执行。
- purge 只操作资讯归档 Base（`cfg.bitable`），**不碰 salon 选题 Base**，且**绝不调 `ensure_initialized`**（防误建 Base）。
- dev/test 可较自由地联调，但两者共享同一归档文件（靠「环境」列区分），操作时按环境参数隔离。
- 不在未经确认时 push / 合并 / 处理分支收尾；凭据不上库、不进文档。
