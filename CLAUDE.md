# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态：文档驱动，尚未实现

`topic_collection` 是一个**主题信息聚合 + 个人知识库**系统（采集 RSS/API → 本地 LLM 摘要/嵌入 → 可搜索 Wiki + 知识图谱）。当前仓库**只有设计文档，没有任何应用代码**（Python 3.14 venv 为空）。实现工作尚未开始。

**先读这两份文档再动手**（文档用中文撰写，新增文档/注释沿用中文）：
- `docs/PRD.md` —— 产品需求（做什么、阶段划分、验收标准）
- `docs/DESIGN.md` —— **权威实现蓝图**（表 DDL、LLM Provider 接口、流水线状态机、配置 schema、Phase 1 任务清单 §14）。任何实现都必须与 DESIGN.md 一致。

## 已冻结的技术决策（不要重新讨论）

| 项 | 决策 |
|---|---|
| 阶段 | **Phase 1 = CLI 入口、无 WebUI、可用即可**；WebUI Dashboard 移入 Phase 2 |
| 数据库 | Docker Compose 起 `pgvector/pgvector:pg17`，`127.0.0.1:5432`，`tc/tc`，`CREATE EXTENSION vector`（见 DESIGN §5.4） |
| 本地 LLM | oMLX @ `http://localhost:8000`，**不鉴权**（已实测确认，不发 Authorization 头） |
| 生成模型 | `Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16-mlx-4Bit`（thinking 风格，必须用 json_mode 拿结构化输出） |
| 嵌入模型 | `Qwen3-Embedding-8B-4bit-DWQ`，输出 **4096 维**，DDL 用 `vector(4096)` + HNSW |
| 重排模型 | `Qwen3-Reranker-4B-mxfp8`，Phase 2 用 |
| 检索 | 混合：jieba 预切词写入 `tsvector('simple')` + GIN（关键词）∪ pgvector HNSW（语义） |
| 订阅源 | 独立文件 `config/feeds.yaml`（加源只改这一个文件）→ `tc feeds import` 幂等 upsert 进 DB `feeds` 表 |
| 输出语言 | 中文 |

注意：`Qwen3.5-9B-Claude-4.6-HighIQ-THINKING-HERETIC-UNCENSORED` 在 oMLX 上**加载失败**（Missing 154 parameters），不可用作生成模型。

## 架构要点（详见 DESIGN.md）

- **单进程全异步**：FastAPI + httpx + SQLAlchemy 2 async（asyncpg）。队列 = Postgres 表 `processing_jobs`，worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取，无 Redis/Celery。
- **Services 层是应用 API**：`app/services/` 承载全部业务逻辑；CLI（Phase 1）与 WebUI（Phase 2）都只是薄封装——新功能写进 services，不要写进 CLI/路由。
- **数据流水线**：fetch → normalize → dedup（url_hash/content_hash，LLM 之前）→ clean → 入队 processing_jobs → LLM 各阶段（summarize/embed/topics/wiki）→ 索引。增量处理，产物按 `(article, task, model, content_hash)` 缓存。
- **LLM Provider 三能力**：`generate`（`POST /v1/chat/completions`，json_mode）、`embed`（`POST /v1/embeddings`）、`rerank`（`POST /v1/rerank`，Cohere 风格，P2）。
- **oMLX 实测事实**（DESIGN §15）：嵌入 4096 维、rerank 入参 `query/documents/top_n`、json_mode 有效、无鉴权可用。

## 计划命令（尚未实现，来自 DESIGN.md）

- `docker compose up -d` 起 Postgres；`python -m scripts.init_db` 建表+建扩展
- CLI（typer，Phase 1 主入口）：`tc feeds import` / `tc topic add` / `tc topic list` / `tc fetch` / `tc summarize` / `tc list` / `tc search` / `tc article <id>`

## 环境

- macOS Apple Silicon，Python 3.14（`project/.venv`）
- Docker（开发数据库）；oMLX 常驻 `localhost:8000` 提供三个本地模型
- 凭据一律走环境变量，不入库不入 repo（当前 LLM 不鉴权，无 token 需要）
