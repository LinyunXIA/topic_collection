# AGENTS.md — topic_collection (v2)

## 定位与边界

- v2 = RSS → 飞书摘要卡机器人。唯一业务包是 `feedkicker/`；根目录若出现 `app/` 是 v1 缓存残留，直接删。
- `main` = v2 线；v1 完整历史在 `v1` 分支。不要把 v1 的架构（队列状态机/PG/advisory lock）带回来。
- `docs/DESIGN.md` 是工程实现权威（章节号 §N 被 commit/PR 引用），`docs/PRD.md` 是产品权威。文档为中文。

## 命令

```bash
.venv/bin/python -m pytest -q                 # 46 用例全离线，<1s；变慢=打真网了，必须修
.venv/bin/tc-push [--env dev|test|prod] [--dry-run]
.venv/bin/python -m feedkicker.bitable --env prod [--init|--reseed]   # 归档运维
```

- Python ≥3.12，用仓库内 `.venv`；依赖改动后 `pip install -e .[dev]`。
- 提交走 feature 分支 → PR → merge，不直推 main。

## 环境与凭据

- 配置三份：`config-{dev,test,prod}.yaml`，**全部 gitignored**，真实 webhook/signature secret 只存本地文件或 env（`FEISHU_WEBHOOK`/`FEISHU_SECRET`）。
- 覆盖顺序：`--db` > `TC_DB` > `--env` > `TC_APP_ENV` > prod；db 按 env 分流 `data/tc-{env}.sqlite3`。
- prod 由 launchd 每日 8:30/16:00 拉起（`~/Library/LaunchAgents/com.feedkicker.push.plist`，内含 `TC_APP_ENV=prod`）；改完 plist 要 `launchctl bootout && bootstrap`。
- 运行时依赖两个已登录的外部 CLI：`lark-cli`（飞书）、`gh`（GitHub）。测试中必须 mock 其 subprocess/httpx 调用——曾发生过测试数据误写到线上文档的事故。

## 编排顺序（不可调换）

```
抓取入库 → 写多维表格归档（先档案）→ 推飞书卡 → mark_pushed
```

- `mark_pushed` 在**发送成功后**才执行：失败条目保留待推，下轮重发卡片。
- 归档同步失败只 WARNING，卡片照发（表格是常驻档案，链接永远有效）。
- 跨源去重键 = `canonicalize(url)`：去 fragment、host 小写、**保留 query**；推送侧和归档侧都靠它。

## 飞书自定义机器人三坑（实测踩过）

1. 签名：`f"{timestamp}\n{secret}"` 作 HMAC-SHA256 的 **key** 对空串求摘要再 Base64（`feishu.gen_sign`）。
2. 卡片 div 内文本标签只能 `lark_md`，写 `markdown` 会被拒（业务码 11246）。
3. 请求体 ≤20KB，超限由 `build_card` 降级裁剪（剥 description → 丢最旧条目）；不要在 send 层截断 JSON。

## 其他约定

- 卡片「详情」按钮经 `detail_label` 参数化；发送失败时 `strip_actions` 去按钮降级重试一次。
- 多维表格是唯一在线档案：prod 与 dev-test 双 Base（dev/test 共享文件用「环境」列区分），按来源/按日期双分组视图。
- `sheets_archive` / gh-pages(`publish`) 链路已废弃移除（DESIGN §16/§17 有记录），`site.enabled=false` 全环境——不要复活。
- 代码零注释风格；新逻辑靠命名与 tests 表达意图。
