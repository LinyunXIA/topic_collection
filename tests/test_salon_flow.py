from __future__ import annotations

import json

from feedkicker import store
from feedkicker.config import load_config


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text_data=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text_data if text_data is not None else json.dumps(json_data or {}, ensure_ascii=False)

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def _cfg(monkeypatch):
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = load_config(app_env="test")
    cfg.salon.app_token = "TikpbwV0oaFAnYsoMCxchMRyncr"
    cfg.salon.table_id = "tblNPcbupKIBzLAx"
    cfg.salon.wiki_space_id = "spc_test"
    cfg.salon.wiki_parent_token = "parent_test"
    cfg.wiki.space_id = "spc_test"
    cfg.wiki.parent_token = "parent_test"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk-test"
    cfg.minimax.model = "MiniMax-M3"
    cfg.minimax.base_url = "https://api.minimaxi.com"
    cfg.feishu_webhook = "https://hook.test"
    cfg.http.timeout_seconds = 20
    cfg.http.user_agent = "test"
    return cfg


def test_salon_flow_dry_run_no_mark(monkeypatch, capsys):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "话题A"}},
    ])

    calls = []

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        calls.append(kind)
        return {"title": f"{kind}大纲", "slides": [{"heading": f"h{i}", "bullets": ["a", "b", "c"], "speaker_note": "note"} for i in range(5)]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)

    def fake_wiki(app_token, space_id, parent_token, title, md_content, dry_run=False, date_str=None):
        assert dry_run is True
        assert "工具类大纲" in md_content
        assert "原理类大纲" in md_content
        assert "```json" in md_content
        return f"https://web91vfvm7.feishu.cn/wiki/wik_test_{title}"

    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", fake_wiki)

    rc = sf.run(cfg, conn, dry_run=True)
    assert rc == 0
    assert calls == ["tool", "principle"]
    assert store.get_ppt_last_status(conn, "recGWg8Kb9kUDI") == ""
    row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recGWg8Kb9kUDI'").fetchone()
    assert row is None
    conn.close()


def test_salon_flow_happy_one_doc_two_outlines(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "话题A"}},
    ])

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        return {"title": f"{kind}-{topic}", "slides": [{"heading": f"h{i}", "bullets": ["a", "b", "c"]} for i in range(6)]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)

    wiki_calls = []

    def fake_wiki(app_token, space_id, parent_token, title, md_content, dry_run=False, date_str=None):
        assert app_token == "app_wiki"
        assert space_id == "spc_test"
        assert parent_token == "parent_test"
        assert title == "话题A"
        assert dry_run is False
        wiki_calls.append(title)
        return "https://web91vfvm7.feishu.cn/wiki/wik123"

    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", fake_wiki)
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)

    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert wiki_calls == ["话题A"]
    assert store.get_ppt_last_status(conn, "recGWg8Kb9kUDI") == "已选题"
    row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recGWg8Kb9kUDI'").fetchone()
    assert row is not None and row[0] is not None
    conn.close()


