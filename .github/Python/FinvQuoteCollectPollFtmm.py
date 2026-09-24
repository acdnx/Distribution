#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

# 本脚本与下列同目录模块一起工作，显式加入搜索路径以保证任意 cwd 下可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# GitHubCommitContent 只使用其公开递交能力；HTTP 收发走 HttpUtil、控制台编码走 ConsoleUtil
from GitHubCommitContent import commit_content
from ConsoleUtil import ensureConsoleUtf8
from MvsvQuoteBuilder import (CN_EXCHANGES, buildMvsvContent,
                              buildMvsvFileName)
from FtmmQuoteV2WebRestClient import (ENV_API_BASE, extractMinuteList,
                                      fetchFiveDayMinuteQuote, isApiBaseUsable,
                                      maskApiUrl, resolveApiBase)
from DateTimeUtil import (fmtDateSuffix, fmtDisplay, fmtTsSuffix, isUsDst, nowBeijing,
                           parseDt, shiftDays, toEpochSeconds, utcNow)
from SupabaseRestClient import SupabaseRestClient, SupabaseRestError, eqFilter

# ============ 数据落点常量（按需求锁定：ACANX/Distribution @ quote）============
# 刻意不走 Commit.json / .git 解析（那里登记的是 acdnx/Distribution 转存端）。
GITHUB_OWNER = "ACANX"
GITHUB_REPO = "Distribution"
GITHUB_BRANCH = "quote"
DATA_DIR = "Data/Finv/SecurityQuoteV5/FTMM/Min"          # MVSV 数据文件落点目录
LOG_DIR = "Data/Finv/SecurityQuoteV5/ExecLog"    # 执行日志落点目录

# ============ 调度常量 ============
# 单次运行采集的标的数量。按需求写死在此，后续如需调整直接改这里。
POLL_COUNT = 3
FORCE_FETCH_INTERVAL = 72 * 3600          # 距上次检查超过 72 小时强制采集（秒）

# ============ 环境变量 ============
# Supabase 凭据由 SupabaseRestClient 从环境变量读取（不在此处留存副本）
GIT_COMMIT_TOKEN = os.environ.get("GIT_COMMIT_TOKEN", "").strip()
# 状态读取源 = 视图（列口径见 STATE_VIEW_COLUMNS；视图直出 region/market/timezone/
# symbol/name_sc/weight_priority/weight_frequency，故本作业**不再依赖任何外部配置文件**）
STATE_VIEW = os.environ.get("POLL_STATE_VIEW",
                            "finv_quote_collect_state_poll_futu_view").strip()
# 状态回写目标 = 表（视图只读，更新一律落表）
STATE_TABLE = os.environ.get("POLL_STATE_TABLE", "finv_quote_collect_state_poll_futu").strip()
# 行身份列：2026-09-25 起表主键由 secu_code 改为 usc（视图与表同名同值）
STATE_KEY = "usc"
# 视图查询的列清单——**必须与视图定义严格对齐**（PostgREST 对不存在的列直接 400）。
# 已知视图定义（security_invoker）已自行筛选：z/a/b 三表 flag_enable='1'
# 且 b.provider='FTMM'，故：
#   · 不查 flag_enable（视图未暴露该列），也**不要**在客户端拼该过滤条件；
#   · 取到的行天然都是启用中的 FTMM 品种，无需再按 provider 过滤。
STATE_VIEW_COLUMNS = ("usc,name_sc,region,market,timezone,provider,symbol,futu_symbol,"
                      "dt_last_check,dt_last_fetch,ts_latest_data,count_last_fetch,"
                      "count_fail,count_stale,weight_priority,weight_frequency")
