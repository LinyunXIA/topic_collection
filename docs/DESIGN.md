# 技术设计文档 — Topic Collection

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述（目录结构 / DDL / 接口）只在一处维护、另一处引用，避免漂移
> 版本：v0.10 · 2026-08-19 · 切片一完成（与 PRD v0.9 同步）
> v0.10：**切片一+切片二完成**——**语言检测 pycld3→lingua-language-detector**（§2，pycld3 需 protobuf 编译器无法在 3.14 安装，lingua 纯 Python、75 语言支持）；**Docker 端口 5432→5433**（§5.4/§9，宿主机 5433 避免与本地 PG 冲突）；§14 切片一 1.1-1.9 + 切片二 2.1-2.4 + Day 1 全部完成；**真实环境验收**：20 篇 HN 文章端到端跑通（20/20 summary + 40+ embedding）；**60/60 pytest 全部通过**；切片二新增混合检索 `search(q)`（语义 top-k ∪ 关键词 top-k → RRF 融合，§7）+ CLI `--mode hybrid|semantic|keyword`
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

### 4.3 降级链路

| 能力 | 主（oMLX） | 降级 |
|---|---|---|
| 生成 | `/v1/chat/completions` | Ollama（切换 backend） |
| 嵌入 | `/v1/embeddings` + `Qwen3-Embedding-8B` | **无进程内降级**——`bge-small-zh`(512d) / `bge-m3`(1024d) 维度不匹配 `vector(1536)` 且向量空间不同，混存会互相检索失效。oMLX 不可用 → 语义通道关闭、仅关键词（Dashboard 提示，§7/§11） |
| 重排 | `/v1/rerank` + `Qwen3-Reranker-4B`（P2） | 进程内 `bge-reranker-v2-m3` → 不重排（保持 RRF 融合，§7） |

### 4.4 `LLMClient` 门面

并发信号量（默认 1）、每调用超时、指数退避重试（5xx/超时/连接拒绝）、`healthy` 标志与**单次健康探测**。重试/超时只在此层处理，services 不碰传输。**两层重试分工**：客户端=秒级抖动重试（单次调用内）；job 级 `lock_until` 退避（§6）=分钟级长中断（oMLX 整体不可用），互不冲突。**错误分类**：401/403/400 是永久/配置错误（鉴权失败、请求格式错），**不走指数退避**、直接抛永久类由 job 层按 `max_attempts` 死信；只 5xx/超时/连接拒绝归瞬时、走退避。**并发=1 是待验证假设**：oMLX 按请求切换模型会抖动加载是真，但 MLX 可在统一内存同时常驻多模型（27B-4bit ≈14GB + 8B 嵌入 ≈5GB，64GB+ Mac 装得下，无需切换、无抖动）——若实测同时常驻可行，信号量改 per-capability 一槽（gen 一个、embed 一个），embed 不被 27B 的 20–60s 阻塞、语义索引吞吐翻倍。P1 先按 1，§16 记为已知限制。

**`healthy` 标志归属**：是 `LLMClient` 实例的**进程内**内存状态。**Phase 1 单进程**（§6 运维模式：worker + scheduler 同 asyncio 循环）下 worker 与 scheduler 共享同一个 `LLMClient`，`healthy` 全局可见，无需跨进程同步——简化为：
- **scheduler**：跑定时 healthcheck 任务（§10，每 5m `GET /v1/models`）更新 `LLMClient.healthy` 与 Dashboard 横幅
- **worker**：作为 oMLX 的唯一消费者，**仍自带自探测兜底**——领取空手且 `lock_until` 都未到期时、或连续 N 次 LLM 调用失败时，发一次 `GET /v1/models`（或 `POST /v1/embeddings` 探活）刷新 `healthy`、决定 sleep 退避时长；不盲信 scheduler 5m 一次的快照（掉线可能在两次探测之间发生）
- **CLI**：短命进程，不持有常驻 `LLMClient`；`tc status` 调用时即时探测一次报告健康，不与常驻进程共享状态（CLI 命令本身不走 worker，§6 运维模式）

**`--check-llm` 启动校验覆盖全部配置模型**：不只查主 `llm.model`，还对 `llm.models` 里每个 per-task 覆盖（summarize/translate/entities/topics/wiki/report）+ `embed.model` + `rerank.model` 逐个 `GET /v1/models` 比对——拼错的覆盖模型名只会在该 job 运行时 404，启动期就暴露能省一整轮退避排查。

### 4.5 提示词契约（一律中文输出）

