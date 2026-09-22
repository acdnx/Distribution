# -*- coding: utf-8 -*-
"""QuoteCollect 作业公共基础件：异常类型、日志与环境变量读取。

被 `SupabaseJobRepo` 与 `QuoteCollectRunner` 共同依赖，故独立成模块以避免循环导入。
本模块仅依赖标准库。

对外用途（其它两个模块会用到的部分）：
    JobExecutionError   作业执行错误，`permanent=True` 表示重试无意义的永久性失败
    _log / _warn        带时间戳的运行日志（_warn 输出到 stderr）
    _envText / _envFlag / _envInt   环境变量读取（缺失或非法时安全回退）
    _shortError         把异常压成单行摘要（入日志与数据库 last_error）
"""

from datetime import datetime
import os
import sys
from typing import Optional

#: 真值集合（与 SupabaseImportWeekMvsv.py 的开关口径保持一致）
TRUE_VALUES = ("1", "true", "yes", "on")


class JobExecutionError(Exception):
    """作业执行错误：承载「作业身份 + 是否永久失败」，供状态流转决定写 FAILED 还是 ABORTED

    Attributes:
        permanent: True = 永久性失败（配置错、数据/口径不支持），重试无意义 → FAILED；
            False = 临时性失败（网络、限流、被中断）→ ABORTED，允许后续重试。
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _log(message: str) -> None:
    """打印一行带时间戳的运行日志（stdout，flush 以便 Actions 实时看到进度）"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def _warn(message: str) -> None:
    """打印告警到 stderr（stderr 与 _log 分开，便于日志过滤）"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print("[%s] [WARN] %s" % (stamp, message), file=sys.stderr, flush=True)


def _envText(name: str, default: str = "") -> str:
    """读环境变量并 strip；未设置或为空时返回默认值"""
    return (os.environ.get(name) or "").strip() or default


def _envFlag(name: str, default: bool = False) -> bool:
    """读布尔型环境变量（TRUE_VALUES 命中为真；未设置时取 default）"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in TRUE_VALUES


def _envInt(name: str, default: Optional[int] = None) -> Optional[int]:
    """读整数型环境变量；未设置或非法时返回 default（不抛异常，仅因环境脏值就崩在入口）"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _warn("%s=%r 不是整数，已忽略并取默认值 %r" % (name, raw, default))
        return default


def _shortError(error: Optional[BaseException]) -> str:
    """把异常压成一行可入库的错误摘要（截断避免超长）"""
    if error is None:
        return "未知错误"
    text = "%s: %s" % (type(error).__name__, error) if str(error) else type(error).__name__
    text = " ".join(text.split())
    return text[:2000]

