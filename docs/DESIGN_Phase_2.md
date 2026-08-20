# 技术设计文档 — Phase 2 蓝图 — Topic Collection

> 关联文档：[DESIGN.md](DESIGN.md)（Phase 1/1+/1++ 已部署，无问题，新起点） + [PRD.md](PRD.md)
> 版本：v0.15 · 2026-08-20 · Phase 2 增量：DB 隔离 + embed/rerank 外部化 + 翻译后台 + 飞书推送
> 本文件为 Phase 2（WebUI Dashboard + 实体/图谱/报告/API 连接器）及后续阶段的权威设计，原 DESIGN.md 仅保留 Phase 1/1+/1++ 已部署内容

> **拆分说明**：2026-08-20 Phase 1/1+/1++ 全部署且 214/214，无 OPEN Issue。按用户要求将 DESIGN.md 中 Phase 2 及之后章节移至此文件，DESIGN.md 作为 Phase 1 新起点。

> v0.15（2026-08-20 增量）：`PR #40` 补 `tests/test_regression_31_34.py` 10 用例，`204→214`；`§14 2.6.3` 拆为 `2.6.3a`/`2.6.3b`（`a003` 仅 `tsv` 列，跨表 UNION 待做）；`§10.4` 白名单下沉共享出口；`§5.4.1` 补池外长连接约束。

> v0.15（2026-08-20）：**Phase 2 PRD 4 项增量**——① `§5.4.1` 生产 DB 本机 `postgres:5432 postgres/${POSTGRES_PASSKEY}` 与开发 `5433 tc/tc` 隔离，可单独 `TC_APP_ENV=prod` 启动；② `§4.3/4.7` `embed`/`rerank` 解除强制本地，`per-capability` 自由切本地/外部（`dimensions=1536` 不变，标准 `ProviderPatch`）；③ `§6.Z` 翻译本地 27B 后台慢任务，落库即原文可读、译文 `translating: true` 轮询；④ `§10.4` 日报/周报飞书 Webhook 机器人推送（`open.feishu.cn`）。

