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
from app.ingest.service import fetch_and_store
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


def _resolve_feeds_path() -> "Path":
    """解析订阅源文件路径（方案 C，DESIGN §9）"""
    import os
    from pathlib import Path

    # TC_FEEDS_CONFIG 显式覆盖
    if "TC_FEEDS_CONFIG" in os.environ:
        return Path(os.environ["TC_FEEDS_CONFIG"])
    app_env = os.environ.get("TC_APP_ENV", "dev")
    # 优先 env 专属文件
    p_env = Path(f"config/feeds.{app_env}.yaml")
    if p_env.exists():
        return p_env
    return Path("config/feeds.yaml")


async def _feeds_import():
    """同步 feeds.yaml → DB（幂等 upsert，方案 C env 隔离）。"""
    import os
    import yaml
    from pathlib import Path

    settings = load_settings()
    feeds_path = _resolve_feeds_path()
    if not feeds_path.exists():
        console.print(f"[red]{feeds_path} 不存在[/red]")
        return

    with open(feeds_path) as f:
        data = yaml.safe_load(f) or {}

    feeds = data.get("feeds", [])
    console.print(f"📋 发现 {len(feeds)} 个订阅源（{feeds_path}）")

    # 当前 env（用于 DB 行级隔离）
    cur_env = os.environ.get("TC_APP_ENV", "dev")

    async with get_session(settings) as session:
        for feed in feeds:
            feed_type = feed.get("type", "rss")
            name = feed.get("name", "")
            url = feed.get("url", "")
            enabled = feed.get("enabled", True)
            env = feed.get("env", cur_env)

            if not url:
                console.print(f"  ⚠️  跳过无 URL 的条目: {name}")
                continue

            # 幂等 upsert（按 url, env），兼容旧库无 env 约束时回退 url 单列
            try:
                result = await session.execute(
                    text(
                        "INSERT INTO feeds (type, name, url, enabled, env) "
                        "VALUES (:type, :name, :url, :enabled, :env) "
                        "ON CONFLICT (url, env) DO UPDATE SET enabled=EXCLUDED.enabled, type=EXCLUDED.type, name=EXCLUDED.name "
                        "RETURNING id"
                    ),
                    {"type": feed_type, "name": name, "url": url, "enabled": enabled, "env": env},
                )
                row = result.first()
                if row:
                    # 区分是 insert 还是 update（RETURNING id 在 DO UPDATE 时也返回）
                    # 简化：统一视为已存在/更新
                    console.print(f"  ✅ 同步: {name} ({url}) [{env}]")
                else:
                    console.print(f"  ↻ 已存在: {name} [{env}]")
            except Exception as e:
                # 回退：旧库无 (url, env) 唯一约束时按 url 单列
                if "feeds_url_env_uniq" in str(e) or "env" in str(e).lower():
                    raise
                # 尝试旧 upsert
                result = await session.execute(
                    text(
                        "INSERT INTO feeds (type, name, url, enabled) "
                        "VALUES (:type, :name, :url, :enabled) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {"type": feed_type, "name": name, "url": url, "enabled": enabled},
                )
                row = result.first()
                if row:
                    console.print(f"  ✅ 新增: {name} ({url})")
                else:
                    await session.execute(
                        text("UPDATE feeds SET enabled=:enabled WHERE url=:url"),
                        {"enabled": enabled, "url": url},
                    )
                    console.print(f"  ↻ 已存在: {name}")

    console.print("[green]✅ feeds import 完成[/green]")


async def _feeds_fetch(feed_name: str | None = None, count: int | None = None):
    """抓取文章并入队。count 限制单次抓取条数（从第一条起按顺序取 N 条）。

    抓取 → dedup → clean → insert → tsv → enqueue → match_keywords 全流程
    在 app.ingest.service.fetch_and_store；本函数只负责选 feed + 用户可见输出。
    方案 C：按 TC_APP_ENV 过滤 env 列，实现 dev/prod 隔离。
    """
    import os

    from app.ingest.feeds import FeedFetcher

    settings = load_settings()
    fetcher = FeedFetcher(settings)
    cur_env = os.environ.get("TC_APP_ENV", "dev")

    async with get_session(settings) as session:
        # 获取 enabled feeds（按 env 隔离，兼容旧库无 env 列时回退）
        try:
            if feed_name:
                result = await session.execute(
                    text("SELECT * FROM feeds WHERE enabled=true AND env=:env AND name=:name"),
                    {"env": cur_env, "name": feed_name},
                )
            else:
                result = await session.execute(
                    text("SELECT * FROM feeds WHERE enabled=true AND env=:env"),
                    {"env": cur_env},
                )
            feeds = result.mappings().all()
        except Exception:
            # 旧库无 env 列，回退
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

        def _cli_progress(stage: str, payload: dict) -> None:
            """CLI 进度回调：阶段式输出（截断 / 关键词 / 完成）。"""
            if stage == "truncated":
                console.print(
                    f"  ⚠️  截断: 从 {payload['kept'] + payload['dropped']} 条中"
                    f"取前 {payload['kept']} 条（--count={payload['kept']}）"
                )
            elif stage == "keywords":
                console.print(f"    🏷️  关键词命中 {payload['count']} 个主题")
            elif stage == "done":
                console.print(f"  ✅ {payload['feed']} 新增 {payload['new']} 篇")

        total_new = 0
        for feed in feeds:
            console.print(f"📥 抓取: {feed['name']} ({feed['url']})")
            try:
                new_count, _ = await fetch_and_store(
                    session, dict(feed), fetcher,
                    count=count, progress=_cli_progress,
                )
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


# ── extract（实体抽取，DESIGN §14 2.3，fix #79） ────────────────────

@app.command()
def extract(
    article_id: int = typer.Argument(help="文章 ID"),
):
    """抽取文章实体并入库（DESIGN §14 2.3）。"""
    _run_async(_extract(article_id))


async def _extract(article_id: int):
    from app.llm.client import LLMClient
    from app.llm.factory import build_provider
    from app.services.entities import extract_entities

    settings = load_settings()
    provider = build_provider("generate", settings)
    llm_client = LLMClient(provider=provider, max_concurrency=1)
    async with get_session(settings) as session:
        await extract_entities(session, article_id, settings, llm_client)
        await session.commit()
        console.print(f"[green]✅ 实体抽取完成: article {article_id}[/green]")


# ── report（日报/周报，DESIGN §14 2.5，fix #79） ────────────────────

@app.command()
def report(
    action: str = typer.Argument(help="generate | list"),
    report_type: str = typer.Option("daily", "--type", "-t", help="daily|weekly（generate 时）"),
    limit: int = typer.Option(20, "--limit", "-l"),
):
    """报告管理。generate = 生成日报/周报；list = 列出报告。"""
    _run_async(_report(action, report_type, limit))


async def _report(action: str, report_type: str, limit: int):
    settings = load_settings()

    if action == "list":
        async with get_session(settings) as session:
            result = await session.execute(
                text(
                    "SELECT id, report_type, period_start, period_end, status, created_at "
                    "FROM reports ORDER BY created_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            rows = result.mappings().all()
        if not rows:
            console.print("[yellow]没有报告[/yellow]")
            return
        table = Table(title="报告列表")
        table.add_column("ID", style="dim")
        table.add_column("类型")
        table.add_column("周期开始")
        table.add_column("状态")
        table.add_column("创建时间")
        for r in rows:
            color = "green" if r["status"] == "succeeded" else "yellow" if r["status"] == "pending" else "red"
            table.add_row(str(r["id"]), r["report_type"], str(r["period_start"])[:10], f"[{color}]{r['status']}[/{color}]", str(r["created_at"])[:19])
        console.print(table)
        return

    if action != "generate":
        console.print(f"[red]未知操作: {action}（generate | list）[/red]")
        return

    from datetime import datetime

    from app.llm.client import LLMClient
    from app.llm.factory import build_provider
    from app.services.reports import generate_daily_report, generate_weekly_report

    provider = build_provider("generate", settings)
    llm_client = LLMClient(provider=provider, max_concurrency=1)
    async with get_session(settings) as session:
        if report_type == "weekly":
            rid = await generate_weekly_report(session, datetime.now(), settings, llm_client)
        else:
            rid = await generate_daily_report(session, datetime.now(), settings, llm_client)
    console.print(f"[green]✅ {report_type} 报告生成完成 (id={rid})[/green]")


# ── backup ─────────────────────────────────────────────────────────

@app.command()
def backup():
    """备份数据库（pg_dump | gzip）。"""
    import subprocess
    result = subprocess.run(["bash", "scripts/backup.sh"], capture_output=False)
    if result.returncode != 0:
        console.print("[red]备份失败[/red]")
        raise typer.Exit(1)


# ── reindex（fix #34） ────────────────────────────────────────────

@app.command()
def reindex(
    all_articles: bool = typer.Option(True, "--all", help="重建全部文章的 tsv（默认全量，保留兼容）"),
    only_null: bool = typer.Option(False, "--only-null", help="仅重建 tsv IS NULL 的文章（增量快速模式，fix #39 之前默认漏掉仅含摘要段的存量）"),
    wiki: bool = typer.Option(False, "--wiki", help="同时回填 wiki_pages.tsv（fix #57，默认 --all 时亦回填 wiki）"),
    batch_size: int = typer.Option(500, "--batch-size", "-b", help="每批处理条数"),
):
    """重建 articles.tsv + wiki_pages.tsv（纯本地 jieba + UPDATE，fix #34/#39/#57）。"""
    # fix #39：默认全量，避免漏掉 PR #1 前已 summarize（tsv 仅含摘要段非 NULL）的存量
    effective_all = not only_null
    # 兼容：显式 --no-all 视为 only_null（typer 自动提供 --no-all 取反）
    if not all_articles and not only_null:
        effective_all = False
    # fix #57：默认 --all 同时回填 wiki（显式 --wiki 亦触发）
    do_wiki = wiki or effective_all
    _run_async(_reindex(effective_all, batch_size, do_wiki))


async def _reindex(all_articles: bool, batch_size: int, do_wiki: bool = True):
    """遍历文章/词条调用 update_*_tsv（自动补齐段）。"""
    from app.db.fts import update_article_tsv, update_wiki_tsv

    settings = load_settings()
    factory = get_session_factory(settings)

    # ── articles ──
    async with factory() as session:
        where_clause = "" if all_articles else "WHERE tsv IS NULL"
        result = await session.execute(text(f"SELECT id FROM articles {where_clause} ORDER BY id"))
        ids = [r[0] for r in result.fetchall()]

    if not ids:
        console.print("[green]无需重建 articles：没有匹配的文章[/green]")
    else:
        console.print(f"🔄 重建 articles tsv：共 {len(ids)} 篇 (batch={batch_size})")
        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            async with factory() as session:
                for aid in batch:
                    await update_article_tsv(session, aid, "", "")
                await session.commit()
            console.print(f"  ✓ articles {min(i+batch_size, len(ids))}/{len(ids)}")
        console.print(f"[green]✅ articles tsv 重建完成：{len(ids)} 篇[/green]")

    # ── wiki_pages ──（fix #57）
    if not do_wiki:
        return
    async with factory() as session:
        where_clause = "" if all_articles else "WHERE tsv IS NULL"
        try:
            result = await session.execute(text(f"SELECT id FROM wiki_pages {where_clause} ORDER BY id"))
            wiki_ids = [r[0] for r in result.fetchall()]
        except Exception:
            console.print("[yellow]wiki_pages 表不存在或 tsv 列未就绪，跳过 wiki 回填[/yellow]")
            return
    if not wiki_ids:
        console.print("[green]无需重建 wiki：没有匹配的词条[/green]")
        return
    console.print(f"🔄 重建 wiki_pages tsv：共 {len(wiki_ids)} 篇 (batch={batch_size})")
    for i in range(0, len(wiki_ids), batch_size):
        batch = wiki_ids[i : i + batch_size]
        async with factory() as session:
            for wid in batch:
                await update_wiki_tsv(session, wid)
            await session.commit()
        console.print(f"  ✓ wiki {min(i+batch_size, len(wiki_ids))}/{len(wiki_ids)}")
    console.print(f"[green]✅ wiki_pages tsv 重建完成：{len(wiki_ids)} 篇[/green]")


if __name__ == "__main__":
    app()
