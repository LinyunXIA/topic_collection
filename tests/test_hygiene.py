"""#205/#236：真实 prod record id 不再以明文留在 tracked 文件，只以 sha256 canary 守护。

判据两层：短 token 集合逐个 sha256 与 `_FLAGGED_SHA256` 比对；≥15 字符的
record-id 形态 token 必须付之阙如（`git grep` 无匹配 rc==1）。非 git 工作树
（rc=128）会显式失败而非假绿；无 git 二进制同样 fail。
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from feedkicker.config import PROJECT_ROOT

_FLAGGED_SHA256 = "d8fee92aca715b955e88e7560005abe7e68f27e4eb7e767299867640074dfcd8"

_TOKEN_RE = r"rec[A-Za-z0-9]{8,}"

_LONG_TOKEN_RE = r"rec[A-Za-z0-9]{12,}"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
        )
    except OSError as e:
        pytest.fail(f"git 不可用，脱敏 canary 无法验证（不得假绿）: {e}")


def _require_work_tree() -> None:
    proc = _git("rev-parse", "--is-inside-work-tree")
    assert proc.returncode == 0 and proc.stdout.strip() == "true", (
        f"必须在 git 工作树内运行脱敏 canary（仓外 rc=128 是假绿）: {proc.stderr.strip()}"
    )


def test_no_real_prod_record_id_in_tracked_files() -> None:
    _require_work_tree()
    proc = _git("grep", "-h", "-o", "-E", _TOKEN_RE)
    assert proc.returncode in (0, 1), f"git grep 异常 rc={proc.returncode}: {proc.stderr.strip()}"
    tokens = sorted({t for t in proc.stdout.splitlines() if t})
    hits = [t for t in tokens if hashlib.sha256(t.encode()).hexdigest() == _FLAGGED_SHA256]
    assert hits == [], f"tracked 文件仍含真实 prod record id（哈希命中 {len(hits)} 个 token）"


def test_no_long_record_id_tokens_in_tracked_files() -> None:
    _require_work_tree()
    proc = _git("grep", "-h", "-o", "-E", _LONG_TOKEN_RE)
    assert proc.returncode == 1, (
        "tracked 文件出现 ≥15 字符的 record-id 形态 token（无匹配时 rc==1）:\n" + proc.stdout
    )


_DOC_PATHSPEC = ("docs", "README.md", "AGENTS.md", "*.example")

_BASE_TOKEN_RE = r"[A-Za-z0-9]{20,}"

_TABLE_ID_RE = r"tbl[A-Za-z0-9]{12,}"


def _looks_like_base_token(token: str) -> bool:
    """真实 Base app_token 形态：≥20 位字母数字、同时含字母与数字（排除 pyright 规则名等纯字母标识符）。"""
    return len(token) >= 20 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def _doc_matches(pattern: str) -> list[str]:
    proc = _git("grep", "-n", "-o", "-E", pattern, "--", *_DOC_PATHSPEC)
    assert proc.returncode in (0, 1), f"git grep 异常 rc={proc.returncode}: {proc.stderr.strip()}"
    return [ln for ln in proc.stdout.splitlines() if ln]


def test_base_token_predicate_ignores_code_identifiers() -> None:
    assert _looks_like_base_token("Abc123Def456Ghi789Jkl0")
    assert not _looks_like_base_token("reportImplicitStringConcatenation")
    assert not _looks_like_base_token("short")


def test_no_real_base_token_shape_in_tracked_docs() -> None:
    _require_work_tree()
    hits = [ln for ln in _doc_matches(_BASE_TOKEN_RE) if _looks_like_base_token(ln.rsplit(":", 1)[-1])]
    assert hits == [], f"tracked 文档出现 Base token 形态标识符（脱敏回归）: {hits[:5]}"


def test_no_real_table_id_shape_in_tracked_docs() -> None:
    _require_work_tree()
    hits = _doc_matches(_TABLE_ID_RE)
    assert hits == [], f"tracked 文档出现 tbl 表 id 形态标识符（脱敏回归）: {hits[:5]}"
