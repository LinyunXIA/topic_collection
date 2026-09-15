"""F38 `score:` 配置段解析：自 config.py 拆出以守住 ≤200 行门（DESIGN §26.3）。"""

from __future__ import annotations

from typing import Any

from feedkicker.config_models import MAX_SCORE_BATCH, ProviderConf, ScoreConf


def parse_score(
    raw: dict[str, Any], base: ScoreConf, providers: dict[str, ProviderConf]
) -> ScoreConf:
    """解析 score 段：`batch_size` 取 max(1, …) 且不得超过 MAX_SCORE_BATCH，`timeout_seconds` 须 > 0，
    越界 raise（调用方 rc2）。

    providers 的 key 占位/env 回退口径复用 `_providers_conf`（由调用方解析后传入），与
    extract 段一致；prompt_file 为仓库根相对路径，缺省 prompts/score.md。
    """
    batch = max(1, int(raw.get("batch_size", base.batch_size)))
    if batch > MAX_SCORE_BATCH:
        raise ValueError(f"score.batch_size={batch} 超过上界 {MAX_SCORE_BATCH}")
    timeout = float(raw.get("timeout_seconds", base.timeout_seconds))
    if timeout <= 0:
        raise ValueError(f"score.timeout_seconds={timeout} 必须 > 0")
    return ScoreConf(
        enabled=bool(raw.get("enabled", base.enabled)),
        prompt_file=str(raw.get("prompt_file") or base.prompt_file),
        batch_size=batch,
        provider=str(raw.get("provider") or base.provider),
        max_calls=max(0, int(raw.get("max_calls", base.max_calls))),
        timeout_seconds=timeout,
        providers=providers,
    )
