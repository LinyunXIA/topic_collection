# 技术设计文档 — Topic Collection

> 关联文档：[PRD.md](PRD.md)（产品需求——产品范围/验收的权威；本文件为工程实现权威）
> 共享的结构性描述（目录结构 / DDL / 接口）只在一处维护、另一处引用，避免漂移
> 版本：v0.14 · 2026-08-20 · Phase 1/1+/1++ 已部署（13 项 Issue 闭环，204/204 tests，Phase 2 已拆分至 DESIGN_Phase_2.md）
> v0.14：**代码与设计对齐 + 新增 5 项回归修复**——与当前代码（204/204 tests passing, `pytest --collect-only` 204）对齐，`gh issue --state open` 0：
>   **P0 阻断 2 项**——① `app/scheduler.py:251` 直注协程函数（`fix #30` 去 `lambda: ensure_future`，APScheduler 线程池 `RuntimeError: no current event loop` 导致 5 任务永不执行）；② `app/services/search.py:170` `DISTINCT ON + ORDER BY article_id` 改 `ORDER BY distance` 全局相似度 + 应用层去重（`fix #31`，`PRD 9` 按 id 选结果，HNSW 失效，RRF 污染）；
>   **P1/P2 3 项**——③ `app/ingest/dedup.py:44` 空/过短短路（`_EMPTY_CONTENT_HASH` + `<32`）+ 30d 窗口（`fix #32`，空正文 feed 全吞为首篇）；④ `app/scheduler.py:114` `drain_queue` `UPDATE ... RETURNING id` 限定本轮 `ANY(:ids)`（`fix #33`，全表 `processing` 越界每 24h 重入队）；⑤ `app/services/cli.py:618` `tc reindex [--all]` 纯本地 `update_article_tsv` 回填存量 `NULL`（`fix #34`，`a003` 仅 wiki 回填）；
>   同步更新：`DESIGN.md:5` 版本 200→204；`§5.1.5` `a005/a006` 已合入 `task CHECK`/`pg_trgm`；`§7` 检索 `ORDER BY distance` 替代 `DISTINCT ON`。
> **2026-08-20 拆分**：Phase 2 及之后设计（含 §14 Phase 2 清单）已移至 [DESIGN_Phase_2.md](DESIGN_Phase_2.md)，本文件仅保留 Phase 1/1+/1++（CLI 入口，无 WebUI，可用即可）设计，204/204 tests，0 OPEN。
> **2026-08-20 更新**：Phase 2 实施清单移至 DESIGN_Phase_2.md `§14`，并按 v0.15 优先级重排（生产 DB 隔离 P0 最高）。

