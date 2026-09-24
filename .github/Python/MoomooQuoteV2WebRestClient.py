#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoomooQuoteV2WebRestClient —— moomoo 官方 quote-v2-web 分钟行情直连客户端（模式2）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
本文件是「行情获取模式2」的实现：**不再经过自建 HTTP 端点，而是在 Python 进程内直连
moomoo（富途）官方 quote-v2-web 接口**拉取证券分钟行情（5 日窗口）。

它是 Go 云函数 futuquoteapi 的等价移植版，原始实现位于：
    UniCloudFunctionGo/internal/meta/cf/handler/futuquoteapi/{handler.go, stock.go, remote_config.go}
本文件（尤其是响应 JSON 结构）与 Go 版保持**严格一致**。这不是风格偏好，而是硬约束：
采集作业的下游（MvsvQuoteBuilder）直接消费响应里的 data.list，任何字段形态差异都会
落进 .mvsv 数据文件。故本文件对「数值类型」「空值补零」「omitempty 键省略」「字段顺序」
都逐条对齐，并在注释里写明每条对齐的依据。

形态说明：
    - 纯函数库，没有命令行入口；import 无副作用、不触网（环境变量在调用时才读）；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，模块私有实现（下划线前缀）保持 snake_case；
    - HTTP 收发复用同目录 HttpUtil（通用传输层），本文件只负责行情业务语义；
    - 仅标准库：hashlib / hmac / json / os / time / urllib.parse / uuid；
    - **与 FtmmQuoteV2WebRestClient（模式1）零代码依赖**：互不 import、不共享私有实现，
      各自独立可用、可测、可单独重构。模式的归口与选择见 FtmmQuoteV2WebRestClient。

二、与模式1 的关系（两者必须共存、互不依赖）
----------------------------------------------------------------------------------------
    模式1（FtmmQuoteV2WebRestClient）  请求环境变量指定的自建端点（LAMBDA_FTMM_API_BASE），
                                       跨域与上游对接由云函数侧解决；
    模式2（本文件）                    Python 内直接对接 moomoo 官方 quote-v2-web。
两者**对外契约相同**：成功 → 形如 {"code":1,"message":"OK","data":{...}} 的 dict
（data.list 为分钟数据行）；失败 → None（不抛异常）。因此可被同一份采集作业无缝切换。

三、响应契约（与 Go 版逐字段对齐）
----------------------------------------------------------------------------------------
成功（HTTP 2xx 且上游 code==0）：
    {
      "code": 1,                    # Go 的 ErrCodeSuccess
      "data": {                     # Go 的 QuoteData；字段顺序 = Go 结构体定义顺序
        "sid":        <int>,        # ← data.stockId
        "list":       [ {...} ],    # 见下（上游缺 list 时为 null，与 Go 的 nil 切片一致）
        "section":    [ {"begin": <int>, "end": <int>} ],   # ← data.time_section
                                    #   空/缺失则**整个键不出现**（Go 的 `,omitempty`）
        "ts_server":  <int>,        # ← data.server_time
        "c":          <num>,        # ← data.last_close_price（昨收价）
        "h":          <num>,        # ← data.highest_price
        "l":          <num>,        # ← data.lowest_price
        "cnt":        <int>,        # = len(list)
        "request_id": <str>,        # Go 取 Lambda 请求 ID；Python 侧无该上下文，用等价 uuid 占位
        "ts":         <int>         # 服务端处理时间戳（本进程 now）
      },
      "message": "OK"
    }
    ⚠️ 顶层键顺序 code → data → message：Go 用 map[string]interface{} 序列化，键按字典序
    输出，故此处也按该顺序构造（本文件返回 dict，键序仅影响序列化后的文本，不影响语义）。

    list 每行（Go 的 MinuteItem，**7 个键恒定存在、无 omitempty**，故缺失字段按零值补）：
        {"ts": <int>, "c": <num>, "v": <num>, "t": <num>,
         "r": <str>, "cr": <str>, "cp": <num>}
        ts ← time；c ← cc_price；v ← volume；t ← turnover；cp ← change_price；
        r 与 cr 都取 ratio（Go 里两字段同值，cr 是后续用来替换 r 的备用位）。
        注意：上游的 price 字段 Go 侧**未使用**，本移植同样不映射。

失败：返回 None，**不抛异常**（与模式1 一致）；诊断信息走 print，错误码沿用 Go 的定义，
便于与 Go 版日志/文档对照（Go 把这些码放在响应体里，本移植放进日志）：
    1001 moomoo 调用失败（网络异常 / HTTP 状态码异常 / 响应非 JSON / 字段类型不符）
    1002 远程配置获取失败（非 404）；1003 上游 code=500（参数错误）；1004 上游其它非 0 码
    1005 内部错误（缺少必要参数）；1006 远程配置 404（证券暂不支持）；1007 远程配置超时

四、参数三级获取策略（与 Go 一致）
----------------------------------------------------------------------------------------
    1) 本地枚举 SECURITY_CONFIGS（对应 Go 的 stock.go）—— 命中即用，最快；
    2) 远程配置 https://cfgdistnet.pages.dev/Quote/Futu/{secuCode}.json 的 queryParams 节点，
       字段类型为 JSON 数字，需转成字符串（同 Go 的 convertRemoteQueryParams）；
       404 → 1006；超时 → 1007；其它异常 → 1002；
    3) Go 还支持「直接传 stockId 等原始参数」的兼容路径——那是 HTTP query 层语义，
       而本库的入参是 secuCode，故不实现（对本采集作业无影响）。

