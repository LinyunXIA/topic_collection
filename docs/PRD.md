# PRD：Topic Collection —— 主题信息采集 + 摘要 / 翻译 / 知识图谱 / LLM Wiki

版本：v0.9（2026-08-19）· 状态：切片一完成，48/48 tests passed
> 工程细节（目录结构 / DDL / LLM 接口 / 流水线）以 [DESIGN.md](DESIGN.md) 为权威
> v0.9：切片一实现完成（PRD §15 验收 1/7/8 通过 + 真实环境 20 篇 HN 跑通）；与 DESIGN v0.10 同步；Docker 端口 5433（§11）
> v0.8：§11 配置砍成示意 + 指向 DESIGN §9（结构性内容只在一处维护，连续三轮同步漂移后彻底消灭副本）；与 DESIGN v0.9 同步
> v0.7：架构审查三轮落档——**summaries upsert content_hash 谓词改对**（§5 §15 #7 + DESIGN §6：比对文章当前版本而非存量摘要，旧谓词方向反过期覆盖新）；**pick-and-claim 不再自增 attempt**（§14 §15 #7 + DESIGN §6：attempt 由永久失败路径独占，否则瞬时退避占 max_attempts 预算）；**文章状态机迁移时机明确**（DESIGN §6：pending→processing/processing→done/loser 同事务 done）+ **drain_queue 谓词改三条件互锁**（DESIGN §6：`status='pending' AND dedupe_of IS NULL AND NOT EXISTS`，阻断 loser 周期性复活）；**超时转永久直接 failed**（§14 §15 #7 + DESIGN §6/§11：不再 attempt+1 循环，180s×3=9 分钟试完直接死信）；**主题聚合 dedupe_of IS NULL 硬约束**（§5 §15 #16 + DESIGN §6：loser 在 dedup 之前已写 article_topics，不过滤就重复占位）；**§13 测试加 D5/D6 断言**（summaries 守卫、loser 不复活）；与 DESIGN v0.8 同步
> v0.6：架构审查二轮落档——**Phase 1 回归单进程**（§5 架构决策：worker + APScheduler 同 asyncio 循环，`make worker` 起全套，drain_queue 随 scheduler 在场、高量 feed 不滞留；CLI 走 services 不起 worker）；**重试分类落 schema**（§14 风险表：瞬时 5xx/超时 attempt 不自增、无限续跑退避封顶 15m 不进死信；永久 401/403/JSON 失败 max_attempts 死信；**401/403 归永久**）；**近似去重向量 body↔body 同粒度**（§5/§15 #16）；**embed_summary 优先级 4→6**（§5）；**init_db 统一走 Alembic**（§11）；**`tc summarize` 作用域明确** + **`tc topic add` 同步触发近 30 天 reclassify**（§4 F11/§15 #3）；与 DESIGN v0.7 同步
> v0.4：架构审查落档——重试按瞬时/永久错误分类（§14/§15 #7）、检索 P1 即 RRF（§5）、跨源向量近似去重（§3/§5）、CPU 密集走 to_thread（§13）、`tc backup` CLI 主触发备份（§4 F11/§11）等；与 DESIGN v0.5 同步

---

## 1. 背景与目标（Context）

用户每天面对大量分散在 RSS、API、网站中的信息（技术博客、新闻、领域动态），缺乏一个统一的「采集 → 消化 → 沉淀」工具。本产品构建一个**本地运行、数据私密、中文友好的主题信息聚合与个人知识库系统**：

- 从任意 RSS/Atom、开放 API、网页爬虫采集文章；
- 用**本地 LLM（oMLX，Apple Silicon）**对内容做中文摘要、翻译、实体与关系抽取、主题分类；
- 沉淀为**可搜索的知识库 Wiki** 和**实体-关系知识图谱**；
- 通过**本地 Web Dashboard** 可视化浏览，并由**定时任务**产出日报/周报。

项目背景：`topic_collection/` 为全新空项目（Python 3.14.6，Apple Silicon，空 venv），从零搭建。

### 已确认的产品决策

| 维度 | 决策 |
|---|---|
| 数据源 | 任意 RSS/Atom + 开放 API/爬虫 + 按主题关键词跨源聚合（三者全要） |
| 产品形态 | Phase 1 以 **CLI** 为核心可用入口（fetch/summarize/search/list）；**WebUI Dashboard 整体移入 Phase 2**；定时任务/报告 Phase 2 |
| LLM Wiki | 可搜索的知识库站点（文章归档、自动分类、交叉链接、全文检索） |
| 知识图谱 | 实体-关系三元组 + 图可视化（ECharts） |
| LLM 能力 | 仅本地推理：oMLX（`http://localhost:8000`，OpenAI 兼容 RESTful，**本机不鉴权**）本地提供 3 模型——生成 `Qwen3.8-27B-MLX-4bit`（备选 9B INSTRUCT；THINKING 待修复）、嵌入 `Qwen3-Embedding-8B-4bit-DWQ`（**原生 4096 维，经 `dimensions=1536` 服务端截断**）、重排 `Qwen3-Reranker-4B`（§8） |
| 输出语言 | 中文（摘要/Wiki 默认中文，原文保留；翻译默认译为简体中文） |
| 数据库 | PostgreSQL + pgvector（向量语义检索 + tsvector 中文全文检索） |

---

## 2. 目标用户与使用场景

