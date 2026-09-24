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
from MvsvQuoteBuilder import buildMvsvContent, buildMvsvFileName, extractSummary
from FtmmQuoteV2WebRestClient import fetchFiveDayMinuteQuote, extractMinuteList
from DateTimeUtil import (fmtDisplay, fmtTsSuffix, isUsDst, nowBeijing,
                           parseDt, shiftDays, toEpochSeconds, utcNow)
from SupabaseRestClient import SupabaseRestClient, SupabaseRestError, eqFilter

# ============ 数据落点常量（按需求锁定：ACANX/Distribution @ quote）============
# 刻意不走 Commit.json / .git 解析（那里登记的是 acdnx/Distribution 转存端）。
GITHUB_OWNER = "ACANX"
GITHUB_REPO = "Distribution"
GITHUB_BRANCH = "quote"
DATA_DIR = "Data/Finv/SecuQuote/V5"          # MVSV 数据文件落点目录
LOG_DIR = "Data/Finv/SecuQuote/ExecLog"    # 执行日志落点目录

# ============ 调度常量 ============
# 单次运行采集的标的数量。按需求写死在此，后续如需调整直接改这里。
POLL_COUNT = 1
FORCE_FETCH_INTERVAL = 72 * 3600          # 距上次检查超过 72 小时强制采集（秒）

# ============ 环境变量 ============
# Supabase 凭据由 SupabaseRestClient 从环境变量读取（不在此处留存副本）
GIT_COMMIT_TOKEN = os.environ.get("GIT_COMMIT_TOKEN", "").strip()
STATE_TABLE = os.environ.get("POLL_STATE_TABLE", "finv_quote_collect_state_poll_futu").strip()
FLAG_ENABLE = os.environ.get("POLL_FLAG_ENABLE", "1").strip()
DRY_RUN = os.environ.get("POLL_DRY_RUN", "").strip().lower() in ("true", "1", "yes", "on")
TEMP_DIR = Path(os.environ.get("POLL_TEMP_DIR", "")) if os.environ.get("POLL_TEMP_DIR", "").strip() \
    else Path(tempfile.gettempdir()) / "finv_quote_collect_poll_ftmm"

# 品种 → 市场 / 权重 元数据（load_config_meta() 填充；空值时回退中性权重）
CODE_MARKET: Dict[str, str] = {}
CODE_WEIGHT_BASE: Dict[str, int] = {}
CODE_WEIGHT_FREQ: Dict[str, int] = {}

# 执行日志聚合（运行结束时整体上传；密钥类信息一律不进这里）
EXEC_LOG: Dict[str, object] = {
    "start_time": None,
    "dry_run": DRY_RUN,
    "state_backend": "supabase_pg",
    "state_table": STATE_TABLE,
    "flag_enable": FLAG_ENABLE,
    "poll_count": POLL_COUNT,
    "selected": [],
    "results": [],
    "errors": [],
}


# ============ Config.json 元数据（临时兼容旧版） ============
def load_config_meta() -> None:
    """从 Config.json 加载市场/权重元数据（旧版临时兼容；缺失时回退中性权重）

    只取 code → market / weight_base / weight_freq 三个映射；品种全集以状态表为准，
    Config.json 中多出的品种不会参与轮询。文件缺失仅告警不阻断（中性权重 5.0 兜底）。
    """
    config_file = os.environ.get("CONFIG_FILE", "Config.json")
    cfg_path = Path(config_file)
    if not cfg_path.is_absolute():
        cfg_path = Path(os.path.dirname(os.path.abspath(__file__))) / config_file
    if not cfg_path.exists():
        print("⚠️ [配置] 未找到 %s：市场/权重元数据缺失，全部品种按中性权重 5.0 处理" % cfg_path)
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        print("⚠️ [配置] 读取 %s 失败（%s）：全部品种按中性权重 5.0 处理" % (cfg_path, e))
        return

    for s in config.get("secu", []):
        code = s.get("code")
        if not code:
            continue
        CODE_MARKET[code] = s.get("market", "UNKNOWN")
        for key, target in (("weight_base", CODE_WEIGHT_BASE), ("weight_freq", CODE_WEIGHT_FREQ)):
            raw = s.get(key)
            if raw is None:
                continue
            try:
                value = int(raw)
                if 1 <= value <= 10000:
                    target[code] = value
            except (TypeError, ValueError):
                pass
    print("[配置] 已加载 %s：%d 个品种的市场/权重元数据" % (cfg_path, len(CODE_MARKET)))


