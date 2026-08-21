"""egress 白名单配置化测试 — 根目录 security/web_site_list.yaml

覆盖：
- 文件存在且含 dev/test/prod 三段
- ALLOWED_HOSTS 从 yaml 按 TC_APP_ENV 加载（conftest 强制 test）
- 白名单命中 / 非白名单拒绝 / 私网放行
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core import egress


class TestWhitelistYaml:
    def test_example_file_has_three_env_sections(self):
        # 真实文件 gitignored 不入库；仓库内提交的是 .example.yaml 示例
        p = Path("security/web_site_list.example.yaml")
        assert p.exists(), "缺少 security/web_site_list.example.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for env in ("dev", "test", "prod"):
            assert isinstance(data.get(env), list), f"{env} 段缺失或非列表"
            assert data[env], f"{env} 段为空"

    def test_real_file_gitignored(self):
        # 真实 web_site_list.yaml 应被 .gitignore 忽略，不入库
        gi = Path(".gitignore").read_text(encoding="utf-8")
        assert "web_site_list.yaml" in gi

    def test_allowed_hosts_loaded_from_yaml(self):
        # conftest 强制 TC_APP_ENV=test → 测试白名单必含 api.openai.com（openai provider 测试用）
        assert "api.openai.com" in egress.ALLOWED_HOSTS

    def test_is_allowed_whitelisted(self):
        assert egress._is_allowed("https://api.openai.com/v1/embeddings")

    def test_is_allowed_rejects_non_whitelisted(self):
        assert not egress._is_allowed("https://not-in-whitelist.example.com/v1")

    def test_is_allowed_private_pass(self):
        assert egress._is_allowed("http://localhost:8000/v1/chat/completions")
        assert egress._is_allowed("http://127.0.0.1:8000/")