---

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
      "subject": "通义千问 3",
      "predicate": "developed_by",
      "object": "阿里巴巴",
      "confidence": 0.85,
      "evidence_span": "Qwen3 由阿里巴巴达摩院开源..."
    }
  ]
}
```

> **⚠ 关键约束：`relations.subject` 和 `relations.object` 必须引用已抽取实体的 `canonical_name_zh`**（不是 `name` / `surface`）。`complete_extract` 通过 `_build_entity_id_map` 按 `canonical_name_zh` 建立映射——若引用英文 `name`（如 `"Qwen3"`），映射永远找不到、**所有关系会被静默跳过**（§6.Y）。prompt 中必须明确此约束。

**严格约束**（写入前在 `entities.upsert` 服务层校验）：

1. **`grounding` 规则**：每个 entity 的 `surface` 必须在原文 `content_text` 子串内（`surface in content_text`）；若 LLM 给出的 surface 不在原文，由 `services/entities.normalize_surface()` 自动修正为原文最近邻 span，仍找不到 → `confidence *= 0.5`；找不到且无法对齐 → 丢弃。**统一执行**：`complete_extract` 钩子（§6.Y）必须先调 `normalize_surface()` 尝试对齐，再做 confidence 降级判断——不要跳过对齐直接砍半
2. **跨语言归一**：所有实体都必须有 `canonical_name_zh`（即使原文是英文，也要给中文规范化名，存 `entities.aliases_json` + `entities.canonical_name`）；形成跨语言别名岛屿统一（"OpenAI"/"开放AI" → 同一 entity）
3. **type 枚举**：`person | org | product | model | technology | concept | event | location | other`（LLM prompt 给定）；存 `entities.entity_type`
4. **别名合并策略**：upsert `entities` 表时按 `(entity_type, canonical_name_zh)` UNIQUE 冲突，新实体的 `aliases` 数组与既有 `aliases_json` 取**并集（dedupe）**——用 `jsonb_array_elements_text` 展开、去重后重聚合（§6.Y SQL 中 `||` 拼接需替换为去重逻辑）

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

### 4.8 Phase 2 扩展：`embed`/`rerank` 自由切本地/外部（v0.15）

- **现状 `DESIGN.md:208` 强制本地**（隐私）→ Phase 2 解除，`per-capability` 自由切（与 `generate` 同形），`dimensions=1536` 不变，标准开发候选未定
- **配置**：`config/config.yaml:27` `llm.embed {backend: omlx|openai, endpoint, api_key_env, model, patch:{send_dimensions:true,dimensions_value:1536}}`，`llm.rerank` 同；`app/config.py:49` `EmbedSettings/RerankSettings{backend,endpoint,api_key_env,model}` 已含 `api_key_env`
- **工厂**：`app/llm/factory.py:173` 去 `if backend != "omlx": raise`，改为 `if backend=="omlx" → _build_omlx else if backend in providers → _build_openai(embedding_model=cfg.model, patch=_dict_to_patch(...))`，`rerank` 同理（外部不支持则早失败提示回退 `omlx`）
- **适配**：`app/llm/patches.py:16` 预定义 `OPENAI_PATCH(send_dimensions=true)` 或 `SILICONFLOW_PATCH` 按需，`app/llm/adapter.py:88` `send_dimensions` 走 `ProviderPatch`，`app/llm/openai.py:91` 外部 `rerank` 补 `POST /v1/rerank`
- **存储/检索**：`vector(1536)` 锁死，`app/db/engine.py:98` 维度校验不变，`app/services/search.py:168` `WHERE model=:active` + `app/services/llm_tasks.py:259` 校验 `dim==1536`，切换模型需 `TRUNCATE article_embeddings` 或 `scripts/backfill` 重建 `HNSW`
- **Worker**：`app/worker.py:80` 增 `rerank_llm = LLMClient(build_provider("rerank"))` 供 `§7.1` 复用，`TC_LLM__EMBED__BACKEND=openai` 环境覆盖

### 5.1.5 Phase 2 增量 DDL（与 §14 切片同步迁移，已落地 `a003-a006`，历史快照）

> **已落地**：原设计承诺 Phase 1 预创建，实为 `a004`（`fix #5`）按最终形态一次建表，Phase 1 仅 10 表。`a003 wiki tsv`/`a005 task CHECK`/`a006 pg_trgm` 已合入，`RENAME/DROP` 语句仅适用于 2026-08-19 前旧库，需 `IF EXISTS` 包裹；现库 `alembic upgrade head` 即得最终形态，下述增量为历史快照。

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
ALTER TABLE reports ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
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

#### DDL 迁移顺序与兼容性（历史）

1. 设计要求 `IF EXISTS/IF NOT EXISTS` 幂等，但实码 `a003:34` `ADD COLUMN/CREATE INDEX` 无 `IF NOT EXISTS`，`a004` 全为 `create_table`，靠 Alembic 版本表阻断重复 `upgrade`，非 SQL 幂等；`a005` 用 `DO $$ IF EXISTS` 包 `DROP` 已示范
2. 新增 CHECK 约束前先 `UPDATE processing_jobs SET task=...` 把已存在的未知 task 映射到合法值（不能因为加约束让 worker 任务跑挂）
3. 索引加 `CONCURRENTLY`（生产无锁），dev 阶段无所谓

### 5.4.1 开发/生产 DB 隔离（Phase 2 新增，v0.15）

| 环境 | DSN | 宿主机端口 | 用户 | 密码来源 | 启动方式 |
|---|---|---|---|---|---|
| `dev`（默认） | `postgresql+asyncpg://tc:tc@localhost:5433/topic_collection` | `5433 → 5432` | `tc` | 硬编码 `tc`（仅本地 dev） | `docker compose up -d` + `TC_APP_ENV=dev`（默认） |
| `prod` | `postgresql+asyncpg://postgres:${POSTGRES_PASSKEY}@localhost:5432/topic_collection` | `5432`（本机 postgres 默认端口） | `postgres` | 环境变量 `POSTGRES_PASSKEY`（必填，`TC_DB__PROD_DSN` 可整串覆盖） | `prod WebUI: TC_APP_ENV=prod uvicorn app.main:app`（`lifespan` 拉起 worker+scheduler，唯一入口）；`prod CLI/backfill: TC_APP_ENV=prod python -m app.worker` 仅无 WebUI 时，二者禁止同库并发（`pg_try_advisory_lock` 互斥，`§14 2.1.1`） |

