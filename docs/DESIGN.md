# 技术设计文档 — RSS → 飞书 转发机器人（v2 起点）

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述只在一处维护、另一处引用，避免漂移
> 版本：v0.2 · 2026-08-25 · v0.1 已上线；v0.2 增加 GitHub Pages 详情页

## 1. 架构总览

单进程、无网络服务、无队列，一次 cron 运行 = 抓全部 → 下载入库 → 查待推 → 组卡片 → 推飞书 → 标已推 → 退出。

```
                cron（每天 8:00 / 16:00）
                      │  python -m feedkicker.push
                      ▼
            ┌───────────────────────┐
  Http ───► │   feedkicker.push     │ ─―► 飞书群机器人 webhook
  RSS/Atom  │  (单进程，跑完即退)     │      (interactive 汇总卡片)
            └──────────┬────────────┘
                       ▼
           data/tc.sqlite3（本地存档已下载新闻）
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
| 配置 | `PyYAML` | `config.yaml` + 环境变量覆盖 |
| 存储 | stdlib `sqlite3` | `data/tc.sqlite3`，自动建表建目录 |
| 测试 | `pytest` | feedparser 本地 fixture + 内存 sqlite（`:memory:`） |
| 调度 | cron（系统级） | 进程不由应用常驻，见 §9 |

## 3. 目录结构

```
topic_collection/
├── pyproject.toml            # 依赖 + [project.scripts] tc-push
├── config.yaml               # feed 清单 + webhook + 参数（见 §4）
├── data/                     # 运行时生成：tc.sqlite3（gitignore）
├── logs/                     # cron 重定向写日志（gitignore）
├── feedkicker/
│   ├── __init__.py
│   ├── config.py             # 读 config.yaml + env 覆盖
│   ├── fetch.py              # feedparser 抓取 + 归一化
│   ├── store.py              # sqlite 打开/建表/入库/待推/标已推/首跑
│   ├── feishu.py             # 卡片构建 + webhook 发送 + 业务码校验
│   └── push.py               # 编排主流程 + --dry-run
└── tests/
    └── test_push.py
```

## 4. 配置

**分环境文件**：`config-dev.yaml` / `config-test.yaml` / `config-prod.yaml`（均不入库，凭据本地持有）。
未指定 `--config` 时按 `TC_APP_ENV` / `--env` 推导对应文件；dev/test 凭据留空（不推送真实群），
仅保留量子位单源、冷启动窗口 1 天；prod 为完整源清单 + 真实凭据。以 prod 为例：

```yaml
feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/<token>"
feishu_secret: "<签名密钥>"   # 机器人开启「签名校验」安全设置时的密钥；未开启则留空
bootstrap_days: 3      # 冷启动窗口：新源首跑最多推最近 N 天
site:
  enabled: false       # GitHub Pages 详情页流程暂停（2026-08-25）
http:
  timeout_seconds: 20
  user_agent: "rss2feishu/0.2 (+local cron; private)"
feeds:
  - name: "HN 热榜"
    url: "https://news.ycombinator.com/rss"
```

环境变量（`feedkicker/config.py` 覆盖顺序：默认值 < config.yaml < 环境变量）：
- `FEISHU_WEBHOOK` —— 覆盖 webhook（凭据不进文件可选）
- `FEISHU_SECRET` —— 覆盖签名密钥（同上）
- `TC_APP_ENV` —— 运行环境 `dev|test|prod`（默认 `prod`），决定默认 db 路径
- `TC_DB` —— sqlite 路径，显式指定时优先级高于环境推导

数据库按环境分流：默认 `data/tc-{env}.sqlite3`（dev/test/prod 各一库，互不污染；
launchd 生产任务显式注入 `TC_APP_ENV=prod`）。CLI `--env` / `--db` 可覆盖。

`config.py` 用 `dataclass` 类型化：`Config{feishu_webhook, feishu_secret, bootstrap_days, http: HttpConf{timeout_seconds,user_agent}, feeds: list[Feed{name,url}]}`。

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

## 6. 核心流程（push.py 编排）

```
run(config):
  1. 读 config；打开 store（建目录、跑 CREATE TABLE IF NOT EXISTS）
  2. 出参 new_items:[], feed_fails:int, now=utc_now()
  3. for feed in config.feeds:
       try:
         entries = fetch.feed(feed)              # 归一化，见 §6.1
         store.download(feed, entries, now)      # INSERT ON CONFLICT DO NOTHING
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
  9. if ok: store.mark_pushed(new_items); store.clear_fail(feed)
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
- `update_first_run_all`：把本次实际抓到的 feed 记 `first_run_at`（无论成败都记一次，防止反复触发窗口把牌子"欠推"越卷越多）。

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

## 10. 日志与可观测

满足 PRD §11 #8 与 #7："不常驻、可回溯"。`logging` 输出到 stdout/stderr（cron 重定向到 `logs/push.log`）：
- INFO：每次运行摘要（成功/失败 feed 数、新增条目数、推送是否成功）
- WARNING：某源失败、webhook 为空、发送失败、超长截断
- ERROR：未捕获异常（应基本不出现，顶层 try 兜底 `--` traceback）

审计查询：`sqlite3 data/tc.sqlite3 "SELECT Count(*) FROM articles"` / `WHERE pushed_at IS NULL` 看待推存量。

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
- [x] `send(payload, webhook, http)`：POST + StatusCode/code 校验 + 30000 截断

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
- 按 feed 分组（保 config 顺序）；description `html.escape` 后原样展示；纯 stdlib 字符串模板零依赖
- `render_index`：按日期倒序归档目录（取最近 60 天有数据的日期）

### 15.3 publish.py 发布

- `gh api` Contents API：GET sha 判定新建 vs 更新 → PUT base64 单文件；幂等、无 git 工作区依赖、无冲突
- gh 二进制解析：shutil.which → /opt/homebrew/bin/gh → /usr/local/bin/gh（launchd 环境 PATH 兜底）
- `wait_published(url)`：httpx GET 轮询，200 且非骨架占位即认为生效

### 15.4 飞书摘要卡改造

- 每源最多 `site.top_n` 条（默认 5），组尾「… 还有 M 条见详情页」；详情页无 20KB 限制
- 底部 action button「📰 查看全部 N 条」+ 同文 markdown 链接行（双保险）
- 发送失败且带按钮时：`strip_actions` 去按钮降级重试一次（防旧版客户端/接口不兼容 action 元素）
- 20KB 兜底保留：top_n 截断后仍超限 → 先剥 description 再从尾部丢条目
- **语义变更**：site.enabled 时改为 mark_pushed 先于发送（页面内容完整性优先）；
  发送失败不再自动重试本批条目（已上页面），由 §15.5 求救通道兜底可见性。site 关闭时保持 v0.1 语义

### 15.5 失败求救通道

- meta 表记 `push_fail_streak`：发送成功清零；失败 +1
- 连败 ≥3 → `send_text` 往同一群发纯文本求救（msg_type=text，同签名注入）后计数清零防刷屏
- 纯文本不受 interactive 卡片标签限制，卡片通道挂掉时仍可送达

### 15.6 配置新增（config.yaml）

```yaml
site:
  enabled: true        # TC_SITE_ENABLED=0 可整体关闭回退纯卡片模式
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
