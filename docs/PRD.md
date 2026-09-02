# PRD：RSS → 飞书 转发机器人（v2 起点）

版本：v0.1（2026-08-24）· 状态：**已评审通过**
> 关联文档：[DESIGN.md](DESIGN.md)（工程实现权威）

## 1. 背景与目标（Context）

v1 原始仓库做成了重度工程化的知识库系统（RSS/API 采集 → LLM 摘要/翻译/实体图谱 → PG+pgvector 语义检索 → WebUI → 日报周报），12 项特性、226 测试、十余轮架构审查。但产品定位是**本机单用户**，工程上却长出了队列状态机、advisory lock、租约回收、多跳去重等**只为分布式/高并发才需要**的器械，导致每轮审查都在修状态机 bug。复杂度失控。

v2 **重开 = 瘦身**。真正的起点比 v1 小一个数量级：**不是一个知识库，是一个飞书转发机器人**——订阅 RSS/Atom 网站，定时抓取新条目，把新增内容以批量汇总卡片推送到飞书群机器人，其余一律不做。

一句话价值主张：**你关注的信息源，定时自动汇聚成一张卡片，推送到你正在看飞书的地方。**

### 已确认的产品决策

| 维度 | 决策 |
|---|---|
| 数据源 | RSS / Atom（列表随 config 维护） |
| 推送目标 | 飞书群机器人 Webhook |
| 推送形态 | **interactive 交互卡片**，按 feed 分组，一次运行 = 一张汇总卡 |
| 推送内容 | 标题 + 链接（可点击）+ 原文自带 description（原样带上） |
| 触发 | **cron 定时**，进程运行完即退出（无常驻服务、无 Web 端口） |
| 冷启动 | 新 feed 首跑**最多推最近 3 天**文章，更早历史不推（入库但不推） |
| 去重/存储 | **本机 SQLite** 存档已下载新闻；靠唯一键去重，不重发 |
| LLM / 向量检索 / 图谱 / 周报 / WebUI | 一律**不属于 v2 起点** |
| 部署 | 本机、本地、单用户、数据私密 |
| 文档语言 | 中文 |

---

## 2. 目标用户与使用场景

- **用户**：本人（单用户本地工具）。
- **典型场景**：
  1. 订阅几个 RSS：技术博客、HN、某站点公告；
  2. 每天 **8:00 与 16:00** 各触发一次，把自上次以来各源**新增**文章聚成一张卡片推飞书；
  3. 飞书里点标题跳原文、扫一眼自带摘要决定是否精读；
  4. 加一个新订阅源时，首跑最多带 3 天内的文章热身，不轰炸聊天列表；
  5. 某源挂了，卡片上安静出现一行「⚠ N 个源失败」，其余照常；
  6. 想回看某篇时，本机 SQLite 里能查到已下载的历史（存档，非最终目标）。

---

## 3. 产品范围（Scope）

### In Scope
- RSS/Atom 订阅源抓取（HTTP，带 UA、超时）
- 条目下载入库：**本机 SQLite 存档新闻**（标题/链接/description/发布时间/首次下载时间/是否已推）
- 新增条目识别与去重（靠表唯一键），不重发已推送内容
- 冷启动窗口：新源首跑最多推最近 `bootstrap_days`（默认 3 天）
- 飞书群机器人 Webhook 推送：interactive 汇总卡片，按 feed 分组
- 抓取失败可见性：卡片底部一行「⚠ N 个源失败」
- 配置：`config.yaml` 维护 feed 清单 + webhook + 窗口参数
- 定时：cron + 一条 CLI 命令，无常驻进程
- `--dry-run`：只打印卡片 payload 不发，便于联调

### Out of Scope（本版本明确不做）
- LLM 本地推理 / 摘要 / 翻译 / 实体图谱
- 向量语义检索 / WebUI Dashboard / 日报周报
- 多用户 / 鉴权 / 云端 / 多进程分布式
- 网页主动抓取（非 RSS，仅消费 RSS/Atom）
- 对 description 做清洗 / 重新排版（用户明确「原样带上」）
- 已推内容的撤回 / 编辑（飞书 webhook 不可径）

---

## 4. 核心特性与用户故事

| # | 特性 | 用户故事/验收要点 | 优先级 |
|---|---|---|---|
| F1 | 订阅源抓取 | 抓任意 RSS/Atom，带超时与 UA；一源失败不影响其他源 | P0 |
| F2 | 下载入库 + 去重 | 新条目落 sqlite；同条目二次运行不重发（唯一键幂等） | P0 |
| F3 | 飞书批量推送 | 一次运行把新增条目聚成一张 interactive 汇总卡推群机器人；无新增不发空卡 | P0 |
| F4 | 冷启动窗口 | 新源首跑最多推最近 N 天；更早条目入库但不推 | P0 |
| F5 | 失败可见性 | 有源抓失败时卡片底部带「⚠ N 个源失败」行；全成功则不出现 | P1 |
| F6 | 手动/联调 | `--dry-run` 打印 payload 不发；cron 无常驻 | P0 |

---

## 5. 系统架构

