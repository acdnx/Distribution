#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMDCBWD31MigrationFile —— 下载上传清单 → 逐文件 OBS 上传 → 回传成功清单
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
承接 DMDCBWD11CollectFile（把仓库数据文件清单上传到远端）之后的“真正把文件上传到 OBS”环节：

    1. 从 Commit.json 登记的目标仓库（Owner/Repo）的 BranchMigration 分支，下载
       Upstream.json 中 UploadFileListPath 指定的清单 txt（GitHub Contents GET）；
    2. 把清单按行读取，每行是该文件相对【仓库根目录】的相对路径；
    3. 逐行到本地仓库工作树取对应文件：
       - 文件不存在：打印日志 + 进程内汇总（不报错、跳过，不终止）；
       - 文件存在：调用 OBSClient.upload_file 上传（携带 rel_path / key / owner /
         repo / branch / md5 / root_prefix / cid / callback_url 等回调上下文；
         其中回调上下文 branch 取 Upstream.json 的 BranchCurrent【来源分支】
         （与清单 Branch/{BranchCurrent}/ 目录段口径一致，供回调处理端
         归档到 Archive/Branch/{branch}/{cid}/；勿误传 BranchMigration）；
         key 传 None 时由 OBSClient 自动构造，规则：
         {OBSRootPrefix}/{运行当天yyyyMMdd}/{CID}/{文件名}，
         CID 按 Upstream.json 的 OBSCIDRoutes 前缀路由规则解析
         （先配置先命中，未命中任何规则用默认 3501806882199176893）；
         OBSRootPrefix 与回调端点不再放进 Upstream.json（避免入库），改由环境变量注入：
         前缀必填 = HWC_OBS_ROOT_PREFIX；回调端点可选 = HWC_OBS_CALLBACK_URL
         （设置了回调端点，PUT 才携带 x-obs-callback 头），由 OBS 在对象落盘成功后
         【服务端原生回调】该端点（回调体含仓库上下文 + OBS 系统变量，
         见 OBSClient 模块说明）；
    4. 汇总上传成功的文件，一行一个写入本地临时 txt；
    5. 调用 GitHubCommitContent.commit_content_file 将该成功清单回传到远端
       Branch/{BranchCurrent}/UploadSuccessList_yyyyMMdd_HHmmssSSS_{MD5}.txt
       （提交分支 = Commit.json 的 BranchMigration；MD5 为内容哈希、大写 hex）。

配置来源
----------------------------------------------------------------------------------------
    Commit.json（目标仓库身份，GitHubCommitContent 默认值）：
        { "Owner": "ACANX", "Repo": "Dist", "BranchMigration": "Migration", ... }

    Upstream.json（仓库内路径类配置；OBS 前缀 / 回调端点已移出，改走环境变量）：
        {
          "BranchCurrent": "dev",                                     // Branch/{BranchCurrent} 目录段
          "BranchMigration": "Migration",                             // 迁移文件驻留/上传目标远端分支
          "UploadFileListPath": "Branch/dev/UploadFileList_....txt"   // 待下载清单的仓库内路径
          "OBSCIDRoutes": [                                           // CID 前缀路由（先配置先命中）
            { "FilePrefix": "UpStream/Archive/20260825/HK_HKEX_", "CID": "11..." },
            { "FilePrefix": "UpStream/Archive/20260825/CN_CN_A",   "CID": "22..." }
          ]                                                           // 未命中任何规则 -> 默认 CID
        }

    环境变量（OBS 运行期配置，不入仓库；GitHub Actions 以 Secret 注入）：
        HWC_OBS_ROOT_PREFIX  必填：OBS 对象 key 前缀（原 Upstream.json 的 OBSRootPrefix）
        HWC_OBS_CALLBACK_URL 可选：服务端原生回调端点（原 Upstream.json 的 CallbackUrl；
                              未设置则不回调，PUT 不携带 x-obs-callback）

退出码：
    0 = 流程正常结束（含“本地文件缺失 / OBS 上传失败被跳过、成功清单为空”等非致命情形）；
    1 = 致命失败（配置缺失 / 下载清单失败 / 回传成功清单失败）。

【环境要求】
    - Python 3.8+，仅标准库；
    - 远端下载/回传依赖环境变量 GIT_COMMIT_TOKEN（由 GitHubCommitContent 读取）；
    - OBS 上传必需 HWC_OBS_ROOT_PREFIX（对象 key 前缀）；可选 HWC_OBS_CALLBACK_URL（回调端点）；
    - 同目录需存在：GitHubCommitContent.py、UpstreamFileList.py、OBSClient.py、
      Commit.json、Upstream.json。

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