- **用户**：个人/极客（单用户本地工具），关注技术、行业、研究动态。
- **典型场景**：
  1. 每天新增几篇到几十篇订阅文章，自动获得中文摘要，扫一遍即可决定是否精读；
  2. 外文文章一键翻译成中文全文；
  3. 按主题（如「RAG」「多模态」「某公司动态」）跨源聚合，追踪一个话题的持续进展；
  4. 浏览「XX 公司与 XX 产品」的实体关系图谱，点击节点回看相关文章；
  5. 在 Wiki 中按主题/实体浏览已沉淀的知识，并全文检索；
  6. 早晨收到昨日日报、周一收到周报，含主题热度与摘要精华。

---

## 3. 产品范围（Scope）

### In Scope
- 数据采集：RSS/Atom、配置化 API 连接器、礼貌网页抓取；按主题关键词聚合
- 内容处理：HTML→Markdown 清洗、去重（URL/内容精确 + 嵌入建好后跨源向量近似去重，见 §5 / DESIGN §6）、语言检测
- LLM 处理（本地）：中文摘要+要点、全文翻译、实体与关系三元组抽取、主题分类、Wiki 词条生成、报告综合
- 知识沉淀：PostgreSQL 存储、实体-关系图谱、可搜索 Wiki、交叉链接
- 展示：本地 Web Dashboard（Feed 管理、文章列表/详情、图谱、Wiki、话题、报告、设置）
- 自动化：定时抓取 + 流水线消费 + 日报/周报
- 本地 LLM 抽象：oMLX（主）+ Ollama（备选，可切换）；嵌入**无进程内降级**——oMLX 不可用时关闭语义通道、仅关键词（DESIGN §4.3）

### Out of Scope（本版本）
- 多用户/账号/鉴权
- 云端部署、多进程/分布式（单应用进程 + 单机 PostgreSQL 足够）
- 移动端 App
- 深度社交/推荐算法
- 对任意网站的通用语义解析（仅礼貌抓取主流页面 + 主内容提取）

---

## 4. 核心特性与用户故事

| # | 特性 | 用户故事 | 优先级 |
|---|---|---|---|
| F1 | RSS/Atom 订阅 | 添加任意 RSS/Atom 源，定时抓取，304/去重，失败自动禁用 | P0 |
| F2 | 中文摘要 | 新文章自动生成中文摘要 + 3–5 条要点，可重新生成 | P0 |
| F3 | 知识库检索 | 对文章/Wiki 做全文搜索（中文友好） | P0 |
| F4 | 文章归档与 Wiki 词条 | 每篇文章生成中文词条页（P0 基础），跨文章按实体/话题互链（P2 完整） | P0（基础）/P2（完整） |
| F5 | 主题关键词聚合 | 定义主题+关键词并分类（P1）、跨源聚合按热度（P2 视图） | P1/P2 |
| F6 | 全文翻译 | 非中文文章一键译为简体中文全文 | P2 |
| F7 | 知识图谱 | 抽取实体-关系三元组，ECharts 力导向图可视化，可筛选 | P2 |
| F8 | 日报/周报 | 每日/每周自动生成报告（主题热度、精华摘要、源健康、图谱增长） | P2 |
| F9 | 开放 API 连接器 | 配置化连接 HN / GitHub / arXiv 等（骨架 P2，广度 P3） | P2/P3 |
| F10 | 网页抓取 | 礼貌抓取 + 主内容提取（readability、反爬礼仪、增量抓取） | P3 |
| F11 | CLI（Phase 1 主入口） | `feeds import` / `fetch`（**`--count N` 限制单次抓取条数，Phase 1+**） / `topic add`（**同步触发近 30 天 `match_keywords()` 重算 + 未命中关键词文章入队 `topics`，§6/§15 #3**） / `topic list` / `summarize`（**作用域 = 全部 `status='pending'`（无 summary）的文章；可选 `--article <id>` 单篇强制重生成走 `complete_summarize()` 钩子，§6**） / `list [--topic]` / `search` / `article <id>` / **`status`**（队列深度 / 失败任务 / LLM 健康，**无 WebUI 期间唯一可观测性**） / **`retry <article_id> <task>`**（走 `complete_*()` 钩子，详见 DESIGN §6）；**`backup`**（`pg_dump` 备份主触发，DESIGN §10，数据比代码值钱）；report/graph 导出留 Phase 2；**`reclassify`**（P3，主题关键词快路径跳过 LLM 分类的兜底全量重跑，§15 #3） | P0 |
| F12 | 告警 | 主题命中、Feed 故障、LLM 掉线时通知（桌面/邮件） | P3 |

---

## 5. 系统架构

单 Python 进程，三子系统协作，无 Celery/Redis（单用户用不到）：

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
               │   PostgreSQL 15+ (pgvector + tsvector + GIN)   │   │
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

