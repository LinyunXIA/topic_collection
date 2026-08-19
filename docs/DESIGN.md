# 技术设计文档 — Topic Collection

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述（目录结构 / DDL / 接口）只在一处维护、另一处引用，避免漂移
> 版本：v0.11 · 2026-08-19 · Phase 1+ 适配器层完成（与 PRD v0.11 同步）
> v0.11：**Phase 1+ 适配器层完成**——`app/llm/patches.py`（ProviderPatch + 5 个预定义 patch：OMLX/OPENAI/MINIMAX/DEEPSEEK_CHAT/DEEPSEEK_REASONER）+ `app/llm/adapter.py`（LLMAdapter 统一适配层：build_payload/parse_response + strip_think_tags/strip_code_fences）；Provider（openai.py/omlx.py）简化为 HTTP 传输壳；factory 支持 config dict→ProviderPatch 转换；MiniMax-M3 通讯验证通过（healthcheck + generate）；**148/148 pytest 全部通过**（+32 adapter tests）
> v0.10：**切片一+二+三+横切全部完成**——**语言检测 pycld3→lingua-language-detector**（§2）；**Docker 端口 5432→5433**（§5.4/§9）；§14 全部任务完成（1.1-1.9 + 2.1-2.4 + 3.1-3.3 + X.1-X.3 + Day 1）；**真实环境验收**：20 篇 HN 文章端到端跑通（20/20 summary + 40+ embedding）；**86/86 pytest 全部通过**；切片二新增混合检索 `search(q)`（RRF 融合，§7）；切片三新增 topic CRUD + classify_topics + wiki 词条；横切新增 scheduler + A1 重试分类 + B4 近似去重 + pipeline 并发测试
> v0.9：**架构审查四轮——SQL 逻辑错误 + done 判定补洞 + 文档同步清理**——
>   **硬伤 6（mention_count 合并错行）**——§6 dedup 命中步骤 1 SQL 拆两条：loser 只置 `status='done', dedupe_of=winner`，新增步骤 1.5 将 loser 的 `mention_count` 累加到 winner（原 SQL 错写为 loser 自身翻倍，winner 一分没拿）；§6 多跳扁平化段文字说明一致；
>   **硬伤 7（processing→done 判定两个洞）**——§6 状态机 `processing→done` 规则简化为「每次 job 进入终态后检查：该文章不存在任何 queued/running job → 置 done」，与任务集合无关，不再维护非可选 task 清单；`complete_summarize` 职责 ⑥ + `complete_embed` 职责 ④ + 永久失败死信路径各加 done 检查；覆盖关键词命中（topics 缺席自动满足）与失败路径（无钩子触发）两个漏洞；
>   **中等 6（recover SQL 覆盖 error 字段）**——§6 recover SQL 不再写 `error='[recovered x N]'`，原始错误保留在 error 字段、recover 次数由 `recover_count` 追踪；
>   **中等 7（unparseable job 跳过→循环）**——§6 `pending→unparseable` 段 worker 发现 unparseable 文章时标记 job `status='superseded'` 而非跳过（跳过导致 running→recover→再领取→再跳过循环）；
>   **文档**——§13 测试清单补 D5（summaries upsert content_hash 版本守卫断言）、D6（loser done + drain_queue 不复活断言）；§13 D4 日志规约删 `error_class` 的 `timeout` 枚举（§5.1 已删）；PRD §11 config YAML 砍成示意 + 指向 DESIGN §9（连续三轮同步漂移，彻底消灭副本）
> v0.8：**架构审查三轮——SQL/谓词层逻辑错误集中落档**——
>   **硬伤 1（summaries upsert 谓词方向反）**——§6 状态机原子性段 SQL `WHERE summaries.content_hash IS DISTINCT FROM EXCLUDED.content_hash` 改为 `WHERE EXCLUDED.content_hash = (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id)`（比对**文章当前版本**而非存量摘要）；守卫意义段同步说明旧谓词只挡幂等重写、放行过期覆盖，方向反；§13 加 D5 测试断言；
>   **硬伤 2（pick-and-claim 自增 attempt 抵消瞬时不耗预算）**——§6 worker 领取 SQL 删除 `attempt=attempt+1`（v0.5 残留），attempt 完全归永久失败路径所有；§14 1.6 任务描述加注「领取 SQL 不自增 attempt」；
>   **硬伤 3（drain_queue 谓词导致 loser 周期性复活）**——§6 加**文章状态迁移触发点**（pending→processing 在 pick-and-claim、processing→done 在最后 task complete 钩子里、loser 在 dedup 命中同事务直接 done）；§6 backpressure 段改谓词为 `WHERE a.status='pending' AND a.dedupe_of IS NULL AND NOT EXISTS (SELECT 1 FROM processing_jobs j WHERE j.article_id=a.id)`，三条件互锁；§6 dedup 命中段加 loser done + 删 article_topics 双保险；§13 加 D6 测试断言；
>   **中等 4（超时转永久不再 attempt+1 循环）**——§6 失败 SQL / 重试矩阵 / §11 表三处对齐为「直接 failed」（180s×3 = 9 分钟试完再耗 9 分钟重试病态文章无意义）；
>   **中等 5（主题聚合 dedupe_of IS NULL 硬约束）**——§6 主题分类规则段加 `JOIN articles a ON a.id=at.article_id WHERE a.dedupe_of IS NULL` 统一规则 + dedup 事务删 article_topics 双保险；
>   **小注**——`complete_embed` 钩子职责 ② 加近似去重判定（与 ① ③ 同事务），§5.1 加 `recover_count INT DEFAULT 0` 列、`error_class` 删悬空的 `'timeout'` 枚举值、recover SQL 改用计数（不再 append error 字段）；§5.2 dedup 措辞统一为「在 summarize 被领取前拦截」+ partial HNSW 索引实现提示（active model 字面量拼查询或 EXPLAIN 验证）
> v0.7：架构审查二轮落档——**Phase 1 回归单进程**（§6 运维模式：worker + APScheduler 同 asyncio 循环，`python -m app.worker` 起全套，drain_queue 随 scheduler 在场、C1 自愈；CLI 仍只走 services 不起 worker）；**重试分类落 schema**（§5.1 `processing_jobs` 加 `consecutive_timeouts/error_class`；§6 失败 SQL 按瞬时/永久分路径：瞬时不自增 attempt、永不死信、退避封顶 15m，永久自增 + `max_attempts` 死信；§4.4/§6/§11 **401/403 归永久类**）；**近似去重向量改 body↔body 同粒度**（§6 去重段：查询向量与候选都用 `kind='body'` 行，不再 mean(title,body) 对 mixed 池排序）；**embed_summary 优先级 4→6**（§6 优先级表，让 27B 生成链先排空再切 8B 嵌入、杜绝 gen↔embed 模型抖动）；**init_db 统一走 Alembic**（§5.4/§14：init_db = CREATE EXTENSION + `alembic upgrade head`，schema 唯一真源 = 迁移）；**supersede 竞态纠偏**（§6 状态机原子性段 + summaries upsert 带 `content_hash` 版本判定）；**dedupe_of 多跳扁平化**（§6 去重命中：沿链回溯到终极 winner、loser 改指、mention_count 转移）；**HNSW + model 过滤对策**（§5.2：active 模型走 partial HNSW 索引）；**worker 续租随处理协程**（§6：续租与处理同 task、httpx 必带超时，防 lease 永不过期）；**article_versions 写入时机**（§5.1：raw_text always / raw_html 按需 + P3 保留）；**`tc summarize` 语义 + `tc topic add` 触发近窗 reclassify**（PRD §4 F11 / §6）；**选型/路径/日志补全**（§2 语言检测 lingua-language-detector、§9 config 路径 `TC_CONFIG`、§13 日志双 sink、§6 fetch_failures 成功归零）
> v0.6：近似去重闭环（§6：title+body 向量 / 取消路径 supersede / 阈值 0.95 + 同语言 + 可逆 + 日志 / config 补 `dedup.{threshold,window_days,k}`）；`complete_summarize` 入队 wiki（§6）；topics 入队移到摘要后（§6，单触发 + token 省 + 跨语言判定稳）；超时转永久类死信规则（§6 重试矩阵：healthcheck 正常 + 同 job 连续 3 次超时 → 永久死信）；backpressure 全量入库 + 仅限 LLM 入队 + drain_queue 补队（§6）；embed_summary 优先级降到 4（§6 攒批、避免 gen↔embed 模型交替）；HNSW `ef_search` 注释纠错（§5.1，查询期 GUC 不在建索引 WITH）；pg_dump 走 `docker compose exec postgres`（§10）；主题变更重算默认窗口限制（§6 + §9 `topics.reclassify_recent_days`）；§6 ingest 全局 semaphore + 每域限速规格落档；§9 补全配置键（`ingestion.global_concurrency/per_host_interval_ms/dedup.{threshold,window_days,k}` + `topics.reclassify_recent_days`）；§1/§4.2/§5.2/§9/§15 维度表述统一为「原生 4096 经 `dimensions=1536` 截断」
> v0.5：架构审查落档——重试按瞬时/永久错误分类（§6/§11）、检索 P1 即 RRF（§7）、跨源向量近似去重（§6）、CPU 密集走 to_thread（§2）、`complete_embed` 钩子（§6）、`tc backup` 主触发备份（§10）、supersede 同事务（§6）、tsv 两阶段刷新（§5.3）、HNSW 调参（§5.1）等

---

## 1. 架构总览

单应用进程 + 外部 PostgreSQL 服务，三子系统协作：

```
                    ┌─────────────────────────────────────────────┐
                    │               FastAPI 进程                    │
                    │                                             │
  Internet ──► ┌────┴─────┐   ┌──────────────┐   ┌──────────────┐  │
  RSS/API/     │ Ingestor │──►│ Pipeline     │──►│ Wiki + Graph │  │
  Scrape       │ (async) │   │ (LLM stages) │   │ Builders     │  │
               └────┬─────┘   └──────┬───────┘   └──────┬───────┘  │
                    ▼                ▼                  ▼          │
               ┌───────────────────────────────────────────────┐   │
               │  PostgreSQL 15+ (pgvector + tsvector + GIN)   │   │
               └───────────────────────────────────────────────┘   │
                    ▲                ▲                  ▲          │
               ┌────┴─────┐   ┌──────┴───────┐   ┌──────┴───────┐  │
               │ APSched  │   │ Web UI       │   │ LLM Providers│  │
               │ scheduler│   │ (Jinja+HTMX, │   │ oMLX / Ollama│  │
               └──────────┘   │  ECharts)    │   │ (localhost)  │  │
                              └──────────────┘   └──────────────┘  │
               ┌──────────────────────────────────────────────────┐│
               │ Services 层 = 应用 API → CLI 只是薄 typer 封装    ││
               └──────────────────────────────────────────────────┘│
```

**关键决策**
- 单进程、全异步（httpx + SQLAlchemy 2.0 async + asyncpg）；队列 = Postgres 表 `processing_jobs`，worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取，无需 Redis/Celery
- **LLM 全部走本地 oMLX**（`http://localhost:8000`，OpenAI 兼容 RESTful，**本机不鉴权**，已实测），三模型分工：
  - 生成：`Qwen3.8-27B-MLX-4bit`（实测可用，质量更佳；备选 9B INSTRUCT 更轻量；`THINKING` 变体加载失败待修复）
  - 嵌入：`Qwen3-Embedding-8B-4bit-DWQ`（原生 4096 维，经 `dimensions=1536` 服务端截断，§4.2/§5.2）
  - 重排（P2）：`Qwen3-Reranker-4B-mxfp8`（实测 `/v1/rerank` Cohere 风格可用）
- 增量处理：仅新/变更文章入流水线；LLM 产物按 `(article, task, model, content_hash)` 缓存
- 检索双通道：向量语义（pgvector HNSW）+ 关键词全文（tsvector + GIN，jieba 预切词）

---

## 2. 技术选型

| 领域 | 选型 | 说明 |
|---|---|---|
| 语言/运行时 | Python 3.14 | venv 已就绪。**3.14 风险**：selectolax / asyncpg / lxml 等 C 扩展依赖在 3.14 上的 wheel 可能不齐；装不上别恋战，**退 3.12 / 3.13**，解释器版本不是这个项目的价值所在 |
| Web 框架 | FastAPI + uvicorn | async，内置 OpenAPI |
| 数据库 | PostgreSQL 15+ + pgvector | dev 用 `docker-compose.yml`（pgvector/pgvector 镜像） |
| ORM | SQLAlchemy 2.0 async + asyncpg | + Alembic 迁移 |
| RSS 解析 | feedparser | Atom/RSS |
| HTTP 客户端 | httpx | 抓取 + 调 oMLX |
| 清洗/抓取 | selectolax + trafilatura | 主内容提取 |
| 中文分词 | jieba | FTS 预切词 |
| 语言检测 | lingua-language-detector | `cleaner.py` 判定 `articles.lang`（决定是否走 translate / 限同语言近似去重，§6）；纯 Python，支持 75 种语言，无需编译 C 扩展（pycld3 需 protobuf 编译器，3.14 下安装失败） |
| 调度 | APScheduler (AsyncIOScheduler) | 定时抓取/报告 |
| 前端 | Jinja2 + HTMX + ECharts（本地 vendored） | 离线可用 |
| LLM 客户端 | httpx 直连 oMLX | 无官方 SDK 依赖 |
| 配置 | pydantic-settings + YAML | 凭据走环境变量 |
| 测试 | pytest | LLM 可 mock |
| 执行模型 | `asyncio.to_thread` | **CPU 密集任务（jieba 切词 / selectolax / trafilatura / HTML→Markdown）一律经 `to_thread`/`run_in_executor` 卸到线程池**，不阻塞 async 事件循环——asyncio 协作式调度对纯 CPU 无让出点，同步跑会让 worker 停领新任务、Phase 2 单进程下 WebUI 请求延迟尖刺 |

---

## 3. 目录结构

```
topic_collection/
├── pyproject.toml              # 依赖 + uvicorn 入口
├── README.md
├── docker-compose.yml          # dev: pgvector/postgres
├── config/
│   ├── config.yaml             # 主配置（无凭据、不含订阅源）
│   └── feeds.yaml              # 订阅源清单（独立文件，加订阅只改这里）
├── app/
│   ├── main.py                 # create_app() + lifespan（db/scheduler/worker）
│   ├── config.py               # Settings（pydantic-settings，env 覆盖）
│   ├── db/
│   │   ├── engine.py           # async engine + 会话工厂 + 扩展校验(vector)
│   │   ├── models.py           # SQLAlchemy 模型
│   │   ├── fts.py              # tsvector 维护 + jieba 预切词
│   │   └── migrations/         # Alembic
│   ├── ingest/
│   │   ├── base.py             # FeedItem/ApiItem/ScrapeResult 数据类
│   │   ├── feeds.py            # RSS/Atom 抓取（ETag/304）
│   │   ├── api.py              # 配置化 API 连接器（P2 骨架）
│   │   ├── scrape.py           # 礼貌抓取 + 主内容提取（P2）
│   │   └── dedup.py            # canonicalize_url / content_hash
│   ├── llm/
│   │   ├── base.py             # LLMProvider Protocol + 请求/结果类型
│   │   ├── omlx.py             # oMLX 三端点实现
│   │   ├── ollama.py           # Ollama 备选
│   │   ├── client.py           # 门面：并发/重试/健康检查
│   │   ├── prompts.py          # 中文输出提示词模板
│   │   └── structured.py       # JSON 解析 + 修复
│   ├── services/
│   │   ├── cleaner.py          # HTML→Markdown 清洗
│   │   ├── llm_tasks.py        # summarize/translate/entities/topics/wiki/embed_core/embed_summary
│   │   ├── entities.py         # 实体合并/关系（P2）
│   │   ├── topics.py           # 主题 CRUD + 聚合（P1 基础）
│   │   ├── wiki.py             # 词条构建 + 混合检索
│   │   ├── graph.py            # ECharts JSON（P2）
│   │   ├── reports.py          # 日报/周报（P2）
│   │   └── cli.py              # typer 薄封装（Phase 1 主入口）
│   ├── pipeline.py             # 队列 + worker 领取逻辑 + recover
│   ├── worker.py               # 入口：单进程 asyncio loop 挂 worker task + APScheduler（Phase 1，§6 运维模式）
│   ├── scheduler.py            # APScheduler 任务定义（被 worker.py / main.py lifespan 拉起）
│   └── api/                    # (Phase 2) WebUI
│       ├── deps.py             # session/settings/llm 依赖注入
│       ├── dashboard.py        # /, /settings
│       ├── feeds.py            # feed CRUD
│       ├── articles.py         # 列表/详情/重试
│       ├── wiki.py             # wiki + search
│       ├── graph.py            # 图谱 JSON（P2）
│       └── topics.py           # 主题（P2）
│   └── web/                    # (Phase 2) 前端资源
│       ├── templates/          # Jinja2 页面
│       └── static/             # app.js / echarts.min.js / styles.css（本地 vendored）
├── data/  logs/  scripts/  tests/
```

### 3.1 Phase 2 模块职责（详细）

#### `app/api/` 路由文件（FastAPI，Phase 2 上线）

| 文件 | 职责 |
|---|---|
| `deps.py` | `Depends` 工厂：`get_session`、`get_settings`、`get_llm_client`、`get_current_user`（本地单用户 = 占位返回固定 user；不做鉴权） |
| `dashboard.py` | `GET /` 概览：流水线统计、队列深度、LLM 健康横幅、最近 20 篇、源健康；`GET/POST /settings` 模型与并发配置 |
| `feeds.py` | `GET /feeds` 列表 + 筛选；`POST /feeds` 新增/编辑（form）；`POST /feeds/{id}/fetch` 立即抓取；`POST /feeds/{id}/disable` 禁用 |
| `articles.py` | `GET /articles` 列表 + 筛选 + 分页；`GET /articles/{id}` 详情（Tab：原文/摘要/翻译/实体/相关话题/Wiki）；`POST /articles/{id}/retry/{task}` 手动重试；`POST /articles/{id}/undedupe` 撤销去重；`GET /api/articles/{id}/similar` 同主题相似文章 |
| `wiki.py` | `GET /wiki` 词条索引（按 kind 分组）；`GET /wiki/{slug}` 词条页；`GET /wiki/{slug}/raw` 纯 Markdown（导出用） |
| `search.py` | `GET /search?q=` 混合检索（关键词+语义+Reranker），结果含文章+Wiki 高亮片段 |
| `graph.py` | `GET /graph` 图谱页（HTMX partial 渲染 force-graph）；`GET /api/graph.json` 数据 JSON（filter via query） |
| `topics.py` | `GET /topics` 主题列表（跨源聚合视图：主题×源×文章数）；`GET/POST /topics/{id}` 编辑；`POST /topics/{id}/reclassify` 重算近窗 |
| `reports.py` | `GET /reports` 报告列表；`GET /reports/{id}` 报告渲染；`POST /reports/{id}/retry` 重新生成；`GET /reports/{id}/export.md` Markdown 下载 |
| `health.py` | `GET /api/health` LLM 队列 worker 状态；`GET /api/llm-status` LLM ping（每 30s htmx 轮询） |

#### `app/web/` 前端层级（Jinja2 + HTMX 1.x）

```
templates/
├── base.html                 # 全局 layout：左侧 sidebar + 顶部 llm-status 横幅 + content block
├── components/
│   ├── pagination.html       # 通用分页
│   ├── htmx_helpers.html     # hx-target/hx-swap 宏
│   ├── toast.html            # 操作反馈
│   └── error_banner.html     # 路由错误展示
├── partials/                 # HTMX partial swap（hx-get/hx-post 返回）
│   ├── article_row.html
│   ├── topic_chip.html
│   ├── feeds_table.html
│   ├── graph_filter.html
│   └── report_card.html
├── overview.html
├── feeds/{list,edit}.html
├── articles/{list,detail}.html
├── wiki/{index,page}.html
├── graph/page.html
├── topics/{list,edit}.html
├── reports/{list,view}.html
├── settings/page.html
└── search/results.html

static/
├── htmx.min.js               # vendored 离线
├── echarts.min.js            # vendored 离线
├── sortable.min.js           # vendored 离线
├── pico.min.css              # classless CSS（Pico CSS subset）
└── app.js                    # 自写：sidebar 折叠、图表初始化、Spinner
```

#### `app/services/` Phase 2 新增

- `entities.py` — `extract_entities(article_id)` / `upsert_entities(...)` / `link_relations(...)` / `merge_aliases(...)` / `resolve_entity(name)` / `entity_relations_graph(entity_id, depth=1)`
- `graph.py` — `graph_json(*, topic_id=None, entity_type=None, since_days=None) -> dict` 含 ECharts 4 字段：`{categories, nodes, links, filters}`；`graph_node_articles(node_id)` 节点回看文章
- `reports.py` — `generate_daily_report()` / `generate_weekly_report()` / `_aggregate_stats(period_start, period_end)` / `_render_html(markdown)` (markdown lib extras=`toc,fenced-code,tables`) / `_mark_failed(report_id, error)`

#### `app/ingest/api.py`（Phase 2 骨架）

`fetch_api(feed) -> list[FeedItem]`：读取 `feeds.config_json` 含 `{endpoint, method='GET', params, headers, rate_limit_per_hour, items_path, mapper:{title, url, author, time, content}}` → httpx 调用 → jmespath 提 items → mapper 字段映射 → `FeedItem` 列表。**stricter than RSS**：source_url 用 mapper 提取的稳定字段（如 HN `id`），无则 content_hash 提供兜底。HN/GitHub/arXiv starter yaml 见 §9。

#### 跨层约定

- **API 路由只做路由 + 表单校验 + 调 service，不写业务** —— 与 §6 运维模式一致
- **HTMX partial 模板继承 `_partial.html`** 避免套 base.html 完整 layout
- **WebUI 不可用时（开发期或 oMLX 掉线期）**：模板渲染走降级分支，UI 仍可浏览已 `done` 文章，流水线状态如实展示（不隐藏）

---

## 4. LLM Provider 抽象（核心）

### 4.1 接口（三能力）

```python
class LLMProvider(Protocol):
    name: str                        # "omlx" | "ollama"
    base_url: str                    # oMLX OpenAI 兼容端点
    api_key: str | None              # 可选；None/空 = 不发送鉴权头（本机默认不鉴权）
    generation_model: str            # Qwen3.8-27B-MLX-4bit（默认生成）
    embedding_model: str             # Qwen3-Embedding-8B-4bit-DWQ
    rerank_model: str | None         # Qwen3-Reranker-4B-mxfp8（P2）

    async def generate(req: GenerateRequest) -> GenerateResult
        # POST {base}/v1/chat/completions（本机不鉴权；api_key 非空时才带 Bearer）
    async def embed(texts: list[str], model: str | None = None) -> EmbedResult
        # POST {base}/v1/embeddings → list[list[float]]；运行时实测维度
    async def rerank(query: str, docs: list[str], top_n: int) -> list[int]
        # POST {base}/v1/rerank（Cohere 风格）→ 排序后的 doc 下标；不存在则降级
    async def healthcheck() -> HealthStatus
        # 探测 /v1/models 或逐个端点可用性

class GenerateRequest:
    model: str; messages: list[dict]; temperature: float = 0.3
    max_tokens: int | None; json_mode: bool = False; timeout_s: float = 180
class GenerateResult: text: str; finish_reason: str; usage: dict | None; latency_ms: int
```

### 4.2 oMLX 实现要点（已实测确认）

- 统一 httpx 异步客户端；**本机不鉴权**，不发 Authorization 头（已实测）；若 `api_key` 非空再带 Bearer
- `json_mode=True` → `{"response_format": {"type": "json_object"}}` —— **实测被接受，返回合法 JSON**
- **端点均已实测可用**：`/v1/embeddings`（`Qwen3-Embedding-8B-4bit-DWQ`，返回 1536 维 float）、`/v1/rerank`（Cohere 风格：入参 `query/documents/top_n`，出参 `results:[{index, relevance_score}]`）
- **向量维度校验**：启动/首个 embed 后实测维度与 `db.vector_dim`（=1536）比对，不一致即告警并阻止写入（防 HNSW 失配 / 模型切换）
- **Qwen3-Embedding 指令感知（instruct prefix）**：`Qwen3-Embedding` 官方推荐 **query 侧**拼 instruct 前缀（`"Given a web search query, retrieve relevant passages that answer the query: "`，可按 §4.5 提示词风格微调），**document 侧不加**——区分使用检索质量明显更好。在 `app/llm/embed.py` 封装层**一处**处理：`embed_query(text)` 自动加前缀，`embed_documents(texts)` 不加；上层 services 无感。截断维度 `dimensions=1536` 也走同一封装（§5.2）