- **配置**：`app/config.py:13` `DBSettings{dsn, pool_size, vector_dim, prod_dsn?, env: Literal[dev,prod]=dev}`，`TC_DB__PROD_DSN` / `POSTGRES_PASSKEY` fail fast；`config/config.yaml:6` 加 `db.prod` 示例与 `TC_DB__PROD_DSN` 覆盖说明
- **引擎**：`app/db/engine.py:25` `get_engine` 按 `env` 选 `dsn`，`check_extensions` prod 仍校验 `vector/pg_trgm/vector_dim`，`init_db` 需本机 `postgres` 库已建 `CREATE EXTENSION IF NOT EXISTS vector`（`a001` 不写，由 `scripts/init_db.py:26` 负责）
- **备份**：`app/scheduler.py:218` `run_pg_backup` dev 走 `docker compose exec -T postgres pg_dump -U tc`，prod 走 `pg_dump -h localhost -U postgres`（`PGPASSWORD=${POSTGRES_PASSKEY}`），`TC_DB__PROD_DSN` 已覆盖则解析 host/user/db
- **初始化**：生产库当前为空，需 `initdb` + `createdb topic_collection` + `psql -c "CREATE USER postgres WITH PASSWORD '\$POSTGRES_PASSKEY'"`（若未设）+ `python -m scripts.init_db` 幂等（`CREATE EXTENSION + alembic upgrade head`）
- **互斥（池外长连接，fix #3）**：`worker_loop` 启动前 `asyncpg.connect(dsn)` **池外专用长连接** `SELECT pg_try_advisory_lock(hashtext('topic_collection_worker'))` 并持有至进程退出（`pg_try_advisory_lock` 会话级绑定单连接，`get_session` 池化连接归还即失锁，双活静默），未获锁则 `exit 1`，`uvicorn` 与 `python -m app.worker` 二选一，`pg_try_advisory_xact_lock` 禁用（事务级提交即丢），30s 心跳校验连接未断

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
    
    await complete_extract(session, article_id, content_hash, parsed, content_text=row["content_text"], settings=settings)

