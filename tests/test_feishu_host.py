from feedkicker import bitable, wiki


def test_feishu_host_env_override(monkeypatch):
    monkeypatch.setenv("TC_FEISHU_HOST", "example.feishu.cn")
    assert bitable.base_url("appT") == "https://example.feishu.cn/base/appT"
    assert wiki.wiki_url("nodeT") == "https://example.feishu.cn/wiki/nodeT"
    assert wiki.docx_url("docT") == "https://example.feishu.cn/docx/docT"


def test_feishu_host_default_when_unset(monkeypatch):
    monkeypatch.delenv("TC_FEISHU_HOST", raising=False)
    assert bitable.base_url("appT") == "https://web91vfvm7.feishu.cn/base/appT"
    assert wiki.wiki_url("nodeT") == "https://web91vfvm7.feishu.cn/wiki/nodeT"
    assert wiki.docx_url("docT") == "https://web91vfvm7.feishu.cn/docx/docT"
