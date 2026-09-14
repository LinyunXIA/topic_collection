from __future__ import annotations

from pathlib import Path

import pytest

from feedkicker.config import load_config


def _isolate_env(monkeypatch):
    for name in ("MiniMax_Key", "MINIMAX_API_KEY", "TC_SALON_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _yaml(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config-alias.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_canonical_keys_parse_and_wiki_falls_back_to_salon(tmp_path, monkeypatch):
    """#162：文档化键为唯一入口；wiki.* 仍按 DESIGN §22 回退 salon.wiki_*。"""
    _isolate_env(monkeypatch)
    path = _write_config(
        tmp_path,
        _yaml(
            "salon:",
            "  enabled: true",
            "  app_token: app-canonical",
            "  table_id: tbl-canonical",
            "  wiki_space_id: spc-canonical",
            "  wiki_parent_token: parent-canonical",
            "minimax:",
            "  api_key: sk-canonical",
            "  model: MiniMax-M9",
            "  base_url: https://api.example.com",
        ),
    )
    cfg = load_config(config_path=path, app_env="test")
    assert cfg.salon.app_token == "app-canonical"
    assert cfg.salon.wiki_space_id == "spc-canonical"
    assert cfg.salon.wiki_parent_token == "parent-canonical"
    assert cfg.minimax.api_key == "sk-canonical"
    assert cfg.minimax.model == "MiniMax-M9"
    assert cfg.minimax.base_url == "https://api.example.com"
    assert cfg.wiki.space_id == "spc-canonical"
    assert cfg.wiki.parent_token == "parent-canonical"


@pytest.mark.parametrize("raw", ["0", "-30"])
def test_bootstrap_days_clamped_to_min_one(tmp_path, monkeypatch, raw):
    """#273：YAML 0/负值不得原样接受（首跑会把全部有日期条目置 pushed，静默丢历史）。"""
    _isolate_env(monkeypatch)
    path = _write_config(tmp_path, _yaml(f"bootstrap_days: {raw}"))

    assert load_config(config_path=path, app_env="test").bootstrap_days == 1


def test_undocumented_aliases_are_not_honored(tmp_path, monkeypatch):
    """#162：salon.wiki_space / salon.minimax_api_key / minimax.minimax_api_key /
    wiki.wiki_space_id / wiki.wiki_parent_token 别名已移除，读取即得默认值或走文档化回退。"""
    _isolate_env(monkeypatch)
    path = _write_config(
        tmp_path,
        _yaml(
            "salon:",
            "  wiki_space: spc-alias",
            "  wiki_parent_token: parent-salonly",
            "  minimax_api_key: sk-alias",
            "minimax:",
            "  minimax_api_key: sk-nested-alias",
            "wiki:",
            "  wiki_space_id: spc-wiki-alias",
            "  wiki_parent_token: parent-wiki-alias",
        ),
    )
    cfg = load_config(config_path=path, app_env="test")
    assert cfg.salon.wiki_space_id == ""
    assert cfg.salon.wiki_parent_token == "parent-salonly"
    assert cfg.minimax.api_key == ""
    assert cfg.wiki.space_id == ""
    assert cfg.wiki.parent_token == "parent-salonly"