# 执行日志里记录视图自带的筛选口径（自证：为什么日志里没有 flag_enable 过滤）
STATE_VIEW_FILTER = "视图定义：flag_enable=1（z/a/b 三表）且 provider=FTMM"
DRY_RUN = os.environ.get("POLL_DRY_RUN", "").strip().lower() in ("true", "1", "yes", "on")
TEMP_DIR = Path(os.environ.get("POLL_TEMP_DIR", "")) if os.environ.get("POLL_TEMP_DIR", "").strip() \
    else Path(tempfile.gettempdir()) / "finv_quote_collect_poll_ftmm"

# 执行日志聚合（运行结束时整体上传；密钥类信息一律不进这里）
EXEC_LOG: Dict[str, object] = {
    "start_time": None,
    "dry_run": DRY_RUN,
    "state_backend": "supabase_pg",
    "state_view": STATE_VIEW,
    "state_table": STATE_TABLE,
    "state_view_filter": STATE_VIEW_FILTER,
    "poll_count": POLL_COUNT,
    "selected": [],
    "results": [],
    "errors": [],
}


# ============ 状态读写（**读视图、写表**，PostgREST 访问见 SupabaseRestClient.py） ============
def _to_int(value, default=0):
    """宽松转 int（PG 返回值可能为 None / 字符串）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_weight(value, default=1):
    """宽松转调度权重（沿用旧 Config.json 的口径：1..10000 之外一律视为无效，回落 default）

    视图的 weight_priority / weight_frequency 取自状态表 z 列，理论上已受表约束；
    这里仍按旧口径夹一道，避免脏值把 score 打成 0 或负数导致品种长期选不中。
    """
    try:
        weight = int(value)
    except (TypeError, ValueError):
        return default
    return weight if 1 <= weight <= 10000 else default


def _one_line(text):
    """把文案压成单行并截断（PostgREST 的错误响应可能多行且很长）"""
    return " ".join(str(text).split())[:200]


def _scrub(text):
    """把文本中出现的行情端点替换为打码形态

    异常文案可能夹带完整 URL（如 urllib 的 unknown url type: 'https://...'），
    而执行日志是要上传到 quote 分支的，一旦落库即持久外泄，故写入前统一擦除端点。

    :param text: 待写入日志/执行日志的文本
    :return: 端点已打码的文本
    """
    try:
        base = resolveApiBase()
    except Exception:
        return text
    return text.replace(base, maskApiUrl(base)) if isApiBaseUsable(base) else text


def load_state(client):
    """读取状态视图（读视图、写表）：返回 (usc 列表, 状态映射)

    读取源为视图 STATE_VIEW（finv_quote_collect_state_poll_futu_view）——它已把
    region / market / timezone / symbol 等元数据、weight_priority / weight_frequency
    两个调度权重、以及各状态字段拼在一行，故**本作业不再依赖任何外部配置文件**
    （Config.json 已退出）；行身份为 STATE_KEY = usc（2026-09-25 起表主键由
    secu_code 改为 usc）。

    采集请求代码即 **usc**（它正是原 secu_code 的重命名，与旧版请求口径一致）；
    视图另有 futu_symbol（数据源侧代码）与 symbol（= quote_market.futu_symbol），
    本脚本不使用，如后续需改用 futu_symbol 请先与用户确认。

    **不拼 flag_enable 过滤**：视图定义（security_invoker）已自行筛掉未启用品种
    （z/a/b 三表 flag_enable='1' 且 provider='FTMM'），且该列并不在视图暴露的列里，
    客户端硬拼会直接 400。

    状态映射每项含三组键：
      · 头部元数据（视图直出）：usc / name / region / market / timezone / symbol
      · 调度权重（视图直出）：weight_base（← weight_priority）/ weight_freq（← weight_frequency）
      · 状态字段（回写表用）：time_last_check / time_last_fetch / ts_latest_data /
        count_last_fetch / count_fail / count_stale
    """
    query_string = "select=%s&order=%s.asc" % (STATE_VIEW_COLUMNS, STATE_KEY)
    text = client.query(STATE_VIEW, query_string)
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise SupabaseRestError("状态视图查询响应不是 JSON 数组")

    usc_list: List[str] = []
    state_map: Dict[str, Dict[str, Union[str, int]]] = {}
    for row in rows:
        usc = str(row.get(STATE_KEY) or "").strip()
        if not usc:
            continue
        usc_list.append(usc)
        state_map[usc] = {
            # —— MVSV 头部元数据（视图直出；name ← name_sc，同时用于日志与 # Name） ——
            "usc": usc,
            "name": str(row.get("name_sc") or "").strip(),
            "region": str(row.get("region") or "").strip(),
            "market": str(row.get("market") or "").strip(),
            "timezone": str(row.get("timezone") or "").strip(),
            "symbol": str(row.get("symbol") or "").strip(),
            # —— 调度权重（视图直出；列名对应关系见 docstring） ——
            "weight_base": _to_weight(row.get("weight_priority")),
            "weight_freq": _to_weight(row.get("weight_frequency")),
            # —— 状态字段（与视图列一一对应） ——
            "time_last_check": row.get("dt_last_check") or "",
            "time_last_fetch": row.get("dt_last_fetch") or "",
            "ts_latest_data": _to_int(row.get("ts_latest_data")),
            "count_last_fetch": _to_int(row.get("count_last_fetch")),
            "count_fail": _to_int(row.get("count_fail")),
            "count_stale": _to_int(row.get("count_stale")),
        }
    print("[状态] 从视图 %s 加载成功：%d 个品种（筛选口径由视图定义负责）"
          % (STATE_VIEW, len(usc_list)))
    return usc_list, state_map


def patch_state_row(client, usc, state):
    """把单品种状态回写**表** STATE_TABLE（视图只读；dt_update 由表上触发器维护）

    过滤键为 STATE_KEY（usc，表主键）。时间列为空（从未采集/从未取到新数据）时
    不出现在载荷里，保持 NULL 原状；计数列强制夹非负，满足表上的 chk_*_nonnegative 约束。
    """
    payload = {}
    if state.get("time_last_check"):
        payload["dt_last_check"] = state["time_last_check"]
    if state.get("time_last_fetch"):
        payload["dt_last_fetch"] = state["time_last_fetch"]
    payload["ts_latest_data"] = max(0, _to_int(state.get("ts_latest_data")))
    payload["count_last_fetch"] = max(0, _to_int(state.get("count_last_fetch")))
    payload["count_fail"] = max(0, _to_int(state.get("count_fail")))
    payload["count_stale"] = max(0, _to_int(state.get("count_stale")))

    filters = eqFilter(STATE_KEY, usc)
    text = client.patch(STATE_TABLE, filters, payload)
    try:
        hit = len(json.loads(text)) if (text or "").strip() else 0
    except ValueError:
        hit = -1
    if hit == 0:
        print("⚠️ [状态] %s 回写未命中任何行（品种可能已被移除）" % usc)
    else:
        print("[状态] %s 回写成功（%s）：%s"
              % (usc, STATE_TABLE, json.dumps(payload, ensure_ascii=False)))


# ============ 分市场时段权重（沿用旧版，now 一律为北京时间） ============
# isUsDst 为纯日历规则，已抽至 DateTimeUtil.py
def normalize_market(market):
    """把市场标识归一为「时段判定口径」：交易所级的 SH/SZ/BJ 归为 A 股整体

    状态视图的 market 是**交易所级**（SH/SZ/BJ），而 A 股三所的交易日时段一致，
    故统一按 "A" 判定；其余市场标识与大写形态保持一致。

    :param market: 市场标识（视图口径）
    :return: 归一后的标识（大写）；空值返回 ""（由调用方决定兜底）
    """
    key = str(market or "").strip().upper()
    return "A" if key in CN_EXCHANGES else key


def get_market_weight(market, now):
    """分市场时段权重（与旧版一致；未知市场/周末取低权重，中性兜底 5.0）

    :param market: 市场标识，可传交易所级（SH/SZ/BJ，会先归一到 A 股口径）
    """
    market = normalize_market(market)
    weekday = now.weekday()
    is_weekend = weekday >= 5
    if market == "A":
        if is_weekend:
            return 0.1
        m_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        m_end = now.replace(hour=11, minute=30, second=0, microsecond=0)
        a_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
        a_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
        ext_end = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if (m_start <= now <= m_end) or (a_start <= now <= a_end):
            return 10.0
        if m_start <= now <= ext_end:
            return 5.0
        return 1.0
    if market == "HK":
        if is_weekend:
            return 0.1
        m_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        m_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        a_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
        a_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
        ext_end = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if (m_start <= now <= m_end) or (a_start <= now <= a_end):
            return 10.0
        if m_start <= now <= ext_end:
            return 5.0
        return 1.0
    if market == "US":
        if is_weekend:
            return 0.1
        dst = isUsDst(now)
        if dst:
            trade_start = now.replace(hour=21, minute=30, second=0, microsecond=0)
            trade_end = now.replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=1)
            ext_end = now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            trade_start = now.replace(hour=22, minute=30, second=0, microsecond=0)
            trade_end = now.replace(hour=5, minute=0, second=0, microsecond=0) + timedelta(days=1)
            ext_end = now.replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(days=1)
        if now >= trade_start or now <= trade_end:
            return 10.0
        if now >= trade_start or now <= ext_end:
            return 5.0
        return 1.0
    if market in ("FX", "FUTURE"):
        return 1 if is_weekend else 8.0
    if market == "CRYPTO":
        return 8.0
    return 5.0


# ============ 智能调度（指数退避 + 无新数据冷却 + 72h 强制采集） ============
def resolve_weight_market(usc, state_map):
    """取该品种用于**时段权重**的市场标识：直接取状态视图的 market

    2026-09-25 起权重与市场都由视图直出（Config.json 已退出），视图的 market 是
    交易所级（SH/SZ/BJ），由 get_market_weight 内部归一为 A 股口径。

    :return: 市场标识；视图没给则返回 "UNKNOWN"（中性权重 5.0 兜底）
    """
    return (state_map.get(usc) or {}).get("market") or "UNKNOWN"


def select_best_codes(usc_list, state_map, now, count):
    """从状态视图取到的品种中选出本轮要采集的至多 count 个

    usc_list 为视图行身份（usc）。规则与旧版一致：
      1) 强制候选（从未采集 / 时间解析失败 / 距上次检查超 72h）按超时长度降序优先；
      2) 强制候选不足 count 时，其余品种按
         score = elapsed × 市场权重 × (base_w × freq_w) / 2^(fail+stale)
         降序补足（惩罚因子 = 2^(count_fail + count_stale)）。
    其中 base_w / freq_w 取自视图的 weight_priority / weight_frequency（旧版取自
    Config.json，口径不变）；缺值按 1 处理。

    :return: [{"usc","score","reason","reason_detail"}, ...]（强制优先，得分降序）
    """
    # ---------- 强制采集 ----------
    force_candidates = []
    for usc in usc_list:
        state = state_map.get(usc)
        lct_str = state.get("time_last_check") if state else None
        if not lct_str:
            force_candidates.append((usc, float("inf"), "从未采集"))
            continue
        lct_dt = parseDt(lct_str)
        if lct_dt is None:
            force_candidates.append((usc, float("inf"), "时间解析失败"))
            continue
        elapsed_check = (now - lct_dt).total_seconds()
        if elapsed_check > FORCE_FETCH_INTERVAL:
            force_candidates.append(
                (usc, elapsed_check, "距上次检查%.1f小时" % (elapsed_check / 3600)))
    force_candidates.sort(key=lambda x: x[1], reverse=True)
    selected = [{"usc": c, "score": 0.0, "reason": "force",
                 "reason_detail": "强制采集 (超过72小时未检查) - %s" % info}
                for c, _e, info in force_candidates]
    if len(selected) >= count:
        for s in selected[:count]:
            print("[调度] 强制采集: %s, %s" % (s["usc"], s["reason_detail"]))
        return selected[:count]

    # ---------- 收益最大化（指数退避 + 无新数据冷却） ----------
    remaining = count - len(selected)
    scored = []
    log_lines = [
        "  {'USC':<6s} {'elapsed':>10s} {'market_w':>8s} {'base_w':>6s} {'freq_w':>6s} "
        "{'static_w':>8s} {'fail':>4s} {'stale':>5s} {'penalty':>8s} {'score':>12s} {'REASON':<20s}"
    ]
    for usc in usc_list:
        if any(s["usc"] == usc for s in selected):
            continue
        state = state_map.get(usc)
        last_new_str = state.get("time_last_fetch") if state else None
        count_fail = state.get("count_fail", 0) if state else 0
        count_stale = state.get("count_stale", 0) if state else 0
        # 从未采集 / 解析失败 → 按 30 天前计（旧版口径）
        last_dt = parseDt(last_new_str, default=shiftDays(now, -30))

        elapsed = (now - last_dt).total_seconds()
        market_weight = get_market_weight(resolve_weight_market(usc, state_map), now)
        base_w = _to_weight(state.get("weight_base") if state else None)
        freq_w = _to_weight(state.get("weight_freq") if state else None)
        total_static_weight = base_w * freq_w
        penalty = 2 ** (count_fail + count_stale)
        score = elapsed * market_weight * total_static_weight / penalty

        reasons = []
        if elapsed > 3600:
            reasons.append("long_elapsed")
        if market_weight > 5:
            reasons.append("high_market")
        if base_w > 1:
            reasons.append("high_base")
        if freq_w > 1:
            reasons.append("high_freq")
        if count_fail > 0:
            reasons.append("fail_x%d" % count_fail)
        if count_stale > 0:
            reasons.append("stale_x%d" % count_stale)
        if not reasons:
            reasons.append("default")

        log_lines.append(
            "  %-6s %10.0f %8.1f %6d %6d %8d %4d %5d %8d %12.1f %-20s"
            % (usc, elapsed, market_weight, base_w, freq_w, total_static_weight,
               count_fail, count_stale, penalty, score, ",".join(reasons)))
        scored.append({"usc": usc, "score": score, "reason": "score",
                       "comp": {"elapsed": elapsed, "market_weight": market_weight,
                                "base_w": base_w, "freq_w": freq_w,
                                "count_fail": count_fail, "count_stale": count_stale}})

    print("[调度] 各品种分数:")
    for line in log_lines:
        print(line)

    scored.sort(key=lambda x: x["score"], reverse=True)
    picked = scored[:remaining]
    for s in picked:
        comp = s["comp"]
        parts = []
        if comp["elapsed"] > 3600:
            parts.append("距上次新数据%.1f小时" % (comp["elapsed"] / 3600))
        if comp["market_weight"] > 5:
            parts.append("市场时段活跃(权重%.0f)" % comp["market_weight"])
        if comp["base_w"] > 1:
            parts.append("基础权重x%d" % comp["base_w"])
        if comp["freq_w"] > 1:
            parts.append("频率权重x%d" % comp["freq_w"])
        if comp["count_fail"] > 0:
            parts.append("失败%d次" % comp["count_fail"])
        if comp["count_stale"] > 0:
            parts.append("无新数据%d次" % comp["count_stale"])
        if not parts:
            parts.append("无特殊优势，默认选择")
        s["reason_detail"] = "收益最大 - %s" % ", ".join(parts)
        print("[调度] 选中品种: %s, 得分: %.1f, 原因: %s"
              % (s["usc"], s["score"], s["reason_detail"]))
    selected.extend(picked)
    return selected


# ============ 行情采集与文件生成（契约分别见 FtmmQuoteV2WebRestClient.py / MvsvQuoteBuilder.py） ============
def collect_single(usc, meta):
    """采集单个品种：行情 API → MVSV 落盘；成功返回摘要 dict，失败返回 None

    文件头部的 Symbol / Name / Region / Market / TimeZone 直接取状态视图的元数据
    （2026-09-25 起不再依赖 Config.json 关联）；请求代码即 usc（原 secu_code）。

    :param usc: 品种身份（视图主键，同时写入 # SecuCode / # USC 与落点目录）
    :param meta: load_state 给出的视图元数据（name/region/market/timezone/symbol）
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    now = nowBeijing()
    # 文件按「一天一个」命名（000985_Min_20260820.mvsv），同一天的重复运行覆盖当日文件
    date_suffix = fmtDateSuffix(now)
    fetch_time = fmtDisplay(now)
    print("[采集] 开始处理品种: %s（名称: %s），北京时间 %s"
          % (usc, meta.get("name") or "无名称", fetch_time))
    raw = fetchFiveDayMinuteQuote(usc)
    if not raw:
        return None
    minute_list = extractMinuteList(raw)
    if not minute_list:
        print("[采集] %s 数据列表为空" % usc)
        return None
    latest_ts = max(item.get("ts", 0) for item in minute_list) if minute_list else 0
    # V5 文件契约见 MvsvQuoteBuilder.py：头部 16 行 + 数据行；
    # 市场元数据（Region/Market/TimeZone/Symbol）为视图直出，本脚本不再做任何推断
    content = buildMvsvContent(usc, minute_list, market=meta.get("market"),
                               region=meta.get("region"),
                               timezone=meta.get("timezone"),
                               symbol=meta.get("symbol"), usc=usc,
                               name=meta.get("name"))
    file_name = buildMvsvFileName(usc, date_suffix)
    file_path = TEMP_DIR / file_name
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[采集] 文件保存成功: %s" % file_path)
    return {
        "usc": usc,
        "file_path": str(file_path),
        "content": content,
        "fetch_time": fetch_time,
        "record_count": len(minute_list),
        "ts_latest_data": latest_ts,
    }


