# 技术设计文档 — Topic Collection

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述（目录结构 / DDL / 接口）只在一处维护、另一处引用，避免漂移
> 版本：v0.5 · 2026-08-18 · 随决策持续更新（与 PRD v0.4 同步）
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
  - 嵌入：`Qwen3-Embedding-8B-4bit-DWQ`（实测输出 1536 维）
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
│   ├── pipeline.py             # 队列 + worker + recover
│   ├── scheduler.py            # APScheduler 任务
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

并发信号量（默认 1）、每调用超时、指数退避重试（401/5xx/超时）、`healthy` 标志与**单次健康探测**。重试/超时只在此层处理，services 不碰传输。**两层重试分工**：客户端=秒级抖动重试（单次调用内）；job 级 `lock_until` 退避（§6）=分钟级长中断（oMLX 整体不可用），互不冲突。**并发=1 是待验证假设**：oMLX 按请求切换模型会抖动加载是真，但 MLX 可在统一内存同时常驻多模型（27B-4bit ≈14GB + 8B 嵌入 ≈5GB，64GB+ Mac 装得下，无需切换、无抖动）——若实测同时常驻可行，信号量改 per-capability 一槽（gen 一个、embed 一个），embed 不被 27B 的 20–60s 阻塞、语义索引吞吐翻倍。P1 先按 1，§16 记为已知限制。

**`healthy` 标志归属**：是 `LLMClient` 实例的**进程内**内存状态，**不跨进程共享**。Phase 1 worker/scheduler/CLI 三进程分离时，worker 看不到 scheduler 的探测结果。Phase 1 修复方案：
- **worker**：作为 oMLX 的唯一消费者，**自己探测**——领取空手且 `lock_until` 都未到期时、或连续 N 次 LLM 调用失败时，发一次 `GET /v1/models`（或 `POST /v1/embeddings` 探活），决定 sleep 退避时长
- **scheduler**：仅负责 Dashboard 横幅（§10），不参与门控决策
- Phase 2 单进程时回到原设计：scheduler 内定时探测 + `LLMClient.healthy` 全局可见

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
  USING hnsw (vector vector_cosine_ops)  -- 建议 ef_construction=128、ef_search=64 以达 P95<100ms（§13）；按量级与召回实测微调