async def complete_extract(session, article_id, content_hash, parsed, *, content_text, settings):
    """公共钩子（同事务）：
    1. entities upsert（按 (entity_type, canonical_name_zh) UNIQUE 冲突；aliases/description/mention_count 合并）
    2. grounding 校验：surface 必须在原文（§4.6.1 统一策略：先 normalize_surface 对齐最近邻 span，
       对不上再 confidence *= 0.5；找不到且无法对齐 → 丢弃）
    3. article_entities upsert（confidence, surface）
    4. relations upsert（按 (subject_id, predicate, object_id) UNIQUE 冲突；source_articles_json 去重追加）
    5. 决定 generate_entity_wiki 入队（仅 entity 是新的 / description 变更）
    6. check_and_set_done
    """
    from app.services.entities import normalize_surface  # 延迟导入

    # 1. entities upsert
    for ent in parsed.get("entities", []):
        # grounding 校验（§4.6.1 统一策略：先尝试对齐，再降级）
        surface = ent.get("surface")
        if surface and content_text and surface not in content_text:
            # 先尝试 normalize_surface 对齐最近邻 span
            aligned = await normalize_surface(content_text, surface)
            if aligned:
                ent["surface"] = aligned  # 修正为对齐后的 span
            else:
                ent["confidence"] = (ent.get("confidence") or 0.5) * 0.5
                if ent["confidence"] < 0.1:
                    continue  # 丢弃
        # aliases_json 合并：现有 + 新
        await session.execute(
            text("""
                INSERT INTO entities (canonical_name_zh, aliases_json, entity_type, description, mention_count, confidence)
                VALUES (:zh, :aliases_json, :type, :desc, 0, :conf)
                ON CONFLICT (entity_type, canonical_name_zh) DO UPDATE SET
                  aliases_json = (
                    SELECT jsonb_agg(DISTINCT v)
                    FROM jsonb_array_elements(
                      COALESCE(entities.aliases_json, '[]'::jsonb) ||
                      COALESCE(EXCLUDED.aliases_json, '[]'::jsonb)
                    ) v
                  ),
                  description = CASE WHEN EXCLUDED.confidence > entities.confidence
                                     THEN EXCLUDED.description ELSE entities.description END,
                  -- mention_count = 涉及该实体的文章数（从 article_entities 聚合，而非每次 extract +1）
                  mention_count = (SELECT COUNT(*) FROM article_entities ae WHERE ae.entity_id = entities.id),
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
    
    # 取回 entity_id 映射（composite key = (entity_type, canonical_name_zh) 防同名不同类型混淆）
    eid_map = await _build_entity_id_map(session, parsed)
    
    # 3. article_entities upsert
    for ent in parsed.get("entities", []):
        eid = eid_map.get((ent.get("type", "other"), ent["canonical_name_zh"]))
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
    # relations.subject/object 引用 canonical_name_zh（§4.6.1 约束）
    # 需要一个反向映射 canonical_name_zh → (type, id) 来查找 entity_id
    name_to_eid = {zh: eid for (typ, zh), eid in eid_map.items()}
    for rel in parsed.get("relations", []):
        sid = name_to_eid.get(rel["subject"])
        oid = name_to_eid.get(rel["object"])
        if not sid or not oid:
            continue
        await session.execute(
            text("""
                INSERT INTO relations (subject_id, predicate, object_id, source_articles_json, confidence, last_seen_at)
                VALUES (:s, :p, :o, jsonb_build_array(:aid::bigint), :c, now())
                ON CONFLICT (subject_id, predicate, object_id) DO UPDATE SET
                  -- 去重追加：先过滤已存在的 article_id 再拼接（防 retry 重复记同一篇文章）
                  source_articles_json = (
                    SELECT jsonb_agg(DISTINCT v)
                    FROM jsonb_array_elements(
                      COALESCE(relations.source_articles_json, '[]'::jsonb) ||
                      COALESCE(EXCLUDED.source_articles_json, '[]'::jsonb)
                    ) v
                  ),
                  confidence = GREATEST(relations.confidence, EXCLUDED.confidence),
                  last_seen_at = now()
            """),
            {"s": sid, "p": rel["predicate"], "o": oid, "aid": article_id, "c": rel.get("confidence", 0.5)}
        )
    
    # 5. 决定 generate_entity_wiki 入队（仅 entity 是新的 / description 变更）
    new_entity_ids = await _detect_new_or_changed_entities(session, article_id, list(eid_map.values()))
    if new_entity_ids:
        await enqueue_entity_wiki(session, article_id, new_entity_ids, content_hash)

    # 6. done 检查
    await check_and_set_done(session, article_id)

async def enqueue_entity_wiki(session, article_id, entity_ids, content_hash):
    """入队 generate_entity_wiki（payload 合并策略）。

    ⚠ 身份模型说明（Issue #3 修正）：
    - generate_entity_wiki 是 entity 作用域，但活跃唯一键是 (article_id, task)——
      同一篇文章可能先后抽取到不同批次的新 entity
    - 策略：ON CONFLICT DO UPDATE 合并 entity_ids 到 payload_json（去重），
      不用 DO NOTHING（否则第二批 entity 静默丢弃，永远不生 wiki）
    - 同一 entity 出现在多篇文章：各文章各自入队、wiki_pages upsert 按 ref_id 幂等，
      先完成者写入，后者覆盖（同一版本 LLM 产物相同，无实质竞态）
    - generate_topic_wiki 同理：topic 作用域，(article_id, task) 键控时 payload 带 topic_ids
    """
    import json as _json
    payload = _json.dumps({"entity_ids": entity_ids}, ensure_ascii=False)
    await session.execute(
        text("""
            -- 先 supersede 同 (article_id, task) 的活跃 job（含 payload 合并语义）
            UPDATE processing_jobs SET status='superseded', updated_at=now()
            WHERE article_id=:aid AND task='generate_entity_wiki' AND status IN ('queued','running');
            -- 入队：冲突时合并 entity_ids（ON CONFLICT 只在 supersede 后无活跃行时触发）
            INSERT INTO processing_jobs (article_id, task, status, content_hash, priority, payload_json)
            VALUES (:aid, 'generate_entity_wiki', 'queued', :ch, 5, :payload::jsonb)
            ON CONFLICT (article_id, task) WHERE status IN ('queued','running') DO UPDATE SET
              payload_json = processing_jobs.payload_json || EXCLUDED.payload_json,
              content_hash = EXCLUDED.content_hash,
              updated_at = now()
        """),
        {"aid": article_id, "ch": content_hash, "payload": payload}
    )

async def _build_entity_id_map(session, parsed):
    """把 parsed.entities 映射回 entities.id，返回 {(entity_type, canonical_name_zh): entity_id}
    
    ⚠ 必须同时索引 entity_type + canonical_name_zh：UNIQUE 是 (entity_type, canonical_name_zh)，
    同名不同类型（如「苹果」org vs「苹果」product）是不同 entity。
    """
    keys = [(e.get("type", "other"), e["canonical_name_zh"]) for e in parsed.get("entities", [])]
    if not keys:
        return {}
    # 用 row_to_tuple 避免 dict 覆盖同名不同类型
    r = await session.execute(
        text("SELECT id, entity_type, canonical_name_zh FROM entities "
             "WHERE (entity_type, canonical_name_zh) = ANY(:keys)"),
        {"keys": keys}
    )
    return {(row["entity_type"], row["canonical_name_zh"]): row["id"] for row in r.mappings()}

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
    - **可逆（`undedupe` 单事务，`§8.1`）**：`dedupe_of→NULL` + `status='pending'`（非 `processing`，`drain_queue` 仅捞 `pending`）+ 同事务 `DELETE article_topics` 重建 `match_keywords` + `winner mention_count -= loser_mention`（`GREATEST(1, ...)`）+ `enqueue(embed_core,summarize)`（`pending→processing` 由 `enqueue_jobs` 驱动）；CLI `tc article <id> undedupe`（P2 WebUI 按钮）
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

### 6.Z 中文翻译后台慢任务（Phase 2 新增，v0.15）

- **模型**：本地 `Qwen3.8-27B-MLX-4bit`（与 `summarize` 复用 27B，不单设轻量；`llm.translate.backend: omlx|openai` 可单设，但默认本地，不阻塞阅读）
- **触发**：`complete_summarize` 同事务 `if lang != 'zh' → enqueue_jobs([translate], priority=7)`（最低，`§6` 表）；`auto_translate` 可配，`lang==zh` 跳过
- **执行**：`run_translate(session,job,llm)` 读 `articles.content_text|content_md + title` → `get_prompt("translate")` → `generate (27B, 180s)` → `translations` upsert `UNIQUE(article_id,src_lang,tgt_lang,model) WHERE content_hash` + `check_and_set_done(task != 'translate')`；`processing_jobs` 长期 `queued` 亦不阻塞 `articles.status → done`（`check_and_set_done` 谓词 `WHERE task != 'translate'`，`§6` 状态机 `processing→done` 排除 `translate`，`§14 2.2` 清单已补）
- **展示**：`GET /articles/{id}` 查 `translations` 无行 → 返回 `translating: true`，前端徽标 `翻译中，请稍后` + `HTMX every 5s hx-get /api/articles/{id}/translate_status` 轮询；有行则 `translated_title/translated_content` 与原文双栏，`CLI tc article <id>` 同理
- **Worker**：`_TASK_CAPABILITY translate→generate`（`app/worker.py:31`），`handlers translate→run_translate`，`drain_queue` 不回灌 `translate`（仅 `embed_core/summarize`），`recover` 租约保护
- **配置**：`config/config.yaml: llm.translate {backend: omlx, model: Qwen3.8-27B-MLX-4bit}`，`TC_LLM__TRANSLATE__BACKEND` 可覆盖

---

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
| `POST /articles/{id}/undedupe` | redirect → `/articles/{id}` | path | 404 | `dedupe_of→NULL + status='pending'` + 重建 `article_topics` + 回退 `winner mention_count` + `enqueue(embed_core,summarize)`（`§6` 单事务，`drain_queue` 接手） |
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
    # 使用 ON CONFLICT DO UPDATE：同一 (report_type, period_start, period_end) 唯一（§5.1.5），
    # 重试/重新生成覆盖旧记录，不触发 UNIQUE 冲突
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

### 10.4 飞书 Webhook 机器人推送（Phase 2 新增，v0.15）

- **选型**：先走 **WebHook 机器人**（中国版 `open.feishu.cn`），`POST https://open.feishu.cn/open-apis/bot/v2/hook/{token}` 推送 Markdown 卡片，无需鉴权续期；正式 `im.message.create + drive` 上传预留（需 `app_id/app_secret` 换 `tenant_access_token`，可 @人/建文档）
- **配置**：`app/config.py:109` `FeishuSettings{enabled: bool=False, webhook_env: str="FEISHU_WEBHOOK", events: list[str]=["daily","weekly"]}`，全走环境变量（`FEISHU_WEBHOOK=https://open.feishu.cn/...`），`config/config.yaml: schedule.feishu {enabled, webhook_env}` 示例，`PRD §12` 外发白名单
- **服务**：`app/services/notify.py: send_feishu_markdown(webhook, title, markdown)` → `httpx POST {msg_type: "post", content: {post: {zh_cn: {title, content: [[{tag:"text",text:...}]]}}}}` 或 `interactive` 卡片，10s 超时，失败仅 `logger.warning` + `reports.error`，不写 `fetch_events`（`feed_id NOT NULL FK`，报告无 feed 上下文，同 `dedup` 已改 `logger`），`report_events` 表 Phase 2 预留
- **触发**：`reports.status → succeeded` 后链式 `if feishu.enabled and report_type in events → send_feishu_markdown`，或 `app/scheduler.py:253` 定时 `feishu_notify` 复用日报触发；`CLI tc report retry` 同链
- **安全**：`webhook` 含 `token` 走 `env` 不入库不入 repo，`open.feishu.cn` 中国版域名（国际 `open.larksuite.com` 可切），失败不阻塞报告生成

---

## 14. Phase 2 实施清单（v0.15 执行序，切片与优先级已对齐）

> **执行序（切片号即优先级序）**：`2.0 DB隔离 P0` → `2.1 WebUI P1` → `2.2 翻译 P2` → `2.3 实体 P2` → `2.4 图谱 P3` → `2.5 报告 P3` → `2.6 检索 P3`（含 `§4.8 embed/rerank` P1 部分提前至 `2.0` 后并行）→ `2.7 API P3` → `2.8 验收`；`10.4 飞书` 紧接 `2.5` 报告后（P3）。`2.0` 新增为 P0 前置，`2.1 WebUI` 不再隐含 DB 隔离，`P1 embed/rerank` 随 `2.0` 并行、代码量小。

**切片 2.0 生产 DB + 进程隔离（P0，v0.15 新增，最高）**：
- [ ] 2.0.1 `app/config.py:13` `DBSettings{env: dev|prod, prod_dsn}` + `TC_DB__PROD_DSN`/`POSTGRES_PASSKEY` fail fast，`config/config.yaml:6` `db.prod` 示例
- [ ] 2.0.2 `app/db/engine.py:25` `get_engine` 按 `env` 选 `dsn`，`check_extensions` prod 仍 `vector/pg_trgm`，`scripts/init_db.py:26` 本机 `postgres:5432` 幂等 `CREATE EXTENSION`
- [ ] 2.0.3 `docker-compose.yml:10` 保留 `5433 tc/tc` dev，`app/scheduler.py:218` `run_pg_backup` prod 走 `pg_dump -h localhost -U postgres PGPASSWORD`，`Makefile` 加 `make prod: TC_APP_ENV=prod`
- [ ] 2.0.4 初始化验证：`initdb` 空库 → `createdb topic_collection` → `alembic upgrade head` → `pytest 214` (=204+10 PR #40)，`TC_APP_ENV=prod uvicorn` 为 WebUI 唯一入口，`python -m app.worker` 仅 `dev`/CLI 批量，二者禁止同库并发
- [ ] D0 `a007` 迁移幂等 + `engine` 单测（`prod` 分支）

**切片 2.1 WebUI Dashboard 骨架（P1，验收 1 回归 + 2 部分 UI 触发，`prod` 唯一入口）**：
- [ ] 2.1.1 `app/main.py: create_app()` + lifespan 顺序：init_db → probe oMLX 三端点 → **池外长连接** `pg_try_advisory_lock` 单例校验（`asyncpg.connect` 直连，不经 `get_engine` 池，持有至 `lifespan` 退出 `pg_advisory_unlock`）→ `recover_interrupted` → 启动 scheduler + worker task；`uvicorn app.main:app --host 127.0.0.1 --port 7111`（与 `python -m app.worker` 互斥，`§5.4.1`）
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
- [x] 2.3.1 §5.1.5 entities / relations / article_entities DDL 增量迁移（含 pg_trgm 扩展）——已落地 `a004`/`a006`，`§5.1.5` 为历史快照
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
- [x] 2.5.1 §5.1.5 reports.status / started_at / completed_at / error 列 DDL 增量；`reports_period_uniq` UNIQUE 索引——已落地 `a004`
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
- [x] 2.6.3a §5.1.5 `wiki_pages.tsv` 加列 + GIN + `update_wiki_tsv` + backfill——已落地 `a003`（`fix #6`）
- [ ] 2.6.3b §7.1 Wiki 跨表 UNION RRF 融合——`app/services/search.py:search()` 四集合 UNION → RRF 统一分页/score（废 `score 0.0` 附加），`app/db/fts.py:search_wiki_fts` 仅作子查询，`D10` 跨表排序
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
- [ ] 2.8.6 ≥ 214 + N 新测试全部通过（D7–D15，基线 214=204+10 PR #40，原 162+N 已滞后两轮），CI 全绿

> **v0.15 增量与优先级说明（与 §14 执行序一致，切片号即优先级序）**：
> - **P0 `§5.4.1` 生产 DB 隔离（`2.0`）**：`dev 5433 tc/tc` vs `prod 5432 postgres/${POSTGRES_PASSKEY}`，`TC_APP_ENV=prod` 可单独启动，需初始化（最高，阻塞生产，`2.1 WebUI` 不再隐含）
> - **P1 `2.1 WebUI` + `§4.8` `embed`/`rerank` 外部化（`2.6` 前置并行）**：WebUI 为 Phase 2 入口，`embed` 小改解锁检索/成本，二者可并行于 `2.0` 后
> - **P2 `§6.Z` 翻译后台（`2.2`）+ `2.3 实体`/`2.4 图谱`**：翻译本地 27B 慢后台，原文可读；实体为图谱前置
> - **P3 `2.5 报告` → `10.4 飞书` → `2.6 检索` → `2.7 API` → `2.8 验收`**：飞书紧接报告后，检索含 `rerank` 已在 P1 部分完成

---

## 17. 文档元约定（Phase 2 新增）

- **结构性内容只在一处维护**：PRD §11 已锁定本原则（消除副本漂移）；DESIGN.md 与 CLAUDE.md 互引不重复
- **Phase 2 实施细节以本文件 §8 / §10.1 / §10.2 / §10.3 / §6.X / §7.1 为权威**；任何 PRD 与本文件冲突以本文件为准（PRD 是产品合同，DESIGN 是工程蓝图；以"实现上 PRD 可被调整"原则落地）
- **章节交叉引用规范**：本文用 `§X.Y` 引节、`§X` 引节首；引 PRD 用 `PRD §X`；引 PRD #X 引验收条目
- **未来追加的 P3 任务**：新增 P3 段而非塞入既有 Phase 2 切片，保持 §14 切片粒度一致
