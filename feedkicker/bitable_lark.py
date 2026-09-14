from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def _shanghai_tz():
    try:
        return ZoneInfo("Asia/Shanghai")
    except (ValueError, OSError):
        return timezone(timedelta(hours=8))


SHANGHAI = _shanghai_tz()

log = logging.getLogger(__name__)

_CHUNK = 200

MAX_OFFSET = 20000

MAX_OFFSET_LAST = 200000

_LARK_CANDIDATES = ("/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli")


def _guard_offset(offset: int) -> None:
    """绝对兜底（页指纹为主检测，#232）：正常大表可翻到 20 万 offset，仅防指纹失效。"""
    if offset > MAX_OFFSET_LAST:
        raise RuntimeError(
            f"分页 offset 超过绝对兜底 {MAX_OFFSET_LAST}，疑似未按 --offset 翻页，中止以避免死循环"
        )


def _page_fingerprint(page: Any) -> str:
    """本页指纹：优先 id 集合的 sorted sha1，无 id 时对行式数据做 sha1。"""
    if not isinstance(page, dict):
        return ""
    records = page.get("records") or page.get("items")
    if isinstance(records, list) and records:
        ids = [
            str(r.get("record_id") or r.get("id") or r.get("recordId") or "")
            for r in records
            if isinstance(r, dict)
        ]
        ids = [i for i in ids if i]
        if ids:
            return hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()
    rows = page.get("data")
    if isinstance(rows, list) and rows:
        return hashlib.sha1(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return ""


def _page_guard(prev_fp: str, page: Any) -> str:
    """本页指纹与上页相同即判定 lark-cli 忽略 --offset，raise 而非靠 offset 天花板（#232）。"""
    fp = _page_fingerprint(page)
    if prev_fp and fp and fp == prev_fp:
        raise RuntimeError("分页未前进（疑似 lark-cli 忽略 --offset），中止以避免死循环")
    return fp


def lark_bin() -> str:
    found = shutil.which("lark-cli")
    if found:
        return found
    for p in _LARK_CANDIDATES:
        if shutil.which(p):
            return p
    raise FileNotFoundError("找不到 lark-cli，请先安装 @larksuite/cli 并完成 auth login")


def _run(
    args: list[str], stdin_text: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str] | None:
    """执行 lark-cli 子进程。

    launchd 的 PATH 只有 /usr/bin:/bin，lark-cli 是 env node 脚本会以 rc=127 失败；
    显式增补 homebrew 与二进制所在目录（#123）。
    """
    bin_path = lark_bin()
    cmd = [bin_path] + args
    env = os.environ.copy()
    extra = [os.path.dirname(bin_path), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join([p for p in extra if p] + [env.get("PATH", "")])
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("lark-cli 执行异常: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("lark-cli 失败(%d): %s", proc.returncode, proc.stderr.strip()[:300])
    return proc


def _parse(proc: subprocess.CompletedProcess[str] | None) -> tuple[bool, dict[str, Any]]:
    """lark-cli 业务失败时退出码仍为 0，失败信号在 stdout JSON 顶层 ok:false；

    非 JSON 输出（markdown/help）以 returncode 判定。
    """
    if proc is None or proc.returncode != 0:
        return False, {}
    raw = (proc.stdout or "").strip()
    if not raw or raw[0] not in "{[":
        return True, {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return True, {}
    if not isinstance(obj, dict):
        return True, {}
    if obj.get("ok") is False:
        err = obj.get("error") or {}
        log.warning("lark-cli 业务失败: %s", str(err.get("message") or err)[:300])
        return False, {}
    return True, (obj.get("data") or {})


def _ok(proc) -> bool:
    return _parse(proc)[0]


def _data(proc: subprocess.CompletedProcess[str] | None) -> dict[str, Any]:
    return _parse(proc)[1]


@contextlib.contextmanager
def _json_arg(payload: dict[str, Any]):
    """lark-cli 的 --json 不支持 stdin、@文件只接受 cwd 内相对路径；

    大批记录走 argv 会超 ARG_MAX（Errno 7 Argument list too long），落临时文件传引用。
    """
    fd, tmp_path = tempfile.mkstemp(prefix=".lark-json-", suffix=".json", dir=".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        yield "--json", f"@./{os.path.basename(tmp_path)}"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _has_batch_verb() -> str | None:
    proc = _run(["base", "--help"])
    txt = proc.stdout if proc and proc.stdout else ""
    if "+record-batch-update" in txt:
        return "+record-batch-update"
    if "+record-update" in txt:
        return "+record-update"
    return None


def _markdown_record_ids(stdout: str) -> list[str]:
    ids = []
    for line in stdout.splitlines():
        m = re.match(r"^\|\s*(rec[A-Za-z0-9]+)\s*\|", line)
        if m:
            ids.append(m.group(1))
    return ids