CREATE TABLE processing_jobs (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  task TEXT NOT NULL
    CHECK (task IN ('summarize','translate','entities','topics','wiki','embed_core','embed_summary')),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','superseded')),
  content_hash TEXT,                 -- 入队时的文章内容版本
  attempt INT DEFAULT 0, max_attempts INT DEFAULT 3,
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
      - "127.0.0.1:5432:5432"          # 仅回环，不暴露外网
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
python -m scripts.init_db             # 建表 + CREATE EXTENSION vector（幂等）
docker compose down                   # 停库（数据保留在 pgdata 卷）
```
- DSN 与 §9 一致：`postgresql+asyncpg://tc:tc@localhost:5432/topic_collection`
- 扩展由 `scripts/init_db` 执行 `CREATE EXTENSION IF NOT EXISTS vector;`，`pgvector/pgvector:pg17` 镜像已内置该扩展，无需额外安装

---

## 6. 数据流水线 & 状态机

```
fetch → normalize → dedup(url_hash/content_hash) → clean → 入队 processing_jobs
       → LLM 各阶段(summarize/embed_core/embed_summary/topics/wiki) → 图谱/词条 → tsv/向量索引
       → (embed 建好后) 跨源向量近似去重：title+summary 向量对近 N 天文章 cosine ≥ ~0.92 → dedupe_of 合并 mention_count
```
**事务边界**：每篇文章的 `insert article → enqueue jobs` 在同一事务（崩溃不留孤儿文章）；supersede 旧 job 与新 job 入队同事务（见下）。

**文章状态机**：`pending → processing → done | unparseable | error`（部分任务失败仍可 `done`，详情页可重试单个任务）

**入队规则（按任务）**：
| 任务 | 优先级 | 触发 | 模型 |
|---|---|---|---|
| `embed_core` | 高 | 新文章（title+body） | Qwen3-Embedding-8B |
| `embed_summary` | 高 | `summarize` 成功后（summary）**或手动 `tc retry summarize`**——**必须走同一条钩子 `complete_summarize()`，不能只有自动流水线触发**（否则手动重生成后 summary 向量停在旧版本） | Qwen3-Embedding-8B |
| `summarize` | 高 | 新文章 | Qwen3.8-27B |
| `topics` | 中 | 新文章（未命中关键词）+ 主题变更（重算） | Qwen3.8-27B |
| `wiki` | 低 | 摘要落地后（实体 P2） | Qwen3.8-27B |
| `translate` | 低 | lang≠zh 且用户触发 | Qwen3.8-27B |

**backpressure**：单次 fetch 每个 feed 入队上限 `ingestion.max_items_per_fetch`（默认 50），超限截断并记 fetch_events 水位告警——并发=1 下千条 feed 首抓会积压数小时（27B 20–60s/篇），不限流会让 `fetch_interval_hours` 越积越多；分批回灌，P3 再做更精细的水位调度。

**Phase 1 wiki 词条 `related_json` 规范**：Phase 1 不抽实体（`entities` task 不入队），`related_json` = 同主题 article 列表（来自 `article_topics`，按 `score DESC, published_at DESC` 取前 5）；P2 实体抽取上线后，`related_json` 合并"同主题 + 共现实体"两组链接

**主题分类规则（关键词快路径 + LLM 慢路径，P1）**：
- **快路径（关键词预匹配）**：`match_keywords()` 对新文章 title+content 检查启用主题的关键词——命中即记 `article_topics(method='keyword')`，score 由命中强度计算（title 命中加权 + 命中词数），**命中即计入、不跑 LLM**（省调用）
- **慢路径（LLM 分类）**：**未命中任何关键词**的文章才进 `classify_topics` job——给定全部启用主题+关键词打分 0–1，`score ≥ 0.6`（可配 `topics.llm_threshold`）记 `method='llm'`
- **一致性**：`UNIQUE(article_id, topic_id)` 一篇文章对一主题仅一行；两路径按 (article, topic) **互斥**——关键词已命中的主题不再 LLM 复议（故不存在"关键词命中但 LLM 判低分"的冲突）；关键词命中的文章整体跳过 LLM 分类（P1 接受的召回取舍：不会跨主题发现未命中关键词的主题，P3 可补跑全量）
- **聚合排序**：`aggregate_topic()` 按 `score DESC, published_at DESC`；展示标注 method 来源（keyword/llm），可筛可解释
- **主题变更重算**：主题/关键词增改后重跑 `match_keywords()`——不再命中的旧 `method='keyword'` 行删除；未命中关键词的文章重新入队 `classify_topics`（幂等 + 活跃态唯一约束保护）

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
- 优先级数值约定：`embed_core=1`、`embed_summary=1`、`summarize=2`、`topics=3`、`wiki=4`、`translate=5`

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
  4. 入队 `classify_topics` job（关键词未命中的文章走 LLM 慢路径，§6）
- **调用方**：
  - **自动**：worker 处理 `summarize` 任务成功后调用
  - **手动**：`tc retry <article_id> summarize` 走同一条钩子（不能用 LLM 重新跑完后只 UPDATE summaries，否则 `embed_summary` 不会补入队 → summary 向量停在旧版本；F2 P0 必踩的坑）
- **抽象边界**：钩子只关心「summary 落库之后该发生什么」，不感知调用方是自动还是手动

**`complete_embed(article_id, kind, result)` 公共钩子（与 `complete_summarize` 对称，自动 + 手动重试都走）**：
- **职责**（同事务）：① `INSERT INTO article_embeddings ... ON CONFLICT (article_id, kind, model) DO UPDATE SET vector=..., content_hash=..., dim=...`（§5.1）；② `UPDATE processing_jobs SET status='succeeded', lock_until=NULL WHERE id=$1 AND status='running'`（带 running 守卫，§6 状态机原子性）
- **调用方**：worker 处理 `embed_core`/`embed_summary` 成功后调用；`tc retry <article_id> embed_core|embed_summary` 走同一钩子——否则手动重嵌只 upsert 向量而不推进 job 状态/不守卫 supersede，与 F2 P0 同类坑
- **kind 映射**：`embed_core` 写 `title`+`body` 两行、`embed_summary` 写 `summary` 一行（§5.2）；钩子按 job payload 的 kind 集合循环 upsert
- **维度校验**：result 向量维度 ≠ `db.vector_dim`(1536) → 阻断写入并告警（§4.2/§5.2，防 HNSW 失配）

**worker 领取（单条原子 pick-and-claim，FOR UPDATE SKIP LOCKED + UPDATE 同事务）**：
```sql
UPDATE processing_jobs
SET status='running', lock_until=now() + INTERVAL '5 minutes', attempt=attempt+1
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

**worker 运行模型（常驻自驱，非心跳驱动）**：
- lifespan 启动**单个 asyncio worker task**：`循环 { 领取(SKIP LOCKED) → 无任务 sleep ~1s → 处理完继续 }`；领取与处理都在 await 点让出事件循环，不阻塞 fetch / HTTP
- 入队到开始 ≤ 当前在飞任务时长 + ~1s（并发=1 下在飞任务即 LLM 调用时长）
- **scheduler 的 drain_queue 不参与领取**（避免双领取者歧义），只做维护，见 §10
- LLM 掉线期间所有 queued 带未来 lock_until → 领取空手返回后 worker **自探测 oMLX**（`GET /v1/models` 一次，§4.4）决定 sleep 退避时长，不空转打 oMLX；Phase 1 三进程分离时 `LLMClient.healthy` 不跨进程共享，必须本地探测
- **领取门控（可选，推荐 Phase 1 开）**：领取前先自探测，不 healthy 则直接 sleep 退避、**不领新 job**——避免掉线期间新 job（`lock_until NULL`，本会被立刻领取）被领走消耗 attempt、3 次后进死信。配合下一节「瞬时错误不进死信」双保险，保 PRD §15 #7「恢复后自动续跑」

**状态机原子性（lock_until 租约模型 + 事务合并）**：
- **领取 → 持租约**：见上一段 SQL，pick-and-claim 单条原子同事务；`lock_until` 既是 queued 退避门控、也是 running 存活凭证，**语义统一**
- **续租**：长 LLM 任务（>3 分钟）处理中定期 `UPDATE lock_until=now()+INTERVAL '5 minutes' WHERE id=$1 AND status='running'`；并发=1 下 27B 长文 20–60s 实际不需要续租，但封装层统一处理以防未来 P2 切高并发或长任务
- **完成（产物落库 + 状态推进同事务 + running 守卫）**：
  ```sql
  BEGIN;
  -- 1) 产物 upsert（summaries / entities / wiki_pages 等）—— 与状态推进原子
  INSERT INTO summaries (...) VALUES (...) ON CONFLICT (article_id, lang, model) DO UPDATE SET ...;
  -- 2) tsv 刷新（§6 关键词通道补全）
  UPDATE articles SET tsv=to_tsvector('simple', ...) WHERE id=$1;
  -- 3) 状态推进（带 WHERE status='running' 守卫）
  UPDATE processing_jobs SET status='succeeded', lock_until=NULL
  WHERE id=$1 AND status='running';
  COMMIT;
  ```
  守卫意义：job 被 supersede 后，旧 LLM 结果若还先落库一瞬，单 worker 下最终会被新结果覆盖，但**测试与排障都因此变得很难**；事务合并后连这个窗口都没有
- **失败**：`UPDATE processing_jobs SET status='queued', lock_until=now()+INTERVAL '<退避时长>', attempt=attempt+1, error=$2 WHERE id=$1 AND status='running'`，到点自动被 SKIP LOCKED 领取
- **进程中断**：崩溃/杀进程时 `status='running'` 且 `lock_until` 留在未来 —— **租约过期才算真死**
- **recover（租约回收）**：`UPDATE processing_jobs SET status='queued', lock_until=NULL, error=COALESCE(error,'')||'[recovered]' WHERE status='running' AND lock_until < now()` —— **谁跑都安全**（多 worker / scheduler 启动时跑也只会回收已过期的行，不动活任务；Phase 1 三进程分离不再有误伤风险）
- **归属**：**仅 worker 启动时跑** `recover_interrupted()`（scheduler 不跑，避免双领取者歧义）；Phase 1 运维模式下 worker 是唯一常驻消费者（§6 运维模式 / §10）
- 启动顺序：init_db → 探测 oMLX → `recover_interrupted()` → 启动 worker（见 §8 / §10）；**`recover_interrupted()` 在 worker 启动时跑**（§6 recover 归属）

**运维模式（Phase 1 vs Phase 2）**：
- **Phase 2（WebUI 上线后）**：FastAPI lifespan 在 `app/main.py:create_app()` 启动顺序 = init_db（校验 vector 扩展/维度）→ 探测 oMLX 三端点 → `recover_interrupted()` → 启动 scheduler + worker task（同一进程）
- **Phase 1（无 WebUI，CLI 入口）**：worker 单独常驻，通过 `python -m app.worker`（或 `make worker`）启动；scheduler 同样独立 `python -m app.scheduler`。CLI 命令（`tc fetch` / `tc summarize` / `tc search` ...）走 services 层但不启动 worker——入队后必须有 worker 在跑才能真正消费。开发期推荐两个终端：`make worker` + `tc fetch` / `tc search` 等

**去重**：URL hash 相同 → 复用旧文章，`mention_count+1`；URL 不同但 content_hash 相同 → 记 `dedupe_of`。**跨源近似去重（嵌入建好后）**：同事件多源转载/改写 URL 与 content_hash 都不同，但语义近似——新文章 embed 落库后，用其 title+summary 向量对近 N 天文章做 `ORDER BY vector <=> $1 LIMIT k`，cosine ≥ ~0.92 则判为同事件，记 `dedupe_of` 合并 mention_count，主题视图/日报不重复占位（PRD §15 #16）。**去重在 LLM 花钱前完成**（精确去重）；近似去重在 embed 后、summarize 前若已命中可跳过该文 LLM。

**重试/降级矩阵**：
| 失败 | 处理 |
|---|---|
| 抓取网络错误 | 记录 fetch_events，下次周期再试；连续 `ingestion.feed_disable_after`（默认 5）次自动禁用 feed；陈旧 `fetch_events` 按 `fetch_events_retention_days`（默认 90 天）定期清理 |
| 文章不可解析 | status=unparseable，保留原文，跳过 LLM |
| LLM 401/5xx/超时（瞬时） | job 保持 `queued`，`lock_until` = 退避 1m→5m→**15m 封顶**，**无限重试不进死信**——掉线是基础设施问题不是内容问题，attempt 预算不消耗；worker 领取门控（§6）避免掉线期间领新 job；到点自动被 SKIP LOCKED 领取续跑 |
| LLM JSON 解析失败 / 内容不可解析（永久） | `max_attempts=3` 后 `failed` 死信，记 error；文章详情可手动 `tc retry`（走 `complete_*` 钩子） |
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
  dsn: postgresql+asyncpg://tc:tc@localhost:5432/topic_collection   # 见 §5.4 docker-compose
  pool_size: 5
  vector_dim: 1536            # 实测 Qwen3-Embedding-8B 输出维度
web: { host: 127.0.0.1, port: 7111 }   # 必须 ≠ oMLX 端口 (8000)，避免与本地 LLM 端口冲突
llm:
  backend: omlx               # omlx | ollama
  endpoint: http://localhost:8000   # oMLX OpenAI 兼容端点（实测）
  # 鉴权：本机不鉴权（已确认），不发 Authorization 头；如需鉴权再设 api_key_env
  model: Qwen3.8-27B-MLX-4bit   # 实测可用（质量更佳）
  # 备选：Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED-MLX-mxfp8（更轻量）
  # THINKING 变体（Qwen3.5-9B-…-THINKING-HERETIC-UNCENSORED）oMLX 加载失败，修复后可用
  max_concurrency: 1            # 默认 1；待实测 oMLX 同时常驻 27B+8B 嵌入可行则升 2（gen/embed 分槽，§4.4/§16）
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
  max_items_per_fetch: 50         # 单次 fetch per-feed 入队上限（backpressure，§6/§11）
  fetch_events_retention_days: 90 # fetch_events 审计表保留天数（cleanup_fetch_events 日任务清理，§10）
topics: { llm_threshold: 0.6 }   # classify_topics LLM 打分阈值（关键词快路径不经过此值）
schedule: { daily_report: "08:00", weekly_report: "Mon 08:00" }
```

