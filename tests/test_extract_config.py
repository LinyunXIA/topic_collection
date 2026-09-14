"""F29：extract 配置段解析 + provider 抽象（全 mock 离线，不发起真实调用）。"""

from __future__ import annotations

import json

import pytest

from feedkicker import extract_llm
from feedkicker.config import load_config
from feedkicker.config_models import ExtractConf, ProviderConf


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MiniMax_Key", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text_data=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text_data if text_data is not None else json.dumps(json_data or {}, ensure_ascii=False)

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def _ok_resp(text: str = "hello") -> FakeResp:
    return FakeResp(200, {"choices": [{"message": {"content": text}}]})


def test_load_extract_defaults() -> None:
    cfg = load_config(app_env="test")

    assert cfg.extract.enabled is False
    assert cfg.extract.since_days == 7
    assert cfg.extract.batch_size == 30
    assert cfg.extract.provider == "minimax"
    assert cfg.extract.prompt_file == "prompts/extract.md"
    assert cfg.extract.max_calls == 0
    assert cfg.extract.providers == {}


def test_load_extract_section_and_placeholder_cleared(tmp_path) -> None:
    path = tmp_path / "extract.yaml"
    path.write_text(
        "extract:\n"
        "  enabled: true\n"
        "  since_days: 3\n"
        "  batch_size: 5\n"
        "  provider: deepseek\n"
        "  max_calls: 2\n"
        "  providers:\n"
        "    deepseek:\n"
        "      api_key: \"<deepseek-key>\"\n"
        "      model: \"deepseek-reasoner\"\n"
        "      tool_label: \"DS（DeepSeek）\"\n",
        encoding="utf-8",
    )

    cfg = load_config(path, app_env="test")

    assert cfg.extract.enabled is True
    assert cfg.extract.since_days == 3 and cfg.extract.batch_size == 5
    assert cfg.extract.provider == "deepseek" and cfg.extract.max_calls == 2
    ds = cfg.extract.providers["deepseek"]
    assert ds.api_key == ""
    assert ds.model == "deepseek-reasoner"


def test_provider_key_env_fills_placeholder(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-ds")
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-env-mm-lower")
    monkeypatch.setenv("MiniMax_Key", "sk-env-mm")
    path = tmp_path / "extract.yaml"
    path.write_text(
        "extract:\n"
        "  providers:\n"
        "    deepseek:\n"
        "      api_key: \"<deepseek-key>\"\n"
        "    minimax: {}\n",
        encoding="utf-8",
    )

    cfg = load_config(path, app_env="test")

    assert cfg.extract.providers["deepseek"].api_key == "sk-env-ds"
    assert cfg.extract.providers["minimax"].api_key == "sk-env-mm"


def test_resolve_provider_registry_defaults_and_overrides() -> None:
    cfg = ExtractConf(provider="minimax", providers={"minimax": ProviderConf(api_key="sk-1", model="M-custom")})

    conf = extract_llm.resolve_provider(cfg)

    assert conf.base_url == "https://api.minimaxi.com/v1"
    assert conf.model == "M-custom"
    assert conf.tool_label == "MMX（MiniMax）"
    assert conf.api_key == "sk-1"


def test_resolve_provider_unknown_raises() -> None:
    with pytest.raises(RuntimeError, match="未知 extract provider"):
        extract_llm.resolve_provider(ExtractConf(provider="openai"))


def test_missing_key_raises_without_http(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(extract_llm.httpx, "post", lambda *a, **k: calls.append("post"))

    with pytest.raises(RuntimeError, match="缺少 api_key"):
        extract_llm.call_llm(ExtractConf(provider="minimax", providers={}), "probit")

    assert calls == []


def test_call_llm_provider_switch_changes_url(monkeypatch) -> None:
    urls: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        urls.append(url)
        return _ok_resp("原始文本")

    monkeypatch.setattr(extract_llm.httpx, "post", fake_post)
    mm_cfg = ExtractConf(provider="minimax", providers={"minimax": ProviderConf(api_key="k")})
    ds_cfg = ExtractConf(provider="deepseek", providers={"deepseek": ProviderConf(api_key="k")})

    assert extract_llm.call_llm(mm_cfg, "p") == "原始文本"
    assert extract_llm.call_llm(ds_cfg, "p") == "原始文本"

    assert urls == [
        "https://api.minimaxi.com/v1/chat/completions",
        "https://api.deepseek.com/v1/chat/completions",
    ]


def test_call_llm_payload_and_headers(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["json"] = json
        captured["headers"] = headers
        return _ok_resp()

    monkeypatch.setattr(extract_llm.httpx, "post", fake_post)
    cfg = ExtractConf(provider="deepseek", providers={"deepseek": ProviderConf(api_key="sk-x", model="deepseek-chat")})

    extract_llm.call_llm(cfg, "提示词")

    assert captured["json"]["model"] == "deepseek-chat"
    assert captured["json"]["messages"] == [{"role": "user", "content": "提示词"}]
    assert captured["headers"]["Authorization"] == "Bearer sk-x"


def test_call_llm_retries_429_once(monkeypatch) -> None:
    responses = [FakeResp(429, {"error": "rate"}), _ok_resp("ok-2nd")]
    calls: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(extract_llm.httpx, "post", fake_post)

    assert extract_llm.call_llm(ExtractConf(providers={"minimax": ProviderConf(api_key="k")}), "p") == "ok-2nd"
    assert len(calls) == 2


def test_call_llm_retryable_business_code_exhausts(monkeypatch) -> None:
    calls: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(url)
        return FakeResp(200, {"base_resp": {"status_code": 1002}})

    monkeypatch.setattr(extract_llm.httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="可重试错误"):
        extract_llm.call_llm(ExtractConf(providers={"minimax": ProviderConf(api_key="k")}), "p")
    assert len(calls) == 2


def test_call_llm_empty_content_raises(monkeypatch) -> None:
    monkeypatch.setattr(extract_llm.httpx, "post", lambda *a, **k: FakeResp(200, {"choices": []}))

    with pytest.raises(RuntimeError, match="缺少 content"):
        extract_llm.call_llm(ExtractConf(providers={"minimax": ProviderConf(api_key="k")}), "p")
