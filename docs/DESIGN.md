# 技术设计文档 — RSS → 飞书 转发机器人（v2 起点）

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述只在一处维护、另一处引用，避免漂移
> 版本：v0.5 · 2026-09-02 · v0.1 卡片推送、v0.2 GitHub Pages（已废弃，§15）、v0.3–v0.5 多维表格归档定稿（§16/§18）

## 1. 架构总览

单进程、无网络服务、无队列，一次 cron 运行 = 抓全部 → 下载入库 → 查待推 → 组卡片 → 推飞书 → 标已推 → 退出。

```
                cron（每天 8:30 / 16:00）
                      │  python -m feedkicker.push
                      ▼
            ┌───────────────────────┐
  Http ───► │   feedkicker.push     │ ─―► 飞书群机器人 webhook
  RSS/Atom  │  (单进程，跑完即退)     │      (interactive 汇总卡片)
            └──────────┬────────────┘
                       ▼
           data/tc-{env}.sqlite3（本地存档已下载新闻）
```

**核心约束**
- 无常驻进程、无 Web 端口、无消息队列：由 cron / 手动拉起来，干完即退。
- 单进程内**串行**处理多源（规模小，不值得并发）；一源失败 caught，不影响其他源。
- 全部为**同步**代码（httpx sync + sqlite3 sync）——不需要 asyncio。

## 2. 技术选型

| 领域 | 选型 | 说明 |
|---|---|---|
| 语言/运行时 | Python 3.12+ | 依赖全纯 Python（feedparser/httpx/PyYAML）+ stdlib sqlite3，无 C 扩展、无 wheel 风险 |
| RSS 解析 | `feedparser` | 纯 Python；`entry.published_parsed` 给 struct_time，免额外时间库 |
| HTTP | `httpx` sync | 抓 feed + 打飞书 webhook，统一设超时/UA |
| 配置 | `PyYAML` | `config-{env}.yaml` + 环境变量覆盖 |
| 存储 | stdlib `sqlite3` | `data/tc-{env}.sqlite3`，自动建表建目录 |
| 测试 | `pytest` | feedparser 本地 fixture + 内存 sqlite（`:memory:`） |
| 调度 | cron（系统级） | 进程不由应用常驻，见 §9 |

## 3. 目录结构

```
topic_collection/
├── README.md                 # 项目总览与快速上手（入口，§23 F24）
├── pyproject.toml            # 依赖 + [project.scripts] tc-push/tc-salon/tc-purge/tc-extract/tc-score
├── config-{dev,test,prod}.yaml  # 三环境分文件，均 gitignored（见 §4）
├── prompts/extract.md        # 资讯→选题提炼提示词（用户原文 + 输出 JSON schema，§25）
├── prompts/score.md          # 话题自动打分提示词（用户原文 + 「本批话题（含横向上文）」注入说明，§26）
├── docs/
│   ├── PRD.md / DESIGN.md    # 产品权威 / 工程实现权威
│   ├── CLI.md                # 8 命令命令行详解（§23 F25）
│   └── OPS.md                # 运维手册：配置/凭据、launchd、飞书坑、排障（§23 F26）
├── data/                     # 运行时生成：tc-{env}.sqlite3（gitignore）
├── logs/                     # launchd 重定向写日志（gitignore）
├── feedkicker/
│   ├── config.py             # 读 config-{env}.yaml + env 覆盖 + facade re-export（§21.2）
│   ├── config_models.py      # 路径常量与全部配置 dataclass（叶子模块，§21.2）
│   ├── log_setup.py          # 共享日志初始化：basicConfig + 静音 httpx/httpcore（#287）
│   ├── fetch.py              # feedparser 抓取 + 归一化
│   ├── store.py              # sqlite 主表 + facade re-export（§21.2）
│   ├── store_conn.py         # sqlite 连接/schema 迁移：WAL + busy_timeout（§21.2/#239）
│   ├── store_meta.py         # meta 键值表（叶子模块）
│   ├── store_salon.py        # salon 选题 sqlite 状态（ppt 同步/last_status/落库）
│   ├── feishu.py / feishu_card.py    # webhook 发送 / 卡片构建（facade，§21.2）
│   ├── feishu_card_body.py   # 卡片 body 组装与截断提示（自 feishu_card 抽出，§21.2）
│   ├── feishu_host.py        # 飞书租户域名单点（TC_FEISHU_HOST 覆盖）
│   ├── bitable.py            # 多维表格 CLI + facade re-export（§21.2/§21.4）
│   ├── bitable_lark.py       # lark-cli 进程层：_run/_parse/_json_arg/SHANGHAI（§21.4）
│   ├── bitable_schema.py     # Base/数据表初始化：字段、建库、ensure_initialized（§21.4）
│   ├── bitable_views.py      # 视图/字段/分享设置：setup_view/按日期视图/组织内只读（§21.4）
│   ├── bitable_records.py    # 记录读写：去重建链、批量写、清空重灌、sync_env（§21.4）
│   ├── bitable_backfill.py   # 归档日期解析与存量回填 backfill_empty_archive_dates（§21.4）
│   ├── bitable_purge.py      # 滚动保留的 bitable 侧删除（§20）
│   ├── bitable_reseed.py     # --reseed 前置清空 purge_all_records（bitable_records re-export，§21.4）
│   ├── minimax.py            # MiniMax 大纲 facade：gen_outline + re-export patch 点（#346）
│   ├── minimax_transport.py  # MiniMax chat 调用与错误码归一（_resolve_api_key/_extract_code/_norm_code/_RETRY_CODES/call_minimax_chat，#346）
│   ├── minimax_parse.py      # MiniMax 大纲响应解析：tool_calls/content 回退 + think 剥离（#346）
│   ├── minimax_schema.py     # prompt 与 function-calling schema
│   ├── reasoning.py          # thinking 内联推理块剥离 strip_reasoning（#321/#323/#331）
│   ├── wiki.py / wiki_lark.py          # Wiki 归档编排 / lark-cli 调用层
│   ├── wiki_home.py          # Wiki「首页」自动索引：node-list → 月块表格 → overwrite（§22）
│   ├── topic.py              # 已选题分页拉取（facade）
│   ├── topic_records.py      # topic 响应记录归一（自 topic 抽出，§21.2）
│   ├── salon_flow.py         # 沙龙编排主流程（§19）
│   ├── salon_md.py / salon_notify.py  # 大纲 markdown/stub / 卡片与连败 SOS
│   ├── extract_source.py     # 近 N 天 RSS 行选源（F28，§25）
│   ├── extract_llm.py        # LLM provider 抽象 / 批量提示词 / JSON 解析与多源合并（F29–F30，§25）
│   ├── extract_parse.py      # 提示词构建与 JSON 解析（自 extract_llm 拆出，§25.4/#250）
│   ├── extract_write.py      # 选题表字段映射 + 「资讯链接 OR 话题名称」双键去重写入（F31，§25）
│   ├── extract_flow.py       # tc-extract 编排 + CLI（F32，§25）
│   ├── extract_report.py     # dry-run 清单与运行统计输出（自 extract_flow 拆出，§25.6/#252）
│   ├── score_source.py       # 读「沙龙话题清单」全表行 + 组批（≤100）（F38/F39，§26）
│   ├── score_llm.py          # provider 调用（复用 PROVIDERS/resolve_provider）（F38，§26）
│   ├── score_parse.py        # 打分契约解析/归一/否决/缺失/分布校验（F40，§26）
│   ├── score_write.py        # 写列/幂等/统计（F41，§26）
│   ├── score_report.py       # dry-run 清单与摘要（F40/F41，§26）
│   ├── score_flow.py         # tc-score 编排 + CLI（F38–F41，§26）
│   ├── push.py               # push 编排主流程
│   └── purge.py              # tc-purge 编排：365 天滚动保留（§20）
└── tests/                    # 全离线，subprocess/httpx 一律 mock（test_push/test_salon_*/test_purge 等）
```

## 4. 配置

**分环境文件**：`config-dev.yaml` / `config-test.yaml` / `config-prod.yaml`（均不入库，凭据本地持有）。
未指定 `--config` 时按 `TC_APP_ENV` / `--env` 推导对应文件；dev/test 凭据留空（不推送真实群），
仅保留量子位单源、冷启动窗口 1 天；prod 为完整源清单 + 真实凭据。以 prod 为例：
配置选择优先级：`--config` 显式路径 > `--env` > `TC_APP_ENV` > 默认 `prod`。

```yaml
feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/<token>"
feishu_secret: "<签名密钥>"   # 机器人开启「签名校验」安全设置时的密钥；未开启则留空
bootstrap_days: 3      # 冷启动窗口：新源首跑最多推最近 N 天（下限 1，上限 3650，超限 rc 2）
site:
  top_n: 5             # 摘要卡每源保留的最新条数（site 段仅此项生效；enabled 已不再被读取）
http:
  timeout_seconds: 20
  user_agent: "rss2feishu/0.2 (+local cron; private)"
feeds:
  - name: "HN 热榜"
    url: "https://news.ycombinator.com/rss"
```

环境变量（`feedkicker/config.py` 覆盖顺序：默认值 < config-{env}.yaml < 环境变量）：
- `FEISHU_WEBHOOK` —— 覆盖 webhook（凭据不进文件可选）
- `FEISHU_SECRET` —— 覆盖签名密钥（同上）
- `TC_APP_ENV` —— 运行环境 `dev|test|prod`（默认 `prod`），决定默认 db 路径
- `TC_DB` —— sqlite 路径，显式指定时优先级高于环境推导

数据库按环境分流：默认 `data/tc-{env}.sqlite3`（dev/test/prod 各一库，互不污染；
launchd 生产任务显式注入 `TC_APP_ENV=prod`）。CLI `--env` / `--db` 可覆盖。

`config.py` 用 `dataclass` 类型化（以 `feedkicker/config.py` 为准，共 13 个顶层字段）：
`Config{app_env, feishu_webhook, feishu_secret, bootstrap_days, http: HttpConf{timeout_seconds, user_agent}, feeds: list[Feed{name, url}], db_path, site: SiteConf{top_n}, bitable: BitableConf{enabled, app_token, table_id, url, retention_days}, salon: SalonConf{enabled, app_token, table_id, wiki_space_id, wiki_parent_token, trigger_weekday, trigger_hour, trigger_minute}, minimax: MinimaxConf{api_key, model, base_url}, wiki: WikiConf{space_id, parent_token, app_token}, extract: ExtractConf{enabled, since_days, batch_size, provider, prompt_file, max_calls, providers: dict[str, ProviderConf{base_url, model, api_key, tool_label}]}}`。

配置键权威面（canonical）：`salon.wiki_space_id`/`salon.wiki_parent_token`、`minimax.api_key`/`model`/`base_url`、`wiki.space_id`/`parent_token`/`app_token`、`bitable.*`（`enabled`/`app_token`/`table_id`/`url`/`retention_days`）、`site.top_n`。
**未文档化别名已移除**（#162）：`salon.wiki_space`、`salon.minimax_api_key`、`minimax.minimax_api_key`、`wiki.wiki_space_id`、`wiki.wiki_parent_token`；`wiki.space_id`/`parent_token` 未配置时回退 `salon.wiki_space_id`/`salon.wiki_parent_token`（§22.2）。

## 5. 数据模型（DDL）

```sql
CREATE TABLE IF NOT EXISTS articles (
  feed_id      TEXT NOT NULL,
  entry_key    TEXT NOT NULL,      -- guid | canonicalized link
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  description  TEXT,               -- 原文自带摘要，原样存
  published_at TEXT,               -- ISO-8601 UTC；可为 NULL（feed 未给时间）
  first_seen   TEXT NOT NULL,      -- 首次下载时点 UTC
  pushed_at    TEXT,               -- NULL=待推送；非空=已发飞书
  PRIMARY KEY (feed_id, entry_key)
);

CREATE TABLE IF NOT EXISTS feeds (
  feed_id      TEXT PRIMARY KEY,   -- config 里的 feed name
  url          TEXT NOT NULL,
  first_run_at TEXT NOT NULL,      -- 首跑时点 UTC（判冷启动窗口）
  fail_streak  INTEGER DEFAULT 0   -- 连续失败次数（成功归零）
);

CREATE INDEX IF NOT EXISTS idx_articles_pending ON articles (pushed_at)
  WHERE pushed_at IS NULL;
```

运行期迁移列（非基线 DDL）：connect 时由 `store.py` 以 PRAGMA 检查并 `ALTER TABLE … ADD COLUMN` 补齐 `articles.bitable_synced_at`（多维表格归档打标，§16.3）与 `articles.ppt_synced_at`（沙龙大纲打标，§19/F18）；本 DDL 仅基线，schema 以 `store._SCHEMA` + 迁移为准。

另有 `meta(key, value)` 键值表（`store_meta.py`，CREATE IF NOT EXISTS 对存量库透明）：`push_fail_streak`、`salon_fail_streak`、`purge_last_run_at` 等运行期状态（§15.5/§20）。

## 6. 核心流程（push.py 编排）