# ============ 文件递交（GitHubCommitContent 直连 Contents API） ============
def upload_to_repo(content, path_key, commit_msg):
    """把文本内容递交到常量锁定的目标仓库（ACANX/Distribution @ quote）"""
    print("[上传] 提交文件: %s/%s@%s %s"
          % (GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH, path_key))
    result = commit_content(path_key, content, branch=GITHUB_BRANCH,
                            commit_msg=commit_msg, owner=GITHUB_OWNER, repo=GITHUB_REPO)
    if result["success"]:
        url = "https://github.com/%s/%s/blob/%s/%s" % (GITHUB_OWNER, GITHUB_REPO,
                                                       GITHUB_BRANCH, path_key)
        print("[上传] 成功 (HTTP %s): %s" % (result["http_status"], url))
        return {"success": True, "url": url, "message": None}
    print("[上传] 失败: %s" % result.get("message"))
    return {"success": False, "url": None, "message": result.get("message")}


# ============ 单品种轮询（采集 → 状态机流转 → 回写） ============
def poll_one(client, sel, state_map, now, dry_run):
    """处理一个选中品种：状态机流转与旧版 handle_poll 完全一致

    有新数据 → 重置 fail/stale、推进 fetch 指针；无新数据 → stale+1、fail 清零；
    采集失败 → fail+1。任何分支 dt_last_check 都推进到本次运行时刻。
    回写走表（STATE_TABLE），过滤键为 usc。
    """
    usc = sel["usc"]
    state = state_map.setdefault(usc, {
        "time_last_check": "", "time_last_fetch": "",
        "ts_latest_data": 0, "count_last_fetch": 0,
        "count_fail": 0, "count_stale": 0,
    })
    state["time_last_check"] = now.isoformat()

    outcome = {"usc": usc, "reason": sel["reason"],
               "reason_detail": sel["reason_detail"],
               "poll_success": False, "upload_success": None,
               "record_count": 0, "error": None}

    result = collect_single(usc, state)
    outcome["poll_success"] = result is not None

    if result:
        outcome["record_count"] = result["record_count"]
        new_ts = result["ts_latest_data"]
        old_ts = _to_int(state.get("ts_latest_data"))
        if new_ts > old_ts:
            state["count_fail"] = 0
            state["count_stale"] = 0
            state["time_last_fetch"] = now.isoformat()
            state["ts_latest_data"] = new_ts
            state["count_last_fetch"] = result["record_count"]
            print("[状态] %s 有新数据，ts_latest_data=%d" % (usc, new_ts))
        else:
            state["count_stale"] = _to_int(state.get("count_stale")) + 1
            state["count_fail"] = 0
            print("[状态] %s 无新数据，count_stale=%d" % (usc, state["count_stale"]))

        if dry_run:
            print("[dry_run] 跳过文件递交：%s" % usc)
        else:
            fname = Path(result["file_path"]).name
            path_key = "%s/%s/%s" % (DATA_DIR, usc, fname)
            commit_msg = "[FinvQuoteCollectPollFtmm] %s minute quote data @%s" % (usc, result["fetch_time"])
            up = upload_to_repo(result["content"], path_key, commit_msg)
            outcome["upload_success"] = up["success"]
            if not up["success"]:
                outcome["error"] = "递交失败: %s" % up.get("message")
                EXEC_LOG["errors"].append("%s 递交失败: %s" % (usc, up.get("message")))
    else:
        state["count_fail"] = _to_int(state.get("count_fail")) + 1
        outcome["error"] = "行情采集失败"
        print("[状态] %s 采集失败，count_fail=%d" % (usc, state["count_fail"]))

    if dry_run:
        print("[dry_run] 跳过状态回写：%s → %s" % (usc, json.dumps(state, ensure_ascii=False)))
    else:
        patch_state_row(client, usc, state)
    return outcome


