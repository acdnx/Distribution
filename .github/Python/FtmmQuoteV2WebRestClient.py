#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FtmmQuoteV2WebRestClient —— FTMM 行情 V2 Web REST 接口客户端（纯标准库，无第三方依赖）
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
把「从远端行情 API 端点拉取行情数据」的 HTTP 细节（端点解析、URL 拼装、超时、
异常处理、响应校验、日志口径）封装到一个文件统一维护，供同目录各采集脚本
（当前为 FinvQuoteCollectPollFtmm.py，后续可扩展其他采集器）import 复用，
避免每个脚本各自复制一份请求逻辑、改端点或改校验口径时多处漂移。

形态说明：
    - 本文件是纯函数库，没有命令行入口；通过同目录其他 Python 脚本 import 调用；
    - import 时不发起任何网络请求、无全局副作用（环境变量在调用时才读）；
    - 公开函数：resolveApiBase（端点解析）、fetchFiveDayMinuteQuote（分钟线行情拉取）、
      extractMinuteList（从响应取数据行）；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，模块私有实现保持 snake_case；
    - HTTP 收发复用同目录 HttpUtil 的 sendRequest / parseJson（通用传输层），
      本文件只负责行情业务语义（端点解析、URL 拼装、响应校验、日志口径）；
    - 纯标准库（urllib 经 HttpUtil 间接使用），可直接拷到任何 Python 3.8+ 环境使用。

二、端点解析规则
----------------------------------------------------------------------------------------
| 输入      | 优先级                                                            |
|-----------|-------------------------------------------------------------------|
| base_url  | 1) 调用方显式传入的参数（局部覆盖，便于测试或同脚本多端点）        |
|           | 2) 环境变量 API_BASE（经 GitHub Actions secrets/vars 注入）         |
|           | 3) 内置默认端点 DEFAULT_API_BASE（旧版 Poll.py 沿用的 Futu 分钟线）|

三、返回值契约（与旧版 Poll.py 一致，调用方无需改判读逻辑）
----------------------------------------------------------------------------------------
    fetchFiveDayMinuteQuote(secu_code, ...) →
        成功：API 原始 JSON dict（已校验 code == 1 且 data 非空），
              调用方可直接取 data["list"] / data 的 c/h/l/o/cnt；
        失败：None（网络异常 / 非 JSON / API 业务码非 1 / data 为空）。
    失败一律返回 None，**不抛异常**（单品种失败不应打断轮询链路，由调用方记
    count_fail 做指数退避）；诊断信息走 print，调用方 stdout 只含自己的输出。

四、环境变量
----------------------------------------------------------------------------------------
    API_BASE   选填   行情 API 端点（未配置时使用内置默认端点；端点常以 secret 注入）

五、使用示例（同目录脚本）
----------------------------------------------------------------------------------------
    from FtmmQuoteV2WebRestClient import fetchFiveDayMinuteQuote, extractMinuteList

    raw = fetchFiveDayMinuteQuote("HSI")            # 端点按 参数 → API_BASE → 默认 解析
    if raw is None:
        ...                                    # 采集失败：记 fail 计数
    minute_list = extractMinuteList(raw)     # 分钟数据行列表（可能为空 list）

六、注意事项
----------------------------------------------------------------------------------------
    - 请求参数固定为 ?secuCode={code}（与旧版一致；代码做 URL 编码，支持非 ASCII 代码）；
    - 单次请求默认 30 秒超时；
    - 日志会打印完整请求 URL：端点若以 GitHub secret 注入，平台会自动遮蔽该值；
    - 端点只填端点本身，不要带查询串。

【环境要求】Python 3.8+，仅标准库；可直连行情 API 端点。
"""

import os
import urllib.parse

from HttpUtil import parseJson, sendRequest

# ============ 端点与请求常量 ============
# 端点环境变量名（约定专用）
ENV_API_BASE = "LAMBDA_FTMM_API_BASE"
# 内置默认端点（旧版 Poll.py 沿用的 Futu 分钟线端点）
DEFAULT_API_BASE = "===LAMBDA_FTMM_API_BASE==="
# 单次请求默认超时（秒）
DEFAULT_TIMEOUT = 30
# 请求头 User-Agent
USER_AGENT = "FtmmQuoteV2WebRestClient/1.0"
# 证券代码查询参数名（与旧版一致）
PARAM_SECU_CODE = "secuCode"
# API 业务成功码（与旧版一致）
SUCCESS_CODE = 1


def resolveApiBase(base_url=None):
    """解析行情 API 端点：参数 → 环境变量 API_BASE → 内置默认端点

    :param base_url: 调用方显式传入的端点；None 时走环境变量 / 默认值
    :return: 端点字符串（已 strip，末尾不带 /）
    """
    for candidate in ((base_url or "").strip(), os.environ.get(ENV_API_BASE, "").strip()):
        if candidate:
            return candidate.rstrip("/")
    return DEFAULT_API_BASE


def fetchFiveDayMinuteQuote(secu_code, base_url=None, timeout=DEFAULT_TIMEOUT):
    """拉取单个证券的分钟线行情（FiveDayMinute 窗口；GET ?secuCode={code}）

    :param secu_code: 证券代码（如 HSI / 000001 / NVDA）
    :param base_url: 显式端点；None 时按 resolveApiBase 的规则解析
    :param timeout: 单次请求超时秒数，默认 30
    :return: 成功 → API 原始 JSON dict（已校验 code == 1 且 data 非空）；
             失败 → None（不抛异常，诊断信息已 print）
    """
    url = "%s?%s=%s" % (resolveApiBase(base_url), PARAM_SECU_CODE,
                        urllib.parse.quote(str(secu_code), safe=""))
    print("[行情] 请求数据: %s" % url)
    status, text, err = sendRequest("GET", url, headers={"User-Agent": USER_AGENT},
                                    timeout=timeout)
    if err:
        print("[行情] %s 请求失败: %s" % (secu_code, err))
        return None
    if status is None or not (200 <= status < 300):
        print("[行情] %s HTTP 异常: %s" % (secu_code, status))
        return None
    data = parseJson(text)
    if data is None:
        print("[行情] %s 响应解析失败（非 JSON）" % secu_code)
        return None
    if data.get("code") == SUCCESS_CODE and data.get("data"):
        cnt = len(extractMinuteList(data))
        print("[行情] %s 数据获取成功，记录数: %d" % (secu_code, cnt))
        return data
    print("[行情] %s API 返回异常: %s" % (secu_code, data.get("message", "unknown")))
    return None


def extractMinuteList(quote_data):
    """从已校验的行情 JSON 中取出分钟数据行列表

    :param quote_data: fetchFiveDayMinuteQuote 的成功返回（API 原始 JSON）
    :return: 数据行列表（每行 dict，含 ts/c/v/t/cr/cp 等键）；缺失或结构异常返回 []
    """
    if not isinstance(quote_data, dict):
        return []
    data_node = quote_data.get("data") or {}
    minute_list = data_node.get("list") if isinstance(data_node, dict) else None
    return minute_list if isinstance(minute_list, list) else []


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("FtmmQuoteV2WebRestClient 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from FtmmQuoteV2WebRestClient import fetchFiveDayMinuteQuote, extractMinuteList, resolveApiBase")
