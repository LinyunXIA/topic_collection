"""第七轮审计 P2（#288/#289/#291）——失败优先验收。

全离线：lark-cli/httpx 一律 mock。
"""

from __future__ import annotations

import logging
from pathlib import Path

from feedkicker import feishu, push, store
from feedkicker.config import Config, Feed, HttpConf, SiteConf
from feedkicker.config import load_config as load_cfg
from feedkicker.extract_write import link_keys


def _yaml(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config-audit7.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _isolate_env(monkeypatch) -> None:
    for name in ("MiniMax_Key", "MINIMAX_API_KEY", "TC_SALON_TOKEN"):
        monkeypatch.delenv(name, raising=False)


# ── #288 link_keys：markdown 与裸 URL 混排取并集 ──


def test_link_keys_mixed_markdown_and_bare_url_union() -> None:
    assert link_keys("[x](https://e.com/2)\nhttps://e.com/1") == {
        "https://e.com/2",
        "https://e.com/1",
    }


def test_link_keys_markdown_only_still_works() -> None:
    assert link_keys("[量子位](https://e.com/a)") == {"https://e.com/a"}


def test_link_keys_bare_only_still_works() -> None:
    assert link_keys("https://e.com/1\nhttps://e.com/2") == {
        "https://e.com/1",
        "https://e.com/2",
    }


# ── #289 推送侧跨源 canonicalize 去重（渲染层） ──


def _item(feed: str, key: str, title: str, url: str) -> dict:
    return {
        "feed_id": feed,
        "entry_key": key,
        "title": title,
        "url": url,
        "description": "",
        "published_at": None,
    }


def test_build_card_dedupes_same_url_across_sources() -> None:
    """同 URL 不同 guid 的两源条目只渲染一次，主归属取 feed_order 首个，其余标「亦见」。"""
    items = [
        _item("B源", "b1", "B转载", "https://e.com/same"),
        _item("A源", "a1", "A原创", "https://e.com/same"),
    ]
    card = feishu.build_card(items, 0, ["A源", "B源"])
    content = card["card"]["elements"][0]["text"]["content"]

    assert content.count("https://e.com/same") == 1
    assert "A原创" in content
    assert "B转载" not in content
    assert "亦见 B源" in content


def test_build_card_keeps_distinct_urls() -> None:
    items = [
        _item("A源", "a1", "A篇", "https://e.com/a"),
        _item("B源", "b1", "B篇", "https://e.com/b"),
    ]
    card = feishu.build_card(items, 0, ["A源", "B源"])
    content = card["card"]["elements"][0]["text"]["content"]

    assert content.count("https://e.com/a") == 1
    assert content.count("https://e.com/b") == 1
    assert "亦见" not in content


def test_push_dedupes_card_but_marks_all_pending(monkeypatch) -> None:
    """去重只在渲染层：mark_pushed 仍以原始 pending 为准，两条全部标记，不留孤儿。"""
    conn = store.connect(":memory:")
    cfg = Config(
        feishu_webhook="hook-x",
        http=HttpConf(timeout_seconds=5),
        feeds=[Feed(name="A源", url="https://a.example/rss"), Feed(name="B源", url="https://b.example/rss")],
        site=SiteConf(),
    )
    entries_a = [_item("A源", "a1", "A篇", "https://e.com/same")]
    entries_b = [_item("B源", "b1", "B篇", "https://e.com/same")]

    def fake_fetch(url, http):
        return entries_a if "a.example" in url else entries_b

    monkeypatch.setattr(push, "fetch_feed", fake_fetch)
    sent: list[dict] = []
    monkeypatch.setattr(feishu, "send", lambda payload, *a, **kw: sent.append(payload) or True)

    assert push.run(cfg, conn) == 0
    content = sent[0]["card"]["elements"][0]["text"]["content"]
    assert content.count("https://e.com/same") == 1
    assert store.select_pending(conn) == []
    assert len(store.select_unsynced(conn)) == 2
    conn.close()


# ── #291 未知配置键 / 显式 enabled: false 均 WARNING，不硬失败 ──


def test_unknown_config_key_warns_but_still_loads(tmp_path, monkeypatch, caplog) -> None:
    _isolate_env(monkeypatch)
    path = _write_config(
        tmp_path,
        _yaml("salon:", "  enabled: true", "  app_token: app-x", "  enabeld: true"),
    )

    with caplog.at_level(logging.WARNING):
        cfg = load_cfg(config_path=path, app_env="test")

    assert cfg.salon.app_token == "app-x"
    assert any("配置未知键" in r.getMessage() for r in caplog.records)


def test_disabled_segment_warns(tmp_path, monkeypatch, caplog) -> None:
    _isolate_env(monkeypatch)
    path = _write_config(tmp_path, _yaml("salon:", "  enabled: false"))

    with caplog.at_level(logging.WARNING):
        cfg = load_cfg(config_path=path, app_env="test")

    assert cfg.salon.enabled is False
    assert any("salon.enabled" in r.getMessage() for r in caplog.records)
