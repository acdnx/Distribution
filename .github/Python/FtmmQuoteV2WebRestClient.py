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
    - 公开函数：resolveApiBase（端点解析）、fetchFiveDayMinuteQuote（分钟线行情拉取，按模式分发）、
      extractMinuteList（从响应取数据行）、currentQuoteMode / isQuoteSourceReady /
      describeQuoteSource（模式读取与统一就绪判定）；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，模块私有实现保持 snake_case；
    - HTTP 收发复用同目录 HttpUtil 的 sendRequest / parseJson（通用传输层），
      本文件只负责行情业务语义（端点解析、URL 拼装、响应校验、日志口径）；
    - 纯标准库（urllib 经 HttpUtil 间接使用），可直接拷到任何 Python 3.8+ 环境使用。

二、行情获取模式（迁移开关，默认沿用原有行为）
----------------------------------------------------------------------------------------
| 模式 | 取值 | 行为                                                              |
|------|------|-------------------------------------------------------------------|
| 1    | 默认 | 请求环境变量指定的自建端点（LAMBDA_FTMM_API_BASE）——原有行为不变   |
| 2    | 可选 | 直连 moomoo 官方 quote-v2-web                                     |

模式2 的实现位于同目录 MoomooQuoteV2WebRestClient.py。

**开关 = 本文件顶部的常量 `QUOTE_FETCH_MODE`**（纯代码常量，**不读任何环境变量或配置文件**）：
默认 `MODE_CFP_ENDPOINT`（=1，原有行为）；要切到模式2 就把它改成 `MODE_MOOMOO`（=2）并提交。
取值非法时**导入即抛 ValueError**（fail fast，避免写错值后静默走错分支）。
两种模式**对外契约完全相同**（成功返回同结构 dict、失败返回 None），因此调用方无需改判读
逻辑，切换只改这一个常量。程序内如需临时覆盖，可用 fetchFiveDayMinuteQuote 的仅限关键字
参数 mode（供测试/灰度用，不参与生产配置）。
两套实现**互不依赖**：本文件对模式2 的引用是「单向 + 运行时惰性 import」，且仅在模式2
生效时发生；模式2 不 import 本文件任何内容，故任一侧缺失或损坏都不影响另一侧独立可用。

⚠️ 已核对（2026-09-25）：落盘文件 .mvsv 的头部元数据 Name / Symbol / Region / Market /
TimeZone **全部取自状态视图**（collect_single 的 meta 入参），与行情响应无关；采集侧也只消费
响应的 data["list"]。故模式2 不返回 name 这类元数据字段，对落盘内容**零影响**——两模式产出的
.mvsv 在数据行与头部上都应逐字节一致。

三、端点解析规则
----------------------------------------------------------------------------------------
| 输入      | 优先级                                                            |
|-----------|-------------------------------------------------------------------|
| base_url  | 1) 调用方显式传入的参数（局部覆盖，便于测试或同脚本多端点）        |
|           | 2) 环境变量 LAMBDA_FTMM_API_BASE（主用；secrets/vars 注入）         |
|           | 3) 环境变量 API_BASE（兼容旧名；主用未配置时才回落）                |
|           | 4) 占位符 DEFAULT_API_BASE（===LAMBDA_FTMM_API_BASE===，**不可请求**）|

⚠️ 端点属运行机密，**代码内不写死真实端点**：DEFAULT_API_BASE 只是「未配置」的可见标记，
不是可用端点。拿到解析值后必须先用 isApiBaseUsable 判定，否则请求会失败。