# ============ 执行日志上传 ============
def upload_exec_log():
    """把本次执行日志上传到目标仓库（dry_run / 缺令牌时跳过）"""
    if DRY_RUN:
        print("[执行日志] dry_run 模式，跳过上传")
        return
    if not GIT_COMMIT_TOKEN:
        print("[执行日志] 缺少 GIT_COMMIT_TOKEN，跳过上传")
        return
    utc_dt = utcNow()
    bj_dt = nowBeijing()
    log_name = fmtTsSuffix(bj_dt) + ".log"
    path_key = "%s/%s" % (LOG_DIR, log_name)
    entry = {
        "ts": toEpochSeconds(utc_dt),
        "dt": fmtDisplay(bj_dt),
        "state_backend": EXEC_LOG["state_backend"],
        "state_view": EXEC_LOG["state_view"],
        "state_table": EXEC_LOG["state_table"],
        "state_view_filter": EXEC_LOG["state_view_filter"],
        "poll_count": EXEC_LOG["poll_count"],
        "dry_run": EXEC_LOG["dry_run"],
        "start_time": EXEC_LOG["start_time"],
        "selected": EXEC_LOG["selected"],
        "results": EXEC_LOG["results"],
        "errors": EXEC_LOG["errors"],
    }
    content = json.dumps(entry, indent=2, ensure_ascii=False)
    print("[执行日志] 上传日志到 %s/%s@%s %s"
          % (GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH, path_key))
    result = upload_to_repo(content, path_key, "[FinvQuoteCollectPollFtmm] exec log %s" % log_name)
    if not result["success"]:
        print("[执行日志] 上传失败: %s" % result.get("message"))


