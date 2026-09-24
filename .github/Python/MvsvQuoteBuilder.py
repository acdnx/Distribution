#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MvsvQuoteBuilder —— MVSV 行情数据文件生成契约库（V5 格式）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把「生成 .mvsv 行情数据文件」的**文件格式契约**（头部字段、字段布局、数据行
映射、命名规则、市场元数据）集中到一个文件维护，供同目录各采集脚本
（当前 FinvQuoteCollectPollFtmm.py，后续可扩展）import 复用。

格式权威依据（不得擅自更改）：
    - 用户提供的 V5 样例 `.github/Python/000985_Min_20260820.mvsv`；
    - 本文件输出的头部与该样例**逐字节一致**（已用测试固定）。

契约变更记录（用户明确要求，勿擅自回调）：
    - 2026-09-25：价格四列由 `o|c|l|h`（开|收|低|高）调整为
      **`o|h|l|c`（Open|High|Low|Close）**，即 OHLC 常规顺序；
      同步更新 # 字段名称 / # Field / # FieldName 三行头部与数据行列序，
      `# FieldType` 不受影响（这四列同为 Decimal）。样例已按新序重排。
    - 2026-09-25：补齐缺失的第 12 个中文名 **前收价**（对应 lc / LastClose）。
      此前 `# 字段名称` 只有 11 项，比 `# Field` / `# FieldName` / `# FieldType`
      少一项——因为本格式的中文名是从更早的 **11 字段**结构
      （Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent）
      沿用下来的，V5 新增第 12 列 lc 时漏补。现四行条目数统一为 12。
    - 2026-09-25：`# TimeZone` 由「恒为空」改为**按市场/地区给缺省 IANA 时区**
      （规则见「三」；CN/SH/SZ/BJ → `Asia/Shanghai`，US → `America/New_York`）。
      调用方仍可传 `timezone=` 显式覆盖，属"临时处理"方案。
    - 2026-09-25：市场标识 `FUTURES` **统一改为 `FUTURE`**（用户要求）。
      改动点共 4 处，必须同改否则查表失配：
        · 本文件 `MARKET_META` 的键与 `"market"` 值；
        · 本文件 `TIMEZONE_BY_REGION` 的键；
        · `Config.json` 中 GCMain 的 `"market"` 值；
        · `FinvQuoteCollectPollFtmm.py` 的 `market in ("FX", "FUTURE")` 判定。
      **不再兼容旧值 `FUTURES`**（按"统一"语义做干净重命名）。
    - 2026-09-25：头部 `# Region` / `# Market` / `# TimeZone` / `# Symbol` 改为
      **优先取状态视图 `finv_quote_collect_state_poll_futu_view` 的直出列**
      （调用方经 `region=` / `market=` / `timezone=` / `symbol=` 传入），
      **不再依赖外部文件（Config.json）关联**；同时 `# SecuCode` / `# USC`
      改用视图主键 **usc**（该表主键字段由 secu_code 改为 usc）。
      实现方式：`resolveMarketMeta` / `buildMvsvContent` 新增可选参数
      `region` / `symbol` / `usc`；**region 与 symbol 都不传时仍走原
      Config.json 兼容路径**，故老调用（只传 market=）行为不变、不回归。
      Market 取值形态随之由「A」变为交易所级（SZ / SH / BJ），
      故新增 CURRENCY_BY_MARKET 覆盖交易所级标识。

形态说明：
    - 纯函数库，没有命令行入口；import 无副作用、不触网；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，私有实现保持 snake_case；
    - 公开函数：buildMvsvContent（文件内容）、buildMvsvFileName（文件命名）、
      resolveMarketMeta（市场元数据解析）。

