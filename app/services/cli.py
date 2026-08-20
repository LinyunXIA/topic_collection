"""CLI 入口 — Phase 1 主入口（typer，DESIGN §3/§14）

tc feeds import / fetch
tc summarize
tc list / search
tc article <id>
tc status
tc retry <article_id> <task>
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_engine, get_session, get_session_factory, check_extensions, dispose_engine
from app.db.fts import search_articles_fts, update_article_tsv
from app.ingest.dedup import url_hash, content_hash, apply_exact_dedup
from app.services.cleaner import clean_article
from app.services.llm_tasks import (
    complete_summarize,
    complete_embed,
    run_embed_core,
    run_embed_summary,
)
from app.services.topics import match_keywords
from app.pipeline import enqueue_jobs

app = typer.Typer(name="tc", help="Topic Collection CLI")
console = Console()


def _run_async(coro):
    """运行异步协程。"""
    return asyncio.run(coro)


# ── feeds ──────────────────────────────────────────────────────────

@app.command()
def feeds(
    action: str = typer.Argument(help="import | fetch"),
    feed_name: str = typer.Option(None, "--name", "-n", help="指定 feed 名称"),
    count: int = typer.Option(None, "--count", "-c", help="限制抓取条数（从第一条起按顺序取 N 条）"),
):
    """管理订阅源。import = 同步 feeds.yaml → DB；fetch = 抓取文章。"""
    if action == "import":
        _run_async(_feeds_import())
    elif action == "fetch":
        _run_async(_feeds_fetch(feed_name, count))
    else:
        console.print(f"[red]未知操作: {action}[/red]")
        raise typer.Exit(1)


async def _feeds_import():
    """同步 feeds.yaml → DB（幂等 upsert）。"""
    import yaml
    from pathlib import Path

    settings = load_settings()
    feeds_path = Path("config/feeds.yaml")
    if not feeds_path.exists():
        console.print("[red]config/feeds.yaml 不存在[/red]")
        return

    with open(feeds_path) as f:
        data = yaml.safe_load(f) or {}

    feeds = data.get("feeds", [])
    console.print(f"📋 发现 {len(feeds)} 个订阅源")

    async with get_session(settings) as session:
        for feed in feeds:
            feed_type = feed.get("type", "rss")
            name = feed.get("name", "")
            url = feed.get("url", "")
            enabled = feed.get("enabled", True)

            if not url:
                console.print(f"  ⚠️  跳过无 URL 的条目: {name}")
                continue

            # 幂等 upsert（按 url）
            result = await session.execute(
                text(
                    "INSERT INTO feeds (type, name, url, enabled) "
                    "VALUES (:type, :name, :url, :enabled) "
                    "ON CONFLICT DO NOTHING "
                    "RETURNING id"
                ),
                {"type": feed_type, "name": name, "url": url, "enabled": enabled},
            )
            row = result.first()
            if row:
                console.print(f"  ✅ 新增: {name} ({url})")
            else:
                # 已存在，更新 enabled 状态
                await session.execute(
                    text("UPDATE feeds SET enabled=:enabled WHERE url=:url"),
                    {"enabled": enabled, "url": url},
                )
                console.print(f"  ↻ 已存在: {name}")

    console.print("[green]✅ feeds import 完成[/green]")


async def _feeds_fetch(feed_name: str | None = None, count: int | None = None):
    """抓取文章并入队。count 限制单次抓取条数（从第一条起按顺序取 N 条）。"""
    from app.ingest.feeds import FeedFetcher

    settings = load_settings()
    fetcher = FeedFetcher(settings)

    async with get_session(settings) as session:
        # 获取 enabled feeds
        if feed_name:
            result = await session.execute(
                text("SELECT * FROM feeds WHERE enabled=true AND name=:name"),
                {"name": feed_name},
            )
        else:
            result = await session.execute(
                text("SELECT * FROM feeds WHERE enabled=true")
            )
        feeds = result.mappings().all()

        if not feeds:
            console.print("[yellow]没有启用的订阅源[/yellow]")
            return

        total_new = 0
        for feed in feeds:
            console.print(f"📥 抓取: {feed['name']} ({feed['url']})")
            try:
                items, new_etag, new_lm = await fetcher.fetch_feed(
                    feed_id=feed["id"],
                    url=feed["url"],
                    etag=feed["etag"],
                    last_modified=feed["last_modified"],
                )

                # --count 截断：从第一条起按顺序取 N 条（DESIGN P1+.2）
                if count is not None and len(items) > count:
                    truncated = len(items) - count
                    items = items[:count]
                    console.print(f"  ⚠️  截断: 从 {truncated + count} 条中取前 {count} 条（--count={count}）")
                    # 记 fetch_events 审计
                    await session.execute(
                        text(
                            "INSERT INTO fetch_events (feed_id, event_type, ok, item_count) "
                            "VALUES (:fid, 'fetch_count_limited', true, :cnt)"
                        ),
                        {"fid": feed["id"], "cnt": truncated},
                    )

                new_count = 0
                for item in items:
                    u_hash = url_hash(item.source_url)
                    c_hash = content_hash(item.content_text)

                    # 精确去重双闸（DESIGN §6）：url_hash 同 / content_hash 同
                    # → winner mention_count+1 + 审计，跳过 insert + enqueue
                    if await apply_exact_dedup(session, feed["id"], u_hash, c_hash):
                        continue

                    # 清洗
                    cleaned = await clean_article(
                        item.content_html or item.content_text,
                        item.title,
                    )

                    if not cleaned["is_parseable"]:
                        status = "unparseable"
                    else:
                        status = "pending"

                    # 插入文章
                    await session.execute(
                        text(
                            "INSERT INTO articles "
                            "(feed_id, source_url, url_hash, content_hash, title, "
                            " author, published_at, content_text, content_md, "
                            " lang, word_count, status) "
                            "VALUES (:fid, :url, :uh, :ch, :title, "
                            " :author, :pub, :ct, :cm, "
                            " :lang, :wc, :status) "
                            "ON CONFLICT (url_hash) DO NOTHING "
                            "RETURNING id"
                        ),
                        {
                            "fid": feed["id"],
                            "url": item.source_url,
                            "uh": u_hash,
                            "ch": c_hash,
                            "title": item.title,
                            "author": item.author,
                            "pub": item.published_at,
                            "ct": cleaned["content_text"],
                            "cm": cleaned["content_md"],
                            "lang": cleaned["lang"],
                            "wc": cleaned["word_count"],
                            "status": status,
                        },
                    )

                    # 获取新插入的 article_id
                    art_result = await session.execute(
                        text("SELECT id FROM articles WHERE url_hash=:uh"),
                        {"uh": u_hash},
                    )
                    art_row = art_result.first()
                    if art_row:
                        # tsv 阶段一：title + content_text（DESIGN §5.3）
                        # 不做这步的话 articles.tsv 一直是 NULL，关键词检索永远搜不到原文
                        await update_article_tsv(
                            session, art_row[0],
                            title=item.title or "",
                            content_text=cleaned["content_text"] or "",
                        )
                    if art_row and status == "pending":
                        # 入队 LLM 任务
                        await enqueue_jobs(
                            session, art_row[0],
                            ["embed_core", "summarize"],
                            c_hash,
                        )
                        # 关键词匹配
                        matched = await match_keywords(session, art_row[0])
                        if matched:
                            console.print(f"    🏷️  关键词命中 {len(matched)} 个主题")

                    new_count += 1

                # 更新 feed 状态
                await session.execute(
                    text(
                        "UPDATE feeds SET etag=:etag, last_modified=:lm, "
                        "last_fetched_at=now(), fetch_status='ok', "
                        "fetch_failures=0 WHERE id=:fid"
                    ),
                    {"etag": new_etag, "lm": new_lm, "fid": feed["id"]},
                )

                console.print(f"  ✅ 新增 {new_count} 篇")
                total_new += new_count

            except Exception as e:
                console.print(f"  ❌ 抓取失败: {e}")
                await session.execute(
                    text(
                        "UPDATE feeds SET fetch_failures=fetch_failures+1, "
                        "last_error=:err, fetch_status='error' WHERE id=:fid"
                    ),
                    {"err": str(e)[:500], "fid": feed["id"]},
                )

    console.print(f"\n[green]✅ 抓取完成，共新增 {total_new} 篇[/green]")


# ── summarize ──────────────────────────────────────────────────────

@app.command()
def summarize(
    article_id: int = typer.Argument(help="文章 ID"),
):
    """重新生成文章摘要（走 complete_summarize 钩子）。"""
    _run_async(_summarize(article_id))


async def _summarize(article_id: int):
    from app.llm.factory import build_provider
    from app.llm.client import LLMClient
    from app.services.llm_tasks import run_summarize as _run_summarize

    settings = load_settings()
    provider = build_provider("generate", settings)
    llm_client = LLMClient(provider=provider, max_concurrency=1)

    async with get_session(settings) as session:
        # 获取文章
        result = await session.execute(
            text("SELECT title, content_text, content_hash FROM articles WHERE id=:aid"),
            {"aid": article_id},
        )
        row = result.mappings().first()
        if not row:
            console.print(f"[red]文章 {article_id} 不存在[/red]")
            return

        console.print(f"📝 生成摘要: {row['title'][:50]}...")

        # 模拟 job dict
        job = {
            "id": 0,
            "article_id": article_id,
            "task": "summarize",
            "content_hash": row["content_hash"],
        }

        await _run_summarize(session, job, settings, llm_client)
        console.print("[green]✅ 摘要生成完成[/green]")


# ── list ───────────────────────────────────────────────────────────

@app.command("list")
def list_articles(
    limit: int = typer.Option(20, "--limit", "-l"),
    topic: str = typer.Option(None, "--topic", "-t", help="按主题筛选"),
):
    """列出文章。"""
    _run_async(_list_articles(limit, topic))


async def _list_articles(limit: int, topic: str | None):
    settings = load_settings()
    async with get_session(settings) as session:
        if topic:
            result = await session.execute(
                text(
                    "SELECT a.id, a.title, a.status, a.lang, a.published_at, "
                    "  t.name as topic_name "
                    "FROM articles a "
                    "JOIN article_topics at ON at.article_id = a.id "
                    "JOIN topics t ON t.id = at.topic_id "
                    "WHERE a.dedupe_of IS NULL AND t.name = :topic "
                    "ORDER BY a.published_at DESC LIMIT :limit"
                ),
                {"topic": topic, "limit": limit},
            )
        else:
            result = await session.execute(
                text(
                    "SELECT id, title, status, lang, published_at "
                    "FROM articles WHERE dedupe_of IS NULL "
                    "ORDER BY published_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            )

        rows = result.mappings().all()
        if not rows:
            console.print("[yellow]没有文章[/yellow]")
            return

        table = Table(title="文章列表")
        table.add_column("ID", style="dim")
        table.add_column("标题", max_width=50)
        table.add_column("状态")
        table.add_column("语言")
        table.add_column("发布时间")

        for row in rows:
            status_color = {"done": "green", "processing": "yellow", "pending": "dim"}.get(
                row["status"], "red"
            )
            table.add_row(
                str(row["id"]),
                row["title"][:50],
                f"[{status_color}]{row['status']}[/{status_color}]",
                row.get("lang", ""),
                str(row.get("published_at", ""))[:10],
            )

        console.print(table)


# ── search ─────────────────────────────────────────────────────────

@app.command()
def search(
    query: str = typer.Argument(help="搜索关键词"),
    mode: str = typer.Option("hybrid", "--mode", "-m", help="检索模式: hybrid|semantic|keyword"),
    limit: int = typer.Option(20, "--limit", "-l"),
):
    """混合检索（hybrid=RRF 融合 / semantic=纯向量 / keyword=纯关键词）。"""
    _run_async(_search(query, mode, limit))


async def _search(query: str, mode: str, limit: int):
    from app.services.search import search as hybrid_search

    settings = load_settings()

    # 语义/hybrid 模式需要 LLM client（embed 能力）
    llm_client = None
    if mode in ("hybrid", "semantic"):
        try:
            from app.llm.factory import build_provider
            from app.llm.client import LLMClient as _LC
            provider = build_provider("embed", settings)
            llm_client = _LC(provider=provider, max_concurrency=1)
            health = await llm_client.healthcheck()
            if not health.healthy:
                console.print("[yellow]⚠️  LLM 不可用，降级为关键词模式[/yellow]")
                llm_client = None
                mode = "keyword"
        except Exception:
            mode = "keyword"

    async with get_session(settings) as session:
        resp = await hybrid_search(session, query, settings, llm_client, mode, limit)

        if not resp.results:
            console.print(f"[yellow]未找到匹配「{query}」的文章[/yellow]")
            return

        table = Table(title=f"搜索结果: {query} [{resp.mode}]")
        table.add_column("ID", style="dim")
        table.add_column("标题", max_width=50)
        table.add_column("来源")
        table.add_column("分数", justify="right")

        for r in resp.results:
            source_color = "cyan" if r.source == "article" else "magenta"
            table.add_row(
                str(r.id),
                r.title[:50],
                f"[{source_color}]{r.source}[/{source_color}]",
                f"{r.score:.4f}",
            )

        console.print(table)


# ── article ────────────────────────────────────────────────────────

@app.command()
def article(
    article_id: int = typer.Argument(help="文章 ID"),
):
    """查看文章详情。"""
    _run_async(_article(article_id))


async def _article(article_id: int):
    settings = load_settings()
    async with get_session(settings) as session:
        result = await session.execute(
            text(
                "SELECT a.*, s.summary_text, s.key_points_json, s.confidence "
                "FROM articles a "
                "LEFT JOIN summaries s ON s.article_id = a.id AND s.lang = 'zh' "
                "WHERE a.id=:aid"
            ),
            {"aid": article_id},
        )
        row = result.mappings().first()
        if not row:
            console.print(f"[red]文章 {article_id} 不存在[/red]")
            return

        console.print(Panel(
            f"[bold]{row['title']}[/bold]\n"
            f"状态: {row['status']} | 语言: {row.get('lang', '?')} | "
            f"字数: {row.get('word_count', '?')}\n"
            f"URL: {row['source_url']}\n"
            f"发布时间: {row.get('published_at', '?')}",
            title=f"文章 #{article_id}",
        ))

        if row.get("summary_text"):
            console.print(Panel(
                row["summary_text"],
                title="摘要",
            ))
            if row.get("key_points_json"):
                kp = row["key_points_json"]
                if isinstance(kp, str):
                    kp = json.loads(kp)
                for i, point in enumerate(kp, 1):
                    console.print(f"  {i}. {point}")


# ── status ─────────────────────────────────────────────────────────

@app.command()
def status():
    """系统状态：队列深度 / 失败任务 / LLM 健康。"""
    _run_async(_status())


async def _status():
    settings = load_settings()

    # 队列统计
    async with get_session(settings) as session:
        result = await session.execute(
            text(
                "SELECT status, COUNT(*) as cnt "
                "FROM processing_jobs GROUP BY status"
            )
        )
        stats = {r["status"]: r["cnt"] for r in result.mappings().all()}

        result = await session.execute(
            text("SELECT COUNT(*) as cnt FROM articles WHERE dedupe_of IS NULL")
        )
        article_count = result.scalar()

        result = await session.execute(
            text("SELECT COUNT(*) as cnt FROM articles WHERE status='pending'")
        )
        pending_count = result.scalar()

    # LLM 健康（探测 generate 端点）
    from app.llm.factory import build_provider
    gen = settings.llm.generate
    gen_backend = gen.backend if gen else settings.llm.backend
    gen_endpoint = gen.endpoint if gen else settings.llm.endpoint
    gen_model = gen.model if gen else settings.llm.model
    provider = build_provider("generate", settings)
    health = await provider.healthcheck()

    # embed 状态
    emb_backend = settings.llm.embed.backend
    emb_model = settings.llm.embed.model

    # 展示
    llm_color = "green" if health.healthy else "red"
    console.print(Panel(
        f"[{llm_color}]Generate: {'✅ 健康' if health.healthy else '❌ 不可用'}[/{llm_color}]\n"
        f"  Backend: {gen_backend}\n"
        f"  端点:   {gen_endpoint}\n"
        f"  模型:   {gen_model}\n"
        f"  延迟:   {health.latency_ms}ms\n"
        f"  可用:   {', '.join(health.models[:3]) if health.models else '?'}\n"
        f"\n"
        f"[cyan]Embed: {emb_backend}[/cyan]  模型: {emb_model}",
        title="系统状态",
    ))

    table = Table(title="队列统计")
    table.add_column("状态")
    table.add_column("数量", justify="right")
    for s in ["queued", "running", "succeeded", "failed", "superseded"]:
        table.add_row(s, str(stats.get(s, 0)))
    console.print(table)

    console.print(f"📊 文章总数: {article_count} | 待处理: {pending_count}")


# ── retry ──────────────────────────────────────────────────────────

@app.command()
def retry(
    article_id: int = typer.Argument(help="文章 ID"),
    task: str = typer.Argument(
        help="任务名称 (summarize|embed_core|embed_summary|topics|wiki)"
    ),
):
    """重试指定文章的任务（走对应 complete_* 钩子）。"""
    _run_async(_retry(article_id, task))


async def _retry(article_id: int, task: str):
    from app.llm.factory import build_provider
    from app.llm.client import LLMClient

    settings = load_settings()
    # 按 task 选择对应能力的 provider
    if task in ("summarize", "topics"):
        capability = "generate"
    elif task in ("embed_core", "embed_summary"):
        capability = "embed"
    else:
        capability = "generate"  # 默认用 generate（wiki 实际不需要 LLM，仍走 generate 客户端兼容）
    provider = build_provider(capability, settings)
    llm_client = LLMClient(provider=provider, max_concurrency=1)

    async with get_session(settings) as session:
        result = await session.execute(
            text("SELECT content_hash, title FROM articles WHERE id=:aid"),
            {"aid": article_id},
        )
        row = result.mappings().first()
        if not row:
            console.print(f"[red]文章 {article_id} 不存在[/red]")
            return

        console.print(f"🔄 重试 {task}: {row['title'][:50]}...")

        job = {
            "id": 0,
            "article_id": article_id,
            "task": task,
            "content_hash": row["content_hash"],
        }

        if task == "summarize":
            from app.services.llm_tasks import run_summarize
            await run_summarize(session, job, settings, llm_client)
        elif task == "embed_core":
            await run_embed_core(session, job, settings, llm_client)
        elif task == "embed_summary":
            await run_embed_summary(session, job, settings, llm_client)
        elif task == "topics":
            from app.services.topics import classify_topics
            await classify_topics(session, article_id, settings, llm_client)
        elif task == "wiki":
            from app.services.wiki import generate_article_wiki
            await generate_article_wiki(session, article_id, settings)
        else:
            console.print(f"[red]未知任务: {task}[/red]")
            return

        console.print(f"[green]✅ {task} 完成[/green]")


# ── topic ──────────────────────────────────────────────────────────

@app.command()
def topic(
    action: str = typer.Argument(help="add | list"),
    name: str = typer.Option(None, "--name", "-n", help="主题名称 (add 时必填)"),
    keywords: str = typer.Option(None, "--keywords", "-k", help="关键词，逗号分隔 (add 时必填)"),
    description: str = typer.Option("", "--desc", "-d", help="主题描述"),
):
    """管理主题。add = 创建主题；list = 列出所有主题。"""
    _run_async(_topic(action, name, keywords, description))


async def _topic(action: str, name: str | None, keywords: str | None, description: str):
    from app.services.topics import create_topic, list_topics, reclassify_recent, TopicExistsError

    settings = load_settings()
    factory = get_session_factory(settings)

    if action == "add":
        if not name or not keywords:
            console.print("[red]add 需要 --name 和 --keywords[/red]")
            return
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        async with factory() as session:
            try:
                topic_id = await create_topic(session, name, kw_list, description)
                await session.commit()
            except TopicExistsError as e:
                # 重复主题：UNIQUE 约束触发，给出已存在 id（fix #11）
                await session.rollback()
                existing = f" (id={e.existing_id})" if e.existing_id is not None else ""
                console.print(f"[red]❌ 主题「{e.name}」已存在{existing}，未创建新行[/red]")
                console.print("   [dim]如需修改关键词/描述，请用 update 命令或先删除再重建[/dim]")
                return
            console.print(f"✅ 主题「{name}」已创建 (id={topic_id})")

            # 同步触发近窗重算（DESIGN §6）
            console.print(f"🔄 重算近 {settings.topics.reclassify_recent_days} 天文章...")
            requeued = await reclassify_recent(session, topic_id, settings)
            await session.commit()
            console.print(f"   入队 {requeued} 个 topics job（LLM 慢路径）")

    elif action == "list":
        async with factory() as session:
            topics = await list_topics(session)
            if not topics:
                console.print("[yellow]没有主题[/yellow]")
                return
            table = Table(title="主题列表")
            table.add_column("ID", style="dim")
            table.add_column("名称")
            table.add_column("关键词")
            table.add_column("状态")
            for t in topics:
                kw = t.get("keywords_json") or []
                status = "✅" if t.get("enabled") else "❌"
                table.add_row(
                    str(t["id"]),
                    t["name"],
                    ", ".join(kw[:5]) + ("..." if len(kw) > 5 else ""),
                    status,
                )
            console.print(table)
    else:
        console.print(f"[red]未知操作: {action}[/red]")


# ── backup ─────────────────────────────────────────────────────────

@app.command()
def backup():
    """备份数据库（pg_dump | gzip）。"""
    import subprocess
    result = subprocess.run(["bash", "scripts/backup.sh"], capture_output=False)
    if result.returncode != 0:
        console.print("[red]备份失败[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
