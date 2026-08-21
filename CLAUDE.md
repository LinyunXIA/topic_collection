# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态：Phase 1/1+/1++ + Phase 2 P0/P1 完成，226/226 tests passing

`topic_collection` 是一个**主题信息聚合 + 个人知识库**系统（采集 RSS/API → 本地 LLM 摘要/嵌入 → 可搜索 Wiki + 知识图谱）。**切片一（端到端闭环）、切片二（混合检索）、切片三（主题+Wiki）+ 横切（scheduler + 测试 + 验收）+ Phase 1+（外部 LLM API + 适配器层 + fetch --count）+ Phase 2 P0（DB 隔离 + advisory lock + 飞书推送 + rerank/embed 外部化 + enqueue 合并）+ Phase 2 P1（prompt 约束 + slug + related_json + 详情 Tab + wiki 去重 + 健康横幅 + 列表筛选 + feeds config_json + settings per-capability + reindex wiki + P2 边界）已全部完成**，226/226 tests passing（`pytest tests/ -q`），真实环境 20 篇 HN 文章端到端跑通，MiniMax-M3 外部 API 通讯验证通过。**Phase 1 MVP + Phase 1+ + Phase 2 P0/P1 全部实现**，PRD §15 验收 1/2/3/4/5/6/7/8/9/16/17/18 全部通过（2/4/6 为 Phase 2 新增）。

测试计数核对方式：`pytest tests/ --collect-only -q | tail -1` 应输出 `226 tests collected`。

**先读这两份文档再动手**（文档用中文撰写，新增文档/注释沿用中文）：
- `docs/PRD.md` —— 产品需求（做什么、阶段划分、验收标准）
- `docs/DESIGN.md` —— **权威实现蓝图**（表 DDL、LLM Provider 接口、流水线状态机、配置 schema、Phase 1 任务清单 §14）。任何实现都必须与 DESIGN.md 一致。

## 已冻结的技术决策（不要重新讨论）

| 项 | 决策 |
|---|---|
| 阶段 | **Phase 1 = CLI 入口、无 WebUI、可用即可**；WebUI Dashboard 已落地 Phase 2（`app/api` + `app/web`，`uvicorn app.main:app --host 127.0.0.1 --port 7111`） |
| 数据库 | Docker Compose 起 `pgvector/pgvector:pg17`，`127.0.0.1:5433`（宿主机 5433 避免与本地 PG 冲突），`tc/tc`，`CREATE EXTENSION vector`（见 DESIGN §5.4）；**生产隔离**：`TC_APP_ENV=prod` 走本机 `5432 postgres/${POSTGRES_PASSKEY}`，`TC_DB__PROD_DSN` 可覆盖 |
| 本地 LLM | oMLX @ `http://localhost:8000`，**不鉴权**（已实测确认，不发 Authorization 头） |
| 生成模型 | `Qwen3.8-27B-MLX-4bit`（thinking 风格，必须用 json_mode 拿结构化输出） |
| 嵌入模型 | `Qwen3-Embedding-8B-4bit-DWQ`，输出 **1536 维**，DDL 用 `vector(1536)` + HNSW，**外部化可选** `backend: openai` + `dimensions=1536` |
| 重排模型 | `Qwen3-Reranker-4B-mxfp8`，**外部化可选**，外部暂抛 `ValueError` 提示回退 `omlx` |
| 检索 | 混合：jieba 预切词写入 `tsvector('simple')` + GIN（关键词）∪ pgvector HNSW（语义），**RRF 融合** `1/(k+rank)` k=60，**wiki 去重按 `ref_id` 非 `id`** |
| 语言检测 | `lingua-language-detector`（纯 Python，75 语言；pycld3 需 protobuf 编译器在 3.14 下安装失败） |
| 订阅源 | 独立双文件 `config/feeds.dev.yaml` / `feeds.prod.yaml`（`TC_FEEDS_CONFIG` 可覆盖）+ `feeds.yaml` 兼容 → `tc feeds import` 按 `(url, env)` upsert，`config_json` 支持 API/scrape 类型 |
| 输出语言 | 中文 |
| 共享出口 | `app/core/egress.py` 白名单（`open.feishu.cn`/`open.larksuite.com`/`api.openai.com` 等），`safe_post` 校验，飞书/外部 LLM 唯一出口 |
| 进程单例 | `pg_try_advisory_lock(hashtext('topic_collection_worker'))` 池外长连接持有至退出，未获锁 `sys.exit(1)`，`force recover` 单实例锁定 |

