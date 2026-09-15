"""Round 10 验证补遗：JSON 期望键候选/trim_url 尾标点/非 dict 短路/stderr 脱敏/& 实体化/匹配键不截断（全离线）。"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import bitable_lark, bitable_purge, feishu_card, score_parse
from feedkicker.config_models import Config
from feedkicker.extract_parse import parse_topics
from feedkicker.fetch_url import trim_url
from feedkicker.score_source import name_key, name_text

_DIMS = dict.fromkeys(
    ("普适痛点强度", "分层承载力", "可演示性", "时效与稀缺", "内容复用价值", "讲解成本"), 4.0
)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _topic(name: str) -> dict:
    return {
        "话题名称": name,
        "可使用工具": "工具X",
        "相关AI原理": "原理Y",
        "资讯链接": ["https://a.com/1"],
        "出处来源": ["源Z"],
    }


def _score_item(name: str, record_id: str = "") -> dict:
    return {
        "话题名称": name,
        "record_id": record_id,
        "gate": "pass",
        "dimensions": {k: 4.0 for k in _DIMS},
        "reason": "依据 可使用工具",
        "weighted_total": 4.0,
    }


# ── P1-1 JSON 候选按期望键选择 ──


def test_p1_real_fence_beats_longer_bare_example() -> None:
    real = json.dumps({"topics": [_topic("真话题")]}, ensure_ascii=False)
    example = json.dumps({"topics": [_topic("示例话题" * 30)]}, ensure_ascii=False)
    raw = f"```json\n{real}\n```\n说明：格式示例 {example} 请忽略"

    got, dropped = parse_topics(raw)

    assert [t["话题名称"] for t in got] == ["真话题"] and dropped == 0


def test_p1_last_fence_wins_over_longer_example_fence() -> None:
    example = json.dumps({"topics": [_topic("示例话题" * 30)]}, ensure_ascii=False)
    real = json.dumps({"topics": [_topic("真话题")]}, ensure_ascii=False)
    raw = f"示例：\n```json\n{example}\n```\n真实结果：\n```json\n{real}\n```"

    got, _dropped = parse_topics(raw)

    assert [t["话题名称"] for t in got] == ["真话题"]


def test_p1_empty_topics_beats_spurious_example() -> None:
    example = json.dumps({"results": [_topic("示例话题")]}, ensure_ascii=False)
    raw = f'{{"topics": []}}\n（示例：{example}）'

    got, dropped = parse_topics(raw)

    assert got == [] and dropped == 0


def test_p1_topics_beats_longer_scores_block() -> None:
    scores = json.dumps({"scores": [_score_item("假分")] * 30}, ensure_ascii=False)
    real = json.dumps({"topics": [_topic("真话题")]}, ensure_ascii=False)
    raw = f"{real}\n{scores}"

    got, _dropped = parse_topics(raw)

    assert [t["话题名称"] for t in got] == ["真话题"]


def test_p1_score_expected_keys() -> None:
    fake = json.dumps({"topics": [_topic("假话题")]}, ensure_ascii=False)
    real = json.dumps({"scores": [_score_item("真实话题")]}, ensure_ascii=False)

    items, dropped, _keys = score_parse.parse_results_full(f"{fake}\n{real}")

    assert [i["话题名称"] for i in items] == ["真实话题"] and dropped == 0


# ── P2-1 trim_url 裸尾标点 ──


@pytest.mark.parametrize("token", ["https://e.com/a.", "https://e.com/a!", "https://e.com/a,"])
def test_p2_bare_trailing_ascii_punct_stripped(token: str) -> None:
    assert trim_url(token) == "https://e.com/a"


def test_p2_query_tail_and_balanced_parens_preserved() -> None:
    assert trim_url("https://e.com/search?q=a!") == "https://e.com/search?q=a!"
    assert trim_url("https://e.com/a_(b_(c))") == "https://e.com/a_(b_(c))"
    assert trim_url("(https://e.com/a)") == "https://e.com/a"
    assert trim_url("<https://e.com/a>") == "https://e.com/a"


# ── P3-1 purge 非 dict 短路 ──


def test_p3_purge_non_dict_page_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"ok": True, "data": [1, 2, 3]}, ensure_ascii=False)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError, match="无法识别"):
        bitable_purge._list_records("app", "tbl")


# ── P3-2 名桶空但 id 唯一命中 ──


def test_p3_empty_bucket_unique_id_accepted() -> None:
    rows = [{"record_id": "rec1", "话题名称": "A"}]

    normalized, _dist, dropped = score_parse.normalize_results(
        [_score_item("A 话题", record_id="rec1")], rows
    )

    assert dropped == 0 and len(normalized) == 1 and normalized[0]["record_id"] == "rec1"


def test_p3_used_id_not_reused_for_other_name() -> None:
    rows = [{"record_id": "rec1", "话题名称": "A"}, {"record_id": "rec2", "话题名称": "B"}]

    normalized, _dist, dropped = score_parse.normalize_results(
        [_score_item("A"), _score_item("别名", record_id="rec1")], rows
    )

    assert [n["record_id"] for n in normalized] == ["rec1"] and dropped == 2


# ── P3-3 stderr 脱敏 ──


def test_p3_run_stderr_redacted(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    secret = "SECRET_APP_TOKEN_XYZ"
    proc = FakeProc(2, stdout="", stderr=f"Error: invalid --base-token {secret} --table-id tblSECRET")

    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/x/lark-cli")
    monkeypatch.setattr(bitable_lark.subprocess, "run", lambda *a, **k: proc)

    with caplog.at_level(logging.WARNING):
        bitable_lark._run(["base", "+record-list", "--base-token", secret], timeout=1)

    assert secret not in caplog.text and "tblSECRET" not in caplog.text


# ── P3-4 仅实体样式 & 转义 ──


def test_p3_plain_ampersand_unchanged() -> None:
    assert feishu_card.escape_inline("AI & 医疗") == "AI & 医疗"


def test_p3_entity_style_ampersand_neutralized() -> None:
    out = feishu_card.escape_inline("&lt;at id=all&gt;")

    assert "<" not in out and ">" not in out and "&amp;lt;" in out


# ── P3-5 匹配键不截断 ──


def test_p3_name_key_not_truncated() -> None:
    assert name_key("甲" * 200 + "X") != name_key("甲" * 200 + "Y")
    assert name_text("甲" * 200 + "X") == "甲" * 200


def test_p3_long_name_injection_still_matches() -> None:
    row = {"record_id": "recX", "话题名称": "超" * 250}
    shown = name_text(row["话题名称"])

    normalized, _dist, dropped = score_parse.normalize_results([_score_item(shown)], [row])

    assert dropped == 0 and len(normalized) == 1 and normalized[0]["record_id"] == "recX"


# ── P3-6 reseed 护栏自证 ──


def test_p3_reseed_same_base_guard_precedes_lark(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from feedkicker import bitable, bitable_schema

    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = cfg.salon.app_token = "appSame"
    cfg.bitable.table_id = cfg.salon.table_id = "tblSame"
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/x/lark-cli")
    monkeypatch.setattr(
        bitable_schema, "ensure_initialized",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("护栏前不得建/查 Base")),
    )

    with caplog.at_level(logging.ERROR):
        rc = bitable.main(["--reseed", "--env", "test"])

    assert rc == 2 and "与 salon 选题 Base 相同" in caplog.text


def _T(name: str) -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "话题名称": name,
                    "可使用工具": "T",
                    "相关AI原理": "P",
                    "资讯链接": ["https://e.com/1"],
                    "出处来源": ["F"],
                }
            ]
        },
        ensure_ascii=False,
    )


_P1_REAL = _T("真话题")
_P1_LONG_EXAMPLE = "\n示例：\n" + _T("示例话题") + "\n" + "（以上为格式示例，请按此输出）" * 20

_P1_CASES = {
    "A": _P1_REAL + _P1_LONG_EXAMPLE,
    "B": "```json\n" + _T("示例话题") + "\n```\n" + "多余说明" * 50 + "\n```json\n" + _P1_REAL + "\n```",
    "C": '{"topics": []}' + "\n" + _P1_LONG_EXAMPLE,
    "D": _P1_REAL + '\n{"scores":[{"话题名称":"x","gate":"pass","dimensions":{"a":1},"reason":"y"}],"pad":"' + "z" * 400 + '"}',
}


@pytest.mark.parametrize(
    ("case", "expected"),
    [("A", ["真话题"]), ("B", ["真话题"]), ("C", []), ("D", ["真话题"])],
)
def test_p1_residue_topics_cases(case: str, expected: list[str]) -> None:
    got, dropped = parse_topics(_P1_CASES[case])

    assert [t["话题名称"] for t in got] == expected and dropped == 0


def _S(name: str) -> str:
    return json.dumps(
        {"scores": [{"话题名称": name, "gate": "pass", "dimensions": dict(_DIMS), "reason": "依据"}]},
        ensure_ascii=False,
    )


_SCORE_REAL = _S("真话题")
_SCORE_LONG_EXAMPLE = "\n示例：\n" + _S("示例话题") + "\n" + "（以上为格式示例，请按此输出）" * 20
_SCORE_CASES = {
    "A": _SCORE_REAL + _SCORE_LONG_EXAMPLE,
    "B": "```json\n" + _S("示例话题") + "\n```\n" + "多余说明" * 50 + "\n```json\n" + _SCORE_REAL + "\n```",
    "C": '{"scores": []}' + "\n" + _SCORE_LONG_EXAMPLE,
    "D": _SCORE_REAL + '\n{"topics":[{"话题名称":"x"}],"pad":"' + "z" * 400 + '"}',
}


@pytest.mark.parametrize(
    ("case", "expected"),
    [("A", ["真话题"]), ("B", ["真话题"]), ("C", []), ("D", ["真话题"])],
)
def test_p1_residue_score_cases(case: str, expected: list[str]) -> None:
    items, dropped, _keys = score_parse.parse_results_full(_SCORE_CASES[case])

    assert [i["话题名称"] for i in items] == expected and dropped == 0
