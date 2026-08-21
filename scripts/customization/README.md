# Phase 2+ 排错工具集（scripts/customization）

小排错 / 运维脚本，随需求逐个补充（持续迭代，先做能用的）。

每个脚本都是独立可运行的 Python 脚本，放 `scripts/customization/` 下，不侵入 `tc` CLI。

## 现有脚本

### `fetch_rss_raw.py` — 查看 RSS/Atom 源原始返回格式

排「字段名对不上 / 解析失败 / 抓不到」时，先看源到底返回了什么。

```bash
# 基础：打印状态码 / Content-Type / 原始 XML（前 4000 字节）
.venv/bin/python scripts/customization/fetch_rss_raw.py https://hnrss.org/frontpage

# 结构化：额外用 feedparser 解析，展示 feed/条目字段名
.venv/bin/python scripts/customization/fetch_rss_raw.py <url> --parse

# 自定义
.venv/bin/python scripts/customization/fetch_rss_raw.py <url> --bytes 2000 --parse --limit 3
```

> 注：走共享出口白名单（PRD §12 / #78，域名见根目录 `security/web_site_list.yaml`）。
> 任意**非白名单** RSS 域名抓取需 `export FEED_FETCH_ALLOW_ALL=1`，否则脚本会提示并被拦截
> （不放任裸请求）。