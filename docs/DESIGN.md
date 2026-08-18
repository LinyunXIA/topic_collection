# 技术设计文档 — Topic Collection

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述（目录结构 / DDL / 接口）只在一处维护、另一处引用，避免漂移
> 版本：v0.4 · 2026-08-17 · 随决策持续更新（与 PRD v0.3 同步）

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
  - 生成：`Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16-mlx-4Bit`（实测可用，质量更佳；备选 9B INSTRUCT 更轻量；`THINKING` 变体加载失败待修复）
  - 嵌入：`Qwen3-Embedding-8B-4bit-DWQ`（实测输出 4096 维）
  - 重排（P2）：`Qwen3-Reranker-4B-mxfp8`（实测 `/v1/rerank` Cohere 风格可用）
- 增量处理：仅新/变更文章入流水线；LLM 产物按 `(article, task, model, content_hash)` 缓存
- 检索双通道：向量语义（pgvector HNSW）+ 关键词全文（tsvector + GIN，jieba 预切词）

---

## 2. 技术选型

| 领域 | 选型 | 说明 |
|---|---|---|
| 语言/运行时 | Python 3.14 | venv 已就绪 |
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
    generation_model: str            # Qwen3.6-27B-AEON-...（默认生成）
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
- **端点均已实测可用**：`/v1/embeddings`（`Qwen3-Embedding-8B-4bit-DWQ`，返回 4096 维 float）、`/v1/rerank`（Cohere 风格：入参 `query/documents/top_n`，出参 `results:[{index, relevance_score}]`）
- **向量维度校验**：启动/首个 embed 后实测维度与 `db.vector_dim`（=4096）比对，不一致即告警并阻止写入（防 HNSW 失配 / 模型切换）

### 4.3 降级链路

| 能力 | 主（oMLX） | 降级 |
|---|---|---|
| 生成 | `/v1/chat/completions` | Ollama（切换 backend） |
| 嵌入 | `/v1/embeddings` + `Qwen3-Embedding-8B` | 进程内 `fastembed`（`bge-small-zh-v1.5`/`bge-m3`）→ 仍失败则语义检索降级纯关键词 |
| 重排 | `/v1/rerank` + `Qwen3-Reranker-4B`（P2） | 进程内 `bge-reranker-v2-m3` → 不重排（保持加权融合） |

### 4.4 `LLMClient` 门面

并发信号量（默认 1）、每调用超时、指数退避重试（401/5xx/超时）、`healthy` 标志与定时健康检查。重试/超时只在此层处理，services 不碰传输。**两层重试分工**：客户端=秒级抖动重试（单次调用内）；job 级 `lock_until` 退避（§6）=分钟级长中断（oMLX 整体不可用），互不冲突。

### 4.5 提示词契约（一律中文输出）

| 任务 | 输出 |
|---|---|
| `summarize` | JSON `{"summary_zh", "key_points":[]}`（3–5 要点） |
| `translate` | 简体中文纯文本 |
| `extract_entities` | JSON `{"entities":[{name,type,aliases,description}], "relations":[{subject,predicate,object}]}` |
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
  vector vector(4096),               -- 实测 4096 维，迁移时定死
  UNIQUE (article_id, kind, model)   -- upsert 保留最新
);
CREATE INDEX emb_hnsw_idx ON article_embeddings
  USING hnsw (vector vector_cosine_ops);

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
  summary_text TEXT, key_points_json JSONB, confidence NUMERIC,
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
  ref_id BIGINT,                    -- 多态引用：按 kind 指向 article/topic/entity（manual 无 ref），单一 FK 不可行
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

### 5.2 向量维度决策（已实测）

- `Qwen3-Embedding-8B-4bit-DWQ` 实测输出 **4096 维**（`/v1/embeddings` 不裁剪，直接返回全量 float）
- 迁移 DDL 与 `db.vector_dim` **统一 4096**，无截断
- 保留运行时校验：`embed` 返回维度 ≠ 4096 → 告警并阻断写入（防 HNSW 失配 / 模型切换）

