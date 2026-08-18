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
from app.db.fts import search_articles_fts
from app.ingest.dedup import url_hash, content_hash
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
):
    """管理订阅源。import = 同步 feeds.yaml → DB；fetch = 抓取文章。"""
    if action == "import":
        _run_async(_feeds_import())
    elif action == "fetch":
        _run_async(_feeds_fetch(feed_name))
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


async def _feeds_fetch(feed_name: str | None = None):
    """抓取文章并入队。"""
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

                new_count = 0
                for item in items:
                    u_hash = url_hash(item.source_url)
                    c_hash = content_hash(item.content_text)

                    # 幂等：url_hash 已存在则 mention_count+1
                    existing = await session.execute(
                        text("SELECT id FROM articles WHERE url_hash=:uh"),
                        {"uh": u_hash},
                    )
                    existing_row = existing.first()
                    if existing_row:
                        await session.execute(
                            text(
                                "UPDATE articles SET mention_count=mention_count+1 "
                                "WHERE id=:aid"
                            ),
                            {"aid": existing_row[0]},
                        )
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
    from app.llm.omlx import OMLXProvider
    from app.llm.client import LLMClient
    from app.services.llm_tasks import run_summarize as _run_summarize

    settings = load_settings()
    provider = OMLXProvider(base_url=settings.llm.endpoint, generation_model=settings.llm.model)
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
    limit: int = typer.Option(20, "--limit", "-l"),
):
    """关键词全文搜索。"""
    _run_async(_search(query, limit))


async def _search(query: str, limit: int):
    settings = load_settings()
    async with get_session(settings) as session:
        article_ids = await search_articles_fts(session, query, limit)

        if not article_ids:
            console.print(f"[yellow]未找到匹配「{query}」的文章[/yellow]")
            return

        # 获取详情
        result = await session.execute(
            text(
                "SELECT id, title, status, lang FROM articles "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": article_ids},
        )
        rows = {r["id"]: dict(r) for r in result.mappings().all()}

        table = Table(title=f"搜索结果: {query}")
        table.add_column("ID", style="dim")
        table.add_column("标题", max_width=50)
        table.add_column("状态")
        table.add_column("语言")

        for aid in article_ids:
            row = rows.get(aid, {})
            table.add_row(
                str(aid),
                row.get("title", "?")[:50],
                row.get("status", "?"),
                row.get("lang", ""),
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

    # LLM 健康
    from app.llm.omlx import OMLXProvider
    provider = OMLXProvider(base_url=settings.llm.endpoint)
    health = await provider.healthcheck()

    # 展示
    llm_color = "green" if health.healthy else "red"
    console.print(Panel(
        f"[{llm_color}]LLM: {'✅ 健康' if health.healthy else '❌ 不可用'}[/{llm_color}]\n"
        f"端点: {settings.llm.endpoint}\n"
        f"延迟: {health.latency_ms}ms\n"
        f"模型: {', '.join(health.models[:3]) if health.models else '?'}",
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
    task: str = typer.Argument(help="任务名称 (summarize|embed_core|embed_summary)"),
):
    """重试指定文章的任务（走对应 complete_* 钩子）。"""
    _run_async(_retry(article_id, task))


async def _retry(article_id: int, task: str):
    from app.llm.omlx import OMLXProvider
    from app.llm.client import LLMClient

    settings = load_settings()
    provider = OMLXProvider(base_url=settings.llm.endpoint, generation_model=settings.llm.model)
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
        else:
            console.print(f"[red]未知任务: {task}[/red]")
            return

        console.print(f"[green]✅ {task} 完成[/green]")


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