四、返回值契约（与旧版 Poll.py 一致，调用方无需改判读逻辑）
----------------------------------------------------------------------------------------
    fetchFiveDayMinuteQuote(secu_code, ...) →
        成功：行情原始 JSON dict（已校验 code == 1 且 data 非空），
              调用方可取 data["list"]（分钟数据行，每行含 ts/c/v/t/r/cr/cp），
              以及 data 的 sid / section / ts_server / c / h / l / cnt / request_id / ts；
        失败：None（网络异常 / 非 JSON / 业务码非 1 / data 为空）。
        ⚠️ 上面这份 data 字段清单**两种模式一致**：模式2 直连 moomoo 时按 Go 版
        QuoteData 的结构组装，刻意与模式1 经云端拿到的结构保持同形。
        （2026-09-25 核对：Go 的 futuquoteminute / futuquoteapi 两个版本都没有 `o` 字段，
          故此处不再列出 `o`；本作业实际只消费 data["list"]。）
    失败一律返回 None，**不抛异常**（单品种失败不应打断轮询链路，由调用方记
    count_fail 做指数退避）；诊断信息走 print，调用方 stdout 只含自己的输出。

五、环境变量
----------------------------------------------------------------------------------------
    LAMBDA_FTMM_API_BASE   必填   行情 API 端点（主用；在仓库 Settings → Secrets and
                                 variables → Actions 里配成 secret，避免明文入日志）
                                  —— **仅模式1 需要**；模式2 不使用端点
    API_BASE              兼容   旧名；仅当主用未配置时回落读取
    两者都未配置且处于模式1 → 解析为占位符，isApiBaseUsable 判定 False，请求直接失败。
    ⚠️ **行情获取模式不是环境变量**，而是本文件顶部的 QUOTE_FETCH_MODE 常量（见「二」）。

六、使用示例（同目录脚本）
----------------------------------------------------------------------------------------
    from FtmmQuoteV2WebRestClient import fetchFiveDayMinuteQuote, extractMinuteList

    raw = fetchFiveDayMinuteQuote("HSI")            # 端点按 参数 → API_BASE → 默认 解析
    if raw is None:
        ...                                    # 采集失败：记 fail 计数
    minute_list = extractMinuteList(raw)     # 分钟数据行列表（可能为空 list）

    # 切换行情获取模式：改本文件顶部的 QUOTE_FETCH_MODE 常量后提交，调用代码一行都不用动
    #   QUOTE_FETCH_MODE = MODE_CFP_ENDPOINT  → 走自建端点（默认，原行为）
    #   QUOTE_FETCH_MODE = MODE_MOOMOO    → 直连 moomoo 官方 quote-v2-web

七、注意事项
----------------------------------------------------------------------------------------
    - 请求参数固定为 ?secuCode={code}（与旧版一致；代码做 URL 编码，支持非 ASCII 代码）；
    - 单次请求默认 30 秒超时；
    - **日志不打印明文端点**：请求 URL 一律经 maskApiUrl 打码（域名只留前 2 字符、
      路径只留末段资源名、保留 scheme 与查询串），避免端点经日志外泄；
      不依赖 Actions 的 secret 自动遮蔽（配成 var 或本地运行时就不会被遮蔽）；
    - 端点只填端点本身，不要带查询串。
    - **模式2 的上游限流风险**（2026-09-25 实测定量，切换前请先读）：moomoo 对同一来源 IP
      **连发约 7 次**后返回「HTTP 200 + HTML 限流页」，且拦截**持续约 12 分钟**
      （实测 05:43:35 被拦 → 05:56:05 才恢复；期间每 60s 探测连续 6 次均被拦）。
      本作业约 0.6 次/分钟，与实测触发条件（≈38 次/分钟）相差约 60 倍，当前节奏应安全；
      但**持续速率的安全配额未测出**，调整 POLL_COUNT / 轮询周期前请先用 dry_run 观察。
      识别方式、完整时间线与补救手段（注入 cookie/csrf 环境变量，无需改代码）
      见同目录 MoomooQuoteV2WebRestClient.py 模块文档「六」。