**embedding 长文策略**：
- 语义检索**主依赖 `title` + `summary` 的向量**（天然短、安全），`body` 向量只作补充
- `body` embed 设 `max_tokens≈8192` 截断（`Qwen3-Embedding-8B` max_model_len=40960，长文行为未实测，规避超限/静默截断风险）
- **`embed_core` / `embed_summary` 两个独立任务，按 payload 写 `article_embeddings`**：新文章入队 `embed_core` 写 `title` + `body` 两行（均可即时就绪）；`summary` 向量在 `summarize` 成功后由 `embed_summary` 补写（upsert `kind='summary'`）——避免 embed 先于 summary 执行时拿空文本建向量
- **拆成两个 task 值**（而非同一 task 的不同 kind payload）：入队幂等/活跃唯一只看 `(article_id, task)`（§5.1/§6），若共用 `embed` 一 task，`embed_core` 遇 LLM 退避卡在 `lock_until`（仍 `queued` 占槽）期间，`summarize` 成功后补入的 summary embed 会撞活跃槽被 `ON CONFLICT DO NOTHING` 静默丢弃，`summary` 向量将永久缺失；拆开后两次入队天然不冲突、退避互不影响，也无需"embed 顺带补 summary"的兜底逻辑
- `embed_core` 写 2 行（title+body）、`embed_summary` 写 1 行（summary），单任务粒度暂不可拆；Phase 2 再上正文分块 + 池化

### 5.3 全文检索（中文友好）

- `articles.tsv tsvector('simple', ...)`：插入前用 **jieba** 对 title+content 预切词为空格分隔 tokens
- GIN 索引；避免依赖需编译的 `zhparser`
- `wiki_pages` 同理建 `wiki_tsv`

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
```

**文章状态机**：`pending → processing → done | unparseable | error`（部分任务失败仍可 `done`，详情页可重试单个任务）

**入队规则（按任务）**：
| 任务 | 优先级 | 触发 | 模型 |
|---|---|---|---|
| `embed_core` | 高 | 新文章（title+body） | Qwen3-Embedding-8B |
| `embed_summary` | 高 | `summarize` 成功后（summary） | Qwen3-Embedding-8B |
| `summarize` | 高 | 新文章 | Qwen3.6-27B |
| `topics` | 中 | 新文章（未命中关键词）+ 主题变更（重算） | Qwen3.6-27B |
| `wiki` | 低 | 摘要落地后（实体 P2） | Qwen3.6-27B |
| `translate` | 低 | lang≠zh 且用户触发 | Qwen3.6-27B |

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
- 内容变更（活跃 job 期间 content_hash 变化）：旧 job 标 `superseded`（不占活跃槽位）→ 再入队新 job：
  ```sql
  UPDATE processing_jobs SET status='superseded', updated_at=now()
  WHERE article_id=$1 AND task=$2 AND status IN ('queued','running');
  ```
- 优先级数值约定：`embed_core=1`、`embed_summary=1`、`summarize=2`、`topics=3`、`wiki=4`、`translate=5`

**worker 领取（SKIP LOCKED）**：
```sql
SELECT * FROM processing_jobs
WHERE status='queued' AND (lock_until IS NULL OR lock_until < now())
ORDER BY priority, created_at LIMIT 1 FOR UPDATE SKIP LOCKED;
```

**worker 运行模型（常驻自驱，非心跳驱动）**：
- lifespan 启动**单个 asyncio worker task**：`循环 { 领取(SKIP LOCKED) → 无任务 sleep ~1s → 处理完继续 }`；领取与处理都在 await 点让出事件循环，不阻塞 fetch / HTTP
- 入队到开始 ≤ 当前在飞任务时长 + ~1s（并发=1 下在飞任务即 LLM 调用时长）
- **scheduler 的 drain_queue 不参与领取**（避免双领取者歧义），只做维护，见 §10
- LLM 掉线期间所有 queued 带未来 lock_until → 领取空手返回后 sleep 退避（healthcheck 门控），不空转打 oMLX

**状态机原子性**：worker 领取 job 后立即 `UPDATE ... SET status='running', lock_until=NULL`；完成时 `UPDATE ... SET status='succeeded' WHERE status='running'`（WHERE 条件防覆盖已被 supersede 的 job）；崩溃/进程退出时 status 留在 `running`，由启动时 `recover_interrupted()` 统一改回 `queued`，**先于 worker 启动**避免与新领取竞争（见 §8 lifespan 启动顺序）

**运维模式（Phase 1 vs Phase 2）**：
- **Phase 2（WebUI 上线后）**：FastAPI lifespan 在 `app/main.py:create_app()` 启动顺序 = init_db（校验 vector 扩展/维度）→ 探测 oMLX 三端点 → `recover_interrupted()` → 启动 scheduler + worker task（同一进程）
- **Phase 1（无 WebUI，CLI 入口）**：worker 单独常驻，通过 `python -m app.worker`（或 `make worker`）启动；scheduler 同样独立 `python -m app.scheduler`。CLI 命令（`tc fetch` / `tc summarize` / `tc search` ...）走 services 层但不启动 worker——入队后必须有 worker 在跑才能真正消费。开发期推荐两个终端：`make worker` + `tc fetch` / `tc search` 等

**去重**：URL hash 相同 → 复用旧文章，`mention_count+1`；URL 不同但 content_hash 相同 → 记 `dedupe_of`。**去重在 LLM 花钱前完成。**

**重试/降级矩阵**：
| 失败 | 处理 |
|---|---|
| 抓取网络错误 | 记录 fetch_events，下次周期再试；连续 `ingestion.feed_disable_after`（默认 5）次自动禁用 feed；陈旧 `fetch_events` 按 `fetch_events_retention_days`（默认 90 天）定期清理 |
| 文章不可解析 | status=unparseable，保留原文，跳过 LLM |
| LLM 401/5xx/超时 | job 保持 `queued`，`lock_until` = 退避 1m→5m→15m，max_attempts=3；worker 领取条件自动跳过未到期的行，到点自动续跑（无需额外状态） |
| 内容变更（活跃 job 期间） | 旧 job→`superseded`，入队新 job（幂等，见上） |
| LLM JSON 解析失败 | structured.parse_with_repair（去围栏→找平衡{}→带错重问一次）→ 仍失败 low_confidence |
| 进程中断 | 启动时 `recover_interrupted()` 将 running→queued |

---

## 7. 检索设计（混合）

```
search(q):
  1) 语义: embed(q) → article_embeddings ORDER BY vector <=> $1 LIMIT k1
     （title/summary/body 三粒度都参与；k1 后按 article_id 去重取最高分，避免同文重复）
  2) 关键词: jieba(q) → articles.tsv @@ to_tsquery('simple', ...) LIMIT k2
  3) 融合: P1 加权求和(关键词命中加分)；P2 RRF + rerank(top-k 候选)
  4) 同时检索 wiki_pages 词条，合并分页