注意：`Qwen3.5-9B-Claude-4.6-HighIQ-THINKING-HERETIC-UNCENSORED` 在 oMLX 上**加载失败**（Missing 154 parameters），不可用作生成模型。

## 架构要点（详见 DESIGN.md）

- **单进程全异步**：FastAPI + httpx + SQLAlchemy 2 async（asyncpg）。队列 = Postgres 表 `processing_jobs`，worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取，无 Redis/Celery。
- **Services 层是应用 API**：`app/services/` 承载全部业务逻辑；CLI（Phase 1）与 WebUI（Phase 2）都只是薄封装——新功能写进 services，不要写进 CLI/路由。
- **数据流水线**：fetch → normalize → dedup（url_hash/content_hash，LLM 之前）→ clean → 入队 processing_jobs → LLM 各阶段（summarize/embed/topics/wiki）→ 索引。增量处理，产物按 `(article, task, model, content_hash)` 缓存。
- **LLM Provider 三能力**：`generate`（`POST /v1/chat/completions`，json_mode）、`embed`（`POST /v1/embeddings`）、`rerank`（`POST /v1/rerank`，Cohere 风格，P2）。
- **oMLX 实测事实**（DESIGN §15）：嵌入 1536 维、rerank 入参 `query/documents/top_n`、json_mode 有效、无鉴权可用。
- **Phase 2 增量**：`app/core/egress.py` 白名单外发、`app/services/notify.py` 飞书 `safe_post`、`app/api` 三端点健康探测、`app/services/wiki.py` `related_json` 三源合并 + `slug` 分 kind、`app/api/articles.py` 详情 7 Tab + 列表筛选。

## 实现状态（切片进度）

| 切片 | 内容 | 状态 | 测试 |
| :--- | :--- | :--- | ---: |
| **切片一** | 脚手架 + DB + LLM + Ingest + Pipeline + CLI | ✅ 完成 | 48 passed |
| **切片二** | 混合检索（RRF 融合 + CLI --mode） | ✅ 完成 | +12 passed |
| **切片三** | 主题 CRUD + classify_topics + Wiki 词条 | ✅ 完成 | +15 passed |
| **横切** | scheduler + A1 重试分类 + B4 近似去重 + 验收 | ✅ 完成 | +11 passed |
| **Phase 1+** | 外部 LLM API 切换 + 适配器层 + fetch --count + 重试分类修复 | ✅ 完成 | +62 passed |
| **Day 1** | 备份脚本 + tc backup | ✅ 完成 | — |
| **Bugfix** | generate.model fallback regression（worker 切 minimax 后 services 仍用顶层 llm.model） | ✅ 完成 | +2 passed |
| **Bugfix** | worker 死锁三连修：①recover_interrupted force_all_running 启动期抢锁 ②worker_loop 60s 周期 recover ③任务处理 lease 后台续租（renew_lease 真正接入） | ✅ 完成 | +6 passed |
| **Bugfix** | topics/wiki handler 缺位（worker 只打 warning 跳过，job 卡 running）+ article 状态机缺 pending→processing（done 永远不触发） | ✅ 完成 | +6 passed |
| **Bugfix** | topics.name 缺 UNIQUE 约束（create_topic 静默建重复行） | ✅ 完成 | +2 passed |
| **Tech debt** | 抓取逻辑分叉抽 fetch_and_store + api_key_env 统一 + GenerateSettings.models 删除 + ORM vector 占位注释 + 文档同步（fix #9） | ✅ 完成 | +5 passed |
| **Cleanup** | 删 _classify_http_error + TransientError 死代码（fix #12） | ✅ 完成 | -6 passed |
| **Schema** | wiki_pages 加 tsv 列 + GIN + jieba backfill + 全文搜索替代 ILIKE（fix #6） | ✅ 完成 | +4 passed |
| **Schema** | Phase 2 五表预创建（translations/entities/article_entities/relations/reports，fix #5） | ✅ 完成 | +10 passed |
| **Bugfix** | scheduler 5 个定时任务用同步 lambda + ensure_future 包装，APScheduler 丢进线程池抛 RuntimeError，任务体从未执行（fix #30） | ✅ 完成 | +1 passed |
| **P0** | advisory lock 单例 `pg_try_advisory_lock` 池外长连接 + `sys.exit(1)`（fix #42，`app/main.py` + `app/worker.py`） | ✅ 完成 | — |
| **P0** | 飞书 Webhook 推送（fix #43，`app/core/egress.py` + `app/services/notify.py` + `reports.py` 同事务 `send_feishu_markdown`） | ✅ 完成 | — |
| **P0** | rerank 外部化（fix #44，`factory` 支持 `backend: openai`，外部抛 `ValueError` 回退 `omlx`） | ✅ 完成 | — |
| **P0** | embed 外部化补充 `dimensions=1536`（fix #45，`OPENAI_PATCH/OPENAI_EMBED_PATCH`） | ✅ 完成 | +1 passed |
| **P1** | entity/topic wiki 入队 payload 合并（fix #46，`pipeline.py` `enqueue_*_wiki` + `main/worker` 占位替换） | ✅ 完成 | — |
| **P1** | 实体抽取 prompt 约束 `canonical_name_zh`（fix #47，`prompts.py` `extract_entities` + `generate_report`） | ✅ 完成 | — |
| **P1** | wiki slug 分 kind（fix #48，`topic-`/`entity-`/`manual`） | ✅ 完成 | — |
| **P1** | related_json 三源合并（fix #49，`build_related_json` topic/entity/feed → top10） | ✅ 完成 | — |
| **P1** | 详情 7 Tab 补 entities/topics/wiki（fix #50，`api/articles.py` + `detail.html`） | ✅ 完成 | — |
| **P1** | wiki 去重集成测试（fix #51，`test_search.py` `TestWikiDedup` 按 `ref_id`） | ✅ 完成 | +1 passed |
| **P1** | 健康横幅三端点（fix #53，`api/health.py` `generate/embed/rerank` + `health_banner.html`） | ✅ 完成 | — |
| **P1** | 列表筛选 feed/topic/status/q（fix #54，`api/articles.py` 动态 WHERE + `list.html`/`article_row.html`） | ✅ 完成 | — |
| **P1** | feeds config_json（fix #55，`api/feeds.py` + `edit.html` API/scrape 模板） | ✅ 完成 | — |
| **P1** | settings per-capability（fix #56，`api/settings.py` + `page.html` 4 段） | ✅ 完成 | — |
| **P1** | reindex 回填 wiki（fix #57，`cli.py` `--wiki` + `--all` 双表） | ✅ 完成 | — |
| **P2** | 细节与边界（fix #58，`pipeline.py` force recover 文档；#53/#57 已单独） | ✅ 完成 | — |
| **Test** | 回归测试预期更新（fix #44/#45，`test_adapter`/`test_openai_provider`） | ✅ 完成 | +1 passed |