```
run(config):
  1. 读 config；打开 store（建目录、跑 CREATE TABLE IF NOT EXISTS）
  2. 出参 new_items:[], feed_fails:int, now=utc_now()
  3. for feed in config.feeds:
       try:
         entries = fetch.feed(feed)              # 归一化，见 §6.1
         store.download(feed, entries, now)      # INSERT ON CONFLICT DO NOTHING
         store.clear_fail(feed)                  # 抓取成功即清零该源 fail_streak（push.py）
         if store.is_first_run(feed):            # feeds 无该行
             cutoff = now - timedelta(days=config.bootstrap_days)
             store.promise_skip_old(feed, cutoff)  # 窗口外置 pushed_at，入档不推
       except Exception as e:
         feed_fails += 1; store.bump_fail(feed); log.error(e)   # 一源失败不影响他源
  4. new_items = store.select_pending()          # SELECT * WHERE pushed_at IS NULL
  5. if not new_items:
         store.update_first_run_all(config, now) # 记首跑；不发空卡，正常退出
         return 0
  6. payload = feishu.build_card(new_items, feed_fails, config.http)  # §7
  7. if config.dry_run: print(payload); return 0  (仅 --dry-run)
  8. ok = feishu.send(payload)                    # §8
  9. if ok: store.mark_pushed(new_items)          # 推送成功只打标 + 清零 meta push_fail_streak
     store.update_first_run_all(config, now)     # 首跑标记无论成败都记（防重复跑窗口）
     return 0 if ok else 1
```

### 6.1 fetch.feed() 归一化

`feedparser.parse(url)`（httpx 拿到 bytes 再 parse 或由 feedparser 直抓，二选一；推荐 httpx 抓 bytes + `feedparser.parse(content)`，统一 UA/超时/码判断）。每 entry → dict：

```
entry_key      = entry.guid 若非空，否则 canonicalize(entry.link)     # §6.2
title          = entry.title or ""（去空白）
url            = canonicalize(entry.link)
description    = entry.summary or entry.description or ""（原样，不清洗）
published_at   = iso_utc(entry.published_parsed) 若存在，否则 None
```

`feedparser` 的 `bozo`/`entries` 为空或 HTTP 非 200（按 httpx 码区分 2xx/4xx/5xx，480 重定向等按 httpx 默认跟随）视为该源失败。

### 6.2 canonicalize(url) / entry_key 规则

- `canonicalize`：URL parse + `#` fragment 去掉 + host 转小写；保留 query（query 差异可能代表不同文章，不粗暴丢弃）。
- `entry_key`：优先 `entry.guid`；无 guid 时用 canonicalize(link)；两 source 均无 → 用 title 规范化（strip、lower）作兜底，避免空 key 全部撞一条。

### 6.3 store.download 幂等

`INSERT INTO articles (...) VALUES (...) ON CONFLICT (feed_id, entry_key) DO NOTHING`。已存在 = 已下载，跳过；新行 `pushed_at=NULL` = 待推送。整店单事务，异常回滚。

### 6.4 冷启动窗口

`store.is_first_run(feed)` = `feeds` 表无该 `feed_id`。首跑时：
- `cutoff = now - timedelta(days=config.bootstrap_days)`
- `promise_skip_old`: `UPDATE articles SET pushed_at=COALESCE(pushed_at, now) WHERE feed_id=? AND published_at IS NOT NULL AND published_at < cutoff AND pushed_at IS NULL`
- 效果：窗口外（>N 天前发布）条目**入库但不推**；`published_at` 为 NULL 的条目**不会**被窗口排除（当作新增推，避免丢新条目）。
- `update_first_run_all`：**仅对本轮成功抓到内容的 feed** 记 `first_run_at`（#113）。首跑即失败的源保留未首跑状态（`bump_fail` 插入的空戳行不被升级），恢复后仍按窗口过滤历史，避免全量历史当新条目推送（F4）；失败连击由 `fail_streak` 记录。

## 7. 飞书卡片构建（feishu.build_card）

interactive 汇总卡片，一次运行一张；按 feed 分组。

```python
def escape_inline(text):
    # 最小转义：保持原样但同时不破坏卡片 markdown 布局
    text = text.replace("\r", "").replace("\n", " ")   # 压成单行（换行交给卡内 \n 控制）
    return re.sub(r'([\\`*_\[\]()#])', r'\\\1', text)   # 转义会撞 markdown 的保留字
```

```python
def build_card(new_items, feed_fails, feed_order, max_bytes=20000):
    groups = groupby_feed(new_items)                 # 保 config 顺序
    parts = []
    for feed_name, items in groups:
        parts.append(f"**{escape_inline(feed_name)}**")
        for it in items:
            parts.append(f"[{escape_inline(it.title)}]({it.url})")
            if it.description: parts.append(escape_inline(it.description))
        parts.append("")                              # feed 之间空行
    content = "\n".join(parts)
    elements = [{"tag": "div",
                 "text": {"tag": "lark_md", "content": content}}]   # 自定义机器人旧版卡片不支持 div 内 markdown 标签
    if feed_fails:
        elements += [{"tag": "hr"},
                     {"tag": "div", "text": {"tag": "lark_md",
                                             "content": f"⚠ {feed_fails} 个源失败"}}]
    if dropped:
        elements += [{"tag": "div", "text": {"tag": "lark_md",
                                             "content": f"… 已截断 {dropped} 条旧条目"}}]
    return {"msg_type": "interactive",
            "card": {"header": {"title": {"tag": "plain_text",
                                          "content": f"Feeds 汇总  {local_now():%H:%M}"},
                                 "template": "blue"},
                     "elements": elements}}
```

说明：
- 每 feed 一段分组；组内每篇 `[标题](链接)` + description 原样（仅 `escape_inline` 压行 + 转义保留字，见 PRD §10"最小让步"）。
- 标题内容为空 → 用 url 兜底；description 为空 → 不残留空行。
- **20KB 降级裁剪**（飞书自定义机器人请求体上限，官方文档）：序列化后按 UTF-8 字节数检查；超限先剥全部 description 重排，仍超则从最旧条目起逐条丢弃直至塞下；发生丢弃时卡片 footer 追加「… 已截断 N 条」。保证发出的 payload 永远是完整合法 JSON（替代早期"30000 字符硬截断"——残缺 JSON 必被拒且条目永久 pending）。
- 无新条目时 build_card 不会被调用（push 流程 §6 step 5 直接返回）。

## 8. 推送 / 校验（feishu.send）

```python
def send(payload, webhook_url, timeout_seconds, user_agent, secret=""):
    if not webhook_url:
        log.warning("跳过：webhook 为空"); return False
    try:
        body_payload = dict(payload)
        if secret:                                    # 签名校验安全设置（官方算法）
            ts = str(int(time.time()))                # 秒级时间戳，1 小时内有效
            body_payload["timestamp"] = ts
            body_payload["sign"] = gen_sign(ts, secret)
        body = json.dumps(body_payload, ensure_ascii=False)
        resp = httpx.post(webhook_url, content=body.encode("utf-8"), timeout=timeout_seconds,
                          headers={"Content-Type": "application/json", "User-Agent": user_agent})
        if resp.status_code != 200: log.warning(...); return False
        data = resp.json()
        code = data.get("StatusCode", data.get("code", 0))   # 飞书业务码
        return code == 0
    except Exception as e:
        log.warning("推送异常: %s", e); return False

