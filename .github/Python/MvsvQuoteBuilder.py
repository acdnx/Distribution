#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MvsvQuoteBuilder —— 分钟级行情 MVSV 文件生成契约库（纯标准库，无第三方依赖）
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
把「生成 .mvsv 行情文件」的通用契约集中到一个文件统一维护，供同目录下各采集脚本
（当前为 FinvQuoteCollectPollFtmm.py，后续可扩展其他采集器）import 复用，避免
每个脚本各自复制一份格式逻辑、改格式时多处漂移。

形态说明：
    - 本文件是纯函数库，没有命令行入口；通过同目录其他 Python 脚本 import 调用；
    - import 时不发起任何网络请求、不读环境变量、无全局副作用；
    - 公开函数：buildMvsvContent（文件内容）、buildMvsvFileName（文件命名）、
      extractSummary（汇总字段提取）；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，模块私有实现保持 snake_case；
    - 纯标准库实现，可直接拷到任何 Python 3.8+ 环境使用。

二、MVSV 文件契约（与旧版 Poll.py 完全一致，下游解析方以此为契约）
----------------------------------------------------------------------------------------
1. 文件头（中英文双语，# 前缀注释行，顺序固定）：
       # 标题 / # 数据供应商 / # 字段 / # 字段名称 / # 字段类型 /
       # 计数 / # 采集时间 / # 证券代码 / [# 备注(可选汇总)] /
       # Title / # DataProvider / # Field / # FieldName / # FieldType /
       # Count / # FetchTime / # SecuCode / 空行
2. 字段布局：ts|c|v|t|r|cp（时间戳(UTC)|收盘价|成交量|成交额|涨跌幅(%)|涨跌值）；
   数据行按数据源字段名取值拼接：ts|c|v|t|cr|cp（cr 对应文件头里的 r）。
3. 数据供应商固定为 FT。
4. 可选汇总行（# 备注 / 英文块无对应行）：由 extractSummary 从行情 API 的
   data 节点提取 c/h/l/o/cnt 五个键，缺失键自动跳过。
5. 文件命名：{code}_{period}_{ts_suffix}.mvsv（period 默认 Min；
   ts_suffix 形如 20260924_223000，由调用方按北京时间生成）。

三、使用示例（同目录脚本）
----------------------------------------------------------------------------------------
    from MvsvQuoteBuilder import buildMvsvContent, buildMvsvFileName, extractSummary

    summary = extractSummary(raw["data"])                       # {c,h,l,o,cnt}
    content = buildMvsvContent(code, minute_list, summary, fetch_time)
    file_name = buildMvsvFileName(code, "20260924_223000")    # HSI_Min_20260924_223000.mvsv

【环境要求】Python 3.8+，仅标准库。
"""

# ============ MVSV 契约常量 ============
# 数据供应商（文件头 # 数据供应商 / # DataProvider）
PROVIDER = "FT"
# 字段布局（文件头 # 字段 / # Field 与 # 字段类型 / # FieldType 共用同一顺序）
FIELD_LAYOUT = "ts|c|v|t|r|cp"
FIELD_NAMES_CN = "时间戳(UTC)|收盘价|成交量|成交额|涨跌幅(%)|涨跌值"
FIELD_NAMES_EN = "Ts|Close|Volume|Turnover|ChangePct|ChangePrice"
FIELD_TYPES = "int|float|int|float|str|float"
# 汇总字段键序（# 备注 行按此顺序拼接，缺失键跳过）
SUMMARY_KEYS = ("c", "h", "l", "o", "cnt")
# 数据行字段名（数据源 JSON 键；cr 对应文件头字段 r）
DATA_ROW_KEYS = ("ts", "c", "v", "t", "cr", "cp")


def extractSummary(data_node):
    """从行情 API 的 data 节点提取汇总字段

    :param data_node: 行情 API 响应 JSON 的 data 节点（dict）
    :return: {"c","h","l","o","cnt"} 五键 dict，缺失键为 None
             （buildMvsvContent 生成 # 备注 行时自动跳过 None）
    """
    return {k: data_node.get(k) for k in SUMMARY_KEYS}


def buildMvsvFileName(secu_code, ts_suffix, period="Min"):
    """生成 MVSV 文件名：{code}_{period}_{ts_suffix}.mvsv

    :param secu_code: 证券代码（如 HSI）
    :param ts_suffix: 时间戳后缀（形如 20260924_223000，调用方按北京时间生成）
    :param period: 数据周期标识，默认 Min（分钟级）
    :return: 文件名字符串（不含目录），如 HSI_Min_20260924_223000.mvsv
    """
    return "%s_%s_%s.mvsv" % (secu_code, period, ts_suffix)


def buildMvsvContent(secu_code, minute_list, data_summary, fetch_time):
    """构建 MVSV 文件完整文本内容（与旧版 Poll.py 的输出逐字节一致）

    :param secu_code: 证券代码（如 HSI）
    :param minute_list: 分钟数据行列表，每行为含 ts/c/v/t/cr/cp 键的 dict
    :param data_summary: 汇总 dict（extractSummary 的输出；可为 None/空 dict，
                         此时不生成 # 备注 行）
    :param fetch_time: 采集时间字符串（如 "2026-09-24 22:30:00"，北京时间）
    :return: 完整 MVSV 文本（行尾 \n 拼接，末行为最后一条数据行）
    """
    lines = [
        '# 标题 : "%s 分钟级行情数据"' % secu_code,
        "# 数据供应商 : %s" % PROVIDER,
        "# 字段 : %s" % FIELD_LAYOUT,
        "# 字段名称 : %s" % FIELD_NAMES_CN,
        "# 字段类型 : %s" % FIELD_TYPES,
        "# 计数 : %d" % len(minute_list),
        '# 采集时间 : "%s"' % fetch_time,
        "# 证券代码 : %s" % secu_code,
    ]
    if data_summary:
        parts = ["%s=%s" % (k, data_summary[k])
                 for k in SUMMARY_KEYS
                 if k in data_summary and data_summary[k] is not None]
        if parts:
            lines.append('# 备注 : "汇总: %s"' % "|".join(parts))
    lines += [
        '# Title : "%s Minute Quote Data"' % secu_code,
        "# DataProvider : %s" % PROVIDER,
        "# Field : %s" % FIELD_LAYOUT,
        "# FieldName : %s" % FIELD_NAMES_EN,
        "# FieldType : %s" % FIELD_TYPES,
        "# Count : %d" % len(minute_list),
        '# FetchTime : "%s"' % fetch_time,
        "# SecuCode : %s" % secu_code,
        "",
    ]
    for item in minute_list:
        lines.append("|".join("%s" % item.get(k, "") for k in DATA_ROW_KEYS))
    return "\n".join(lines)


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("MvsvQuoteBuilder 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from MvsvQuoteBuilder import buildMvsvContent, buildMvsvFileName, extractSummary")