**测试合计：226 passed**（204 + 0 + 0 + 1 + 0 + 0 + 1 + 1 + 1 = 226，`--collect-only` 226）

## 项目结构

```
app/
├── config.py          # pydantic-settings 加载配置
├── main.py            # FastAPI create_app() + lifespan（advisory lock + worker + scheduler）
├── worker.py          # Phase 1 入口：worker task + 信号处理（advisory lock）
├── pipeline.py        # 队列入队 + Worker（SKIP LOCKED）+ 重试分类 + recover + enqueue_*_wiki
├── scheduler.py       # 定时任务：fetch_all + drain_queue + pg_backup + healthcheck
├── core/
│   └── egress.py      # 共享出口白名单 safe_post/safe_get（PRD §12）
├── db/
│   ├── engine.py      # async engine + 扩展/维度校验
│   ├── models.py      # 15 张表 ORM 模型（Phase 2 含 translations/entities/article_entities/relations/reports）
│   ├── fts.py         # jieba 预切词 + tsvector 维护（article + wiki）
│   └── migrations/    # Alembic（001_initial_schema + a003-a007）
├── llm/
│   ├── base.py        # LLMProvider Protocol + 类型
│   ├── patches.py     # ProviderPatch（OMLX/OPENAI/MINIMAX/DEEPSEEK + OPENAI_EMBED_PATCH）
│   ├── adapter.py     # LLMAdapter（统一适配层，80% 通用 OpenAI 逻辑）
│   ├── omlx.py        # oMLX OpenAI 兼容实现（HTTP 传输壳）
│   ├── openai.py      # OpenAI 兼容外部 API（HTTP 传输壳）
│   ├── client.py      # LLMClient（并发/重试/健康，含 rerank 降级）
│   ├── factory.py     # per-capability provider factory（generate/embed/rerank）
│   ├── prompts.py     # 中文提示词模板（summarize/translate/extract_entities/generate_report）
│   ├── structured.py  # JSON parse + repair
│   └── fake.py        # FakeLLMProvider（开发/测试用）
├── ingest/
│   ├── base.py        # FeedItem 数据类
│   ├── feeds.py       # RSS 抓取（ETag/304 + 每域限速）
│   ├── api.py         # API 连接器（P2 骨架，config_json 驱动）
│   └── dedup.py       # url_hash + content_hash（sha256）
├── services/
│   ├── cleaner.py     # HTML→Markdown（trafilatura）+ 语言检测（lingua）
│   ├── llm_tasks.py   # summarize/translate/entities/topics/wiki/embed + complete 钩子
│   ├── entities.py    # 实体合并/关系（P2）
│   ├── topics.py      # 主题 CRUD + 关键词匹配 + classify_topics LLM 慢路径
│   ├── wiki.py        # 文章词条生成（Markdown + related_json 三源 + slug 分 kind）
│   ├── search.py      # 混合检索（RRF 融合 + rerank + wiki ref_id 去重）
│   ├── graph.py       # 图谱 ECharts JSON（P2）
│   ├── reports.py     # 日报/周报 + 飞书推送 _maybe_notify_feishu
│   ├── notify.py      # 飞书 Webhook 推送（PRD §10.4）
│   └── cli.py         # CLI 入口（feeds/summarize/list/search/article/topic/status/retry/backup/reindex）
├── api/               # Phase 2 WebUI 路由
│   ├── deps.py        # session/settings/llm 依赖注入
│   ├── health.py      # /api/health + /api/llm-status 三端点
│   ├── articles.py    # 列表筛选 + 详情 7 Tab + similar
│   ├── feeds.py       # feed CRUD（含 config_json）
│   ├── settings.py    # per-capability 设置页
│   └── ...            # dashboard/wiki/search/graph/topics/reports
└── web/
    ├── templates/     # Jinja2（base + articles/list/detail + feeds/edit + settings/page + health_banner）
    └── static/        # htmx/echarts/pico（vendored）
```