# ============ 主流程 ============
def main():
    ensureConsoleUtf8()
    EXEC_LOG["start_time"] = utcNow().isoformat()
    print("======== FinvQuoteCollectPollFtmm 轮询开始 ========")
    print("模式: %s | 状态视图: %s | 状态表: %s | POLL_COUNT=%d"
          % ("dry_run 演练" if DRY_RUN else "正常", STATE_VIEW, STATE_TABLE, POLL_COUNT))
    print("视图筛选口径: %s（客户端不再另加过滤）" % STATE_VIEW_FILTER)
    print("数据落点: %s/%s @ %s（脚本内常量锁定）" % (GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH))

    # 凭据：Supabase 由客户端从环境变量解析（缺失抛 ValueError，不打印取值）
    if not DRY_RUN and not GIT_COMMIT_TOKEN:
        print("❌ 缺少 GIT_COMMIT_TOKEN 环境变量（数据递交需要；dry_run 模式可缺）")
        return 1

    try:
        client = SupabaseRestClient.fromEnv()
    except ValueError as e:
        print("❌ Supabase 凭据缺失: %s" % e)
        return 1

    # 行情端点自检：未配置就在这里失败，避免白跑一轮才在采集阶段炸
    # （端点值可能含机密，只报「已配置/未配置」，不打印取值）
    api_base = resolveApiBase()
    if not isApiBaseUsable(api_base):
        print("❌ 行情 API 端点未配置或非法：请在仓库 secrets/vars 配置 %s"
              "（值为完整 http(s) 端点）；当前解析结果不是合法 URL" % ENV_API_BASE)
        return 1
    print("行情端点: 已配置（环境变量 %s，值不打印）" % ENV_API_BASE)

    try:
        usc_list, state_map = load_state(client)
    except Exception as e:
        err = _one_line(e)          # PostgREST 的错误响应可能多行且很长，压成单行再落日志
        print("❌ 状态视图读取失败: %s" % err)
        EXEC_LOG["errors"].append("state_load: %s" % err)
        return 1

    if not usc_list:
        print("状态视图 %s 中没有可采集的品种，无事可做" % STATE_VIEW)
        return 0

    now = nowBeijing()
    print("[轮询] 当前北京时间: %s, 品种数: %d" % (now.isoformat(), len(usc_list)))

    selected = select_best_codes(usc_list, state_map, now, POLL_COUNT)
    EXEC_LOG["selected"] = [{"usc": s["usc"], "reason": s["reason"],
                             "reason_detail": s["reason_detail"]} for s in selected]

    any_fail = False
    for sel in selected:
        try:
            outcome = poll_one(client, sel, state_map, now, DRY_RUN)
        except Exception as e:
            err = _scrub("%s" % e)
            outcome = {"usc": sel["usc"], "reason": sel["reason"],
                       "reason_detail": sel["reason_detail"],
                       "poll_success": False, "upload_success": None,
                       "record_count": 0, "error": "处理异常: %s" % err}
            EXEC_LOG["errors"].append("%s: %s" % (sel["usc"], err))
            print("❌ [轮询] %s 处理异常: %s" % (sel["usc"], err))
        EXEC_LOG["results"].append(outcome)
        if (not outcome.get("poll_success")) or outcome.get("upload_success") is False:
            any_fail = True

    poll_ok = sum(1 for r in EXEC_LOG["results"] if r.get("poll_success"))
    upload_ok = sum(1 for r in EXEC_LOG["results"] if r.get("upload_success") is True)
    upload_skip = sum(1 for r in EXEC_LOG["results"] if r.get("upload_success") is None)
    print("======== FinvQuoteCollectPollFtmm轮询结束 ========")
    print("合计: 选中 %d，作业成功 %d，递交成功 %d%s"
          % (len(EXEC_LOG["results"]), poll_ok, upload_ok,
             ("，递交未发生 %d（%s）" % (upload_skip,
                                    "dry_run 演练" if DRY_RUN else "采集未成功，未进入递交"))
             if upload_skip else ""))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
