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

_MAX_PAGES = 1000

MAX_OFFSET_LAST = _CHUNK * _MAX_PAGES

_LARK_CANDIDATES = ("/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli")


def _guard_offset(offset: int) -> None:
    """绝对兜底（页指纹为主检测，#232）：上限 = `_CHUNK × _MAX_PAGES` = 20 万 offset（1000 页）。

    取飞书单表量级（十万行）之上、远离现实归档规模，仅防指纹失效；合法大表不误杀（#245）。
    """
    if offset > MAX_OFFSET_LAST:
        raise RuntimeError(
            f"分页 offset 超过绝对兜底 {MAX_OFFSET_LAST}，疑似未按 --offset 翻页，中止以避免死循环"
        )


def guard_pages(pages: int) -> None:
    """页数兜底（与 `--limit` 无关，_MAX_PAGES=1000）：offset 天花板不是唯一兜底（#290）。"""
    if pages > _MAX_PAGES:
        raise RuntimeError(
            f"分页超过页数上限 {_MAX_PAGES}（与 --limit 无关），疑似未按 --offset 翻页，中止以避免死循环"
        )


def _page_fingerprint(page: Any) -> str:
    """本页指纹：id 集合取 sorted sha1（无序化），无 id 时对行内容排序后 sha1。

    CLI 忽略 --offset 且每次打乱行序时，保序指纹永不命中；无序化后第 2 页即熔断（#243）。
    """
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
    top_ids = page.get("record_ids") or page.get("recordIds") or page.get("ids")
    ids = [str(i) for i in top_ids if i] if isinstance(top_ids, list) else []
    if ids:
        return hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()
    rows = page.get("data")
    if isinstance(rows, list) and rows:
        canon = sorted(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows)
        return hashlib.sha1("\n".join(canon).encode()).hexdigest()
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
    显式增补 homebrew 与二进制所在目录（#123）。lark_bin 缺失一并返回 None，
    由调用方按其「无返回」语义降级（#237）。
    """
    try:
        bin_path = lark_bin()
        cmd = [bin_path] + args
        env = os.environ.copy()
        extra = [os.path.dirname(bin_path), "/opt/homebrew/bin", "/usr/local/bin"]
        env["PATH"] = os.pathsep.join([p for p in extra if p] + [env.get("PATH", "")])
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