## CLI 命令（已实现）

```bash
# 环境启动
docker compose up -d                  # 起 Postgres（port 5433）
python -m scripts.init_db             # CREATE EXTENSION vector + alembic upgrade head

# 订阅源管理
tc feeds import                       # feeds.yaml → DB（幂等 upsert，env 隔离）
tc feeds fetch                        # 抓取所有 enabled feed（env 过滤）
tc feeds fetch --name "Hacker News"   # 抓取指定 feed
tc feeds fetch -c 3 --name "HN"       # 只抓前 3 条（--count 限制条数）

# 文章处理
tc summarize <article_id>             # 重新生成摘要（走 complete_summarize 钩子）
tc list [--topic <name>]              # 文章列表
tc article <id>                       # 文章详情 + 摘要

# 搜索
tc search "关键词"                    # 混合检索（默认 hybrid）
tc search "关键词" --mode semantic    # 纯语义
tc search "关键词" --mode keyword     # 纯关键词

# 主题管理
tc topic add --name "AI" --keywords "人工智能,LLM,机器学习"
tc topic list
tc list --topic "AI"                  # 按主题筛选文章

# 系统管理
tc status                             # 队列深度 / 失败任务 / LLM 健康
tc retry <article_id> <task>          # 重试指定任务
tc backup                             # pg_dump | gzip 备份
tc reindex --all                      # 重建 articles + wiki tsv（默认全量）
tc reindex --wiki                     # 仅回填 wiki_pages.tsv

# Worker & WebUI
make worker                           # 启动 worker（常驻消费队列，advisory lock）
uvicorn app.main:app --host 127.0.0.1 --port 7111  # 启动 WebUI（与 worker 互斥）
```

## 运行测试

```bash
pytest tests/ -q                      # 226 passed
pytest tests/ --collect-only -q       # 226 tests collected
```

测试需要 Docker Postgres 运行（`docker compose up -d`）。

## 开发中已修复的已知坑

1. **`get_prompt` dict 惰性求值**：Python dict 字面量立即计算所有 `.format()`，导致无关模板 KeyError → 改为先查后 format
2. **asyncpg `:vec::vector`**：`::` 被 asyncpg 误解析 → 改为 `CAST(:vec AS vector)`
3. **Alembic asyncpg DSN**：async 驱动在 Alembic 中报 `MissingGreenlet` → env.py 用同步驱动 psycopg2
4. **pytest asyncpg event loop**：每个测试不同 loop → `asyncio_default_fixture_loop_scope = "session"`
5. **GIN 索引**：`postgresql.GIN()` 不存在 → `postgresql_using="gin"`

## 环境

- macOS Apple Silicon，Python 3.14（`.venv`）—— 依赖已验证可安装
- Docker（开发数据库，port 5433）；oMLX 常驻 `localhost:8000` 提供三个本地模型
- 凭据一律走环境变量，不入库不入 repo（当前 LLM 不鉴权，无 token 需要）