五、环境变量（三个全部**可选**）
----------------------------------------------------------------------------------------
    QUOTE_TOKEN      签名 token；设置后**直接使用**、跳过本地计算（对应 Go 的 QUOTE_TOKEN）
    FUTU_CSRF_TOKEN  CSRF Token；仅在设置时随请求发送（对应 Go 的 FUTU_CSRF_TOKEN）
    MOOMOO_COOKIES   Cookie 串；仅在设置时随请求发送（对应 Go 的 MOOMOO_COOKIES）

    ⚠️ 与 Go 版的一处**有意差异**（不是遗漏）：Go 在环境变量缺失时会回落到代码内硬编码的
    cookie / csrfToken 默认值；本移植**不带任何硬编码默认值**。理由有二：
      · 本仓库是公开仓库，把 cookie 写进代码等于公开凭据（Go 所在仓库为私有仓库）；
      · 2026-09-25 实测：**cookie 与 futu-x-csrf-token 都不是必需头**——仅带 quote-token
        即可稳定拿到 200（「全头 / 去 cookie / 去 cookie+csrf / 仅 quote-token」四组对照
        全部成功），只有 quote-token 错误时才失败（返回 code=500 Params Error）。
    因此二者改为「按需注入」：未配置就不发送，实测行为与 Go 一致。

六、注意事项与实测踩坑（2026-09-25 实测）
----------------------------------------------------------------------------------------
    - **限流页伪装成 200（2026-09-25 实测定量，本模式最大的线上风险）**：请求过频时 moomoo
      返回 HTTP **200** + `content-type: text/html`，页面标题为 `403 - Operations too frequent`；
      响应头显示是 **moomoo 自家网关**（`Server: fgw_web_conn/*`，不是 Cloudflare）。
      实测特征（同一来源 IP）：
        · 连发约 **7 次**后开始被拦（前 7 次全部成功，第 8 次起全部被拦）；
        · **拦截按来源 IP，持续约 12 分钟**——实测 05:43:35 起被拦，
          05:49:58~05:55:04 连续 6 次探测（间隔 60s）仍被拦，**05:56:05 恢复并正常返回 1205 行**；
        · 静默 90 秒 + 按 25 秒间隔请求，仍全部被拦（说明不是「秒级窗口」，会持续较久）。
      只判 HTTP 状态码会把这种响应误判成成功；本文件显式识别「响应非 JSON」并单独提示该场景
      （Go 版会落到 1001 的 JSON 解析失败）。
      ⚠️ 生产口径：本作业每 5 分钟采 3 个品种（POLL_COUNT=3），约 0.6 次/分钟；
      实测触发条件是**短时连发**（7 次 / 约 11 秒 ≈ 38 次/分钟），两者相差约 60 倍，故当前
      节奏应安全。但**「持续速率的安全配额」未测出**，故调大 POLL_COUNT / 缩短周期前，
      必须先用 dry_run 观察一段时间；若被拦，一轮 3 个品种会全部失败（由作业记 count_fail）。
      ⚠️ 尚未解释、需线上观察的差异：Go 版默认携带硬编码 cookie + csrfToken，本移植不带
      （公开仓库不能存凭据；且 2026-09-25 的头组合对照实测表明二者非必需）。
      若线上出现「Go 端点能拿、本模式被拦」，很可能是 moomoo 对**无会话请求限得更严**——
      此时用 FUTU_CSRF_TOKEN / MOOMOO_COOKIES 两个环境变量注入即可，**无需改代码**。
    - **远程配置端点的 Cloudflare 403**：不携带 User-Agent 时，`cfgdistnet.pages.dev` 会对
      `Python-urllib/3.x` 直接 403。故本文件对两次外呼都显式带上浏览器 UA
      （Go 的 http.Client 用默认 UA 未被拦，Python 的默认 UA 会被拦——这是移植必须补的差异）。
    - **数值类型即内容**：下游 MvsvQuoteBuilder 用 str(value) 原样落盘，所以
      `2665800` 与 `2665800.0` 会产生**不同的 .mvsv 内容**。Go 把上游 JSON 数字读成 float64
      再序列化，整数值仍输出成 `2665800`；故本文件对「整数值的浮点数」统一归一为 int
      （见 _go_style_number），确保与 Go（以及模式1 经 Go 端点拿到的数据）完全一致。
    - **参数拼接口径**：Go 对 stockId/marketType/type/marketCode/instrumentType/
      subInstrumentType **无条件**写入查询串（即使为空），只有 req_section 是条件写入
      （omitempty）。本文件保持同一口径，不额外做「空值即省略」的优化。
    - **单次请求超时 15 秒**、**远程配置 5 秒**（均与 Go 一致）。
    - 日志**不打印** quote-token / cookie / csrf（属凭据）；请求 URL 本身是公开上游端点，
      照常打印以便排查。

【环境要求】Python 3.8+，仅标准库；运行环境需可直连 www.moomoo.com。
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid

from HttpUtil import parseJson, sendRequest

