# PRD / DESIGN 验证报告

> 审查对象：`docs/PRD.md`、`docs/DESIGN.md`
> 审查日期：2026-08-13
> 审查方式：两份文档全文通读 + 交叉核对（决策一致性、阶段归属、模型/接口命名、DDL 完整性、过时残留）

---

## 结论概览

文档主体结构完整、核心架构清晰，但**多轮增量决策（27B 生成模型 / 不鉴权 / 固定 4096 维 / CLI=Phase 1）没有回溯到 PRD 的早期段落**，留下若干事实性矛盾。共发现问题 **12 项**：高优先级 6 项（事实矛盾）、中优先级 4 项（过时标注）、设计缺口 2 项（需决策）。

---

## 🔴 高优先级 —— 事实矛盾，会误导实现

| # | 位置 | 问题 |
|---|---|---|
| 1 | PRD §8 L184 | 接口代码块 `default_model` 注释仍为 **`Qwen3.5-9B-…-THINKING-…`**（已确认加载失败的模型），与全文默认 27B 矛盾 |
| 2 | PRD §8 L185 | `api_key` 注释仍写「Bearer token 来自 TC_LLM_API_KEY」，与"本机不鉴权"矛盾 |
| 3 | PRD §8 L202 | 嵌入维度写「运行时实测 / Matryoshka 可截断」，与 PRD §7 L170、DESIGN §5.2 的「固定 4096」矛盾 |
| 4 | DESIGN §5.1 DDL | **缺 `article_versions` 表** —— PRD §7 L157 有、风险表 L327 与流水线均引用，但建表 DDL 未定义 |
| 5 | PRD §16 L376 / L383 | `llm/{base,ollama,mlx,...}` 写的是 `mlx`（DESIGN §3 为 `omlx`）；`typer（P2）` 与"CLI = Phase 1 主入口"矛盾 |
| 6 | PRD §8 L181-189 vs DESIGN §4.1 | LLM 接口命名不一致：PRD 用 `complete()` + `default_model`；DESIGN 用 `generate()` + `generation_model`，且 PRD 的 Protocol 缺 `embed`/`rerank` 方法 |

---

## 🟡 中优先级 —— 过时的阶段/优先级/模型名标注

| # | 位置 | 问题 |
|---|---|---|
| 7 | PRD §1 L12、§3 L54 | 「Ollama / MLX 双后端」残留，应为 oMLX（主）+ Ollama/进程内降级 |
| 8 | PRD §4 L77-80 | F9「P1 骨架/P2 广度」、F10「P1 骨架/P2 健壮」、F12 告警「P2」与 §12 路线图矛盾（API 骨架=P2、爬虫=P3、告警=P3） |
| 9 | PRD §14 L321-322 | 风险表「首选 Qwen2.5 14B / DeepSeek-R1-distill」「7–14B」「分类用 3B」全部过时（现单模型 27B） |
| 10 | PRD §11 L261 / L292 | 「默认 Ollama」「可选托管 MLX server」「seed_feeds」残留 |

---

## 🟠 设计缺口 —— 需决策

| # | 问题 |
|---|---|
| 11 | **Phase 1 无法定义主题**：路线图 L300 含 `classify_topics`，验收 #3 是「定义主题跨源聚合」，但 Phase 1 CLI 只有 `feeds import/fetch/summarize/list/search/article`，**缺 `topic` 命令** |
| 12 | 验收 #5「Wiki 按**主题/实体**浏览」被归入 Phase 1（L340），但实体抽取/浏览是 Phase 2 —— Phase 1 实际只能做到「按关键词全文搜索」 |

---

## 修正建议

### 事实性错误（#1–#10）—— 直接修正，无需决策

- **#1/#2/#3/#6**：统一 PRD §8 的 LLM 接口与 DESIGN §4.1 一致——`generate()` / `embed()` / `rerank()` 三能力、`generation_model=Qwen3.6-27B…`、`api_key` 可选（默认不鉴权）、嵌入维度固定 4096。
- **#4**：DESIGN §5.1 DDL 补 `article_versions` 表（`article_id, kind(raw_html/raw_text), content, created_at`）。
- **#5**：PRD §16 目录 `llm/mlx` → `omlx`；`typer（P2）` → `typer（P1 主入口）`。
- **#7/#8/#9/#10**：PRD §1/§3/§4/§11/§14 的过时模型名、阶段标注、降级描述与 §12 路线图对齐。

### 待决策项

- **#11（主题）**：建议 Phase 1 CLI 增加 `tc topic add/list`（主题存 DB，关键词匹配 + LLM 打分，成本低），保持"Phase 1 即能验证主题聚合"；若倾向精简，则把主题整体推迟 Phase 2。
- **#12（验收 #5）**：建议将 Phase 1 验收 #5 收窄为「按**关键词**全文搜索」，主题/实体浏览归 Phase 2。

---

## 总体评价

- **架构**：单进程全异步 + Postgres 队列（SKIP LOCKED）+ Services 薄封装，设计合理且与实测 oMLX 事实一致。
- **数据模型**：除缺 `article_versions` 表外，DDL 与字段命名整体自洽。
- **主要风险**：文档漂移集中在 PRD 早期章节，修复后即可作为实现基线。