> v0.13：**代码与设计对齐 + GitHub Issue 8 项闭环**——与当时代码（200/200 tests passing, `pytest --collect-only` 200）对齐：
>   **P0 阻断 3 项**——① `app/worker.py:150` 装配 `setup_scheduler`（`app/scheduler.py:209` 新增 `AsyncIOScheduler` 工厂，`fetch_all/drain_queue/healthcheck/pg_backup/cleanup_fetch_events` 同 loop 常驻，`DESIGN.md:1292` §6 运维模式兑现）；② `app/scheduler.py:131` `cleanup_fetch_events` 修复 `INTERVAL ':days days'` 字面量绑定为 `f"INTERVAL '{int(days)} days'"`（与 `reclassify_recent` 同类修复）；③ `app/scheduler.py:173` `run_pg_backup` 改 `await asyncio.to_thread(subprocess.run, ...)` 防阻塞事件循环；
>   **P1 Schema 3 项**——④ `app/db/models.py:282` + `a004_phase2_tables.py` 预创建 `translations/entities/article_entities/relations/reports` + `pg_trgm`（`§5.1` 承诺兑现）；⑤ `a003_wiki_tsv.py` + `app/db/models.py:259` `wiki_pages.tsv + GIN` + `app/db/fts.py:121` `update_wiki_tsv` + `app/services/search.py:232` `ILIKE→tsv @@ websearch_to_tsquery`；⑥ `a004` 扩展 `processing_jobs.task CHECK` 至 9 值（`extract_entities/generate_entity_wiki/generate_topic_wiki`，`§5.1.5`）；
>   **P2 健壮 2 项**——⑦ `app/ingest/service.py:33` 收敛 `fetch_all`/`_feeds_fetch` 至 `fetch_and_store` 单一实现 + `app/ingest/dedup.py:apply_exact_dedup` 补 `content_hash` 第二闸（`§6` 精确去重闭环）；⑧ `app/config.py:34` `GenerateSettings.models` 删除（`fix #9.3` 单一真源 `LLMSettings.models`）+ `api_key`→`api_key_env` 统一 + `app/db/models.py:148` vector/tsv 占位注释；
>   同步更新：`DESIGN.md:9` 配置 schema 与 `config.yaml` 一致；`§10` 调度表与 `setup_scheduler` 触发器一致；测试计数 148→200。
> v0.12：**Phase 2 蓝图架构审查修订**——切片 2.3 实体落库伪代码全面修正：
>   **严重 1（relations 命名空间）**——§4.6.1 relations schema 示例 `subject/object` 改为 `canonical_name_zh`（原用英文 `name` 与 `_build_entity_id_map` 映射对不上，所有关系静默跳过）；§6.Y 加 `name_to_eid` 反向映射 + prompt 约束注释；
>   **严重 2（_build_entity_id_map 缺 entity_type 过滤）**——映射键改为 `(entity_type, canonical_name_zh)` 二元组，查询 `WHERE (entity_type, canonical_name_zh) = ANY(:keys)`，防止同名不同类型（「苹果」org vs product）混淆；
>   **严重 3（entity/topic 任务队列身份错配）**——`generate_entity_wiki` 入队改为 `ON CONFLICT DO UPDATE` 合并 `payload_json.entity_ids`（原 `DO NOTHING` 静默丢弃同文章后续实体）；新增 `enqueue_entity_wiki()` 函数 + payload 合并策略说明；`generate_topic_wiki` 同理声明；
>   **中等 4（mention_count 重跑翻倍）**——改为从 `article_entities` 聚合 `COUNT(*)`（非每次 extract +1）；
>   **中等 5（JSONB `||` 拼接不去重）**——`aliases_json` 和 `source_articles_json` 都改用 `jsonb_array_elements` + `DISTINCT` 去重追加；
>   **中等 6（grounding 伪代码两处不一致）**——`complete_extract` 签名补 `content_text` 参数；grounding 逻辑统一为 §4.6.1 策略（先 `normalize_surface()` 对齐，再降置信）；
>   **小问题**——§5.1.5 `reports.status` DEFAULT `'pending'`（原 `'succeeded'` 方向反）；§10.1 报告重试说明 `ON CONFLICT DO UPDATE`；§6.X entity wiki 触发 SQL 子查询补 `AND kind='entity'`；§7.1 相似文章 SQL target 侧补 `model=<active>`；§7.1 CTE `active_model` 加字面量拼接注释（§5.2 partial HNSW）；`semantic_wikis` 注释修正（cosine distance 越小越好，×1.05 是降权非 boost）；§16.1 新增 `source_articles_json` 悬空 id 限制
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
      WHERE entity_id = e.id AND changed_at > (
        SELECT updated_at FROM wiki_pages WHERE ref_id = e.id AND kind = 'entity' LIMIT 1
      )
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
-- ⚠ :active_model 必须以字面量拼入查询字符串（§5.2 partial HNSW 索引要求常量谓词），
--   不能用 prepared statement 参数化（PG planner 不匹配 partial index 谓词）。
--   active model 名来自 config，非用户输入，无注入风险。
WITH semantic_articles AS (
  SELECT article_id AS ref_id, 'article' AS kind, vector <=> :q_vec AS distance
  FROM article_embeddings WHERE model = '<active_embed_model>'
  ORDER BY distance LIMIT 60
),
semantic_wikis AS (
  -- Wiki 无独立向量，靠相关 article 的相关性传递。
  -- distance * 1.05 让 wiki 结果略后于原文（cosine distance 越小越好，乘 >1 即降权）；
  -- 效果：同一相关度下 wiki 排在 article 后面，符合用户预期（先看原文再看词条）
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
-- ⚠ target 与 candidate 都必须锁定 active embed model（§5.2 partial HNSW）
SELECT a.id, a.title, ae.vector <=> target.vector AS distance, at.score AS topic_score
FROM article_embeddings target
JOIN article_embeddings ae ON ae.model = target.model
                           AND ae.kind = 'summary'
                           AND ae.article_id != target.article_id
JOIN articles a ON a.id = ae.article_id
WHERE target.article_id = :aid AND target.kind = 'summary'
  AND target.model = '<active_embed_model>'   -- 锁定 active model，多模型 A/B 时不跨模型混算
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

**Phase 2 已拆分**：WebUI / 实体 / 翻译 / 报告 / 图谱 / 高级检索 / API 连接器 等全部移至 [DESIGN_Phase_2.md](DESIGN_Phase_2.md) `§14` 实施清单，本文不再重复。

> **Phase 2 优先级（v0.15，执行序与切片对齐，切片号即优先级序）**：
> 1. **P0 `2.0 §5.4.1` 生产 DB + 进程隔离** — 阻塞生产，`2.1 WebUI` 不再隐含
> 2. **P1 `2.1 WebUI` + `§4.8` `embed`/`rerank`（`2.6` 前置并行）** — WebUI 为入口，`embed` 小改解锁检索，二者可并行于 `2.0` 后
> 3. **P2 `2.2 翻译` + `2.3 实体`/`2.4 图谱`** — 翻译慢后台原文可读，实体为图谱前置
> 4. **P3 `2.5 报告` → `10.4 飞书` → `2.6 检索` → `2.7 API` → `2.8 验收`** — 飞书紧接报告后

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
| 13 | **`relations.source_articles_json` 删除文章后留悬空 id** | 文章被删（ON DELETE CASCADE 从 articles 表移除）后，JSONB 列表里仍保留其 id；`ON DELETE SET NULL` 只覆盖 scalar `source_article_id`，不影响 JSONB | 应用层查询时用 `JOIN` 过滤或定期清理；P3 加 `source_articles_json` 清理任务 |

---

