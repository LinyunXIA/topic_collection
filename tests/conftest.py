"""测试基线：注入临时 config-test.yaml，消除对 cwd 与 gitignored 文件的依赖（#163）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from feedkicker import config

_TEST_CONFIG_YAML = """\
bootstrap_days: 1
feeds:
  - name: 量子位
    url: https://www.qbitai.com/feed
bitable:
  enabled: true
http:
  timeout_seconds: 20
"""


@pytest.fixture(autouse=True)
def _isolated_test_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "config-test.yaml"
    path.write_text(_TEST_CONFIG_YAML, encoding="utf-8")
    real_config_path_for = config.config_path_for

    def injected_config_path_for(app_env: str) -> Path:
        return path if app_env == "test" else real_config_path_for(app_env)

    monkeypatch.setattr(config, "config_path_for", injected_config_path_for)


@pytest.fixture(autouse=True)
def _stub_lark_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 dry-run CLI 会实探 lark_bin；测试不依赖宿主机是否安装 lark-cli（#245）。"""
    from feedkicker import bitable_lark

    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/usr/bin/fake-lark-cli")
