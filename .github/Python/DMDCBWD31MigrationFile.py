#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMDCBWD31MigrationFile —— 下载上传清单 → 逐文件 OBS 上传 → 回传成功清单
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
承接 DMDCBWD11CollectFile（把仓库数据文件清单上传到远端）之后的“真正把文件上传到 OBS”环节：

    1. 从 Commit.json 登记的目标仓库（Owner/Repo）的 BranchMigration 分支，下载
       分支配置 Migration.{当前分支}.json 中 UploadFileListPath 指定的清单 txt
       （GitHub Contents GET）；
    2. 把清单按行读取，每行是该文件相对【仓库根目录】的相对路径；
    3. 逐行到本地仓库工作树取对应文件：
       - 文件不存在：打印日志 + 进程内汇总（不报错、跳过，不终止）；
       - 文件存在：调用 OBSClient.upload_file 上传（携带 rel_path / key / owner /
         repo / branch / md5 / root_prefix / cid / callback_url 等回调上下文；
         其中回调上下文 branch 取 BranchCurrent【来源分支】——由当前 git 检出
         动态取得（与清单 Branch/{BranchCurrent}/ 目录段口径一致，供回调处理端
         归档到 Archive/Branch/{branch}/{cid}/；勿误传 BranchMigration）；
         key 传 None 时由 OBSClient 自动构造，规则：
         {OBSRootPrefix}/{运行当天yyyyMMdd}/{CID}/{文件名}，
         CID 按分支配置的 OBSCIDRoutes 前缀路由规则解析
         （先配置先命中，未命中任何规则用默认 3501806882199176893）；
         OBSRootPrefix 与回调端点不放进配置文件（避免入库），改由环境变量注入：
         前缀必填 = HWC_OBS_ROOT_PREFIX；回调端点可选 = HWC_OBS_CALLBACK_URL
         （设置了回调端点，PUT 才携带 x-obs-callback 头），由 OBS 在对象落盘成功后
         【服务端原生回调】该端点（回调体含仓库上下文 + OBS 系统变量，
         见 OBSClient 模块说明）；
    4. 汇总上传成功的文件，一行一个写入本地临时 txt（与上传清单同一口径：
       四元组条目原样回写 {最后修改时间}|{SHA1}|{MD5}|{相对路径}，历史格式条目
       仍只写相对路径——详见 compose_success_content）；
    5. 调用 GitHubCommitContent.commit_content_file 将该成功清单回传到远端
       Branch/{BranchCurrent}/UploadSuccessList_yyyyMMdd_HHmmssSSS_{MD5}.txt
       （提交分支 = Commit.json 的 BranchSuccess【成功清单专用分支】；
         未登记 BranchSuccess 时回退 BranchMigration，保持原有行为；
         MD5 为内容哈希、大写 hex）。

配置来源
----------------------------------------------------------------------------------------
    Commit.json（目标仓库身份 / 各流程回传分支，GitHubCommitContent 默认值）：
        { "Owner": "ACANX", "Repo": "Dist", "BranchMigration": "Migration",
          "BranchSuccess": "Success", "BranchDelete": "Delete" }
        BranchSuccess = UploadSuccessList 成功清单的回传分支（未登记则回退 BranchMigration）
        该文件登记的是【仓库级】身份与回传分支，与当前分支无关 —— 权威副本只有
        dev 分支上的一份，运行时经 Contents API 取回并落盘缓存（见
        GitHubCommitContent.load_commit_config）；取不到才回退本地同目录副本。

    Migration.{BranchCurrent}.json（仓库内路径类配置；OBS 前缀 / 回调端点已移出，改走环境变量）：
        文件名中的 {BranchCurrent} 随【当前 git 分支】动态解析（resolve_config_path）：
        在 quote 分支上读 Migration.quote.json，在 quote-gold 分支上读
        Migration.quote-gold.json —— 同一份脚本原样放到哪个分支就读哪份配置，
        各分支因此无需各维护一份脚本副本，合并时配置文件也不会冲突。
        {
          "UploadFileListPath": "Branch/{BranchCurrent}/UploadFileList_....txt",  // 待下载清单的仓库内路径
          "OBSCIDRoutes": [                                           // CID 前缀路由（先配置先命中）
            { "FilePrefix": "UpStream/Archive/20260825/HK_HKEX_", "CID": "11..." },
            { "FilePrefix": "UpStream/Archive/20260825/CN_CN_A",   "CID": "22..." }
          ]                                                           // 未命中任何规则 -> 默认 CID
        }
        历史字段：BranchCurrent / BranchMigration / TargetBranch 均已废弃 —— BranchCurrent
        由当前 git 分支动态取得，BranchMigration 取自 Commit.json（取值重复），
        TargetBranch 无任何代码读取。文件中若仍残留这些键，一律忽略（残留
        BranchCurrent 时额外告警）。

    环境变量（OBS 运行期配置，不入仓库；GitHub Actions 以 Secret 注入）：
        HWC_OBS_ROOT_PREFIX  必填：OBS 对象 key 前缀（原配置文件的 OBSRootPrefix）
        HWC_OBS_CALLBACK_URL 可选：服务端原生回调端点（原配置文件的 CallbackUrl；
                              未设置则不回调，PUT 不携带 x-obs-callback）

