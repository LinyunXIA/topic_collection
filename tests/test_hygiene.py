"""#205：真实 prod record id 已替换为合成 id，tracked 全仓零命中。"""

from __future__ import annotations

import subprocess

from feedkicker.config import PROJECT_ROOT

_NEEDLE = "recGWg8" + "Kb9kUDI"


def test_no_real_prod_record_id_in_tracked_files() -> None:
    proc = subprocess.run(
        ["git", "grep", "-n", _NEEDLE],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, f"仍存在真实 prod record id:\n{proc.stdout}"