一次 cron 运行 = 抓全部 → 下载入库 → 查待推 → 组卡片 → 推飞书 → 标已推 → 退出。

```
                cron（每天 8:00 / 16:00）
                      │
                      ▼
            ┌───────────────────────┐
  Http ───► │   feedkicker.push     │ ──► 飞书群机器人 webhook
  RSS/Atom  │  (单进程，跑完即退)     │     (interactive 汇总卡片)
            └──────────┬────────────┘
                       ▼
            SQLite（本地存档已下载新闻）
```

- **无常驻服务、无 Web 端口、无队列**：进程起 → 干完 → 退，由 cron/手动拉起来。
- **依赖极简**：`feedparser`（RSS 解析）、`httpx`（同步 HTTP）、`PyYAML`（配置）+ stdlib `sqlite3`；无 C 扩展、无 Docker。
- 多源并发：按源顺序串行即可（规模小）；发送失败仅 warning，不抛、不影响其他源。

---

## 6. 模块划分

| 模块 | 职责 |
|---|---|
| `feedkicker/config.py` | 读 `config.yaml` + 环境变量覆盖（`FEISHU_WEBHOOK`、`TC_DB`） |
| `feedkicker/fetch.py` | `feedparser` 抓取 + 归一化 `{key,title,url,description,published_at}` + 每源错误捕获 |
| `feedkicker/store.py` | sqlite 打开/建表/下载入库（ON CONFLICT DO NOTHING）/查待推/标已推/首跑判定 |
| `feedkicker/feishu.py` | 构建 interactive 汇总卡片 + POST webhook + 业务码校验 |
| `feedkicker/bitable.py` | 多维表格归档（lark-cli 封装、跨源去重、双分组视图，见 §14–§16） |
| `feedkicker/push.py` | 编排主流程（先档案后推送）；`--dry-run` 只打印不发 |

---

## 7. 数据模型（SQLite，本地新闻库）

sqlite 就是**已下载新闻的存档**，不只是一本去重账本；去重靠表唯一键，另用「是否已推」区分待推送。

```sql
-- 已下载新闻存档（去重键 = feed 的 guid，否则规范化 link）
CREATE TABLE articles (
  feed_id      TEXT NOT NULL,
  entry_key    TEXT NOT NULL,      -- guid | canonicalized link
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  description  TEXT,               -- 原文自带摘要，原样存
  published_at TEXT,               -- ISO-8601 UTC（冷启动窗口用）
  first_seen   TEXT NOT NULL,      -- 首次下载时点 UTC
  pushed_at    TEXT,               -- NULL=待推送；非空=已发飞书
  PRIMARY KEY (feed_id, entry_key)
);

-- 每 feed 首次运行标记（判冷启动）+ 失败连击统计
CREATE TABLE feeds (
  feed_id      TEXT PRIMARY KEY,
  url          TEXT NOT NULL,
  first_run_at TEXT NOT NULL,
  fail_streak  INTEGER DEFAULT 0
);
```

- 下载：`INSERT ... ON CONFLICT DO NOTHING`；已存在 = 已下载，跳过；新行 `pushed_at=NULL` = 待推送。
- 首跑窗口：feed 无 `first_run_at` 时，新入库条目的 `published_at < 首跑时点 - bootstrap_days` 直接置 `pushed_at`（入档不推）。
- 推送成功：`UPDATE articles SET pushed_at=now WHERE <本次 id>`。
- DB 路径默认 `data/tc.sqlite3`（项目根内），`TC_DB` 覆盖。

---

## 8. 配置（config.yaml）

```yaml
feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/<token>"
feishu_secret: "<签名密钥>"   # 机器人开启「签名校验」时的密钥，未开启留空
bootstrap_days: 3      # 冷启动窗口（新源首跑最多推最近 N 天）
http:
  timeout_seconds: 20
  user_agent: "rss2feishu/0.1 (+local cron; private)"
feeds:
  - name: "HN 热榜"
    url: "https://news.ycombinator.com/rss"
  - name: "某某博客"
    url: "https://example.com/feed"
```
`feishu_webhook` 亦可用环境变量 `FEISHU_WEBHOOK` 覆盖（凭据不进配置文件可选）；签名密钥同理走 `FEISHU_SECRET`。

---

## 9. 定时（crontab，无常驻）

> **更新（v0.2 F10，见 §13）**：macOS 实际已改用 **launchd**（每天 8:30 / 16:00，
> 睡眠唤醒补跑；plist 见 DESIGN §9）。以下 cron 片段为 v0.1 最初设计记录。

```cron
# 每天 8:00 与 16:00 各跑一次；每次 = 自上次以来新增的一张汇总卡
0 8 * * * cd /path/topic_collection && .venv/bin/python -m feedkicker.push >> logs/push.log 2>&1
0 16 * * * cd /path/topic_collection && .venv/bin/python -m feedkicker.push >> logs/push.log 2>&1
```

---

## 10. 飞书消息格式（interactive 交互卡片）

单次运行一张卡片：