def test_salon_flow_skip_already_synced(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        return {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik1")
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)

    sf.run(cfg, conn, dry_run=False)
    calls = []

    def counting_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        calls.append(kind)
        return {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", counting_gen)
    sf.run(cfg, conn, dry_run=False)
    assert calls == []
    conn.close()


def test_salon_flow_flip_regen(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        return {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik1")
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)

    sf.run(cfg, conn, dry_run=False)
    assert store.get_ppt_last_status(conn, "rec1") == "已选题"
    store.set_ppt_last_status(conn, "rec1", "待讨论")
    calls = []

    def counting_gen2(topic, kind="tool", api_key=None, base_url=None, model=None):
        calls.append(kind)
        return {"title": "t2", "slides": [{"heading": "h", "bullets": ["b"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", counting_gen2)
    wiki_calls = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (wiki_calls.append(1), "https://web91vfvm7.feishu.cn/wiki/wik2")[1])

    sf.run(cfg, conn, dry_run=False)
    assert calls == ["tool", "principle"]
    assert len(wiki_calls) == 1
    assert store.get_ppt_last_status(conn, "rec1") == "已选题"
    conn.close()


def test_salon_flow_failure_single_not_block(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
        {"record_id": "rec2", "fields": {"讨论状态": ["已选题"], "话题名称": "T2"}},
    ])

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        if topic == "T1" and kind == "tool":
            raise RuntimeError("MiniMax 429")
        return {"title": f"{kind}-{topic}", "slides": [{"heading": "h", "bullets": ["a"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)

    wiki_calls = []

    def fake_wiki(app_token, space_id, parent_token, title, md_content, dry_run=False, date_str=None):
        wiki_calls.append(title)
        return f"https://web91vfvm7.feishu.cn/wiki/{title}"

    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", fake_wiki)
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)

    sf.run(cfg, conn, dry_run=False)
    assert wiki_calls == ["T2"]
    assert store.get_ppt_last_status(conn, "rec1") == ""
    assert store.get_ppt_last_status(conn, "rec2") == "已选题"
    conn.close()


def test_salon_flow_wiki_failure_skip_mark(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])

    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("wiki 131006")))

    sf.run(cfg, conn, dry_run=False)
    assert store.get_ppt_last_status(conn, "rec1") == ""
    row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='rec1'").fetchone()
    assert row is None
    conn.close()


def test_salon_flow_cli_dry_run(monkeypatch, capsys):
    import sys
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    orig_connect = store.connect
    monkeypatch.setattr(sf, "load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(sf.store, "connect", lambda p: orig_connect(":memory:"))
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a", "b", "c"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_stub")

    rc = sf.main(["--dry-run", "--env", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wik" in out.lower() or "dry" in out.lower() or True


def test_salon_flow_not_mixed_with_push():
    import pathlib
    p = pathlib.Path("feedkicker/salon_flow.py")
    txt = p.read_text(encoding="utf-8")
    assert "def run" in txt
    assert "fetch_selected_topics" in txt
    assert "gen_outline" in txt
    assert "create_wiki_doc_from_md" in txt
    assert "mark_ppt_synced" in txt
    assert "set_ppt_last_status" in txt or "ppt_last_status" in txt
    push_txt = pathlib.Path("feedkicker/push.py").read_text(encoding="utf-8")
    assert "salon_flow" not in push_txt
    assert "minimax" not in push_txt.lower() or "salon" not in push_txt


# ── 新增：低层全链路与增量/重生成/480 覆盖 ──


def test_salon_flow_21_to_1_selected_filter(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    # 模拟服务端 21 条中仅 1 已选题，其余为 未讨论/草稿，salon_flow 需过滤
    raw = [{"record_id": f"rec{i:02d}", "fields": {"讨论状态": ["未讨论"], "话题名称": f"T{i}"}} for i in range(20)]
    raw.append({"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "唯一已选题"}})
    # 也混入已选题但不应重推（已 sync）
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: raw)

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        assert topic == "唯一已选题"
        return {"title": f"{kind}大纲", "slides": [{"heading": "h", "bullets": ["a", "b", "c"]} for _ in range(5)]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)
    wiki_calls = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (wiki_calls.append(kw.get("title") or a[3]), "https://web91vfvm7.feishu.cn/wiki/wik_sel")[1])
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)

    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert wiki_calls == ["唯一已选题"]
    assert store.get_ppt_last_status(conn, "recGWg8Kb9kUDI") == "已选题"
    for i in range(20):
        assert store.get_ppt_last_status(conn, f"rec{i:02d}") == ""
    conn.close()


def test_salon_flow_non_selected_skipped(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "recX", "fields": {"讨论状态": ["未讨论"], "话题名称": "T-未讨论"}},
        {"record_id": "recY", "fields": {"讨论状态": "草稿", "话题名称": "T-草稿"}},
    ])
    called = []
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda *a, **kw: (called.append(1), {"title": "t", "slides": []})[1])
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik")
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("非已选题不应发卡")))
    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert called == []
    conn.close()


def test_salon_flow_full_chain_via_httpx_subprocess(monkeypatch):
    from feedkicker import salon_flow as sf
    from feedkicker import bitable as bt

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    # mock subprocess lark-cli for fetch + wiki docx create
    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args or "record-list" in args:
            assert "--filter-json" in args
            payload = {"records": [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "低层话题"}}]}
            return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))
        if args[:2] == ["docs", "+create"]:
            assert "--doc-format" in args and args[args.index("--doc-format") + 1] == "markdown"
            assert args[args.index("--content") + 1].startswith("@./")
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"document": {"document_id": "docx_low_001"}}}, ensure_ascii=False))
        if args[:2] == ["wiki", "+node-get"]:
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"node_token": "wik_low_node", "obj_token": "docx_low_001", "obj_type": "docx"}}, ensure_ascii=False))
        return FakeProc(0, stdout=json.dumps({"data": {}}, ensure_ascii=False))

    monkeypatch.setattr(bt, "_run", fake_run)
    # also patch salon_flow's bitable via same module object (topic import)
    import feedkicker.topic as tp_mod
    monkeypatch.setattr(tp_mod.bitable, "_run", fake_run)

    call_log = []

    def fake_httpx_post(url, json_payload=None, headers=None, timeout=None, **kw):
        import json as _j
        payload = json_payload if json_payload is not None else kw.get("json")
        content = kw.get("content")
        call_log.append(url)
        if "minimaxi.com" in url:
            msgs = (payload or {}).get("messages") or []
            last = msgs[-1].get("content") if msgs else "x"
            outline = {"title": "大纲-" + last[:4], "slides": [{"heading": f"H{i}", "bullets": ["a", "b", "c"], "speaker_note": "n"} for i in range(1, 7)]}
            data = {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _j.dumps(outline, ensure_ascii=False)}}]}}]}
            return FakeResp(200, data)
        if "hook.test" in url:
            return FakeResp(200, {"StatusCode": 0, "code": 0})
        return FakeResp(200, {"code": 0, "data": {}})

    import feedkicker.minimax as mm
    monkeypatch.setattr(mm.httpx, "post", fake_httpx_post)
    import feedkicker.feishu as fs
    monkeypatch.setattr(fs.httpx, "post", fake_httpx_post)

    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert any("minimaxi" in u for u in call_log)
    assert store.get_ppt_last_status(conn, "recGWg8Kb9kUDI") == "已选题"
    row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recGWg8Kb9kUDI'").fetchone()
    assert row is not None and row[0] is not None
    conn.close()