**关键架构决策**
- **单应用进程 + 外部 PostgreSQL 服务**（PRD §3 Out of Scope 本意）：Phase 1 无 WebUI 时 `python -m app.worker`（`make worker`）在一个 asyncio 循环里常驻 **worker task + APScheduler**——不拆 worker/scheduler/CLI 三进程（多进程只会制造 `LLMClient.healthy` 不共享、drain_queue 缺位等自找的坑）；Phase 2 加 uvicorn WebUI 路由同进程。CLI（`tc ...`）走 services 层、不启动 worker。FastAPI 承载 Web UI、APScheduler（AsyncIOScheduler）、流水线 worker（asyncio 任务）
- 全异步：httpx 抓取、SQLAlchemy 2.0 async + asyncpg
- **队列 = Postgres 表**（`processing_jobs`），worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务，可跨重启、Dashboard 可观测，无需 Redis/Celery；活跃态 `(article_id, task)` 部分唯一索引 + `ON CONFLICT DO NOTHING` 幂等入队，内容变更时旧 job 标 `superseded`（详见 DESIGN §5.1/§6）
- **PostgreSQL + pgvector**：`article_embeddings.vector(1536)` 支撑语义检索，`tsvector + GIN` 支撑中文全文检索；开发环境已确认用 **Docker Compose** 起 pgvector（`docker compose up -d`，pgvector 官方镜像，见 DESIGN §5.4）
- **生成/嵌入/重排全走 oMLX**（`http://localhost:8000` 已实测）：OpenAI 兼容 **RESTful** 三端点——`POST /v1/chat/completions`（生成 `Qwen3.8-27B-MLX-4bit`，备选 9B INSTRUCT）、`POST /v1/embeddings`（嵌入 `Qwen3-Embedding-8B-4bit-DWQ`，**原生 4096 维，经 `dimensions=1536` 服务端截断**）、`POST /v1/rerank`（重排 `Qwen3-Reranker-4B-mxfp8`，P2）；本机不鉴权；Ollama 为生成备选可切换
- **增量处理**：仅新/变更文章入流水线；每个 LLM 产物按 `(article, task, model, content_hash)` 缓存

**数据流水线**：`fetch → normalize → dedup（URL hash / content hash，LLM 花钱前）→ clean → 按任务入队 processing_jobs → LLM 各阶段（embed_core → summarize → embed_summary → topics → wiki）→ 图谱与 Wiki 构建 → 向量 + tsvector 索引`；**每篇文章的 insert→enqueue 同一事务**（崩溃不留孤儿文章，DESIGN §6）；**embed_core 落地后、summarize 入队前对近 `dedup.window_days`（默认 30 天）文章做向量近似去重**（用 **body↔body 同粒度**向量——查询向量与候选都取 `kind='body'` 行，余弦相似度 ≥ `dedup.threshold`（默认 0.95）→ 命中走 supersede + `dedupe_of` 合并 mention_count（多跳扁平化到终极 winner）、限同语言、可逆、记 `fetch_events` 日志，DESIGN §6 / PRD §15 #16）
- 每个抓取源独立 try/catch；连续失败 N 次自动禁用并提示
- LLM 掉线 → 任务保持 `queued` + `lock_until` 退避重试（worker 领取条件自动跳过未到期行，到点自动续跑），healthcheck 门控防打爆；文章仍可浏览原文
- 启动时 `recover_interrupted()` **按 lock_until 租约回收**过期 `running` 任务（租约未到=活任务，不动；跨进程安全，详见 DESIGN §6）
- **主题分类（P1）**：关键词快路径命中即计入（`method=keyword`，不跑 LLM）；仅未命中关键词的文章进 LLM 打分（≥0.6 记入，`method=llm`）；`UNIQUE(article_id, topic_id)` 一篇文章一主题一行；聚合按 `score DESC, published_at DESC`（规则详见 DESIGN §6）

---

## 6. 模块划分

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `app/config.py` | 加载 `config.yaml` + 环境变量覆盖，pydantic-settings 类型化配置 | `get_settings() -> Settings` |
| `app/db/` | 引擎/会话、SQLAlchemy 模型、pgvector + tsvector 索引、Alembic 迁移 | `init_db()`, `get_session()`, `run_migrations()` |
| `app/ingest/` | 采集：RSS 抓取（ETag/304）、API 连接器（配置驱动）、礼貌爬虫、去重；**全局并发 semaphore（`ingestion.global_concurrency`）+ 每域限速（`ingestion.per_host_interval_ms`）**——源多不打爆对端、不被对端识别为机器人封禁（DESIGN §6） | `fetch_feed()`, `fetch_api()`, `scrape()`, `canonicalize_url()/content_hash()` |
| `app/services/cleaner.py` | HTML→文本→Markdown 清洗、样板去除、编码修复、语言检测 | `clean_html(html) -> CleanedContent` |
| `app/llm/` | 本地 LLM 抽象：Provider Protocol、oMLX/Ollama 生成后端、嵌入（oMLX `/v1/embeddings`，**无降级**）、重排（oMLX `/v1/rerank`，P2）、facade（重试/并发/健康检查）、提示词模板、JSON 修复 | `LLMProvider.generate()/embed()/rerank()`, `LLMClient` |
| `app/services/llm_tasks.py` | 各 LLM 阶段（幂等、缓存、类型化错误） | `summarize/translate/extract_entities/classify_topics/generate_wiki_entry/generate_report/embed_core/embed_summary` |
| `app/services/entities.py` | 实体去重合并、别名、关系计数与置信度 | `upsert_entities()`, `link_relations()`, `resolve()` |
| `app/services/topics.py` | 主题 CRUD、关键词预匹配、跨源聚合查询 | `match_keywords()`, `aggregate_topic()` |
| `app/services/wiki.py` | 生成/更新文章/主题/实体词条页、交叉链接、全文检索；**删 article/topic/entity 时同事务删对应 wiki_page**（`ref_id` 多态无 FK，DESIGN §5.1/§7） | `build_*_page()`, `link_related_pages()`, `search(q)` |
| `app/services/graph.py` | 组装 ECharts categories/nodes/links JSON，可按主题/时间筛选 | `graph_json(filters)` |
| `app/services/reports.py` | 日报/周报生成（结构化统计 → LLM 综合为 Markdown/HTML） | `generate_daily_report()`, `generate_weekly_report()` |
| `app/pipeline.py` | 队列消费、任务分发、退避/死信、状态流转 | `enqueue()`, `worker_loop()`, `process_job()`, `recover_interrupted()` |
| `app/api/` | FastAPI 路由 + Jinja2/HTMX WebUI（**Phase 2**） | `GET /`, `/feeds`, `/articles/{id}`, `/wiki/{slug}`, `/graph`, `/search`, … |
| `app/scheduler.py` | APScheduler：定时抓取、排空队列、日报/周报 | `setup_scheduler(app_state)` |
| `app/services/cli.py` | CLI 薄封装（**Phase 1 主入口**） | typer 命令：**feeds import / topic add / topic list / fetch / summarize / list [--topic] / search / article <id> / status**（队列深度 + 失败任务 + LLM 健康，无 WebUI 期间唯一可观测性）/ **retry <article_id> <task>**（走 `complete_*()` 钩子，DESIGN §6）/ **backup**（`pg_dump` 主触发，§4 F11/DESIGN §10）/ **reclassify**（P3，主题关键词快路径跳过 LLM 分类的兜底全量重跑，§15 #3）；逻辑全部复用 services |