### 4.3 OpenAI 兼容外部 Provider（Phase 1+）

- **实现**：`app/llm/openai.py`（`OpenAIProvider`），遵循 §4.1 `LLMProvider` Protocol
- **端点**：与 oMLX 相同路径（`/v1/chat/completions`、`/v1/embeddings`、`/v1/models`），但 **Authorization header 必带**（外部 API 必鉴权）
- **json_mode**：`response_format: {type: json_object}`，与 oMLX 行为一致
- **embed `dimensions`**：**不传**（不同 OpenAI 兼容 API 支持情况不一），由 `complete_embed` 钩子统一校验维度（§5.2）
- **rerank**：`raise NotImplementedError`（OpenAI 不支持 Cohere 风格 rerank）；rerank 强制走本地 oMLX
- **错误分类**（§4.4 / §6）：401/403/400 → `PermanentError`（不退避，`attempt+1`，`max_attempts` 死信）；5xx/429 → 瞬时退避；**在 `LLMClient._retry_transient` 内联判断**（不调用 `_classify_http_error` 方法——Python except 块内 raise 的异常不被同 try 的其他 except 捕获）
- **instruct prefix**：OpenAI embedding 不加 instruct prefix（`embed_instruct_prefix = ""`）；oMLX 加 Qwen3 prefix（§4.2）；`LLMClient.embed_query` 通过 `provider.embed_instruct_prefix` 属性读取（Protocol 新增此字段）
- **per-capability 切换**：`app/llm/factory.py` 的 `build_provider(capability, settings)` 按能力构建 provider；generate 可选 `omlx | openai`，embed/rerank 强制 `omlx`（隐私，§12）；API key 从环境变量读取（`api_key_env` 字段引用 env var 名），启动时 fail fast
- **配置**（§9）：`llm.generate.backend: openai` + `llm.providers.openai.endpoint` + `llm.providers.openai.api_key_env: OPENAI_API_KEY`；环境变量 `TC_LLM__GENERATE__BACKEND=openai`

### 4.4 降级链路

| 能力 | 主（oMLX） | 降级 |
|---|---|---|
| 生成 | `/v1/chat/completions` | Ollama（切换 backend） |
| 嵌入 | `/v1/embeddings` + `Qwen3-Embedding-8B` | **无进程内降级**——`bge-small-zh`(512d) / `bge-m3`(1024d) 维度不匹配 `vector(1536)` 且向量空间不同，混存会互相检索失效。oMLX 不可用 → 语义通道关闭、仅关键词（Dashboard 提示，§7/§11） |
| 重排 | `/v1/rerank` + `Qwen3-Reranker-4B`（P2） | 进程内 `bge-reranker-v2-m3` → 不重排（保持 RRF 融合，§7） |

### 4.5 `LLMClient` 门面

并发信号量（默认 1）、每调用超时、指数退避重试（5xx/超时/连接拒绝）、`healthy` 标志与**单次健康探测**。重试/超时只在此层处理，services 不碰传输。**两层重试分工**：客户端=秒级抖动重试（单次调用内）；job 级 `lock_until` 退避（§6）=分钟级长中断（oMLX 整体不可用），互不冲突。**错误分类**：401/403/400 是永久/配置错误（鉴权失败、请求格式错），**不走指数退避**、直接抛永久类由 job 层按 `max_attempts` 死信；只 5xx/超时/连接拒绝归瞬时、走退避。**并发=1 是待验证假设**：oMLX 按请求切换模型会抖动加载是真，但 MLX 可在统一内存同时常驻多模型（27B-4bit ≈14GB + 8B 嵌入 ≈5GB，64GB+ Mac 装得下，无需切换、无抖动）——若实测同时常驻可行，信号量改 per-capability 一槽（gen 一个、embed 一个），embed 不被 27B 的 20–60s 阻塞、语义索引吞吐翻倍。P1 先按 1，§16 记为已知限制。

**`healthy` 标志归属**：是 `LLMClient` 实例的**进程内**内存状态。**Phase 1 单进程**（§6 运维模式：worker + scheduler 同 asyncio 循环）下 worker 与 scheduler 共享同一个 `LLMClient`，`healthy` 全局可见，无需跨进程同步——简化为：
- **scheduler**：跑定时 healthcheck 任务（§10，每 5m `GET /v1/models`）更新 `LLMClient.healthy` 与 Dashboard 横幅
- **worker**：作为 oMLX 的唯一消费者，**仍自带自探测兜底**——领取空手且 `lock_until` 都未到期时、或连续 N 次 LLM 调用失败时，发一次 `GET /v1/models`（或 `POST /v1/embeddings` 探活）刷新 `healthy`、决定 sleep 退避时长；不盲信 scheduler 5m 一次的快照（掉线可能在两次探测之间发生）
- **CLI**：短命进程，不持有常驻 `LLMClient`；`tc status` 调用时即时探测一次报告健康，不与常驻进程共享状态（CLI 命令本身不走 worker，§6 运维模式）

**`--check-llm` 启动校验覆盖全部配置模型**：不只查主 `llm.model`，还对 `llm.models` 里每个 per-task 覆盖（summarize/translate/entities/topics/wiki/report）+ `embed.model` + `rerank.model` 逐个 `GET /v1/models` 比对——拼错的覆盖模型名只会在该 job 运行时 404，启动期就暴露能省一整轮退避排查。

### 4.6 提示词契约（一律中文输出）

| 任务 | 输出 |
|---|---|
| `summarize` | JSON `{"summary_zh", "key_points":[], "confidence":0.0-1.0}`（3–5 要点；`confidence` 入 `summaries.confidence`，§5.1） |
| `translate` | 简体中文纯文本 |
| `extract_entities` | JSON `{"entities":[{name, surface, type, aliases, description, canonical_name_zh}], "relations":[{subject, predicate, object}]}`。**grounding**：`surface` 必须是原文子串/近似 span，校验不过则降 `confidence` 或丢弃，防 LLM 幻觉实体污染图谱；**跨语言归一**：保留原文 `surface` + 中文 `canonical_name_zh`，`aliases_json` 收别名互链，避免 "OpenAI"/"开放AI" 在图谱分裂成方言岛（§5.1/§7） |
| `classify_topics` | JSON `{"scores":{topic_id:0.87}}` |
| `generate_wiki_entry` | 中文 Markdown 词条 |
| `generate_report` | 中文 Markdown |

### 4.6.1 Phase 2 提示词契约展开（实施细节）

#### `extract_entities`（Phase 2 切片 2.3）

完整输出 schema：

```json
{
  "entities": [
    {
      "name": "Qwen3",
      "surface": "Qwen3",
      "type": "model",
      "aliases": ["通义千问 3", "Qwen-3", "千问三代"],
      "description": "阿里巴巴发布的开源大语言模型系列，第三代。",
      "canonical_name_zh": "通义千问 3",
      "confidence": 0.92
    }
  ],
  "relations": [
    {
      "subject": "Qwen3",
      "predicate": "developed_by",
      "object": "Alibaba",
      "confidence": 0.85,
      "evidence_span": "Qwen3 由阿里巴巴达摩院开源..."
    }
  ]
}
```

**严格约束**（写入前在 `entities.upsert` 服务层校验）：

1. **`grounding` 规则**：每个 entity 的 `surface` 必须在原文 `content_text` 子串内（`surface in content_text`）；若 LLM 给出的 surface 不在原文，由 `services/entities.normalize_surface()` 自动修正为原文最近邻 span，仍找不到 → `confidence *= 0.5`；找不到且无法对齐 → 丢弃
2. **跨语言归一**：所有实体都必须有 `canonical_name_zh`（即使原文是英文，也要给中文规范化名，存 `entities.aliases_json` + `entities.canonical_name`）；形成跨语言别名岛屿统一（"OpenAI"/"开放AI" → 同一 entity）
3. **type 枚举**：`person | org | product | model | technology | concept | event | location | other`（LLM prompt 给定）；存 `entities.entity_type`
4. **别名合并策略**：upsert `entities` 表时按 `(entity_type, canonical_name_zh)` UNIQUE 冲突，新实体的 `aliases` 数组与既有 `aliases_json` 取并集（dedupe）

#### `generate_entity_wiki` / `generate_topic_wiki`（Phase 2 切片 2.4）

- **输入**：`entity_id` 或 `topic_id` + 关联文章 summaries 片段
- **输出**：Markdown 词条
  ```markdown
  # 通义千问 3

  **别名**：Qwen3、Qwen-3
  **类型**：模型
  **首次提及**：2026-08-15 in article 1234
  **置信度**：0.92

  ## 概述

  通义千问 3（Qwen3）是阿里巴巴达摩院于 2025 年开源的大语言模型系列...

  ## 出现文章（5 篇）

  | # | 标题 | 摘要 |
  |---|---|---|
  | 1234 | Qwen3 发布 | ... |

  ## 关系

  - 由 [Alibaba](slug:entity-alibaba) 开发
  - 同家族：[Qwen2.5](slug:entity-qwen25)
  ```
- **强制 constraint**：词条内容**只**引用 ground-truth 来源（相关 article_ids），不输出 LLM 自由发挥的"介绍性散文"——通过 prompt 限定 + 后处理 cite 校验

#### `generate_report`（Phase 2 切片 2.5）

- **输入**：`stats: dict`（schema 见 §10）+ `period: {start, end}` + `report_type: 'daily'|'weekly'`
- **输出**：中文 Markdown 综合
- **结构 prompt 约束**：
  - 第 1 章：本周期概览（统计直引）
  - 第 2 章：Top 主题 + 精华摘要（每主题一段 LLM 综合）
  - 第 3 章：实体/关系增长（Top N）
  - 第 4 章：源健康
  - 第 5 章：潜在异常（队列积压、LLM 延迟飙升）
- **style**：中性、纪实风格；不允许 LLM 制造统计量（Prompt 明确给出 `stats`，要求只引用 `stats` 中的数字）
- **后处理**：服务端 `markdown(md, extras=['toc','fenced-code','tables'])` 渲染为 HTML，同事务写 `reports.content_html`

### 4.7 适配器层（LLMAdapter，Phase 1+）

**问题**：不同 OpenAI 兼容 API（oMLX / MiniMax / DeepSeek / OpenAI）在请求和响应格式上有差异（think 标签、json_mode 可靠性、endpoint 路径、temperature 支持等）。每换一个 provider 都要改代码。

**方案**：统一 DTO + ProviderPatch 声明式配置。

```
GenerateRequest (内部DTO)
    ↓ LLMAdapter.build_generate_payload() + ProviderPatch
OpenAI 标准 payload
    ↓ HTTP POST
Raw JSON Response
    ↓ LLMAdapter.parse_generate_response() + ProviderPatch
GenerateResult (内部DTO，text 已清理 think/围栏)
```

**三层分工**：
- **Provider（openai.py / omlx.py）**：只做 HTTP 传输（headers、timeout、error handling），请求/响应格式委托给 adapter
- **LLMAdapter（adapter.py）**：80% 通用 OpenAI 逻辑（构建 payload、解析 response、strip think/fences）
- **ProviderPatch（patches.py）**：20% 差异声明（config 驱动，不改代码）

**ProviderPatch 字段**：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `send_dimensions` | bool | false | embed 是否发 dimensions 参数（oMLX=true，外部=false） |
| `dimensions_value` | int | 1536 | dimensions 值 |
| `drop_request_fields` | list[str] | [] | 要移除的请求字段（如 DeepSeek-R1 不支持 temperature） |
| `extra_body_fields` | dict | {} | 额外请求字段 |
| `strip_think_tags` | bool | false | 清理 `<think>...</think>` 块 |
| `strip_code_fences` | bool | false | 清理 ` ```json ... ``` ` 代码围栏 |
| `finish_reason_map` | dict | {} | finish_reason 值映射 |
| `chat_path` | str | /v1/chat/completions | chat 端点路径（DeepSeek 用 /chat/completions） |
| `embed_path` | str | /v1/embeddings | embed 端点路径 |
| `models_path` | str | /v1/models | 模型列表端点路径 |

**预定义 Patch**（`app/llm/patches.py`）：
- `OMLX_PATCH`：`send_dimensions=True, dimensions_value=1536`
- `MINIMAX_PATCH`：`strip_think_tags=True, strip_code_fences=True`
- `DEEPSEEK_CHAT_PATCH`：`chat_path="/chat/completions"`
- `DEEPSEEK_REASONER_PATCH`：`strip_think_tags=True, chat_path="/chat/completions", drop_request_fields=["temperature"]`
- `OPENAI_PATCH`：空（标准 OpenAI 无特殊 patch）

**新增 provider 流程（零代码改动）**：
1. `config.yaml` 加 `providers.xxx: {endpoint, api_key_env, patch: {...}}`
2. `factory.py` 自动识别 → `_build_openai(patch=ProviderPatch(...))` → provider 用 adapter 处理
3. 完成

---

## 5. 数据模型（PostgreSQL + pgvector）

