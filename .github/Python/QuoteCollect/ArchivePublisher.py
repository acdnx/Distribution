# -*- coding: utf-8 -*-
"""归档落点发布：把本地 MVSV 文件经 GitHub Contents API 落到仓库的归档路径。

从 `QuoteCollectRunner.py` 提取，是本目录内**唯一**与 GitHub Contents API 耦合的模块。
任何「把本地文件推送到仓库归档路径」的作业都可复用它，无需了解采集细节。

对外用途：
    build_remote_path(task)                     拼出仓库内落点路径
    compute_digest(local_file)                  核算 sha256 / 字节数 / 行数（上传前后对账）
    push_to_repo(...)                           经 Contents API 提交（含 50MB 前置检查）
    make_commit_message(remote_path, task)      生成提交说明

仓库身份（owner / repo / branch）由调用方显式传入，故本模块不读 Commit.json 或 .git。
"""

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from GitHubCommitContent import commit_content_file

from JobCore import _envText, _log, _warn, normalize_period
from MvsvWriter import buildMvsvName

def compute_digest(localFile: Path) -> Tuple[str, int, int]:
    """核算本地文件指纹：sha256 + 字节数 + 行数（上传前后对账用）

    Args:
        localFile: 本地文件路径。

    Returns:
        (sha256 十六进制, 字节数, 行数)。
    """
    raw = localFile.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    # 行数：以换行符计数（MVSV 每行以 \n 结尾，故行数 = \n 个数）
    lines = raw.count(b"\n")
    return digest, len(raw), lines


# ---------------------------------------------------------------------------
# 四、落点路径与远端写入
# ---------------------------------------------------------------------------
def build_remote_path(task: Dict[str, Any]) -> str:
    """拼出仓库内落点路径（第五节的模式）

    Args:
        task: 已适配的 task dict。

    Returns:
        `Archive/Finv/SecuQuoteData/FTMM/{period}/{文件名}`（POSIX 风格，不含前导 /）。
    """
    period = normalize_period(task["period"])
    fileName = buildMvsvName(task, period)
    return "%s/%s" % (ARCHIVE_ROOT_TEMPLATE % period, fileName)


def push_to_repo(remotePath: str, localFile: Path, owner: str, repo: str, branch: str,
                 commitMsg: str) -> Dict[str, Any]:
    """经 GitHub Contents API 提交文件（第六节）

    Args:
        remotePath: 仓库内落点路径。
        localFile: 本地 MVSV 文件。
        owner / repo / branch: 目标仓库身份与分支（显式传入，不走 Commit.json 解析链）。
        commitMsg: 提交说明。

    Returns:
        结果 dict：`success` / `message` / `path` / `http_status`，另带 `digest` 元信息。

    Raises:
        JobExecutionError: 文件超过 Contents API 阈值（永久性失败）时抛出。
    """
    digest, size, lines = compute_digest(localFile)
    if size > GITHUB_CONTENTS_MAX_BYTES:
        raise JobExecutionError(
            "文件 %s 大小 %s 字节，超过 Contents API 单文件上限 %s 字节，无法经该接口提交"
            % (localFile.name, format(size, ","), format(GITHUB_CONTENTS_MAX_BYTES, ",")),
            permanent=True)

    _log("推送落点：%s/%s@%s ← %s（sha256 %s…｜%s 字节｜%d 行）"
         % (owner, repo, branch, remotePath, digest[:16], format(size, ","), lines))
    result = commit_content_file(remotePath, str(localFile), branch=branch,
                                 commit_msg=commitMsg, owner=owner, repo=repo)
    result["digest"] = digest
    result["size"] = size
    result["lines"] = lines
    if result.get("success"):
        _log("推送成功：HTTP %s｜%s" % (result.get("http_status"), remotePath))
    else:
        _warn("推送失败：HTTP %s｜%s" % (result.get("http_status"), result.get("message")))
    return result


def make_commit_message(remotePath: str, task: Dict[str, Any]) -> str:
    """生成提交说明（含作业身份与运行标识，便于在仓库历史里追溯）"""
    runId = _envText(ENV_RUN_ID, _envText("GITHUB_RUN_ID", "-"))
    return ("[FTMM][collect] %s｜job=%s｜%s ~ %s｜run=%s"
            % (task["_job"].get("job_name") or remotePath, task["_job"].get("id") or "-",
               task.get("start"), task.get("end"), runId))


#: Contents API 单文件硬阈值（字节）：超过则无法经该接口提交，前置拦截并明确报错
GITHUB_CONTENTS_MAX_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# 配置：落点路径与推送
# ---------------------------------------------------------------------------
#: 归档根（相对仓库根，不含文件名），形如 Archive/Finv/SecuQuoteData/FTMM/{period}/
ARCHIVE_ROOT_TEMPLATE = "Archive/Finv/SecuQuoteData/FTMM/%s"


#: 运行标识（仅日志与提交信息用）
ENV_RUN_ID = "JOB_RUN_ID"