环境变量覆盖：`TC_LLM_BACKEND` / `TC_DB_DSN` / `TC_WEB_PORT`（pydantic-settings）；`TC_LLM_API_KEY` 仅当开启鉴权时使用。

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
| **pg_backup** | **每日 03:00** | **`pg_dump` 压缩到 `data/backups/tc-YYYYMMDD.sql.gz`，保留 14 天**——个人知识沉淀库数据比代码值钱，pgdata 卷不是备份（§14 Day 1）。**主触发 = `tc backup` CLI**（PRD §4 F11），scheduler 此项为可选自动化；Phase 1 不依赖 scheduler 常驻，用户须定期手动 `tc backup` 或常驻 scheduler |
| **cleanup_fetch_events** | **每日 04:00** | 清理 `fetch_events` 中超过 `fetch_events_retention_days`（默认 90 天）的行（drain_queue 30s 太频不适合做清理，独立日任务） |
| daily_report | 每日 08:00 | 日报（P2） |
| weekly_report | 周一 08:00 | 周报（P2） |
| healthcheck | 每 5m | LLM 健康探测，**仅 scheduler** 更新 Dashboard 横幅；worker 自探测见 §4.4 / §6 |

---

## 11. 错误处理与降级总表

| 场景 | 行为 | UI 呈现 |
|---|---|---|
| oMLX 全挂 | **瞬时类任务**（生成/嵌入）保持 queued + lock_until 退避 1m→5m→15m 封顶、**无限续跑不进死信**；worker 领取门控（§6）掉线期间不领新 job；文章可浏览原文 | 概览红色横幅「LLM 离线」（scheduler 探测驱动） |
| oMLX 瞬时错误但任务已领取 | 不消耗 attempt 预算（瞬时类无 max_attempts）；到点自动续跑 | 任务详情显示退避倒计时 |
| 永久错误（JSON/不可解析） | `max_attempts=3` 后 `failed` 死信 | 文章详情可手动重试 |
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
- **db**：pytest + 临时 Postgres（docker compose 测试库）；向量维度校验用例
- **降级**：mock `/v1/embeddings` 404 → 断言语义通道降级
- **结构化日志（D4）**：job 级日志规约 `job_id/task/attempt/latency_ms/error_class`，卡住的 running job 可凭日志定位（`status` 命令的排障底座）