---

## 7. 数据模型（PostgreSQL + pgvector）

PostgreSQL 15+ + `pgvector` 扩展 + SQLAlchemy 2.0（asyncpg）+ Alembic。核心表：

| 表 | 用途 | 关键字段 |
|---|---|---|
| `feeds` | 全部数据源（rss/api/scrape） | `type, url, enabled, config_json, etag, last_modified, fetch_status, fetch_failures` |
| `articles` | 归一化文章 | `feed_id, source_url, url_hash UNIQUE, content_hash, title, lang, status(pending/processing/done/unparseable/error), dedupe_of, mention_count`；另有 `tsv tsvector`（GIN 索引） |
| `article_embeddings` | 多粒度向量（标题/摘要/正文） | `article_id, kind, model, content_hash, dim, vector vector(1536)` — UNIQUE(article_id, kind, model)，upsert 保留最新，HNSW 索引；**1536 维** = 模型原生 4096 经 oMLX `dimensions=1536` 服务端截断（HNSW 2000 维上限，DESIGN §5.2）；正文 embed 截断 8K（见 DESIGN §5.2） |
| `article_versions` | 原始内容留档（供重处理） | `kind(raw_html/raw_text), content` |
| `summaries` | 中文摘要缓存 | `summary_text, key_points_json, confidence, content_hash, UNIQUE(article_id, lang, model)` — upsert 保留最新 |
| `translations` | 翻译缓存 | `src_lang, tgt_lang, translated_title, translated_content, content_hash` — UNIQUE(article_id, src_lang, tgt_lang, model) |
| `entities` | 去重后实体 | `canonical_name, aliases_json, entity_type, description, mention_count, confidence` |
| `relations` | 三元组 | `subject_id, predicate, object_id, source_article_id, confidence` |
| `topics` | 用户主题+关键词 | `name, description, keywords_json, enabled` |
| `article_topics` | 文章↔主题 | `score, method(keyword/llm), UNIQUE(article_id, topic_id)` |
| `wiki_pages` | 生成的词条 | `kind(article/topic/entity/manual), ref_id, slug UNIQUE, content_md, related_json` |
| `processing_jobs` | 流水线队列 | `task, status(queued/running/succeeded/failed/superseded), attempt, priority, error, lock_until, content_hash`；活跃态 `(article_id, task)` 部分唯一索引（防重复入队） |
| `reports` | 报告 | `report_type(daily/weekly), period, content_md, content_html, stats_json` |
| `fetch_events` | 源健康审计 | `ok, error, item_count` |

**检索（双通道，中文友好）**：
- **向量语义检索**：`article_embeddings.vector(1536)`（`Qwen3-Embedding-8B-4bit-DWQ` 原生 4096 维，经 `dimensions=1536` 服务端截断，DESIGN §5.2），**HNSW** 索引，余弦距离 `ORDER BY vector <=> $1`；对标题/摘要/正文分别建 embedding——检索主依赖 title+summary，正文超长截断 8K 只作补充（DESIGN §5.2），供语义搜索与相似文章推荐。
- **关键词全文检索**：`articles.tsv tsvector` + **GIN** 索引；中文用 **jieba 预切词**后写入 `tsvector('simple', ...)` 提升召回（避免依赖需编译的 `zhparser` 扩展）。
- **混合检索**：`search(q)` = 向量 top-k ∪ 关键词 top-k → **P1 即用 RRF 融合**（`1/(k+rank)`，k≈60，量纲无关、~10 行，比原始加权求和更简单且正确——cosine 与 ts_rank 量纲不可比，直接相加无意义）；P2 在 RRF 之上叠 oMLX Reranker（DESIGN §7）。
- 单用户量级（数万篇）Postgres 单机足够，不引入 Elasticsearch / Meilisearch。

---

## 8. LLM 抽象层（本地推理）

### 接口
```python
class LLMProvider(Protocol):
    name: str                # "omlx" | "ollama"
    base_url: str            # oMLX OpenAI 兼容端点
    api_key: str | None      # 可选；None/空 = 不发送鉴权头（本机默认不鉴权）
    generation_model: str    # Qwen3.8-27B-MLX-4bit
    embedding_model: str     # Qwen3-Embedding-8B-4bit-DWQ
    rerank_model: str | None # Qwen3-Reranker-4B-mxfp8（P2）
    async def generate(req: GenerateRequest) -> GenerateResult   # POST /v1/chat/completions
    async def embed(texts: list[str], model=None) -> EmbedResult  # POST /v1/embeddings
    async def rerank(query, docs, top_n) -> list[int]             # POST /v1/rerank（P2）
    async def healthcheck() -> bool
class GenerateRequest: model, messages, temperature=0.3, max_tokens, json_mode, timeout_s=180
class GenerateResult: text, finish_reason, usage, latency_ms
```
`LLMClient` facade：并发信号量（默认 1）、超时、指数退避重试、`healthy` 健康状态。重试/超时只在这层处理，services 不碰传输细节。

