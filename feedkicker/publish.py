from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import time

import httpx

log = logging.getLogger(__name__)

_GH_CANDIDATES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")


def gh_bin() -> str:
    found = shutil.which("gh")
    if found:
        return found
    for p in _GH_CANDIDATES:
        if shutil.which(p):
            return p
    raise FileNotFoundError("找不到 gh CLI，请安装 GitHub CLI 或将其加入 PATH")


def _run_gh(args: list[str], stdin_text: str | None = None, timeout: float = 60):
    cmd = [gh_bin(), "api"] + args
    try:
        return subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("gh api 执行异常: %s", e)
        return None


def get_file_sha(repo: str, branch: str, remote_path: str) -> str | None:
    proc = _run_gh(
        [f"repos/{repo}/contents/{remote_path}", "--jq", ".sha", "-f", f"ref={branch}"],
        timeout=30,
    )
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip().strip('"') or None


def publish_file(
    repo: str, branch: str, remote_path: str, content: str, message: str
) -> bool:
    body: dict = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    sha = get_file_sha(repo, branch, remote_path)
    existed = sha is not None
    if sha:
        body["sha"] = sha

    proc = _run_gh(
        [
            "--method",
            "PUT",
            f"repos/{repo}/contents/{remote_path}",
            "--input",
            "-",
        ],
        stdin_text=json.dumps(body),
        timeout=60,
    )
    if proc is None or proc.returncode != 0:
        err = proc.stderr.strip()[:300] if proc is not None else "unknown"
        log.warning("发布 %s 失败: %s", remote_path, err)
        return False
    log.info("已发布 %s（%s）", remote_path, "更新" if existed else "新建")
    return True


def wait_published(url: str, timeout_s: int = 60, interval_s: int = 3) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True)
            if resp.status_code == 200 and "归档尚未生成" not in resp.text[:300]:
                return True
        except Exception as e:
            log.debug("轮询 %s 异常: %s", url, e)
        time.sleep(interval_s)
    log.warning("等待页面生效超时（%ds）：%s", timeout_s, url)
    return False