# ============ 上游端点与常量 ============
# 上游标识（日志用）
UPSTREAM_LABEL = "moomoo quote-v2-web（直连）"
# moomoo 官方分钟行情接口（公开上游端点，非本项目的机密端点）
MOOMOO_API_BASE = "https://www.moomoo.com/quote-api/quote-v2/get-quote-minute"
# 远程证券配置的基础 URL 列表（对应 Go 的 RemoteConfigBaseURLs；公开三方配置服务，
# 非本项目机密端点——它是设计上给客户端公开读取的静态配置，不含凭据）。
# 当前取第一个节点，Go 声明了列表以便将来多节点高可用，本移植保持同样的可扩展形态。
REMOTE_CONFIG_BASE_URLS = ("https://cfgdistnet.pages.dev/Quote/Futu",)
# 单次上游请求默认超时（秒，与 Go 的 15s 一致）
DEFAULT_TIMEOUT = 15
# 远程配置请求超时（秒，与 Go 的 RemoteConfigTimeout 一致）
REMOTE_CONFIG_TIMEOUT = 5
# 签名密钥（与 Go 的 "quote_web" 一致）
SIGN_KEY = "quote_web"
# 签名中间值截取长度（Go: hmac 结果取前 10 → sha256 结果取前 10）
TOKEN_SLICE = 10

# 请求头（与 Go 的 setRequestHeaders 一致）
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
ACCEPT_HEADER = "application/json, text/plain, */*"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
REFERER = "https://www.moomoo.com/"

# 环境变量名（三者均可选；见模块文档「五」）
ENV_QUOTE_TOKEN = "QUOTE_TOKEN"
ENV_CSRF_TOKEN = "FUTU_CSRF_TOKEN"
ENV_COOKIES = "MOOMOO_COOKIES"

# 业务成功码 / 文案（与 Go 一致）
SUCCESS_CODE = 1
SUCCESS_MESSAGE = "OK"
# 上游 moomoo 自身的成功码（0 = 成功）
MOOMOO_SUCCESS_CODE = 0
# 上游「参数错误」码（Go 用它区分 1003 与 1004）
MOOMOO_PARAM_ERROR_CODE = 500

# 业务错误码（沿用 Go 的定义，便于对照；本移植只把它们写进日志）
ERR_CODE_MOOMOO_API = 1001
ERR_CODE_REMOTE_CONFIG = 1002
ERR_CODE_MOOMOO_API_PARAM_ERROR = 1003
ERR_CODE_MOOMOO_API_UNKNOWN = 1004
ERR_CODE_INTERNAL = 1005
ERR_CODE_REMOTE_CONFIG_NOT_FOUND = 1006
ERR_CODE_REMOTE_CONFIG_TIMEOUT = 1007

# 请求参数键名。**顺序即签名序列化顺序**（对应 Go 的 QuoteMinuteSignParam 字段定义顺序），
# 而签名必须与 URL 里的参数一致，故该顺序不可随意调整（import 期有 _assert_config_order 守卫生效）。
PARAM_STOCK_ID = "stockId"
PARAM_MARKET_TYPE = "marketType"
PARAM_TYPE = "type"
PARAM_MARKET_CODE = "marketCode"
PARAM_INSTRUMENT_TYPE = "instrumentType"
PARAM_SUB_INSTRUMENT_TYPE = "subInstrumentType"
PARAM_REQ_SECTION = "req_section"
PARAM_TIMESTAMP = "_"
# 签名顺序清单：前 6 个恒存在，req_section 可选，_ 恒在末位
PARAM_ORDER = (PARAM_STOCK_ID, PARAM_MARKET_TYPE, PARAM_TYPE, PARAM_MARKET_CODE,
               PARAM_INSTRUMENT_TYPE, PARAM_SUB_INSTRUMENT_TYPE, PARAM_REQ_SECTION)
# 无条件写入 URL 的参数（Go 里这 6 个不做 omitempty；只有 req_section 是条件写入）
PARAM_ALWAYS = (PARAM_STOCK_ID, PARAM_MARKET_TYPE, PARAM_TYPE, PARAM_MARKET_CODE,
                PARAM_INSTRUMENT_TYPE, PARAM_SUB_INSTRUMENT_TYPE)


def _security_config(stock_id, market_code, instrument_type, sub_instrument_type,
                    market_type="2", line_type="2", req_section=""):
    """构造一条证券参数（键序严格按签名顺序；line_type 恒为 "2"=分钟线，与 Go 一致）

    私有辅助，仅用于本文件的枚举表初始化，避免 20 条记录手工重复。
    """
    config = {
        PARAM_STOCK_ID: stock_id,
        PARAM_MARKET_TYPE: market_type,
        PARAM_TYPE: line_type,
        PARAM_MARKET_CODE: market_code,
        PARAM_INSTRUMENT_TYPE: instrument_type,
        PARAM_SUB_INSTRUMENT_TYPE: sub_instrument_type,
    }
    if req_section:
        # Go 的 `req_section,omitempty`：为空则**不参与签名也不出现在 URL**
        config[PARAM_REQ_SECTION] = req_section
    return config