二、MVSV 文件契约（V5，以样例为准）
----------------------------------------------------------------------------------------
1. 编码 UTF-8（无 BOM），行尾 LF（\\n），文件以换行结束；
2. 头部固定 16 行 + 1 空行，随后是数据行；**没有**采集时间 / 备注汇总行；
3. 头部（大括号内为变量，其余逐字固定，包括引号与冒号后空格）：

    # 标题 : {code} 分钟级行情数据
    # 字段名称 : "时间戳|日期|时间|开盘价|最高价|最低价|收盘价|成交量|成交额|涨跌值|涨跌幅|前收价"
    # DataProvider : FTMM
    # Field : "ts|d|t|o|h|l|c|v|t|cp|cr|lc"
    # FieldName : "Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover|ChangePrice|ChangeRatio|LastClose"
    # FieldType : "Int|Int|Int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal"
    # TypeKLine : Min
    # Count : {行数}
    # Symbol : {完整符号，如 SZ.159937（视图直出）}
    # SecuCode : {usc}
    # USC : {usc}
    # Name : {名称，恒为空（视图有 name_sc 但样例口径为空，是否启用待用户确认）}
    # Region : {区域，如 CN（视图直出）}
    # Market : {市场，如 SZ（视图直出，交易所级）}
    # TimeZone : {IANA 时区标识，如 Asia/Shanghai（视图直出，见「三」）}
    # Currency : {币种，如 CNY}

    其中 Symbol / SecuCode / USC / Region / Market / TimeZone 由状态视图
    `finv_quote_collect_state_poll_futu_view` 的直出列决定（2026-09-25 起）。

4. 数据行按 12 字段以 | 拼接（空字段保留空位，lc 为空时行尾自然带 |）：

    ts | d | t | o | h | l | c | v | t | cp | cr | lc

    | 列 | 取值规则                                                        |
    |----|-------------------------------------------------------------------|
    | ts | 数据源时间戳（秒级 Unix），原样                                  |
    | d  | ts 按**北京时间**换算的 YYYYMMDD                                 |
    | t  | ts 按**北京时间**换算的 HHMMSS                                   |
    | o  | 开盘价 = **前 60 秒（前一分钟）记录**的收盘价；该分钟无记录时为空      |
    | h  | 恒为空（FTMM 分钟线接口不提供 high）                             |
    | l  | 恒为空（FTMM 分钟线接口不提供 low）                              |
    | c  | 数据源收盘价 c，原样                                             |
    | v  | 数据源成交量 v，原样                                             |
    | t  | 数据源成交额 t，原样（与第 3 列同名是 V5 格式的既定怪癖，勿改）  |
    | cp | 数据源涨跌值 cp（相对昨收），原样                                |
    | cr | 数据源涨跌幅 cr（相对昨收，%），原样                             |
    | lc | 与 o **恒等**（前 60 秒记录的收盘价）；该分钟无记录时同样为空
           （中文字段名「前收价」，英文 LastClose）                              |

    价格四列顺序为 **Open|High|Low|Close（o|h|l|c）**，2026-09-25 由
    用户明确要求从 `o|c|l|h` 调整而来。

    数值一律**原样透传**，不做四舍五入 / 补零 / 格式化。
    o 与 lc 恒等（开盘价即上一分钟收盘价），故分钟缺口后的首行两列同时为空。

5. 文件命名：{code}_{period}_{date_suffix}.mvsv（period 默认 Min；
   date_suffix 形如 20260820，**仅日期、北京时间口径，一天一个文件**；
   同一品种同一天重复采集覆盖当日文件。后缀由 DateTimeUtil.fmtDateSuffix 生成）。

三、市场元数据（Symbol / Region / Market / TimeZone / Currency）
----------------------------------------------------------------------------------------
resolveMarketMeta 有**两条取值路径**，判据是「region 与 symbol 是否传入」：

【一号路径 · 视图直出（正常运行时走这条）】
    调用方把状态视图 `finv_quote_collect_state_poll_futu_view` 的 region / market /
    symbol / timezone 原样传入，本库**原样采用、不做推断**，只补两个视图没有的列：
      · Currency：按「市场 → 地区」查 CURRENCY_BY_MARKET / CURRENCY_BY_REGION
        （SH/SZ/BJ/A/CN → CNY，HK → HKD，US/FUTURE/FX/CRYPTO → USD；查不到留空）；
      · TimeZone：调用方未传时按「地区 → 市场」查 TIMEZONE_BY_REGION 取缺省。
    market 若显式传成 Config.json 口径的 `A`，按代码前缀换算为 SH/SZ
    （头部的 Market 一律是交易所级，不写 `A`）。