# ============ 状态读写（PostgREST 访问见 SupabaseRestClient.py） ============
def _to_int(value, default=0):
    """宽松转 int（PG 返回值可能为 None / 字符串）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_state(client):
    """读取状态表：返回 (品种代码列表, 状态映射)

    只取 flag_enable 选中的行；列名与 PG 表结构一一对应：
    dt_last_check / dt_last_fetch / ts_latest_data / count_last_fetch /
    count_fail / count_stale。
    """
    query_string = ("select=secu_code,dt_last_check,dt_last_fetch,ts_latest_data,"
                    "count_last_fetch,count_fail,count_stale"
                    "&%s&order=secu_code.asc" % eqFilter("flag_enable", FLAG_ENABLE))
    text = client.query(STATE_TABLE, query_string)
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise SupabaseRestError("状态表查询响应不是 JSON 数组")

    codes: List[str] = []
    state_map: Dict[str, Dict[str, Union[str, int]]] = {}
    for row in rows:
        code = (row.get("secu_code") or "").strip()
        if not code:
            continue
        codes.append(code)
        state_map[code] = {
            "time_last_check": row.get("dt_last_check") or "",
            "time_last_fetch": row.get("dt_last_fetch") or "",
            "ts_latest_data": _to_int(row.get("ts_latest_data")),
            "count_last_fetch": _to_int(row.get("count_last_fetch")),
            "count_fail": _to_int(row.get("count_fail")),
            "count_stale": _to_int(row.get("count_stale")),
        }
    print("[状态] 从 %s 加载成功：%d 个品种（flag_enable=%s）"
          % (STATE_TABLE, len(codes), FLAG_ENABLE))
    return codes, state_map


def patch_state_row(client, code, state):
    """把单品种状态回写状态表（dt_update 由表上触发器自动维护，flag_enable 不动）

    时间列为空（从未采集/从未取到新数据）时不出现在载荷里，保持 NULL 原状；
    计数列强制夹非负，满足表上的 chk_*_nonnegative 约束。
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

    filters = eqFilter("secu_code", code)
    text = client.patch(STATE_TABLE, filters, payload)
    try:
        hit = len(json.loads(text)) if (text or "").strip() else 0
    except ValueError:
        hit = -1
    if hit == 0:
        print("⚠️ [状态] %s 回写未命中任何行（品种可能已被移除）" % code)
    else:
        print("[状态] %s 回写成功：%s" % (code, json.dumps(payload, ensure_ascii=False)))


# ============ 分市场时段权重（沿用旧版，now 一律为北京时间） ============
# isUsDst 为纯日历规则，已抽至 DateTimeUtil.py
def get_market_weight(market, now):
    """分市场时段权重（与旧版一致；未知市场/周末取低权重，中性兜底 5.0）"""
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
    if market in ("FX", "FUTURES"):
        return 1 if is_weekend else 8.0
    if market == "CRYPTO":
        return 8.0
    return 5.0