---

## 14. Phase 1 MVP 任务清单（可用即可，无 WebUI，CLI 为入口）

**Day 1 必备（不属任何切片，立即做）**：
- [ ] 0. **pg_dump 备份脚本** `scripts/backup.sh`（`pg_dump | gzip → data/backups/tc-YYYYMMDD.sql.gz`，保留 14 天）+ **`tc backup` CLI 主触发**（PRD §4 F11）+ scheduler 每日 03:00 可选自动化（§10）—— **数据比代码值钱，pgdata 卷不是备份**

**切片一：端到端跑通闭环**（对应验收 1/7/8）
> 这一片把 FakeLLM 集成测试搭起来——27B 真跑一篇 20–60s，开发迭代必须靠 mock，不然改一行提示词等一分钟
- [ ] 1.1 脚手架：`pyproject.toml` + `docker-compose.yml`(pgvector) + config（`config.yaml` + `feeds.yaml`） + `scripts/init_db` + **退路 Python 3.12/3.13 备好**（§2）
- [ ] 1.2 `app/db`：models + **Alembic 迁移（DDL §5，维度定死 `vector(1536)` + `db.vector_dim=1536`，§5.2 切片一前必须敲定）+ 扩展/维度校验** + jieba 预切词（§5.3 `to_tsvector('simple', 拼接文本)` 写入，**不要**用 `array_to_tsvector`）
- [ ] 1.3 `app/llm`：base Protocol + omlx.py（生成/嵌入/端点探测；embed 封装层含 **instruct prefix**，query 加 / document 不加，§4.2）+ client（并发/重试/健康 + 单次探测 §4.4）+ prompts + structured（含 `parse_with_repair`，§6）+ **FakeLLM mock**（开发期 + 集成测试用，三端点内存实现，固定回放 fixture）
- [ ] 1.4 `app/ingest`：feeds.py（feedparser + ETag/304）+ dedup.py
- [ ] 1.5 `app/services/cleaner.py`：HTML→Markdown + 语言检测
- [ ] 1.6 `app/pipeline.py`：processing_jobs 入队（幂等 `ON CONFLICT DO NOTHING` + supersede **同事务**，见 §5.1 部分唯一索引 / §6）+ worker（**单条原子 pick-and-claim SQL，§6**，FOR UPDATE SKIP LOCKED + UPDATE 同事务）+ lock_until 租约 5 分钟 + 长任务续租 + **重试按瞬时/永久分类**（瞬时无限续跑退避封顶 15m 不进死信，永久 3 次死信，§6/§11）+ **领取门控**（掉线时不领新 job，§6）+ recover（**按租约回收过期 running，仅 worker 启动时跑，跨进程安全**，§6）
- [ ] 1.7 `app/services/llm_tasks.py`：`summarize` 任务（走 `complete_summarize()` 钩子，§6：**summaries upsert + tsv 刷新（两阶段，§5.3）+ embed_summary 入队同事务**；手动 `tc retry summarize` 也走同一钩子，F2 P0）+ `complete_embed()` 钩子（embed_core/embed_summary 落库 + job 状态推进同事务，手动 `tc retry embed_*` 也走，§6）+ `app/services/topics.py`：`match_keywords()`（供切片三的 `classify_topics` 快路径）；**CPU 密集（jieba/清洗）一律 `asyncio.to_thread`**（§2，不阻塞事件循环）
- [ ] 1.8 `app/services/cli.py`（切片一部分）：`feeds import` / `fetch` / `summarize` / `list` / `search`（**先纯关键词**）/ `article <id>` / **`status`**（**队列深度 / 失败任务 / LLM 健康，无 WebUI 期间唯一可观测性**，连 psql 排障成本太高）/ **`retry <article_id> <task>`**（走对应 `complete_*()` 钩子）
- [ ] 1.9 验收：PRD §15 验收 1（建库 + 抓取 + 清洗）/ 7（中文摘要）/ 8（关键词全文搜索），**用 FakeLLM 跑通**