【二号路径 · 旧 market 口径兼容（只传 market= 时走这条）】
    （注意：本库是**纯函数库，自身从不读任何文件**；这里的「Config.json 口径」指的是
      调用方过去从 Config.json 拿到的 market 取值词表（A / HK / US / FX / FUTURE / CRYPTO），
      2026-09-25 起调用方已改从状态视图取元数据，这条路径只为兼容老调用而保留。）
    region 与 symbol 都未传 → 判定为旧调用口径，按原规则解析：
      · market="A"：Symbol/Market 前缀按代码判定交易所（5/6/9 开头 → SH，
        其余 0/2/3 等 → SZ），Region=CN，Currency=CNY，TimeZone=Asia/Shanghai；
      · 其他 market：按 MARKET_META 表取 Market/Region，Symbol = {Market}.{code}；
        表内取值是与用户确认的约定，**调整须经用户同意**；
      · market 未知 / 未传：Symbol={code}，其余留空（宁空勿猜）。

TimeZone（IANA 标识，如 Asia/Shanghai）：**优先用调用方显式传入的 timezone 参数**
（视图直出值）；未传时按「地区 → 市场」查 TIMEZONE_BY_REGION 取缺省；两者都查不到
则留空（宁空勿猜）。当前缺省口径（2026-09-25 用户指定，属"临时处理"，日后可改为
数据源直出）：
    · CN / SH / SZ / BJ → `Asia/Shanghai`
    · US（含 Region=US 的 FUTURE）→ `America/New_York`（美东，自带 EST/EDT 夏令时）
    · HK → `Asia/Hong_Kong`
    · FX / CRYPTO → 留空（24 小时市场，无单一时区）

四、使用示例（同目录脚本）
----------------------------------------------------------------------------------------
    from MvsvQuoteBuilder import buildMvsvContent, buildMvsvFileName

    # 一号路径：头部市场元数据取自状态视图直出列（推荐）
    content = buildMvsvContent("159937", minute_list, usc="159937",
                               region="CN", market="SZ", symbol="SZ.159937",
                               timezone="Asia/Shanghai")

    # 二号路径：无视图元数据时按 Config.json 口径解析（兼容旧调用）
    content = buildMvsvContent("000985", minute_list, market="A")
    file_name = buildMvsvFileName("000985", "20260820")       # 000985_Min_20260820.mvsv