# ============ 智能调度（指数退避 + 无新数据冷却 + 72h 强制采集） ============
def select_best_codes(codes, state_map, now, count):
    """选出本轮要采集的至多 count 个品种

    规则与旧版一致：
      1) 强制候选（从未采集 / 时间解析失败 / 距上次检查超 72h）按超时长度降序优先；
      2) 强制候选不足 count 时，其余品种按
         score = elapsed × 市场权重 × (base_w × freq_w) / 2^(fail+stale)
         降序补足（惩罚因子 = 2^(count_fail + count_stale)）。

    :return: [{"code","score","reason","reason_detail"}, ...]（强制优先，得分降序）
    """
    # ---------- 强制采集 ----------
    force_candidates = []
    for code in codes:
        state = state_map.get(code)
        lct_str = state.get("time_last_check") if state else None
        if not lct_str:
            force_candidates.append((code, float("inf"), "从未采集"))
            continue
        lct_dt = parseDt(lct_str)
        if lct_dt is None:
            force_candidates.append((code, float("inf"), "时间解析失败"))
            continue
        elapsed_check = (now - lct_dt).total_seconds()
        if elapsed_check > FORCE_FETCH_INTERVAL:
            force_candidates.append(
                (code, elapsed_check, "距上次检查%.1f小时" % (elapsed_check / 3600)))
    force_candidates.sort(key=lambda x: x[1], reverse=True)
    selected = [{"code": c, "score": 0.0, "reason": "force",
                 "reason_detail": "强制采集 (超过72小时未检查) - %s" % info}
                for c, _e, info in force_candidates]
    if len(selected) >= count:
        for s in selected[:count]:
            print("[调度] 强制采集: %s, %s" % (s["code"], s["reason_detail"]))
        return selected[:count]

    # ---------- 收益最大化（指数退避 + 无新数据冷却） ----------
    remaining = count - len(selected)
    scored = []
    log_lines = [
        "  {'CODE':<6s} {'elapsed':>10s} {'market_w':>8s} {'base_w':>6s} {'freq_w':>6s} "
        "{'static_w':>8s} {'fail':>4s} {'stale':>5s} {'penalty':>8s} {'score':>12s} {'REASON':<20s}"
    ]
    for code in codes:
        if any(s["code"] == code for s in selected):
            continue
        state = state_map.get(code)
        last_new_str = state.get("time_last_fetch") if state else None
        count_fail = state.get("count_fail", 0) if state else 0
        count_stale = state.get("count_stale", 0) if state else 0
        # 从未采集 / 解析失败 → 按 30 天前计（旧版口径）
        last_dt = parseDt(last_new_str, default=shiftDays(now, -30))

        elapsed = (now - last_dt).total_seconds()
        market_weight = get_market_weight(CODE_MARKET.get(code, "UNKNOWN"), now)
        base_w = CODE_WEIGHT_BASE.get(code, 1)
        freq_w = CODE_WEIGHT_FREQ.get(code, 1)
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
            % (code, elapsed, market_weight, base_w, freq_w, total_static_weight,
               count_fail, count_stale, penalty, score, ",".join(reasons)))
        scored.append({"code": code, "score": score, "reason": "score",
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
              % (s["code"], s["score"], s["reason_detail"]))
    selected.extend(picked)
    return selected


# ============ 行情采集与文件生成（契约分别见 FtmmQuoteV2WebRestClient.py / MvsvQuoteBuilder.py） ============
def collect_single(code):
    """采集单个品种：行情 API → MVSV 落盘；成功返回摘要 dict，失败返回 None"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    now = nowBeijing()
    ts_suffix = fmtTsSuffix(now)
    fetch_time = fmtDisplay(now)
    print("[采集] 开始处理品种: %s，北京时间 %s" % (code, fetch_time))
    raw = fetchFiveDayMinuteQuote(code)
    if not raw:
        return None
    data_node = raw["data"]
    minute_list = extractMinuteList(raw)
    if not minute_list:
        print("[采集] %s 数据列表为空" % code)
        return None
    latest_ts = max(item.get("ts", 0) for item in minute_list) if minute_list else 0
    summary = extractSummary(data_node)
    content = buildMvsvContent(code, minute_list, summary, fetch_time)
    file_name = buildMvsvFileName(code, ts_suffix)
    file_path = TEMP_DIR / file_name
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[采集] 文件保存成功: %s" % file_path)
    return {
        "code": code,
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
    """
    code = sel["code"]
    state = state_map.setdefault(code, {
        "time_last_check": "", "time_last_fetch": "",
        "ts_latest_data": 0, "count_last_fetch": 0,
        "count_fail": 0, "count_stale": 0,
    })
    state["time_last_check"] = now.isoformat()

    outcome = {"code": code, "reason": sel["reason"],
               "reason_detail": sel["reason_detail"],
               "poll_success": False, "upload_success": None,
               "record_count": 0, "error": None}

    result = collect_single(code)
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
            print("[状态] %s 有新数据，ts_latest_data=%d" % (code, new_ts))
        else:
            state["count_stale"] = _to_int(state.get("count_stale")) + 1
            state["count_fail"] = 0
            print("[状态] %s 无新数据，count_stale=%d" % (code, state["count_stale"]))

        if dry_run:
            print("[dry_run] 跳过文件递交：%s" % code)
        else:
            fname = Path(result["file_path"]).name
            path_key = "%s/%s/%s" % (DATA_DIR, code, fname)
            commit_msg = "[FinvQuoteCollectPollFtmm] %s minute quote data @%s" % (code, result["fetch_time"])
            up = upload_to_repo(result["content"], path_key, commit_msg)
            outcome["upload_success"] = up["success"]
            if not up["success"]:
                outcome["error"] = "递交失败: %s" % up.get("message")
                EXEC_LOG["errors"].append("%s 递交失败: %s" % (code, up.get("message")))
    else:
        state["count_fail"] = _to_int(state.get("count_fail")) + 1
        outcome["error"] = "行情采集失败"
        print("[状态] %s 采集失败，count_fail=%d" % (code, state["count_fail"]))

    if dry_run:
        print("[dry_run] 跳过状态回写：%s → %s" % (code, json.dumps(state, ensure_ascii=False)))
    else:
        patch_state_row(client, code, state)
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
        "state_table": EXEC_LOG["state_table"],
        "flag_enable": EXEC_LOG["flag_enable"],
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
    print("模式: %s | 状态表: %s | flag_enable=%s | POLL_COUNT=%d"
          % ("dry_run 演练" if DRY_RUN else "正常", STATE_TABLE, FLAG_ENABLE, POLL_COUNT))
    print("数据落点: %s/%s @ %s（脚本内常量锁定）" % (GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH))

    load_config_meta()

    # 凭据：Supabase 由客户端从环境变量解析（缺失抛 ValueError，不打印取值）
    if not DRY_RUN and not GIT_COMMIT_TOKEN:
        print("❌ 缺少 GIT_COMMIT_TOKEN 环境变量（数据递交需要；dry_run 模式可缺）")
        return 1

    try:
        client = SupabaseRestClient.fromEnv()
    except ValueError as e:
        print("❌ Supabase 凭据缺失: %s" % e)
        return 1

    try:
        codes, state_map = load_state(client)
    except Exception as e:
        print("❌ 状态表读取失败: %s" % e)
        EXEC_LOG["errors"].append("state_load: %s" % e)
        return 1

    if not codes:
        print("状态表中没有 flag_enable=%s 的品种，无事可做" % FLAG_ENABLE)
        return 0

    now = nowBeijing()
    print("[轮询] 当前北京时间: %s, 品种数: %d" % (now.isoformat(), len(codes)))

    selected = select_best_codes(codes, state_map, now, POLL_COUNT)
    EXEC_LOG["selected"] = [{"code": s["code"], "reason": s["reason"],
                             "reason_detail": s["reason_detail"]} for s in selected]

    any_fail = False
    for sel in selected:
        try:
            outcome = poll_one(client, sel, state_map, now, DRY_RUN)
        except Exception as e:
            outcome = {"code": sel["code"], "reason": sel["reason"],
                       "reason_detail": sel["reason_detail"],
                       "poll_success": False, "upload_success": None,
                       "record_count": 0, "error": "处理异常: %s" % e}
            EXEC_LOG["errors"].append("%s: %s" % (sel["code"], e))
            print("❌ [轮询] %s 处理异常: %s" % (sel["code"], e))
        EXEC_LOG["results"].append(outcome)
        if (not outcome.get("poll_success")) or outcome.get("upload_success") is False:
            any_fail = True

    poll_ok = sum(1 for r in EXEC_LOG["results"] if r.get("poll_success"))
    upload_ok = sum(1 for r in EXEC_LOG["results"] if r.get("upload_success") is True)
    upload_skip = sum(1 for r in EXEC_LOG["results"] if r.get("upload_success") is None)
    print("======== FinvQuoteCollectPollFtmm轮询结束 ========")
    print("合计: 选中 %d，作业成功 %d，递交成功 %d%s"
          % (len(EXEC_LOG["results"]), poll_ok, upload_ok,
             ("，递交跳过 %d（dry_run）" % upload_skip) if upload_skip else ""))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