```
- P1：简单加权即可满足「中文关键词 + 语义近似」双场景
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

**内部约定**：FastAPI lifespan 启动顺序 = init_db（校验 vector 扩展/维度）→ 探测 oMLX 三端点 → `recover_interrupted()`（running→queued，**先于 worker 启动**，避免与新领取竞争）→ 启动 scheduler + worker task。

---

## 9. 配置 Schema（config.yaml + feeds.yaml）

**`config.yaml`**（系统配置，**不含订阅源**）：

```yaml
data_dir: ./data
db:
  dsn: postgresql+asyncpg://tc:tc@localhost:5432/topic_collection   # 见 §5.4 docker-compose
  pool_size: 5
  vector_dim: 4096            # 实测 Qwen3-Embedding-8B 输出维度
web: { host: 127.0.0.1, port: 7111 }   # 必须 ≠ oMLX 端口 (8000)，避免与本地 LLM 端口冲突
llm:
  backend: omlx               # omlx | ollama
  endpoint: http://localhost:8000   # oMLX OpenAI 兼容端点（实测）
  # 鉴权：本机不鉴权（已确认），不发 Authorization 头；如需鉴权再设 api_key_env
  model: Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16-mlx-4Bit   # 实测可用（质量更佳）
  # 备选：Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED-MLX-mxfp8（更轻量）
  # THINKING 变体（Qwen3.5-9B-…-THINKING-HERETIC-UNCENSORED）oMLX 加载失败，修复后可用
  max_concurrency: 1
  models: { summarize: <model>, translate: <model>, entities: <model>,
            topics: <model>, wiki: <model>, report: <model> }   # 默认=generation model
  embed:
    backend: omlx             # omlx(/v1/embeddings) | inproc(fastembed) 降级
    model: Qwen3-Embedding-8B-4bit-DWQ
    max_tokens: 8192          # 正文 embed 截断上限（title/summary 天然短不截断，见 §5.2）
  rerank:                     # P2 启用
    model: Qwen3-Reranker-4B-mxfp8
ingestion:
  fetch_interval_hours: 6
  user_agent: "TopicCollection/0.1 (+local personal KB)"
  max_scrape_bytes: 5242880
  feed_disable_after: 5          # feed 连续失败 N 次自动禁用（§6）
  fetch_events_retention_days: 90 # fetch_events 审计表保留天数（P3 归档策略统一处理）
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
| daily_report | 每日 08:00 | 日报（P2） |
| weekly_report | 周一 08:00 | 周报（P2） |
| healthcheck | 每 5m | LLM 健康探测，更新横幅 |

---

## 11. 错误处理与降级总表

| 场景 | 行为 | UI 呈现 |
|---|---|---|
| oMLX 全挂 | 生成任务保持 queued + lock_until 退避，到点自动续跑；文章可浏览原文 | 概览红色横幅「LLM 离线」 |
| 仅嵌入不可用 | 语义通道关闭 | 搜索页提示「仅关键词模式」 |
| feed 连续失败 | 自动禁用 | Feed 列表状态徽标 |
| 任务最终失败 | job=failed，记 error | 文章详情可手动重试 |
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
- **db**：pytest + 临时 Postgres（docker compose 测试库）；向量维度校验用例
- **降级**：mock `/v1/embeddings` 404 → 断言语义通道降级