【环境要求】Python 3.8+，仅标准库（时区换算复用同目录 DateTimeUtil）。
"""

from datetime import datetime

from DateTimeUtil import BEIJING_TZ

# ============ MVSV 契约常量（与 V5 样例逐字一致，禁止擅改） ============
# 数据供应商（# DataProvider）
PROVIDER = "FTMM"
# 字段布局（# Field；注意 v 后面的第二个 t 是成交额，与第 3 列时间同名，为既定怪癖）
# 价格四列顺序为 Open|High|Low|Close（o|h|l|c），2026-09-25 用户明确要求
FIELD_LAYOUT = "ts|d|t|o|h|l|c|v|t|cp|cr|lc"
# 字段名称（# 字段名称，中文；12 项，与 # Field / # FieldName / # FieldType 一一对齐；
# 第 12 项「前收价」= LastClose = lc，2026-09-25 经用户确认补入）
FIELD_NAMES_CN = ("时间戳|日期|时间|开盘价|最高价|最低价|收盘价|成交量|成交额|涨跌值|涨跌幅"
                  "|前收价")
# 字段名称（# FieldName，英文，12 项）
FIELD_NAMES_EN = ("Timestamp|Date|Time|Open|High|Low|Close|Volume|Turnover"
                  "|ChangePrice|ChangeRatio|LastClose")
# 字段类型（# FieldType，3 Int + 9 Decimal；价格四列同为 Decimal，故列序调整不影响本行）
FIELD_TYPES = "Int|Int|Int|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal|Decimal"
# 数据行字段序（与 FIELD_LAYOUT 一一对应）
DATA_ROW_KEYS = ("ts", "d", "t", "o", "h", "l", "c", "v", "t", "cp", "cr", "lc")


def _assert_contract_consistent():
    """import 期自检：字段定义各行必须等长，防止「改了一处漏改其余」

    2026-09-25 就出现过 `# 字段名称` 只有 11 项、比其余三行少一项的漏补问题，
    故这里直接以 FIELD_LAYOUT 的项数为基准，不一致即抛错（fail fast，宁可启动即挂）。
    """
    expect = len(FIELD_LAYOUT.split("|"))
    groups = {
        "# 字段名称": FIELD_NAMES_CN,
        "# Field": FIELD_LAYOUT,
        "# FieldName": FIELD_NAMES_EN,
        "# FieldType": FIELD_TYPES,
        "数据行列序": "|".join(DATA_ROW_KEYS),
    }
    bad = {k: len(v.split("|")) for k, v in groups.items() if len(v.split("|")) != expect}
    if bad:
        raise ValueError("MVSV 契约字段数不一致（应以 # Field 的 %d 项为准）：%r" % (expect, bad))


_assert_contract_consistent()

# 各周期的中文标题后缀（V5 样例：Min → 分钟级行情数据）
TITLE_BY_PERIOD = {"Min": "分钟级行情数据"}
# 未知周期的标题兜底
DEFAULT_TITLE = "行情数据"

# ============ 市场元数据（非 A 市场的取值约定，调整须经用户同意） ============
# 【二号路径 Config.json 兼容】按 Config.json 的 market 取值查表；仅映射 Market/Region，
# Currency / TimeZone 由 _resolve_currency / _resolve_default_timezone 统一补缺省。
# 【一号路径 视图直出】不使用本表（视图的 region/market/symbol 已权威）。
MARKET_META = {
    "HK":     {"market": "HK",     "region": "HK"},
    "US":     {"market": "US",     "region": "US"},
    "FX":     {"market": "FX",     "region": ""},
    "FUTURE": {"market": "FUTURE", "region": "US"},
    "CRYPTO": {"market": "CRYPTO", "region": ""},
}
# A 股判定为 SH 的代码首字符（60/68 主板科创、5x ETF、9x B 股等）
CN_SH_PREFIXES = ("5", "6", "9")
# 交易所级 A 股市场标识（视图 market 的取值形态；用于时段判定等需要「A 股整体」口径的场景）
CN_EXCHANGES = ("SH", "SZ", "BJ")

# ============ 币种缺省（# Currency；视图不返回币种，由 Market / Region 推） ============
CURRENCY_BY_MARKET = {
    "A": "CNY", "SH": "CNY", "SZ": "CNY", "BJ": "CNY",   # A 股及其三个交易所
    "HK": "HKD",
    "US": "USD", "FUTURE": "USD", "FX": "USD", "CRYPTO": "USD",
}
CURRENCY_BY_REGION = {"CN": "CNY", "HK": "HKD", "US": "USD"}

# ============ 时区缺省（# TimeZone；2026-09-25 用户指定的"临时处理"口径） ============
# 中国大陆（沪/深/北，含 Region=CN）统一用 Asia/Shanghai（无夏令时）
CN_TIMEZONE = "Asia/Shanghai"
# 美东：America/New_York 是 IANA 对 US Eastern Time 的标准标识，自带 EST/EDT 切换
US_EASTERN_TIMEZONE = "America/New_York"
# 「地区 / 市场标识 → IANA 时区标识」缺省表（键统一大写）。
# 查不到即留空（宁空勿猜）：FX / CRYPTO 是 24 小时市场，无单一时区。
TIMEZONE_BY_REGION = {
    "CN": CN_TIMEZONE,               # A 股 Region
    "SH": CN_TIMEZONE,               # 上交所
    "SZ": CN_TIMEZONE,               # 深交所
    "BJ": CN_TIMEZONE,               # 北交所
    "US": US_EASTERN_TIMEZONE,       # 美股 / 美区
    "HK": "Asia/Hong_Kong",          # 港股
    "FUTURE": US_EASTERN_TIMEZONE,  # 期货：Region 为空时的兜底
}


def resolveCnExchange(secu_code):
    """按 A 股代码判定交易所：5/6/9 开头 → SH，其余 → SZ

    :param secu_code: 6 位 A 股代码（如 600570 / 000985 / 159934）
    :return: "SH" 或 "SZ"
    """
    return "SH" if str(secu_code).startswith(CN_SH_PREFIXES) else "SZ"


def _resolve_default_timezone(resolved_market, region):
    """按「地区 → 市场」取 # TimeZone 缺省 IANA 标识；都查不到返回空串（宁空勿猜）"""
    for key in (region, resolved_market):
        tz = TIMEZONE_BY_REGION.get((key or "").strip().upper())
        if tz:
            return tz
    return ""


