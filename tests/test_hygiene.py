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