### 生成后端：oMLX（RESTful）
- **调用方式**：OpenAI 兼容 **RESTful** `POST {endpoint}/v1/chat/completions`；模型 = `Qwen3.8-27B-MLX-4bit`。
- **鉴权**：**本机不鉴权**（已实测，不带 token 正常）；若将来开启，`api_key_env` 从环境变量读取，不写进仓库。
- **json_mode**：请求 `{"response_format": {"type": "json_object"}}`（oMLX 支持时启用），否则走解析+修复（§8 结构化输出）。
- **备选 Ollama**：同为 OpenAI 兼容 REST，配置一键切换。

### 嵌入模型（Embeddings，支撑 pgvector）—— 直接用 oMLX 本地嵌入模型
- **为什么用独立嵌入模型**：pgvector 语义检索必须把文本变成向量；9B 生成模型不产出嵌入，故用 oMLX 上的**嵌入模型** `Qwen3-Embedding-8B-4bit-DWQ`。
- **主后端（oMLX）**：`POST {endpoint}/v1/embeddings`，与生成同走本地、不鉴权。**无进程内 fastembed 降级**（维度不匹配 + 向量空间不同，混存不可检索；详见 DESIGN §4.3）。oMLX 不可用 → 语义通道关闭、仅关键词（Dashboard 提示）。
- **维度**：Qwen3-Embedding-8B 模型最大输出 4096 维，**用 `dimensions=1536` 服务端截断**（HNSW 2000 维上限 + OpenAI 推荐档位，详见 DESIGN §5.2）；DDL 与 `db.vector_dim` 统一 1536；启动时校验一致（防 HNSW 失配 / 模型切换）。
- **指令感知**：query 侧需拼 instruct 前缀、document 侧不加（Qwen3-Embedding 官方推荐；DESIGN §4.2），在 embed 封装层一处处理。
- 嵌入按 `(article, kind, model)` 缓存；嵌入拆两个独立 P1 任务：`embed_core`（title+body，与 `summarize` 并行入队）+ `embed_summary`（`summarize` 成功后补 summary；独立 task 去重，避免 `embed_core` 退避占槽吞掉 summary 入队，见 DESIGN §5.2）。

### Reranker（重排序）—— P1 不需要，P2 直接用 oMLX 模型
- 混合检索的「向量 top-k ∪ 关键词 top-k」在 P1 **即用 RRF 融合**（量纲无关，§5/§15 第 9 条）即可满足 MVP 验收。
- P2 精度增强：调 oMLX `POST {endpoint}/v1/rerank`，模型 `Qwen3-Reranker-4B-mxfp8`，对 top-k 候选重排；若 oMLX 未暴露 `/v1/rerank`，降级进程内 `bge-reranker-v2-m3`。

### 提示词职责（一律强制中文输出）
| 任务 | 契约 | 输出 |
|---|---|---|
| `summarize` | 中心论点 + 3–5 要点，不加观点；给置信度 | JSON `{"summary_zh": ..., "key_points":[...], "confidence": 0.0-1.0}`（`confidence` 入 `summaries.confidence`，DESIGN §5.1） |
| `translate` | 忠实译成简体中文，保留技术术语 | 纯文本 |
| `extract_entities` | 抽取本文命名实体及关系；**实体 surface 必须是原文子串/近似 span**（grounding，幻觉实体降置信或丢弃）；跨语言实体保留原文 surface + 中文规范化名 | JSON `{"entities":[{"name, surface, type, aliases, description, canonical_name_zh"}], "relations":[{"subject, predicate, object"}]}`（DESIGN §4.5/§7） |
| `classify_topics` | 给定主题+关键词，打分 0–1 | JSON `{"scores":{topic_id:0.87}}` |
| `generate_wiki_entry` | 中性中文百科式词条（文章+实体关系） | Markdown |
| `generate_report` | 按期聚合主题精华 | Markdown |

### 模型建议
- **生成（统一走 oMLX）**：默认 `Qwen3.8-27B-MLX-4bit`（实测可用，质量更佳，json_mode 正常）承担全部生成任务；备选轻量 `Qwen3.5-9B-…-INSTRUCT-…-MLX-mxfp8`；`9B THINKING` 变体 oMLX 加载失败（Missing 154 parameters）待修复。27B 长文约 20–60s，并发=1 后台处理可接受；各任务同用一模型避免多模型抢占内存；分任务 A/B 留 P3。
- **嵌入**：oMLX `Qwen3-Embedding-8B-4bit-DWQ`（本地、无需另起服务）。**无降级链**：fastembed 模型维度（512/1024）≠ `vector(1536)`，向量空间也不同，混存互相检索失效 → 直接走「语义通道关闭、仅关键词」（DESIGN §4.3）
- **重排（P2）**：oMLX `Qwen3-Reranker-4B-mxfp8`。

### 结构化输出健壮性
`json_mode`（支持处开启）→ 解析失败用 `structured.parse_with_repair`（去代码围栏、找首个平衡 `{}`、带错误重问一次）→ 仍失败则标记 `low_confidence`，文章仍可用。**部分失败不阻塞**：每个任务独立 job、独立缓存；`extract_entities` 失败但摘要成功 → 文章 `done` + 部分 Wiki 页，可对单个任务重试（Phase 1 CLI / Phase 2 详情页）。

---

## 9. Web Dashboard 页面

Jinja2 服务端渲染 + HTMX 动态 + ECharts（本地 vendored JS，离线可用）。