# ============ 证券参数枚举（与 Go 的 stock.go 逐条一致） ============
# 说明：
#   · 键为 secuCode（业务语义代码），值为 moomoo 原始请求参数；
#   · 未登记的代码会走远程配置（见 _load_remote_config），因此新增证券**无需改代码**；
#   · Go 里另有一个 DefaultSecurityConfig（默认证券配置），但经核查**声明后从未被使用**
#     （未命中枚举时走的是远程配置而非它），故本移植不保留该死代码。
SECURITY_CONFIGS = {
    # 纽约黄金期货（映射到富途牛牛港股分钟线）
    "GCMain": _security_config("70000294", "70", "9", "9008"),
    # 现货黄金 XAU/USD（映射到港交所黄金期货分钟线）
    "XAUUSD": _security_config("72000031", "122", "10", "10001", market_type="11"),
    # 美元兑人民币汇率 USD/CNY（映射到港交所汇率期货分钟线）
    "USDCNY": _security_config("72000000", "120", "10", "10001", market_type="11"),
    # 英伟达 NVIDIA（美股）
    "NVDA": _security_config("202597", "11", "3", "3002", req_section="1"),
    # SPDR 黄金信托基金 GLD（美股 ETF）
    "GLD": _security_config("205078", "12", "4", "4002"),
    # VanEck 黄金矿业 ETF GDX（美股 ETF）
    "GDX": _security_config("201965", "10", "4", "4002"),
    # iShares 黄金信托 ETF IAU（美股 ETF）
    "IAU": _security_config("205150", "10", "4", "4002"),
    # 恒生科技指数 HTI（港股指数）
    "HTI": _security_config("800700", "1", "6", "6001", market_type="1"),
    # 恒生指数 HSI（港股指数）
    "HSI": _security_config("800000", "1", "6", "6001", market_type="1"),
    # 招商银行（A 股）
    "600036": _security_config("50616191183396", "30", "3", "3002", market_type="4"),
    # 东方财富（A 股）
    "300059": _security_config("63075892009115", "35", "3", "3002", market_type="4"),
    # 小米集团（港股）
    "01810": _security_config("76033806042898", "1", "3", "3002", market_type="1"),
    # 华安黄金 ETF 518880（A 股 ETF）
    "518880": _security_config("1518880", "30", "4", "4002", market_type="4"),
    # 黄金 ETF 易方达 159934（A 股 ETF）
    "159934": _security_config("2159934", "31", "4", "4002", market_type="4"),
    # 黄金 ETF 博时 159937（A 股 ETF）
    "159937": _security_config("2159937", "31", "4", "4002", market_type="4"),
    # 易方达黄金主题 LOF 161116（A 股 LOF）
    "161116": _security_config("2161116", "31", "4", "4001", market_type="4"),
    # 嘉实黄金 LOF 160719（A 股 LOF）
    "160719": _security_config("2160719", "31", "4", "4001", market_type="4"),
    # PAX Gold PAXG（加密货币）
    "PAXG": _security_config("12003870", "360", "11", "11002", market_type="17"),
    # PAX Gold/USD 交易对 PAXGUSD（加密货币交易对）
    "PAXGUSD": _security_config("12003913", "360", "11", "11003", market_type="17"),
    # SpaceX SPCX（美股）
    "SPCX": _security_config("88514981222649", "11", "3", "3002", req_section="1"),
}


class _PayloadError(Exception):
    """上游响应字段类型与 Go 结构体不匹配

    复刻 Go 的 json.Unmarshal 语义：Go 把 JSON 数字读进 float64 / int64 字段，
    遇到类型不符（如字符串塞进 float64）会**整份响应解析失败**。本移植同样选择
    fail-closed——因为下游用 str(value) 原样落盘，放过脏类型会静默污染 .mvsv 内容。
    """


def _assert_config_order():
    """import 期守卫：校验枚举表里每条参数的键序与 PARAM_ORDER 一致

    为什么值得守：签名是对「参数 JSON」做的，键序错了 token 就错，而错误表现只是上游
    返回 code=500 Params Error（1003），极难反查到「键序」这个根因。故在 import 期 fail fast。
    """
    for code, config in SECURITY_CONFIGS.items():
        expected = [key for key in PARAM_ORDER if key in config]
        actual = list(config.keys())
        if actual != expected:
            raise ValueError("SECURITY_CONFIGS[%s] 键序与 PARAM_ORDER 不一致: %s != %s"
                             % (code, actual, expected))


def isUpstreamReady():
    """模式2 的数据源是否就绪

    模式2 直连公开上游，不需要任何本地端点配置（cookie/csrf 亦非必需，见模块文档「五」），
    故恒为 True。存在的意义是让调用方（模式门面）能用**统一的方式**做启动自检。

    :return: True
    """
    return True