def _resolve_currency(region, market):
    """按「市场 → 地区」取 # Currency；都查不到返回空串（宁空勿猜）"""
    for table, key in ((CURRENCY_BY_MARKET, market), (CURRENCY_BY_REGION, region)):
        currency = table.get((key or "").strip().upper())
        if currency:
            return currency
    return ""


def _compose_market_meta(market, region, symbol):
    """把 Market / Region / Symbol 组合成五键元数据（Currency 与 TimeZone 补缺省）"""
    return {"symbol": symbol, "region": region, "market": market,
            "currency": _resolve_currency(region, market),
            "timezone": _resolve_default_timezone(market, region)}


def _resolve_legacy_market_meta(secu_code, market):
    """二号路径：按 Config.json 口径的 market 解析元数据（无视图元数据时的旧行为）"""
    m = (market or "").strip().upper()
    if m == "A":
        exch = resolveCnExchange(secu_code)
        return _compose_market_meta(exch, "CN", "%s.%s" % (exch, secu_code))
    info = MARKET_META.get(m)
    if info is None:
        return _compose_market_meta("", "", str(secu_code))
    return _compose_market_meta(info["market"], info["region"],
                                "%s.%s" % (info["market"], secu_code))


def resolveMarketMeta(secu_code, market=None, region="", symbol="", usc=""):
    """解析 V5 头部的市场元数据（Symbol / Region / Market / TimeZone / Currency）

    两条路径，判据是「region 与 symbol 是否传入」（规则详见模块文档「三」）：
      1) 视图直出：region / market / symbol 原样采用（只补 Currency 与 TimeZone 缺省）；
      2) Config.json 兼容：region 与 symbol 都未传时按旧口径解析（老调用不回归）。

    :param secu_code: 证券代码（写入 # SecuCode；视图路径下一般就传 usc）
    :param market: 市场标识；视图口径为交易所级（SH/SZ/BJ/HK/US/FX/FUTURE/CRYPTO），
                   兼容口径为 Config.json 口径（A/HK/US/FX/FUTURE/CRYPTO）
    :param region: 视图直出的地区（如 CN / HK / US）；传入即走视图路径
    :param symbol: 视图直出的完整符号（如 SZ.159937）；传入即走视图路径
    :param usc: 视图主键 USC（视图路径下用于推导 Symbol 的兜底）
    :return: {"symbol","region","market","timezone","currency"} 五键 dict
    """
    m = (market or "").strip().upper()
    r = (region or "").strip().upper()
    sym = (symbol or "").strip()

    # 二号路径：只有 market（旧调用口径）
    if not r and not sym:
        return _resolve_legacy_market_meta(secu_code, market)

    # 一号路径：视图直出。头部的 Market 一律交易所级，故「A」按代码前缀换算
    if m == "A":
        m = resolveCnExchange(usc or secu_code)
    if not sym:
        sym = ("%s.%s" % (m, usc or secu_code)) if m else str(usc or secu_code)
    return _compose_market_meta(m, r, sym)


def buildMvsvFileName(secu_code, date_suffix, period="Min"):
    """生成 MVSV 文件名：{code}_{period}_{date_suffix}.mvsv

    :param secu_code: 证券代码（如 000985）
    :param date_suffix: 日期后缀（形如 20260820，调用方用 DateTimeUtil.fmtDateSuffix
                        按北京时间生成；**仅日期**，故一天一个文件）
    :param period: 数据周期标识，默认 Min（分钟级）
    :return: 文件名字符串（不含目录），如 000985_Min_20260820.mvsv
    """
    return "%s_%s_%s.mvsv" % (secu_code, period, date_suffix)


def _fmt(value):
    """数值原样转字符串；None 转空串（不做任何格式化/取整）"""
    return "" if value is None else str(value)


