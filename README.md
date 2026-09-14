# feedkicker

**RSS → 飞书机器人**：抓取订阅源，先写入多维表格归档，再把摘要卡推送到飞书群；每周五自动把沙龙的「已选题」生成工具类 / 原理类双大纲并归档到飞书 Wiki。面向本机运维（macOS + launchd）。

架构与数据流一句话：`抓取(fetch) → 入库(sqlite) → 多维表格归档(bitable) → 推飞书卡(feishu) → mark_pushed`，唯一业务包是 `feedkicker/`（详见 [`docs/DESIGN.md` §1](docs/DESIGN.md#1-架构总览)）。

> 本 README 是入口；逐命令参数、分环境示例、退出码与错误码见 [`docs/CLI.md`](docs/CLI.md)，配置/凭据/定时/排障见 [`docs/OPS.md`](docs/OPS.md)。

## 目录导航

| 文档 | 定位 |
|---|---|
| `README.md`（本文） | 总览 + 快速上手（入口） |
| [`docs/CLI.md`](docs/CLI.md) | 8 个命令详解：参数、dev/test/prod 示例、退出码、错误码对照 |
| [`docs/OPS.md`](docs/OPS.md) | 运维手册：配置与凭据、launchd 定时、飞书三坑、排障 |
| [`docs/PRD.md`](docs/PRD.md) | 产品权威（需求与阶段，权威层级最高） |
| [`docs/DESIGN.md`](docs/DESIGN.md) | 工程实现权威（§N 被 commit/PR 引用） |
| [`AGENTS.md`](AGENTS.md) | 协作约定与命令速查 |

## 运行环境

- **Python ≥ 3.12**（`pyproject.toml` `requires-python`）。
- 使用**仓库内 `.venv`**：所有命令用 `.venv/bin/python` / `.venv/bin/tc-*`，不依赖全局环境。
- 外部依赖一个**已登录的 `lark-cli`**（飞书）：Wiki 归档、多维表格操作都经它执行；未安装或未登录时相关命令会失败。`lark-cli` 只读/写入真实飞书资源，**联调优先 `--dry-run`**。
- 平台为 macOS：定时由 launchd 拉起（见 OPS）。

## 安装

```bash
cd /Users/linyunxia/PycharmProjects/topic_collection
.venv/bin/pip install -e '.[dev]'
```

> **仅支持 editable 安装**（`pip install -e`）：`prompts/`、`config-*.yaml` 位于包外（仓库根），非 editable 安装（普通 `pip install .`）不随包分发、运行时不可用。

依赖改动后重跑该命令。自检三件套：

```bash
.venv/bin/python -m pytest -q      # 全离线用例，应 <1s（变慢=打真网了）
.venv/bin/ruff check .             # 0 errors
.venv/bin/basedpyright             # 0 errors
```

## 配置与凭据概览

配置三份、**全部 gitignored**（真实 webhook/密钥只存本地）：`config-dev.yaml` / `config-test.yaml` / `config-prod.yaml`，各自有 `.example` 模板可复制起步。未指定 `--config` 时按环境推导对应文件，db 按环境分流 `data/tc-{env}.sqlite3`。

覆盖顺序（高优先在前）：

```
--db  >  TC_DB  >  --env  >  TC_APP_ENV  >  prod（默认）
```

即：命令行 `--env` 覆盖环境变量 `TC_APP_ENV`，`--db` 覆盖 `TC_DB` 与环境推导，都不给则回落到 **prod**。敏感值可走环境变量而非落文件：`FEISHU_WEBHOOK` / `FEISHU_SECRET` / `MiniMax_Key` / `TC_SALON_TOKEN`。

字段面、三份 `.example`、prod 与 dev/test 双 Base、环境分级纪律：见 [`docs/OPS.md`](docs/OPS.md)。

## 快速上手（dev，`--dry-run` 跑通 push）

目标：在不发送、不归档（不写多维表格）的前提下，跑通一次抓取与卡片 payload 打印。

```bash
# 1) 准备 dev 配置（首次）：从模板复制后填入 dev 群 webhook 等
cp config-dev.yaml.example config-dev.yaml

# 2) 抓取入库照常，但只打印 payload、不发送、不归档
.venv/bin/tc-push --env dev --dry-run
```

预期：日志打印 `运行开始：环境=dev，db=.../data/tc-dev.sqlite3`，随后打印卡片 JSON payload（`msg_type: interactive`），末尾 `dry-run：共 N 条待推，已打印 payload 未发送`，退出码 `0`。

注意 `--dry-run` **并非纯只读**：RSS 抓取与 sqlite 写库（`download`/首跑标记）照常，仅跳过「发送」与「多维表格归档」。

## 命令总览

| 命令 | 用途 | 入口 | 详解 |
|---|---|---|---|
| `tc-push` | 抓取 RSS → 归档 → 推飞书摘要卡（每日 8:30/16:00） | entry point | [CLI §tc-push](docs/CLI.md#tc-push) |
| `tc-salon` | 已选题 → 双大纲 → Wiki 归档（周五 10:00） | entry point | [CLI §tc-salon](docs/CLI.md#tc-salon) |
| `tc-purge` | 365 天滚动保留清理（默认 dry-run，`--apply` 才删） | entry point | [CLI §tc-purge](docs/CLI.md#tc-purge) |
| `tc-extract` | 近 N 天资讯 → LLM 提炼选题 → salon 表（默认 dry-run，`--apply` 才写） | entry point | [CLI §tc-extract](docs/CLI.md#tc-extract) |
| `python -m feedkicker.wiki_home` | 重建 Wiki 首页（大纲按月索引） | `-m` 模块 | [CLI §wiki_home](docs/CLI.md#wiki_home) |
| `python -m feedkicker.bitable` | 多维表格归档运维（`--init`/`--reseed`/`--backfill`） | `-m` 模块 | [CLI §bitable](docs/CLI.md#bitable) |
| `python -m feedkicker.wiki` | 单篇 Wiki docx 创建（联调） | `-m` 模块 | [CLI §wiki](docs/CLI.md#wiki) |
| `python -m feedkicker.topic` | 已选题分页拉取（只读） | `-m` 模块 | [CLI §topic](docs/CLI.md#topic) |

命令清单与 `pyproject.toml [project.scripts]`（`tc-push`/`tc-salon`/`tc-purge`/`tc-extract`）及 `-m` 模块一致。

## 操作纪律（摘要）

- **prod 库默认禁写**：prod 示例一律 `--dry-run`；真跑命令在 CLI.md 单列并标 ⚠️。
- **`tc-purge --apply` 必须人工**：launchd 只做每月 1 号 10:30 的 dry-run 巡检。
- 卡片发送成功后才 `mark_pushed`；归档同步失败只 WARNING，卡片照发。
- 排障、飞书三坑、定时重载：见 [`docs/OPS.md`](docs/OPS.md)。