退出码：
    0 = 流程正常结束（含“本地文件缺失 / OBS 上传失败被跳过、成功清单为空”等非致命情形）；
    1 = 致命失败（配置缺失 / 下载清单失败 / 回传成功清单失败）。

【环境要求】
    - Python 3.8+，仅标准库；
    - 远端下载/回传依赖环境变量 GIT_COMMIT_TOKEN（由 GitHubCommitContent 读取）；
    - OBS 上传必需 HWC_OBS_ROOT_PREFIX（对象 key 前缀）；可选 HWC_OBS_CALLBACK_URL（回调端点）；
    - 同目录需存在：GitHubCommitContent.py、UpstreamFileList.py、OBSClient.py、
      Commit.json（取不到远端时的本地回退副本）、Migration.{当前分支}.json。

二、运行方式
----------------------------------------------------------------------------------------
    $env:GIT_COMMIT_TOKEN  = "ghp_你的Token"
    $env:HWC_OBS_ROOT_PREFIX  = "GitHub/EventGridOBSStorage/G0128M"                   # 必填
    $env:HWC_OBS_CALLBACK_URL = "https://distdmcb.103456.xyz/API/V1/GitHub/CallBack"  # 可选
    python3 DMDCBWD31MigrationFile.py

    # 或 import 调用：
    import DMDCBWD31MigrationFile
    code = DMDCBWD31MigrationFile.main()      # 0=成功/非致命跳过，1=致命失败