def _build_data_rows(minute_list):
    """把数据源分钟行映射为 V5 的 12 字段数据行（规则见模块文档「二、4」）"""
    # ts -> 收盘价 索引：lc 按**时间口径**取 ts−60（前一分钟）记录的收盘价，
    # 与列表顺序无关——分钟有缺口（隔夜/休市）时，缺口后首行的 lc 为空
    close_by_ts = {}
    for item in minute_list:
        try:
            close_by_ts[int(item.get("ts"))] = item.get("c")
        except (TypeError, ValueError):
            continue
    rows = []
    for item in minute_list:
        ts = item.get("ts", "")
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            ts_int = None
        if ts_int is None:
            d_txt, t_txt, o_txt = "", "", ""
        else:
            dt_bj = datetime.fromtimestamp(ts_int, tz=BEIJING_TZ)
            d_txt, t_txt = dt_bj.strftime("%Y%m%d"), dt_bj.strftime("%H%M%S")
            # 开盘价 = 前一分钟（ts−60）记录的收盘价；该分钟无记录则为空
            o_txt = _fmt(close_by_ts.get(ts_int - 60))
        # o 与 lc **恒等**（同取前一分钟收盘价）；h / l 接口不提供恒为空
        # 列序：ts | d | t | o | h | l | c | v | t | cp | cr | lc（OHLC 常规顺序）
        rows.append("|".join([
            _fmt(ts), d_txt, t_txt, o_txt, "", "", _fmt(item.get("c")),
            _fmt(item.get("v")), _fmt(item.get("t")), _fmt(item.get("cp")),
            _fmt(item.get("cr", item.get("r"))), o_txt,
        ]))
    return rows


def buildMvsvContent(secu_code, minute_list, market=None, name="", period="Min",
                     timezone="", region="", symbol="", usc=""):
    """构建 V5 格式 MVSV 文件完整文本（头部 16 行 + 空行 + 数据行，以 \\n 结尾）

    头部 Symbol / SecuCode / USC / Region / Market / TimeZone / Currency 的取值口径
    见模块文档「三」：region / symbol 传入即走**状态视图直出**路径（推荐）。

    :param secu_code: 证券代码（写入 # SecuCode；视图路径下一般与 usc 同值）
    :param minute_list: 分钟数据行列表（数据源原始 dict 列表，含 ts/c/v/t/cr/cp 键）
    :param market: 市场标识（视图直出为交易所级 SH/SZ/BJ/…；兼容路径为 A/HK/US/…）
    :param name: 证券名称；**恒为空**（视图有 name_sc，但样例口径为空，是否启用待确认）
    :param period: 数据周期标识，默认 Min（写入 # TypeKLine 与标题）
    :param timezone: IANA 时区标识（视图直出，如 Asia/Shanghai）；
                     留空则按 market / 地区取缺省，查不到仍留空
    :param region: 视图直出的地区（如 CN）
    :param symbol: 视图直出的完整符号（如 SZ.159937）
    :param usc: 视图主键 USC（写入 # USC）；留空回落到 secu_code
    :return: 文件完整文本（UTF-8、LF、以换行结束）
    """
    meta = resolveMarketMeta(secu_code, market, region=region, symbol=symbol, usc=usc)
    tz = (timezone or "").strip() or meta["timezone"]
    lines = [
        "# 标题 : %s %s" % (secu_code, TITLE_BY_PERIOD.get(period, DEFAULT_TITLE)),
        '# 字段名称 : "%s"' % FIELD_NAMES_CN,
        "# DataProvider : %s" % PROVIDER,
        '# Field : "%s"' % FIELD_LAYOUT,
        '# FieldName : "%s"' % FIELD_NAMES_EN,
        '# FieldType : "%s"' % FIELD_TYPES,
        "# TypeKLine : %s" % period,
        "# Count : %d" % len(minute_list),
        "# Symbol : %s" % meta["symbol"],
        "# SecuCode : %s" % secu_code,
        "# USC : %s" % (usc or secu_code),
        "# Name : %s" % name,
        "# Region : %s" % meta["region"],
        "# Market : %s" % meta["market"],
        "# TimeZone : %s" % tz,
        "# Currency : %s" % meta["currency"],
        "",
    ]
    lines.extend(_build_data_rows(minute_list))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("MvsvQuoteBuilder 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from MvsvQuoteBuilder import buildMvsvContent, buildMvsvFileName")