# Upstream.json 中本流程仓库内路径配置键（仅路径类；OBS 前缀 / 回调端点走环境变量）
UPSTREAM_JSON = "Upstream.json"
JSON_KEY_UPLOAD_FILE_LIST_PATH = "UploadFileListPath"
JSON_KEY_BRANCH_CURRENT = "BranchCurrent"
# OBS 对象 key 前缀（必填）：原 Upstream.json 的 OBSRootPrefix，改环境变量注入
ENV_OBS_ROOT_PREFIX = "HWC_OBS_ROOT_PREFIX"
# 服务端原生回调端点（可选）：原 Upstream.json 的 CallbackUrl，改环境变量注入；
# 未设置则不回调（upload_file 不携带 x-obs-callback 头）
ENV_OBS_CALLBACK_URL = "HWC_OBS_CALLBACK_URL"
# CID 前缀路由：OBSCIDRoutes = [ {FilePrefix, CID}, ... ]，顺序即优先级（先配置先命中）
JSON_KEY_OBS_CID_ROUTES = "OBSCIDRoutes"
JSON_KEY_ROUTE_FILE_PREFIX = "FilePrefix"
JSON_KEY_ROUTE_CID = "CID"

# 成功清单文件名模式：UploadSuccessList_yyyyMMdd_HHmmssSSS_{MD5}.txt
REMOTE_BASE_DIR = "Branch"
SUCCESS_FILE_PREFIX = "UploadSuccessList"

# 本次 OBS 上传的“存储桶目标 key”：None = 不显式指定，由 OBSClient.upload_file
# 按规则自动构造（{OBSRootPrefix}/{运行当天yyyyMMdd}/3501806882199176893/{文件名}，
# 其中 OBSRootPrefix 由 Upstream.json 的 OBSRootPrefix 字段配置并随调用传入）；
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


def load_commit_identity(cfg_path=None):
    """读取 Commit.json 并校验目标仓库身份字段（owner / repo / branch_migration）

    :param cfg_path: Commit.json 路径；None = 同目录默认
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


def load_upstream_upload_settings(cfg_path=None):
    """读取 Upstream.json 的仓库内路径配置（含 CID 前缀路由）；OBS 前缀 / 回调端点
    不在本文件，改由 load_obs_runtime_env() 从环境变量注入

    :param cfg_path: Upstream.json 路径；None = 同目录默认
    :return: (settings, err)：成功时 settings 为
             dict{upload_file_list_path, branch_current, cid_routes}、err 为 None；
             失败时 settings 为 None、err 为失败原因
             cid_routes = [ {prefix, cid}, ... ]，顺序即优先级（先配置先命中）；
             未配置 OBSCIDRoutes 字段时为空列表（全部文件走默认 CID）
    """
    path = cfg_path or os.path.join(_SCRIPT_DIR, UPSTREAM_JSON)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return None, "读取 %s 失败: %s" % (path, e)
    except ValueError as e:
        return None, "%s 不是合法 JSON: %s" % (path, e)
    if not isinstance(data, dict):
        return None, "%s 顶层应为 JSON 对象" % path

    def grab(key):
        val = data.get(key)
        return str(val).strip() if val is not None else ""

    path_value = grab(JSON_KEY_UPLOAD_FILE_LIST_PATH)
    branch_current = grab(JSON_KEY_BRANCH_CURRENT)
    if not (path_value and branch_current):
        missing = [k for k, v in ((JSON_KEY_UPLOAD_FILE_LIST_PATH, path_value),
                                  (JSON_KEY_BRANCH_CURRENT, branch_current))
                   if not v]
        return None, "%s 缺少字段 %s（不得为空）" % (path, "、".join(missing))

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
            "cid_routes": cid_routes}, None


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

    :param message: 日志内容（可含 [INFO]/[PASS]/[FAIL] 等分级前缀）
    """
    print("[%s] %s" % (_now_tag(), message))


def parse_manifest_lines(text):
    """解析清单文本为“一行一个”的相对路径列表

    :param text: 清单全文
    :return: list[str]：每项为该行去除首尾空白后的相对路径；空行与全空白行忽略
    """
    text = (text or "").lstrip("﻿")  # 去掉可能的 BOM
    lines = []
    for line in text.splitlines():
        rel = line.strip().replace("\\", "/")
        if rel:
            lines.append(rel)
    return lines


def load_obs_runtime_env():
    """从环境变量读取 OBS 运行期配置（原 Upstream.json 的 OBSRootPrefix / CallbackUrl）

    前缀必填（构造对象 key 需要）；回调端点可选（未设置则上传不携带 x-obs-callback）。

    :return: (root_prefix, callback_url, err)：成功时 err 为 None、二者为字符串（可空）；
             前缀缺失时 root_prefix/callback_url 为 None、err 为失败原因字符串
    """
    root_prefix = os.environ.get(ENV_OBS_ROOT_PREFIX, "").strip()
    callback_url = os.environ.get(ENV_OBS_CALLBACK_URL, "").strip()
    if not root_prefix:
        return None, None, ("缺少环境变量 %s（OBS 对象 key 前缀，必填；"
                            "该值原为 Upstream.json 的 OBSRootPrefix，已移出配置文件）"
                            % ENV_OBS_ROOT_PREFIX)
    return root_prefix, callback_url, None