"""

import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# 同目录模块 import：显式把本脚本所在目录加入 sys.path（兼容任意 cwd 执行）
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import GitHubCommitContent  # noqa: E402
import OBSClient            # noqa: E402
from UpstreamFileList import _repo_root  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

# 分支配置文件（只存仓库内路径类配置；OBS 前缀 / 回调端点走环境变量）命名约定：
#   Migration.{BranchCurrent}.json（与本脚本同目录）
# 文件名中的 {BranchCurrent} **随当前 git 分支动态解析**（见 resolve_config_path）——
# 同一份脚本原样放到 quote / quote-gold / … 都能加载该分支自己的配置，各分支因此
# 无需各自维护一份 DMDCBWD31MigrationFile.py 副本；两份配置在合并时也不会冲突。
CONFIG_NAME_TEMPLATE = "Migration.%s.json"
JSON_KEY_UPLOAD_FILE_LIST_PATH = "UploadFileListPath"
# 历史字段：BranchCurrent 现由当前 git 分支动态取得。配置文件里若仍残留该键，
# 一律忽略并告警（仅作迁移期提示，不参与解析）。
# 另：TargetBranch 已删除（全仓无代码读取）、BranchMigration 已删除（取值与
# Commit.json 完全重复），故此处不再声明对应常量。
JSON_KEY_BRANCH_CURRENT_LEGACY = "BranchCurrent"
# OBS 对象 key 前缀（必填）：原配置文件的 OBSRootPrefix，改环境变量注入
ENV_OBS_ROOT_PREFIX = "HWC_OBS_ROOT_PREFIX"
# 服务端原生回调端点（可选）：原配置文件的 CallbackUrl，改环境变量注入；
# 未设置则不回调（upload_file 不携带 x-obs-callback 头）
ENV_OBS_CALLBACK_URL = "HWC_OBS_CALLBACK_URL"
# CID 前缀路由：OBSCIDRoutes = [ {FilePrefix, CID}, ... ]，顺序即优先级（先配置先命中）
JSON_KEY_OBS_CID_ROUTES = "OBSCIDRoutes"
JSON_KEY_ROUTE_FILE_PREFIX = "FilePrefix"
JSON_KEY_ROUTE_CID = "CID"

# Commit.json 中「成功清单回传分支」键：UploadSuccessList 提交到该分支；
# 未登记 / 为空时回退到 BranchMigration（保持该字段引入前的原有行为）
COMMIT_JSON_KEY_BRANCH_SUCCESS = "BranchSuccess"

# 成功清单文件名模式：UploadSuccessList_yyyyMMdd_HHmmssSSS_{MD5}.txt
REMOTE_BASE_DIR = "Branch"
SUCCESS_FILE_PREFIX = "UploadSuccessList"

# 本次 OBS 上传的“存储桶目标 key”：None = 不显式指定，由 OBSClient.upload_file
# 按规则自动构造（{OBSRootPrefix}/{运行当天yyyyMMdd}/3501806882199176893/{文件名}，
# 其中 OBSRootPrefix 由环境变量 HWC_OBS_ROOT_PREFIX 提供并随调用传入）；
# 后续如需改为固定 key，可在此一次性替换。
OBS_KEY_AUTO = None


def _script_dir():
    """返回本脚本所在目录（绝对路径）"""
    return os.path.dirname(os.path.abspath(__file__))


def _ensure_console_utf8():
    """将 stdout/stderr 重配置为 UTF-8（errors=replace）

    防止 Windows 遗留控制台 / 非 UTF-8 locale 下输出中文时抛 UnicodeEncodeError
    导致看似“没有任何输出”的静默崩溃。重配置失败时静默跳过。

    :return: 无
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def current_branch(start_dir=None):
    """取当前工作树所在的分支名（即 BranchCurrent）

    这是“当前分支”的唯一事实来源 —— 配置文件路径与 BranchCurrent 都由它推出，
    不再写进配置文件，否则又多出一份可能与实际检出的分支不一致的副本，
    而消除这类副本正是本次改造的目的。

    :param start_dir: 判定分支的起始目录（须位于目标仓库工作树内）；None = 本脚本所在目录
    :return: (branch, err)：成功 branch 为分支名、err 为 None；
             失败 branch 为 None、err 为失败原因
    """
    base = start_dir or _SCRIPT_DIR
    try:
        proc = subprocess.run(
            ["git", "-C", base, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return None, "无法执行 git（%s）；本流程需要 git 判定当前分支" % e
    if proc.returncode != 0:
        return None, "git rev-parse --abbrev-ref HEAD 失败：%s" % (
            (proc.stderr or "").strip() or "退出码 %d" % proc.returncode)
    name = (proc.stdout or "").strip()
    if not name or name == "HEAD":
        return None, ("当前为游离 HEAD（detached HEAD），判定不出分支名；"
                      "配置文件按约定名为 %s，请在具名分支的检出上运行"
                      % (CONFIG_NAME_TEMPLATE % "<当前分支>"))
    return name, None


def resolve_config_path(cfg_path=None, start_dir=None):
    """解析分支配置文件路径（约定名 Migration.{当前分支}.json）

    :param cfg_path: 显式指定的配置路径（测试注入 / 特殊部署用）；None = 按约定解析
    :param start_dir: 判定当前分支的起始目录；None = 本脚本所在目录
    :return: (path, branch, err)：成功 path 为配置文件路径、branch 为当前分支名、
             err 为 None；失败 path 为 None、err 为失败原因
    """
    branch, err = current_branch(start_dir)
    if branch is None:
        return None, None, err
    if cfg_path:
        return cfg_path, branch, None
    return os.path.join(_SCRIPT_DIR, CONFIG_NAME_TEMPLATE % branch), branch, None


def load_commit_identity(cfg_path=None):
    """读取 Commit.json 并校验目标仓库身份字段（owner / repo / branch_migration）

    :param cfg_path: Commit.json 路径；None = 权威副本（dev）优先，取不到用同目录副本
    :return: (cfg, err)：成功时 cfg 为 dict{owner,repo,branch_migration}、err 为 None；
             失败时 cfg 为 None、err 为失败原因
    """
    raw = GitHubCommitContent.load_commit_config(cfg_path)
    if raw is None:
        return None, "Commit.json 缺失或非法，无法解析目标仓库身份"
    missing = [k for k in ("owner", "repo", "branch_migration") if not raw.get(k)]
    if missing:
        return None, "Commit.json 缺少登记字段: %s" % "、".join(missing)
    return {"owner": raw["owner"], "repo": raw["repo"],
            "branch_migration": raw["branch_migration"]}, None


def load_success_branch(cfg_path=None, fallback=""):
    """读取 Commit.json 的 BranchSuccess（UploadSuccessList 成功清单的回传分支）

    走 GitHubCommitContent.load_commit_config 这一统一入口 —— 它现在会连带返回
    branch_success，且默认从权威分支（dev）取回配置。原先本函数另行打开同一文件读一遍，
    两处口径可能不一致；统一后不复存在。

    解析不出（配置不可用 / 字段缺失 / 字段为空串）时，一律回退 fallback，
    以保证未登记该字段的既有部署行为不变（调用方传 BranchMigration）。

    :param cfg_path: 本地 Commit.json 路径；None = 权威副本（dev）优先
    :param fallback: 解析不出 BranchSuccess 时的回退分支
    :return: 分支名（非空字符串）
    """
    cfg = GitHubCommitContent.load_commit_config(cfg_path)
    if not isinstance(cfg, dict):
        return fallback
    return (cfg.get("branch_success") or "").strip() or fallback


def load_upstream_upload_settings(cfg_path=None, start_dir=None):
    """读取分支配置文件的仓库内路径配置（含 CID 前缀路由）；OBS 前缀 / 回调端点
    不在本文件，改由 load_obs_runtime_env() 从环境变量注入

    配置文件按约定名 Migration.{当前分支}.json 动态解析；BranchCurrent 不再从文件读取，
    而由当前 git 分支取得（见 current_branch）。

    :param cfg_path: 配置文件路径；None = 按约定解析
    :param start_dir: 判定当前分支的起始目录；None = 本脚本所在目录
    :return: (settings, err)：成功时 settings 为
             dict{upload_file_list_path, branch_current, cid_routes, config_path}、
             err 为 None；失败时 settings 为 None、err 为失败原因
             cid_routes = [ {prefix, cid}, ... ]，顺序即优先级（先配置先命中）；
             未配置 OBSCIDRoutes 字段时为空列表（全部文件走默认 CID）
    """
    path, branch_current, err = resolve_config_path(cfg_path, start_dir)
    if path is None:
        return None, "解析分支配置文件路径失败: %s" % err
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return None, "读取 %s 失败: %s（分支 %s 的配置文件按约定名为 %s）" % (
            path, e, branch_current, CONFIG_NAME_TEMPLATE % branch_current)
    except ValueError as e:
        return None, "%s 不是合法 JSON: %s" % (path, e)
    if not isinstance(data, dict):
        return None, "%s 顶层应为 JSON 对象" % path

    # 历史字段 BranchCurrent：已改为按当前 git 分支动态取得，残留则忽略并告警
    if JSON_KEY_BRANCH_CURRENT_LEGACY in data:
        _log("[WARN] %s 中的 %s 已废弃并被忽略：BranchCurrent 现由当前 git 分支"
             "动态取得（本次 = %s）"
             % (path, JSON_KEY_BRANCH_CURRENT_LEGACY, branch_current))

    def grab(key):
        val = data.get(key)
        return str(val).strip() if val is not None else ""

    path_value = grab(JSON_KEY_UPLOAD_FILE_LIST_PATH)
    if not path_value:
        return None, "%s 缺少字段 %s（不得为空）" % (path, JSON_KEY_UPLOAD_FILE_LIST_PATH)

    # ---- CID 前缀路由：可选；缺省 = 空（全部走默认 CID）----
    routes_value = data.get(JSON_KEY_OBS_CID_ROUTES)
    if routes_value is None:
        cid_routes = []
    elif not isinstance(routes_value, list):
        return None, "%s 的 %s 应为 JSON 数组" % (path, JSON_KEY_OBS_CID_ROUTES)
    else:
        cid_routes = []
        for idx, item in enumerate(routes_value):
            if not isinstance(item, dict):
                return None, "%s 的 %s[%d] 应为 JSON 对象" % (
                    path, JSON_KEY_OBS_CID_ROUTES, idx)
            route_prefix = (str(item.get(JSON_KEY_ROUTE_FILE_PREFIX) or "").strip())
            route_cid = (str(item.get(JSON_KEY_ROUTE_CID) or "").strip())
            if not route_prefix or not route_cid:
                return None, "%s 的 %s[%d] 缺少 %s 或 %s（不得为空）" % (
                    path, JSON_KEY_OBS_CID_ROUTES, idx,
                    JSON_KEY_ROUTE_FILE_PREFIX, JSON_KEY_ROUTE_CID)
            cid_routes.append({"prefix": route_prefix, "cid": route_cid})

    return {"upload_file_list_path": path_value,
            "branch_current": branch_current,
            "cid_routes": cid_routes,
            "config_path": path}, None


def resolve_cid(rel_path, cid_routes=None, default_cid=None):
    """按 CID 前缀路由规则为 rel_path 匹配 CID（配置在前的先命中）

    :param rel_path: 该文件相对仓库根目录的路径（清单行原值）
    :param cid_routes: 路由规则列表 [{prefix, cid}, ...]，顺序即优先级（先配置先命中）
    :param default_cid: 未命中任何规则时的默认 CID；None = OBSClient.DEFAULT_OBS_CID
    :return: (cid, matched_prefix)：
        - cid：命中的 CID；未命中时返回 default_cid
        - matched_prefix：命中的前缀；未命中时为 None
    """
    default = default_cid or OBSClient.DEFAULT_OBS_CID
    rel = rel_path or ""
    for route in (cid_routes or []):
        prefix = route.get("prefix") or ""
        if prefix and rel.startswith(prefix):
            return (route.get("cid") or default), prefix
    return default, None


def _build_timestamp(now=None):
    """生成清单文件名用时间戳 yyyyMMdd_HHmmssSSS（毫秒 3 位）

    :param now: datetime 对象；None = 当前本地时间（便于测试注入）
    :return: 时间戳串，如 "20260909_143000123"
    """
    dt = now or datetime.datetime.now()
    return dt.strftime("%Y%m%d_%H%M%S") + "%03d" % (dt.microsecond // 1000)


def _md5_hex_upper(data):
    """返回字节串的 MD5 大写 hex

    :param data: bytes
    :return: 32 位大写 hex 字符串
    """
    return hashlib.md5(data).hexdigest().upper()


def _now_tag():
    """当前时间戳，格式 yyMMdd.HHmmss.SSS（毫秒 3 位）

    例：260909.155256.618。用于给日志行加前缀，方便排查、分析链路执行耗时。

    :return: 时间戳字符串
    """
    now = datetime.datetime.now()
    return now.strftime("%y%m%d.%H%M%S") + ".%03d" % (now.microsecond // 1000)


def _log(message):
    """带 yyMMdd.HHmmss.SSS 时间前缀的日志输出（走 stdout）

    flush=True 不可省：CI 里 stdout 是管道、非 TTY，Python 默认块缓冲，日志会攒到
    进程结束才一次性写出，导致 GitHub 侧的接收时间戳全部挤在同一秒、与实际产出时刻
    相差几十秒（本文件此前就出现过整段日志挤在 05:00:41 的现象）。逐行 flush 后，
    日志行的接收时间与行内 [yyMMdd.HHmmss.SSS] 前缀才能对上。

    :param message: 日志内容（可含 [INFO]/[PASS]/[FAIL] 等分级前缀）
    """
    print("[%s] %s" % (_now_tag(), message), flush=True)


def parse_manifest_lines(text):
    """解析清单文本为条目列表（一行一个）

    现行格式为四元组，**路径在最后一位**（便于“从左按 | 切分、剩余整体作为路径”，
    即使路径中含 | 也不会错位）：

        {最后修改时间}|{SHA1}|{MD5}|{相对路径}

    兼容历史格式（一行只有一个相对路径）：此时 sha1 / md5 / mtime 均为 None，
    由调用方决定回退策略（当前实现：本地重算 md5，sha1 传 None）。

    :param text: 清单全文
    :return: list[dict]，每项 {"rel_path": str, "sha1": str|None,
             "md5": str|None, "mtime": str|None}；空行与全空白行忽略
    """
    text = (text or "").lstrip("﻿")  # 去掉可能的 BOM
    entries = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        fields = raw.split("|", 3)   # 至多切 3 次 → 第 4 段保留其余全部内容
        if len(fields) == 4:
            mtime, sha1, md5, rel_path = fields
        else:
            mtime, sha1, md5, rel_path = None, None, None, raw
        rel_path = rel_path.strip().replace("\\", "/")
        if not rel_path:
            continue
        entries.append({
            "rel_path": rel_path,
            "sha1": (sha1 or "").strip() or None,
            "md5": (md5 or "").strip() or None,
            "mtime": (mtime or "").strip() or None,
        })
    return entries


def entry_has_hash(entry):
    """判断条目是否带四元组信息（时间戳 / SHA1 / MD5 任一非空）

    历史格式（仅路径）的条目三者均为 None；四元组条目三者应齐备，但为容忍
    上游产出残缺（如 {时间戳}||{MD5}|{路径}），此处按“任一非空”判定。

    :param entry: parse_manifest_lines 的条目 dict
    :return: bool
    """
    return bool(entry.get("mtime") or entry.get("sha1") or entry.get("md5"))


def compose_success_content(entries):
    """把上传成功的条目拼成“一行一个”的成功清单文本（UTF-8，LF 换行，末行带换行）

    与上传清单（UploadFileList）同一口径，便于下游按同一套规则解析：
        - 四元组条目 → 原样回写 {最后修改时间}|{SHA1}|{MD5}|{相对路径}
          （缺失的字段留空，不写 "None"）；
        - 历史格式条目 → 仍只写相对路径（该条目本就没有哈希可回写）。

    :param entries: 上传成功的条目列表（parse_manifest_lines 的元素）
    :return: 待写入 txt 的完整文本；entries 为空返回空字符串
    """
    if not entries:
        return ""
    lines = []
    for entry in entries:
        if entry_has_hash(entry):
            lines.append("%s|%s|%s|%s" % (entry.get("mtime") or "",
                                          entry.get("sha1") or "",
                                          entry.get("md5") or "",
                                          entry["rel_path"]))
        else:
            lines.append(entry["rel_path"])
    return "\n".join(lines) + "\n"


def load_obs_runtime_env():
    """从环境变量读取 OBS 运行期配置（原配置文件的 OBSRootPrefix / CallbackUrl）

    前缀必填（构造对象 key 需要）；回调端点可选（未设置则上传不携带 x-obs-callback）。

    :return: (root_prefix, callback_url, err)：成功时 err 为 None、二者为字符串（可空）；
             前缀缺失时 root_prefix/callback_url 为 None、err 为失败原因字符串
    """
    root_prefix = os.environ.get(ENV_OBS_ROOT_PREFIX, "").strip()
    callback_url = os.environ.get(ENV_OBS_CALLBACK_URL, "").strip()
    if not root_prefix:
        return None, None, ("缺少环境变量 %s（OBS 对象 key 前缀，必填；"
                            "该值原为配置文件的 OBSRootPrefix，已移出配置文件）"
                            % ENV_OBS_ROOT_PREFIX)
    return root_prefix, callback_url, None


def main(cfg_commit_path=None, cfg_branch_path=None, out_dir=None,
         read_fn=None, commit_fn=None, obs_fn=None):
    """主流程：下载上传清单 → 逐文件 OBS 上传 → 回传成功清单

    :param cfg_commit_path: Commit.json 路径；None = 权威副本（dev）优先，取不到用同目录副本
    :param cfg_branch_path: 分支配置 Migration.{当前分支}.json 路径；None = 按当前 git 分支解析
    :param out_dir: 成功清单本地临时目录；None = 系统临时目录
    :param read_fn: 远端文件读取函数（签名同 GitHubCommitContent.read_file_text）；
                    None = 使用 GitHubCommitContent.read_file_text
    :param commit_fn: 远端文件提交函数（签名同 commit_content_file）；
                      None = 使用 GitHubCommitContent.commit_content_file
    :param obs_fn: OBS 上传函数（签名同 OBSClient.upload_file）；
                   None = 使用 OBSClient.upload_file（真实上传，key 传 None 自动构造）
    :return: 退出码：0 = 流程正常结束；1 = 致命失败
    """
    _ensure_console_utf8()

    # ---- 1) 读取配置：Commit.json（身份 / 迁移分支）+ Migration.{当前分支}.json（路径）
    #          + 环境变量（OBS）----
    identity, err = load_commit_identity(cfg_commit_path)
    if identity is None:
        _log("[FAIL] %s" % err)
        return 1
    owner, repo, branch_migration = (identity["owner"], identity["repo"],
                                     identity["branch_migration"])
    # 成功清单（UploadSuccessList）回传分支：Commit.json 的 BranchSuccess；
    # 未登记该字段时回退 BranchMigration，保持原有行为
    branch_success = load_success_branch(cfg_commit_path,
                                         fallback=branch_migration)
    settings, err = load_upstream_upload_settings(cfg_branch_path)
    if settings is None:
        _log("[FAIL] %s" % err)
        return 1
    upload_path = settings["upload_file_list_path"]
    branch_current = settings["branch_current"]
    cid_routes = settings["cid_routes"]
    config_path = settings["config_path"]
    root_prefix, callback_url, env_err = load_obs_runtime_env()
    if env_err is not None:
        _log("[FAIL] %s" % env_err)
        return 1
    _log("[INFO] 目标仓库 = %s/%s | 迁移分支 = %s（BranchMigration）"
          % (owner, repo, branch_migration))
    _log("[INFO] 当前分支（BranchCurrent）= %s（取自当前 git 检出，决定配置文件名）"
          % branch_current)
    _log("[INFO] 分支配置 = %s" % config_path)
    _log("[INFO] 成功清单回传分支 = %s（Commit.json 的 %s%s）"
          % (branch_success, COMMIT_JSON_KEY_BRANCH_SUCCESS,
             "" if branch_success != branch_migration
             else "，未登记或为空，已回退 BranchMigration"))
    _log("[INFO] OBS 对象前缀（env %s）= %s" % (ENV_OBS_ROOT_PREFIX, root_prefix))
    if callback_url:
        _log("[INFO] 上传成功后服务端原生回调（env %s）：%s"
              % (ENV_OBS_CALLBACK_URL, callback_url))
    if cid_routes:
        _log("[INFO] CID 前缀路由规则 %d 条（先配置先命中，未命中走默认 %s）:"
              % (len(cid_routes), OBSClient.DEFAULT_OBS_CID))
        for _route in cid_routes:
            _log("[INFO]   %s -> %s" % (_route["prefix"], _route["cid"]))

    # ---- 2) 下载上传清单 txt（远端）----
    read = read_fn or GitHubCommitContent.read_file_text
    download = read(upload_path, branch=branch_migration, owner=owner, repo=repo)
    if not (isinstance(download, dict) and download.get("success")):
        message = download.get("message") if isinstance(download, dict) else download
        _log("[FAIL] 下载上传清单失败: %s" % message)
        return 1
    entries = parse_manifest_lines(download.get("text"))
    dl_branch = download.get("branch") or branch_migration
    with_hash = sum(1 for e in entries if e["md5"])
    _log("[INFO] 上传清单下载自 GitHub：%s/%s 分支 %s"
          % (owner, repo, dl_branch))
    _log("[INFO]   ↳ 远程文件：https://github.com/%s/%s/blob/%s/%s"
          "（共 %d 行：%d 行四元组 {最后修改时间}|{SHA1}|{MD5}|{路径}，"
          "%d 行历史格式仅含路径）"
          % (owner, repo, dl_branch, upload_path, len(entries),
             with_hash, len(entries) - with_hash))

    # ---- 3) 定位本地仓库根，逐行处理 ----
    root = _repo_root()
    if root is None:
        _log("[FAIL] 未能在仓库中找到 .git 入口，无法定位仓库根目录")
        return 1

    obs = obs_fn or OBSClient.upload_file
    ok_entries, missing_count, fail_count = [], 0, 0
    for entry in entries:
        rel_path = entry["rel_path"]
        local_file = os.path.join(root, rel_path)
        if not os.path.isfile(local_file):
            missing_count += 1
            _log("[跳过] 本地文件不存在: %s" % rel_path)
            continue
        # 回调口径哈希：优先取清单四元组里已有的值（清单由采集端按 git 最后提交时间 +
        # 内容 SHA1/MD5 产出）；清单为历史格式（仅路径）时才本地重算 md5、sha1 留空。
        if entry["md5"]:
            digest_md5 = entry["md5"]
            digest_sha1 = entry["sha1"]
        else:
            with open(local_file, "rb") as f:
                digest_md5 = _md5_hex_upper(f.read())
            digest_sha1 = None
        cid_value, _matched = resolve_cid(rel_path, cid_routes)
        result = obs(local_file=local_file, rel_path=rel_path, key=OBS_KEY_AUTO,
                     owner=owner, repo=repo, branch=branch_current,
                     sha1=digest_sha1, md5=digest_md5, root_prefix=root_prefix,
                     cid=cid_value, callback_url=callback_url)
        if isinstance(result, dict) and result.get("success"):
            ok_entries.append(entry)
            _log("[上传成功] [%s] %s" % (cid_value, rel_path))
        else:
            fail_count += 1
            message = result.get("message") if isinstance(result, dict) else result
            _log("[上传失败] %s -> %s" % (rel_path, message))
    _log("[INFO] 汇总：共 %d 个 | 成功 %d | 本地缺失 %d | 上传失败 %d"
          % (len(entries), len(ok_entries), missing_count, fail_count))

    # ---- 4) 无成功文件：正常结束（无需回传成功清单）----
    if not ok_entries:
        _log("[INFO] 无上传成功的文件，跳过回传 UploadSuccessList")
        return 0

    # ---- 5) 成功清单一行一个写本地临时 txt，并回传远端 ----
    # 与上传清单同一口径：四元组条目原样回写四元组，历史格式条目仍只写路径
    content = compose_success_content(ok_entries)
    hash_rows = sum(1 for e in ok_entries if entry_has_hash(e))
    _log("[INFO] 成功清单 %d 行：%d 行四元组 {最后修改时间}|{SHA1}|{MD5}|{路径}，"
          "%d 行历史格式仅含路径"
          % (len(ok_entries), hash_rows, len(ok_entries) - hash_rows))
    digest = _md5_hex_upper(content.encode("utf-8"))
    file_name = "%s_%s_%s.txt" % (SUCCESS_FILE_PREFIX, _build_timestamp(), digest)
    path_key = "%s/%s/%s" % (REMOTE_BASE_DIR, branch_current, file_name)
    out = out_dir or tempfile.gettempdir()
    local_txt = os.path.join(out, file_name)
    try:
        with open(local_txt, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError as e:
        _log("[FAIL] 写入本地成功清单失败: %s" % e)
        return 1

    commit = commit_fn or GitHubCommitContent.commit_content_file
    try:
        result = commit(path_key, local_txt, branch=branch_success,
                        commit_msg="UploadSuccessList @%s" % _build_timestamp())
    finally:
        try:
            os.remove(local_txt)
        except OSError:
            pass

    if isinstance(result, dict) and result.get("success"):
        _log("[PASS] 成功清单已回传[HTTPStatus:%s] https://github.com/%s/%s/blob/%s/%s"
              % (result.get("http_status"), owner, repo, branch_success, path_key))
        return 0
    message = result.get("message") if isinstance(result, dict) else result
    _log("[FAIL] 成功清单回传失败（目标分支 %s）: %s" % (branch_success, message))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
