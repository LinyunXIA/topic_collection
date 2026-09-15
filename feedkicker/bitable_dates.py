"""归档日期/推送时间归一（自 bitable_backfill 下沉，DESIGN §3）。

`_shanghai_date` 数字分支加合理性闸：仅 10–13 位按 epoch，8/14 位按紧凑日期，其余返回 None，
避免短数字串（`2026`/`12345678`）被当 epoch 落到 1970 → purge 误删（#R9-33 残留，#R10-22）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from feedkicker import bitable_lark


def _cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("link", "text", "value", "title"):
            if k in v and isinstance(v[k], str):
                return v[k]
            if k in v and v[k] is not None:
                return str(v[k])
        vals = [str(x) for x in v.values() if isinstance(x, str) and x]
        return vals[0] if vals else ""
    if isinstance(v, list):
        if v and isinstance(v[0], str):
            return v[0]
        if v and isinstance(v[0], dict):
            for k in ("link", "text", "value"):
                if k in v[0]:
                    return str(v[0][k])
        return ""
    return str(v)


def _compact_date(s: str, fmt: str) -> str | None:
    try:
        dt = datetime.strptime(s, fmt).replace(tzinfo=bitable_lark.SHANGHAI)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def _epoch_date(s: str) -> str | None:
    iv = int(s)
    if iv > 1_000_000_000_000:
        iv = iv // 1000
    try:
        dt = datetime.fromtimestamp(iv, tz=UTC).astimezone(bitable_lark.SHANGHAI)
    except (ValueError, OSError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%d")


def cutoff_date_shanghai(days: int, now: datetime | None = None) -> str:
    """上海时区下 now-days 的 %Y-%m-%d 日期串（字典序即时间序）。"""
    ref = now if now is not None else datetime.now(bitable_lark.SHANGHAI)
    return (ref - timedelta(days=days)).astimezone(bitable_lark.SHANGHAI).strftime("%Y-%m-%d")


def pushed_date(fields: dict[str, Any]) -> str | None:
    """fields 「推送时间」→ 上海 %Y-%m-%d；为空回退「归档日期」（#198 存量行）。

    兼容 epoch 毫秒/ISO/纯日期；归档早于 mark_pushed 时推送时间为空，无回退则这些行永不进入
    保留窗口（F22 契约失效）。
    """
    raw = _cell_str(fields.get("推送时间"))
    if raw:
        d = _shanghai_date(raw)
        if d:
            return d
    arch = _cell_str(fields.get("归档日期"))
    return _shanghai_date(arch) if arch else None


def _shanghai_date(s: str) -> str | None:
    if not s or not s.strip():
        return None
    s = s.strip()
    if s.isdigit():
        if len(s) == 8:
            return _compact_date(s, "%Y%m%d")
        if len(s) == 14:
            return _compact_date(s, "%Y%m%d%H%M%S")
        return _epoch_date(s) if 10 <= len(s) <= 13 else None
    try:
        if "T" in s or s.endswith("Z") or "+" in s[10:]:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=bitable_lark.SHANGHAI)
            else:
                dt = dt.astimezone(bitable_lark.SHANGHAI)
            return dt.strftime("%Y-%m-%d")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)  # noqa: DTZ007
                dt = dt.replace(tzinfo=bitable_lark.SHANGHAI)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=bitable_lark.SHANGHAI)
        else:
            dt = dt.astimezone(bitable_lark.SHANGHAI)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None
