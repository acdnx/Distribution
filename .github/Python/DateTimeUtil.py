#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DateTimeUtil —— 日期时间 / 时间戳 / 字符串转换工具库（纯标准库）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把采集链路里反复出现的「取当前北京时间」「解析状态表时间列」「时间与字符串互转」
「秒级时间戳」「美国夏令时判定」等纯日期时间逻辑集中到一个文件维护，供同目录各
脚本（当前 FinvQuoteCollectPollFtmm.py，后续可扩展）import 复用，避免各处各写一份
导致时区口径漂移。

形态说明：
    - 纯函数库，没有命令行入口；import 无副作用、不触网；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**；
    - 时区口径统一：**内部一律使用 tz-aware datetime**，北京时间固定为 UTC+8；
    - 默认字符串格式沿用旧版 Poll.py（文件名后缀 %Y%m%d_%H%M%S、展示 %Y-%m-%d %H:%M:%S）。

二、时区口径（重要）
----------------------------------------------------------------------------------------
    - 行情采集落盘文件名、日志名、展示时间 → **北京时间（UTC+8）**；
    - Supabase PG 的 timestamptz 由 PostgREST 返回 **UTC 偏移的 ISO 字符串**
      （如 2026-09-24T14:00:00+00:00），parseDt 读回后统一为 tz-aware，
      与北京时间直接相减不会出错；
    - 写回时间列时传北京时间 ISO 字符串（带 +08:00 偏移），PG 存为 timestamptz 正确落库。

三、公开函数
----------------------------------------------------------------------------------------
    utcNow()           当前 UTC 时间（tz-aware）
    nowBeijing()            当前北京时间（tz-aware）
    toBeijing(dt)           任意 tz-aware 时间 → 北京时间
    parseDt(value, default=None)   宽松解析 ISO 字符串（naive 按北京时间补齐）
    fmtTsSuffix(dt=None)  → "20260924_223000"（文件名后缀）
    fmtDisplay(dt=None)    → "2026-09-24 22:30:00"（展示与日志）
    toEpochSeconds(dt=None) → int 秒级时间戳
    shiftDays(dt, days)    dt 平移指定天数（正数为未来）
    isUsDst(now=None)     是否处于美国夏令时（3月第二个周日 02:00 ~ 11月第一个周日 02:00）

四、使用示例
----------------------------------------------------------------------------------------
    from DateTimeUtil import nowBeijing, fmtTsSuffix, fmtDisplay, parseDt

    now = nowBeijing()                                  # 调度与命名的统一时间基准
    file_name = "%s_Min_%s.mvsv" % (code, fmtTsSuffix(now))
    last = parseDt(row.get("dt_last_check"))       # 解析失败返回 None
    if last is None:
        ...
    print("采集时间:", fmtDisplay(now))

【环境要求】Python 3.8+，仅标准库。
"""

from datetime import datetime, timedelta, timezone

# ============ 时区与格式常量 ============
# 北京时间（UTC+8）
BEIJING_TZ = timezone(timedelta(hours=8))
# UTC 时区
UTC_TZ = timezone.utc
# 文件名时间戳后缀格式（北京时间的 ts_suffix）
FORMAT_TS_SUFFIX = "%Y%m%d_%H%M%S"
# 展示 / 日志用的可读格式
FORMAT_DISPLAY = "%Y-%m-%d %H:%M:%S"


def utcNow():
    """当前 UTC 时间（tz-aware）"""
    return datetime.now(UTC_TZ)


def nowBeijing():
    """当前北京时间（tz-aware，UTC+8）"""
    return datetime.now(BEIJING_TZ)


def toBeijing(dt):
    """把任意 tz-aware 时间转换为北京时间

    :param dt: tz-aware datetime
    :return: 北京时间的 datetime；naive 输入按北京时间原样返回（不臆断其原始时区）
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BEIJING_TZ)


def parseDt(value, default=None):
    """宽松解析时间字符串为 tz-aware datetime

    适配来源：Supabase PG 的 timestamptz（PostgREST 返回带 UTC 偏移的 ISO，
    可能以 Z 结尾）、旧版 DynamoDB 落下的北京时间 ISO、以及无偏移的本地时间字符串。

    :param value: 待解析的字符串/None
    :param default: 解析失败或输入为空时的返回值（默认 None）
    :return: tz-aware datetime（naive 输入按北京时间补齐）；失败返回 default
    """
    if not value:
        return default
    if isinstance(value, datetime):
        return value.replace(tzinfo=BEIJING_TZ) if value.tzinfo is None else value
    text = str(value).strip()
    if not text:
        return default
    # Python 3.10 及更早的 fromisoformat 不认 'Z' 结尾，统一换成 +00:00 偏移
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed


def fmtTsSuffix(dt=None):
    """格式化为文件名时间戳后缀：20260924_223000（默认取当前北京时间）"""
    return (toBeijing(dt) if dt is not None else nowBeijing()).strftime(FORMAT_TS_SUFFIX)


def fmtDisplay(dt=None):
    """格式化为可读时间：2026-09-24 22:30:00（默认取当前北京时间）"""
    return (toBeijing(dt) if dt is not None else nowBeijing()).strftime(FORMAT_DISPLAY)


def toEpochSeconds(dt=None):
    """转为 int 秒级 Unix 时间戳（默认取当前 UTC 时刻）"""
    target = dt if dt is not None else utcNow()
    if target.tzinfo is None:
        target = target.replace(tzinfo=BEIJING_TZ)
    return int(target.timestamp())


def shiftDays(dt, days):
    """把时间平移指定天数（days 为负表示往前）

    :param dt: datetime（tz-aware 或 naive 均可，原样保留时区）
    :param days: 平移天数（正 = 未来，负 = 过去）
    :return: 平移后的 datetime
    """
    return dt + timedelta(days=days)


def isUsDst(now=None):
    """判断是否处于美国夏令时（3月第二个周日 02:00 ~ 11月第一个周日 02:00）

    入参需为本地（美东）时间基准的 datetime；本函数只做日历规则计算，不感知市场。

    :param now: 目标时刻；None 表示当前 UTC 时刻
    :return: bool
    """
    now = now if now is not None else utcNow()
    year = now.year
    mar1 = datetime(year, 3, 1, tzinfo=now.tzinfo)
    first_sun_mar = mar1 + timedelta(days=(6 - mar1.weekday()) % 7)
    second_sun_mar = first_sun_mar + timedelta(days=7)
    dst_start = second_sun_mar.replace(hour=2)
    nov1 = datetime(year, 11, 1, tzinfo=now.tzinfo)
    first_sun_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    dst_end = first_sun_nov.replace(hour=2)
    return dst_start <= now < dst_end


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("DateTimeUtil 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from DateTimeUtil import nowBeijing, fmtTsSuffix, fmtDisplay, parseDt")