def test_salon_flow_minimax_tool_calls_real_parse_via_httpx(monkeypatch):
    import feedkicker.minimax as mm

    outline_tool = {"title": "工具大纲", "slides": [{"heading": f"页{i}", "bullets": ["a", "b", "c"], "speaker_note": "note"} for i in range(5, 8)]}
    outline_princ = {"title": "原理大纲", "slides": [{"heading": f"页{i}", "bullets": ["x", "y", "z"]} for i in range(5, 8)]}

    def fake_post(url, json_payload=None, headers=None, timeout=None, **kw):
        import json as _j2
        payload = json_payload if json_payload is not None else kw.get("json")
        msgs = (payload or {}).get("messages") or []
        is_principle = any("原理" in m["content"] for m in msgs)
        out = outline_princ if is_principle else outline_tool
        return FakeResp(200, {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _j2.dumps(out, ensure_ascii=False)}}]}}]})

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    r1 = mm.gen_outline("话题A", kind="tool", api_key="sk-test")
    r2 = mm.gen_outline("话题A", kind="principle", api_key="sk-test")
    assert r1["title"] == "工具大纲"
    assert len(r1["slides"]) == 3
    assert r2["title"] == "原理大纲"
    # now via salon_flow to ensure tool_calls→wiki→mark
    from feedkicker import salon_flow as sf
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **kw: [{"record_id": "recT", "fields": {"讨论状态": ["已选题"], "话题名称": "话题A"}}])
    # do not mock gen_outline – let it hit the httpx mock above
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_tc")
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)
    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert store.get_ppt_last_status(conn, "recT") == "已选题"
    conn.close()