def main(cfg_commit_path=None, cfg_upstream_path=None, out_dir=None,
         read_fn=None, commit_fn=None, obs_fn=None):
    """主流程：下载上传清单 → 逐文件 OBS 上传 → 回传成功清单

    :param cfg_commit_path: Commit.json 路径；None = 同目录（便于测试注入）
    :param cfg_upstream_path: Upstream.json 路径；None = 同目录
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

    # ---- 1) 读取配置：Commit.json（身份 / 迁移分支）+ Upstream.json（路径）+ 环境变量（OBS）----
    identity, err = load_commit_identity(cfg_commit_path)
    if identity is None:
        _log("[FAIL] %s" % err)
        return 1
    owner, repo, branch_migration = (identity["owner"], identity["repo"],
                                     identity["branch_migration"])
    settings, err = load_upstream_upload_settings(cfg_upstream_path)
    if settings is None:
        _log("[FAIL] %s" % err)
        return 1
    upload_path = settings["upload_file_list_path"]
    branch_current = settings["branch_current"]
    cid_routes = settings["cid_routes"]
    root_prefix, callback_url, env_err = load_obs_runtime_env()
    if env_err is not None:
        _log("[FAIL] %s" % env_err)
        return 1
    _log("[INFO] 目标仓库 = %s/%s | 迁移分支 = %s（BranchMigration）| Branch 目录段 = %s"
          % (owner, repo, branch_migration, branch_current))
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
    lines = parse_manifest_lines(download.get("text"))
    dl_branch = download.get("branch") or branch_migration
    _log("[INFO] 上传清单下载自 GitHub：%s/%s 分支 %s"
          % (owner, repo, dl_branch))
    _log("[INFO]   ↳ 远程文件：https://github.com/%s/%s/blob/%s/%s（共 %d 行，"
          "每行为相对仓库根目录的路径）"
          % (owner, repo, dl_branch, upload_path, len(lines)))

    # ---- 3) 定位本地仓库根，逐行处理 ----
    root = _repo_root()
    if root is None:
        _log("[FAIL] 未能在仓库中找到 .git 入口，无法定位仓库根目录")
        return 1

    obs = obs_fn or OBSClient.upload_file
    ok_paths, missing_count, fail_count = [], 0, 0
    for rel_path in lines:
        local_file = os.path.join(root, rel_path)
        if not os.path.isfile(local_file):
            missing_count += 1
            _log("[跳过] 本地文件不存在: %s" % rel_path)
            continue
        # 计算回调口径哈希：md5（内容、大写 hex）；sha1 暂不计算，预留传 None
        with open(local_file, "rb") as f:
            digest_md5 = _md5_hex_upper(f.read())
        cid_value, _matched = resolve_cid(rel_path, cid_routes)
        result = obs(local_file=local_file, rel_path=rel_path, key=OBS_KEY_AUTO,
                     owner=owner, repo=repo, branch=branch_current,
                     sha1=None, md5=digest_md5, root_prefix=root_prefix,
                     cid=cid_value, callback_url=callback_url)
        if isinstance(result, dict) and result.get("success"):
            ok_paths.append(rel_path)
            _log("[上传成功] [%s] %s" % (cid_value, rel_path))
        else:
            fail_count += 1
            message = result.get("message") if isinstance(result, dict) else result
            _log("[上传失败] %s -> %s" % (rel_path, message))
    _log("[INFO] 汇总：共 %d 个 | 成功 %d | 本地缺失 %d | 上传失败 %d"
          % (len(lines), len(ok_paths), missing_count, fail_count))

    # ---- 4) 无成功文件：正常结束（无需回传成功清单）----
    if not ok_paths:
        _log("[INFO] 无上传成功的文件，跳过回传 UploadSuccessList")
        return 0

    # ---- 5) 成功清单一行一个写本地临时 txt，并回传远端 ----
    content = "\n".join(ok_paths) + "\n"
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
        result = commit(path_key, local_txt, branch=branch_migration,
                        commit_msg="UploadSuccessList @%s" % _build_timestamp())
    finally:
        try:
            os.remove(local_txt)
        except OSError:
            pass

    if isinstance(result, dict) and result.get("success"):
        _log("[PASS] 成功清单已回传 %s/%s 分支 %s：%s（http_status=%s）"
              % (owner, repo, branch_migration, path_key, result.get("http_status")))
        return 0
    message = result.get("message") if isinstance(result, dict) else result
    _log("[FAIL] 成功清单回传失败: %s" % message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