| 路由 | 页面 | 内容 |
|---|---|---|
| `GET /` | 概览 | 流水线统计（总数/今日）、待处理队列、LLM 健康横幅、最近文章、源健康 |
| `GET/POST /feeds` | Feed 管理 | 增删改/禁用，按类型填字段，立即抓取，错误历史 |
| `GET /articles?feed=&topic=&status=&q=` | 文章列表 | 筛选表格 + FTS 搜索 + 分页 |
| `GET /articles/{id}` | 文章详情 | Tab：原文 / 中文摘要 / 全文翻译 / 实体关系 / 相关话题 / Wiki 词条；状态+重试 |
| `GET /wiki` | Wiki 浏览 | 按 kind 分组的词条索引 + 搜索 |
| `GET /wiki/{slug}` | Wiki 词条页 | 渲染 Markdown，交叉链接实体/话题/相关文章 |
| `GET /graph` | 知识图谱 | ECharts 力导向图，按实体类型/主题/时间筛选，点节点回看相关文章 |
| `GET/POST /topics` | 主题管理 | 主题 CRUD、关键词编辑、跨源聚合表（主题×源×文章数） |
| `GET /reports` | 报告 | 日报/周报列表 + 渲染，导出 Markdown/HTML |
| `GET/POST /settings` | 设置 | LLM 后端+分任务模型、并发、健康测试（流式）、定时时间 |
| `GET /search?q=` | 搜索结果 | 混合检索（关键词+语义），文章+Wiki 命中与摘要片段，可切换纯语义模式 |

---

## 10. 定时任务与报告

APScheduler：每日（默认 08:00）日报、每周（默认周一 08:00）周报，另含定时抓取与排空队列。报告存 `reports` 表。

- **日报**：今日新增 N 篇（分源）、Top 5 摘要精华、新实体/新关系、队列与失败任务、源健康、LLM 状态与 token 估算
- **周报**：主题热度排行（跨源聚合，核心价值点）、每主题摘要精华（LLM 综合）、图谱增长（节点/边新增、Top 新实体）、每源统计表、存储与归档建议
- 报告由「结构化统计 JSON → LLM 综合为 Markdown」，非原文堆砌

---

## 11. 配置（DX）

`config/config.yaml`（默认 oMLX）：**结构性内容只在 DESIGN §9 维护**，此处仅列关键项示意——

```yaml
data_dir: ./data
db: { dsn: postgresql+asyncpg://tc:tc@localhost:5433/topic_collection, pool_size: 5, vector_dim: 1536 }
llm: { backend: omlx, endpoint: http://localhost:8000, model: Qwen3.8-27B-MLX-4bit }
# 完整 schema 见 DESIGN §9（含 llm.max_timeout_retries / ingestion.global_concurrency /
# per_host_interval_ms / dedup.* / topics.reclassify_recent_days 等，不再在此副本维护）
ingestion: { fetch_interval_hours: 6, max_items_per_fetch: 50 }
schedule: { daily_report: "08:00", weekly_report: "Mon 08:00" }
# 订阅源不写在主配置 → 独立文件 config/feeds.yaml（加订阅只改那一个文件）
```

**订阅源配置独立**：`config/feeds.yaml` 为订阅源清单（type/url/enabled/可选 config），新增源只需在文件里加一项 → `tc feeds import`（或启动自动同步，幂等 upsert 进 DB `feeds` 表）→ `tc fetch` 抓取。schema 与同步机制见 DESIGN §9。
- 开发环境（Docker，已确认）：`docker compose up -d` 一键起 pgvector（`pgvector/pgvector:pg17` 镜像，配置见 DESIGN §5.4）；首次运行 `scripts/init_db` 建库建扩展
- 环境变量覆盖：`TC_LLM_BACKEND=omlx`、`TC_DB_DSN=postgresql+asyncpg://...` 等（pydantic-settings；**凭据一律走环境变量，不入库不入 repo**）
- DX：`uv run` / `make dev` 启动（lifespan 拉起 DB init + scheduler + worker）；`scripts/` 放 init_db / import_feeds / backfill（嵌入模型切换后全量重嵌，DESIGN §5.2） / **backup**（`tc backup` 调用，§4 F11/DESIGN §10）；`--check-llm` 验证后端与模型（含 per-task 覆盖模型，DESIGN §4.4）

---

## 12. 分阶段路线图

| 阶段 | 范围 | 覆盖需求 |
|---|---|---|
| **Phase 1 MVP（可用即可，无 WebUI）** | 建库（Postgres + pgvector + tsvector）；RSS 抓取（ETag/去重）；清洗；本地 LLM（oMLX）+ `summarize`(中文) + `embed_core`/`embed_summary`(1536维) + `classify_topics`（关键词+LLM）；基础 Wiki 词条 + 混合检索；**CLI 入口**（feeds import / topic add / topic list / fetch / summarize / list / search / article）；定时抓取+流水线；config（config.yaml + feeds.yaml）+ `docker-compose.yml`(pgvector) | RSS(1a)、LLM Wiki 基础(3)、本地 LLM oMLX(5a)、中文输出(6)、语义检索(pgvector 基础)、定时抓取(2b 部分)、CLI 可用入口(2c 提前) |
| **Phase 1+（CLI 增强，MVP 用后改进）** | 外部 LLM API 切换（OpenAI 兼容协议，per-capability：generate 可选本地/外部，embed/rerank 强制本地）；`tc feeds fetch --count N` 限制单次抓取条数；`_classify_http_error` 重试分类修复（401/403/400 → PermanentError） | 外部 LLM API(F5a 增强)、CLI 体验优化 |
| **Phase 2** | **WebUI Dashboard**（概览/Feeds/文章/详情/搜索/设置/图谱/报告页）；混合检索完善（RRF + oMLX Reranker、相似文章推荐）；中文翻译；实体关系抽取→entities/relations 表+合并；图谱页（ECharts）；主题聚合 UI+跨源视图；完整 Wiki（主题/实体词条+交叉链接）；日报/周报；API 连接器骨架 | Web+Dashboard(2a)、报告(2b)、知识图谱(4)、API/爬虫骨架(1b 部分)、主题聚合(1c)、Reranker 增强(3)、混合检索高级(3) |
| **Phase 3** | API 连接器广度（arXiv/GitHub/通用 OpenAPI）+ 健壮爬虫（readability、反爬礼仪、增量抓取）；高级搜索（过滤/保存搜索/实体搜索）；告警（主题命中/Feed 故障/LLM 掉线）；分任务多模型 A/B；存储归档裁剪 | API/爬虫广度(1b)、高级搜索(3)、告警(12) |