---

## 14. Phase 1 MVP 任务清单（可用即可，无 WebUI，CLI 为入口）

- [ ] 1. 项目脚手架：`pyproject.toml`、`docker-compose.yml`(pgvector)、config（`config.yaml` + `feeds.yaml`）、scripts/init_db
- [ ] 2. `app/db`：models + Alembic 迁移（DDL §5）+ 扩展/维度校验 + jieba 预切词
- [ ] 3. `app/llm`：base Protocol + omlx.py（生成/嵌入/端点探测）+ client（并发/重试/健康）+ prompts + structured
- [ ] 4. `app/ingest`：feeds.py（feedparser + ETag/304）+ dedup.py
- [ ] 5. `app/services/cleaner.py`：HTML→Markdown + 语言检测
- [ ] 6. `app/pipeline.py`：processing_jobs 入队（幂等 `ON CONFLICT DO NOTHING` + supersede，见 §5.1 部分唯一索引）+ worker（SKIP LOCKED）+ recover
- [ ] 7. `app/services/llm_tasks.py`：summarize / embed_core + embed_summary / classify_topics（关键词快路径 + LLM 慢路径，合并规则见 §6）；`app/services/topics.py`：topic CRUD + `match_keywords()`（供 `tc topic add/list`）
- [ ] 8. `app/services/wiki.py`：文章词条生成 + 混合检索 search(q)（§7）
- [ ] 9. `app/scheduler.py`：fetch_all + drain_queue
- [ ] 10. `app/services/cli.py`：typer 命令 `feeds import` / `topic add` / `topic list` / `fetch` / `summarize` / `list [--topic]` / `search` / `article <id>`（Phase 1 主入口；`feeds import` 读 `config/feeds.yaml` upsert 进 DB 见 §9；`topic` 写 DB `topics` 表）
- [ ] 11. 测试：FakeLLM 集成用例 + 单元用例
- [ ] 12. 验收：对照 PRD §15 Phase 1 条目（1/3/5/7/8/9）走通

> **WebUI（`app/api` + `app/web`）整体移入 Phase 2。**

---

## 15. oMLX 实测结论（2026-08-12）

| 项 | 结论 |
|---|---|
| 端口 | `http://localhost:8000` |
| 鉴权 | ✅ 已关闭：三端点不带 token 均正常（models 200 / embeddings dim 4096 / chat 生成 OK） |
| 模型列表 | `GET /v1/models` 正常，列出全部模型（含 DeepSeek-V4 / Qwen3.6-27B / MarkItDown 等） |
| 嵌入 | `POST /v1/embeddings` ✅ `Qwen3-Embedding-8B-4bit-DWQ`，输出 **4096 维** float |
| 重排 | `POST /v1/rerank` ✅ Cohere 风格：入参 `query/documents/top_n`；出参 `results:[{index, relevance_score}]` |
| json_mode | ✅ `response_format:{type:json_object}` 被接受，返回合法 JSON |
| 生成模型 | `Qwen3.6-27B-AEON…-mlx-4Bit` ✅ 可用（json_mode 正常，质量更佳）；`9B INSTRUCT…` ✅ 可用（更轻量）；**`9B THINKING…` ⚠️ 加载失败**（Missing 154 parameters，权重不完整/损坏） |

> **遗留**：`Qwen3.5-9B-Claude-4.6-HighIQ-THINKING-HERETIC-UNCENSORED` 需在 oMLX 侧修复（补全权重/重新量化）。修复后把 config `llm.model` 改回即可，代码无需变更。

---

## 16. 已知限制（接受，或 P3 处理）

- **英文大小写**：`tsvector('simple')` 不做大小写归一化，英文检索大小写漏配；应用层检索时 lowercase 缓解，完整处理留 P3
- **外键策略**：产物 / 向量 / 队列 / 主题归属统一 `ON DELETE CASCADE`（删文章/主题/Feed 自动清理孤儿行）；`dedupe_of`、`articles.feed_id`、`relations.source_article_id` 用 `ON DELETE SET NULL`（保留引用方转独立）；仅 `wiki_pages.ref_id` 为多态引用无法建 FK（§5.1 注释），靠应用层校验——P3 归档裁剪直接受益于级联
- **生成/嵌入共用并发=1**：有意为之——oMLX 按请求切换模型会抖动加载，并发收益不抵抖动；吞吐可接受