| 任务 | 输出 |
|---|---|
| `summarize` | JSON `{"summary_zh", "key_points":[], "confidence":0.0-1.0}`（3–5 要点；`confidence` 入 `summaries.confidence`，§5.1） |
| `translate` | 简体中文纯文本 |
| `extract_entities` | JSON `{"entities":[{name, surface, type, aliases, description, canonical_name_zh}], "relations":[{subject, predicate, object}]}`。**grounding**：`surface` 必须是原文子串/近似 span，校验不过则降 `confidence` 或丢弃，防 LLM 幻觉实体污染图谱；**跨语言归一**：保留原文 `surface` + 中文 `canonical_name_zh`，`aliases_json` 收别名互链，避免 "OpenAI"/"开放AI" 在图谱分裂成方言岛（§5.1/§7） |
| `classify_topics` | JSON `{"scores":{topic_id:0.87}}` |
| `generate_wiki_entry` | 中文 Markdown 词条 |
| `generate_report` | 中文 Markdown |

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
  - `pending → processing`：worker pick-and-claim 首个该文章 job 时同事务 `UPDATE articles SET status='processing' WHERE id=$1 AND status='pending'`（status 守卫：processing 不会被重复推进，job 锁就够，但显式 status 守卫便于测试断言）
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
| `wiki` | 4 | 摘要落地后（实体 P2） | Qwen3.8-27B |
| `embed_summary` | 6 | `summarize` 成功后（summary）**或手动 `tc retry summarize`**——**必须走同一条钩子 `complete_summarize()`，不能只有自动流水线触发**（否则手动重生成后 summary 向量停在旧版本） | Qwen3-Embedding-8B |
| `translate` | 低 | lang≠zh 且用户触发 | Qwen3.8-27B |

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
- lifespan 启动**单个 asyncio worker task**：`循环 { 领取(SKIP LOCKED) → 无任务 sleep ~1s → 处理完继续 }`；领取与处理都在 await 点让出事件循环，不阻塞 fetch / HTTP
- 入队到开始 ≤ 当前在飞任务时长 + ~1s（并发=1 下在飞任务即 LLM 调用时长）
- **scheduler 的 drain_queue 不参与领取**（避免双领取者歧义），只做维护，见 §10
- LLM 掉线期间所有 queued 带未来 lock_until → 领取空手返回后 worker **自探测 oMLX**（`GET /v1/models` 一次，§4.4）决定 sleep 退避时长，不空转打 oMLX；Phase 1 单进程下 `LLMClient.healthy` 与 scheduler 共享，但 worker 仍以自探测为准（不盲信 scheduler 5m 快照）
- **领取门控（可选，推荐 Phase 1 开）**：领取前先自探测，不 healthy 则直接 sleep 退避、**不领新 job**——避免掉线期间新 job（`lock_until NULL`，本会被立刻领取）被领走后立刻失败回滚（瞬时虽不自增 attempt、不进死信，但每个被领走又失败的 job 都会带上未来 `lock_until` 退避，等于把一堆本可立即排队的新 job 提前推到退避队列、拉长恢复后的消费时延）。配合下一节「瞬时错误不自增 attempt、不进死信」双保险，保 PRD §15 #7「恢复后自动续跑」

**状态机原子性（lock_until 租约模型 + 事务合并）**：
- **领取 → 持租约**：见上一段 SQL，pick-and-claim 单条原子同事务；`lock_until` 既是 queued 退避门控、也是 running 存活凭证，**语义统一**
- **续租（随处理协程，不另起 watchdog）**：长 LLM 任务（>3 分钟）处理中定期 `UPDATE lock_until=now()+INTERVAL '5 minutes' WHERE id=$1 AND status='running'`——**续租逻辑必须与 LLM 调用跑在同一个 asyncio task 内**（在 `await llm.generate()` 外层包一个续租循环，或用 `asyncio.wait_for` + 周期性 `UPDATE`）。**不要**另起独立 watchdog 协程去续租：那样当 LLM 调用 hang（httpx 不返回）时，watchdog 仍会持续续租、lease 永不过期，单 worker concurrency=1 下整条流水线**永久卡死**、`recover_interrupted()` 也救不回来（lease 一直在未来）。续租随处理协程则 hang 时停续租、lease 到期、下次启动 `recover_interrupted()` 回收。**`httpx` 调用必须带 `timeout=`**（GenerateRequest.timeout_s 默认 180s，§4.1）——这是防 hung 的第一道闸，续租随处理协程是第二道。并发=1 下 27B 长文 20–60s 实际不需要续租，但封装层统一处理以防未来 P2 切高并发或长任务
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
- [ ] 3.1 `app/services/topics.py` 完善：topic CRUD + `match_keywords()` 快路径 + `classify_topics` LLM 慢路径（合并规则见 §6）
- [ ] 3.2 `app/services/wiki.py`：文章词条生成（`related_json` = 同主题 article top-5，§6）
- [ ] 3.3 CLI 补：`topic add` / `topic list` / `list --topic`
- [ ] 3.4 验收：PRD §15 验收 3（主题跨源聚合）/ 5（Wiki 按**关键词**全文搜索，主题/实体浏览 P2）

**横切**：
- [ ] X.1 `app/scheduler.py`：fetch_all + drain_queue + pg_backup（**自动化补充，主触发是 `tc backup` CLI**，§10）+ cleanup_fetch_events（§10）
- [ ] X.2 测试：FakeLLM 集成用例（切片一就要有）+ 单元用例（dedup / cleaner / structured / fts / pipeline 并发）+ **重试分类用例（A1）** + **跨源近似去重用例（B4）**（§13）
- [ ] X.3 验收：对照 PRD §15 Phase 1 条目（1/3/5/7/8/9/16）走通

> **WebUI（`app/api` + `app/web`）整体移入 Phase 2。**

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