---

## 13. 非功能需求

- **性能**：单机数万篇文章量级流畅；LLM 并发=1 后台处理不阻塞 UI；Dashboard 秒级响应；**CPU 密集任务（jieba 切词、HTML→Markdown、trafilatura 解析）一律走 `asyncio.to_thread`，不阻塞 async 事件循环**（DESIGN §2）；抓取设全局并发 semaphore（`ingestion.global_concurrency`）+ 每域限速（`ingestion.per_host_interval_ms`，DESIGN §6）；向量检索走 HNSW，**建索引期 `ef_construction=128`（DDL WITH）、查询期 `SET hnsw.ef_search=64`**——二者不可混（DESIGN §5.1），目标 P95 < 100ms
- **运行时依赖**：Docker Compose 起 PostgreSQL 15+（`pgvector/pgvector:pg17` 镜像，已确认）；DB 仅本机回环访问（`127.0.0.1`），凭据由配置/环境变量管理
- **可靠性**：源失败自动禁用+审计；LLM 掉线优雅降级（文章仍可浏览原文）；任务可重试/恢复/死信；增量处理幂等
- **隐私/安全**：全部本地运行，无数据出机；Dashboard 默认绑定 `127.0.0.1`；不保存任何云凭据
- **可维护性**：Services 层为应用 API，CLI/Web 均薄封装；Alembic 迁移；Rich 滚动日志；配置类型化校验
- **可测试性**：LLM 可 mock；单元（dedup/cleaner/structured/FTS）+ 集成（fake LLM）

---

## 14. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 本地 LLM 中文质量 | 摘要/实体偏弱 | 首选 Qwen3.8-27B（实测质量更佳）；提示词带示例；置信度评分；手动「重新生成」+ 人工编辑词条兜底 |
| LLM 速度（27B） | 流水线积压 | 并发=1 后台 worker；仅增量处理；预热模型（备选 9B INSTRUCT 更轻量可降级）；**重试按错误类拆**——瞬时错误（连接拒绝/5xx/超时，含整段掉线）`attempt` 不自增、无限续跑、退避封顶 15m、不进死信；永久错误（**401/403/400** 鉴权配置错 / JSON 解析失败 / 不可解析）`attempt` 自增、`max_attempts=3` 死信（DESIGN §6/§11） |
| LLM 掉线积压 | 期间任务积压、恢复前不消费 | 见上一行：瞬时类不自增 attempt、不进死信，恢复后自动续跑（§15 #7）；worker 领取门控掉线期间不领新 job |
| 高量 feed 首抓积压 | 并发=1 下千条积压数小时，fetch_interval 越积越多 | `max_items_per_fetch` 截断 LLM 入队（文章全量入库）+ 水位告警记 fetch_events；**drain_queue 每 30s 扫 pending 文章补入队**（Phase 1 单进程随 scheduler 在场，DESIGN §6/§10/§11） |
| 生成/嵌入并发 | 全局=1 让便宜 embed 被 27B 阻塞，语义索引吞吐受限 | P1 暂按 1；待实测 oMLX 同时常驻 27B+8B 可行则升 gen/embed 分槽（B1，DESIGN §4.4/§16） |
| LLM JSON 漂移 | 三元组损坏 | json_mode/schema + 修复解析 + 重问一次；低置信度也入库并在 UI 标注 |
| 中文检索召回 | 搜不到 | jieba 预切词写入 `tsvector('simple')`；必要时补充同义词表；混合检索兜底 |
| 反爬（P3） | 抓取失败/封禁 | 礼貌 UA + robots 检查 + 每域限速抖动 + 退避重试；优先 RSS/API |
| Feed 不稳定 | 采集噪音 | ETag/304、失败计数自动禁用、审计事件、UI 状态徽标 |
| 存储增长 | DB 变慢 | 原始 HTML 仅按需存 `article_versions`；按策略归档裁剪（P3） |
| Postgres 运维依赖 | 环境难跑/版本不一 | `docker-compose` 一键起 pgvector 镜像；锁定 PG15+；启动时校验 `vector` 扩展并给出可执行提示 |
| pgvector 维度/索引失配 | 检索慢或报错 | 启动校验 embedding 维度与 `vector_dim`；HNSW 用 `vector_cosine_ops` 并文档化；切换模型后重建索引 |
| 向量存储增长 | 磁盘/入库变慢 | 主存 title+summary、正文截断 8K；按策略裁剪归档（P3）；周期性 `REINDEX` |
| 凭据泄露（若开鉴权） | token 写进 repo/DB | 默认不鉴权无需 token；若开启则 `api_key_env` 只存环境变量名，config.yaml 不含真实值 |
| 嵌入/重排端点不可用 | 语义检索不可用 | oMLX `/v1/embeddings` 不可用 → **语义通道关闭、仅关键词**（Dashboard 提示）；嵌入无进程内降级（维度/向量空间不匹配，§8/DESIGN §4.3）；`/v1/rerank` 不可用 → 降级进程内 `bge-reranker-v2-m3` → 保持 RRF 融合（P2） |
| oMLX 掉线 | 流水线停摆 | 网络错/5xx/超时（瞬时）→ 保持 queued + `lock_until` 退避重试、**`attempt` 不自增、不进死信、退避封顶 15m**、恢复后自动续跑；401/403/JSON/不可解析（永久）`attempt` 自增、3 次死信可手动重试（§14/§15 #7）；Dashboard 健康横幅；配置一键切回 Ollama 备选 |