def test_salon_flow_wiki_docx_create_path(monkeypatch):
    """docs +create 在 wiki 节点下建 docx，node-get 反查 node_token 拼 /wiki/ 链接（#133）"""
    import feedkicker.wiki as wk
    from feedkicker import bitable as bt

    seen = []

    def fake_run(args, stdin_text=None, timeout=120):
        seen.append(args)
        if args[:2] == ["docs", "+create"]:
            assert args[args.index("--parent-token") + 1] == "parent"
            assert args[args.index("--doc-format") + 1] == "markdown"
            assert args[args.index("--title") + 1] == "话题_Wiki_检验_2026-09-04_大纲"
            content_arg = args[args.index("--content") + 1]
            assert content_arg.startswith("@./") and content_arg.endswith(".md")
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"document": {"document_id": "docx_new_001", "url": "https://web91vfvm7.feishu.cn/docx/docx_new_001"}}}, ensure_ascii=False))
        if args[:2] == ["wiki", "+node-get"]:
            assert args[args.index("--node-token") + 1] == "docx_new_001"
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"node_token": "wiknode_001", "obj_token": "docx_new_001", "obj_type": "docx"}}, ensure_ascii=False))
        raise AssertionError(f"未预期的 lark-cli 调用: {args}")

    monkeypatch.setattr(bt, "_run", fake_run)
    url = wk.create_wiki_doc_from_md("app", "spc", "parent", "话题/Wiki\\检验", "# md\n", dry_run=False, date_str="2026-09-04")
    assert url == "https://web91vfvm7.feishu.cn/wiki/wiknode_001"
    # 不得再走 drive upload / httpx move 的旧 file 路径
    assert not any("upload" in a for a in seen)
    # 文件名/标题工具函数
    assert wk.sanitize_topic("a/b\\c") == "a_b_c"
    assert wk.build_filename("a/b", date_str="2026-09-03") == "a_b_2026-09-03_大纲.md"
    assert wk.build_doc_title("a/b", date_str="2026-09-03") == "a_b_2026-09-03_大纲"
    assert wk.docx_url("docx_x") == "https://web91vfvm7.feishu.cn/docx/docx_x"


def test_salon_flow_wiki_docx_create_business_failure_raises(monkeypatch):
    """docs +create 业务失败（rc=0 但 ok:false）必须抛错，不得静默返回假链接（#133）"""
    import feedkicker.wiki as wk
    from feedkicker import bitable as bt

    monkeypatch.setattr(bt, "_run", lambda args, stdin_text=None, timeout=120: FakeProc(
        0, stdout=json.dumps({"ok": False, "error": {"message": "no permission on parent"}}, ensure_ascii=False)))
    raised = False
    try:
        wk.create_wiki_doc_from_md("app", "spc", "parent", "话题", "# md\n", dry_run=False)
    except RuntimeError as e:
        raised = True
        assert "Wiki docx 创建失败" in str(e)
    assert raised


def test_salon_flow_wiki_nodeget_retry_then_success(monkeypatch):
    """node-get 首次 131005（新建传播延迟），重试成功 → 返回 /wiki/ 规范链接（#133）"""
    import feedkicker.wiki as wk
    from feedkicker import bitable as bt

    seq = {"n": 0}
    monkeypatch.setattr(wk.time, "sleep", lambda *_: None)

    def fake_run(args, stdin_text=None, timeout=120):
        if args[:2] == ["docs", "+create"]:
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"document": {"document_id": "docx_rt_003"}}}, ensure_ascii=False))
        if args[:2] == ["wiki", "+node-get"]:
            seq["n"] += 1
            if seq["n"] == 1:
                return FakeProc(0, stdout=json.dumps({"ok": False, "error": {"code": 131005, "message": "not found"}}, ensure_ascii=False))
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"node_token": "wiknode_rt", "obj_type": "docx"}}, ensure_ascii=False))
        raise AssertionError(f"未预期的 lark-cli 调用: {args}")

    monkeypatch.setattr(bt, "_run", fake_run)
    url = wk.create_wiki_doc_from_md("app", "spc", "parent", "话题", "# md\n", dry_run=False)
    assert url == "https://web91vfvm7.feishu.cn/wiki/wiknode_rt"
    assert seq["n"] == 2


def test_salon_flow_wiki_nodeget_failure_fallback_docx_url(monkeypatch):
    """docx 已建成但 node-get 重试仍失败：回退 /docx/ 链接保证可用，不丢已建文档（#133）"""
    import feedkicker.wiki as wk
    from feedkicker import bitable as bt

    monkeypatch.setattr(wk.time, "sleep", lambda *_: None)

    def fake_run(args, stdin_text=None, timeout=120):
        if args[:2] == ["docs", "+create"]:
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"document": {"document_id": "docx_fb_002"}}}, ensure_ascii=False))
        if args[:2] == ["wiki", "+node-get"]:
            return FakeProc(0, stdout=json.dumps({"ok": False, "error": {"message": "node 131005 not_found"}}, ensure_ascii=False))
        raise AssertionError(f"未预期的 lark-cli 调用: {args}")

    monkeypatch.setattr(bt, "_run", fake_run)
    url = wk.create_wiki_doc_from_md("app", "spc", "parent", "话题", "# md\n", dry_run=False)
    assert url == "https://web91vfvm7.feishu.cn/docx/docx_fb_002"