```json
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {"tag": "plain_text", "content": "Feeds 汇总  14:30"},
      "template": "blue"
    },
    "elements": [
      {
        "tag": "div",
        "text": {
          "tag": "lark_md",
          "content": "**HN 热榜**\n[标题1](https://…)\n原文自带摘要第一行\n\n[标题2](https://…)\n原文自带摘要"
        }
      },
      { "tag": "hr" },
      {
        "tag": "div",
        "text": { "tag": "lark_md", "content": "⚠ 2 个源失败" }
      }
    ]
  }
}
```

- 每 feed 一段分组；组内每篇：`[标题](链接)` + description（原样）。
- description「原样带上」所需的**最小让步**：做 markdown 转义，避免 `**`/`` ` ``/`[ ](`/换行破坏卡片布局（这是原样带 + 卡片渲染的唯一必要代价）。
- 失败行仅在有失败时出现；无新条目不发空卡、无失败行。
- 业务校验：`resp.json()` 里 `StatusCode == 0` 或 `code == 0` 才算成功；webhook 空则跳过；机器人开启「签名校验」时按官方算法注入顶层 `timestamp` + `sign`；请求体 ≤20 KB（超限降级裁剪 description → 旧条目，见 DESIGN §7）。

---

## 11. 验收标准（可测）

1. **抓取**：对一真实 RSS 源运行，能取出标题/链接/description/发布时间。
2. **去重**：同一 feed 连续运行两次，第二次无重复条目、不发空卡。
3. **冷启动**：注入带 5 天历史的 feed，`bootstrap_days=3`，首跑只推最近 3 天内有时间戳的条目；更早的入库但 `pushed_at` 非空（不推）。
4. **推送形态**：`--dry-run` 输出的 payload 是合法 interactive 卡片，含标题/可点链接/description，转义正确；描述为空时行内不残留。
5. **失败可见**：一个坏 URL 的 feed，卡片带「⚠ N 个源失败」行，其余正常推送。
6. **存档可回溯**：已推条目保留在 `articles` 表中，可 SQL 查询回看。
7. **真实闭环**：配置真实 webhook 后，一次 cron/手动运行，飞书收到批量汇总卡片；再跑一次不重发。
8. **无常驻**：进程运行结束即退出，无后台残留、无 Web 端口监听。

---

## 12. 明确不做 / 待定（Not Now / Open）

- 明确不做：LLM、图谱、周报、翻译、向量检索、WebUI、多用户、网页爬虫。
- 待定（后续版本再议）：飞书发送失败的重试策略、description 长度上限、按源独立调度、支持多个飞书群。
---

## 13. v0.2 增量（2026-08-25）

在 v0.1 基础上新增 **GitHub Pages 详情页**，飞书卡片瘦身为摘要 + 跳转：

| # | 特性 | 验收要点 | 优先级 |
|---|---|---|---|
| F7 | 每日详情页 | 每班跑完发布 `daily/日期.html`（当天全量、跨源去重、via 标注）+ 归档目录页；公开可访问 | P0 |
| F8 | 摘要卡瘦身 | 每源 top N 条 + 「📰 查看全部」按钮跳当天页面；点击时页面必已可达（发布→轮询→发卡顺序） | P0 |
| F9 | 发布降级 | Pages 发布失败仍发无按钮摘要卡；连败 ≥3 次群内纯文本求救 | P1 |
| F10 | 定时加固 | launchd 取代 cron（睡眠唤醒补跑），每天 8:30 / 16:00 | P1 |

- 公开性决策：聚合内容为公开 RSS 信息，接受公网可读（修订 v0.1「数据私密」约束）
- 明确不做不变；「description 长度上限」「多群」等待定项延续 §12

---

## 14. v0.3 增量（2026-08-25）

| # | 特性 | 验收要点 | 优先级 |
|---|---|---|---|
| F11 | 多维表格归档 | 单表按来源分组视图；组织内链接只读；历史全量回填 | P1 |
| F12 | 跨源去重入表 | 同一文章（规范化 URL）在表内仅一行，多来源取首见 | P1 |

- GitHub Pages 流程暂停（site.enabled=false），多维表格承接"可浏览档案"角色

---

## 15. v0.4 增量（2026-08-25）

| # | 特性 | 验收要点 | 优先级 |
|---|---|---|---|
| F13 | 在线表格归档 | 三环境统一：按日期分工作表、滚动保留 1 年、组织内只读、先档案后推送 | P0 |
| F14 | 卡片详情链接 | 摘要卡附「📰 详情见在线表格」按钮，指向对应环境的归档文件 | P0 |

- 多维表格方案（§14 F11/F12）被本方案取代，Base 留作静态快照

---

## 16. v0.5 增量（2026-08-26）

| # | 特性 | 验收要点 | 优先级 |
|---|---|---|---|
| F15 | 回归多维表格 | 三环境统一 Bitable 归档；prod/dev-test 双 Base；组织内只读 | P0 |
| F16 | 双分组视图 | 「按来源」+「按日期」视图开箱即用，替代日期分表方案 | P1 |

- §15 的电子表格方案废弃；F13/F14 中"在线表格"表述统一改为"多维表格"
- 365 天滚动策略延续：定期清理推送时间超期记录（后续版本实现自动化）