def gen_sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"         # 官方：timestamp+"\n"+密钥 作 key，
    hmac_code = hmac.new(string_to_sign.encode(), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode()       # 对空串求 HmacSHA256 再 Base64
```

- 签名仅在配置 `feishu_secret`（yaml `feishu_secret` / env `FEISHU_SECRET`）时注入；校验失败返回业务码 19021。
- 卡片体积由 build_card 保证 ≤20KB，send 不再做内容截断。

- 发送失败仅 warning，不抛；不影响已入库/已置首跑的副作用（推送是尽力而为的一环）。
- `ok=False` 时**不 mark_pushed** → 下次运行这些条目仍是待推（自然重试窗口）。幂等 + 重复发送由飞书 webhook 语义决定：**不能保证不重发**，但若上次发送实际成功而进程在 mark 前崩溃，下次会重发——接受该权衡，标记为 §12 待定（PRD §12 已有"发送失败重试策略"待定）。

## 9. 调度（launchd，无常驻）

v0.2 起 macOS 用 **launchd** 取代 cron：`StartCalendarInterval` 在机器睡眠错过触发点后，唤醒时会补跑一次（cron 直接丢弃）。

```xml
<!-- ~/Library/LaunchAgents/com.feedkicker.push.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.feedkicker.push</string>
  <key>ProgramArguments</key>
  <array><string>/Users/linyunxia/PycharmProjects/topic_collection/.venv/bin/python</string>
         <string>-m</string><string>feedkicker.push</string></array>
  <key>WorkingDirectory</key><string>/Users/linyunxia/PycharmProjects/topic_collection</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/Users/linyunxia/PycharmProjects/topic_collection/logs/push.log</string>
  <key>StandardErrorPath</key><string>/Users/linyunxia/PycharmProjects/topic_collection/logs/push.log</string>
</dict></plist>
```

- 加载：`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.feedkicker.push.plist`
- 进程由 launchd 拉起，跑完退出，无后台残留、无 Web 端口监听。
- 手动/联调：`python -m feedkicker.push --dry-run`（打 payload 不发不发布）；`--config <路径>`；`--db <路径>`。
- `--dry-run` 并非纯只读：RSS 抓取与 sqlite 写库（`download`/`clear_fail`/`promise_skip_old` 等首跑标记）照常，仅跳过发送与 bitable 归档（#183）。

## 10. 日志与可观测

满足 PRD §11 #8 与 #7："不常驻、可回溯"。`logging` 输出到 stdout/stderr（cron 重定向到 `logs/push.log`）：
- INFO：每次运行摘要（成功/失败 feed 数、新增条目数、推送是否成功）
- WARNING：某源失败、webhook 为空、发送失败、超长截断
- ERROR：未捕获异常（应基本不出现，顶层 try 兜底 `--` traceback）

审计查询：`sqlite3 data/tc-{env}.sqlite3 "SELECT Count(*) FROM articles"` / `WHERE pushed_at IS NULL` 看待推存量。

## 11. 测试（tests/test_push.py）

尽量内网/无网络跑；feedparser 用本地 fixture 字节串，sqlite 用 `:memory:`。

- `test_fetch_normalizes`：喂一段 RSS XML，断言 title/link/description/published_at/entry_key 归一化正确。
- `test_guid_vs_link_key`：有 guid 用 guid；无 guid 用 canonicalize(link)；fragment 去掉。
- `test_download_idempotent`：同一 feed 两次 `download`，第二次 0 新增。
- `test_bootstrap_window`：注入带 5 天前条目的 feed，`bootstrap_days=3`，首跑只推 3 天内；更早的入库但 `pushed_at` 非空。
- `test_null_published_not_excluded`：`published_at IS NULL` 的条目不被窗口排除。
- `test_second_run_after_bootstrap`：首跑后不再按窗口，普通新增照推。
- `test_build_card_grouping`：按 feed 分组、顺序保持、description 压单行转义、空 description 无残留。
- `test_build_card_failure_footer`：`feed_fails>0` 时带「⚠ N 个源失败」；为 0 时不带。
- `test_send_business_code`：mock httpx.post，断言 `StatusCode=0` 判定成功、非 0 判定失败。
- `test_gen_sign_known_vector` / `test_send_injects_signature`：官方算法固定向量；secret 非空注入 timestamp/sign、为空不注入。
- `test_build_card_trims_to_20kb`：超限降级裁剪后序列化 ≤20KB 且 JSON 完整带截断提示；小体量不裁剪。
- `test_no_new_items_no_empty_card`：无待推时 snapshot 记录 `build_card` 不被调用/不发空卡。

> v0.3–v0.5 起新增 bitable 系列测试（`_cell` 字段/归档日期跨零点、跨源去重、双视图、
> 重灌、backfill、sync_env、ok:false 业务失败、@文件传参、existing_links 失败中止）与
> 首跑失败恢复窗口测试；用例总数以 AGENTS.md 命令一节为准（测试全离线，subprocess/httpx 一律 mock）。

## 12. 速率与错误分类（暂从简）

- HTTP 抓 feed：httpx 默认跟随重定向；>30 个 feed 时建议手动加每源间隔（现阶段串行 + timeout 足够）。
- 错误分类暂不分瞬时/永久——**每次失败仅计数 + 下次重试**（`fail_streak` 记录，成功归零）。是否需要"连续失败 N 次静默/警告抬升"留 §13 待定。

## 13. 待定 / 明确不做（Not Now）

对应 PRD §12：
- 不做：LLM / 图谱 / 周报 / 翻译 / 向量检索 / WebUI / 多用户 / 网页爬虫。
- 待定：飞书发送重试与去重权衡（§8 已注）；失败告警抬升策略（§12）；description 长度上限；按源独立调度；多飞书群；冷启动窗口可配置在 feed 粒度。

---

## 14. v0.1 工作清单

状态：⬜ 待办 · ◾ 进行中 · ✅ 完成

### 脚手架
- [x] `pyproject.toml`：依赖 feedparser / httpx / PyYAML / pytest；`[project.scripts] tc-push = "feedkicker.push:main"`
- [x] `config.yaml`：示例内容（webhook 用占位、`bootstrap_days: 3`、2 个示例 feed）
- [x] `.gitignore` 补 `data/`、`logs/`、`config.yaml`（若 webhook 凭据入文件则忽略，凭据走 env 可选）
- [x] 验证：`pip install -e .` 后 `tc-push --help` 可跑

### config.py
- [x] `dataclass`：Config / HttpConf / Feed；默认值 < config.yaml < env 覆盖顺序
- [x] env：`FEISHU_WEBHOOK` 覆盖 webhook、`TC_DB` 覆盖 db 路径
- [x] `--config <路径>` / `--db <路径>` CLI 覆写

### store.py
- [x] 打开连接，自动建 `data/` 目录；`CREATE TABLE IF NOT EXISTS` 两表 + 待推索引
- [x] `download(feed, entries, now)`：INSERT ON CONFLICT DO NOTHING，单事务
- [x] `is_first_run(feed)` / `update_first_run_all(feedes, now)`
- [x] `promise_skip_old(feed, cutoff)`：窗口外置 pushed_at，`published_at IS NULL` 不排除
- [x] `select_pending()` / `mark_pushed(items)` / `bump_fail(feed)` / `clear_fail(feed)`

### fetch.py
- [x] httpx 抓 bytes（UA/超时/2xx 判定）+ `feedparser.parse`
- [x] 归一化 entry：entry_key(guid→canonicalize(link)→title 兜底)、title、url、description（原样）、published_at(iso_utc)
- [x] `canonicalize()`：去 fragment、host 小写、保留 query
- [x] 每源异常捕获由 push.py 层做（fetch 只抛）

### feishu.py
- [x] `escape_inline`：压行 + 转义 `\` `` ` `` `*` `_` `[` `]` `(` `)` `#`
- [x] `build_card(new_items, feed_fails, ...)`：按 feed 分组 + 失败 footer（见 §7）
- [x] `send(payload, webhook, http)`：POST + StatusCode/code 校验；体积由 build_card 保证 ≤20KB（20000 字节）

### push.py
- [x] `main(argv)`：argparse（--dry-run / --config / --db）；顶层 try 兜底退出码
- [x] §6 编排：抓全部→download→首跑窗口→select_pending→build_card→send→mark
- [x] 无待推不发空卡、正常退出
- [x] `--dry-run` 打印 payload 不发
- [x] 退出码：成功 0 / 有失败 1；日志 INFO/WARNING

### 测试（tests/test_push.py）
- [x] 覆盖 §11 全量表（fetch 归一化、key 规则、幂等、窗口、NULL 不排除、二次运行、卡片分组/转义/失败 footer、业务码 mock、无空卡）
- [x] 全部通过：`pytest -q`（17 passed）

### 部署与验收
- [ ] `--dry-run` 用真实 config 联调，核对卡片布局与转义（✅ 冒烟已过：HN 30 条组卡 + 坏源 footer 正确；真实 webhook 联调待办）
- [ ] crontab 写入两条（8:00 / 16:00），重定向日志
- [ ] 真实 webhook 闭环：跑一次收到卡片；再跑一次不重发（PRD §11 #7）
- [ ] 无常驻验证：进程退出无残留、无端口监听（PRD §11 #8）（进程跑完即退已确认，端口验证随 cron 部署复核）
---

## 15. v0.2 设计 — GitHub Pages 详情页 + 摘要卡（2026-08-25）

> **【已废弃 · 2026-08-26 起】** site.py / publish.py 已从代码库移除，`site.enabled` 全环境
> false（AGENTS.md：不要复活）；摘要卡 top_n/详情按钮语义由多维表格承接（§16/§18）。
> 本节仅保留设计记录，勿据此实现。

### 15.1 数据流与顺序保证

```
抓取入库 → select_pending
 ├─ 空 → 不发卡不动网页，退出 0
 └─ 有 → mark_pushed(先标记，页面需含本批)
        → site.render_daily(当天全部已推) → publish gh-pages(daily/日期.html + index.html)
        → wait_published 轮询 URL（3s×20 次）
        → feishu 摘要卡（每源 top_n + 📰 按钮 → 详情页）→ 标已推语义见 §15.4
```

顺序保证：**发布并确认可达后才发卡**——用户点击按钮时页面必然已存在。
轮询超时（Pages 构建慢）照发卡片，极端情况早点击几十秒 404。

### 15.2 site.py 渲染

- **全局去重**：`canonicalize(url)` 相同的条目合并为一条，主归属 = feed_order 中最靠前的源，
  其余源标注「亦见 X + Y」（修复 HN 热榜 ∩ HN AI 高赞跨源重复推送问题）
  （site.py 已废弃；同一规则现由推送侧 `feishu_card.build_card` 在渲染层执行，#289：
  去重先于 top_n 截断与计数，`mark_pushed` 仍按原始 pending 全量标记避免孤儿）
- 按 feed 分组（保 config 顺序）；description `html.escape` 后原样展示；纯 stdlib 字符串模板零依赖
- `render_index`：按日期倒序归档目录（取最近 60 天有数据的日期）

### 15.3 publish.py 发布

- `gh api` Contents API：GET sha 判定新建 vs 更新 → PUT base64 单文件；幂等、无 git 工作区依赖、无冲突
- gh 二进制解析：shutil.which → /opt/homebrew/bin/gh → /usr/local/bin/gh（launchd 环境 PATH 兜底）
- `wait_published(url)`：httpx GET 轮询，200 且非骨架占位即认为生效

### 15.4 飞书摘要卡改造

- 每源最多 `site.top_n` 条（默认 5；**保最新**：按时效键 `published_at or first_seen` 降序取前 N，卡内仍最旧在前，#200），组尾「… 还有 M 条见详情页」；详情页无 20KB 限制
- 底部 action button「📰 查看全部 N 条」+ 同文 markdown 链接行（双保险）
- 发送失败且带按钮时：`strip_actions` 去按钮降级重试一次（防旧版客户端/接口不兼容 action 元素）
- 20KB 兜底保留：top_n 截断后仍超限 → 先剥 description 再丢最旧条目（`selected.pop(0)`，方向与 top_n 保最新一致）
- **语义变更**：site.enabled 时改为 mark_pushed 先于发送（页面内容完整性优先）；
  发送失败不再自动重试本批条目（已上页面），由 §15.5 求救通道兜底可见性。site 关闭时保持 v0.1 语义

### 15.5 失败求救通道

- meta 表记 `push_fail_streak`：发送成功清零；失败 +1
- 连败 ≥3 → `send_text` 往同一群发纯文本求救（msg_type=text，同签名注入）后计数清零防刷屏
- 纯文本不受 interactive 卡片标签限制，卡片通道挂掉时仍可送达

### 15.6 配置新增（config.yaml）

```yaml
site:
  enabled: true        # 遗留字段：site.enabled 已不再被读取（§4 仅保留 top_n）
  base_url: "https://linyunxia.github.io/topic_collection"
  repo: "LinyunXIA/topic_collection"
  branch: "gh-pages"
  top_n: 5
```

DDL 新增 `meta(key,value)` 表（CREATE IF NOT EXISTS，对存量库透明）。

### 15.7 v0.2 验收

1. 卡片带按钮，点击打开当天页面，页面在卡片发出时已可达（轮询保证）
2. 页面跨源去重 + via 标注正确；16:00 班重写含全天内容
3. 发布失败 → 无按钮卡照发；连败 3 次群内收到纯文本求救
4. `TC_SITE_ENABLED=0` 回退 v0.1 行为

---

## 16. 多维表格归档（v0.3 引入 · v0.5 定稿为唯一归档形态）

> 曾在 v0.4 短暂被电子表格方案（§17）取代，v0.5 起恢复并增强为最终形态。
> v0.4 的电子表格文件留置作快照，不再更新。

### 16.1 形式决策

单数据表「文章」+ 默认视图按「来源」分组、组内发布时间倒序——对应原 git page 的
"每 feed 一个分类区块"，同时保留跨源搜索/统计能力。否决按源分多表（碎片化）。

字段：标题(主键 text)/链接(url)/来源(text)/摘要(text)/发布时间(datetime)/推送时间(datetime)

### 16.2 feedkicker/bitable.py

- 基于 `lark-cli base` 子命令封装（subprocess；`shutil.which` + `/opt/homebrew/bin` 兜底）
- lark-cli 业务失败时**退出码仍为 0**，失败信号在 stdout JSON 顶层 `ok:false`（#114）；
  大 payload（200 条/批）走 `--json @./临时文件` 传参绕开 ARG_MAX（#112，stdin/绝对路径均不支持）
- `ensure_initialized`：config 有 token 则跳过；否则 title-resolve 找同名 Base，
  找不到才创建（Base「AI 资讯归档」+ 表「文章」全字段 schema）
- `sync_records`：写入前拉取表内已有链接集合（record-list 分页），
  `canonicalize` 后过滤已存在 + 批内去重（修复跨源重复入表）；≤200 条/批；
  全部批次成功才返回 True
- 时间单元格：ISO UTC → 本地时区 "%Y-%m-%d %H:%M"
- 权限：`permission.public patch --yes`——external_access=false +
  link_share_entity=tenant_readable（仅组织内获得链接者可读）

> v0.5 增强（「环境」列 / 归档日期字段 / 按日期视图 / existing_links 幂等去重 /
> purge_all_records 重灌）见 §18。

### 16.3 存储与编排

- articles 加列 `bitable_synced_at TEXT`（connect 时 PRAGMA 检查自动补列）
- push.py 编排顺序为**先档案后推送**（与 AGENTS.md 一致）：bitable.enabled 且非 dry-run 时
  先 select_unsynced → sync → mark_synced，成功后再推卡片（按钮指向的归档在发卡时已可达）；
  归档任一环节失败仅 WARNING，卡片照发，失败批次保留待重试；dry-run 不触发
- 独立运维入口：`python -m feedkicker.bitable --env prod [--init]`
- 推送链路自动新建（config 无 token 且同名 Base 不存在）的 Base 是**裸表**：分组视图与
  组织内只读需补跑一次 `--init`；既有 Base 的 token/table_id 写在本地 config 中时不涉及
- 配置：`bitable{enabled, app_token, table_id, url}`；三环境均开启（dev/test 共享 Base）

### 16.4 事故记录

首轮回填因 URL 字段不支持 {link,text} 对象形态失败一批（改纯字符串修复），
重试导致 200 行重复 + 1 行跨源重复；已逐行清理。教训：
batch 失败必须整批不打标（已实现），跨源去重必须在写入侧兜底（已实现）。

---

## 17. v0.4 设计 — 飞书电子表格归档【已废弃，回归 §16 多维表格】

> 电子表格平铺形态缺失分组视图/筛选能力，v0.5 起三环境统一回归多维表格。
> 三个 sheets 文件留置作快照；`sheets_archive.py` 模块已移除（git 历史可回溯）。

### 17.1 形式

三环境统一写入飞书电子表格；**每个日期一个工作表 tab**（如 `2026-08-25`），滚动保留 365 天，
更早的 tab 每次写入后自动删除。

| 文件 | 使用环境 | 标题 |
|---|---|---|
| prod 专用 | prod | AI 资讯归档 |
| 共享 | dev + test | AI 资讯归档 · dev-test |

列结构：`环境 | 来源 | 标题 | 链接 | 摘要 | 发布时间 | 推送时间`（首行表头自动写入）。

### 17.2 feedkicker/sheets_archive.py

基于 `lark-cli sheets` 子命令封装：
- `ensure_initialized`：config 无 token 时创建文件并把 token/url **回写本地 config-{env}.yaml**
- `ensure_day_sheet`：当日 tab 不存在则创建并写表头
- `append_rows`：`count_last_data_row`（解析 csv-get 的 annotated_csv `[row=N]` 前缀）自愈定位追加行号；
  `+csv-put --csv -` 走 stdin 批量写入
- `sync_env`：select_unsynced → annotate → **批内 canonicalize(url) 跨源去重** →
  按日分组 → 断点续传（meta 键 `arch_done_{env}_{day}` 记已写行数，重试只补增量）→
  mark_synced → prune_old_tabs
- 权限：组织内链接只读（external_access=false / link_share_entity=tenant_readable）

### 17.3 编排顺序（v0.4 最终版）

```
抓取入库 → 待推队列
  └─ 有 → sync_env 写在线表格（先档案后推送）
        ├─ 成功 → 打标；卡片附「📰 详情见在线表格」按钮
        └─ 失败 → WARNING；卡片仍发仍带链接（表格常驻，仅缺最新几条）
        → 发送成功才 mark_pushed（失败下次重试卡片）
```

- top_n 摘要形态随 archive.enabled 恢复（每源最新 5，20KB 兜底丢最旧）
- dry-run 不写表格、不发送
- 配置段：`archive{enabled, spreadsheet_token, url}`（三环境均已启用；dev/test 共享同一文件，
  以「环境」列区分行）

### 17.4 运维入口

```bash
python -m feedkicker.sheets_archive --env prod [--init]   # [--init] 设置组织内只读分享
```

---

## 18. v0.5 — 回归多维表格 · 视图化组织（2026-08-26 终态）

### 18.1 结构

| Base | 环境 | 说明 |
|---|---|---|
| AI 资讯归档 | prod | v0.3 建，复用 |
| AI 资讯归档 · dev-test | dev + test 共享 | 「环境」text 列区分 |

字段（dev/test 多一列环境）：标题/链接(url)/来源/摘要/发布时间(dt)/推送时间(dt)/**归档日期**(text)

视图：①按来源（group 来源 · 发布时间倒序）②按日期（group 归档日期倒序 · 推送时间倒序）

### 18.2 bitable.py（v0.5 增强点）

- `fields_for(env)` / `create_base(title, env)` / `create_table(token, env)`
- `existing_links` 幂等去重（写入前拉全量链接集合 canonicalize 比对 + 批内去重）
  ——标记列仅作加速缓存，误标可自愈
- `ensure_archive_date_field` 存量 Base 自动补列；`purge_all_records --reseed`
  清空重灌（markdown 解析 record_id 批删）
- `create_date_view` 按日期分组视图自动创建
- `sync_env(bt, env, conn)`：select_unsynced → 去重过滤 → ≤200/批 → mark_synced；
  空 token/table_id 防御性报错

### 18.3 验收记录（2026-08-26）

- prod 重灌 266 条（跨源去重 1）→ 增量 60 条 → 总 324 行与本地一致（差 2 行为去重跳过项）
- dev/test 共享 Base 重灌各 10 行
- 两 Base「按来源」「按日期」双视图就绪；组织内只读复核通过
- 真实发卡：按钮指向对应 Base URL；二跑无新条目不重发

---

## 19. v0.6 — AI 沙龙每周大纲 · 定时与配置（2026-09-03）

### 19.1 目标与边界

- 每周五 10:00（可配置）自动将 Tikp 多维表「AI 沙龙换题管理」(`<salon-app-token>` / `<salon-table-id>`) 中新增的「已选题」增量生成双大纲并入 Wiki
- 仅产 Markdown 大纲（自适应 5–8 页），不产 PPTX；独立进程 `feedkicker.salon_flow`，不混入 `push.py` 编排；配置与调度可验证（`--help` / `launchctl print`）
- 增量语义（F18，#211；#292 修订）：服务端 filter 只返回「已选题」，本流程只对**首次**进入「已选题」且从未处理（`ppt_synced_at IS NULL`）的题目生成；跳过判据**仅** `is_ppt_synced(rid)`（`ppt_synced_at IS NOT NULL`），`ppt_last_status_{rid}` 仅作诊断、**不参与**跳过判定——第三段 `mark_topic_archived` 写 `last_status` 失败不再导致下轮重建 Wiki。

### 19.2 配置（config.yaml 新增段）

```yaml
salon:
  enabled: true
  app_token: "<salon-app-token>"
  table_id: "<salon-table-id>"
  wiki_space_id: "<wiki-space>"
  wiki_parent_token: "<wiki-parent>"
  trigger_weekday: 4        # 仅记录用途；调度以 launchd Weekday=5 为准
  trigger_hour: 10
  trigger_minute: 0
minimax:
  api_key: "<via MiniMax_Key env>"
  model: "MiniMax-M3"
  base_url: "https://api.minimaxi.com"
wiki:
  space_id: "<wiki-space>"
  parent_token: "<wiki-parent>"
  app_token: "<wiki-app-token>"
```

- `config-{dev,test,prod}.yaml.example` 已补充 salon/minimax/wiki 段（dev/test/prod 各一，见仓库根）
- 加载优先级：`--config` 显式路径 > `--env` > `TC_APP_ENV` > 默认 `prod`；`MiniMax_Key` / `TC_SALON_TOKEN` / `FEISHU_WEBHOOK` 环境变量覆盖文件
- CLI 与 `push.py` 一致：`--dry-run` / `--env {dev,test,prod}` / `--config <path>` / `--db <path>`（argparse 定义见 `salon_flow.main`，与 `push.main` 对齐）

### 19.3 调度（launchd，周五 10:00）

```xml
<!-- ~/Library/LaunchAgents/com.feedkicker.salon.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.feedkicker.salon</string>
  <key>ProgramArguments</key>
  <array><string>/Users/linyunxia/PycharmProjects/topic_collection/.venv/bin/python</string>
         <string>-m</string><string>feedkicker.salon_flow</string></array>
  <key>WorkingDirectory</key><string>/Users/linyunxia/PycharmProjects/topic_collection</string>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/linyunxia/PycharmProjects/topic_collection/logs/salon.log</string>
  <key>StandardErrorPath</key><string>/Users/linyunxia/PycharmProjects/topic_collection/logs/salon.log</string>
</dict></plist>
```

- 注意：调度以 launchd 实际值 `Weekday=5`（10:00）为准；代码内 `trigger_weekday`/`trigger_hour`/`trigger_minute` 仅作可配置记录与后续动态生成预留，不参与实际调度
- 加载/校验：`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.feedkicker.salon.plist`；`launchctl print gui/$UID/com.feedkicker.salon` 应含 `Weekday 5 10:00`
- 日志：`logs/salon.log`（与 `push.log` 分流）；手动：`python -m feedkicker.salon_flow --dry-run --env test` 打印双大纲 JSON + wiki_url stub 不写库

### 19.4 入口

- `pyproject.toml [project.scripts] tc-salon = "feedkicker.salon_flow:main"`（与 `tc-push` 并列）
- 现有 `com.feedkicker.push.plist`（8:30/16:00）保持不变，新增 plist 独立运行

### 19.5 Wiki 归档链路（docx，#133 修正）

- `wiki.create_wiki_doc_from_md(app_token, space_id, parent, title, md, dry_run, date_str)`：
  1. MD 写 cwd 临时文件（`.wiki-*.md`，lark-cli `@file` 只接受 cwd 内相对路径）；
  2. `lark-cli docs +create --parent-token <wiki父节点> --title {话题}_{日期}_大纲 --doc-format markdown --content @./.wiki-*.md --json` —— 直接在 wiki 树内创建 **docx** 节点（实测 obj_type=docx），取 `data.document.document_id`；
  3. `lark-cli wiki +node-get --node-token <document_id> --json` 反查 `data.node_token`（进度信息走 stderr，stdout 为纯 JSON）；新建节点秒级内可能 131005 not_found（传播延迟），间隔 3s 重试 1 次；
  4. 返回规范链接 `/wiki/<node_token>`。
- 失败语义：`docs +create` 业务失败（rc=0 但 `ok:false` 或缺 document_id）→ RuntimeError，salon_flow 单条 WARNING 跳过、**不标** ppt_synced；node-get 重试后仍失败但 docx 已建成 → WARNING 并回退 `/docx/<document_id>` 链接（文档可正常打开、不丢已建产物）；节点 obj_type 非 docx → WARNING 但仍返回链接。
- 节点标题 `{话题}_{日期}_大纲`（docx 无扩展名）；`build_filename()` 的 `.md` 名仅用于临时文件。
- **弃用路径**：`drive +upload --wiki-token` 只会产出 obj_type=**file** 的附件节点（标题为临时文件名、`docs +fetch` 报 3380002「Only docx is supported」），`move_docs_to_wiki` 兜底对 file 类型无效——该路径与 `_httpx_move`/`_parse_upload_token` 已移除（#133）。

## 20. v0.7 — 365 天滚动保留自动化（#120，2026-09-07）

### 20.1 模块与 CLI

- `feedkicker/purge.py`（CLI 编排，`tc-purge = "feedkicker.purge:main"`）：argparse 同构范式（`--apply` / `--retention-days` / `--config` / `--db` / `--env`），返回码 2=配置错误、1=异常、0=正常；末尾打印 `PurgeStats` JSON。**默认 dry-run，`--apply` 才真删**。
- `feedkicker/bitable_purge.py`（bitable 侧清理，~130 行，新逻辑不进 bitable.py，见 §21.4）。
- `config.BitableConf.retention_days`（默认 365，`config-{env}.yaml` 的 `bitable.retention_days` 可配，`<1` 静默钳为 1，`>36500` 报错 → rc 2）。

### 20.2 算法

- 截止时点（统一上海日界，#181）：`cutoff_date_shanghai(days)` = 上海时区 `now - days` 的 `%Y-%m-%d` 日期串；sqlite 侧 `cutoff_iso(days)` 取该日期 `00:00 Asia/Shanghai` 的 UTC 瞬时 `%Y-%m-%dT%H:%M:%SZ`（字典序可比，先例 `promise_skip_old`）——两库同一截止日，边界当天条目均保留。
- **sqlite**（`purge_sqlite`）：选 `pushed_at IS NOT NULL AND pushed_at < cutoff`；其中仅 `bitable_synced_at IS NOT NULL`（已在线归档）的行可删，超期未归档只计数 WARNING；dry-run 只计数，apply 才 `DELETE` + commit。salon 占位行 `pushed_at` 为 NULL，天然不匹配。
- **bitable**（`purge_expired_records_outcome`）：`+record-list --json --limit 200 --offset N` 分页拉全表（records 包装 / fields+data 行式双形态兼容，范本 backfill），「推送时间」经 `bitable._cell_str` + `bitable._shanghai_date`（epoch 毫秒/ISO/纯日期兼容；「推送时间」为空时回退「归档日期」，#198）归一成上海日期串；dev/test 共享 Base 时请求带出「环境」字段并**仅删除 `环境 == env_name` 的行**（prod/None 全表不过滤，#208），**客户端过滤** `d < cutoff_date`（字典序；截止当天的记录保留，保守方向）；apply 按 200/批 `+record-delete --json '{"record_id_list":[...]}' --yes`，批失败即终止。返回 `(deleted, expired, scanned)`。
- **安全条件**：bitable 段仅在 `enabled` 且 app_token/table_id 非空且不含 `<`（占位守卫）时执行；**绝不调 `ensure_initialized`**（防误建 Base）；只操作 `cfg.bitable` 资讯归档 Base，不碰 salon 选题 Base；首屏 list 失败返回 `(0,0,0)` 零删除；全量分页读完（`complete`）且删除批全部成功才写 meta `purge_last_run_at`，中途分页失败仍删已扫到的过期行但不写 meta（#180）。bitable 运维 CLI（`python -m feedkicker.bitable`）已落地同口径守卫：**非 `--init`/`--reseed`（含无 flag 与仅 `--backfill`/`--fix-archive-date`）且 token 未就绪/占位 → log.error + rc 2，不自动建 Base**；`--init`/`--reseed` 才允许创建/修复（`--init` 遇 `<...>` 占位 token 亦 rc 2，#262）；`--reseed`/`--backfill`/`--fix-archive-date` 互斥（#224/#229）；`--reseed` 先 reset 同步标记再清表，dev/test 按「环境」列**仅清本环境行**、prod 全清，任一批失败即 rc 2 中止（#218）；dev/test 下「环境」为空或不匹配的行保守保留不删但记 WARNING（#235）。

### 20.3 调度（launchd，每月 1 号 dry-run 巡检）

```xml
<!-- ~/Library/LaunchAgents/com.feedkicker.purge.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.feedkicker.purge</string>
  <key>ProgramArguments</key>
  <array><string>/Users/linyunxia/PycharmProjects/topic_collection/.venv/bin/python</string>
         <string>-m</string><string>feedkicker.purge</string></array>
  <key>WorkingDirectory</key><string>/Users/linyunxia/PycharmProjects/topic_collection</string>
  <key>EnvironmentVariables</key>
  <dict><key>TC_APP_ENV</key><string>prod</string></dict>
  <key>StartCalendarInterval</key>
  <dict><key>Day</key><integer>1</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/Users/linyunxia/PycharmProjects/topic_collection/logs/purge.log</string>
  <key>StandardErrorPath</key><string>/Users/linyunxia/PycharmProjects/topic_collection/logs/purge.log</string>
</dict></plist>
```

- ProgramArguments **不带 `--apply`**：调度只做 dry-run 巡检，PurgeStats 落 `logs/purge.log`；真删由人工看过日志后手动 `.venv/bin/python -m feedkicker.purge --apply --env prod`。
- 加载/校验：`plutil -lint ~/Library/LaunchAgents/com.feedkicker.purge.plist`；`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.feedkicker.purge.plist`；`launchctl print gui/$UID/com.feedkicker.purge | grep -i calendar` 应含 `day = 1, hour = 10, minute = 30`。

### 20.4 清单

- [x] `tc-purge` CLI：默认 dry-run、`--apply`、`--retention-days`，PurgeStats JSON
- [x] sqlite 清理：仅删已归档超期行，未归档超期计数 WARNING
- [x] bitable 清理：分页拉取、客户端日期过滤、200/批删除、首屏失败零删除、批失败终止
- [x] 占位 token / 未启用守卫；不调 ensure_initialized；只清资讯归档 Base
- [x] `retention_days` 配置（dataclass 默认 365 + 三份 yaml.example）
- [x] launchd plist（每月 1 号 10:30 仅 dry-run）创建并 bootstrap
- [x] 测试 `tests/test_purge.py`（13 例，全 mock subprocess）

## 21. v0.6+ — salon 技术债清理与质量门（#131，2026-09-07）

审核 P2/P3 三项行为修复 + 模块拆分与静态质量门。行为修复见 commit 1（`fix(salon)`），结构重构见 commit 2（`refactor`）。

### 21.1 行为修复（OBS1–OBS3）

- **OBS1（P2）卡片连败 SOS**：Wiki 大纲卡片推送从「strip_actions 重试 1 次 + WARNING、`run()` 恒返回 0」对齐 push.py 的成熟模式——失败累计 meta `salon_fail_streak`，连续 3 次且 webhook 非空时发纯文本 SOS（`feishu.send_text`，文案含「连续 N 次」与最近一班 Wiki 已建成的提示）后清零；成功即清零（有旧值记恢复日志）。逻辑收敛在 `salon_notify.send_wiki_card(cfg, conn, wiki_urls, dry_run) -> bool`，`salon_flow.run()` 据返回值返回 0/1（卡片最终失败 rc=1，launchd 记失败，与 push 一致）。
- **OBS2（P2）dry-run 不得计费**：`--dry-run` 无条件使用 `salon_md.stub_outlines(title)` 占位大纲（5 页工具/原理页，标题含「工具类大纲」标记），不再因配置了真实 MiniMax key 而发起计费调用（原先每题 2 次）。测试以「gen_outline 被调即抛 AssertionError」做真守卫（旧守卫的 AssertionError 被 broad except 吞掉而假通过）。
- **OBS3（P3）死逻辑移除**：`_unsynced_keys` 的 if/else 两分支都赋 `ppt_synced_is_null=True`，整段删除；同步判定改为单一来源 `store.is_ppt_synced(conn, rid)`（`ppt_synced_at IS NOT NULL`），死代码 `select_pushed_since` / `select_unsynced_topics` 一并删除；跳过判据仅 `is_ppt_synced`，`ppt_last_status` 仅作诊断、不参与跳过判定（#292，见 §19.1）。

### 21.2 模块拆分（依赖单向无环）

```
store_conn（叶子：sqlite 连接/schema 迁移，WAL + busy_timeout，#239）
  ↑
store_meta（叶子：meta 键值表）
  ↑
store_salon（ppt 同步标记 / last_status / mark_topic_archived）
  ↑
store（articles/feeds 主表 + facade re-export store_conn、store_meta、store_salon）

config_models（叶子：PROJECT_ROOT/DEFAULT_DB_PATH/VALID_ENVS + 全部 dataclass）
  ↑
config（读 config-{env}.yaml + env 覆盖；db_path_for/config_path_for/load_config 仍定义于此）

feishu_host（叶子：租户域名单点）→ feishu_card_body（body 组装）→ feishu_card（构建/转义/strip_actions）→ feishu（HTTP 发送 + facade re-export）
minimax_schema（PROMPT_TEMPLATES / function-calling schema）→ minimax（调用 + facade）
wiki_lark（lark-cli docs/wiki 调用与响应解析）→ wiki（编排 + __main__ CLI）
salon_md（标题/大纲 markdown/stub/状态归一）、salon_notify（卡片 + 连败 SOS）→ salon_flow（编排）
topic_records（响应记录归一）→ topic（分页拉取 + facade re-export）

extract_source（叶子：sqlite 近 N 天 RSS 行选源）
extract_llm（provider 注册表/默认值 + call_llm/build_batch_prompt/parse_topics/merge_topics；复用 minimax 错误码惯例）
extract_write（existing_topics/build_record/write_topics；复用 bitable_lark 进程层 + topic_records 归一）
  ↑
extract_flow（选源→分批→LLM→解析→去重→dry-run/写入 编排 + tc-extract CLI）

bitable_lark（叶子：lark-cli 进程层 _run/_parse/_json_arg/_has_batch_verb/SHANGHAI/页指纹）
  ↑
bitable_schema（Base/表初始化：fields_for/create_base/ensure_initialized）
bitable_views（视图/字段/分享：setup_view/create_date_view/set_tenant_readonly）
bitable_backfill（归档日期解析 _cell_str/_shanghai_date + 回填）
  ↑
bitable_reseed（--reseed 前置清空 purge_all_records）
  ↑
bitable_records（记录读写：existing_links/sync_records/sync_env，re-export purge_all_records）
  ↑
bitable（CLI + facade re-export，§21.2）
```

- **facade 约定**：被搬走的公共函数在原模块以 `from x import y as y` 显式 re-export（抑制 ruff F401 且表明是刻意重导），全部既有调用点（`store.get_ppt_last_status`、`feishu.build_card`、`mm.PROMPT_TEMPLATES` 等 ~25 处）与测试 monkeypatch 目标零改动。
- **monkeypatch 约定**：跨模块调用必须走模块属性访问（`feishu.send`、`bitable_lark._run`、`wiki_lark.time.sleep`），不可 `from x import y` 解包后调用，否则 patch 不生效。bitable 拆分（§21.4）后随之迁移的 patch 点：`bitable._run/_parse/_ok/_data/lark_bin/subprocess/os/SHANGHAI` → `bitable_lark.*`；`bitable.find_base_by_title/create_base/get_table_id/create_table/ensure_initialized` → `bitable_schema.*`；`bitable.setup_view/create_date_view/ensure_archive_date_field/set_tenant_readonly` → `bitable_views.*`；`bitable.existing_links/sync_records/purge_all_records/sync_env` → `bitable_records.*`；`bitable._cell_str/_shanghai_date/backfill_empty_archive_dates` → `bitable_backfill.*`。外部调用点（push/wiki/wiki_lark/wiki_home/topic/bitable_purge）同步改为引用 owner 模块。此前的 `wk.time.sleep` → `wiki_lark.time.sleep` 迁移遵循同一约定。
- 拆分后行数（`wc -l feedkicker/*.py`，2026-09-14 实测）：全部 ≤200；最大 `topic.py` 200；`feishu_card.py` 141 + `feishu_card_body.py` 86（#222 抽取）；`config.py` 138 + `config_models.py` 80（#171 拆分，原先 1 行之差逼近 200 行门）。
- **config 拆分（#171）**：`config_models.py` 承载 `PROJECT_ROOT`/`DEFAULT_DB_PATH`/`VALID_ENVS` 与全部 dataclass（`Config.db_path` 默认值一并迁入，`config` 单向依赖 `config_models`，无环）；`config.py` 以 `from feedkicker.config_models import X as X` 全量 re-export，`db_path_for`/`config_path_for`/`load_config` 仍定义于 `config.py`，故 conftest 对 `config.config_path_for` 与调用方对 `feedkicker.config.load_config` 的 patch 目标不变。
- **包版本解耦（#228 裁定）**：`pyproject.toml` `version` 是安装包版本，与产品/文档 v0.x 解耦、不随文档同步；产品版本以 PRD 为准。

### 21.3 质量门配置

- **ruff 0.16.4**（dev 依赖固定版本）：`target-version="py312"`、`line-length=100`、`lint.select=["E4","E7","E9","F","I","UP","BLE","DTZ","FURB","PLW1510"]`。broad except 统一 `except Exception:  # noqa: BLE001`（子进程/外部 API 边界刻意兜底）；`datetime.now(tz)` 强制时区（DTZ）；`subprocess.run(..., check=False)` 显式（PLW1510）；Py3.12 现代化（UP，含 `from datetime import UTC`、原生 `fromisoformat("...Z")`）。`store.py` facade 成组 re-export 在 per-file-ignores 关 I001。命令：`.venv/bin/ruff check .` → 0 errors。
- **basedpyright 1.39.10**：`include=["feedkicker"]`、`pythonVersion="3.12"`。JSON/子进程边界无静态 schema，`reportAny`/`reportUnknown*`/`reportMissingParameterType`/`reportPrivateUsage`（跨模块调 `bitable_lark._run/_parse` 的架构所需）/`reportExplicitAny`（边界 `dict[str, Any]` 刻意）/`reportUnusedCallResult`（`__main__` argparse 惯例）降级 none；`reportUnusedParameter` 保留 warning。代码侧修完全部 error：裸 `dict`/`list` 一律参数化为 `dict[str, Any]`/`list[dict[str, Any]]`，`CompletedProcess | None` 与 `data.get("node")` 等 None 分支显式收窄。命令：`.venv/bin/basedpyright` → **0 errors**。
- **零整行注释门**：`feedkicker/*.py` 无整行 `#` 注释（`grep -rn '^[[:space:]]*#' feedkicker/*.py | grep -v noqa` 为空）；承载 lark-cli/launchd 踩坑理由的注释转为函数 docstring（如 `bitable_lark._run` 的 launchd PATH 增补 #123、`_json_arg` 的 ARG_MAX、`wiki_lark.lark_node_get` 的 131005 传播延迟）；inline `# noqa: ...` 允许。

### 21.4 拆分记录

- **bitable.py 拆分完成**（#135，2026-09-14）：原 830 行单文件按边界拆为 6 个 ≤200 行模块，行为逐字节保留（240 用例全绿，<1s 全离线），`bitable.py` 收敛为 CLI + facade re-export。边界与函数归属：
  - `bitable_lark.py`（进程层）：`SHANGHAI`/`_shanghai_tz`、`lark_bin`、`_run`、`_parse`、`_ok`、`_data`、`_json_arg`、`_has_batch_verb`、`_markdown_record_ids`、`_guard_offset`/`_page_fingerprint`/`_page_guard`（页指纹防误杀，#232）、`_CHUNK`。
  - `bitable_schema.py`（Base/表初始化）：`BASE_TITLES`/`TABLE_NAME`/`VIEW_NAME` 等常量、`fields_for`、`base_url`、`find_base_by_title`、`create_base`、`get_table_id`、`create_table`、`ensure_initialized`。
  - `bitable_views.py`（视图/字段/分享）：`_view_id`、`setup_view`、`set_tenant_readonly`、`ensure_archive_date_field`、`create_date_view`。
  - `bitable_reseed.py`（reseed 前置清空）：`_delete_batches`、`_markdown_has_data_row`（#242）、`_env_record_ids`、`purge_all_records`（#197/#218；`bitable_records` 以 `from … import … as …` re-export）。
  - `bitable_records.py`（记录读写）：`_cell`、`existing_links`、`sync_records`、`sync_env`（+ `purge_all_records` re-export）。
  - `bitable_backfill.py`（日期解析/回填）：`_cell_str`、`_shanghai_date`、`backfill_empty_archive_dates`。
  - `bitable.py`：`_tokens_ready`、`_dry_run_plan`、`main` + 全量 facade re-export；`bitable.X` 访问与既有调用点保持不变。
- 拆分后新增子模块登记：`feishu_card_body.py`（#222，body 组装/截断提示，`feishu_card` 引用）、`feishu_host.py`（租户域名单点，`bitable_schema`/`wiki` 引用）、`store_conn.py`（#239，连接/WAL/迁移，`store` re-export）、`topic_records.py`（#237，`_extract_records` 容器校验，`topic` re-export）。
- 跨模块调用一律模块属性访问（`bitable_lark._run`、`bitable_schema.ensure_initialized`）；monkeypatch 目标按 §21.2 迁移到 owner 模块。
- **后续新逻辑**：一律进对应子模块，不再堆进 `bitable.py`（如 #120 清理进 `bitable_purge.py`）。

### 21.5 清单

- [x] OBS1 卡片连败 SOS + `run()` 返回码对齐（salon_notify.py）
- [x] OBS2 dry-run 强制 stub 大纲、测试真守卫（salon_md.stub_outlines）
- [x] OBS3 `_unsynced_keys` 死逻辑与死代码删除、`is_ppt_synced` 单一判定
- [x] 模块拆分：store_meta / store_salon / feishu_card / wiki_lark / minimax_schema / salon_md / salon_notify，facade re-export 保持调用点
- [x] ruff 0.16.4 配置入 pyproject，`ruff check .` 0 errors
- [x] basedpyright 1.39.10 配置入 pyproject，0 errors
- [x] 全部模块（含 bitable.py）≤200 行；零整行注释
- [x] bitable.py 拆分为 lark/schema/views/records/backfill 子模块 + facade（#135）

## 22. v0.7+ — Wiki 首页自动索引（#137 / F23，2026-09-08）

Wiki「首页」节点（space `<wiki-space-id>`，parent/home node `<node_token>`）原本是知识空间模板占位内容；沙龙大纲 docx 虽是其子节点，但主页无任何入口。本特性把主页**整篇 overwrite** 为程序生成的索引页（用户明确：原模板内容抹去、不保留、不做局部区块替换）。

### 22.1 数据流与版式

```
wiki +node-list（space + parent_node_token，--page-all）
  → data.nodes[] 过滤 obj_type=docx 且标题匹配 ^(?P<topic>.+)_(?P<date>\d{4}-\d{2}-\d{2})_大纲$
  → 按日期降序（同日话题升序）
  → build_home_md：# 标题 + 重建说明引用行 + 按月 ## YYYY年M月 大块（最新月在最上，月不补零）
  → docs +update --command overwrite --doc <首页 node token> --doc-format markdown --content @./.wiki-home-*.md
```

生成版式（pipe table 经 lark-cli 导入为飞书原生表格）：

```markdown
# AI 沙龙双大纲归档

> 本页由 feedkicker 每周五自动重建（最近更新 YYYY-MM-DD），共 N 篇。

## 2026年9月

| 生成日期 | 文件名 | 链接 |
|---|---|---|
| 2026-09-08 | 话题名（纯文本，无超链接） | [打开](https://…/wiki/<node_token>) |
```

- **数据源选 node-list 而非 sqlite**：node-list 反映 Wiki 实况且即时可靠；node-get 对新建节点有 131005 传播延迟（§19/§21 已记录），sqlite 里还可能留着 /docx/ 回退链接。node-list 直接给规范 node_token。
- **文件名是纯文本话题名**（不带超链接），超链接只在「链接」列；表格单元格经 `_md_cell` 去换行、竖线转义。

### 22.2 模块与调用点

- `wiki_lark.py` 加 3 个薄封装：`lark_node_list(space_id, parent)`（`wiki +node-list --page-all --json`，timeout 120）、`parse_node_list(proc)`（读 `data.nodes`，注意实测字段是 **nodes 不是 items**；失败/空返回 `[]`）、`lark_doc_overwrite_md(doc_token, rel_path)`（`docs +update --command overwrite`；成功输出非 JSON，`bitable._parse` 以 rc 判定，故不带 `--json`）。
- `wiki_home.py`（新，171 行）：`TITLE_RE`、`list_outline_docs`（node-list 失败抛 RuntimeError）、`build_home_md`（纯函数，`now` 可注入便于测试）、`update_homepage(space_id, parent, dry_run, now) -> bool`（任何失败仅 WARNING 返回 False；dry-run 打印预览不写；临时 md 走 cwd 相对路径 `./.wiki-home-*.md`，finally 删除）、`main(argv)` CLI（`--env/--config/--db/--dry-run`，rc 2 配置错 / 1 异常或更新失败 / 0 成功；space/parent 取 `cfg.wiki.*` 回退 `cfg.salon.wiki_*`）。
- `salon_flow.run()`：卡片推送之后、return 之前，`if wiki_urls and wiki_space and wiki_parent:` 调 `wiki_home.update_homepage(..., dry_run=dry_run)`，外层 broad except 兜底——**主页失败不影响主流程返回码、不触发 SOS**（文档与卡片已成才是主产物）。dry-run 也调用（dry_run=True 打印预览）。
- `salon_flow.run()` 前置守卫：wiki space/parent 为空或占位且非 dry-run 时，在生成大纲前直接 WARNING 跳过建 Wiki 与标记、rc 0（不产生孤儿 docx，#219）；全部话题失败也仅 WARNING，rc 不变（#229）。

### 22.3 运行方式

```bash
.venv/bin/python -m feedkicker.wiki_home --dry-run --env prod   # 预览，只读 node-list 不写
.venv/bin/python -m feedkicker.wiki_home --env prod             # 手动重建/存量回填（prod 写）
```

salon 周五 launchd 班有新文档时自动重建，无需新 plist。

### 22.4 清单

- [x] wiki_lark：lark_node_list / parse_node_list（data.nodes）/ lark_doc_overwrite_md
- [x] wiki_home.py：list_outline_docs / build_home_md / update_homepage / CLI
- [x] salon_flow 卡片后接入，失败仅 WARNING；dry-run 预览
- [x] tests/test_wiki_home.py（13 用例，subprocess 全 mock）；既有 sf.run 测试 autouse 打桩 update_homepage 防真实子进程
- [x] ruff / basedpyright 0 errors，319 用例全绿，模块 ≤200 行
- [x] 合并后人工执行一次 `wiki_home --env prod` 存量回填并核对主页渲染（3 篇，2026年9月表格）—— 执行状态待用户确认（截至本次裁决未核实）（2026-09-14 执行并复核，prod 重建 10 篇索引）

---

## 23. v0.7+ — 项目文档三件套（F24–F27，2026-09-14）

面向运维补三份项目文档：`README.md`（入口）、`docs/CLI.md`（命令详解，核心）、`docs/OPS.md`（运维手册）。均为纯文档，**不改任何 `feedkicker/*.py` / `tests/*` / `pyproject.toml`**；与代码行为的一致性靠 `--help` 实跑 + 源码核对（F27 独立自检）。

### 23.1 交付物清单

| 交付物 | 路径 | 定位 | 对应功能 | PRD |
|---|---|---|---|---|
| 项目总览 | `README.md`（根） | 新读者入口：定位/环境/安装/配置概览/快速上手/命令总览/导航 | F24 | §20 |
| 命令行详解 | `docs/CLI.md` | 8 命令 × dev/test/prod，参数/退出码/副作用/dry-run/错误码 | F25 | §20 |
| 运维手册 | `docs/OPS.md` | 配置与凭据、launchd 定时、飞书三坑、排障、环境纪律 | F26 | §20 |

### 23.2 各文档定位与结构

- **README（F24）**：一句话定位 → 架构一句话（链 §1）→ 运行环境（Python ≥3.12 / 仓库内 `.venv` / 外部 `lark-cli` 已登录）→ 安装（`pip install -e .[dev]`）→ 配置与凭据概览（三份 `config-{env}.yaml` + 覆盖顺序一行 + 细节链 OPS）→ 快速上手（dev `--dry-run` 跑通 `tc-push`）→ 8 命令总览表（锚点链 `docs/CLI.md`）→ 目录导航（README/CLI/OPS/PRD/DESIGN/AGENTS）。保持入口性，不铺开逐命令细节。
- **CLI（F25，核心）**：顶部「通用约定」（env 覆盖顺序 `--db` > `TC_DB` > `--env` > `TC_APP_ENV` > prod；`--env dev|test|prod`；`--config`/`--db`；**prod 示例一律 `--dry-run`**、真跑单列标 ⚠️；脱敏规则）。每命令一节：① 用途 + DESIGN 章节号；② 参数表（flag / 类型 / 默认 / 覆盖关系 / 说明，取自 argparse）；③ 环境差异（config / `data/tc-{env}.sqlite3` / 凭据）；④ dev/test/prod 三示例（示意输出 + 退出码 + 副作用）；⑤ `--dry-run` 示意输出；⑥ 注意/坑。附录：错误码对照表（11246 / 131005 / >20KB，取自代码与 AGENTS.md，不臆造）。
- **OPS（F26）**：① 配置（三份 yaml 字段对齐 `config_models.py` dataclass + `.example` 引用 + 覆盖顺序 + db 分流）；② 凭据（`FEISHU_WEBHOOK`/`FEISHU_SECRET`/`MiniMax_Key`/`TC_SALON_TOKEN`；yaml gitignored；prod 与 dev-test 双 Base，dev/test 共享文件用「环境」列区分）；③ launchd 三 plist（push 8:30/16:00、salon 周五 10:00、purge 每月 1 号 10:30 仅 dry-run）+ `launchctl bootout && bootstrap`；④ 飞书三坑；⑤ 排障（症状→排查→处置）；⑥ 环境分级纪律（prod 默认禁写；purge `--apply` 必须人工）。引用 §4/§8/§9/§16/§20 + AGENTS.md。

### 23.3 清单

- [x] F24 `README.md`：定位/环境/安装/配置概览/快速上手/命令总览表/目录导航
- [x] F25 `docs/CLI.md`：8 命令详解 + dev/test/prod 示例 + 错误码附录（参数/默认/退出码经 `--help`+源码核对）
- [x] F26 `docs/OPS.md`：配置/凭据/launchd/飞书三坑/排障/环境纪律
- [x] F27 三件套一致性自检（独立后续任务，2026-09-14 完成）

**F27 自检结论（2026-09-14）**：六项检查（参数/退出码、脱敏、三环境、交叉引用、prod 示例、与代码一致）全部 PASS，CLI.md 回修 5 处示例；自检记录 `.omo/evidence/docs-trio/f27-selfcheck.log`（本地，gitignored）。

---

## 24. v0.7+ — 第四轮审计修复（#217–#229，2026-09-14）

第四轮全量审计（4 车道；P0=0 / P1×3 组 / P2×8 / P3×3 组）的修复落在三 commit（P1/P2/P3）与对应 PR。
行为修复见各 issue 与 PR 描述；本节记录**不改行为的取舍与语义说明**（#229 裁定，与代码 docstring 互补）。

### 24.1 已知取舍（明示语义，不改变行为）

- **并发 `--reseed` 无互斥**：两个操作员同时跑会在清理窗口互抢（重复行/半清）；本机单进程运维，按运维纪律单点执行，不加锁（`bitable.main` docstring 同注）。
- **卡片 20KB 裁剪语义**：先剥 description，再丢**全局 time_key 最小（最旧）**者，与源分组拼接顺序无关；footer「已截断 N 条旧条目」方向正确（`feishu_card.build_card` docstring 同注，#233 修正 §24.1 原 `pop(0)` 近似）。
- **`existing_links` 跨环境去重**：共享 Base 下不按「环境」过滤链接 → 另一环境已归档的同一 URL 不在本环境重复写（test 视图缺行，非数据丢失）；按环境 `--reseed` 后收敛。
- **`canonicalize` 键规则漂移**：省略默认端口/保留 userinfo 等归一变更会让 guid-less 源旧行 `entry_key` 与新 key 不一致，升级首轮可能重复推卡一次（一次性影响）。
- **`is_ppt_synced` 无 feed 过滤**：仅按 `entry_key` 判定，理论碰撞才误伤；实际 `entry_key` 为 URL/guid，不会跨源碰撞。
- **salon 全失败返回码不变**：`selected` 非空且**有尝试**但 0 条成功时仅 `log.warning`（salon_flow），rc 仍 0，不触发 SOS；稳态全跳过（去重命中）不再误报（#237）。
- **接受项（记录不修）**：① 超大 `detail_url` 时 `build_card` 不保证 ≤20KB（`detail_url` 由 config 控制、现实值远小于预算；本轮只保证常规条目路径 ≤20KB，#239）；② `is_ppt_synced` 无 feed 过滤（理论碰撞，见上）；③ `select_pending` 无 lease + `mark_pushed` 无条件（数据流见 §1）→ 并发/人工重叠可能重复推送，需运行级锁方免，本机单进程运维下视为取舍；④ `#265/#274` 对「rc0 非 JSON / `data=={}`」硬 raise（安全方向：宁可中止也不误删/误写）。

### 24.2 第五轮审计修复语义补充（#231–#239，2026-09-14）

- **分页防死循环改为页指纹**（#232/#243/#245）：`bitable_lark._page_guard` 以本页 record id 集合的 sorted 指纹比对上一页，相同即判 `--offset` 被忽略并中止（行序抖动不再漏检）；（#290 起）`bitable_lark.guard_pages` 以 `_MAX_PAGES`=1000 做页数上限（与 `--limit` 无关，各分页路径统一调用）叠加 `_guard_offset` 的 `_CHUNK × _MAX_PAGES` = 20 万 offset 天花板；`topic.fetch_selected_topics` 另对「空页 + `has_more` 恒真」第 2 页即熔断。（#303）指纹**仅基于 record id**（`records`/`items`/顶层 ids），无 id 返回 `""` 不熔断——原先的 `data` 行内容哈希会让「同值满页」的均匀表被误判为未翻页而永久无法归档/清理，有界性交给 `guard_pages`。`bitable --backfill` 异常捕获后 log.error + rc 2（不再冒 traceback）；缺 lark-cli 时 `bitable.main` 非 dry-run 路径提前 rc 2、`backfill_empty_archive_dates` 直接 raise（不再静默 rc0，dry-run 预览降级 WARNING）。
- **salon 逐题隔离**（#234）：`build_combined_md` 纳入逐题 try，`outline_to_md` 对 `slides`/`bullets`/`speaker_note` 类型归一（slides 非列表显式 raise 由逐题 try 跳过），单条坏 LLM 响应只 WARNING，不拖垮整批、不丢通知。
- **reseed/markdown/existing_links 健壮性**（#235/#242）：dev/test reseed 对「环境」为空/不匹配行保守保留并 WARNING（§20）；prod markdown 路径仅当存在数据行却解析零 record id 时判 `ok=False` 中止（整页删净后重拉只剩表头属正常空表，`ok=True` 不再误阻断，#242）；fields+data 行式缺「链接」字段且有行时 raise（不静默空集）。
- **脱敏 canary 哈希化**（#236）：真实 prod record id 不再以明文（含拼接）留在 tracked；测试改为 sha256 比对 + 长 token 无匹配断言，非 git 工作树显式失败（OPS §2.2 同口径记录）。
- **边界**（#237/#244）：topic 响应容器异常（顶层非 dict / records 非空但非 list[dict] / data 非 list）抛 `RuntimeError` 中止，真正空页才返回 `[]`（#244 收紧 #237 的「空页 + WARNING」吞错）；salon 记录 skipped/attempted 计数；缺 lark-cli 时 `_run` 返回 None、`wiki.main` 统一 rc 2 不 traceback；`wiki_home` space/parent 复用「空或含 `<`」占位守卫 rc 2；minimax 成功码 `"0"` 归一为 0（`_parse_outline_from_response` 复用同款归一，#245）。
- **配置/并发**（#239）：`feishu_webhook`/`feishu_secret` 的 `<...>` 占位在 `load_config` 统一清空（send 层判空即跳过）；`store_conn.connect` 设 `busy_timeout=5000` + `journal_mode=WAL`，ALTER 迁移容忍 duplicate column。
### 24.3 第六轮审计修复登记（#262–#283，2026-09-14）

第六轮全量审计（P2×7 + P3×15）修复落于 commit #286（P2 批 + P3 批 + 独立验证补遗）。行为要点：base 解析 / `--init` 占位 rc2（#262）；空大纲守卫（#263）；extract 去重索引（#264）；响应容器异常硬 raise（#265）；分页/解析健壮性（#269/#270/#271）；dry-run 可见性（#272）；`bootstrap_days` 下限（#273）；`fields+data` 行式守卫（#274/#275）；`fields` 非 `list[dict]` 统一 raise（#276）；`has_more` 归一（#277）；`sqlite_expired_archivable` 巡检可见（#278）；`detail_url` 防御（#279）；`salon.enabled=false` 可见（#280）；占位 token 守卫（#281/#282）；dry-run 零写同源（#283）。

---

## 25. v0.8 — 资讯→选题 LLM 提炼（F28–F37，#247）

> 编号说明：登记提交时 §24 已被「第四轮审计修复」占用，故 v0.8 设计落在 §25。

把最近 N 天（默认 7）的 RSS 资讯分批交给 LLM **先整合去重、再提炼**候选话题，写入 salon 选题表（`cfg.salon`）。默认 dry-run 打印完整待写清单，`--apply` 才写表——落实提示词的「写前确认」要求。

### 25.1 数据流

```
select_source(conn, since_days, limit)    # ppt_synced_at IS NULL 且 COALESCE(published_at,first_seen) >= cutoff
  → 按 batch_size 切批                     # cutoff = UTC now − N 天（ISO 秒级，与库内同格式）
  → call_llm(cfg.extract, prompt)          # provider 分派：minimax / deepseek（OpenAI 兼容）
  → parse_topics(raw) → merge_topics       # 容错 JSON、缺字段整批弃、同话题链接/来源去重
  → existing_index(app_token, table_id)    # 按「资讯链接 OR 话题名称」双键去重（幂等）
  → dry-run 打印完整清单 / --apply: +record-batch-create ≤200/批
```

### 25.2 模块划分（依赖单向）

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `extract_source.py` | sqlite 选源 | `select_source(conn, since_days, limit=None, now=None)` |
| `extract_parse.py` | 提示词构建与 JSON 解析（自 extract_llm 拆出，#250） | `build_batch_prompt(template, items)`、`parse_topics(raw) -> (list[dict], dropped)`、`merge_topics(topics) -> list[dict]` |
| `extract_llm.py` | provider 抽象 + 批量提炼编排 | `call_llm(cfg, prompt) -> str`、`refine_batches(ex, template, batches, max_calls) -> (topics, calls, failed, empty)`、`resolve_provider(cfg, name=None)` |
| `extract_write.py` | 字段映射与写入 | `existing_index(app_token, table_id) -> tuple[set[str], set[str]]`、`build_record(topic, provider_label, run_date, status="未讨论") -> dict`、`write_topics(...) -> tuple[int, int, int]` |
| `extract_flow.py` | 编排 + CLI | `run(cfg, conn, *, apply, since_days=None, limit=None, batch_size=None, max_calls=None, provider=None) -> int`、`main(argv) -> int` |
| `extract_report.py` | dry-run 清单与运行统计输出（自 extract_flow 拆出，#252） | `print_dry_run(planned, skipped)`、`print_summary(stats)` |

- provider 注册表（`extract_llm.PROVIDERS`）：`minimax`（base_url `https://api.minimaxi.com/v1`、model `MiniMax-M3`、key env `MiniMax_Key`/`MINIMAX_API_KEY`、tool_label `MMax`）、`deepseek`（base_url `https://api.deepseek.com/v1`、model `deepseek-chat`、key env `DEEPSEEK_API_KEY`、tool_label `DS`）；`tool_label` 必须是 salon 表 `提取工具` select 字段的**表内已有选项**（`MMax`/`DS`，`飞书` 留给人工路径）；yaml `providers.<name>` 非空字段覆盖注册表默认。
- LLM 传输/解析下沉（#346）：`minimax_transport.py`（`call_minimax_chat`/`_extract_code`/`_norm_code`/`_RETRY_CODES`/`_resolve_api_key`）、`minimax_parse.py`（`parse_outline_from_response`，解析前复用 `reasoning.py` 的 `strip_reasoning` 并回退 `reasoning_content`）；`minimax.py` 仅保留 `gen_outline` facade 并 re-export `httpx`/`PROMPT_TEMPLATES`/`_TOOL_GENERATE_PPT_OUTLINE`/`call_minimax_chat` 等既有 patch 点；`extract_llm` 复用其错误码归一。
- 调用形态统一 OpenAI 兼容 `POST {base_url}/chat/completions`，取 `choices[0].message.content` 原始文本返回；`_post_chat` **单次尝试**：超时/HTTP 429/529/业务可重试码（1002/1004/1039）抛可重试 `RuntimeError`，重试仅由 `refine_batches` 外层做 1 次（总 HTTP ≤2/批，单层重试，PRV-8）；缺 key/占位 key 抛 `RuntimeError` 且**不发起 HTTP**。

### 25.3 配置（`extract:` 段）

```yaml
extract:
  enabled: true
  since_days: 7
  batch_size: 30
  provider: minimax
  prompt_file: prompts/extract.md
  max_calls: 0          # 0 = 不限
  providers:
    minimax:
      api_key: "<MiniMax_Key env>"
      model: "MiniMax-M3"
      base_url: "https://api.minimaxi.com/v1"
      tool_label: "MMax"
    deepseek:
      api_key: "<DEEPSEEK_API_KEY env>"
      model: "deepseek-chat"
      base_url: "https://api.deepseek.com/v1"
      tool_label: "DS"
```

- dataclass：`ExtractConf{enabled, since_days, batch_size, provider, prompt_file, max_calls, providers: dict[str, ProviderConf]}`；`ProviderConf{base_url, model, api_key, tool_label}`（`config_models.py`）。
- key 覆盖：`providers.<name>.api_key` 缺失时按 provider 取 env（`MiniMax_Key`/`MINIMAX_API_KEY`、`DEEPSEEK_API_KEY`）；`<...>` 占位清空（与 `minimax.api_key` 同口径）。
- `prompt_file` 为仓库根相对路径（`config.PROJECT_ROOT / prompt_file`），默认 `prompts/extract.md`。

### 25.4 提示词与 JSON 契约

- `prompts/extract.md` 原样收录用户 4 条提示词（五要素 / 过滤营销与无工具纯新闻 / 无法提炼即跳过 / 写前确认），并追加「输出必须为 JSON」的 schema 段与分批输入说明。
- 解析前剥离推理块：`reasoning.strip_reasoning` 移除成对 `<think|thinking|reasoning>` 块（含嵌套与属性/空白，字符串字面量内标签保留，#331）；仅残留裸开标签时自该处截断，未闭合推理不可能含完整 JSON（#321/#323）。
- 输出契约：`{"topics":[{"话题名称":"","可使用工具":"","相关AI原理":"","资讯链接":[""],"出处来源":[""]}]}`。
- `parse_topics`：容忍 ```json 围栏；JSON 非法、顶层非对象、`topics` 非列表 → raise `ValueError`（调用方重试 1 次，两次都失败才计 failed 并 WARNING 跳过，不抛到运行级）；单个 topic 非对象或缺 5 键 → 丢弃该条、`dropped` 计数并 WARNING，不整批弃（PRV-6）；`资讯链接`/`出处来源` 接受 str（归一为单元素列表）或 list。
- `merge_topics`：按「话题名称」NFKC 归一 + strip + casefold 的比较键合并（写入保留首个原值）；`可使用工具`/`相关AI原理` 首个非空保留；`资讯链接`/`出处来源` 顺序拼接去重（写入时以换行 join）。

### 25.5 去重

- 写入前 `existing_index` 分页拉目标表 **`话题名称` + `资讯链接`** 两列（同页一次拉取），返回 `(归一话题名集合, 归一链接集合)`；名称按 `topic_key`（NFKC+strip+casefold）、链接按 `link_keys` 归一（`_page_guard` 防死循环；响应兼容 records 与 fields+data 两形态，容器异常 raise 中止写入而非静默空集）。
- **链接归一 = 去 markdown 包裹 + 拆行 + 去 tracking 参数**（`link_keys`，`existing_index` 与 `write_topics` 共用）：表内 `资讯链接` 真实值常是 **markdown 链接包裹 + 换行拼接** 的单字符串且带 tracking 参数（如 `[<url1?utm_source=rss>\n<url2>](<url1?utm_source=rss>\n<url2>)`），而 LLM 输出的是不含 utm 的裸 URL 列表——旧 `canonicalize(整串)` 把 `[...](...)`+换行+utm 当一个 URL → 永不命中（真跑 `skipped=0` 已证）。故先取 markdown 链接目标（target）URL、按空白（含换行）拆成多个 URL，再对每个 URL `canonicalize` 后剥 tracking 参数（键名小写以 `utm_` 开头或属 `{spm,from,fbclid,gclid,ref,ref_src,source,mc_cid,mc_eid}`），其余 query 按名排序重建；无法解析则原样 canonicalize。
- **按 资讯链接 OR 话题名称 双键去重**：命中任一既有键（或本批已出现）→ 跳过；两者皆无才写，重复运行不新增重复行（幂等）。动机：LLM 命名非确定性——同一新闻重跑会产出不同「话题名称」，仅按名去重会漏判并重复落表（真跑已证）；链接键是跨命名的稳定兜底，且 `link_keys` 保证 `#frag`/host 大小写/tracking 参数等形态差异不逃逸。`--update` 刷新既有行本期不做。
- **去重规划抽为 `plan_writes(topics, provider_label, run_date, existing_names, existing_links)` → `(将写入记录, 将跳过记录)`**：`write_topics` 真写与 dry-run 打印**共用同一规划**，保证清单标注、summary `pending`/`skipped` 与实际写入三者同源一致（F35）。
- `讨论状态` / `提取工具` 均为单选 select，按 lark-cli select CellValue 协议**一律写单元素数组**：`["未讨论"]` / `[provider_label]`（`base +record-batch-create --help` Tips 明确 select CellValue 恒为数组，`multiple=false` 时也须数组；写字符串会被服务端拒）。取值须为表内已有选项（`讨论状态`：`未讨论`/`已选题`/`不选择`/`待继续评估`；`提取工具`：`MMax`/`DS`），写表外新值被拒 `800030005 Provide an existing option value`（真跑已证）。不再读 `+field-list` 字段元数据判形态（真跑已证伪，PRV-1）。

### 25.6 CLI（`tc-extract`）

```
tc-extract [--apply | --dry-run(默认)] [--since-days N] [--limit N] [--batch-size N]
           [--max-calls N] [--provider {minimax,deepseek}] [--env dev|test|prod]
           [--config PATH] [--db PATH]
```

- 默认 dry-run：打印合并后的待写清单（逐条 `[将写入]`/`[已存在跳过]` 前缀标注，数量与 summary `pending`/`skipped` 同源一致）与统计（批数/调用数/话题数/将写/将跳过/失败写 `failed_writes`/失败批/空批 `empty_batches`），**零写调用**。
- `--since-days` 取值 1..3650（对齐 `extract_source.MAX_SINCE_DAYS`，#269）；`--batch-size` 取值 1..200（`config.MAX_BATCH_SIZE`，#335）；越界 rc 2，不发 HTTP。
- `--provider`：单次运行覆盖 `extract.provider`（缺省取配置，默认 `minimax`）；choices 由 `extract_llm.PROVIDERS` 注册表键动态给出；`run` 用 `dataclasses.replace` 构造有效 `ex`，`resolve_provider`/`refine_batches`（内含 `call_llm`）与 `提取工具` 均取该 provider（minimax→`MMax`，deepseek→`DS`），运行日志打印实际 provider。
- `--apply`：写入 `cfg.salon.app_token/table_id`（不调 `ensure_initialized`，不改表结构）。
- 退出码：2 配置错（config 加载失败 / prompt 文件缺失 / `--provider` 未知 / provider 未注册 / 所选 provider 缺 key（配置与 env 均无，不发起 HTTP）/ salon token 占位或缺失 / `--batch-size` 越界）；1 未捕获异常，以及 `--apply` 全部写入失败（`failed_writes>0 且 written==0`，#358）；0 正常（含部分批/部分写入失败跳过）。

### 25.7 失败语义

- 单批**第 1 次尝试失败（调用异常 或 JSON/契约解析失败）→ 编排层重试 1 次**（`_post_chat` 单次尝试 + `refine_batches` 外层单层重试；「调用 + 解析」共享同一重试预算，总 HTTP ≤2/批），仅两次都失败该批计 failed 跳过、计数并在结束汇总 WARNING，不阻断其余批、rc 仍 0。
- 模型合法返回 `{"topics":[]}`（无话题）→ 计 `empty_batches`（summary 字段）而非 failed；两次尝试后 JSON/契约解析仍失败才计 failed（PRV-2）。
- `max_calls > 0` 时每次调用前检查，达限停止剩余批并 WARNING（联调护栏）；已在重试中执行过至少一次调用却未能完成的当前批计入 failed（#334）。
- 写入分块失败：该块 WARNING、继续后续块，返回实际成功计数（`failed_writes`）。

### 25.8 清单

- [x] F28 `extract_source.py`：`ppt_synced_at IS NULL` + `COALESCE(published_at, first_seen) >= cutoff` + 升序/limit
- [x] F29 `extract:` 配置段 + `extract_llm.call_llm` provider 抽象（minimax/deepseek），缺 key 明确报错
- [x] F30 `prompts/extract.md` + `build_batch_prompt`/`parse_topics`/`merge_topics`
- [x] F31 `extract_write.py`：字段映射 + 「话题名称」去重 + ≤200/批；dry-run 零写
- [x] F32 `tc-extract` CLI + CLI.md/OPS.md 文档 + 测试（全 mock 离线）
- [x] F33 `prompts/extract.md`：过滤仅版本/发布类公告，`可使用工具` 不得是模型名/版本号（#255/#256）
- [x] F34 `prompts/extract.md`：增加「现场可演示」过滤（排除复杂/专有环境等不可演示工具，#257）
- [x] F35 `refine_batches` 解析失败重试 1 次 + dry-run 清单 `[将写入]`/`[已存在跳过]` 标注（#258）
- [x] F36 `prompts/extract.md`：`可使用工具` 不得为评测基准/榜单/数据集（#259）
- [x] F37 `tc-extract --provider {minimax,deepseek}` 单次运行切换 provider（#261）

---

## 26. v0.9 — 话题自动打分（F38–F42）

对「沙龙话题清单」（`cfg.salon`：`app_token=TikpbwV0oaFAnYsoMCxchMRyncr` / `table_id=tblNPcbupKIBzLAx`）**全部行**逐条自动打分：六维各 0–5（0.5 档）→ 加权总分 0–5（1 位小数）→ 写回 `MMax打分`/`MMax理由` 或 `DS打分`/`DS理由`。默认 dry-run 打印清单，`--apply` 才写表；**幂等只补空**。产品约束见 PRD §22。

### 26.1 架构与数据流

```
score_source.read_rows(app_token, table_id, limit)   # 全表分页读 5 个输入字段（可选过滤 --limit）
  → score_source.group_batches(rows, MAX_SCORE_BATCH=100)   # 组批，单批恒 ≤100
  → score_llm.build_prompt(template, batch, prior_scores)   # 注入本批 + 「已打分参考」横向上文
  → score_llm.call_llm(provider_conf, prompt)               # 复用 extract_llm.PROVIDERS/resolve_provider
  → score_parse.parse_scores(raw)                           # strip_reasoning → JSON 契约解析
  → score_parse.normalize(scores)                           # 闸门筛除 / 缺失归一 / 致命否决 / 分布校验
  → dry-run 打印清单 / --apply: score_write.write_scores(...)  # 只补空（或 --force）写 2 列 + 统计
```

- 读表走 lark-cli（`base +record-list` 分页，与 `extract_write.existing_index` 同封装），**不调 `ensure_initialized`、不改表结构**。
- LLM 调用统一 OpenAI 兼容 `POST {base_url}/chat/completions`，取 `choices[0].message.content` 原始文本。
- 归一后按 PRD §22.7 做**分布校验**（统计 `≥4.0` 占比与 `<2.0` 占比），越界只 WARNING 提示、不阻断。

### 26.2 模块划分（依赖单向，每个文件 ≤200 行）

| 模块 | 职责 | 关键接口 | F |
|---|---|---|---|
| `score_source.py` | 读目标表全表行、组批（≤100）、行字段归一 | `read_rows(app_token, table_id, limit=0) -> list[dict]`、`group_batches(rows, size=MAX_SCORE_BATCH) -> list[list[dict]]` | F38/F39 |
| `score_llm.py` | provider 调用 + 提示词注入（横向上文） | `build_prompt(template, batch, prior_scores) -> str`、`call_llm(provider, prompt) -> str` | F38/F39 |
| `score_parse.py` | 契约解析、闸门/否决/缺失归一、分布校验 | `parse_scores(raw) -> list[dict]`、`normalize(scores) -> (list[dict], DistCheck)` | F40 |
| `score_write.py` | 目标列存在性校验、只补空/`--force` 写入、统计 | `ensure_columns(...)`、`plan_writes(scores, existing) -> (write, skip)`、`write_scores(...) -> ScoreStats` | F41 |
| `score_report.py` | dry-run 清单与运行摘要打印 | `print_dry_run(planned, skipped)`、`print_summary(stats)` | F40/F41 |
| `score_flow.py` | `tc-score` 编排 + CLI | `run(cfg, *, apply, provider=None, limit=0, max_calls=0, force=False) -> int`、`main(argv) -> int` | F38–F41 |

- provider 注册表**直接复用** `extract_llm.PROVIDERS` 与 `resolve_provider`（含 `_post_chat` 错误码归一、`_resolve_api_key` 缺 key/占位 key 校验），不另起一套；`minimax → MMax打分/MMax理由`、`deepseek → DS打分/DS理由`（列映射常量在本模块，PRD §22.8）。
- **约束**：`feedkicker/score_*.py` 每个 ≤200 行（`wc -l`），新逻辑进对应子模块，不堆进 `score_flow.py`。

### 26.3 配置（`score:` 段）

```yaml
score:
  enabled: true
  provider: minimax        # minimax | deepseek
  prompt_file: prompts/score.md
  max_calls: 0             # 0 = 不限
```

- dataclass：`ScoreConf{enabled, provider, prompt_file, max_calls}`（`config_models.py`）；provider 的 `base_url`/`model`/`api_key` **复用 `providers` 段**（`ProviderConf`），key 覆盖口径同 `extract`（env `MiniMax_Key`/`MINIMAX_API_KEY`、`DEEPSEEK_API_KEY`，占位 `<...>` 清空）。
- `prompt_file` 为仓库根相对路径（`config.PROJECT_ROOT / prompt_file`），默认 `prompts/score.md`；缺失 → rc 2。

### 26.4 提示词与 JSON 契约

- `prompts/score.md` **原样收录**用户提示词（0 分闸门 / 六维与权重 / 致命否决 / 硬标记 / 打分纪律），末尾追加「`## 本批话题（含横向上文）`」注入说明，由代码填充本批每条 `话题名称`/`可使用工具`/`相关AI原理`/`资讯链接`/`出处来源` 与「已打分参考」。
- 解析前剥离推理块：复用 `reasoning.strip_reasoning`（`<think|thinking|reasoning>`），仅残留裸开标签时截断；若模型走 `reasoning_content` 分支则回退取该字段（与 `minimax_parse` 同口径）。
- 输出契约（模型返回 JSON）：

```json
{
  "scores": [
    {
      "话题名称": "示例话题",
      "gate": "pass",
      "dimensions": {
        "普适痛点强度": 4.0, "分层承载力": 3.5, "可演示性": 5.0,
        "时效与稀缺": 4.0, "内容复用价值": 3.5, "讲解成本": 2.0
      },
      "missing": [],
      "risk_flag": false,
      "source_flag": false,
      "reason": "引用 可使用工具/相关AI原理 的具体依据，≤100 字"
    },
    {
      "话题名称": "某公司发布财报",
      "gate": "zero",
      "reason": "无内容内核；抢救建议：改造为「财报里企业如何裁 AI 预算」"
    }
  ]
}
```

- `parse_scores`：容忍 ```json 围栏；JSON 非法、顶层非对象、`scores` 非列表 → raise `ValueError`（调用方重试 1 次，两次都失败该批计 `failed_batches` 并 WARNING 跳过，不抛到运行级）。
- 单条 `scores` 元素：`gate=zero` 时总分置 0、跳过六维；`gate=pass` 时六维必须是 0–5（含 0.5 档）或 `"缺失"`，越界 / 非数字 / 缺 `reason` 的元素丢弃并 `dropped` 计数。
- `normalize`：按 PRD §22.6 缺失归一（剔除该维权重、其余归一）、PRD §22.4③ 致命否决（`普适痛点强度=0` 或 `分层承载力=0` → `weighted=0`，**六维明细仍输出**）；加权结果四舍五入到 1 位小数。
- `risk_flag`/`source_flag` 为独立 bool，序列化进理由（`｜risk=true/source=false` 形态）。

### 26.5 写入格式

- `打分` 列 = 纯数字字符串（1 位小数，如 `3.5`）。
- `理由` 列 = `理由正文 ｜risk/source 标记 ｜六维明细` 压缩为**单行**；日期可缀在理由末尾，**不新增「打分日期」列**。
- 写调用用 `base +record-update`（按 `record_id` 定位既有行，只更新目标 2 列），**不 batch-create、不删行、不动其它列**。

### 26.6 错误与退出码

- **rc 2（配置/参数非法，且不发任何 LLM/写调用）**：config 加载失败 / prompt 文件缺失 / `--provider` 未知或未注册 / 所选 provider 缺 key（配置与 env 均无）/ salon token 占位或缺失 / **目标 provider 对应列缺失**（`MMax打分`/`MMax理由` 或 `DS打分`/`DS理由` 任一不在表内）。
- **rc 1（未捕获异常，或 `--apply` 全部写入失败）**：`failed_writes > 0 且 written == 0`。
- **rc 0（正常，含部分行/批失败跳过）**：单行解析失败或写入失败只跳过并计数，不阻断其余行。
- 解析失败单批重试 1 次（调用 + 解析共享同一重试预算，总 HTTP ≤2/批）；`max_calls > 0` 时每次调用前检查，达限停止剩余批并 WARNING。

### 26.7 幂等语义

- 默认**只补空**：读表时取目标 provider 两列既有值，`打分` 列非空（或理由非空）的行**跳过**，只对空行调用 LLM 并写入。
- `--force`：忽略既有值，对全部命中行重算并覆盖。
- `plan_writes(scores, existing)` 返回 `(write, skip)`，dry-run 清单、summary `pending`/`skipped` 与实际写入**同源一致**。重复运行 `--apply`（无 `--force`）写入数为 **0**，满足 PRD §22.10 幂等验收。

### 26.8 边界

| 场景 | 行为 |
|---|---|
| 空表（0 行） | 不发 LLM、不写，summary 全 0，rc 0 |
| 目标列缺失 | 在读表后、任何 LLM/写调用前判定，rc 2 |
| 超长字段（`资讯链接`/`相关AI原理` 等） | 输入按字符上限截断后再入提示词；模型 `reason` 超 100 字按上限截断并在六维明细处保留原值 |
| 批上限 | 组批恒 ≤ `MAX_SCORE_BATCH=100`；配置/参数越界 rc 2，不静默放大 |
| 模型返回缺行 / 多行 | 按 `话题名称` 与批内行对齐；缺返回的行计 `dropped`，多出的行忽略并 WARNING |
| 已填行混入 | 默认跳过；`--force` 才重算 |

### 26.9 测试策略

- 全离线：`tests/test_score_*.py` 一律 mock `lark-cli` subprocess 与 `httpx`，不打真网、不写真表（延续 `test_extract_*` 形态）。
- 覆盖点：读表/组批（含 100 行边界、`--limit`）、提示词注入横向上文、契约解析（闸门/缺失/否决/非法 JSON 重试）、幂等只补空与 `--force`、列缺失 rc2、单行失败跳过计数、全失败 rc1、分布校验 WARNING、绝不触碰其它列（断言写入 payload 仅含 2 列）。

### 26.10 各功能设计

- **F38 `tc-score` CLI 骨架 + `score:` 配置段 + 列缺失 rc2 + 退出码**：`score_flow.main`（argparse：`--apply/--dry-run` 默认 / `--provider` / `--limit` / `--max-calls` / `--force` / `--env/--config/--db`）；`ScoreConf` 落 `config_models.py`；读表后先 `ensure_columns` 校验目标列，缺列 rc 2；退出码语义见 26.6。
- **F39 `prompts/score.md` + 组批（≤100）+ 横向上文注入**：提示词文件原样收录 + 注入段说明；`group_batches` 恒 ≤100；`build_prompt` 在批内追加「已打分参考（话题名, 分数）」维持全局分布（PRD §22.7）。
- **F40 契约解析 + 重试与计数**：`score_parse`（闸门 / 六维 / 否决 / 缺失归一 / 分布校验）+ `score_llm` 单批重试 1 次、`failed_batches`/`dropped`/`empty` 计数；`score_report.print_dry_run`/`print_summary`。
- **F41 写入与幂等**：`score_write.plan_writes`（只补空 / `--force`）+ `write_scores`（`base +record-update`，MMax/DS 列映射，`failed_writes` 统计，**绝不触碰其它列**）。
- **F42 文档 + 全离线测试**：`docs/CLI.md` 补 `tc-score` 条目、`docs/OPS.md` 补 `score:` 配置与凭据、`AGENTS.md` 命令速查补一行、DESIGN §3 模块树与 §26、`tests/test_score_*.py`。

### 26.11 清单

- [ ] F38 `score_flow.py` + `score:` 配置段 + 目标列缺失 rc2 + 退出码（0/1/2）
- [ ] F39 `prompts/score.md`（原样提示词 + 注入说明）+ 组批 ≤100 + 横向上文注入
- [ ] F40 `score_parse.py` 契约解析（闸门/六维/否决/缺失归一/分布校验）+ 重试与计数
- [ ] F41 `score_write.py` 写入与幂等（MMax/DS 列映射、只补空/`--force`、统计、绝不触碰其它列）
- [ ] F42 文档（CLI.md/OPS.md/AGENTS.md/DESIGN §3/§26）+ 全离线测试