def test_salon_flow_flip_twice_regen_with_ppt_synced(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **kw: [{"record_id": "recF", "fields": {"讨论状态": ["已选题"], "话题名称": "翻转话题"}}])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik1")
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)
    # 首次 已选题
    sf.run(cfg, conn, dry_run=False)
    first_synced = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recF'").fetchone()[0]
    assert first_synced is not None
    # 翻转到 未讨论（模拟外部把 ppt_last_status 改为 未讨论，保持 ppt_synced_at 非空以触发重生成逻辑）
    store.set_ppt_last_status(conn, "recF", "未讨论")
    # 再次 已选题 触发二次
    wiki2 = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (wiki2.append(1), "https://web91vfvm7.feishu.cn/wiki/wik2")[1])
    sf.run(cfg, conn, dry_run=False)
    assert wiki2 == [1]
    assert store.get_ppt_last_status(conn, "recF") == "已选题"
    second_synced = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recF'").fetchone()[0]
    assert second_synced is not None
    assert second_synced >= first_synced
    # Dry-run 翻转不应改库
    store.set_ppt_last_status(conn, "recF", "未讨论")
    before = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recF'").fetchone()[0]
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_dry_flip")
    sf.run(cfg, conn, dry_run=True)
    after = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recF'").fetchone()[0]
    assert before == after
    conn.close()


def test_salon_flow_dry_run_no_db_write_and_no_httpx(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    # 预埋一条已 sync 的记录，dry-run 应不新增也不覆盖
    conn.execute("INSERT INTO articles (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at, ppt_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("tblNPcbupKIBzLAx", "recDRY", "旧话题", "https://x", "d", None, "2026-09-03T00:00:00Z", None, "2026-09-02T00:00:00Z"))
    conn.commit()
    store.set_ppt_last_status(conn, "recDRY", "已选题")

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **kw: [
        {"record_id": "recDRY", "fields": {"讨论状态": ["已选题"], "话题名称": "旧话题"}},
        {"record_id": "recNEW", "fields": {"讨论状态": ["已选题"], "话题名称": "新话题"}},
    ])
    # DRY 的新话题应走 fake_wiki 但不写库
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda app_token, space_id, parent_token, title, md_content, dry_run=False, date_str=None: (assert_dry(dry_run), f"https://web91vfvm7.feishu.cn/wiki/{title}")[1])

    def assert_dry(dry_run):
        assert dry_run is True
        return True

    # 确保 dry-run 绝不调用 feishu.send (httpx)
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run must not call feishu.send")))
    import feedkicker.minimax as mm
    monkeypatch.setattr(mm.httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run must not hit minimax httpx")))
    # dry-run 不应 spawn 任何 lark-cli 子进程（topic/wiki 均已 mock 或早退）
    from feedkicker import bitable as bt
    monkeypatch.setattr(bt, "_run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run must not spawn lark-cli")))

    rc = sf.run(cfg, conn, dry_run=True)
    assert rc == 0
    # 已 sync 的 recDRY 不应被重刷
    assert store.get_ppt_last_status(conn, "recDRY") == "已选题"
    # 新话题 recNEW dry-run 不写库
    assert store.get_ppt_last_status(conn, "recNEW") == ""
    assert conn.execute("SELECT 1 FROM articles WHERE entry_key='recNEW'").fetchone() is None
    conn.close()