【环境要求】Python 3.8+，仅标准库；模式1 需可直连行情 API 端点，模式2 需可直连 www.moomoo.com。
"""

import os
import urllib.parse

from HttpUtil import parseJson, sendRequest

# ============ 端点与请求常量 ============
# 端点环境变量名（约定专用，主用）
ENV_API_BASE = "LAMBDA_FTMM_API_BASE"
# 端点环境变量名（兼容旧名；主用未配置时才回落读它）
ENV_API_BASE_FALLBACK = "API_BASE"
# 「未配置」占位符：不是可用端点，仅供日志自证与 isApiBaseUsable 判定。
# 端点属运行机密，不写死在代码里，必须由 secrets/vars 注入。
DEFAULT_API_BASE = "===%s===" % ENV_API_BASE
# 合法端点协议前缀
URL_SCHEMES = ("http://", "https://")
# 日志打码：域名保留的前缀字符数（给个环境提示，不足以还原域名）
HOST_KEEP = 2
# 日志打码：路径末尾保留的段数（资源名，便于确认调用的接口）
PATH_TAIL_KEEP = 1
# 单次请求默认超时（秒）
DEFAULT_TIMEOUT = 30
# 请求头 User-Agent
USER_AGENT = "FtmmQuoteV2WebRestClient/1.0"
# 证券代码查询参数名（与旧版一致）
PARAM_SECU_CODE = "secuCode"
# API 业务成功码（与旧版一致）
SUCCESS_CODE = 1

# ============ 行情获取模式（**纯常量开关**，切换只改下面这一行） ============
# 模式1：从环境变量指定的 HTTP 端点获取（原有行为，见下方「三、端点解析规则」）
MODE_CFP_ENDPOINT = 1
# 模式2：直连 moomoo 官方 quote-v2-web（实现在同目录 MoomooQuoteV2WebRestClient.py）
MODE_MOOMOO = 2
SUPPORTED_MODES = (MODE_CFP_ENDPOINT, MODE_MOOMOO)

# ★★ 当前生效的行情获取模式：**取值只能是 MODE_CFP_ENDPOINT 或 MODE_MOOMOO**。
#     切换 = 改这一行 + 提交（本文件是唯一的模式归口，**不读任何环境变量/配置文件**）。
#     默认 MODE_CFP_ENDPOINT，即「不动这一行 ⇒ 行为与模式改造前完全一致」。
QUOTE_FETCH_MODE = MODE_CFP_ENDPOINT

# 模式2 的实现模块名（同目录，惰性 import）
MOOMOO_CLIENT_MODULE = "MoomooQuoteV2WebRestClient"

# 常量取值检查：模式是**改代码**的开关，写错值必须立刻炸掉，不能静默走错分支
# （静默回落会让「我明明改成 2 了」变成一场排查噩梦；导入期即失败最省事）
if QUOTE_FETCH_MODE not in SUPPORTED_MODES:
    raise ValueError(
        "QUOTE_FETCH_MODE 常量取值非法：%r（只能填 MODE_CFP_ENDPOINT(%s) 或 MODE_MOOMOO(%s)）；"
        "请修正 %s 顶部的 QUOTE_FETCH_MODE"
        % (QUOTE_FETCH_MODE, MODE_CFP_ENDPOINT, MODE_MOOMOO, __file__))


def currentQuoteMode():
    """返回当前生效的行情获取模式（即顶部的 QUOTE_FETCH_MODE 常量）

    保留成函数而非让调用方直接读常量，是为了给模式分发留一个**单一入口**：
    将来若要再引入别的取值来源，只需改这里，所有调用方都不用动。

    :return: MODE_CFP_ENDPOINT(1) 或 MODE_MOOMOO(2)
    """
    return QUOTE_FETCH_MODE


def describeQuoteSource():
    """当前行情来源的人类可读描述（日志用）

    :return: 形如「模式1：环境变量端点 LAMBDA_FTMM_API_BASE」/「模式2：...」
    """
    if currentQuoteMode() == MODE_MOOMOO:
        return "模式%d：直连 moomoo quote-v2-web（%s.py）" % (MODE_MOOMOO, MOOMOO_CLIENT_MODULE)
    return "模式%d：环境变量端点 %s" % (MODE_CFP_ENDPOINT, ENV_API_BASE)


def _load_moomoo_client():
    """惰性加载模式2 客户端模块（**只有模式2 生效时才会被调用**）

    刻意不在文件顶部 import：
      · 保证「两种实现互不依赖」在静态依赖上成立——本文件对模式2 的引用是单向、运行时、可选；
      · 模式2 文件缺失或损坏时，模式1 依然完全可用（这里会给出明确提示并走失败契约）。
    """
    try:
        import MoomooQuoteV2WebRestClient as moomoo_client
    except ImportError as exc:
        print("[行情] ⚠️ 模式2 需要同目录的 %s.py，导入失败：%s"
              % (MOOMOO_CLIENT_MODULE, exc))
        return None
    return moomoo_client


def isQuoteSourceReady():
    """行情数据源是否就绪（两种模式**统一**的就绪判定，供启动自检使用）

    - 模式1：端点已配置且形态合法（http/https）；
    - 模式2：直连公开上游，不需要任何本地端点配置，恒就绪。

    :return: True 可以发起行情请求；False 配置缺失（调用方应据此提前失败）
    """
    if currentQuoteMode() == MODE_MOOMOO:
        client = _load_moomoo_client()
        return bool(client) and client.isUpstreamReady()
    return isApiBaseUsable(resolveApiBase())


def _fetch_moomoo_minute_quote(secu_code, timeout, base_url):
    """模式2 分支：委托给 MoomooQuoteV2WebRestClient 直连 moomoo

    契约与模式1 分支一致（成功返回 dict、失败返回 None），故调用方无需感知差别。
    """
    if base_url:
        print("[行情] 提示：模式2 不使用端点参数，已忽略传入的 base_url")
    client = _load_moomoo_client()
    if client is None:
        return None
    return client.fetchFiveDayMinuteQuote(secu_code, timeout=timeout)


def resolveApiBase(base_url=None):
    """解析行情 API 端点：参数 → 环境变量 LAMBDA_FTMM_API_BASE → 兼容旧名 API_BASE → 占位符

    注意：本函数**不校验**可用性。未配置任何环境变量时返回占位符 DEFAULT_API_BASE
    （形如 ===LAMBDA_FTMM_API_BASE===，不是合法 URL），调用方应先用
    isApiBaseUsable 判定，避免把占位符当端点发出请求。

    :param base_url: 调用方显式传入的端点；None 时走环境变量 / 占位符
    :return: 端点字符串（已 strip，末尾不带 /）
    """
    for candidate in ((base_url or "").strip(),
                      os.environ.get(ENV_API_BASE, "").strip(),
                      os.environ.get(ENV_API_BASE_FALLBACK, "").strip()):
        if candidate:
            return candidate.rstrip("/")
    return DEFAULT_API_BASE


def isApiBaseUsable(base):
    """判断端点是否已配置且形态合法（http/https）

    :param base: 端点字符串（通常来自 resolveApiBase）
    :return: True 可发起请求；False 为未配置占位符或非法形态
    """
    return isinstance(base, str) and base.startswith(URL_SCHEMES)


def maskApiUrl(url):
    """把请求 URL 打码后用于日志输出（端点属运行机密，禁止明文入日志）

    打码规则（只保留诊断必需的最小信息）：
        - 保留 scheme（http/https）与查询串（? 之后，只含证券代码等无机密参数）；
        - **域名**只保留前 HOST_KEEP 个字符，其余（含完整域名与端口）打码；
        - **路径前缀**逐段打码，只保留末尾 PATH_TAIL_KEEP 段（资源名，便于确认调用的是哪个接口）。

    示例（**全部为虚构值**：保留域名 + 与真实端点无任何重合的主机前缀与路径）：
        https://api.example.com/v1/market/minute/list?code=517400
          → https://ap***/***/***/***/list?code=517400

    ⚠️ 本函数本身就是「端点不落明文」的一环，故**文档与注释里同样不得出现真实端点**：
    主机名（**含自定义子域前缀**）、路径、参数名一律不得复用真实值——
    只换顶级域而留着真实子域，照样等于泄漏（打码本就保留主机名前 2 字符，两头一对即被猜出；
    连「不要这样写」的反面示例里都不该照抄真实子域）。举例请用 example.com 之类的保留域名 + 自拟通用路径。

    :param url: 完整请求 URL（也可以是端点 base）
    :return: 打码后的字符串
    """
    if not isinstance(url, str) or not url:
        return "***"
    if "://" in url:
        scheme, rest = url.split("://", 1)
        prefix = scheme + "://"
    else:
        prefix, rest = "", url
    # 查询串原样保留；只处理 ? 之前的部分
    path, sep, query = rest.partition("?")
    host, slash, tail = path.partition("/")
    masked_host = (host[:HOST_KEEP] + "***") if host else "***"
    if not slash:
        masked_path = ""
    else:
        segments = [s for s in tail.split("/") if s]
        if not segments:
            masked_path = "/***"
        else:
            keep = segments[-PATH_TAIL_KEEP:] if PATH_TAIL_KEEP > 0 else []
            masked_path = "/" + "/".join(["***"] * (len(segments) - len(keep)) + keep)
    return "%s%s%s%s%s" % (prefix, masked_host, masked_path, sep, query)


def fetchFiveDayMinuteQuote(secu_code, base_url=None, timeout=DEFAULT_TIMEOUT, *, mode=None):
    """拉取单个证券的分钟线行情（按当前模式分发：1=端点 / 2=直连 moomoo）

    **签名向后兼容**：新增的 mode 是仅限关键字、可选参数，原有调用
    `fetchFiveDayMinuteQuote(code)` / `(code, base_url)` / `(code, base_url, timeout)`
    行为与返回结构完全不变（默认走 QUOTE_FETCH_MODE 常量 = MODE_CFP_ENDPOINT = 原有实现）。

    :param secu_code: 证券代码（如 HSI / 000001 / NVDA）
    :param base_url: 显式端点（**仅模式1 使用**）；None 时按 resolveApiBase 的规则解析
    :param timeout: 单次请求超时秒数，默认 30（模式2 的默认值在模式2 文件内，为与 Go 一致的 15）
    :param mode: 显式指定模式（**仅供测试/灰度**）；None 表示用 QUOTE_FETCH_MODE 常量
    :return: 成功 → 行情 JSON dict（已校验 code == 1 且 data 非空）；
             失败 → None（不抛异常，诊断信息已 print）
    """
    effective_mode = currentQuoteMode() if mode is None else mode
    if effective_mode == MODE_MOOMOO:
        return _fetch_moomoo_minute_quote(secu_code, timeout, base_url)

    # ---------- 以下为模式1（端点模式）的原有实现 ----------
    base = resolveApiBase(base_url)
    if not isApiBaseUsable(base):
        # 端点未配置（占位符）或形态非法：明确报错并走失败契约，
        # 不让 urllib 抛 "unknown url type" 这类无信息量的异常
        # 占位符可直接展示（它就是「未配置」的标记）；真实端点值一律打码后再打印
        shown = base if base == DEFAULT_API_BASE else maskApiUrl(base)
        print("[行情] 端点未配置或非法（解析值: %s）；请在仓库 secrets/vars 配置环境变量 %s，"
              "值为完整 http(s) 端点（如 https://api.example.com/v1/market/minute）"
              % (shown, ENV_API_BASE))
        return None
    url = "%s?%s=%s" % (base, PARAM_SECU_CODE,
                        urllib.parse.quote(str(secu_code), safe=""))
    print("[行情] 请求数据: %s" % maskApiUrl(url))
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
