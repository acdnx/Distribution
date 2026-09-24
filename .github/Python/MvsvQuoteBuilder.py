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

字段顺序变更记录（用户明确要求，勿擅自回调）：
    - 2026-09-25：价格四列由 `o|c|l|h`（开|收|低|高）调整为
      **`o|h|l|c`（Open|High|Low|Close）**，即 OHLC 常规顺序；
      同步更新 # 字段名称 / # Field / # FieldName 三行头部与数据行列序，
      `# FieldType` 不受影响（这四列同为 Decimal）。样例已按新序重排。
    - 2026-09-25：补齐缺失的第 12 个中文名 **前收价**（对应 lc / LastClose）。
      此前 `# 字段名称` 只有 11 项，比 `# Field` / `# FieldName` / `# FieldType`
      少一项——因为本格式的中文名是从更早的 **11 字段**结构
      （Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent）
      沿用下来的，V5 新增第 12 列 lc 时漏补。现四行条目数统一为 12。

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
    # Symbol : {exchange}.{code}
    # SecuCode : {code}
    # USC : {code}
    # Name : {名称，暂无恒为空}
    # Region : {区域}
    # Market : {市场}
    # TimeZone : {暂无恒为空}
    # Currency : {币种}

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

三、市场元数据（Symbol / Region / Market / Currency）
----------------------------------------------------------------------------------------
- A 股（market="A"）：Market/Symbol 前缀按代码判定交易所：
  5/6/9 开头 → SH，其余（0/2/3 等）→ SZ；Region=CN，Currency=CNY；
- 其他市场：按 MARKET_META 表取 Market/Region/Currency，Symbol = {Market}.{code}；
  表内取值是与用户确认的约定，**调整须经用户同意**；
- market 未知 / 未传：Symbol={code}，Region/Market/Currency 留空（宁空勿猜）。

四、使用示例（同目录脚本）
----------------------------------------------------------------------------------------
    from MvsvQuoteBuilder import buildMvsvContent, buildMvsvFileName

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
# A 股走 resolveCnExchange 单独处理；其他市场按 Config.json 的 market 取值查表。
MARKET_META = {
    "HK":      {"market": "HK",      "region": "HK", "currency": "HKD"},
    "US":      {"market": "US",      "region": "US", "currency": "USD"},
    "FX":      {"market": "FX",      "region": "",   "currency": "USD"},
    "FUTURES": {"market": "FUTURES", "region": "US", "currency": "USD"},
    "CRYPTO":  {"market": "CRYPTO",  "region": "",   "currency": "USD"},
}
# A 股判定为 SH 的代码首字符（60/68 主板科创、5x ETF、9x B 股等）
CN_SH_PREFIXES = ("5", "6", "9")


def resolveCnExchange(secu_code):
    """按 A 股代码判定交易所：5/6/9 开头 → SH，其余 → SZ

    :param secu_code: 6 位 A 股代码（如 600570 / 000985 / 159934）
    :return: "SH" 或 "SZ"
    """
    return "SH" if str(secu_code).startswith(CN_SH_PREFIXES) else "SZ"


def resolveMarketMeta(secu_code, market=None):
    """解析 V5 头部的市场元数据（Symbol / Region / Market / Currency）

    :param secu_code: 证券代码
    :param market: Config.json 中的市场标识（A / HK / US / FX / FUTURES / CRYPTO）；
                   None 或未知时保守留空（宁空勿猜）
    :return: {"symbol","region","market","currency"} 四键 dict
    """
    m = (market or "").strip().upper()
    if m == "A":
        exch = resolveCnExchange(secu_code)
        return {"symbol": "%s.%s" % (exch, secu_code),
                "region": "CN", "market": exch, "currency": "CNY"}
    info = MARKET_META.get(m)
    if info is None:
        return {"symbol": str(secu_code), "region": "", "market": "", "currency": ""}
    return {"symbol": "%s.%s" % (info["market"], secu_code),
            "region": info["region"], "market": info["market"],
            "currency": info["currency"]}


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


def buildMvsvContent(secu_code, minute_list, market=None, name="", period="Min"):
    """构建 V5 格式 MVSV 文件完整文本（头部 16 行 + 空行 + 数据行，以 \\n 结尾）

    :param secu_code: 证券代码（如 000985）
    :param minute_list: 分钟数据行列表（数据源原始 dict 列表，含 ts/c/v/t/cr/cp 键）
    :param market: Config.json 的市场标识（决定 Symbol/Region/Market/Currency）
    :param name: 证券名称（暂无数据源，恒为空）
    :param period: 数据周期标识，默认 Min（写入 # TypeKLine 与标题）
    :return: 文件完整文本（UTF-8、LF、以换行结束）
    """
    meta = resolveMarketMeta(secu_code, market)
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
        "# USC : %s" % secu_code,
        "# Name : %s" % name,
        "# Region : %s" % meta["region"],
        "# Market : %s" % meta["market"],
        "# TimeZone : ",
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