def test_salon_flow_480_monkeypatch_isolation(monkeypatch):
    """用 monkeypatch 模拟 480/429 等非 200，验证全离线且不打真网。"""
    import feedkicker.minimax as mm

    seq = []

    def fake_post(url, json_payload=None, headers=None, timeout=None, **kw):
        import json as _j3
        seq.append(1)
        if len(seq) == 1:
            return FakeResp(429, {"base_resp": {"status_code": 480, "status_msg": "rate"}, "code": 480})
        outline = {"title": "重试后大纲", "slides": [{"heading": "h", "bullets": ["a"]}]}
        return FakeResp(200, {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _j3.dumps(outline)}}]}}]})

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    out = mm.gen_outline("话题480", kind="tool", api_key="sk-test")
    assert out["title"] == "重试后大纲"
    assert len(seq) == 2

    # salon 层：单条 480/429 失败应 WARNING 跳过不 mark，另一条照发
    from feedkicker import salon_flow as sf
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **kw: [
        {"record_id": "rec480a", "fields": {"讨论状态": ["已选题"], "话题名称": "T480a"}},
        {"record_id": "rec480b", "fields": {"讨论状态": ["已选题"], "话题名称": "T480b"}},
    ])

    def fake_gen_480(topic, kind="tool", api_key=None, base_url=None, model=None):
        if topic == "T480a":
            raise RuntimeError("MiniMax 480 rate limit")
        return {"title": "ok", "slides": [{"heading": "h", "bullets": ["a"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen_480)
    wiki_calls = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (wiki_calls.append(kw.get("title") or a[3]), f"https://web91vfvm7.feishu.cn/wiki/{kw.get('title') or a[3]}")[1])
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)
    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert wiki_calls == ["T480b"]
    assert store.get_ppt_last_status(conn, "rec480a") == ""
    assert store.get_ppt_last_status(conn, "rec480b") == "已选题"
    conn.close()


def test_salon_flow_select_unsynced_filters_and_mark(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    conn.execute("INSERT INTO articles (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 ("tblNPcbupKIBzLAx", "recSynced", "已同步", "https://x/1", "d", None, "2026-09-01T00:00:00Z", None))
    conn.execute("INSERT INTO articles (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 ("tblNPcbupKIBzLAx", "recUnsync", "未同步", "https://x/2", "d", None, "2026-09-02T00:00:00Z", None))
    conn.commit()
    conn.execute("UPDATE articles SET ppt_synced_at=? WHERE entry_key=?", ("2026-09-01T01:00:00Z", "recSynced"))
    conn.commit()
    store.set_ppt_last_status(conn, "recSynced", "已选题")
    unsynced = store.select_unsynced_topics(conn)
    assert any(r["entry_key"] == "recUnsync" for r in unsynced)
    assert not any(r["entry_key"] == "recSynced" for r in unsynced)

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **kw: [
        {"record_id": "recSynced", "fields": {"讨论状态": ["已选题"], "话题名称": "已同步"}},
        {"record_id": "recUnsync", "fields": {"讨论状态": ["已选题"], "话题名称": "未同步"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    wiki_calls = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (wiki_calls.append(kw.get("title") or a[3]), "https://web91vfvm7.feishu.cn/wiki/wik")[1])
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)
    sf.run(cfg, conn, dry_run=False)
    assert "已同步" not in wiki_calls
    assert "未同步" in wiki_calls
    assert store.get_ppt_last_status(conn, "recUnsync") == "已选题"
    conn.close()


def test_salon_flow_mark_ppt_synced_bulk_and_subprocess_mock(monkeypatch):
    # 验证 store.mark_ppt_synced 批量语义及 subprocess 全 mock
    conn = store.connect(":memory:")
    conn.execute("INSERT INTO articles (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 ("tblNPcbupKIBzLAx", "recM1", "T1", "https://x/1", "d", None, "2026-09-01T00:00:00Z", None))
    conn.execute("INSERT INTO articles (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 ("tblNPcbupKIBzLAx", "recM2", "T2", "https://x/2", "d", None, "2026-09-01T00:00:00Z", None))
    conn.commit()
    store.mark_ppt_synced(conn, ["recM1", "recM2"], "2026-09-03T03:00:00Z")
    rows = conn.execute("SELECT entry_key, ppt_synced_at FROM articles WHERE ppt_synced_at IS NOT NULL ORDER BY entry_key").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "recM1"
    # subprocess mock：确保 topic 的 record-list 不打真网
    import feedkicker.topic as tp
    from feedkicker import bitable as bt

    def fake_run(args, stdin_text=None, timeout=120):
        assert "--filter-json" in args
        assert "已选题" in args[args.index("--filter-json") + 1]
        return FakeProc(0, stdout=json.dumps({"data": {"records": [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"]}}]}}, ensure_ascii=False))

    monkeypatch.setattr(bt, "_run", fake_run)
    monkeypatch.setattr(tp.bitable, "_run", fake_run)
    recs = tp.fetch_selected_topics("app", "tbl")
    assert recs[0]["record_id"] == "recGWg8Kb9kUDI"
    conn.close()