---

## 15. 成功指标（验收）

> **Phase 1（CLI 验证，无 WebUI）**：1 / 3 / 5 / 7 / 8 / 9 / 16
> **Phase 1+（CLI 增强）**：17 / 18
> **Phase 2（WebUI 上线后）**：2 / 4 / 6
> **Phase 3（进阶能力）**：10 / 11 / 12 / 13 / 14 / 15

1. 添加一个 RSS 源后，能自动抓取 → 生成中文摘要 → 文章列表可检索
2. 非中文文章可一键翻译为简体中文（Phase 2）
3. 定义一个主题（名称+关键词）后，可按主题过滤的多源文章列表可查（Phase 1 CLI `tc list --topic <name>`）；**列表标注 method 来源（keyword/llm）**；关键词命中文章整体跳过 LLM 分类为已知召回取舍，P3 `tc reclassify` 兜底全量重跑（DESIGN §6/§16）；主题热度排行视图归 Phase 2
4. 知识图谱页可看到实体节点与关系边，点击节点跳回相关文章（Phase 2）
5. Wiki 可按关键词全文搜索（含中文关键词），且每篇新文章都已自动生成 Wiki 词条页；按主题/实体浏览的完整 Wiki 归 Phase 2
6. 日报/周报能按计划生成并在 Dashboard 查看/导出（Phase 2）
7. 拔掉 LLM 服务后，系统不崩溃：文章可浏览、**瞬时类任务保持 queued 退避、无限续跑至恢复（`attempt` 不自增、不因掉线进死信，退避封顶 15m）**，恢复后自动续跑；永久类任务（401/403 鉴权配置错 / 不可解析 / JSON 失败）3 次后死信可手动重试（DESIGN §6/§11）
8. 全程本地运行，无任何数据发送到云端
9. 语义检索生效：用与原文字面不同但语义相近的查询词，仍能召回相关文章（pgvector）；混合检索 **P1 即用 RRF 融合**（量纲无关），P2 叠 Reranker（§5/DESIGN §7）
10. 配置化 API 连接器（Phase 3）：不改代码，仅通过 `feeds.yaml` 配置（endpoint + 参数 + JSONPath 映射）即可接入 arXiv / GitHub / 通用 OpenAPI 等源并进入流水线
11. 健壮网页抓取（Phase 3）：对一个无 RSS 的网站配置 URL 即能礼貌抓取并提取主内容（readability 类）；被限流/反爬时按域退避重试、不触发封禁；增量抓取只处理变更页面
12. 高级搜索（Phase 3）：支持组合过滤（时间范围 / 数据源 / 主题 / 实体类型）+ 保存常用搜索；实体搜索——从图谱或实体页点实体，跳转列出其相关文章
13. 告警（Phase 3）：主题命中新文章、Feed 连续失败、LLM 掉线三类事件，按配置触发通知（桌面 / 邮件）
14. 分任务多模型 A/B（Phase 3）：同一任务（如 `summarize`）可配置多模型并行产出并对比（如 27B vs 9B），结果可标注来源与手动切换
15. 存储归档（Phase 3）：按策略（时间 / 体积 / 状态）归档并裁剪旧文章、原始 HTML 与向量，归档后 DB 体积下降且检索仍可用
16. 跨源同一事件转载/改写经**向量近似去重**合并（`dedupe_of`，多跳扁平化到终极 winner），主题视图与日报不重复占位（Phase 1；嵌入建好后生效，body↔body 同粒度匹配，DESIGN §6）
17. 外部 LLM API 可选切换：config 配置 `llm.generate.backend: openai` + 环境变量设置 API key 后，summarize 走外部 API 完成；embed 仍走本地 oMLX；API key 缺失时 worker fail fast 不入队（Phase 1+）
18. `tc feeds fetch --count N` 限制单次抓取条数：从 feed 第一条起按顺序取 N 条入库，超限截断并记录 fetch_events（Phase 1+）

---

## 16. 项目结构（新建）

> **文件级目录结构以 [DESIGN.md §3](DESIGN.md) 为唯一权威**，此处仅列顶层概览，避免两份文档漂移。

```
topic_collection/
├── pyproject.toml · README.md · docker-compose.yml   # docker-compose: dev 起 pgvector
├── docs/{PRD.md, DESIGN.md}
├── config/{config.yaml, feeds.yaml}
├── app/            # main / config / db / ingest / llm / services / pipeline / scheduler
│                   # Phase 2 追加 api/（Web 路由）+ web/（templates + static）
├── data/  logs/  scripts/  tests/
```

技术栈见 [DESIGN §2](DESIGN.md)（此处不重复维护）。Phase 1 = CLI（typer）入口，无 WebUI。