def _interface_to_string(value):
    """把远程配置里的 JSON 数字/字符串统一转成字符串（对应 Go 的 interfaceToString）

    Go 的 switch 只显式处理 nil / float64 / string，其余走 fmt("%v")：
        nil → ""；float64 → "%.0f"；string → 原值；其它 → "%v"。
    JSON 数字在 Go 里一律是 float64，故统一走 "%.0f"；Python 的 json 会把整数解析成 int，
    对 int 同样用 "%.0f" 可得到与 Go 完全一致的结果（如 31 → "31"）。

    ⚠️ 未对齐（实际不可达）：Go 的 "%v" 对数组/对象输出形如 "[1 2]" / "map[a:1]" 的
    Go 语法，本函数对复合类型退回 Python repr。远程配置的 queryParams 实测只含数字标量，
    故该差异在真实数据上不会出现。布尔值已特判成小写，对齐 Go 的 "%v"。

    :param value: 待转换的值（int / float / str / bool / None）
    :return: 字符串；None → ""
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"     # 对齐 Go 的 fmt("%v") 布尔输出
    if isinstance(value, (int, float)):
        return "%.0f" % value
    return str(value)


def _go_style_number(value):
    """把「整数值的浮点数」归一为 int，复刻 Go 的 float64 往返结果

    为什么必须做：Go 把上游 JSON 数字读成 float64，再用 encoding/json 序列化回文本时，
    整数值输出成 `2665800`（不带小数）。Python 若原样保留 float 2665800.0，
    下游 MvsvQuoteBuilder 的 str(value) 会产出 `2665800.0`，.mvsv 内容就与模式1 不一致了。
    故此处把整数值的 float 归一为 int——这与 Go 的文本产物等价。

    :param value: 上游数值（int / float）
    :return: 归一后的值（近整数范围的整数值 float → int；其余原样）
    """
    if isinstance(value, bool):
        return value
    # 2**53 之后 float64 无法精确表示每个整数，Go 会改用指数记法，故不强行归一
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    return value


def _require_number(value, field):
    """取数值字段（复刻 Go 的 float64/int64 反序列化：缺失按零值，类型不符即失败）

    ⚠️ 有意的宽松点：Go 反序列化 `int64` 字段时，上游若给出带小数点的数字会**直接报错**；
    本函数对 int 字段同样接受「整数值的浮点数」（经 _go_style_number 归一后结果一致），
    仅拒绝真正的非整数浮点。由于下游一律 str(value) 落盘，归一后文本与 Go 完全相同，
    不会造成内容差异；此举只是避免「上游偶发改用 1787189400.0 形式就整轮失败」。

    :param value: 上游原始值
    :param field: 字段名（仅用于报错）
    :return: 数值（已过 _go_style_number）
    :raises _PayloadError: 值存在但不是数字
    """
    if value is None:
        return 0                      # Go：字段缺失 → 零值，不报错
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _PayloadError("字段 %s 期望数字，实际为 %s" % (field, type(value).__name__))
    return _go_style_number(value)


def _require_text(value, field):
    """取文本字段（复刻 Go 的 string 字段反序列化：缺失按零值，类型不符即失败）

    :param value: 上游原始值
    :param field: 字段名（仅用于报错）
    :return: 字符串
    :raises _PayloadError: 值存在但不是字符串
    """
    if value is None:
        return ""                     # Go：字段缺失 → 零值 ""
    if not isinstance(value, str):
        raise _PayloadError("字段 %s 期望字符串，实际为 %s" % (field, type(value).__name__))
    return value


def serializeSignParams(sign_params):
    """把签名参数序列化为紧凑 JSON（对应 Go 的 serializeSignParams）

    要点：
      · 键序取 dict 自身的插入顺序——调用侧（_security_config / _load_remote_config）
        均按 PARAM_ORDER 构造，且 import 期有 _assert_config_order 守卫；
      · 分隔符去掉空格（Go 的 json.Marshal 不产生空格）；
      · Go 的 Marshal 会把 `<` `>` `&` 转义成 \\u00xx，本参数集只含数字与下划线键，
        不涉及该差异（若将来引入了含这些字符的值需另行对齐）。

    :param sign_params: 已按签名顺序排好的参数 dict
    :return: JSON 文本；不可序列化时返回 "{}"（与 Go 一致）
    """
    if not sign_params:
        return "{}"
    try:
        return json.dumps(sign_params, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def generateQuoteToken(sign_params):
    """生成 quote-token 签名（对应 Go 的 generateQuoteToken）

    算法（Go：HMAC-SHA512(input, "quote_web")[:10] → SHA-256[:10]）：
        输入   = 签名参数的紧凑 JSON
        步骤1  = hex(HMAC-SHA512(key="quote_web", msg=输入))[:10]
        步骤2  = hex(SHA-256(步骤1))[:10]  ← 最终 token

    环境变量 QUOTE_TOKEN 非空时**直接返回它**并跳过计算（与 Go 一致，便于抓包复现）。

    :param sign_params: 已按签名顺序排好的参数 dict
    :return: 10 字符的 quote-token
    """
    override = os.environ.get(ENV_QUOTE_TOKEN, "").strip()
    if override:
        return override
    payload = serializeSignParams(sign_params)
    step1 = hmac.new(SIGN_KEY.encode("utf-8"), payload.encode("utf-8"),
                     hashlib.sha512).hexdigest()[:TOKEN_SLICE]
    return hashlib.sha256(step1.encode("utf-8")).hexdigest()[:TOKEN_SLICE]


def buildRequestUrl(params):
    """按 Go 的字段顺序拼装上游请求 URL

    Go 用字符串直接拼接（不做 URL 编码）；本移植改用 urlencode，对当前「纯数字参数」
    产生的查询串与 Go 完全相同，但更稳健（值含特殊字符时不会拼坏）。

    参数可见性口径与 Go 一致：前 6 个参数**无条件**出现（即使值为空串），
    `req_section` 仅在非空时出现（omitempty），`_` 时间戳恒在末位。

    :param params: 含 `_` 时间戳的完整参数 dict（键序按 PARAM_ORDER）
    :return: 完整请求 URL
    """
    pairs = [(key, params.get(key, "")) for key in PARAM_ALWAYS]
    if params.get(PARAM_REQ_SECTION):
        pairs.append((PARAM_REQ_SECTION, params[PARAM_REQ_SECTION]))
    pairs.append((PARAM_TIMESTAMP, params.get(PARAM_TIMESTAMP, "")))
    return "%s?%s" % (MOOMOO_API_BASE, urllib.parse.urlencode(pairs))


def _is_timeout_error(error_text):
    """判断 HttpUtil 返回的错误文本是否属于「超时」（对应 Go 的 1007 判定）

    需要多模式匹配的原因：urllib 常把 socket 超时包成 URLError，
    此时 HttpUtil 给出的是 "网络错误: timed out"，而不是它自己的 "请求超时(>Ns)" 文案。
    只匹配 "timeout" 会漏掉最常见的 "timed out"，从而把 1007 误报成 1002。
    （Go 侧是按 net.Error.Timeout() 判定的类型判断，本移植只能按文案近似。）

    :param error_text: HttpUtil 返回的 error 字符串
    :return: True 表示超时
    """
    if not error_text:
        return False
    lowered = error_text.lower()
    return ("超时" in error_text or "timeout" in lowered
            or "timed out" in lowered or "timed-out" in lowered)


def _load_remote_config(secu_code):
    """从远程配置服务取证券参数（对应 Go 的 fetchRemoteConfig）

    :param secu_code: 证券代码
    :return: (params, error_text)；成功时 error_text 为 None，失败时 params 为 None
    """
    url = "%s/%s.json" % (REMOTE_CONFIG_BASE_URLS[0], urllib.parse.quote(secu_code, safe=""))
    print("[模式2] 请求远程配置: %s" % url)
    # ⚠️ 必须带 User-Agent：该域名前置了 Cloudflare，对 Python-urllib 的默认 UA 直接 403
    status, text, err = sendRequest("GET", url,
                                    headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER},
                                    timeout=REMOTE_CONFIG_TIMEOUT)
    if err:
        if _is_timeout_error(err):
            return None, ("[%d] 未能从三方配置获取到%s证券的参数，分钟行情获取失败: "
                          "配置服务请求超时" % (ERR_CODE_REMOTE_CONFIG_TIMEOUT, secu_code))
        return None, ("[%d] 未能从三方配置获取到%s证券的参数，分钟行情获取失败: "
                      "网络请求异常（%s）" % (ERR_CODE_REMOTE_CONFIG, secu_code, err))
    if status == 404:
        # 证券确实不存在（确定性错误）：调用方可据此判定「暂不支持」
        return None, ("[%d] 未能从预定义证券及三方配置获取到%s证券的参数，分钟行情获取失败: "
                      "上游数据源供应商接口错误，message=Params Error"
                      % (ERR_CODE_REMOTE_CONFIG_NOT_FOUND, secu_code))
    if status is None or not (200 <= status < 300):
        if status is not None and "<html" in (text or "")[:400].lower():
            # Cloudflare 拦截（如缺 UA 被 403）也会以 HTML 返回，单独提示便于定位
            return None, ("[%d] 未能从三方配置获取到%s证券的参数，分钟行情获取失败: "
                          "配置服务返回状态码%s（HTML，疑似被拦截）"
                          % (ERR_CODE_REMOTE_CONFIG, secu_code, status))
        return None, ("[%d] 未能从三方配置获取到%s证券的参数，分钟行情获取失败: "
                      "配置服务返回状态码%s" % (ERR_CODE_REMOTE_CONFIG, secu_code, status))
    config = parseJson(text)
    if not isinstance(config, dict):
        return None, ("[%d] 未能从三方配置获取到%s证券的参数，分钟行情获取失败: "
                      "JSON解析异常" % (ERR_CODE_REMOTE_CONFIG, secu_code))
    query_params = config.get("queryParams")
    if not isinstance(query_params, dict):
        return None, ("[%d] 未能从三方配置获取到%s证券的参数，分钟行情获取失败: "
                      "参数转换异常" % (ERR_CODE_REMOTE_CONFIG, secu_code))
    # 只取 queryParams（Go 把其它扩展字段用 json.RawMessage 延迟解析，不参与业务）
    params = {
        PARAM_STOCK_ID: _interface_to_string(query_params.get("stockId")),
        PARAM_MARKET_TYPE: _interface_to_string(query_params.get("marketType")),
        PARAM_TYPE: _interface_to_string(query_params.get("type")),
        PARAM_MARKET_CODE: _interface_to_string(query_params.get("marketCode")),
        PARAM_INSTRUMENT_TYPE: _interface_to_string(query_params.get("instrumentType")),
        PARAM_SUB_INSTRUMENT_TYPE: _interface_to_string(query_params.get("subInstrumentType")),
    }
    req_section = query_params.get("reqSection")
    if req_section is not None:
        # null（非美股）→ 不传 req_section；有值（美股）→ 转为字符串后传入（同 Go）
        params[PARAM_REQ_SECTION] = _interface_to_string(req_section)
    print("[模式2] 远程配置命中: secuCode=%s stockId=%s marketType=%s marketCode=%s "
          "instrumentType=%s subInstrumentType=%s reqSection=%s"
          % (secu_code, params[PARAM_STOCK_ID], params[PARAM_MARKET_TYPE],
             params[PARAM_MARKET_CODE], params[PARAM_INSTRUMENT_TYPE],
             params[PARAM_SUB_INSTRUMENT_TYPE], params.get(PARAM_REQ_SECTION) or "(无)"))
    return params, None


def resolveSecurityParams(secu_code):
    """解析证券参数（三级策略：本地枚举 → 远程配置）

    :param secu_code: 证券代码（如 HSI / 159937 / NVDA）
    :return: (params, error_text)；成功时 error_text 为 None，失败时 params 为 None。
             params 为**按签名顺序**排列、且**不含** `_` 时间戳的 dict（时间戳在请求时追加）。
    """
    code = str(secu_code or "").strip()
    if not code:
        return None, "[%d] 缺少必要参数: secuCode" % ERR_CODE_INTERNAL
    config = SECURITY_CONFIGS.get(code)
    if config is not None:
        print("[模式2] 命中本地枚举参数: secuCode=%s stockId=%s" % (code, config[PARAM_STOCK_ID]))
        return dict(config), None
    print("[模式2] 本地枚举未命中，转取远程配置: secuCode=%s" % code)
    return _load_remote_config(code)


def _build_headers(sign_params):
    """组装上游请求头（对应 Go 的 setRequestHeaders）

    ⚠️ 日志中**绝不可**出现 quote-token / cookie / csrf。

    :param sign_params: 含 `_` 的完整参数 dict
    :return: 请求头 dict
    """
    headers = {
        "accept": ACCEPT_HEADER,
        "accept-language": ACCEPT_LANGUAGE,
        "cache-control": "no-cache",
        "referer": REFERER,
        "user-agent": USER_AGENT,
    }
    # 以下两个头「按需发送」：Go 会回落到硬编码默认值，本移植不带默认值（见模块文档「五」）
    csrf_token = os.environ.get(ENV_CSRF_TOKEN, "").strip()
    if csrf_token:
        headers["futu-x-csrf-token"] = csrf_token
    cookies = os.environ.get(ENV_COOKIES, "").strip()
    if cookies:
        headers["cookie"] = cookies
    headers["quote-token"] = generateQuoteToken(sign_params)
    return headers


def _looks_like_rate_limit(text):
    """识别「HTTP 200 + HTML 限流页」这种伪装成成功的响应

    :param text: 响应体文本
    :return: True 表示疑似限流页
    """
    if not isinstance(text, str) or "<html" not in text[:400].lower():
        return False
    lowered = text.lower()
    return ("operations too frequent" in lowered
            or "too many requests" in lowered
            or "captcha" in lowered or "verify" in lowered)


def _parse_minute_data(raw_data):
    """解析上游 data 节点（对应 Go：先按对象解析，失败则按数组取第一个元素）

    :param raw_data: 上游 data 字段原始值
    :return: 行情数据 dict；结构不可用时返回 None
    """
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, list):
        return raw_data[0] if raw_data and isinstance(raw_data[0], dict) else None
    return None


def _build_minute_list(items):
    """把上游原始行情行转换为精简输出（对应 Go 的 buildMinuteList）

    每个输出行固定 7 个键（Go 的 MinuteItem 无 omitempty）。Go 对 nil 切片输出 null，
    故此处也区分「字段缺失 → None」与「空数组 → []」。

    :param items: 上游 list 原始值（None 表示字段缺失）
    :return: 转换后的行列表；缺失时为 None（序列化即 null）
    :raises _PayloadError: 字段类型不符
    """
    if items is None:
        return None
    if not isinstance(items, list):
        raise _PayloadError("list 期望数组，实际为 %s" % type(items).__name__)
    output = []
    for item in items:
        if not isinstance(item, dict):
            raise _PayloadError("list 元素期望对象，实际为 %s" % type(item).__name__)
        ratio = _require_text(item.get("ratio"), "ratio")
        output.append({
            "ts": _require_number(item.get("time"), "time"),
            "c": _require_number(item.get("cc_price"), "cc_price"),
            "v": _require_number(item.get("volume"), "volume"),
            "t": _require_number(item.get("turnover"), "turnover"),
            "r": ratio,
            "cr": ratio,              # Go 里 r 与 cr 同取 ratio（cr 为备用位）
            "cp": _require_number(item.get("change_price"), "change_price"),
        })
    return output


def _build_section(time_section):
    """构造交易时段数组（对应 Go 的 []TimeSection）

    与 Go 的字段对齐：只保留 begin / end 两个键（Go 反序列化会丢弃未知键），
    缺失的键按 int64 零值补 0；返回空列表表示该键应被省略（Go 的 `,omitempty`）。

    :param time_section: 上游 time_section 原始值
    :return: [{"begin": int, "end": int}, ...]；缺失/为空时返回 []
    """
    if not isinstance(time_section, list) or not time_section:
        return []
    output = []
    for segment in time_section:
        if not isinstance(segment, dict):
            continue
        output.append({
            "begin": _require_number(segment.get("begin"), "time_section.begin"),
            "end": _require_number(segment.get("end"), "time_section.end"),
        })
    return output


def _build_quote_response(minute_data, request_id):
    """按 Go 的 QuoteData 结构组装 data 节点（字段顺序与 Go 结构体定义一致）

    :param minute_data: 上游 data 节点（dict）
    :param request_id: 请求追踪 ID
    :return: {"code":1, "data":{...}, "message":"OK"}（键序同 Go 的 map 序列化：字典序）
    :raises _PayloadError: 字段类型不符
    """
    items = _build_minute_list(minute_data.get("list"))
    section = _build_section(minute_data.get("time_section"))
    data = {}
    data["sid"] = _require_number(minute_data.get("stockId"), "stockId")
    data["list"] = items
    if section:
        # Go 的 `section,omitempty`：为空则整个键不出现
        data["section"] = section
    data["ts_server"] = _require_number(minute_data.get("server_time"), "server_time")
    data["c"] = _require_number(minute_data.get("last_close_price"), "last_close_price")
    data["h"] = _require_number(minute_data.get("highest_price"), "highest_price")
    data["l"] = _require_number(minute_data.get("lowest_price"), "lowest_price")
    data["cnt"] = len(items) if items else 0
    data["request_id"] = request_id
    data["ts"] = int(time.time())
    # 顶层键序 code → data → message（Go 用 map[string]interface{}，键按字典序输出）
    return {"code": SUCCESS_CODE, "data": data, "message": SUCCESS_MESSAGE}


def fetchFiveDayMinuteQuote(secu_code, timeout=DEFAULT_TIMEOUT):
    """拉取单个证券的分钟线行情（模式2：直连 moomoo quote-v2-web）

    **对外契约与模式1 完全一致**：成功返回与 Go 版结构相同的 dict、失败返回 None、
    不抛异常。故调用方（含模式门面）无需任何改动即可切换。

    :param secu_code: 证券代码（如 HSI / 159937 / NVDA）
    :param timeout: 单次上游请求超时秒数，默认 15（与 Go 一致）
    :return: 成功 → {"code":1,"message":"OK","data":{...}}；
             失败 → None（诊断信息已 print，错误码见模块文档「三」）
    """
    params, error = resolveSecurityParams(secu_code)
    if error:
        print("[模式2] %s 参数解析失败: %s" % (secu_code, error))
        return None
    params[PARAM_TIMESTAMP] = str(int(time.time() * 1000))
    url = buildRequestUrl(params)
    print("[模式2] 请求上游: %s" % url)

    status, text, err = sendRequest("GET", url, headers=_build_headers(params), timeout=timeout)
    if err:
        print("[模式2] %s [%d] 上游请求失败: %s" % (secu_code, ERR_CODE_MOOMOO_API, err))
        return None
    if status is None or not (200 <= status < 300):
        print("[模式2] %s [%d] 上游返回状态码%s"
              % (secu_code, ERR_CODE_MOOMOO_API, status))
        return None

    body = parseJson(text)
    if body is None:
        # 非 JSON：可能是限流页（HTTP 200 + HTML），必须单独提示，否则会被误判成成功
        hint = "（疑似上游限流：HTTP 200 但返回 HTML 限流页）" if _looks_like_rate_limit(text) else ""
        print("[模式2] %s [%d] 上游响应非 JSON%s" % (secu_code, ERR_CODE_MOOMOO_API, hint))
        return None
    if not isinstance(body, dict):
        print("[模式2] %s [%d] 上游响应结构异常（顶层不是对象）"
              % (secu_code, ERR_CODE_MOOMOO_API))
        return None

    upstream_code = body.get("code")
    if upstream_code != MOOMOO_SUCCESS_CODE:
        error_code = (ERR_CODE_MOOMOO_API_PARAM_ERROR
                      if upstream_code == MOOMOO_PARAM_ERROR_CODE else ERR_CODE_MOOMOO_API_UNKNOWN)
        print("[模式2] %s [%d] 上游供应商接口错误: code=%s message=%s"
              % (secu_code, error_code, upstream_code, body.get("message")))
        return None

    minute_data = _parse_minute_data(body.get("data"))
    if minute_data is None:
        print("[模式2] %s [%d] 行情数据格式异常（data 既非对象也非非空数组）"
              % (secu_code, ERR_CODE_MOOMOO_API))
        return None

    try:
        quote_data = _build_quote_response(minute_data, str(uuid.uuid4()))
    except _PayloadError as exc:
        print("[模式2] %s [%d] 行情字段类型不符: %s"
              % (secu_code, ERR_CODE_MOOMOO_API, exc))
        return None

    data = quote_data["data"]
    print("[模式2] %s 数据获取成功，记录数: %d（sid=%s ts_server=%s）"
          % (secu_code, data["cnt"], data["sid"], data["ts_server"]))
    return quote_data


def extractMinuteList(quote_data):
    """从已校验的行情 JSON 中取出分钟数据行列表

    与模式1 同名同契约（此处独立实现，不 import 模式1），便于调用方无条件替换。

    :param quote_data: fetchFiveDayMinuteQuote 的成功返回
    :return: 数据行列表（每行 dict，含 ts/c/v/t/r/cr/cp）；缺失或结构异常返回 []
    """
    if not isinstance(quote_data, dict):
        return []
    data_node = quote_data.get("data") or {}
    minute_list = data_node.get("list") if isinstance(data_node, dict) else None
    return minute_list if isinstance(minute_list, list) else []


_assert_config_order()


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("MoomooQuoteV2WebRestClient 是纯函数库（行情获取模式2：直连 moomoo quote-v2-web），"
          "没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from MoomooQuoteV2WebRestClient import fetchFiveDayMinuteQuote, extractMinuteList")
    print("提示：正常使用请通过 FtmmQuoteV2WebRestClient 的模式门面切换（模式常量），"
          "无需直接调用本文件。")