### 5.1 DDL 要点

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE feeds (
  id BIGSERIAL PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('rss','api','scrape')),
  name TEXT NOT NULL, url TEXT NOT NULL,
  enabled BOOLEAN DEFAULT true,
  config_json JSONB,            -- api/scrape 规格
  etag TEXT, last_modified TEXT,
  last_fetched_at TIMESTAMPTZ, fetch_status TEXT,
  fetch_failures INT DEFAULT 0, last_error TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE articles (
  id BIGSERIAL PRIMARY KEY,
  feed_id BIGINT REFERENCES feeds(id) ON DELETE SET NULL,
  source_url TEXT NOT NULL,
  url_hash TEXT UNIQUE NOT NULL,     -- sha256(canonical url)
  content_hash TEXT NOT NULL,        -- sha256(cleaned text)
  title TEXT NOT NULL, author TEXT,
  published_at TIMESTAMPTZ, fetched_at TIMESTAMPTZ DEFAULT now(),
  content_text TEXT, content_md TEXT, lang TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','processing','done','unparseable','error')),
  dedupe_of BIGINT REFERENCES articles(id) ON DELETE SET NULL,  -- 指向被合并的原始文章（被删则置空转独立）
  mention_count INT DEFAULT 1, word_count INT,
  tsv tsvector,                      -- 关键词全文索引列
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX articles_tsv_idx ON articles USING GIN (tsv);

CREATE TABLE article_versions (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('raw_html','raw_text')),
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
-- 写入时机（§13 保留策略）：
--   raw_text：always（清洗后正文，体积可控、供重处理/重切词，每篇文章 1 行）
--   raw_html：按需（仅 unparseable / content_hash 变更需重抓时留档，正常文章不存原始 HTML 省体积）
-- 保留：P3 加 retention 任务按策略（时间/体积/状态）裁剪；P1 暂不清理但须知晓增长边界

CREATE TABLE article_embeddings (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                -- 'title' | 'summary' | 'body'
  model TEXT NOT NULL,               -- Qwen3-Embedding-8B-4bit-DWQ
  content_hash TEXT NOT NULL,        -- 该向量对应的文章内容版本
  dim INT NOT NULL,
  vector vector(1536),               -- 实测 1536 维，迁移时定死
  UNIQUE (article_id, kind, model)   -- upsert 保留最新；可多模型共存，search 固定查 config 指定的 active embed model（§7），切模型须 scripts/backfill 全量重嵌（§5.2）
);
CREATE INDEX emb_hnsw_idx ON article_embeddings
  USING hnsw (vector vector_cosine_ops);  -- 建索引期只设 ef_construction=128（HNSW WITH 仅接受 m / ef_construction；ef_search 是查询期 GUC，`SET hnsw.ef_search=64`）。全表索引 = 单 active model 时的默认形态；多模型 A/B 共存时切 partial 索引（§5.2 实现提示）
-- 单 active model 时无需 partial；A/B 切换通过 scripts/backfill 触发 DROP+CREATE partial 索引重建

CREATE TABLE processing_jobs (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  task TEXT NOT NULL
    CHECK (task IN ('summarize','translate','entities','topics','wiki','embed_core','embed_summary')),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','superseded')),
  content_hash TEXT,                 -- 入队时的文章内容版本
  attempt INT DEFAULT 0, max_attempts INT DEFAULT 3,   -- attempt 仅在永久类错误自增（§6 失败 SQL 分路径）；领取 SQL 不自增 attempt（硬伤 2）
  error_class TEXT,                  -- 'transient' | 'permanent'：瞬时不自增 attempt、永不死信；永久自增 + max_attempts 死信；超时转永久走 'permanent' 不再独立枚举（避免悬空值，硬伤小注）
  consecutive_timeouts INT DEFAULT 0, -- 同 job 同 content_hash 连续超时计数；成功清零，达 llm.max_timeout_retries 且 healthcheck ok → 直接 failed 死信（§6 矩阵）
  recover_count INT DEFAULT 0,       -- recover_interrupted() 回收次数（不是按 error 字符串 append，避免反复 recover 时 error 字段无限增长）
  priority INT DEFAULT 5,
  payload_json JSONB, result_json JSONB, error TEXT,
  lock_until TIMESTAMPTZ,            -- SKIP LOCKED 领取 + 退避门控
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ
);
-- 活跃态唯一：同一 (article, task) 只允许一条 queued/running，杜绝重复入队
-- superseded/succeeded/failed 不占槽位 → 合法重处理不受影响
-- embed 必须拆为 embed_core/embed_summary 两个 task（§5.2/§6）：唯一索引只看 (article_id, task)、不看 payload
--   kind；若共用 embed 一个 task，embed_core 退避占槽（lock_until 内仍 queued）会静默吞掉后补的
--   embed_summary 入队 → summary 向量永久缺失
CREATE UNIQUE INDEX processing_jobs_active_uniq
  ON processing_jobs (article_id, task)
  WHERE status IN ('queued','running');

-- 领取查询索引：只索引 queued（活跃集小），ORDER BY priority, created_at 与索引序一致
-- lock_until 的 OR 条件无法入索引，但 queued 集受 fetch 周期约束，作残留过滤即可
-- 与上面的防重索引独立、各司其职
CREATE INDEX processing_jobs_queued_idx
  ON processing_jobs (priority, created_at)
  WHERE status = 'queued';

CREATE TABLE summaries (
  id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  lang TEXT DEFAULT 'zh', model TEXT, content_hash TEXT,
  summary_text TEXT, key_points_json JSONB, confidence NUMERIC,  -- 由提示词产出（§4.5），0.0-1.0
  UNIQUE (article_id, lang, model)   -- upsert 保留最新，content_hash 记录版本
);
CREATE TABLE translations (
  id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  src_lang TEXT, tgt_lang TEXT DEFAULT 'zh', model TEXT, content_hash TEXT,
  translated_title TEXT, translated_content TEXT, created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (article_id, src_lang, tgt_lang, model)  -- upsert 保留最新
);
CREATE TABLE entities (
  id BIGSERIAL PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE,
  aliases_json JSONB, entity_type TEXT, description TEXT,
  first_seen_at TIMESTAMPTZ DEFAULT now(), last_seen_at TIMESTAMPTZ,
  mention_count INT DEFAULT 0, confidence NUMERIC
);
CREATE TABLE relations (
  id BIGSERIAL PRIMARY KEY, subject_id BIGINT REFERENCES entities(id),
  predicate TEXT, object_id BIGINT REFERENCES entities(id),
  source_article_id BIGINT REFERENCES articles(id) ON DELETE SET NULL, confidence NUMERIC,
  first_seen_at TIMESTAMPTZ DEFAULT now(), last_seen_at TIMESTAMPTZ,
  UNIQUE (subject_id, predicate, object_id)
);
CREATE TABLE topics (
  id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL,
  description TEXT, keywords_json JSONB, enabled BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE article_topics (
  id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE, topic_id BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  score NUMERIC, method TEXT CHECK (method IN ('keyword','llm')),
  UNIQUE (article_id, topic_id)
);
CREATE TABLE wiki_pages (
  id BIGSERIAL PRIMARY KEY, kind TEXT CHECK (kind IN ('article','topic','entity','manual')),
  ref_id BIGINT,                    -- 多态引用：按 kind 指向 article/topic/entity（manual 无 ref），单一 FK 不可行；删 article/topic/entity 时应用层同事务删对应 wiki_page（PRD §6），P3 归档受益
  title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
  content_md TEXT, related_json JSONB, updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE reports (
  id BIGSERIAL PRIMARY KEY, report_type TEXT CHECK (report_type IN ('daily','weekly')),
  period_start DATE, period_end DATE, content_md TEXT, content_html TEXT,
  stats_json JSONB, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE fetch_events (
  id BIGSERIAL PRIMARY KEY, feed_id BIGINT REFERENCES feeds(id) ON DELETE CASCADE, event_type TEXT,
  ok BOOLEAN, error TEXT, item_count INT, created_at TIMESTAMPTZ DEFAULT now()
);
```

### 5.1.5 Phase 2 增量 DDL（与 §14 切片同步迁移）

Phase 2 表已经在 Phase 1 DDL 预创建（`entities`、`relations`、`translations`、`reports`、`wiki_pages`），但 Phase 2 任务对字段/索引有微调，落到 Alembic 增量迁移：

#### `entities` 表 Phase 2 增量（切片 2.3）

```sql
-- 弃用 canonical_name UNIQUE：跨语言/别名的 entity 必须按 (entity_type, canonical_name_zh) 归并
ALTER TABLE entities RENAME COLUMN canonical_name TO canonical_name_zh;
ALTER TABLE entities DROP CONSTRAINT entities_canonical_name_key;
ALTER TABLE entities ADD CONSTRAINT entities_uniq_per_type_zh
  UNIQUE (entity_type, canonical_name_zh);
CREATE INDEX entities_aliases_gin_idx ON entities USING GIN (aliases_json);
```

迁移步骤：
1. 现有数据若有 `(type='org', canonical_name='OpenAI')` 与 `(type='org', canonical_name='开放AI')` 两条，`merge_aliases()` 服务先把后者 `aliases_json` 并入前者 `aliases_json`、再 DELETE 后者
2. 切换 UNIQUE 约束、加 GIN 索引

#### `relations` 表 Phase 2 增量（切片 2.3 + 2.4）

```sql
-- 同一三元组 (s, p, o) 可能在多篇文章里出现；
-- source_article_id 改成 JSONB 列表维护所有来源文章，避免多行 UNIQUE 冲突丢失
ALTER TABLE relations ADD COLUMN source_articles_json JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE relations ALTER COLUMN source_article_id DROP NOT NULL;
CREATE INDEX relations_subject_idx ON relations (subject_id);
CREATE INDEX relations_object_idx ON relations (object_id);
```

图谱遍历：

```sql
-- 给定实体 X 的 1-hop 邻接（subject 方向）
SELECT r.predicate, e2.canonical_name_zh, r.confidence
FROM relations r JOIN entities e2 ON e2.id = r.object_id
WHERE r.subject_id = :eid
UNION ALL
SELECT r.predicate, e1.canonical_name_zh, r.confidence
FROM relations r JOIN entities e1 ON e1.id = r.subject_id
WHERE r.object_id = :eid;
```

#### `wiki_pages` 表 Phase 2 增量（切片 2.4）

```sql
-- Wiki 全文索引列（与 articles.tsv 同模式，但 Phase 1 DDL 漏了）
ALTER TABLE wiki_pages ADD COLUMN tsv tsvector;
CREATE INDEX wiki_tsv_idx ON wiki_pages USING GIN (tsv);
-- search() 跨表 UNION：articles ∪ wiki_pages（§7 切片 2.6）
```

#### `reports` 表 Phase 2 增量（切片 2.5）

```sql
-- 失败可重试：需要 status / error / started_at / completed_at
ALTER TABLE reports ADD COLUMN status TEXT NOT NULL DEFAULT 'succeeded'
  CHECK (status IN ('pending','running','succeeded','failed'));
ALTER TABLE reports ADD COLUMN started_at TIMESTAMPTZ;
ALTER TABLE reports ADD COLUMN completed_at TIMESTAMPTZ;
ALTER TABLE reports ADD COLUMN error TEXT;
-- 同一 period 同一 type 唯一：避免日重复生成（覆盖即可）
CREATE UNIQUE INDEX reports_period_uniq ON reports (report_type, period_start, period_end);
```

#### `processing_jobs` 任务枚举 Phase 2 增量（切片 2.3 + 2.6）

```sql
-- 当前 CHECK 已含 'summarize','translate','entities','topics','wiki','embed_core','embed_summary'
-- Phase 2 新增：实体/主题 wiki 任务独立入队
ALTER TABLE processing_jobs DROP CONSTRAINT processing_jobs_task_check;
ALTER TABLE processing_jobs ADD CONSTRAINT processing_jobs_task_check
  CHECK (task IN ('summarize','translate','extract_entities','topics','wiki',
                  'generate_entity_wiki','generate_topic_wiki',
                  'embed_core','embed_summary'));
```

#### DDL 迁移顺序与兼容性

1. 所有 ALTER TABLE 均 `IF EXISTS/IF NOT EXISTS` 幂等（多次跑脚本不报错）
2. 新增 CHECK 约束前先 `UPDATE processing_jobs SET task=...` 把已存在的未知 task 映射到合法值（不能因为加约束让 worker 任务跑挂）
3. 索引加 `CONCURRENTLY`（生产无锁），dev 阶段无所谓

### 5.2 向量维度决策（MRL 截断至 1536）

- `Qwen3-Embedding-8B-4bit-DWQ` **模型最大输出 4096 维**，但 pgvector HNSW 索引有 2000 维硬上限（索引元组进 8KB 页），4096 直接建不了；`halfvec` 最多 4000 仍不够
- **选定 1536 维**：oMLX `/v1/embeddings` 支持 OpenAI 风格 `dimensions` 参数，**服务端按值截断**（已实测：4096/2048/1536/1024/512 全部按值返回，缺省 4096）；Qwen3-Embedding 套娃训练（MRL）原生支持 32–4096 区间任意维度，1536 是 OpenAI text-embedding-3-large 同档推荐值，质量损失极小、存储省 5/8、HNSW 余量充足
- 迁移 DDL 用 `vector(1536)`，`config.db.vector_dim=1536`，**两端必须一致**（启动时校验）
- 保留运行时校验：`embed` 返回维度 ≠ 1536 → 告警并阻断写入（防 HNSW 失配 / 模型切换 / 截断参数被改）

**embedding 长文策略**：
- 语义检索**主依赖 `title` + `summary` 的向量**（天然短、安全），`body` 向量只作补充
- `body` embed 设 `max_tokens≈8192` 截断（`Qwen3-Embedding-8B` max_model_len=15360，长文行为未实测，规避超限/静默截断风险）
- **`embed_core` / `embed_summary` 两个独立任务，按 payload 写 `article_embeddings`**：新文章入队 `embed_core` 写 `title` + `body` 两行（均可即时就绪）；`summary` 向量在 `summarize` 成功后由 `embed_summary` 补写（upsert `kind='summary'`）——避免 embed 先于 summary 执行时拿空文本建向量
- **拆成两个 task 值**（而非同一 task 的不同 kind payload）：入队幂等/活跃唯一只看 `(article_id, task)`（§5.1/§6），若共用 `embed` 一 task，`embed_core` 遇 LLM 退避卡在 `lock_until`（仍 `queued` 占槽）期间，`summarize` 成功后补入的 summary embed 会撞活跃槽被 `ON CONFLICT DO NOTHING` 静默丢弃，`summary` 向量将永久缺失；拆开后两次入队天然不冲突、退避互不影响，也无需"embed 顺带补 summary"的兜底逻辑
- `embed_core` 写 2 行（title+body）、`embed_summary` 写 1 行（summary），单任务粒度暂不可拆；Phase 2 再上正文分块 + 池化

**嵌入模型切换与 backfill**：
- `article_embeddings` UNIQUE 含 `model`，可多模型共存；但 `search(q)` 语义通道固定查 config 指定的 **active embed model**（`WHERE model = <active>`，§7），跨模型 top-k 量纲不一不可混
- 切嵌入模型 = `scripts/backfill` 全量重嵌（按 article 重跑 embed_core/embed_summary）+ config 切 active model；旧向量可选清理或留作 A/B（留则 search 仍只查 active，不混入）
- PRD §11 `scripts/backfill` 入口；backfill 期间 search 用旧 active 直到切换点，避免半新半旧
- **HNSW + `WHERE model=<active>` 过滤对策**：§5.1 的 `emb_hnsw_idx` 建在全表 `vector` 上，pgvector HNSW 不原生支持高效过滤检索——多模型共存（留旧模型做 A/B）时 `WHERE model=<active>` 会先做近似最近邻再过滤，候选池被过滤后实际召回数可能不足、需放大 `ef_search` 补偿、P95 上升。**对策（推荐）**：active 模型走 **partial HNSW 索引** `CREATE INDEX ... ON article_embeddings USING hnsw (vector vector_cosine_ops) WHERE model = '<active>'`，切 active model 时重建该 partial 索引；旧模型向量不进该索引、不影响检索性能，仍可做 A/B（走全表扫或各自 partial 索引）。单 active 模型（不留 A/B）时全表索引即可，partial 是为「A/B 共存 + 检索不降速」兜底。文档化：A/B 期间若不建 partial 索引，检索性能预期下降、需调高 `ef_search`
- **partial HNSW 索引实现提示**：PG prepared statement 把 `WHERE model = $1` 视为参数化条件时，planner 可能不匹配 partial 索引谓词（partial index 谓词通常需要常量才能被选中）。实现 search() 时**将 active model 名以字面量拼进查询字符串**——它来自 config 而非用户输入、无注入风险；或保留参数化但每次 query 后 `EXPLAIN` 验证走了 partial 索引。`scripts/backfill` 切 active 模型时同步 `DROP INDEX` + `CREATE INDEX` 重建 partial（HNSW 索引 CREATE 较慢、万级向量分钟级，注意 scheduler 时间窗避开 fetch_all）

### 5.3 全文检索（中文友好）

- `articles.tsv tsvector('simple', ...)`：插入前用 **jieba** 对 title+content 预切词为空格分隔 tokens，再**以拼接后的字符串** `to_tsvector('simple', '<tokens-joined>')` 写入（**不要**用 `'a b c'::tsvector` 或 `array_to_tsvector` 手工构造——simple 词典的 lowercase / token 归一化只在 `to_tsvector` 内部走，绕过去会大小写漏配、词形不归一）
- GIN 索引；避免依赖需编译的 `zhparser`
- `wiki_pages` 同理建 `wiki_tsv`
- **tsv 两阶段刷新（防 content 变更后陈旧）**：① 文章入库时即时刷新 tsv 的 `title + content_text` 段（jieba 切词并入）——content 变更后即使尚未重跑 summarize，关键词通道对原文段也是新的；② `summarize` 成功后补刷 `summary_zh + key_points` 段（§6 `complete_summarize` 同事务）。两段拼接用同一条 `jieba_join(title || ' ' || content_text || ' ' || summary_zh || ' ' || key_points_text)`，确保中文关键词能命中英文文章的摘要表述
- **查询侧**：用 `websearch_to_tsquery('simple', jieba(q))` 或 `plainto_tsquery('simple', jieba(q))`，**不要**用裸 `to_tsquery`——jieba 切出的 token 可能含 `&` `:` `(` `)` 等保留字符，裸 `to_tsquery` 会直接报语法错误；`websearch_to_tsquery` 会自动把非操作符 token 用 `&` 串起来，最安全

### 5.4 开发环境（Docker Compose，已确认）

`docker-compose.yml`（项目根）：

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17      # 内置 vector 扩展
    container_name: tc-postgres
    environment:
      POSTGRES_USER: tc
      POSTGRES_PASSWORD: tc            # 仅本地 dev；生产/非本机改 env 覆盖
      POSTGRES_DB: topic_collection
    ports:
      - "127.0.0.1:5433:5432"          # 宿主机 5433 避免与本地 PG 冲突
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tc -d topic_collection"]
      interval: 5s
      timeout: 5s
      retries: 5
volumes:
  pgdata:
```

使用流程：
```bash
docker compose up -d                  # 起库
python -m scripts.init_db             # CREATE EXTENSION vector + alembic upgrade head（幂等）
docker compose down                   # 停库（数据保留在 pgdata 卷）
```
- DSN 与 §9 一致：`postgresql+asyncpg://tc:tc@localhost:5433/topic_collection`（宿主机 5433 映射容器 5432，见 docker-compose.yml）
- **schema 唯一真源 = Alembic 迁移**：`scripts/init_db` 只做两件事——① `CREATE EXTENSION IF NOT EXISTS vector;`（`pgvector/pgvector:pg17` 镜像已内置，无需额外安装）；② `alembic upgrade head`（建表全走迁移，**不写裸 DDL**）。§5.1 的 DDL 是「迁移产物的参考快照」、不是另一条建表路径——裸 `CREATE TABLE` 会与首迁移冲突（表已存在报错 / alembic 版本表对不上），且与 §14 切片一「Alembic 迁移 + 扩展/维度校验」重复造轮子

---

## 6. 数据流水线 & 状态机

```
fetch → normalize → dedup(url_hash/content_hash) → clean → 入队 processing_jobs
       → LLM 各阶段(embed_core → summarize → embed_summary → topics → wiki) → 图谱/词条 → tsv/向量索引
       → 跨源向量近似去重(§6)：embed_core 落地后、complete_embed 钩子里对近 window_days 文章做 cosine ≤ 1-threshold
         → 命中走 loser status='done' + supersede + dedupe_of 合并（在 summarize 被领取前拦截，§6 complete_embed 职责②）
```
**事务边界**：每篇文章的 `insert article → enqueue jobs` 在同一事务（崩溃不留孤儿文章）；supersede 旧 job 与新 job 入队同事务（见下）。

**文章状态机**：`pending → processing → done | unparseable | error`（部分任务失败仍可 `done`，详情页可重试单个任务）
- **迁移触发点（规格明确，否则实现会一直停在 pending，触发硬伤 3）**：
  - `pending → processing`：**`enqueue_jobs()` 入队时同事务** `UPDATE articles SET status='processing' WHERE id=:aid AND status='pending'`（status 守卫幂等）。实现位置在 `app/pipeline.py` 的 `enqueue_jobs` 头部，最早调用方是 fetch 后入队 + `complete_summarize` 的 cascade 入队。**比 worker pick-and-claim 更靠前触发**，让 `tc list` 在入队后即看到 processing 而非 pending。
  - `processing → done`：**每次** job 进入终态（`succeeded` / `failed` / `superseded`）后，同事务检查：该文章不存在任何 `queued` 或 `running` job → `UPDATE articles SET status='done' WHERE id=$1 AND status='processing'`（**且** `dedupe_of IS NULL`，非 loser）。适用所有路径：关键词命中（topics job 失去入队资格，天然缺席 → 该 job 落地后无剩余 queued/running → 自动 done）、任务失败（fail 后无剩余 → 自动 done）、supersede（旧 job superseded 后若新 job 已不存在 → 自动 done）；embed_summary / translate 失败不阻塞 done（缺席即终态）。**不再维护"非可选 task 清单"**——与任务集合无关的规则不会因新增/删除 task 而产生遗漏
  - `pending → unparseable`：cleaner 阶段 `articles.status='unparseable'` 同事务写入，**跳过所有 LLM 入队**（§6 cleaner / 入队规则表）；worker 领取前查询 articles.status，发现 unparseable **直接标记 `status='superseded'`**（不跳过——跳过的 job 会停在 `running` 直到租约过期被 `recover_interrupted` 回收、再领取再跳过，循环；实际上 unparseable 文章不入队任何 job，此防御分支极少触发，但既然写了就写完整）
  - `processing → error`：保留（当前未触发，留 P3）
  - **近似去重命中时 loser 直接 `done`**：`complete_embed(article_id, kind='body')` 判定命中后，**同一事务**：
    1. `UPDATE articles SET status='done', dedupe_of=$winner WHERE id=$1 AND status='processing'`——loser 不再是 `pending`，drain_queue 谓词按 `status='pending'` 过滤不再补队，**这是阻断 loser 周期性复活的关键**（详见 backpressure 段）；
    2. `UPDATE processing_jobs SET status='superseded' WHERE article_id=$1 AND task IN ('summarize','topics','wiki') AND status IN ('queued','running')`；
    3. `DELETE FROM article_topics WHERE article_id=$1`——双保险（即便 §6 主题聚合过滤写了 `dedupe_of IS NULL`，loser 在判定之前写入的 article_topics 行也得删，否则主题视图重复占位、验收 #16 挂；见中等 5）；
    4. `INSERT INTO fetch_events (event_type='dedup_merge', ok=true, ...)`（审计）
  - **recover 不动 articles.status**：recover 只回收过期 `running` job → `queued`；articles 状态由 job 推进自动跟随，恢复期间文章保持 `processing`，恢复后 job 继续跑、自然 → `done`。若 recover 改 status 会引入「恢复期间 status 反复横跳」的复杂度
  - **§13 测试 D6 必须断言**：mock「文章在 done 之前 status 反复横跳、loser 文章在 dedup 后不再被 drain_queue 补队」

**入队规则（按任务）**：
| 任务 | 优先级 | 触发 | 模型 |
|---|---|---|---|
| `embed_core` | 1 | 新文章（title+body） | Qwen3-Embedding-8B |
| `summarize` | 2 | 新文章；**近似去重命中后跳过**（活跃 job 走 supersede，§6 去重段） | Qwen3.8-27B |
| `topics` | 3 | **`summarize` 落地后 + 关键词未命中**（LLM 读 `summary_zh` 而非外文全文，token 省一个量级、跨语言主题判定更稳）；主题变更（重算，限窗口见 §6） | Qwen3.8-27B |
| `extract_entities` | 3（与 topics 并列优先级；FIFO 排序选其一） | `summarize` 完成后（cascade，同事务入队）；可手动 `tc article <id> extract` | Qwen3.8-27B |
| `wiki` | 4 | 摘要落地后（Phase 1 article wiki） | Qwen3.8-27B |
| `generate_topic_wiki` | 5 | `topics` 完成后首次落地 / 主题关键词变更（`tc topic edit`） | Qwen3.8-27B |
| `generate_entity_wiki` | 5（与 generate_topic_wiki 并列 FIFO） | `extract_entities` 完成后，**只在**新 entity 或 entity.description 显著变更时入队 | Qwen3.8-27B |
| `embed_summary` | 6 | `summarize` 成功后（summary）**或手动 `tc retry summarize`**——**必须走同一条钩子 `complete_summarize()`，不能只有自动流水线触发**（否则手动重生成后 summary 向量停在旧版本） | Qwen3-Embedding-8B |
| `translate` | 7（最低；Phase 2 用户触发为主） | 自动：`lang != 'zh' AND` 用户设置 `ingestion.auto_translate: true`；手动：WebUI "翻译" 按钮 / `tc translate <article_id>` | Qwen3.8-27B |

**Phase 2 入队补全（与 §14 切片对应）**：

#### 任务级触发图

```text
新文章入库（ingest / api / scrape）：
  └─ embed_core
       └─ summarize (Phase 1)
            └─ embed_summary (Phase 1)
                 (去重命中则同事务 done; loser)
            └─ topics (Phase 1)
                 └─ generate_topic_wiki   (Phase 2 首次)
            └─ extract_entities          (Phase 2 cascade)
                 └─ generate_entity_wiki (Phase 2，仅新 entity)
            └─ wiki (Phase 1 article wiki)
```
**优先级配对原则**（数字越小越先）：embed_core(1) 必然先；summarize(2) 与 extract_entities(3) 串行（summarize 先，无内容不抽实体）；topics(3) 与 extract_entities(3) 同优先级 FIFO；wiki(4) 与 generate_*_wiki(5) 严格串行；embed_summary(6) 最后。

**并发冲突**：虽然 `extract_entities` 与 `topics` 同优先级 3，但 worker `pick_and_claim` 加 `FOR UPDATE SKIP LOCKED` 取一条，按 `ORDER BY priority, created_at` FIFO → 这两者按入队时间依次。**没有死锁**，因为 LLM 调用只读 `summaries` / `articles`，互不阻塞。

#### `extract_entities` 详细触发

- **`complete_summarize` 同事务** 入队 `extract_entities`（`enqueue_jobs(article_id, ['topics', 'extract_entities'], ...)`）
- 若文章 `match_keywords()` 命中 → `topics` 不入队（仅 `extract_entities` 入队）
- 手动 `tc article <id> extract`：同 `tc retry` 流程（§6），强制再跑（supersede + 新 job）

#### `generate_entity_wiki` 详细触发

`extract_entities` 完成后由 `complete_extract` 钩子（同事务）：

```sql
-- 对 extract_entities 输出的每个 entity，判是否需要生 wiki：
SELECT e.id FROM entities e
WHERE e.id = ANY(:extracted_ids)
  AND (
    NOT EXISTS (SELECT 1 FROM wiki_pages WHERE ref_id = e.id AND kind = 'entity')
    OR EXISTS (
      SELECT 1 FROM entity_change_log
      WHERE entity_id = e.id AND changed_at > (SELECT updated_at FROM wiki_pages WHERE ref_id = e.id LIMIT 1)
    )
  );
-- 命中 → enqueue_jobs 多个 generate_entity_wiki（同任务 type 共用 priority 5，FIFO）
```

`entity_change_log` 临时表（或者简单做法：每次 extract 实测时比对 `description` 是否变化 ≥ N%、aliases 是否有新项；满足则触发生成）。Phase 2 切片 2.3 实施细节定。

#### `generate_topic_wiki` 详细触发

`topics` 完成后由 `complete_topics` 钩子（同事务）：

- **首次**：topic 完成无 wiki_page → 入队 `generate_topic_wiki`
- **关键词变更**：用户 `tc topic edit` 或 `tc topic add` 同步触发近 30 天 reclassify（§6 主题分类规则）→ 同一事务 supersede 旧 `generate_topic_wiki` job + 入队新 job

#### `translate` 详细触发

- **自动**：`ingestion.auto_translate: true`（config，§9）→ `cleaner.clean_article()` 阶段在 `articles` 写入后立即 `enqueue_jobs(article_id, ['translate'], ...)`
- **手动**：
  - `tc translate <article_id>`：CLI 命令
  - WebUI `/articles/{id}` 详情页 → POST `/articles/{id}/retry/translate` 入队
- **结果落表**：`translations` 表（Phase 1 DDL 已就绪）；`articles.translated_content` **不复制**（避免双数据源，UI 查 `translations`）
- **LLM 输入**：原文 + `key_points`（同 articles）+ `target_language: 'zh'`（prompt 强制输出简体中文，§4.6）
- **content_hash 版本守卫**：`UNIQUE(article_id, src_lang, tgt_lang, model)` upsert + `WHERE EXCLUDED.content_hash = (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id)`（§6 `complete_*` 通用守卫，与 summaries 同模式）

#### Phase 2 入队规则补充说明

- **`extract_entities` / `generate_entity_wiki` / `generate_topic_wiki` 都是幂等入队**（§6 入队语义，`ON CONFLICT DO NOTHING`）：重复 cascade 不会创建重复 job
- **`translate` 与 `summarize` 在同一篇文章上不冲突**：summarize 读 articles.content_text，translate 也读 articles.content_text；两个任务并发跑没有写竞争，content_hash 守卫各自负责（§6 状态机原子性段）
- **`generate_*_wiki` 与 `wiki` 同帧**：article wiki 在 `summarize` 后即生成；entity/topic wiki 在 extract/classify 后才生成——它们彼此独立、不互相依赖
- **取消 / 跳过**：用户可 `tc retry <article_id> <task>` 强制 supersede（§6 重试入口）；手动 `tc article <id> extract` 同效果

**backpressure**：单次 fetch 每个 feed **入库不限**（文章全量写 `articles`，入库便宜、丢文章无法挽回），**仅限 LLM job 入队数** `ingestion.max_items_per_fetch`（默认 50），超限截断**入队**并记 `fetch_events` 水位告警——并发=1 下千条 feed 首抓会积压数小时（27B 20–60s/篇），不限流会让 `fetch_interval_hours` 越积越多。`drain_queue`（§10）每 30s 额外扫描一次补入队，**谓词精确**：`WHERE a.status='pending' AND a.dedupe_of IS NULL AND NOT EXISTS (SELECT 1 FROM processing_jobs j WHERE j.article_id=a.id)`——精确命中「被截断未入队」的文章（状态仍 pending、未被 dedup 命中、不存在任何 processing_jobs 行），**不碰任何处理过或已被 dedup 命中的文章**。旧谓词「`status='pending' 且无任何**活跃** job`」的三大连锁问题（详见 §6 文章状态机迁移触发点）：
- 文章若实现时一直停在 `pending`（极易发生，旧文档未定义迁移触发点），处理过的文章（succeeded job 非活跃）每 30s 被重新入队——全库空转
- dedup loser：命中后 job 全 superseded、status 仍 pending → 30s 后补队 → 重新 embed → 再命中 → 再 supersede → 无限循环，每轮烧一次 8B 嵌入
- 即便改谓词为「无任何 job」，drain_queue 自己清 superseded → loser 变「无任何 job」 → 重新入队 → 死循环
- 新谓词三个条件互锁：必须是 pending（处理过的不在）、必须 dedupe_of IS NULL（loser 不复活）、必须零 job 记录（被截断未入队的特征）——任何一种漏判都被三条件之一挡掉。**配合状态机的 loser 直接 `done`（§6 文章状态机）**双保险闭环。分批回灌策略 P3 再做更精细的水位调度。

**抓取并发（不打爆对端 / 不被对端封）**：
- **全局并发**：`ingestion.global_concurrency`（默认 8）`asyncio.Semaphore`——多 feed 同时抓取时不瞬时起百连接
- **每域限速**：`ingestion.per_host_interval_ms`（默认 500ms）——同一 host 上一次抓取结束后至少等 N ms 再发起下一次，避免被 RSS 服务端识别为机器人/触发 429/被临时封
- **实现位置**：`app/ingest/feeds.py`（RSS）/ `app/ingest/api.py`（P2）/ `app/ingest/scrape.py`（P3），per-host 间隔用每 host `last_request_at: dict[str, float]` + `await asyncio.sleep(...)` 守护
- **降级**：host 持续返回 429/5xx → 进 `feeds.fetch_failures` 计数，达 `feed_disable_after` 自动禁用（§6 重试矩阵）

**Phase 1 wiki 词条 `related_json` 规范**：Phase 1 不抽实体（`entities` task 不入队），`related_json` = 同主题 article 列表（来自 `article_topics`，按 `score DESC, published_at DESC` 取前 5）；P2 实体抽取上线后，`related_json` 合并"同主题 + 共现实体"两组链接

### 6.X Phase 2 wiki 完整版（切片 2.4 + 2.5 / 2.6 完整 Wiki）

Phase 1 wiki 仅 `kind='article'` 一种。Phase 2 完整 Wiki：每篇 wiki_pages 一篇词条，按 `kind` 分四种：

#### `wiki_pages.slug` 命名规则（Phase 2）

| kind | slug 模板 | 例子 |
|---|---|---|
| `article` | `<title-slugified>-<article_id>` | `qwen3-launches-and-evaluates-2026-1234` |
| `topic` | `topic-<topic.name-slugified>` | `topic-rag` |
| `entity` | `entity-<canonical_name_zh-slugified>` | `entity-tongyiqianwen-3` |
| `manual` | 用户提供 slug（unique 校验） | `index`、`welcome` |

**冲突处理**：
- 同 `kind=article` 用 article_id 后缀即可（DB 已 UNIQUE）
- 同 `kind=topic/entity` 用 `topic-{name}-{topic_id}` / `entity-{zh-name}-{entity_id}` 末尾追加 id 保证全局唯一
- `kind=manual` slug 用户输入时校验 DB UNIQUE，冲突 422 + 提示已有的 slug

#### `related_json` 三组合并算法（Phase 2）

```sql
-- 给定 article_id，目标：related_json = 去重、合并同篇后的 top 10
-- 数据源：
WITH same_topic AS (
  SELECT a.id, a.title, at.score, 'topic' AS src
  FROM article_topics at
  JOIN articles a ON a.id = at.article_id
  WHERE at.topic_id IN (SELECT topic_id FROM article_topics WHERE article_id = :aid)
    AND a.dedupe_of IS NULL AND a.id != :aid
  ORDER BY at.score DESC, a.published_at DESC LIMIT 5
),
same_entity AS (
  SELECT a.id, a.title, 0.5 AS score, 'entity' AS src
  FROM entities e
  JOIN relations r ON (r.subject_id = e.id OR r.object_id = e.id)
  JOIN articles a ON (a.id = r.source_article_id)
  WHERE e.id IN (
    -- 当前文章涉及的 entities（来自 article_entities 关联表，本计划未列，留 §16 限制）
    -- Phase 2 引入 article_entities 表；切片 2.3 实施时落地
  )
    AND a.dedupe_of IS NULL AND a.id != :aid
  GROUP BY a.id, a.title
  ORDER BY COUNT(*) DESC, a.published_at DESC LIMIT 5
),
same_feed AS (
  SELECT a.id, a.title, 0.3 AS score, 'feed' AS src
  FROM articles a
  WHERE a.feed_id = (SELECT feed_id FROM articles WHERE id = :aid)
    AND a.id != :aid AND a.dedupe_of IS NULL
  ORDER BY a.published_at DESC LIMIT 3
)
SELECT jsonb_agg(jsonb_build_object('id', id, 'title', title, 'src', src, 'score', score) ORDER BY score DESC) AS related_json
FROM (
  SELECT * FROM same_topic
  UNION SELECT * FROM same_entity
  UNION SELECT * FROM same_feed
) all_rel LIMIT 10;
```

**输出 `related_json`**：list[dict] 每个含 `id / title / src (topic|entity|feed) / score`；store as JSONB in `wiki_pages.related_json`。前端的 wiki 页右侧栏按 `src` 分组渲染（"相关话题"、"共现实体"、"同源文章"）。

#### `article_entities` 表（Phase 2 切片 2.3 必备，DDL 在此声明，迁移脚本见 §5.1）

```sql
-- 当前文章涉及的实体 = 抽取产物的落地表
CREATE TABLE article_entities (
  article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  entity_id BIGINT REFERENCES entities(id) ON DELETE CASCADE,
  confidence NUMERIC,
  surface TEXT,                       -- 该 entity 在本文出现的原文子串
  first_seen_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (article_id, entity_id)
);
CREATE INDEX article_entities_entity_idx ON article_entities (entity_id);
```

无此表 → `related_json` 算法拿不到"本文涉及的 entities"，等同 §16 限制。

#### Wiki 渲染（Phase 2 Markdown 模板）

- **article wiki**：标题 + 摘要 + 要点 + 原文链接 + `## 相关话题` + `## 共现实体` + `## 同源文章`（按 related_json 分组）+ `## 元数据` (lang, published_at, source_url)
- **topic wiki**：定义 + 启用关键词列表 + Top 50 相关文章表格 + `## 该主题下 Top 实体`（按 article_entities 聚合）+ 元数据
- **entity wiki**：别名卡片 + 描述 + `## 首次提及` + `## 关联文章` + `## 关系图`（ECharts 1-hop fragment）
- **manual wiki**：完全 Markdown，用户编辑

### 6.Y Phase 2 实体抽取与归并（切片 2.3 详细算法）

#### `extract_entities(article_id, content_text, lang)` → 输出 → 落库

```python
async def run_extract_entities(session, job, settings, llm_client):
    """读 article.content_text, 调 LLM, 写 entities + relations + article_entities"""
    article_id = job["article_id"]
    content_hash = job["content_hash"]
    
    art = await session.execute(
        text("SELECT title, content_text, lang FROM articles WHERE id=:aid"), {"aid": article_id}
    )
    row = art.mappings().first()
    if not row or not row["content_text"]:
        raise PermanentError(f"article {article_id} 内容为空")
    
    sys_p, user_p = get_prompt(
        "extract_entities", title=row["title"], content=row["content_text"][:8000], lang=row["lang"]
    )
    model = settings.llm.generate.model
    resp = await llm_client.generate(
        GenerateRequest(
            model=model,
            messages=[{"role": "system", "content": sys_p},
                      {"role": "user", "content": user_p}],
            json_mode=True,
        )
    )
    parsed = parse_with_repair(resp.text, expected_keys=["entities", "relations"])
    if not parsed:
        raise PermanentError(f"JSON 解析失败: {resp.text[:200]}")
    
    await complete_extract(session, article_id, content_hash, parsed, settings)


async def complete_extract(session, article_id, content_hash, parsed, settings):
    """公共钩子（同事务）：
    1. entities upsert（按 (entity_type, canonical_name_zh) UNIQUE 冲突；aliases/description/mention_count 合并）
    2. grounding 校验：surface 必须在原文；不通过 confidence *= 0.5 / 丢弃
    3. article_entities upsert（confidence, surface）
    4. relations upsert（按 (subject_id, predicate, object_id) UNIQUE 冲突；source_articles_json 追加）
    5. 决定 generate_entity_wiki 入队（仅 entity 是新的 / description 变更）
    6. check_and_set_done
    """
    # 1. entities upsert
    for ent in parsed.get("entities", []):
        # grounding 校验
        if ent.get("surface") and ent["surface"] not in content_text:
            ent["confidence"] = (ent.get("confidence") or 0.5) * 0.5
            if ent["confidence"] < 0.1:
                continue  # 丢弃
        # aliases_json 合并：现有 + 新
        await session.execute(
            text("""
                INSERT INTO entities (canonical_name_zh, aliases_json, entity_type, description, mention_count, confidence)
                VALUES (:zh, :aliases_json, :type, :desc, 1, :conf)
                ON CONFLICT (entity_type, canonical_name_zh) DO UPDATE SET
                  aliases_json = entities.aliases_json || EXCLUDED.aliases_json,
                  description = CASE WHEN EXCLUDED.confidence > entities.confidence
                                     THEN EXCLUDED.description ELSE entities.description END,
                  mention_count = entities.mention_count + 1,
                  confidence = GREATEST(entities.confidence, EXCLUDED.confidence),
                  last_seen_at = now()
            """),
            {
                "zh": ent["canonical_name_zh"],
                "aliases_json": json.dumps(ent.get("aliases", []), ensure_ascii=False),
                "type": ent.get("type", "other"),
                "desc": ent.get("description"),
                "conf": ent.get("confidence", 0.5),
            }
        )
    
    # 取回 entity_id 映射
    eid_map = await _build_entity_id_map(session, parsed)
    
    # 3. article_entities upsert
    for ent in parsed.get("entities", []):
        eid = eid_map.get(ent["canonical_name_zh"])
        if not eid:
            continue
        await session.execute(
            text("""
                INSERT INTO article_entities (article_id, entity_id, confidence, surface)
                VALUES (:a, :e, :c, :s)
                ON CONFLICT (article_id, entity_id) DO UPDATE SET
                  confidence = GREATEST(article_entities.confidence, EXCLUDED.confidence),
                  surface = EXCLUDED.surface
            """),
            {"a": article_id, "e": eid, "c": ent.get("confidence", 0.5), "s": ent.get("surface")}
        )
    
    # 4. relations upsert
    for rel in parsed.get("relations", []):
        sid = eid_map.get(rel["subject"])
        oid = eid_map.get(rel["object"])
        if not sid or not oid:
            continue
        await session.execute(
            text("""
                INSERT INTO relations (subject_id, predicate, object_id, source_articles_json, confidence, last_seen_at)
                VALUES (:s, :p, :o, jsonb_build_array(:aid::text), :c, now())
                ON CONFLICT (subject_id, predicate, object_id) DO UPDATE SET
                  source_articles_json = relations.source_articles_json || EXCLUDED.source_articles_json,
                  confidence = GREATEST(relations.confidence, EXCLUDED.confidence),
                  last_seen_at = now()
            """),
            {"s": sid, "p": rel["predicate"], "o": oid, "aid": article_id, "c": rel.get("confidence", 0.5)}
        )
    
    # 5. 决定 generate_entity_wiki 入队
    new_entity_ids = await _detect_new_or_changed_entities(session, article_id, eid_map.values())
    if new_entity_ids:
        await enqueue_jobs(session, article_id, ["generate_entity_wiki"], content_hash)
    
    # 6. done 检查
    await check_and_set_done(session, article_id)


async def _build_entity_id_map(session, parsed):
    """把 parsed.entities[].canonical_name_zh 映射回 entities.id（先 INSERT 再 SELECT）"""
    zh_names = [e["canonical_name_zh"] for e in parsed.get("entities", [])]
    if not zh_names:
        return {}
    r = await session.execute(
        text("SELECT id, canonical_name_zh FROM entities WHERE canonical_name_zh = ANY(:names)"),
        {"names": zh_names}
    )
    return {row["canonical_name_zh"]: row["id"] for row in r.mappings()}


async def _detect_new_or_changed_entities(session, article_id, entity_ids):
    """判断哪些 entity 是新出现的 / description 改了的，需要生 wiki。
    实现：对照 entities.description_old_json（触发器维护的旧值快照）的差异，
    或简化为：entity 出现首次（无 article 关联历史）→ 需要 wiki。
    本计划留具体策略给切片 2.3 实施。"""
    ...
```

#### 实体归并算法（merge_aliases 服务）

防止 LLM 在不同文章里对同一实体给出不同规范化名（"OpenAI" / "Open AI" / "开放AI" 等）：

```python
async def merge_aliases(session, alias: str, type: str, canonical_zh: str):
    """把 alias 折叠到 canonical_zh：模糊匹配 + 人工/规则合并。
    
    触发场景：用户 'tc topic edit' 改关键词、用户 'tc entity merge' 命令、periodic job 扫描。
    模糊匹配：pg_trgm (Postgres 内置) `similarity(a.canonical_name_zh, :alias) > 0.6`
    """
    # 1. 模糊查询候选
    candidates = await session.execute(text("""
        SELECT id, canonical_name_zh, aliases_json, mention_count
        FROM entities
        WHERE entity_type = :type
          AND (canonical_name_zh % :alias OR :alias = ANY(SELECT jsonb_array_elements_text(aliases_json)))
    """), {"type": type, "alias": alias})
    # 2. 相似度 > 0.6 的合并入主实体
    ...
```

`pg_trgm` 扩展 Phase 2 启用：`CREATE EXTENSION IF NOT EXISTS pg_trgm;`。

#### 性能与可扩展性

- **单篇抽取**：5–30 entity / 10–50 relations / 27B 30–60s / 篇
- **并发=1**：~50 篇/小时 串行；后台 worker 顺序跑，可接受（Phase 1 单进程方案）
- **批量回灌**：万篇级 P3 `tc reclassify --all` 类似 `tc extract --all`（enqueue 所有 `status='done'` 还未 `extract_entities` 的文章），放后台 worker 跑数小时；`recover_count + 1` 防崩溃续跑

**主题分类规则（关键词快路径 + LLM 慢路径，P1）**：
- **快路径（关键词预匹配）**：`match_keywords()` 对新文章 title+content 检查启用主题的关键词——命中即记 `article_topics(method='keyword')`，score 由命中强度计算（title 命中加权 + 命中词数），**命中即计入、不跑 LLM**（省调用）
- **慢路径（LLM 分类）**：**未命中任何关键词**的文章才进 `classify_topics` job——给定全部启用主题+关键词打分 0–1，`score ≥ 0.6`（可配 `topics.llm_threshold`）记 `method='llm'`
- **一致性**：`UNIQUE(article_id, topic_id)` 一篇文章对一主题仅一行；两路径按 (article, topic) **互斥**——关键词已命中的主题不再 LLM 复议（故不存在"关键词命中但 LLM 判低分"的冲突）；关键词命中的文章整体跳过 LLM 分类（P1 接受的召回取舍：不会跨主题发现未命中关键词的主题，P3 可补跑全量）
- **聚合排序**：`aggregate_topic()` 按 `score DESC, published_at DESC`；展示标注 method 来源（keyword/llm），可筛可解释
- **`dedupe_of IS NULL` 过滤是查询层的硬约束**：loser 文章在 match_keywords 阶段可能已经写入 `article_topics(method='keyword')` 行——之后 dedup 命中虽然 `dedupe_of` 置位但 topic 行还在，若 `aggregate_topic()` / 日报查询 / wiki `related_json` 不显式过滤 loser，主题视图与日报照样重复占位、验收 #16 挂。**统一规则**：所有主题/聚合查询（`aggregate_topic`、`reports.generate_*`、wiki `related_json` 构造、CLI `tc list --topic`）的 SQL 一律 `JOIN articles a ON a.id=at.article_id WHERE a.dedupe_of IS NULL`——防御性强、不依赖 dedup 事务里删 article_topics。**双保险**：dedup 命中同一事务 `DELETE FROM article_topics WHERE article_id=$1`（§6 dedup 命中段步骤 3），即便将来某条新查询忘了过滤、loser 行已被删；不写测试断言其中任何一环都会被绕过
- **主题变更重算（触发 = `tc topic add` / `tc topic edit` 同步执行）**：主题/关键词增改后**在 CLI 命令返回前同步重跑** `match_keywords()`——不再命中的旧 `method='keyword'` 行删除；未命中关键词的文章重新入队 `topics`（幂等 + 活跃态唯一约束保护）。**默认仅重算最近 `topics.reclassify_recent_days`（默认 30 天）文章**——历史几千篇 × 27B 的隐性回填成本极高，全量交给 P3 `tc reclassify --all`（PRD §15 #3 兜底 + §16 已知限制）。`tc topic add` 是高频写操作，同步触发近窗重算（match_keywords 是纯内存 jieba 匹配、毫秒级）可接受；`topics` job 入队后由常驻 worker 异步消费（§6 运维模式），CLI 不阻塞等 LLM

**入队语义（幂等 + 防重复，§5.1 部分唯一索引支撑）**：
- 幂等：`INSERT ... ON CONFLICT (article_id, task) WHERE status IN ('queued','running') DO NOTHING`，重复入队静默丢弃
- **`embed` 拆为 `embed_core` / `embed_summary` 两 task（§5.2）**：唯一约束覆盖 `(article_id, task)` 不含 payload kind，共用一 task 时 `embed_core` 在 `lock_until` 退避（`queued` 仍占槽）期间，`summarize` 成功触发的 summary embed 入队会撞槽被 `DO NOTHING` 吞掉 → `summary` 向量永久缺失；拆分后两批入队各自独立去重，互不阻塞
- 内容变更（活跃 job 期间 content_hash 变化）：旧 job 标 `superseded` + 新 job 入队**必须在同一事务**（消除 TOCTOU 窗口，否则两步之间另一路径可能又入一条活跃 job 撞唯一索引）：
  ```sql
  BEGIN;
  UPDATE processing_jobs SET status='superseded', updated_at=now()
    WHERE article_id=$1 AND task=$2 AND status IN ('queued','running');
  INSERT INTO processing_jobs (article_id, task, status, content_hash, priority, ...)
    VALUES ($1, $2, 'queued', $3, $4, ...) ON CONFLICT DO NOTHING;
  COMMIT;
  ```
- 优先级数值约定：`embed_core=1`、`summarize=2`、`topics=3`、`wiki=4`、`translate=5`、`embed_summary=6`——**27B 生成链（1→4）整体低于 8B 嵌入链（1 除外，6）**：worker 按 `ORDER BY priority, created_at` 领取，会先把 `embed_core`(1) 排空（新文章 title+body 向量就绪，供 dedup 与检索），再消费 `summarize/topics/wiki`(2/3/4)——27B 常驻一次处理完所有生成任务，**最后才切到 `embed_summary`(6)**——8B 常驻一次补完所有 summary 向量。**刻意把 embed_summary 压到 6 而非与 wiki 同级 4**：同优先级 + `created_at` 排序会让 wiki 与 embed_summary 交错领取（27B→8B→27B→8B 模型来回加载，正是要避免的 gen↔embed 抖动）；拉开到 6 后生成链先整体排空再切嵌入，单进程并发=1 下抖动最小。`embed_core=1` 是例外：新文章入队时 summary 还不存在、embed_core 是 dedup 触发的前置，必须最先。P2 切 per-capability 分槽（§4.4/§16）后 gen/embed 各占一槽、不再交替，此数值差异意义减弱，但仍是显式声明便于调优

**关键词通道的中文补全（`summarize` 成功后刷新 tsv）**：
- 入库时 `articles.tsv` 只覆盖 `title + content_text` 的 jieba tokens，英文文章对中文查询关键词召回是 0
- `summarize` 落地后立即 `UPDATE articles SET tsv=to_tsvector('simple', jieba_join(title || ' ' || content_text || ' ' || summary_zh || ' ' || key_points_text)) WHERE id=$1` —— 把 `summary_zh` 与 `key_points` 切词并入（§4.5 摘要产出是 `summary_text + key_points_json`）
- **同一事务里**做（与 `summaries` 行写入 + `embed_summary` 入队同事务），失败回滚——保证「中文摘要 → 关键词通道」原子生效
- **成本**：1 次 jieba 切词（内存级）+ 1 次 `to_tsvector`（PG 内部），可忽略
- **收益**：用户用中文搜英文文章也能命中摘要里的对应表述；与「中文友好」核心卖点直接相关，**Phase 1 必做**

**`complete_summarize(article_id, result)` 公共钩子（自动 + 手动重试都走）**：
- **职责**（同事务，全部或全部不发生）：
  1. `INSERT INTO summaries ... ON CONFLICT (article_id, lang, model) DO UPDATE SET ...`（§5.1）
  2. `UPDATE articles SET tsv=to_tsvector('simple', jieba_join(...)) WHERE id=$1`（关键词通道补全，上一段）
  3. 入队 `embed_summary` job（幂等 `ON CONFLICT DO NOTHING`）
  4. 入队 `topics` job（**仅当关键词未命中**——`match_keywords()` 返回空集的文章走 LLM 慢路径，§6 主题分类规则；摘要后触发读 `summary_zh` 比外文全文 token 省、跨语言判定更稳，故双触发问题自然消解：ingest 时不再入队 topics，仅此一处）
  5. 入队 `wiki` job（**必须**——入队规则表 wiki 触发是「摘要落地后」，漏了 wiki 永远不入队，PRD §15 #5 验收挂；幂等 `ON CONFLICT DO NOTHING`）
  6. **检查文章是否可置 `done`**（§6 状态机）：`SELECT NOT EXISTS (SELECT 1 FROM processing_jobs WHERE article_id=$1 AND status IN ('queued','running'))` → true 时 `UPDATE articles SET status='done' WHERE id=$1 AND status='processing' AND dedupe_of IS NULL`——**每次 job 终态后执行**（关键词命中的文章 topics job 不存在、剩余 job 全部终态 → 自动 done；任务失败路径也覆盖，不再依赖"最后一个 task"概念）
- **调用方**：
  - **自动**：worker 处理 `summarize` 任务成功后调用
  - **手动**：`tc retry <article_id> summarize` 走同一条钩子（不能用 LLM 重新跑完后只 UPDATE summaries，否则 `embed_summary` 不会补入队 → summary 向量停在旧版本；F2 P0 必踩的坑）
- **抽象边界**：钩子只关心「summary 落库之后该发生什么」，不感知调用方是自动还是手动

**`complete_embed(article_id, kind, result)` 公共钩子（与 `complete_summarize` 对称，自动 + 手动重试都走）**：
- **职责**（同事务，全部或全部不发生）：
  1. `INSERT INTO article_embeddings ... ON CONFLICT (article_id, kind, model) DO UPDATE SET vector=..., content_hash=..., dim=... WHERE EXCLUDED.content_hash = (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id)`——同 summaries 的 content_hash 版本守卫，防旧 job 覆盖新向量（§6 状态机原子性，方向修对：仅当本 job 对应内容仍是当前版本才落库）
  2. **`embed_core` 完成后做近似去重判定**（仅 `kind='body'` 钩子里触发；`kind='title'` 与 `embed_summary` 跳过）：`SELECT id, vector FROM article_embeddings WHERE kind='body' AND model=<active> AND article_id IN (SELECT id FROM articles WHERE lang=(SELECT lang FROM articles WHERE id=$1) AND id!=$1 AND dedupe_of IS NULL AND fetched_at>now()-INTERVAL '<window>') ORDER BY vector <=> $body_vec LIMIT <k>` → 若 `distance <= 1 - threshold` 命中 → 进入 dedup 命中事务（§6 dedup 命中段：loser status='done' + dedupe_of + 删 article_topics + supersede summarize/topics/wiki + fetch_events），**不再入队 `summarize`**。**职责 ① ② 都在同一事务**——要么 embed 落库 + 去重判定走完、要么都不发生
  3. `UPDATE processing_jobs SET status='succeeded', lock_until=NULL WHERE id=$1 AND status='running'`（带 running 守卫，§6 状态机原子性）
  4. **检查文章是否可置 `done`**（§6 状态机）：`SELECT NOT EXISTS (SELECT 1 FROM processing_jobs WHERE article_id=$1 AND status IN ('queued','running'))` → true 时 `UPDATE articles SET status='done' WHERE id=$1 AND status='processing' AND dedupe_of IS NULL`
- **调用方**：worker 处理 `embed_core`/`embed_summary` 成功后调用；`tc retry <article_id> embed_core|embed_summary` 走同一钩子——否则手动重嵌只 upsert 向量而不推进 job 状态/不守卫 supersede、与 F2 P0 同类坑。**手动 retry embed_core 会重跑去重判定**：新向量可能命中不同的 winner，旧 job 的 dedupe_of 应当被新 winner 覆盖（手动 retry 通常因人工修了 content）
- **kind 映射**：`embed_core` 写 `title`+`body` 两行、`embed_summary` 写 `summary` 一行（§5.2）；钩子按 job payload 的 kind 集合循环 upsert
- **维度校验**：result 向量维度 ≠ `db.vector_dim`(1536) → 阻断写入并告警（§4.2/§5.2，防 HNSW 失配）

**worker 领取（单条原子 pick-and-claim，FOR UPDATE SKIP LOCKED + UPDATE 同事务）**：
```sql
UPDATE processing_jobs
SET status='running', lock_until=now() + INTERVAL '5 minutes'
WHERE id = (
  SELECT id FROM processing_jobs
  WHERE status='queued' AND (lock_until IS NULL OR lock_until < now())
  ORDER BY priority, created_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
RETURNING *;
```
- **`SELECT ... FOR UPDATE SKIP LOCKED` + `UPDATE status='running'` 必须在同一事务**（`async with conn.transaction():`）。拆成两条会让行锁在 `SELECT` 提交时立刻释放，第二个 worker 同时抢到同一行——单 worker 下窗口窄、bug 难复现，多 worker 直接翻车
- 单条原子 `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING *` 是更稳的写法：行锁持有到 UPDATE 提交，没有「先选后改」的窗口
- 领到 `RETURNING *` 即是入参；空结果 = 队列空，sleep 退避（§4.4 自探测）
- **领取 SQL 不自增 `attempt`**：旧版 `attempt=attempt+1` 是 v0.5 瞬时不分路径的残留——attempt 现在由永久失败路径独占（§6 失败处理 SQL），瞬时永不消耗 attempt 预算。若领取时 +1，job 经过 N 次瞬时退避后再遇一次永久错误，一次就耗光 `max_attempts` 死信——直接违反「永久 3 次死信」与验收 #7。领取 SQL 只推进 `status='running'` 与续 lease 即可

**worker 运行模型（常驻自驱，非心跳驱动）**：
- lifespan 启动**单个 asyncio worker task**：`循环 { 周期 recover → 领取(SKIP LOCKED) → 无任务 sleep ~1s → 处理（含后台 lease 续租）→ 继续 }`；领取与处理都在 await 点让出事件循环，不阻塞 fetch / HTTP
- 入队到开始 ≤ 当前在飞任务时长 + ~1s（并发=1 下在飞任务即 LLM 调用时长）
- **scheduler 的 drain_queue 不参与领取**（避免双领取者歧义），只做维护，见 §10
- LLM 掉线期间所有 queued 带未来 lock_until → 领取空手返回后 worker **自探测 oMLX**（`GET /v1/models` 一次，§4.4）决定 sleep 退避时长，不空转打 oMLX；Phase 1 单进程下 `LLMClient.healthy` 与 scheduler 共享，但 worker 仍以自探测为准（不盲信 scheduler 5m 快照）
- **领取门控（可选，推荐 Phase 1 开）**：领取前先自探测，不 healthy 则直接 sleep 退避、**不领新 job**——避免掉线期间新 job（`lock_until NULL`，本会被立刻领取）被领走后立刻失败回滚（瞬时虽不自增 attempt、不进死信，但每个被领走又失败的 job 都会带上未来 `lock_until` 退避，等于把一堆本可立即排队的新 job 提前推到退避队列、拉长恢复后的消费时延）。配合下一节「瞬时错误不自增 attempt、不进死信」双保险，保 PRD §15 #7「恢复后自动续跑」
- **启动期强制 recover**：`worker_loop` 进入主循环前先 `recover_interrupted(force_all_running=True)`——抢所有 `status='running'` 的 lease（无论 lock_until 是否过期）。仅在 Phase 1 单 worker 假设下成立，处理「前 worker 进程被 Ctrl-C/SIGKILL 强杀、当前 worker 接力、但旧 lease 还没过期」的场景。多 worker 启动会撞锁、误抢，**这个 flag 在 §6 已有注释**
- **运行期周期 recover**：`worker_loop` 主循环每 `WORKER_RECOVER_PERIOD_S=60s` 跑一次 `recover_interrupted(force_all_running=False)`——仅回收 lock_until 已过期的 running job（多 worker 安全）。覆盖「worker 在线但 LLM 调用 hang 在 httpx 不返」的孤儿 lease

**状态机原子性（lock_until 租约模型 + 事务合并）**：
- **领取 → 持租约**：见上一段 SQL，pick-and-claim 单条原子同事务；`lock_until` 既是 queued 退避门控、也是 running 存活凭证，**语义统一**
- **续租（随处理协程，不另起 watchdog）**：实现为独立后台 asyncio task `_lease_renewer(session_factory, job_id, stop_event, interval_s=60)`——`process_job_with_lease_renewal` 处理每个 job 时启动一份，后台每 60s `UPDATE lock_until=now()+INTERVAL '5 minutes' WHERE id=:jid AND status='running'`，handler 返回前 `stop_event.set()` 平滑停（2s 上限）。**续租 task 是后台、不与 handler 争 DB session**。其约束：本节原本的「续租与 LLM 调用同一个 asyncio task」设计即本节的实施；httpx 必须带 timeout（`GenerateRequest.timeout_s=180s`，§4.1）是第一道闸；lease_renewer 同 task 内的后台循环是第二道——hang 时 renewer 自动停、lease 自然到期、worker_loop 周期 recover 兜底回收。**`recover_interrupted(force_all_running=True)`** 是启动期第三道
- **handler 成功 → succeeded（统一转移）**：`process_job_with_lease_renewal` 在 task_handler 正常返回之后**统一**执行：
  ```sql
  UPDATE processing_jobs SET status='succeeded', lock_until=NULL, updated_at=now() WHERE id=:jid;
  ```
  之前只有 `complete_embed()` 自己写 succeeded（`run_summarize` / `run_classify_topics` / `run_generate_wiki` 不写），导致 summarize/topics/wiki job 永久卡 running、lock_until 在 renewer 续命下保持有效、`tc status` 持续显示 running。统一转移后 5 个 task type 一视同仁，renewer 2s 内停 + check_and_set_done 触发文章 processing→done。PermanentError/Exception 路径已在原本的 handle_*_failure 函数里处理 succeeded/failed 转移，process_job_with_lease_renewal 补 check_and_set_done 一并推动
- **完成（产物落库 + 状态推进同事务 + running 守卫）**：
  ```sql
  BEGIN;
  -- 1) 产物 upsert（summaries / entities / wiki_pages 等）—— 与状态推进原子
  --    summaries 带 content_hash 版本判定：仅当本 job 的 content_hash 等于 articles.content_hash
  --    （即本 job 对应的内容版本仍是文章的当前版本）时才允许覆盖。
  --    语义：「过期版本的结果不让落库」——旧 job 带着旧 content_hash 来直接被挡，
  --    同 content_hash 的幂等重跑放行，新版本 job 覆盖旧版本正常放行。
  --    旧版「WHERE summaries.content_hash IS DISTINCT FROM EXCLUDED.content_hash」是反的：
  --    该谓词只挡「同 hash 幂等重写」（本该放行），放行「不同 hash 」（即过期覆盖新），方向反。
  INSERT INTO summaries (article_id, lang, model, content_hash, summary_text, ...) VALUES (...)
  ON CONFLICT (article_id, lang, model) DO UPDATE
  SET content_hash=EXCLUDED.content_hash, summary_text=EXCLUDED.summary_text, ...
  WHERE EXCLUDED.content_hash = (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id);
  -- 2) tsv 刷新（§6 关键词通道补全）
  UPDATE articles SET tsv=to_tsvector('simple', ...) WHERE id=$1;
  -- 3) 状态推进（带 WHERE status='running' 守卫）
  UPDATE processing_jobs SET status='succeeded', lock_until=NULL
  WHERE id=$1 AND status='running';
  COMMIT;
  ```
  - **content_hash 版本判定的作用**：单 worker 下 supersede 竞态窗口本就窄，但「事务合并后窗口消失」的旧表述偏乐观——若 worker 已进入 complete 事务（产物 upsert 已执行）时，content 变更路径的 supersede `UPDATE ... WHERE status IN ('queued','running')` 会被行锁阻塞至 worker 提交；worker 提交后 job 已 `succeeded`、supersede 落 0 行，**旧摘要已落库**，待新 content 的 job 重跑才覆盖。加 `WHERE EXCLUDED.content_hash = (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id)` 后：旧 job 提交的 content_hash ≠ 文章当前 content_hash，被 WHERE 挡掉、不覆盖；新 job 先落库则内容版本更新，旧 job 进来比对失败、被挡；同 content_hash 的幂等重跑（同一 job 重新提交结果）放行。**§13 测试 D5 必须断言**：mock supersede 竞态（H1 旧 job 带着过期 hash 提交、H2 新 job 已经先落）→ 旧结果**未**写入、H2 的结果保留——这种守卫不测等于没有
  守卫意义：job 被 supersede 后，旧 LLM 结果若还先落库一瞬，单 worker 下最终会被新结果覆盖，但**测试与排障都因此变得很难**；事务合并 + 上面的 `content_hash` 版本判定后，旧 job 的 upsert 被版本守卫挡掉、不再短暂覆盖新结果——窗口不仅收窄、且可在测试里断言（详见上一段 content_hash 版本判定说明）
- **失败（按 error_class 分路径，§5.1 新增列支撑）**：
  - **瞬时（5xx/超时/连接拒绝，含 oMLX 整体掉线）**：`error_class='transient'`，**`attempt` 不自增**（死信预算不消耗）、`consecutive_timeouts` 仅在「超时」子类 +1（连接拒绝/5xx 不增），`status` 保持 `queued`、`lock_until=now()+INTERVAL '<退避>'`（1m→5m→15m 封顶），到点自动被 SKIP LOCKED 领取续跑——**永不进死信**
  - **超时转永久（病态文章）**：当 `consecutive_timeouts >= llm.max_timeout_retries`（默认 3）**且** healthcheck 正常（证明不是基础设施掉线）→ **`status='failed'` 死信**、`error_class='permanent'`、记 error（**不再 `attempt+1` + `max_attempts` 循环**：180s × 3 = 9 分钟阻塞后还可能再花 9 分钟试 3 次完全相同的死文章，纯粹浪费；矩阵/§11 已统一为「直接 failed」，此处 SQL 与矩阵对齐）；healthcheck 不过则维持瞬时、`consecutive_timeouts` 不增（§6 矩阵）
  - **永久（401/403/400/JSON 解析失败/内容不可解析）**：`error_class='permanent'`、`attempt+1`、`error=$2`；`attempt >= max_attempts` → `status='failed'` 死信；未达阈值则 `status='queued'`、`lock_until=now()+INTERVAL '<短退避>'` 重试（永久类退避短、快速耗尽 attempt）
  - 通用 SQL（瞬时，attempt 不自增）：`UPDATE processing_jobs SET status='queued', lock_until=now()+INTERVAL '<退避>', error_class='transient', error=$2 WHERE id=$1 AND status='running'`
  - 通用 SQL（永久，attempt 自增 + 死信判定）：`UPDATE processing_jobs SET status=CASE WHEN attempt+1>=max_attempts THEN 'failed' ELSE 'queued' END, lock_until=CASE WHEN attempt+1>=max_attempts THEN NULL ELSE now()+INTERVAL '30s' END, attempt=attempt+1, error_class='permanent', error=$2, consecutive_timeouts=0 WHERE id=$1 AND status='running'`
  - **`status='failed'`（死信）后同事务执行 done 检查**（§6 状态机）：`SELECT NOT EXISTS (SELECT 1 FROM processing_jobs WHERE article_id=$1 AND status IN ('queued','running'))` → true 时 `UPDATE articles SET status='done' WHERE id=$1 AND status='processing' AND dedupe_of IS NULL`——与 complete_* 钩子里的 done 检查对称；死信是终态，做完不再入队后续 task，此时若无剩余 queued/running job，文章可直接 done（覆盖「最后一 job 永久失败」场景）
- **进程中断**：崩溃/杀进程时 `status='running'` 且 `lock_until` 留在未来 —— **租约过期才算真死**
- **recover（租约回收）**：`UPDATE processing_jobs SET status='queued', lock_until=NULL, recover_count=recover_count+1 WHERE status='running' AND lock_until < now()` —— **谁跑都安全**（多 worker / scheduler 启动时跑也只会回收已过期的行，不动活任务；Phase 1 单进程下 worker 是唯一常驻消费者，无跨进程误伤）。**不动 `error` 字段**——原始错误信息保留在 `error` 字段供排障，recover 次数由 `recover_count` 计数器追踪（§5.1 新增列；旧版 `error='[recovered x N]'` 整体覆盖 error 字段，原始错误信息丢失、多次 recover 后 error 字段反复覆写无诊断价值）
- **归属**：**仅 worker 启动时跑** `recover_interrupted()`（scheduler 不跑，避免双领取者歧义）；Phase 1 单进程下 worker 是唯一常驻消费者（§6 运维模式 / §10），recover 与 worker 同进程、无跨进程误伤
- 启动顺序：init_db → 探测 oMLX → `recover_interrupted()` → 启动 worker（同进程内 scheduler 也在此时拉起，见 §6 运维模式 / §10）；**`recover_interrupted()` 在 worker 启动时跑**（§6 recover 归属）

**运维模式（Phase 1 vs Phase 2）**：
- **Phase 1（无 WebUI，CLI 入口）**：**单进程**——`python -m app.worker`（或 `make worker`）在一个 asyncio 事件循环里同时常驻 **worker task + APScheduler（AsyncIOScheduler）**。没有 FastAPI lifespan 不等于要拆进程：APScheduler 的 `AsyncIOScheduler` 跑在同一 loop 上即可承担 fetch_all / drain_queue / cleanup_fetch_events / pg_backup 等定时任务，worker task 见下文「worker 运行模型」也是 loop 上的自驱协程，二者通过 await 点协作、互不阻塞。**drain_queue 随 scheduler 天然在场**（§10），高量 feed 被截断的 pending 文章自动被补入队——这是 Phase 1 选单进程而非拆进程的核心理由（否则 worker 单独常驻时 drain_queue 缺位、pending 文章永久滞留，§6 backpressure / §14 高量 feed 风险被打脸）。CLI 命令（`tc fetch` / `tc summarize` / `tc search` ...）走 services 层、**不启动 worker**——入队后靠常驻 `make worker` 进程消费（开发期「两个终端」：`make worker` + `tc ...`）。**不拆成 worker/scheduler/CLI 三进程**（PRD §3 Out of Scope「单应用进程」scope 的本意；多进程只会制造 `LLMClient.healthy` 不共享、drain_queue 缺位等自找的坑）
- **Phase 2（WebUI 上线后）**：FastAPI lifespan 在 `app/main.py:create_app()` 启动顺序 = init_db（校验 vector 扩展/维度）→ 探测 oMLX 三端点 → `recover_interrupted()` → 启动 scheduler + worker task（**同一进程**，与 Phase 1 一致；WebUI 只是再加一层 uvicorn 路由）

**去重（精确 + 近似两阶段，闭环设计）**：

- **精确去重（LLM 花钱前）**：
  - `url_hash` 相同 → 复用旧文章，`mention_count+1`，**不创建新行**
  - URL 不同但 `content_hash` 相同 → 记 `dedupe_of` 指向原文，**不创建新行**
  - 这一步先于 LLM 入队完成；URL/content_hash 不同的同事件转载/改写留给下一阶段

- **跨源近似去重（embed_core 落地后、summarize 入队前）**——同事件多源转载/改写 URL 与 content_hash 都不同但语义近似：
  - **触发位置**：`complete_embed(article_id, kind='body')` 钩子里，`embed_core` 的 title+body 两条向量全部 upsert 成功后**执行近似去重判定**（详见 §6 `complete_embed` 职责 ②）。机制上：summarize 在 ingest 阶段已入队（§6 入队规则表），去重命中时**在 summarize 被 worker 实际领取之前拦截**——同事务 supersede 该文章的 summarize/topics/wiki 三个 task + 设该文章 status='done'（loser），让已入队的 summarize job 永远不会被领走（活跃唯一索引的活跃集不含 superseded / failed，仍可能短暂轮到 superseded 的 prior 行被 SKIP LOCKED 跳过）。不必纠结"到底谁入队谁不入队"——入队规则表保持 ingest 入队 summarize 简洁，去重在 complete_embed 兜住
  - **查询向量 = 本文章的 `kind='body'` 行**（同粒度匹配，见下）；**注意：此时 summary 向量还不存在**（旧版说「title+summary」是错的）
  - **候选池过滤到 `kind='body'`**：检索 `article_embeddings` 时 **必须 `WHERE kind='body' AND model=<active embed model>`**——不能用 mean(title,body) 查询向量去对 mixed(title/body/summary) 候选池排序：mean 与候选的单 title / 单 summary 行不在同一语义点上，top-k 与 0.95 阈值判定基准飘忽、同一候选的 title 行和 body 行距离差大还会占两个 top-k 位。统一 body↔body 同粒度比较，语义自洽、阈值有意义
  - **检索窗口**：`ingestion.dedup.window_days`（默认 30 天）内的活跃文章，`WHERE kind='body' AND model=<active> AND article_id IN (SELECT id FROM articles WHERE lang = $1 AND id != $article_id AND dedupe_of IS NULL AND fetched_at > now()-INTERVAL '<window>')`，按 `vector <=> $1` 排序取 top `ingestion.dedup.k`（默认 10）
  - **判定阈值**：pgvector `<=>` 是**余弦距离**（= 1 - 余弦相似度），SQL 条件 `vector <=> $1 <= 1 - threshold`，**别写反**（写成 `>=` 相当于「最不像的也合并」，静默吞文章）。默认 `ingestion.dedup.threshold` = **0.95**（相似度），即距离 ≤ 0.05 命中
  - **body 截断的已知边界**：`embed_core` 的 body 向量按 §5.2 截断 8K，超长同事件文若前 8K 开头不同，body↔body 距离偏大、该命中也可能漏判——P1 接受（保守漏判优于误合并）；P2 正文分块 + 池化后可缓解
  - **命中 → 取消后续 LLM（同一事务，全部或全部不发生）**：
    1. `UPDATE articles SET status='done', dedupe_of=<ultimate_winner_id> WHERE id=$1 AND status='processing'`——**loser 直接 done**（不是 superseded+pending，是真 done），drain_queue 谓词 `status='pending'` 不再触发补队、loser 永不复活（详见 §6 文章状态机 / backpressure 段）
    1.5. `UPDATE articles SET mention_count = mention_count + (SELECT mention_count FROM articles WHERE id=$1) WHERE id=<winner>`——**mention_count 累计转移到 winner**（loser 的热度汇聚到原始文章；原 SQL 错写为 loser 自身翻倍，winner 一分没拿到）
    2. `UPDATE processing_jobs SET status='superseded' WHERE article_id=$1 AND task IN ('summarize','topics','wiki') AND status IN ('queued','running')`，消除 TOCTOU 窗口（§6 supersede 同事务原则）
    3. `DELETE FROM article_topics WHERE article_id=$1`——**双保险**：loser 在 match_keywords 判定写入 article_topics（keyword 行）后被 dedup 命中，dedupe_of 置位但 topic 行还在；删掉避免主题视图重复占位、验收 #16 挂（详见中等 5）
    4. 写 `fetch_events(event_type='dedup_merge', ok=true, ...)`，payload 含 winner/loser article_id、cosine 距离、lang，**所有合并事后可审计**
  - **多跳扁平化**：winner 自身可能也是别人的 loser（`dedupe_of` 非空）。命中时先**沿 `dedupe_of` 链回溯到终极 winner**（`dedupe_of IS NULL` 的根文章），把本文章直接指向终极 winner；同时把所有直接指向「中间 winner」的 loser 改指终极 winner、`mention_count` 累计转移到终极 winner——避免链式回溯查询、`WHERE dedupe_of IS NULL` 能一次取到所有独立文章
  - **P1 保守的误合并防护**：
    - **阈值 0.95 起步**：0.92 是经验下界，但「Weekly Digest #N」「Issue #N」类模板化标题易超阈值被静默合并——0.95 显著降低误合并概率；P1 跑一段真实数据后再考虑放松（body↔body 比纯 title 更不易被模板化标题误判，但正文模板化段落同样有此风险）
    - **限同语言**：仅当候选与本文章 `lang` 相同时合并。跨语言（同事件的英文原文 + 中文报道）只做候选标记入 `fetch_events`，不合并——避免读者关心的「同一事件的中英文版本各自保留」
    - **可逆**：`dedupe_of` 置 NULL 即恢复独立；CLI `tc article <id> undedupe`（P2 WebUI 按钮）
  - **配置**（§9）：
    - `ingestion.dedup.threshold`（默认 0.95）
    - `ingestion.dedup.window_days`（默认 30）
    - `ingestion.dedup.k`（默认 10）
  - **覆盖**：跨源同事件转载/改写 → 主题视图与日报不重复占位（PRD §15 #16）

**重试/降级矩阵**：
| 失败 | 处理 |
|---|---|
| 抓取网络错误 | 记录 fetch_events，下次周期再试；连续 `ingestion.feed_disable_after`（默认 5）次自动禁用 feed；**抓取成功时 `fetch_failures` 归零**（`UPDATE feeds SET fetch_failures=0, last_error=NULL WHERE id=$1`，§5.1 计数列需显式重置，否则一次失败永远卡在禁用边缘）；陈旧 `fetch_events` 按 `fetch_events_retention_days`（默认 90 天）定期清理 |
| 文章不可解析 | status=unparseable，保留原文，跳过 LLM |
| LLM 5xx/超时/连接拒绝（瞬时） | job 保持 `queued`，`lock_until` = 退避 1m→5m→**15m 封顶**，**无限重试不进死信**——掉线是基础设施问题不是内容问题，**attempt 不自增、预算不消耗**（§6 失败处理 SQL 分路径）；worker 领取门控（§6）避免掉线期间领新 job；到点自动被 SKIP LOCKED 领取续跑 |
| **LLM 401/403/400（永久/配置）** | 鉴权失败或请求格式错——**不走退避**，job `error_class='permanent'`、`attempt+1`，达 `max_attempts`（默认 3）后 `failed` 死信并记 error；本机默认不鉴权所以平时不触发，一旦开鉴权 token 错就快速失败、横幅明确报错而非伪装成「LLM 掉线无限续跑」（§4.4/§11） |
| **同 job 同 content_hash 连续 K 次（默认 3）超时**（病态文章）| healthcheck 正常但单篇持续 180s 超时（极长/恶意输入/编码炸弹），属**内容问题非基础设施问题**——`consecutive_timeouts+1`，达 `llm.max_timeout_retries` 且 healthcheck 正常 → **`status='failed'` 死信**（直接，不走 attempt+1 循环：3×180s = 9 分钟试完后还要再耗 9 分钟重试 3 次同样死的病态文章无意义；§6/§11）；掉线场景不受影响（healthcheck 不过仍维持瞬时，consecutive_timeouts 不增）|
| LLM JSON 解析失败 / 内容不可解析（永久） | `error_class='permanent'`、`attempt+1`，达 `max_attempts=3` 后 `failed` 死信，记 error；文章详情可手动 `tc retry`（走 `complete_*` 钩子） |
| 内容变更（活跃 job 期间） | 旧 job→`superseded`，入队新 job（幂等，见上） |
| LLM JSON 解析失败 | structured.parse_with_repair（去围栏→找平衡{}→带错重问一次）→ 仍失败 low_confidence |
| 进程中断 | 启动时 `recover_interrupted()` **按租约回收**：`status='running' AND lock_until<now()` → `queued`（过期的才算死，跨进程安全）；瞬时类 job 即便反复中断也不进死信 |

---

## 7. 检索设计（混合）

```
search(q):
  1) 语义: embed_query(q)（加 instruct 前缀，§4.2）→ article_embeddings
     WHERE model = <active embed model>（§5.2，防跨模型量纲不一）
     ORDER BY vector <=> $1 LIMIT k1
     （title/summary/body 三粒度都参与；k1 后按 article_id 去重取最高分，避免同文重复）
  2) 关键词: jieba(q) → articles.tsv @@ websearch_to_tsquery('simple', jieba_joined(q)) LIMIT k2
     （**不要**用裸 `to_tsquery`：jieba tokens 含 `&` `:` 等会炸语法，详见 §5.3）
  3) 融合: P1 = RRF（score = Σ 1/(k+rank)，k≈60；量纲无关，~10 行，比「原始加权求和」更简单且正确——cosine 与 ts_rank 量纲不可比直接相加无意义）；P2 = RRF + oMLX rerank(top-k 候选)
  4) 同时检索 wiki_pages 词条，合并分页；articles ∪ wiki_pages 按 ref_id/article_id 去重，一篇文章只返回一条（词条页作为文章详情的展开，不独立计入结果）
```
- P1：RRF 即可满足「中文关键词 + 语义近似」双场景（rerank 才是真 P2）
- **中文搜英文文章也能命中**：`articles.tsv` 在 `summarize` 成功后被刷新为 `title + content_text + summary_zh + key_points` 的 jieba tokens（§6），中文关键词通过 `summary_zh` 命中英文文章——关键词通道不再单腿
- **多粒度向量**：同一文章最多 3 行（title/summary/body）；`search(q)` 三粒度都参与，top-k 后按 `article_id` 去重保留最高分；`/articles/{id}/similar` 用该文章 `summary` 向量做查询，即「相关文章」
- oMLX `/v1/embeddings` 不可用时：语义通道关闭，仅关键词（Dashboard 提示）

### 7.1 Phase 2 检索增强（Rerank + 相似文章 + Wiki 跨表检索，§14 切片 2.6）

#### Rerank 路径（Cohere 风格入参，§15 #9）

```python
async def search(
    q: str,
    *,
    use_rerank: bool = False,
    mode: Literal["hybrid", "semantic", "keyword"] = "hybrid",
    page: int = 1,
    page_size: int = 20,
    filters: SearchFilters | None = None,
) -> SearchResult:
    ...
    # 1) 走 §7 Phase 1 算法拿混合 top-N（N=60）
    candidates: list[Candidate] = await _rrf_fuse(q, top_n=60, filters=filters)
    # candidates 含 {article_id, title, snippet, rrf_score, source: article|wiki}
    
    # 2) 可选 rerank
    if use_rerank:
        try:
            docs = [c.title + "\n" + c.snippet for c in candidates]
            reranked = await llm.rerank(q, docs, top_n=len(docs))
            candidates = [candidates[i] for i in reranked]
        except (NotImplementedError, httpx.HTTPStatusError, asyncio.TimeoutError):
            # 降级链：oMLX /v1/rerank 不可用 → 进程内 bge-reranker-v2-m3
            try:
                candidates = await _rerank_local_bge(q, candidates)
            except Exception:
                # 双层降级都失败 → 不重排（保持 RRF 顺序）
                logger.warning("rerank 不可用，使用 RRF 顺序")
    
    # 3) 分页
    return SearchResult(items=candidates[(page-1)*page_size : page*page_size], total=N)
```

#### Rerank 入参（PRD §8 已实测 oMLX 支持）

```http
POST {omlx_base}/v1/rerank
Authorization: (本机不鉴权)
Content-Type: application/json

{
  "model": "Qwen3-Reranker-4B-mxfp8",
  "query": "RAG 系统性能优化",
  "documents": ["<doc1>", "<doc2>", ...],
  "top_n": 20
}
```

**出参**：

```json
{
  "results": [
    {"index": 4, "relevance_score": 0.92},
    {"index": 1, "relevance_score": 0.81},
    ...
  ]
}
```

**降级**：oMLX `/v1/rerank` → 进程内 `bge-reranker-v2-m3`（用 transformers 库，懒加载，~3GB 内存驻留）→ 不重排（仅 RRF）。降级链由 `LLMClient.rerank()` 内置 try/except 透明处理。

#### 进程内 bge-reranker-v2-m3 集成（降级层）

```python
# app/llm/local_reranker.py（仅在 oMLX /v1/rerank 不可用时实例化）
from sentence_transformers import CrossEncoder
import threading

_local_reranker: CrossEncoder | None = None
_local_reranker_lock = threading.Lock()

def get_local_reranker() -> CrossEncoder:
    global _local_reranker
    if _local_reranker is None:
        with _local_reranker_lock:
            if _local_reranker is None:
                _local_reranker = CrossEncoder("BAAI/bge-reranker-v2-m3")
    return _local_reranker
```

仅懒加载第一次 `LLMClient.rerank()` 失败后实例化（避免默认占用 3GB 内存）。

#### Wiki 跨表检索（search() 一并支持）

```sql
-- articles UNION wiki_pages 后 RRF 融合
WITH semantic_articles AS (
  SELECT article_id AS ref_id, 'article' AS kind, vector <=> :q_vec AS distance
  FROM article_embeddings WHERE model = :active_model
  ORDER BY distance LIMIT 60
),
semantic_wikis AS (
  -- Wiki 无独立向量，靠相关 article 的相关性传递（rank boost）
  SELECT wp.id AS ref_id, 'wiki' AS kind, sa.distance * 1.05 AS distance
  FROM semantic_articles sa
  JOIN wiki_pages wp ON wp.kind IN ('article','topic','entity')
                     AND (wp.kind='article' AND wp.ref_id=sa.ref_id
                          OR wp.kind='topic' AND wp.ref_id IN (...))  -- 该文章所属 topic 的 wiki
  ...
  ORDER BY distance LIMIT 60
),
keyword_articles AS (
  SELECT id AS ref_id, 'article' AS kind, ts_rank(tsv, websearch_to_tsquery('simple', :q)) AS rank
  FROM articles WHERE tsv @@ websearch_to_tsquery('simple', :q) LIMIT 60
),
keyword_wikis AS (
  SELECT id AS ref_id, 'wiki' AS kind, ts_rank(tsv, websearch_to_tsquery('simple', :q)) AS rank
  FROM wiki_pages WHERE tsv @@ websearch_to_tsquery('simple', :q) LIMIT 60
)
-- RRF 融合四个集合，按 ref_id + kind 去重（一篇 wiki + 一篇 article 不同 kind 都计入）
```

**Wiki 全文索引列**（`wiki_pages.tsv`）在 §5.1.5 已声明；触发器维护：`BEFORE INSERT OR UPDATE ON wiki_pages` 执行 `NEW.tsv = to_tsvector('simple', jieba_join(NEW.title || ' ' || NEW.content_md))`。

#### 相似文章推荐（Phase 2 切片 2.6）

`GET /api/articles/{id}/similar?top_k=10`

```sql
SELECT a.id, a.title, ae.vector <=> target.vector AS distance, at.score AS topic_score
FROM article_embeddings target
JOIN article_embeddings ae ON ae.model = target.model
                           AND ae.kind = 'summary'
                           AND ae.article_id != target.article_id
JOIN articles a ON a.id = ae.article_id
WHERE target.article_id = :aid AND target.kind = 'summary'
  AND a.dedupe_of IS NULL
  AND a.lang = (SELECT lang FROM articles WHERE id = :aid)
ORDER BY distance
LIMIT :top_k * 3;  -- 取 3 倍候选再做主题加权

-- 同主题文章额外加分（已在 article_topics 里的）
SELECT art.*, 1.0 / (1 + distance) + COALESCE(topic_score, 0) * 0.3 AS combined_score
FROM ... ORDER BY combined_score LIMIT :top_k;
```

Phase 1 仅做纯向量 top-k，Phase 2 加入"同主题加权"提升相关性。

#### 检索结果展示（Phase 2）

WebUI `/search` 页：
- 顶部：高级筛选（feed/topic/lang/status/date range）
- 中部：混合结果 list，每条含 kind 标识（article/wiki）、title、snippet 高亮、score 列
- Wiki 结果与 article 结果视觉同等权重，UI 上方有 toggle "全部 / 仅文章 / 仅 Wiki"
- 右侧栏：相关实体卡片（聚合本批结果的 entities top 5）

#### 性能约束（P95 < 100ms）

- 语义通道：`ef_search=64`（§5.2 已配置），N=60 召回 + rerank < 50ms
- 关键词通道：tsvector 命中几十条，rank + 高亮 < 20ms
- Wiki 跨表 UNION：与文章同量级
- LLM rerank（oMLX）：本地 27B 60 docs ~1-3s（不阻塞首屏，可异步二次渲染）

---

## 8. RESTful API / Web 路由（Phase 2：WebUI）

Web 页面（Jinja2 服务端渲染 + HTMX 片段）+ 少量 JSON 端点。**Phase 1 无 WebUI，仅经 CLI 访问 Services 层；下表为 Phase 2 目标。**

| 方法+路径 | 功能 |
|---|---|
| `GET /` | 概览：统计/队列/LLM 健康/最近文章 |
| `GET|POST /feeds`，`POST /feeds/{id}/fetch\|enable` | Feed 管理 + 立即抓取 |
| `GET /articles?feed=&topic=&status=&q=&page=` | 文章列表（筛选+FTS） |
| `GET /articles/{id}` | 详情：原文/摘要/翻译/实体/话题/Wiki + 任务重试按钮 |
| `GET /articles/{id}/similar` | 相似文章（向量 top-k） |
| `GET /wiki`，`GET /wiki/{slug}` | 词条索引 + 渲染 |
| `GET /search?q=&mode=hybrid\|semantic` | 混合检索结果页 |
| `GET|POST /settings` | LLM 后端/模型/并发/调度时间 + 健康测试（流式） |
| `GET /graph`，`GET /api/graph.json` | 图谱页 + 数据（P2） |
| `GET|POST /topics` | 主题管理（P2） |
| `GET /reports`，`GET /reports/{id}` | 报告列表/渲染（P2） |

**内部约定**：FastAPI lifespan 启动顺序 = init_db（校验 vector 扩展/维度=1536）→ 探测 oMLX 三端点 → `recover_interrupted()`（**租约回收**：仅回收 `status='running' AND lock_until<now()` 的过期行，**先于 worker 启动**，避免与新领取竞争）→ 启动 scheduler + worker task。

### 8.1 Phase 2 路由详细规格（实施蓝图）

Phase 1 仅 CLI 入口，Phase 2 起 WebUI。**核心约定**：API 路由**只做**路由 + 表单校验 + 调 service（薄壳），业务逻辑全在 `app/services/`；HTMX partial swap 走 `_partial.html` 子模板（避免每次都套 base layout）。

| Method+Path | 模板 | 关键参数 | 错误码 | 备注 |
|---|---|---|---|---|
| `GET /` | `overview.html` | — | 500 | 渲染统计卡片、queue 表、最近 20 articles、LLM 健康横幅 |
| `GET /api/health` | JSON | — | 200 / 503 | `{llm_healthy, queue_depth, last_healthcheck_at}`，HTMX `hx-get` 每 30s |
| `GET /feeds` | `feeds/list.html` | `?type=rss\|api\|scrape&enabled=` | 200 | 表格 + 状态徽标（healthy/degraded/disabled） |
| `GET /feeds/new` | `feeds/edit.html` | — | 200 | 新增表单 |
| `POST /feeds` | redirect → `/feeds/{id}` | form: `name,url,type,enabled,config_json` | 422 | type=rss/api/scrape；config_json 按 type schema 校验 |
| `GET /feeds/{id}/edit` | `feeds/edit.html` | path: feed_id | 404 | 编辑表单 |
| `POST /feeds/{id}` | redirect → `/feeds/{id}` | form | 422 / 404 | 更新 |
| `POST /feeds/{id}/fetch` | partial swap | — | 404 / 503 | 立即抓取一次（HTMX 显示 toast） |
| `POST /feeds/{id}/disable` | partial swap | — | 404 | 禁用；不删记录 |
| `GET /articles` | `articles/list.html` | `?feed=&topic=&status=&q=&page=` (size=20) | 200 | 筛选表格 + FTS 搜索框 + 分页 + 多选批量重试 |
| `GET /articles/{id}` | `articles/detail.html` | path | 404 | 7 个 Tab：原文/摘要/翻译/实体/相关话题/Wiki；状态栏 |
| `POST /articles/{id}/retry/{task}` | partial swap | path: id, task | 404 / 422 | task ∈ summarize\|extract_entities\|topics\|wiki\|translate\|embed_core\|embed_summary |
| `POST /articles/{id}/undedupe` | redirect → `/articles/{id}` | path | 404 | 清 dedupe_of + status='processing'（让重跑全任务） |
| `GET /api/articles/{id}/similar` | JSON | `?top_k=10` | 404 | 相似文章（§7.1） |
| `GET /wiki` | `wiki/index.html` | `?kind=article\|topic\|entity\|manual&q=` | 200 | 按 kind 分组的卡片索引 |
| `GET /wiki/{slug}` | `wiki/page.html` | path | 404 | 渲染 Markdown + 元数据 + related_json 三栏 |
| `GET /wiki/{slug}/raw` | markdown 文本 | path | 404 | 导出用，HTML 自动渲染关闭 |
| `GET /search` | `search/results.html` | `?q=&mode=hybrid\|semantic\|keyword&use_rerank=true&page=` | 200 | 混合结果 + 高亮 + facets |
| `GET /graph` | `graph/page.html` | — | 200 | ECharts 力导向图 + 筛选面板 |
| `GET /api/graph.json` | JSON | `?topic_id=&entity_type=&since_days=&max_nodes=300` | 422 | 图谱数据（§6.4 切片） |
| `GET /topics` | `topics/list.html` | `?enabled=` | 200 | 跨源聚合表：主题×源×文章数 |
| `GET /topics/new` | `topics/edit.html` | — | 200 | 新增表单 |
| `POST /topics` | redirect → `/topics/{id}` | form: `name,keywords_csv,description` | 422 | 同步触发近 30 天 reclassify（§6 主题分类规则） |
| `GET /topics/{id}/edit` | `topics/edit.html` | path | 404 | |
| `POST /topics/{id}` | redirect → `/topics/{id}` | form | 422 / 404 | 改关键词同步触发 |
| `POST /topics/{id}/reclassify` | partial swap | path | 404 | 手动触发近窗重算（§6 reclassify_recent_days） |
| `GET /reports` | `reports/list.html` | `?report_type=daily\|weekly&limit=` | 200 | 报告卡片列表 |
| `GET /reports/{id}` | `reports/view.html` | path | 404 | Markdown 渲染 + TOC + 元数据 + 重试按钮 |
| `GET /reports/{id}/export.md` | markdown 文本 | path | 404 | Markdown 下载 |
| `POST /reports/{id}/retry` | partial swap | path | 404 | 重新生成（覆盖原 content） |
| `GET /settings` | `settings/page.html` | — | 200 | LLM 模型配置、并发、调度时间 |
| `POST /settings` | partial swap → reload | form | 422 | 部分字段需重启 worker 才生效，UI 提示 |

#### HTMX 策略

| 场景 | 模式 |
|---|---|
| **列表筛选/翻页/排序** | `hx-get="..." hx-target="#list-region" hx-swap="innerHTML" hx-push-url="true"` |
| **表单 POST** | `hx-post="..." hx-target="#result-region" hx-swap="innerHTML"` → 服务端返回 toast partial |
| **长操作**（retry/fetch-now） | `hx-post hx-trigger="click"` + `hx-indicator="#spinner-{id}"` + spinners |
| **健康横幅轮询** | `<div hx-get="/api/health" hx-trigger="every 30s" hx-swap="outerHTML">` |
| **图表 lazy mount** | ECharts 客户端 init，server 端仅返回 option JSON |
| **无限滚动**（articles list） | `IntersectionObserver` + `hx-get` `hx-trigger="revealed"` |

#### 健康横幅与 LLM 状态显示

- 顶部 `<div id="llm-status-bar" hx-get="/api/health" hx-trigger="every 30s">`
- 内容：绿色 `✅ LLM healthy (omlx, 180ms)` / 黄色 `⚠ LLM degraded (5xx 3/10 in last min)` / 红色 `❌ LLM down (last healthcheck 4m ago)` / 灰色 `unknown`
- HTMX swap outerHTML 自然更新 banner

#### 错误形态（HTTP 状态码）

| 状态 | 触发场景 | 渲染 |
|---|---|---|
| 200 / 302 | 正常 | base.html 渲染 |
| 400 | JSON 解析失败（worker 日志） | 不进 UI：CLI `tc status` 看 |
| 404 | 路由 / ref_id / slug 找不到 | `errors/404.html` |
| 422 | 表单字段校验失败（pydantic ValidationError） | `errors/422.html` + 字段错误高亮 |
| 500 | DB down / templates 渲染失败 | `errors/500.html` + 错误栈（dev）/ 友好页（prod，DEV mode flag） |
| 503 | LLM down + 用户操作需要 LLM | toast："LLM 不可用，操作暂存队列等恢复" |

#### Vendored 资源（无 CDN，离线可用）

```
app/static/
├── htmx.min.js          # 1.x latest
├── echarts.min.js       # 5.x latest（force-graph / force-layout）
├── sortable.min.js      # 列表排序（如 dashboard 拖卡片）
├── pico.min.css         # classless CSS，10KB
└── app.js               # 自写：sidebar 折叠、图表初始化、htmx 配置
```

注：**不使用 build pipeline**（无 webpack/vite），所有 JS 通过 `<script src="/static/...js">` 直接引入；CSS 走 `<link rel="stylesheet">`。`base.html` 头部聚合，模板继承避免重复加载。

#### WebUI 单页预算

- 静态资源总预算 ≤ 200KB gzip（htmx 14KB + echarts 150KB + sortable 11KB + pico 10KB + app.js 5KB）
- 首屏 SSR：Jinja2 渲染（不进客户端 bundle，原始 HTML）
- 后端耗时目标：列表页 < 200ms / 详情页 < 300ms / 图谱页 JSON < 500ms（含 PG 查询）

#### API 路由命名约定（与 `app/api/*.py` 文件对应）

| 路由文件 | 含 routes |
|---|---|
| `dashboard.py` | `/`, `/api/health`, `/api/llm-status` |
| `settings.py` | `/settings` |
| `feeds.py` | `/feeds*` |
| `articles.py` | `/articles*`, `/api/articles/{id}/similar` |
| `wiki.py` | `/wiki*` |
| `search.py` | `/search` |
| `graph.py` | `/graph`, `/api/graph.json` |
| `topics.py` | `/topics*` |
| `reports.py` | `/reports*` |
| `health.py` | `/api/health`（或并入 dashboard.py） |

---

## 9. 配置 Schema（config.yaml + feeds.yaml）

**`config.yaml`**（系统配置，**不含订阅源**）：

```yaml
data_dir: ./data
db:
  dsn: postgresql+asyncpg://tc:tc@localhost:5433/topic_collection   # 宿主机 5433 映射容器 5432；见 §5.4 docker-compose
  pool_size: 5
  vector_dim: 1536            # 模型原生 4096 维，经 oMLX /v1/embeddings 的 dimensions=1536 服务端截断；DDL 与本键必须一致（§5.2，启动期校验）
web: { host: 127.0.0.1, port: 7111 }   # 必须 ≠ oMLX 端口 (8000)，避免与本地 LLM 端口冲突
llm:
  backend: omlx               # omlx | ollama
  endpoint: http://localhost:8000   # oMLX OpenAI 兼容端点（实测）
  # 鉴权：本机不鉴权（已确认），不发 Authorization 头；如需鉴权再设 api_key_env
  model: Qwen3.8-27B-MLX-4bit   # 实测可用（质量更佳）
  # 备选：Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED-MLX-mxfp8（更轻量）
  # THINKING 变体（Qwen3.5-9B-…-THINKING-HERETIC-UNCENSORED）oMLX 加载失败，修复后可用
  max_concurrency: 1            # 默认 1；待实测 oMLX 同时常驻 27B+8B 嵌入可行则升 2（gen/embed 分槽，§4.4/§16）
  max_timeout_retries: 3        # 同 job 同 content_hash 连续超时 N 次转永久类死信（防病态文章无限续跑；§6 重试矩阵/§11）
  models: { summarize: <model>, translate: <model>, entities: <model>,
            topics: <model>, wiki: <model>, report: <model> }   # 默认=generation model；--check-llm 启动时全量校验（§4.4）
  embed:
    backend: omlx             # 嵌入无降级（见 §4.3）
    model: Qwen3-Embedding-8B-4bit-DWQ   # = active embed model，search 固定查此 model（§7）；切模型走 scripts/backfill（§5.2）
    max_tokens: 8192          # 正文 embed 截断上限（title/summary 天然短不截断，见 §5.2）
  rerank:                     # P2 启用
    model: Qwen3-Reranker-4B-mxfp8
ingestion:
  fetch_interval_hours: 6
  user_agent: "TopicCollection/0.1 (+local personal KB)"
  max_scrape_bytes: 5242880
  feed_disable_after: 5          # feed 连续失败 N 次自动禁用（§6）
  max_items_per_fetch: 50         # 单次 fetch per-feed 入队上限（backpressure，文章全量入库、仅限 LLM job 入队；§6）
  global_concurrency: 8           # 抓取全局 asyncio.Semaphore 上限（§6）
  per_host_interval_ms: 500       # 同一 host 上一次抓取结束到下一次起手的最小间隔（§6，避免被 RSS 服务端识别为机器人/触发 429）
  fetch_events_retention_days: 90 # fetch_events 审计表保留天数（cleanup_fetch_events 日任务清理，§10）
  dedup:                          # 跨源向量近似去重（§6，embed_core 后 summarize 前）
    threshold: 0.95               # 余弦相似度阈值（pgvector <=> 是距离，条件 <= 1-threshold，别写反）
    window_days: 30               # 候选检索窗口（仅合并近 N 天内的文章）
    k: 10                         # 候选 top-k
    same_lang_only: true          # 限同语言合并（英文原文不会被中文报道吞掉）
topics:
  llm_threshold: 0.6              # topics LLM 打分阈值（关键词快路径不经过此值）
  reclassify_recent_days: 30      # 主题变更重算窗口——超过此天数的历史文章不重跑（避免隐性全量回填，全量交给 P3 tc reclassify --all；§6/PRD §15 #3）
schedule: { daily_report: "08:00", weekly_report: "Mon 08:00" }
```

环境变量覆盖：`TC_LLM_BACKEND` / `TC_DB_DSN` / `TC_WEB_PORT` / **`TC_CONFIG`**（config.yaml 路径，默认 `./config/config.yaml`，解决 CWD 敏感问题——从任意目录跑 `tc ...` 都能定位配置；同理 `TC_FEEDS` 覆盖 feeds.yaml 路径）（pydantic-settings）；`TC_LLM_API_KEY` 仅当开启鉴权时使用。

**`config/feeds.yaml`**（订阅源清单，**独立文件，今后加源只改这一个文件**）：

```yaml
# Topic Collection 订阅源清单
# 新增订阅：在此加一项 → `tc feeds import`（或重启自动同步）→ `tc fetch`
feeds:
  - name: "示例 RSS"
    type: rss                       # rss | api | scrape
    url: "https://example.com/feed.xml"
    enabled: true
    # category: ai                  # 可选：主题分类标签
    # fetch_interval_hours: 6       # 可选：覆盖全局抓取间隔

  - name: "Hacker News API"         # P2 API 连接器示例
    type: api
    url: "https://hacker-news.firebaseio.com/v0/"
    enabled: false
    config:                         # 写入 DB feeds.config_json
      path: topstories
      mapper: { title: .title, url: .url, published: .time }
```



**同步机制**（feeds.yaml ↔ DB `feeds` 表）：
- **DB 为运行时真源**（fetch/worker 从 DB 读 enabled feeds）；**feeds.yaml 为维护清单**
- `tc feeds import` 命令（或启动时自动同步，幂等）：按 `url` upsert 进 DB——新增插入、变更更新、`enabled=false` 仅停用不删记录
- 目的：加一个订阅 = 编辑 YAML 一行，无需碰 SQL / WebUI（Phase 2 WebUI 再做图形化管理）

---

## 10. 定时任务

| 任务 | 触发 | 内容 |
|---|---|---|
| fetch_all | 每 `fetch_interval_hours` | 遍历 enabled feeds 抓取→去重→入队 |
| drain_queue | 每 30s | 维护：清理 superseded / 死信；**不参与领取**（worker 常驻自驱，见 §6） |
| **pg_backup** | **每日 03:00** | **`docker compose exec postgres pg_dump -U tc -d topic_collection` \| gzip → `data/backups/tc-YYYYMMDD.sql.gz`，保留 14 天**——个人知识沉淀库数据比代码值钱，pgdata 卷不是备份（§14 Day 1）。**走 `docker compose exec`**：宿主机不一定装了 PG 客户端，pgvector 镜像内自带 `pg_dump`，exec 直接用最稳。**主触发 = `tc backup` CLI**（PRD §4 F11），scheduler 此项为自动化补充；Phase 1 单进程下 scheduler 随 worker 常驻（§6 运维模式），pg_backup 自动化默认在岗，但用户仍须定期手动 `tc backup` 确认备份产出 |
| **cleanup_fetch_events** | **每日 04:00** | 清理 `fetch_events` 中超过 `fetch_events_retention_days`（默认 90 天）的行（drain_queue 30s 太频不适合做清理，独立日任务） |
| daily_report | 每日 08:00 | 日报（P2） |
| weekly_report | 周一 08:00 | 周报（P2） |
| healthcheck | 每 5m | LLM 健康探测，更新 `LLMClient.healthy` 与 Dashboard 横幅；Phase 1 单进程下与 worker 共享 `healthy`，worker 仍保留自探测兜底（§4.4 / §6，掉线可能发生在两次探测之间） |

### 10.1 Phase 2 报告（切片 2.5 完整设计）

#### `reports.stats_json` schema（每日）

```json
{
  "period": {
    "start": "2026-08-19T00:00:00+08:00",
    "end":   "2026-08-19T23:59:59+08:00"
  },
  "articles_total": 23,
  "articles_by_source": [
    {"feed_id": 1, "name": "Hacker News", "count": 12},
    {"feed_id": 4, "name": "TechCrunch", "count": 8},
    {"feed_id": 2, "name": "arXiv cs.CL", "count": 3}
  ],
  "summaries_generated": 23,
  "embeddings_generated": 92,
  "topics_top": [
    {"topic_id": 3, "name": "RAG", "delta_articles": 5, "delta_articles_prev_week": 2},
    {"topic_id": 1, "name": "LLM Agent", "delta_articles": 3}
  ],
  "entities_new": 47,
  "relations_new": 112,
  "graph_delta": {
    "nodes_added": 47,
    "edges_added": 112,
    "top_new_entities": [
      {"id": 12, "canonical_name_zh": "通义千问 3", "entity_type": "model", "mention_count": 7}
    ]
  },
  "queue_stats": {
    "queued": 0,
    "running": 2,
    "failed": 1,
    "succeeded_today": 19,
    "consecutive_failures": 0
  },
  "feed_health": {
    "healthy": 8,
    "degraded": 1,
    "disabled": 0,
    "degraded_list": [{"feed_id": 2, "failures": 3}]
  },
  "llm": {
    "latency_p95_ms": 28400,
    "requests_count": 86,
    "errors_count": 2,
    "token_est": {"prompt": 312000, "completion": 87000}
  }
}
```

#### 周报 `stats_json` 增量字段

```json
{
  "topics_top20": [...],                 // 替代 topics_top
  "topic_essays": {                       // 每个主题一段 LLM 综合（不写回 table，stats 内联）
    "3": "RAG 本周热度继续上升 ... 关键文章 5 篇集中在 ...",
    "1": "LLM Agent 在 X、Y、Z 文章中提到 ..."
  },
  "graph_growth": {
    "nodes_added_total": 312,
    "edges_added_total": 870,
    "top_new_entities": [...],
    "top_new_relations": [{"subject_id": 12, "predicate": "developed_by", "object_id": 8}]
  },
  "storage_advice": "本周新增 0.5GB，建议 P3 启动 article_versions 裁剪任务"  // (P2 月报)
}
```

#### 报告生成算法（`app/services/reports.py`）

```python
async def generate_daily_report(session, report_dt: datetime):
    report_id = await _create_pending_report(session, "daily", period_start, period_end)
    try:
        # 1. SQL 聚合 → stats dict
        stats = await _aggregate_stats(session, period_start, period_end)
        # 2. LLM 综合
        sys_p, user_p = get_prompt(
            "generate_report",
            report_type="daily", stats=json.dumps(stats, ensure_ascii=False),
            period_start=period_start.isoformat(), period_end=period_end.isoformat(),
        )
        resp = await llm_client.generate(GenerateRequest(
            model=settings.llm.generate.model,
            messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
            json_mode=False,
        ))
        content_md = resp.text.strip()
        # 3. Markdown → HTML 渲染
        content_html = markdown(content_md, extensions=["toc", "fenced_code", "tables"])
        # 4. 同事务写 content_md + content_html + stats_json + 状态 succeeded
        await session.execute(text("""
            UPDATE reports
            SET status='succeeded', content_md=:md, content_html=:html,
                stats_json=:stats_json::jsonb, completed_at=now()
            WHERE id=:rid
        """), {"rid": report_id, "md": content_md, "html": content_html, "stats_json": json.dumps(stats)})
        await session.commit()
    except Exception as e:
        await _mark_failed(session, report_id, str(e))
        raise


async def _aggregate_stats(session, period_start, period_end) -> dict:
    """单 SQL 查询聚合所有维度；失败/重试拆分；entity 取 top 5"""
    r = await session.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM articles WHERE created_at BETWEEN :s AND :e AND dedupe_of IS NULL) AS articles_total,
          (SELECT jsonb_agg(jsonb_build_object('feed_id', f.id, 'name', f.name, 'count', c.cnt) ORDER BY c.cnt DESC)
           FROM (SELECT feed_id, COUNT(*) cnt FROM articles
                 WHERE created_at BETWEEN :s AND :e AND dedupe_of IS NULL
                 GROUP BY feed_id) c JOIN feeds f ON f.id = c.feed_id) AS articles_by_source,
          ...
    """), {"s": period_start, "e": period_end})
    return r.mappings().first()  # dict
```

**伪 SQL 实际由 SQL 聚合查询拆解填充，详情留给切片 2.5 实施。**

#### scheduler 触发

```python
# app/scheduler.py 新增

scheduler.add_job(
    generate_daily_report,
    CronTrigger(hour=8, minute=0),
    args=[],  # 用 now() 自取当日
    id="daily_report",
    name="Generate daily report at 08:00",
    replace_existing=True,
)
scheduler.add_job(
    generate_weekly_report,
    CronTrigger(day_of_week="mon", hour=8, minute=0),
    args=[],
    id="weekly_report",
    name="Generate weekly report Mon 08:00",
    replace_existing=True,
)
```

**周期定义集中在 `app.config.ScheduleSettings`**（§9）：
- `daily_report_hour: int = 8` / `daily_report_minute: int = 0`
- `weekly_report_day_of_week: str = 'mon'` / `weekly_report_hour: int = 8`

#### 失败重试策略

- 报告生成失败（如 LLM 503、Markdown 解析失败）→ `_mark_failed` 写 `reports.status='failed'` + `reports.error`
- WebUI `/reports/{id}` 显示"上次生成失败，[立即重试]"按钮 → `POST /reports/{id}/retry` 调 `generate_daily_report(force_overwrite=True)`
- 同一 `(report_type, period_start, period_end)` 唯一 → 重试覆盖旧记录

### 10.2 Phase 2 API 连接器（切片 2.7 完整设计）

#### `feeds.config_json` schema（按 `type='api'`）

```json
{
  "endpoint": "https://hacker-news.firebaseio.com/v0/topstories.json",
  "method": "GET",
  "headers": {"User-Agent": "topic_collection/0.1"},
  "params": {},
  "auth": null,
  "rate_limit_per_hour": 60,
  "items_path": "$",                     // jmespath：从返回 JSON 提取 id 列表
  "id_to_detail": {                      // 拿 detail：每个 id 调一次
    "endpoint_template": "https://hacker-news.firebaseio.com/v0/item/{id}.json",
    "method": "GET"
  },
  "mapper": {
    "title":    "title",
    "url":      "url",
    "author":   "by",
    "time":     "time",                   // epoch seconds
    "content":  ["text", "url"]           // jmespath 路径或 [field_for_text, fallback_url]
  },
  "language_hint": "en"                   // 默认 en，可选
}
```

#### `app/ingest/api.py: fetch_api(feed) -> list[FeedItem]`

```python
async def fetch_api(feed: FeedRow) -> list[FeedItem]:
    cfg = feed["config_json"]
    async with httpx.AsyncClient(timeout=30, headers=cfg.get("headers", {})) as client:
        # 1. 拉列表
        resp = await client.request(cfg["method"], cfg["endpoint"], params=cfg.get("params", {}))
        resp.raise_for_status()
        ids = jmespath.search(cfg["items_path"], resp.json()) or []
        # 2. 拉详情（每个 id）
        results: list[FeedItem] = []
        for id_ in ids[: feed.get("max_items_per_fetch", 50)]:
            detail_url = cfg["id_to_detail"]["endpoint_template"].format(id=id_)
            detail = await client.request(cfg["id_to_detail"]["method"], detail_url)
            detail.raise_for_status()
            doc = detail.json()
            results.append(_map_to_feed_item(doc, cfg["mapper"], cfg.get("language_hint")))
        return results


def _map_to_feed_item(doc: dict, mapper: dict, lang: str) -> FeedItem:
    return FeedItem(
        source_url=jmespath.search(mapper["url"], doc) or "",
        title=jmespath.search(mapper["title"], doc) or "(no title)",
        author=jmespath.search(mapper["author"], doc) or "",
        published_at=datetime.fromtimestamp(jmespath.search(mapper["time"], doc), tz=UTC),
        content_text=jmespath.search(mapper["content"], doc) or "",
        lang=lang,
    )
```

#### Starter 配置示例（HN / GitHub / arXiv）

```yaml
# HN top stories → 在 config/feeds.yaml（用户后续编辑）
- name: "Hacker News Top"
  type: api
  url: https://hacker-news.firebaseio.com/v0/topstories.json
  enabled: true
  config:
    endpoint: https://hacker-news.firebaseio.com/v0/topstories.json
    method: GET
    rate_limit_per_hour: 60
    items_path: $
    id_to_detail:
      endpoint_template: https://hacker-news.firebaseio.com/v0/item/{id}.json
      method: GET
    mapper:
      title: title
      url: url
      author: by
      time: time
      content: text
    language_hint: en

- name: "GitHub Trending"
  type: api
  url: https://api.github.com/search/repositories
  enabled: true
  config:
    endpoint: https://api.github.com/search/repositories
    method: GET
    params:
      q: "created:>YYYY-MM-DD"
      sort: stars
    headers:
      Accept: application/vnd.github+json
    rate_limit_per_hour: 60
    items_path: items
    mapper:
      title: full_name
      url: html_url
      author: owner.login
      time: created_at
      content: description
    language_hint: en

- name: "arXiv cs.CL"
  type: api
  url: http://export.arxiv.org/api/query
  enabled: true
  config:
    endpoint: http://export.arxiv.org/api/query
    method: GET
    params:
      cat: cs.CL
      max_results: 50
      sortBy: submittedDate
      sortOrder: descending
    rate_limit_per_hour: 30
    items_path: feed.entry
    mapper:
      title: title
      url: id
      author: author.name
      time: published
      content: summary
    language_hint: en
```

#### API 连接器 vs RSS 的差异

- **速率更严**：API 通常有 hourly quota (`GitHub 60/h 未鉴权, 5000/h 鉴权`)，由 `config.feeds[i].config.rate_limit_per_hour` 控制；超出限速本批失败，记 `fetch_events(event_type='rate_limit')`
- **认证**：Bearer / API key 等在 `config.headers` 注入；token 不入 yaml → 占位 `${GITHUB_TOKEN}` 渲染时从 env 读
- **错误形态**：HTTP 4xx → `fetch_events(event_type='api_auth_error')`，连续 N 次禁用；429 → 退避重试（与 RSS 相同的 `feed_disable_after` 机制）
- **content 抓取**：API 连接器自己拉 detail（HN/Reddit 等模式），不用 `scrape.py` 抓详情页——降低对站点的依赖

### 10.3 Phase 2 CLI 命令扩展（与 §4 F11 对齐）

Phase 1 CLI（§3 / PRD §4 F11）：`feeds import | fetch | topic add | topic list | summarize | list | search | article | status | retry | backup`。

Phase 2 加：

| 命令 | 切片 | 含义 |
|---|---|---|
| `tc reclassify [--all] [--topic <id>] [--days <N>]` | 2.3 + 2.5 兜底 | 主题关键词全量重算。默认仅重算近 30 天（复用 `topics.reclassify_recent_days`，§9）；`--all` 强制全量；`--topic <id>` 仅重算该主题；写 `match_keywords()` + 入队 `topics` job 或补 `generate_topic_wiki` |
| `tc extract <article_id>` | 2.3 | 手动入队 `extract_entities` 任务（supersede 旧 + 新 job），同 `tc retry` 流程 |
| `tc translate <article_id> [--force]` | 2.2 | 手动入队 `translate`；`--force` 强制 supersede（即便已存在翻译） |
| `tc entity merge <canonical_zh_a> <canonical_zh_b>` | 2.3 | 把 A 的所有引用、提及、aliases 合并到 B（merge_aliases 服务），A 软删除或保留？本计划选软删除：`status='merged'` 字段（Phase 2 增量） |
| `tc entity search <query>` | 2.3 | CLI 直接查实体（不像 WebUI 走 `/api/entities?q=`，CLI 直接调 `services.entities.resolve_entity`） |
| `tc report list [--type daily\|weekly] [--limit 20]` | 2.5 | 查看历史报告 |
| `tc report show <id>` | 2.5 | 终端渲染 Markdown（rich 渲染）或纯文本打印 content_md |
| `tc report export <id>` | 2.5 | 写到 `data/reports/report-{type}-{period}.md` |
| `tc report retry <id>` | 2.5 | 重新生成（覆盖 reports 行） |
| `tc graph export [--topic <id>] [--since <days>] [--out data/graphs/x.json]` | 2.4 | 导出 `services.graph.graph_json()` JSON |
| `tc graph stats` | 2.4 | 节点 / 边 / top 实体统计 |
| `tc search --rerank` | 2.6 | 走 `search(use_rerank=True)` 路径，ranking 给分 |
| `tc backfill extract_entities [--all]` | 2.3 兜底 | 给历史 done 文章补 extract；并发=1 按时间倒序 enqueue（运行时间可能数小时，不阻塞 worker） |

#### CLI 与 WebUI 的关系

- CLI = 单机调试 / 脚本化运维；WebUI = 日常浏览
- 一份 service 层（`app/services/*.py`）两端共享
- CLI 不发 HTTP 给 WebUI（避免 uvicorn 启动依赖，反向亦不可行）
- 进度显示：CLI 长操作（`extract --all`、`backfill`）用 `rich.progress`；WebUI 长操作用 htmx spinner + toast

#### 性能预算

- `tc reclassify`（仅 30 天）：万级 articles 也只需几分钟（`match_keywords` 内存 jieba 匹配，无 LLM 调用）
- `tc backfill extract_entities`（万级）：27B 50 秒/篇 × 10000 = 数小时；放后台跑，提供进度条 + 中断恢复
- `tc report generate --now`：直接调 `generate_daily_report`，不等待 scheduler；调试用

---

## 11. 错误处理与降级总表

| 场景 | 行为 | UI 呈现 |
|---|---|---|
| oMLX 全挂 | **瞬时类任务**（生成/嵌入，5xx/超时/连接拒绝）保持 queued + lock_until 退避 1m→5m→15m 封顶、**attempt 不自增、无限续跑不进死信**；worker 领取门控（§6）掉线期间不领新 job；文章可浏览原文 | 概览红色横幅「LLM 离线」（scheduler 探测驱动，Phase 1 单进程与 worker 共享 healthy） |
| oMLX 瞬时错误但任务已领取 | `error_class='transient'`、**attempt 不自增**（死信预算不消耗）；到点自动续跑 | 任务详情显示退避倒计时 |
| **LLM 401/403/400（永久/配置）** | `error_class='permanent'`、`attempt+1`、短退避，达 `max_attempts=3` 后 `failed` 死信——鉴权/配置错快速暴露而非伪装成掉线 | 横幅明确报「鉴权失败」而非「LLM 离线」 |
| **病态文章反复超时**（healthcheck 正常） | `consecutive_timeouts` 累加，达 `llm.max_timeout_retries`（默认 3）且 healthcheck 正常 → **直接 `failed` 死信**（不再 attempt+1+max_attempts 循环：3×180s=9 分钟后再耗 9 分钟重试病态文章无意义）；掉线时 healthcheck 不过、计数不增、不与掉线混账 | 文章详情可手动重试或标记 unparseable |
| 永久错误（JSON/不可解析/401/403/400） | `error_class='permanent'`、`attempt+1`，达 `max_attempts=3` 后 `failed` 死信 | 文章详情可手动重试 |
| 高量 feed 首抓积压 | `max_items_per_fetch` 截断 + 水位告警记 fetch_events | fetch_events 状态徽标 |
| 仅嵌入不可用 | 语义通道关闭 | 搜索页提示「仅关键词模式」 |
| feed 连续失败 | 自动禁用 | Feed 列表状态徽标 |
| 向量维度失配 | 阻断写入 + 告警 | Settings 展示实测 vs 配置 |

---

## 12. 安全与凭据

- **默认本机不鉴权**（已确认），无需 token；若开启鉴权则 token 仅存环境变量（`TC_LLM_API_KEY`），不入库不入 repo
- Dashboard 默认绑定 `127.0.0.1`；无外网暴露
- DB 凭据走配置/env，仅本机回环访问
- 全部推理本地完成，无数据出机

---

## 13. 测试策略

- **单元**：dedup（URL/content hash）、cleaner、structured（JSON 修复）、fts（jieba 预切词）、config 校验
- **集成**：FakeLLM（内存 mock 三端点）跑通 抓取→去重→摘要→入库→检索
- **队列**：`embed_core` 退避（`lock_until` 未到）期间 `summarize` 成功 → `embed_summary` 仍能入队并建成 summary 向量；且两 task 各自独立去重（防 §5.2 拆分回归）
- **重试分类（A1）**：mock oMLX 持续掉线（连接拒绝/5xx）→ 断言瞬时类 job **不进死信**、`lock_until` 退避封顶 15m、恢复后续跑；mock 永久错误（JSON 解析失败）→ 断言 3 次后 `failed` 死信
- **跨源近似去重（B4）**：同事件跨源改写（URL/content_hash 不同、语义近似）→ embed 后断言 `dedupe_of` 合并、主题视图不重复占位
- **backfill（B3）**：切 active embed model 后全量重嵌，search 期间不混入旧 model 向量
- **HNSW 性能（D3）**：`ef_construction=128/ef_search=64` 下 P95 < 100ms 基准（万级向量）
- **summaries upsert content_hash 版本守卫（D5）**：mock supersede 竞态（H1 旧 job 带着过期 hash 提交、H2 新 job 已经先落）→ 旧结果**未**写入、H2 的结果保留（§6 状态机原子性）
- **loser done + drain_queue 不复活（D6）**：mock「文章在 done 之前 status 反复横跳、loser 文章在 dedup 后不再被 drain_queue 补队」（§6 文章状态机迁移触发点）
- **db**：pytest + 临时 Postgres（docker compose 测试库）；向量维度校验用例
- **降级**：mock `/v1/embeddings` 404 → 断言语义通道降级
- **结构化日志（D4）**：**双 sink**——① 结构化 JSON（job 级规约 `job_id/task/attempt/latency_ms/error_class`，写 `logs/tc-YYYYMMDD.jsonl`，供 `tc status` 排障与 grep）② 人类可读 Rich 控制台滚动日志（PRD §13，开发期终端实时看）。二者经同一 `logging` 配置分流到不同 handler、不互斥；卡住的 running job 可凭 JSON 日志定位（`tc status` 的排障底座）。`error_class` 与 §5.1 `processing_jobs.error_class` 对齐（transient/permanent）

### 13.1 Phase 2 测试分类（切片 2.1–2.7 配套）

| 类别 | 切片 | 测试形态 |
|---|---|---|
| **D7 WebUI smoke** | 2.1 | FastAPI TestClient + Jinja2 templates → 字符串包含关键文案（"LLM 健康"、"队列"、"搜索"等）；不测视觉；POST 路由用 form/CSRF token；HTMX 部分路由通过 `HX-Request: true` header 触发并断言返回 partial 不含 `<html>` / 含 htmx-triggered swap target |
| **D8 实体归并** | 2.3 | `services.entities.upsert_entities` 幂等 + 别名合并；`merge_aliases` 折叠 OpenAI/开放 AI 到同一 entity；`extract_entities` 完整 pipeline（用 FakeLLM 回放 fixture `entities_fixture.json`）→ 断言 article_entities / relations / entities 三表行数 + relations.source_articles_json |
| **D9 报告 schema** | 2.5 | `_aggregate_stats` 跑真实 DB（test seed）→ 断言 `stats_json` 字段全；`_render_html` 输入 Markdown → 输出 HTML 不为空 + 含 `<h1>`/`<table>` 等；`generate_daily_report` 跑通（FakeLLM）→ reports.status 变 succeeded + content_md / content_html / stats_json 三字段都写 |
| **D10 图谱 + 搜索扩展** | 2.4 + 2.6 | `graph_json(filters=...)` 返回 `{categories, nodes, links}` 三字段全；每个 link 含 subject/object/predicate/confidence；ECharts 兼容（与 echarts.min.js 一致字段名）。`search(use_rerank=True)` 在 FakeLLM 下返回按相关性排序结果；`similar(article_id)` 按 HNSW 距离排序 |
| **D11 翻译完整** | 2.2 | `services.translations.upsert_translation` content_hash 守卫；`run_translate` 走 FakeLLM `translate` fixture 回放；tc translate CLI + WebUI POST 都入队正确 |
| **D12 API 连接器** | 2.7 | `fetch_api(feed)` mock httpx 返回 fixture → list[FeedItem] 字段映射正确；jmespath 提 items / 模板 URL 渲染；rate_limit_per_hour 触发 fetch_events |
| **D13 DDL 迁移幂等** | 2.3–2.6 | `alembic upgrade head` 重复执行不报错；降级回滚后数据一致；DDL 增量 §5.1.5 跨表引用不漏 |
| **D14 CLI smoke** | 2.3–2.7 | `tc reclassify --all`、`tc report export`、`tc graph export`、`tc translate` 全跑 typer 入口 → 不抛异常 + 输出符合预期格式 |
| **D15 性能基准 (Phase 2 维度)** | 2.6 | search P95 < 100ms（万级向量 + Wiki 跨表）；图谱 JSON 序列化 < 500ms（300 节点）；报告生成 end-to-end < 60s（含 LLM FakeLLM 即时回放，但 schema 聚合 < 5s） |

---

## 14. Phase 1 MVP 任务清单（可用即可，无 WebUI，CLI 为入口）

**Day 1 必备（不属任何切片，立即做）**：
- [x] 0. **pg_dump 备份脚本** `scripts/backup.sh`（`pg_dump | gzip → data/backups/tc-YYYYMMDD.sql.gz`，保留 14 天）+ **`tc backup` CLI 主触发**（PRD §4 F11）+ scheduler 每日 03:00 可选自动化（§10）—— **数据比代码值钱，pgdata 卷不是备份**

**切片一：端到端跑通闭环**（对应验收 1/7/8）
> 这一片把 FakeLLM 集成测试搭起来——27B 真跑一篇 20–60s，开发迭代必须靠 mock，不然改一行提示词等一分钟
- [x] 1.1 脚手架：`pyproject.toml` + `docker-compose.yml`(pgvector) + config（`config.yaml` + `feeds.yaml`） + `scripts/init_db` + **退路 Python 3.12/3.13 备好**（§2）
- [x] 1.2 `app/db`：models + **Alembic 迁移（DDL §5 为参考快照，schema 唯一真源 = 迁移；维度定死 `vector(1536)` + `db.vector_dim=1536`，§5.2 切片一前必须敲定）+ 扩展/维度校验** + jieba 预切词（§5.3 `to_tsvector('simple', 拼接文本)` 写入，**不要**用 `array_to_tsvector`）；`scripts/init_db` = `CREATE EXTENSION vector` + `alembic upgrade head`（不写裸 DDL，§5.4）
- [x] 1.3 `app/llm`：base Protocol + omlx.py（生成/嵌入/端点探测；embed 封装层含 **instruct prefix**，query 加 / document 不加，§4.2）+ client（并发/重试/健康 + 单次探测 §4.4）+ prompts + structured（含 `parse_with_repair`，§6）+ **FakeLLM mock**（开发期 + 集成测试用，三端点内存实现，固定回放 fixture）
- [x] 1.4 `app/ingest`：feeds.py（feedparser + ETag/304）+ dedup.py
- [x] 1.5 `app/services/cleaner.py`：HTML→Markdown + 语言检测
- [x] 1.6 `app/pipeline.py`：processing_jobs 入队（幂等 `ON CONFLICT DO NOTHING` + supersede **同事务**，见 §5.1 部分唯一索引 / §6）+ worker（**单条原子 pick-and-claim SQL，§6**，FOR UPDATE SKIP LOCKED + UPDATE 同事务——**注意：领取 SQL 不自增 `attempt`**，attempt 由永久失败路径独占，§6/硬伤 2）+ lock_until 租约 5 分钟 + 长任务续租 + **重试按瞬时/永久分类**（瞬时无限续跑退避封顶 15m 不进死信，永久 3 次死信，§6/§11）+ **领取门控**（掉线时不领新 job，§6）+ recover（**按租约回收过期 running，仅 worker 启动时跑，跨进程安全**，§6）
- [x] 1.7 `app/services/llm_tasks.py`：`summarize` 任务（走 `complete_summarize()` 钩子，§6：**summaries upsert + tsv 刷新（两阶段，§5.3）+ embed_summary 入队同事务**；手动 `tc retry summarize` 也走同一钩子，F2 P0）+ `complete_embed()` 钩子（embed_core/embed_summary 落库 + job 状态推进同事务，手动 `tc retry embed_*` 也走，§6）+ `app/services/topics.py`：`match_keywords()`（供切片三的 `classify_topics` 快路径）；**CPU 密集（jieba/清洗）一律 `asyncio.to_thread`**（§2，不阻塞事件循环）
- [x] 1.8 `app/services/cli.py`（切片一部分）：`feeds import` / `fetch` / `summarize` / `list` / `search`（**先纯关键词**）/ `article <id>` / **`status`**（**队列深度 / 失败任务 / LLM 健康，无 WebUI 期间唯一可观测性**，连 psql 排障成本太高）/ **`retry <article_id> <task>`**（走对应 `complete_*()` 钩子）
- [x] 1.9 验收：PRD §15 验收 1（建库 + 抓取 + 清洗）/ 7（中文摘要）/ 8（关键词全文搜索），**用 FakeLLM 跑通**

**切片二：嵌入 + 混合检索**（对应验收 9）
> 维度策略（§5.2）切片一前定，向量功能本身切片二上
- [x] 2.1 `app/services/llm_tasks.py`：`embed_core`（title+body）+ `embed_summary`（summary），维度 1536 + instruct prefix（§4.2/§5.2）
- [x] 2.2 `app/services/search.py`：`search(q)` 混合检索（语义 top-k ∪ 关键词 top-k → **P1 即 RRF 融合** `1/(k+rank)`，§7）；语义通道 `WHERE model=<active embed model>`（§5.2/§7）；查询侧用 `websearch_to_tsquery`，**不要**裸 `to_tsquery`（§5.3/§7）；articles ∪ wiki_pages 按 ref_id 去重（§7）；`scripts/backfill` 规格定稿（切嵌入模型全量重嵌，§5.2）
- [x] 2.3 CLI：`search` 升级为 `mode=hybrid|semantic|keyword`（默认 hybrid）
- [x] 2.4 验收：PRD §15 验收 9（混合检索 P95 < 100ms，召回 100%；RRF 融合）

**切片三：主题 + Wiki 词条**（对应验收 3/5）
- [x] 3.1 `app/services/topics.py` 完善：topic CRUD + `match_keywords()` 快路径 + `classify_topics` LLM 慢路径（合并规则见 §6）
- [x] 3.2 `app/services/wiki.py`：文章词条生成（`related_json` = 同主题 article top-5，§6）
- [x] 3.3 CLI 补：`topic add` / `topic list` / `list --topic`
- [x] 3.4 验收：PRD §15 验收 3（主题跨源聚合）/ 5（Wiki 按**关键词**全文搜索，主题/实体浏览 P2）

**横切**：
- [x] X.1 `app/scheduler.py`：fetch_all + drain_queue + pg_backup（**自动化补充，主触发是 `tc backup` CLI**，§10）+ cleanup_fetch_events（§10）
- [x] X.2 测试：FakeLLM 集成用例（切片一就要有）+ 单元用例（dedup / cleaner / structured / fts / pipeline 并发）+ **重试分类用例（A1）** + **跨源近似去重用例（B4）**（§13）
- [x] X.3 验收：对照 PRD §15 Phase 1 条目（1/3/5/7/8/9/16）走通

**Phase 1+（CLI 增强，MVP 用后改进）**：
- [x] P1+.1 外部 LLM API 切换（OpenAI 兼容协议）：`app/llm/openai.py`（新 provider）+ `app/llm/factory.py`（per-capability factory：`build_provider(capability, settings)`）+ `app/config.py`（`GenerateSettings`/`ProviderConfig`/`EmbedSettings`/`RerankSettings` 扩展 endpoint/api_key_env 字段）+ `app/llm/client.py`（`_classify_http_error` 接入 `_retry_transient` 调用路径，401/403/400 → `PermanentError`；**在 except 块内联分类逻辑**——Python except 块内 raise 的异常不被同 try 的其他 except 捕获，DESIGN §4.X）；worker 双 `LLMClient`（generate/embed 独立信号量，embed 不被 27B 阻塞）；`app/llm/omlx.py`（`EMBED_INSTRUCT_PREFIX` 提升为 class attribute `embed_instruct_prefix`）+ `app/llm/base.py` Protocol 新增 `embed_instruct_prefix: str`；config schema 新增 `llm.generate.*` + `llm.providers.*`（向后兼容旧扁平字段）；26 个新测试（`test_openai_provider.py`），112/112 全过
- [x] P1+.2 `tc feeds fetch --count N`：CLI 新增 `--count` 选项（`typer.Option(None, "--count", "-c")`），`_feeds_fetch` 接收 `count: int | None`，`fetch_feed` 返回 items 后 `if count: items = items[:count]` 截断；超限记 `fetch_events(event_type='fetch_count_limited')`；测试：mock feed 返回 10 条 → `--count 3` 只入库 3 条
- [x] P1+.3 验收：对照 PRD §15 Phase 1+ 条目（17/18）走通
- [x] P1+.4 LLM 适配器层（统一 DTO + ProviderPatch）：`app/llm/patches.py`（ProviderPatch dataclass + 5 个预定义 patch：OMLX/OPENAI/MINIMAX/DEEPSEEK_CHAT/DEEPSEEK_REASONER）+ `app/llm/adapter.py`（LLMAdapter：build_generate_payload/parse_generate_response + build_embed_payload/parse_embed_response + strip_think_tags/strip_code_fences）；Provider（openai.py/omlx.py）简化为 HTTP 传输壳（委托 adapter）；factory 支持 `provider_cfg.patch` dict→ProviderPatch 转换；config.yaml minimax provider 加 patch 块；MiniMax-M3 通讯验证通过（healthcheck + summarize JSON 解析成功）；22 个 adapter 测试，**148/148 全过**

> **WebUI（`app/api` + `app/web`）整体移入 Phase 2。**

**Phase 1++（Post-MVP 紧急修复与硬化）**：
- [x] ++.1 OpenAIProvider 错误响应日志：`app/llm/openai.py` `_post()` 在 `raise_for_status()` 之前 `logger.error("API %s %s → %d: %s", ...)` 写入 provider 返回的 JSON body（限长 500 字符）。minimax 这类外部 provider 返回的 `{"type":"error", ...}` body 含丰富诊断信息（unknown model / 不支持的参数等），之前只 raise status_code、调试时黑盒
- [x] ++.2 `services.llm_tasks.py` + `services.topics.py` 用 `settings.llm.generate.model` 而非顶层 `settings.llm.model` 作 fallback：`run_summarize` / `complete_summarize` / `classify_topics` 三处 fallback 改为 `settings.llm.generate.model if generate else settings.llm.model`（与 `worker.py:57` / `cli.py:524` 已有的写法对齐）。**根因**：worker 切 minimax 后 minimax API 收到本地模型名 `Qwen3.8-27B-MLX-4bit` → 400 unknown model
- [x] ++.3 worker 鲁棒性（5 连修）：
  - `recover_interrupted(force_all_running=)` 双模式（pipeline.py）：force=True 仅启动期跑（Phase 1 单 worker 假设），force=False 仅回收过期 lease（运行期多 worker 安全）
  - `worker_loop` 启动期 force_recover + 运行期 60s 周期 recover，覆盖「前 worker 强杀」与「当前 worker LLM call hang」两类孤儿 lease
  - `_lease_renewer + process_job_with_lease_renewal`（pipeline.py）：把原本是死代码的 renew_lease 真正接入——后台 asyncio task 每 60s 写 lease_until，handler 完成时 stop_event 平滑停
  - `enqueue_jobs` 同事务触发 `articles.status: pending → processing`：之前整个 codebase 无迁移触发点，tc list 永远 pending、check_and_set_done 不触发
  - worker._TASK_CAPABILITY + handlers 加 `topics=generate` / `wiki=generate` 及对应薄壳（worker.py）；之前 topics/wiki job 被 claim 后只打 warning 跳过、永久卡 running
  - `tc retry` 接受 topics / wiki（cli.py）
  - handler 成功返回后**统一**写 `status='succeeded'`（process_job_with_lease_renewal 内）：summarize/topics/wiki 这类"轻 handler"不再卡 running
- [x] ++.4 测试：6+13+1 = 14 新单测（test_unit.py +2 generate.model fallback；test_crosscutting.py +6 worker routing/state machine/topics wiki handlers/end-to-end + 6 recover/lease/process_job_with_lease_renewal + 1 替换）。**148 → 162 passed**

**Phase 2（WebUI Dashboard + 实体/翻译/报告/图谱/高级检索/API 连接器）**：

> Phase 2 在 Phase 1+ 基础上展开。**所有切片 [ ] 实施完后** 新增测试覆盖 §13 D7–D15；§5.1.5 增量 DDL 完整迁移在本批次完成。

**切片 2.1 WebUI Dashboard 骨架（验收 1 回归 + 2 部分 UI 触发）**：
- [ ] 2.1.1 `app/main.py: create_app()` + lifespan 顺序：init_db → probe oMLX 三端点 → recover_interrupted → 启动 scheduler + worker task；`uvicorn app.main:app --host 127.0.0.1 --port 7111`
- [ ] 2.1.2 `app/api/{deps,health,dashboard,settings}.py` 路由骨架，**全部只做路由 + 调 service**，业务逻辑零侵入
- [ ] 2.1.3 `app/web/templates/base.html` + `components/` + `static/` vendored JS（htmx/echarts/sortable/pico）——见 §8.1 vendored 资源清单
- [ ] 2.1.4 HTMX partial swap 模式 + 错误形态（404/422/500）—— §8.1 HTMX 策略
- [ ] 2.1.5 D7 smoke 测试；WebUI 默认绑定 127.0.0.1，无 CSRF（本地单用户）

**切片 2.2 中文翻译（验收 #2 全）**：
- [ ] 2.2.1 `app/services/llm_tasks.py: run_translate()` 读 `articles.content_text` + `summaries.summary_text`，调 `generate` 中文 prompt（§4.6 translate 契约），`complete_translate` 钩子写 `translations` 表（content_hash 守卫与 summaries 同模式）
- [ ] 2.2.2 `complete_summarize` 同事务入队 `translate`（仅当 `articles.lang != 'zh' AND` user config `ingestion.auto_translate: true`）；手动：WebUI "翻译" 按钮 → POST `/articles/{id}/retry/translate` + `tc translate <article_id>`
- [ ] 2.2.3 D11 翻译测试（`tc translate` CLI + WebUI POST + translations 行写入）
- [ ] 2.2.4 §8 详情页新增 "翻译" Tab，渲染 `translations.translated_content` + `translated_title`；空时显示空 state + CTA "翻译"

**切片 2.3 实体抽取与归并（验收 #4 部分前置）**：
- [ ] 2.3.1 §5.1.5 entities / relations / article_entities DDL 增量迁移（含 pg_trgm 扩展）
- [ ] 2.3.2 `app/services/entities.py: extract_entities()` + 完整 pipeline（grounding 校验、upsert、merge_aliases）：见 §6.Y 伪代码
- [ ] 2.3.3 `complete_summarize` cascade 入队 `extract_entities`（与 topics 并列 priority 3，FIFO）
- [ ] 2.3.4 `complete_extract` 钩子：写 entities + relations + article_entities → 触发 `generate_entity_wiki` 仅在 entity 首次/description 变更
- [ ] 2.3.5 `app/services/entities.py: merge_aliases(canonical_a, canonical_b)` 服务（pg_trgm 模糊匹配 + 强制合并）
- [ ] 2.3.6 CLI：`tc extract <article_id>` / `tc entity merge` / `tc entity search`
- [ ] 2.3.7 D8 实体归并测试：extract pipeline end-to-end + merge_aliases 折叠 + aliases_json GIN 索引生效
- [ ] 2.3.8 §10.3 `tc backfill extract_entities [--all]`（历史 done 文章补跑，可中断恢复）

**切片 2.4 知识图谱（验收 #4 全）**：
- [ ] 2.4.1 `app/services/graph.py: graph_json(*, topic_id, entity_type, since_days, max_nodes=300)` 返回 `{categories, nodes, links, filters}`（ECharts 5.x force-graph 兼容字段名）
- [ ] 2.4.2 `app/api/graph.py: GET /graph` （Jinja2 + force-graph mounted via echarts）+ `GET /api/graph.json`（filter via query）
- [ ] 2.4.3 graph_filter UI 控件（topic multi-select + entity_type checkbox + 时间 slider）
- [ ] 2.4.4 CLI：`tc graph export [--topic] [--since] [--out]` / `tc graph stats`
- [ ] 2.4.5 D10 图谱测试：graph_json 形状 + ECharts 兼容 round-trip；300 节点 JSON 序列化 < 500ms
- [ ] 2.4.6 节点点击 → 跳回相关文章（侧栏 modal 与 §8 `/articles` 列表复用）

**切片 2.5 报告（验收 #6 全）**：
- [ ] 2.5.1 §5.1.5 reports.status / started_at / completed_at / error 列 DDL 增量；`reports_period_uniq` UNIQUE 索引
- [ ] 2.5.2 `app/services/reports.py: _aggregate_stats(period_start, period_end)` 单 SQL 聚合（articles/summaries/embeddings/topics/entities/relations/queue/feeds/llm 全字段）
- [ ] 2.5.3 `generate_daily_report(report_dt)` + `generate_weekly_report()` 服务（§10.1 伪代码）：stats → prompt → LLM → markdown → HTML（`markdown(md, extras=['toc','fenced_code','tables'])`）→ 同事务写 reports
- [ ] 2.5.4 §4.6 `generate_report` prompt 落地（中文 Markdown 5 章结构 + 不允许制造统计量约束）
- [ ] 2.5.5 scheduler `daily_report`(08:00) + `weekly_report`(周一 08:00) 注册（§10.1）
- [ ] 2.5.6 `app/api/reports.py: GET /reports` + `GET /reports/{id}` + `POST /reports/{id}/retry` + `GET /reports/{id}/export.md`
- [ ] 2.5.7 CLI：`tc report list / show / export / retry / generate --now`
- [ ] 2.5.8 D9 报告测试：stats_json schema 完整 + HTML 渲染非空 + 失败 → `status='failed'` + error 字段

**切片 2.6 高级检索（验收 #9 增强）**：
- [ ] 2.6.1 `app/services/search.py: search(*, use_rerank=False, mode='hybrid', page=1, page_size=20, filters)` 加 `use_rerank` 路径（§7.1 算法）
- [ ] 2.6.2 `LLMClient.rerank()` 透明降级链：oMLX `/v1/rerank` → 进程内 `bge-reranker-v2-m3`（§7.1 懒加载）→ 不重排（保持 RRF）
- [ ] 2.6.3 §5.1.5 `wiki_pages.tsv` 加列 + 跨表 UNION RRF（§7.1 Wiki 跨表检索 SQL）
- [ ] 2.6.4 `app/api/articles.py: GET /api/articles/{id}/similar?top_k=10` 同主题加权相似（§7.1 SQL）
- [ ] 2.6.5 `app/web/templates/search/results.html` 高级筛选 + Rerank toggle + Wiki/Article 切换
- [ ] 2.6.6 CLI：`tc search --rerank --mode rerank` 终态展示
- [ ] 2.6.7 D10 搜索扩展测试：use_rerank 排序正确；D15 性能基准 P95 < 100ms 万级 + Wiki

**切片 2.7 API 连接器（验收 #1 + F9）**：
- [ ] 2.7.1 `app/ingest/api.py: fetch_api(feed)` + `_map_to_feed_item(doc, mapper, lang)`（§10.2 完整实现）
- [ ] 2.7.2 `feeds.config_json` schema（§10.2 完整定义）+ Alembic 不需迁移（config_json 早就是 JSONB）
- [ ] 2.7.3 §9 `config/feeds.yaml` 注释示例三件：HN / GitHub Trending / arXiv cs.CL（user 直接复制即可）
- [ ] 2.7.4 rate_limit_per_hour 触发 `fetch_events(event_type='rate_limit')`；连续 4xx → `fetch_failures+1` → `feed_disable_after` 禁用（与 RSS 同机制）
- [ ] 2.7.5 D12 API 连接器测试：mock httpx 返回 fixture → 字段映射正确 + jmespath 提 items 正确 + 模板 URL 渲染正确

**切片 2.8 Phase 2 综合验收（验收 #2 / #4 / #6 / #14）**：
- [ ] 2.8.1 实环境跑通：HN 真实文章 → summarize → extract_entities → topics → wiki → translate → daily report D+1 → graph.json 渲染
- [ ] 2.8.2 验收 #2（翻译）：外文文章一键译为简体中文，UI 可见
- [ ] 2.8.3 验收 #4（图谱）：实体节点与关系边可点击跳回文章
- [ ] 2.8.4 验收 #6（报告）：日报/周报按计划生成，Dashboard 查看 + 导出 Markdown
- [ ] 2.8.5 整体性能：单篇 27B 文章 end-to-end < 2min；搜索 P95 < 100ms；图谱加载 < 1s
- [ ] 2.8.6 ≥ 162 + N 新测试全部通过（D7–D15），CI 全绿

---

## 15. oMLX 实测结论（2026-08-12）

| 项 | 结论 |
|---|---|
| 端口 | `http://localhost:8000` |
| 鉴权 | ✅ 已关闭：三端点不带 token 均正常（models 200 / embeddings dim 1536 / chat 生成 OK） |
| 模型列表 | `GET /v1/models` 正常，列出全部模型（含 DeepSeek-V4 / Qwen3.8-27B / MarkItDown 等） |
| 嵌入 | `POST /v1/embeddings` ✅ `Qwen3-Embedding-8B-4bit-DWQ`，模型输出 4096 维，**实际用 `dimensions=1536` 截断**（HNSW 2000 维上限，§5.2）；**指令感知** — query 侧需加 instruct 前缀，document 侧不加（§4.2） |
| 重排 | `POST /v1/rerank` ✅ Cohere 风格：入参 `query/documents/top_n`；出参 `results:[{index, relevance_score}]` |
| json_mode | ✅ `response_format:{type:json_object}` 被接受，返回合法 JSON |
| 生成模型 | `Qwen3.8-27B-MLX-4bit` ✅ 可用（json_mode 正常，质量更佳）；`9B INSTRUCT…` ✅ 可用（更轻量）；**`9B THINKING…` ⚠️ 加载失败**（Missing 154 parameters，权重不完整/损坏） |

> **遗留**：`Qwen3.5-9B-Claude-4.6-HighIQ-THINKING-HERETIC-UNCENSORED` 需在 oMLX 侧修复（补全权重/重新量化）。修复后把 config `llm.model` 改回即可，代码无需变更。

---

## 16. 已知限制（接受，或 P3 处理）

- **英文大小写**：**这条原表述不准确**——simple 词典本身会 lowercase（`to_tsvector('simple', 'Hello')` 与 `to_tsvector('simple', 'hello')` 结果一致），归一化是 `to_tsvector` 内部做的。真正会漏配的是**绕过** `to_tsvector` 的手工构造（`'a b c'::tsvector` / `array_to_tsvector`）。只要按 §5.3 走 `to_tsvector('simple', ...)` 写入 + `websearch_to_tsquery` 查询，英文大小写**自动解决**，不需要应用层 lowercase
- **外键策略**：产物 / 向量 / 队列 / 主题归属统一 `ON DELETE CASCADE`（删文章/主题/Feed 自动清理孤儿行）；`dedupe_of`、`articles.feed_id`、`relations.source_article_id` 用 `ON DELETE SET NULL`（保留引用方转独立）；仅 `wiki_pages.ref_id` 为多态引用无法建 FK（§5.1 注释），靠应用层校验——P3 归档裁剪直接受益于级联
- **生成/嵌入共用并发=1**：**待验证假设**——oMLX 按请求切换模型会抖动加载是真，但 MLX 统一内存可同时常驻多模型（27B-4bit ≈14GB + 8B 嵌入 ≈5GB，64GB+ Mac 装得下，无需切换、无抖动）。若实测同时常驻可行，信号量改 per-capability 一槽（gen/embed 各一），embed 不被 27B 阻塞、语义索引吞吐翻倍。P1 先按 1，实测后升 2（§4.4）
- **主题关键词快路径跳过 LLM 分类**：命中关键词的文章 `method=keyword` 入库、**整篇跳过 LLM 分类**，不会被评估到其他未命中关键词的主题——主题质量高度依赖关键词设计，LLM 不补救。为已知召回取舍，P3 `tc reclassify` 全量重跑兜底（PRD §15 #3 / §16）；P2 可加「命中关键词也跑 LLM 复议其他主题」选项

### 16.1 Phase 2 已知限制（与 §14 切片同步）

| # | 限制 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **WebUI 单进程 uvicorn `workers=1`** | 中高并发用户场景（数个并行浏览）下，长 LLM 调用可能阻塞其他请求 | 单用户本地工具场景可接受；多 worker 化需要 LLM client 共享状态，问题复杂 |
| 2 | **Jinja2 SSR 全栈**：所有页面服务端渲染，无客户端 bundle，TTFB 受 LLM 影响 | 首屏渲染依赖后端 + DB 响应时间，慢时 200-500ms（可接受） | 已通过 SSR 简化部署，不切换 Client Component |
| 3 | **ECharts 大图（>2000 节点）需前端 LOD** | 图谱页超过 ~2000 节点直接渲染会卡 | 节点 max=300（filter 参数）；超出时分批聚合（`graph_json` 取 top N；Phase 3 加 ELK/force-graph clustering） |
| 4 | **`/v1/rerank` 降级到 `bge-reranker-v2-m3` 需进程内常驻 ~3GB** | 长时占用内存；冷启动 lazy-load 需 5-10 秒 | 不常驻可接受；启用与否由 config 控制 |
| 5 | **`tc reclassify --all` 万级文章库下跑数小时** | 单次命令长时间运行 | 默认仅重算 30 天（`reclassify_recent_days`）；CLI 支持 `--topic <id>` 缩小范围；后台 worker 异步模式（Phase 3） |
| 6 | **HTMX partial swap 模板继承开销** | 列表翻页频繁时 Jinja2 render 重渲染（每次 ~30ms） | 静态模板不依赖 DB 上下文，已是 O(small)；Phase 3 切 Client Component 进一步降延迟 |
| 7 | **`wiki_pages.ref_id` 多态无 FK** | 删 article/topic/entity 时需应用层同事务删对应 wiki_page，遗漏会留孤儿 | §6.3 实施细节明确 wiki_pages 删除顺序；统一 `services.wiki.delete_orphan_pages()` 提供 |
| 8 | **entities.canonical_name_zh UNIQUE 变更需数据迁移** | Phase 1 已有 `canonical_name` UNIQUE 的数据库需要迁移脚本（§5.1.5） → 需手动 merge_aliases 折叠 | Alembic 迁移内置 `merge_aliases` 调用；DEV 环境可重置 |
| 9 | **LLM 报告生成走 27B ≥ 50 秒/天** | scheduler 每日 8:00 触发，CPU 占用 ~1min + 数据库聚合 ~5s | 不可见影响；周报更长；Phase 3 加缓存（同 period 直接读上次） |
| 10 | **图谱性能 vs 节点数线性增长** | 1000+ 节点的 relations UNION 不走索引，遍历 O(N) | 本地个人库典型 <500 节点，可接受；超过加 redis 缓存 + 增量构建 |
| 11 | **API 连接器速率限制为本机配置** | 用户错配 `rate_limit_per_hour=10000` 会打爆被对端 ban | `feeds.config_json` 校验提供合理上限；UI 显示"上次请求被 429 警告" |
| 12 | **Phase 2 增量 DDL 不可拆分为更小 patch** | §5.1.5 所有 `ALTER TABLE` 在同一 Alembic revision；多列加约束需同步 | Phase 2 单一 revision 即可，上线后无关键依赖 |

---

## 17. 文档元约定（Phase 2 新增）

- **结构性内容只在一处维护**：PRD §11 已锁定本原则（消除副本漂移）；DESIGN.md 与 CLAUDE.md 互引不重复
- **Phase 2 实施细节以本文件 §8 / §10.1 / §10.2 / §10.3 / §6.X / §7.1 为权威**；任何 PRD 与本文件冲突以本文件为准（PRD 是产品合同，DESIGN 是工程蓝图；以"实现上 PRD 可被调整"原则落地）
- **章节交叉引用规范**：本文用 `§X.Y` 引节、`§X` 引节首；引 PRD 用 `PRD §X`；引 PRD #X 引验收条目
- **未来追加的 P3 任务**：新增 P3 段而非塞入既有 Phase 2 切片，保持 §14 切片粒度一致