**切片二：嵌入 + 混合检索**（对应验收 9）
> 维度策略（§5.2）切片一前定，向量功能本身切片二上
- [ ] 2.1 `app/services/llm_tasks.py`：`embed_core`（title+body）+ `embed_summary`（summary），维度 1536 + instruct prefix（§4.2/§5.2）
- [ ] 2.2 `app/services/search.py`：`search(q)` 混合检索（语义 top-k ∪ 关键词 top-k → **P1 即 RRF 融合** `1/(k+rank)`，§7）；语义通道 `WHERE model=<active embed model>`（§5.2/§7）；查询侧用 `websearch_to_tsquery`，**不要**裸 `to_tsquery`（§5.3/§7）；articles ∪ wiki_pages 按 ref_id 去重（§7）；`scripts/backfill` 规格定稿（切嵌入模型全量重嵌，§5.2）
- [ ] 2.3 CLI：`search` 升级为 `mode=hybrid|semantic|keyword`（默认 hybrid）
- [ ] 2.4 验收：PRD §15 验收 9（混合检索 P95 < 100ms，召回 100%；RRF 融合）

**切片三：主题 + Wiki 词条**（对应验收 3/5）
- [ ] 3.1 `app/services/topics.py` 完善：topic CRUD + `match_keywords()` 快路径 + `classify_topics` LLM 慢路径（合并规则见 §6）
- [ ] 3.2 `app/services/wiki.py`：文章词条生成（`related_json` = 同主题 article top-5，§6）
- [ ] 3.3 CLI 补：`topic add` / `topic list` / `list --topic`
- [ ] 3.4 验收：PRD §15 验收 3（主题跨源聚合）/ 5（Wiki 按**关键词**全文搜索，主题/实体浏览 P2）

**横切**：
- [ ] X.1 `app/scheduler.py`：fetch_all + drain_queue + pg_backup（**可选自动化，主触发是 `tc backup` CLI**，§10）+ cleanup_fetch_events（§10）
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
