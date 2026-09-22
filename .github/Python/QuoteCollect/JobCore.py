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
from typing import Any, Optional

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


# ---------------------------------------------------------------------------
# 字段归一化（作业表取值 -> 大驼峰口径）
# ---------------------------------------------------------------------------
# 由 SupabaseJobRepo 移入：归档落点拼路径（ArchivePublisher）与任务构建（SupabaseJobRepo）
# 都需要它，放在任一侧都会让另一侧产生不必要的依赖。
#: type_kline 标准取值域（大驼峰）；域外写法告警后按首字母大写兜底
STANDARD_TYPE_KLINE = ("Min", "Min5", "Min10", "Hour", "Hour2", "Hour3", "Hour6",
                       "Day", "Week", "Month", "Year")


def normalize_type_kline(raw: Any) -> str:
    """把作业表 `type_kline` 归一为**大驼峰**（本脚本全链路唯一出口）

    `MIN` / `min` / `Min` → `Min`；`MIN5` → `Min5`；`HOUR` → `Hour`。
    该值决定 `.mvsv` 文件名第 4 段、MVSV 头部 `# TypeKLine`、状态回写与日志展示，
    三处必须一致，故统一从本函数取。

    Args:
        raw: 作业表原值（大小写不限）。

    Returns:
        大驼峰文本；空值返回空串。域外写法按首字母大写兜底并告警。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    label = text.capitalize()
    if label not in STANDARD_TYPE_KLINE:
        _warn("type_kline=%r 不在标准取值域 %s 内，按大驼峰兜底为 %r；请核对作业表取值"
              % (raw, "/".join(STANDARD_TYPE_KLINE), label))
    return label


def normalize_period(raw: Any) -> str:
    """把作业表 `period` 归一为大驼峰（`MON` / `Mon` → `Mon`）

    与 `_periodLabel` 同口径（两者都是 `capitalize()`）；本脚本单独包一层是为了
    在空值时给出**点名到字段**的错误，而不是等到文件名拼接时才暴露。

    Args:
        raw: 作业表原值。

    Returns:
        大驼峰文本。

    Raises:
        JobExecutionError: 取值为空时抛出（永久性失败：没有 period 无法确定落点路径）。
    """
    text = str(raw or "").strip()
    if not text:
        raise JobExecutionError("作业表 period 为空，无法确定落点目录与文件名", permanent=True)
    return text.capitalize()
