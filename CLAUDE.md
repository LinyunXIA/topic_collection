# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态：Phase 1 MVP + Phase 1+ 完成，116/116 tests passing

`topic_collection` 是一个**主题信息聚合 + 个人知识库**系统（采集 RSS/API → 本地 LLM 摘要/嵌入 → 可搜索 Wiki + 知识图谱）。**切片一（端到端闭环）、切片二（混合检索）、切片三（主题+Wiki）+ 横切（scheduler + 测试 + 验收）+ Phase 1+（外部 LLM API + fetch --count）已全部完成**，116/116 tests passing，真实环境 20 篇 HN 文章端到端跑通。**Phase 1 MVP + Phase 1+ 全部实现**，PRD §15 验收 1/3/5/7/8/9/16/17/18 全部通过。

**先读这两份文档再动手**（文档用中文撰写，新增文档/注释沿用中文）：
- `docs/PRD.md` —— 产品需求（做什么、阶段划分、验收标准）
- `docs/DESIGN.md` —— **权威实现蓝图**（表 DDL、LLM Provider 接口、流水线状态机、配置 schema、Phase 1 任务清单 §14）。任何实现都必须与 DESIGN.md 一致。

## 已冻结的技术决策（不要重新讨论）

| 项 | 决策 |
|---|---|
| 阶段 | **Phase 1 = CLI 入口、无 WebUI、可用即可**；WebUI Dashboard 移入 Phase 2 |
| 数据库 | Docker Compose 起 `pgvector/pgvector:pg17`，`127.0.0.1:5433`（宿主机 5433 避免与本地 PG 冲突），`tc/tc`，`CREATE EXTENSION vector`（见 DESIGN §5.4） |
| 本地 LLM | oMLX @ `http://localhost:8000`，**不鉴权**（已实测确认，不发 Authorization 头） |
| 生成模型 | `Qwen3.8-27B-MLX-4bit`（thinking 风格，必须用 json_mode 拿结构化输出） |
| 嵌入模型 | `Qwen3-Embedding-8B-4bit-DWQ`，输出 **1536 维**，DDL 用 `vector(1536)` + HNSW |
| 重排模型 | `Qwen3-Reranker-4B-mxfp8`，Phase 2 用 |
| 检索 | 混合：jieba 预切词写入 `tsvector('simple')` + GIN（关键词）∪ pgvector HNSW（语义），**RRF 融合** `1/(k+rank)` k=60 |
| 语言检测 | `lingua-language-detector`（纯 Python，75 语言；pycld3 需 protobuf 编译器在 3.14 下安装失败） |
| 订阅源 | 独立文件 `config/feeds.yaml`（加源只改这一个文件）→ `tc feeds import` 幂等 upsert 进 DB `feeds` 表 |
| 输出语言 | 中文 |

注意：`Qwen3.5-9B-Claude-4.6-HighIQ-THINKING-HERETIC-UNCENSORED` 在 oMLX 上**加载失败**（Missing 154 parameters），不可用作生成模型。

## 架构要点（详见 DESIGN.md）

- **单进程全异步**：FastAPI + httpx + SQLAlchemy 2 async（asyncpg）。队列 = Postgres 表 `processing_jobs`，worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取，无 Redis/Celery。
- **Services 层是应用 API**：`app/services/` 承载全部业务逻辑；CLI（Phase 1）与 WebUI（Phase 2）都只是薄封装——新功能写进 services，不要写进 CLI/路由。
- **数据流水线**：fetch → normalize → dedup（url_hash/content_hash，LLM 之前）→ clean → 入队 processing_jobs → LLM 各阶段（summarize/embed/topics/wiki）→ 索引。增量处理，产物按 `(article, task, model, content_hash)` 缓存。
- **LLM Provider 三能力**：`generate`（`POST /v1/chat/completions`，json_mode）、`embed`（`POST /v1/embeddings`）、`rerank`（`POST /v1/rerank`，Cohere 风格，P2）。
- **oMLX 实测事实**（DESIGN §15）：嵌入 1536 维、rerank 入参 `query/documents/top_n`、json_mode 有效、无鉴权可用。

## 实现状态（切片进度）

| 切片 | 内容 | 状态 | 测试 |
|---|---|---|---|
| **切片一** | 脚手架 + DB + LLM + Ingest + Pipeline + CLI | ✅ 完成 | 48 passed |
| **切片二** | 混合检索（RRF 融合 + CLI --mode） | ✅ 完成 | +12 passed |
| **切片三** | 主题 CRUD + classify_topics + Wiki 词条 | ✅ 完成 | +15 passed |
| **横切** | scheduler + A1 重试分类 + B4 近似去重 + 验收 | ✅ 完成 | +11 passed |
| **Phase 1+** | 外部 LLM API 切换 + fetch --count + 重试分类修复 | ✅ 完成 | +30 passed |
| Day 1 | 备份脚本 + tc backup | ✅ 完成 | — |
| | | **合计** | **116 passed** |

## 项目结构

```
app/
├── config.py          # pydantic-settings 加载配置
├── worker.py          # Phase 1 入口：worker task + 信号处理
├── pipeline.py        # 队列入队 + Worker（SKIP LOCKED）+ 重试分类 + recover
├── scheduler.py       # 定时任务：fetch_all + drain_queue + pg_backup + healthcheck
├── db/
│   ├── engine.py      # async engine + 扩展/维度校验
│   ├── models.py      # 10 张表 ORM 模型
│   ├── fts.py         # jieba 预切词 + tsvector 维护
│   └── migrations/    # Alembic（001_initial_schema）
├── llm/
│   ├── base.py        # LLMProvider Protocol + 类型
│   ├── omlx.py        # oMLX OpenAI 兼容实现
│   ├── client.py      # LLMClient（并发/重试/健康）
│   ├── prompts.py     # 中文提示词模板
│   ├── structured.py  # JSON parse + repair
│   └── fake.py        # FakeLLMProvider（开发/测试用）
├── ingest/
│   ├── base.py        # FeedItem 数据类
│   ├── feeds.py       # RSS 抓取（ETag/304 + 每域限速）
│   └── dedup.py       # url_hash + content_hash（sha256）
└── services/
    ├── cleaner.py     # HTML→Markdown（trafilatura）+ 语言检测（lingua）
    ├── llm_tasks.py   # summarize/embed_core/embed_summary + complete 钩子
    ├── topics.py      # 主题 CRUD + 关键词匹配 + classify_topics LLM 慢路径
    ├── wiki.py        # 文章词条生成（Markdown + related_json）
    ├── search.py      # 混合检索（RRF 融合）
    └── cli.py         # CLI 入口（feeds/summarize/list/search/article/topic/status/retry/backup）
```

## CLI 命令（已实现）

```bash
# 环境启动
docker compose up -d                  # 起 Postgres（port 5433）
python -m scripts.init_db             # CREATE EXTENSION vector + alembic upgrade head

# 订阅源管理
tc feeds import                       # feeds.yaml → DB（幂等 upsert）
tc feeds fetch                        # 抓取所有 enabled feed
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

# Worker
make worker                           # 启动 worker（常驻消费队列）
```

## 运行测试

```bash
pytest tests/ -v                      # 86 tests, ~2.9s
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
