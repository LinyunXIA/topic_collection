"""第七轮验证补遗（N1–N4 / N10）——失败优先验收。全离线。"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import feishu, purge
from feedkicker import topic as topic_mod
from feedkicker.extract_write import link_keys


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _write_cfg(tmp_path, retention: str):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"bitable:\n  enabled: false\n  retention_days: {retention}\n", encoding="utf-8"
    )
    return path


# ── N1 YAML bitable.retention_days 上界 36500（rc2，无 traceback、无删除） ──


def test_yaml_retention_days_over_bound_rc2(tmp_path, monkeypatch, caplog) -> None:
    path = _write_cfg(tmp_path, "999999999")
    calls = {"n": 0}
    monkeypatch.setattr(
        purge.bitable_purge, "purge_expired_records_outcome",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )

    with caplog.at_level(logging.ERROR):
        rc = purge.main(["--config", str(path), "--db", str(tmp_path / "t.db"), "--env", "test"])

    assert rc == 2
    assert calls["n"] == 0
    assert "未捕获异常" not in caplog.text and "Traceback" not in caplog.text


def test_yaml_retention_days_boundary_ok(tmp_path) -> None:
    path = _write_cfg(tmp_path, "36500")

    assert purge.main(["--config", str(path), "--db", str(tmp_path / "t2.db"), "--env", "test"]) == 0


# ── N2 topic 页数按实际页计数（limit=5 合法多页不误熔断；异常不前进仍终止，#348） ──


def test_topic_limit_5_many_pages_not_false_tripped(monkeypatch) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        off = int(args[args.index("--offset") + 1])
        if off >= 4995:
            return FakeProc(0, json.dumps({"data": {"records": [], "has_more": False}}))
        recs = [
            {"record_id": f"rec{off + i:05d}", "fields": {"讨论状态": ["已选题"]}}
            for i in range(5)
        ]
        return FakeProc(0, json.dumps({"data": {"records": recs, "has_more": True}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)

    records = topic_mod.fetch_selected_topics("app", "tbl", limit=5)

    assert len(records) == 4995


def test_topic_stuck_pagination_bounded_by_page_cap(monkeypatch) -> None:
    monkeypatch.setattr(topic_mod.bitable_lark, "_MAX_PAGES", 3)

    def fake_run(args, stdin_text=None, timeout=120):
        recs = [{"record_id": f"rec{i}", "fields": {}} for i in range(5)]
        return FakeProc(0, json.dumps({"data": {"records": recs, "has_more": True}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)

    with pytest.raises(RuntimeError, match="页数|offset|分页"):
        topic_mod.fetch_selected_topics("app", "tbl", limit=5)


# ── N4 markdown 目标任意嵌套括号 ──


def test_link_keys_deeply_nested_markdown_parens() -> None:
    url = "https://e.com/a_(b_(c))"

    assert link_keys(f"[x]({url})") == link_keys(url) == {url}


def test_link_keys_single_nested_still_ok() -> None:
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"

    assert link_keys(f"[x]({url})") == {url}


# ── N10 飞书业务码字符串形态归一 ──


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"code": "0"}, True),
        ({"StatusCode": "0"}, True),
        ({"code": "19021"}, False),
        ({"code": "abc"}, False),
    ],
)
def test_send_code_string_normalized(monkeypatch, payload, ok) -> None:
    monkeypatch.setattr(feishu.httpx, "post", lambda *a, **k: FakeResp(payload))

    assert feishu.send({"msg_type": "text"}, "https://hook.test/x", 5, "ua") is ok
