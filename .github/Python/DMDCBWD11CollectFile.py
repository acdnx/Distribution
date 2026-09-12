#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMDCBWD11CollectFile —— 编排：收集文件清单 → 写本地 txt → 上传远端 BranchMigration（Migration）分支
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
在不 clone 仓库的前提下，把“仓库内数据文件的清单（manifest）”上传到 GitHub
仓库的指定分支，让上游侧拿到一份“本仓库当前有哪些数据文件”的快照。整个流程三步：

    1. 调用同目录 UpstreamFileList.collect_with_digests() 获取可上传文件清单，
       每个文件附带【内容】的 SHA1 / MD5（大写 hex）；再取该文件在 git 中的
       最后提交时间（见下）；
    2. 将清单“一行一个”写入本地临时 txt 文件（UTF-8、LF 换行），每行为四元组：

           {最后修改时间}|{SHA1}|{MD5}|{相对路径}

       - 最后修改时间：该路径在 git 中最后一次被提交的时间，格式
         yyyyMMddHHmmssSSS、东八区（GMT+8）；稳定值——文件内容不变则同一提交
         下该字段不变，故四元组可用于「同一文件同一版本」的唯一标识与增量去重。
         （切勿改用文件系统 mtime：每次 clone/checkout 都会刷新，会让四元组天天变。）
       - SHA1 / MD5：文件【内容】的哈希，大写 hex；
       - 相对路径：放最后一位，便于解析时“从左按 | 切分、剩余整体作为路径”，
         即使路径中含 | 也不会错位。

    3. 调用同目录 GitHubCommitContent.commit_content_file 上传该 txt 到本仓库的
       {BranchMigration} 分支（默认 "Migration"）；远端落点 path_key 固定模式：

           Branch/{BranchCurrent}/UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.txt

       - BranchCurrent：Upstream.json 中定义的“当前分支”目录段（可自行维护）；
       - yyyyMMdd_HHmmssSSS：本地生成时间（毫秒 3 位）；
       - MD5：清单 txt 文件内容的 MD5（UTF-8 编码、大写 hex），同内容再跑会生成相同 MD5。

{BranchMigration} 与 {BranchCurrent} 均取自同目录 Migration.{BranchCurrent}.json：

    {
      "TargetBranch":    "quote",      // （历史字段，其它脚本在用，保留不动）
      "BranchMigration": "Migration", // 迁移文件驻留/上传目标远端分支（默认 Migration）
      "BranchCurrent":   "quote"        // 远端路径 Branch/... 中的“当前分支”目录段
    }

字段缺失 / 为空 / 文件非法 / 不在 Git 仓库内时一律失败退出——不落到任何默认分支，
避免误传到意外分支（与 GitHubCommitContentDemo 的“无默认分支”约定一致）。

【环境要求】
    - Python 3.8+，仅标准库；
    - 真实上传依赖环境变量 GIT_COMMIT_TOKEN（由 GitHubCommitContent 读取）；
    - 同目录需存在：UpstreamFileList.py（提供 collect）、GitHubCommitContent.py
      （提供 commit_content_file）、Upstream.json（分支配置）；
    - 退出码：0 = 成功或清单为空正常跳过；1 = 任一环节失败。

二、运行方式
----------------------------------------------------------------------------------------
    # 与本文件同目录，直接执行（先注入令牌才会真实上传）：
    $env:GIT_COMMIT_TOKEN = "ghp_你的Token"
    python3 DMDCBWD11CollectFile.py

    # 或 import 调用：
    import DMDCBWD11CollectFile
    code = DMDCBWD11CollectFile.main()      # 0=成功/无清单，1=失败
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

from GitHubCommitContent import commit_content_file  # noqa: E402
from UpstreamFileList import collect_with_digests, _repo_root  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

# 分支配置文件（与本文件同目录）
UPSTREAM_JSON = "Migration.quote.json"

# Upstream.json 中“迁移文件驻留 / 上传目标远端分支”字段名（统一为 BranchMigration，
# 默认 "Migration"；原命名 BranchUpstream / UpstreamBranch 已废弃，勿再使用）
JSON_KEY_BRANCH_MIGRATION = "BranchMigration"

# Upstream.json 中“当前分支目录段（Branch/{BranchCurrent} 用）”字段名
# （你消息里写作 BrenchCurrent，按与 BranchMigration 相同的口径修正为 BranchCurrent；
#   如需保留原拼写，改这里与 Upstream.json 对应键即可）
JSON_KEY_CURRENT_BRANCH = "BranchCurrent"

# 远端目录基名：Branch/{current_branch}/
REMOTE_BASE_DIR = "Branch"

# 清单文件名前缀：UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.txt
FILE_NAME_PREFIX = "UploadFileList"


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


def load_branches_from_json(cfg_path=None):
    """从 Upstream.json 解析 (upstream_branch, current_branch)

    文件缺失 / 非合法 JSON / 顶层不是对象 / 任一枚举键缺失或为空均判定失败，
    不提供任何默认分支值。

    :param cfg_path: Upstream.json 路径；None = 与本脚本同目录的 Upstream.json
    :return: (config, err)：成功时 config 为完整 JSON dict、err 为 None；
             失败时 config 为 None、err 为失败原因字符串
    """
    path = cfg_path or os.path.join(_SCRIPT_DIR, UPSTREAM_JSON)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except OSError as e:
        return None, "读取 %s 失败: %s" % (path, e)
    except ValueError as e:
        return None, "%s 不是合法 JSON: %s" % (path, e)
    if not isinstance(cfg, dict):
        return None, "%s 顶层应为 JSON 对象" % path
    return cfg, None


def _require_str_field(cfg, key):
    """取出配置中指定键的非空字符串值；缺失或为空返回 None"""
    value = cfg.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def make_file_name(timestamp, digest):
    """按 UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.txt 模式拼接清单文件名

    :param timestamp: 时间戳串，形如 "20260909_143000123"（由 build_timestamp 生成）
    :param digest: 清单内容的 MD5（小写 hex）
    :return: 文件名，如 "UploadFileList_20260909_143000123_abc....txt"
    """
    return "%s_%s_%s.txt" % (FILE_NAME_PREFIX, timestamp, digest)


def build_timestamp(now=None):
    """生成清单文件名用的时间戳 yyyyMMdd_HHmmssSSS（含毫秒 3 位）

    :param now: datetime 对象；None = 当前本地时间（便于测试注入）
    :return: 时间戳串，如 "20260909_143000123"
    """
    dt = now or datetime.datetime.now()
    return dt.strftime("%Y%m%d_%H%M%S") + "%03d" % (dt.microsecond // 1000)


# git log 输出的提交起始标记（%x01 + 该提交时间戳秒），用于与文件名行区分。
# 用不可见控制字符做前缀，避免与真实文件名混淆。
_GIT_COMMIT_MARK = "\x01"

# 东八区（GMT+8）：清单里所有时间戳统一按此时区格式化
_TZ_GMT8 = datetime.timezone(datetime.timedelta(hours=8))


def _fmt_gmt8(epoch):
    """epoch 秒（可含小数）→ 东八区 yyyyMMddHHmmssSSS

    git 提交时间只有秒级精度，故毫秒位恒为 000；文件 mtime 回退路径则带真实毫秒。

    :param epoch: Unix 时间戳（秒，int 或 float）
    :return: 形如 "20260912235959123" 的 17 位字符串
    """
    dt = datetime.datetime.fromtimestamp(float(epoch), _TZ_GMT8)
    return dt.strftime("%Y%m%d%H%M%S") + "%03d" % (dt.microsecond // 1000)


def git_last_modified_times(rel_paths, repo_root):
    """取每个路径在 git 中的“最后修改时间”（该路径最后一次被提交的时间）

    单次遍历建好映射，不逐文件调用 git：

        git -c core.quotePath=false log --pretty=format:%x01%ct --name-only \\
            --diff-filter=AMR

    log 由新到旧输出，故某路径【首次】出现所在的那条提交即其最后一次修改；把重命名（R）
    也计入，使改名后的新路径取其改名提交的时间（旧路径已不存在，不受影响）。

    时间戳统一按东八区（GMT+8）格式化为 yyyyMMddHHmmssSSS；git 提交时间仅秒级精度，
    毫秒位恒为 000（保留三位是为了与清单文件名的时间戳口径一致）。

    未提交（untracked）的文件在 git 中查不到，回退取该文件的 mtime，并在返回值中单独
    计数——这类文件本就处于未落定状态，不保证跨环境一致。

    :param rel_paths: 相对仓库根的路径集合（通常来自 UpstreamFileList.collect_with_digests）
    :param repo_root: 仓库根目录绝对路径（用于回退取 mtime）
    :return: (mapping, fallback_count)：
             mapping = {rel_path: "yyyyMMddHHmmssSSS"}（覆盖全部入参路径）；
             fallback_count = 回退为 mtime 的路径数
    :raises RuntimeError: git 不可用、当前目录不在 Git 仓库内，或 git log 执行失败。
                          此时时间戳无法保证稳定，宁可失败也不产出不可信清单。
    """
    wanted = set(rel_paths)
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", "log",
             "--pretty=format:%x01%ct", "--name-only", "--diff-filter=AMR"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise RuntimeError("无法执行 git（%s）；本流程需要 git 以取得稳定的最后修改时间" % e)
    if proc.returncode != 0:
        raise RuntimeError("git log 执行失败：%s"
                           % (proc.stderr or "非 Git 仓库或 git 不可用").strip())

    mapping = {}
    current = None
    for line in proc.stdout.splitlines():
        if line.startswith(_GIT_COMMIT_MARK):
            current = line[len(_GIT_COMMIT_MARK):].strip()
        elif line and current and line in wanted and line not in mapping:
            mapping[line] = _fmt_gmt8(current)

    fallback_count = 0
    for rel_path in sorted(wanted):
        if rel_path in mapping:
            continue
        try:
            mapping[rel_path] = _fmt_gmt8(os.path.getmtime(
                os.path.join(repo_root, rel_path)))
        except OSError:
            mapping[rel_path] = _fmt_gmt8(0)
        fallback_count += 1
    return mapping, fallback_count


def compose_manifest_content(entries):
    """把文件清单拼成“一行一个”的文本（UTF-8，LF 换行，末行带换行）

    每行为四元组：{最后修改时间}|{SHA1}|{MD5}|{相对路径}
    路径放最后一位，便于解析时“从左按 | 切分、剩余整体作为路径”。

    :param entries: dict 列表，每项需含 rel_path / sha1 / md5 / mtime 四个键
                    （前三个来自 UpstreamFileList.collect_with_digests，
                      mtime 来自 git_last_modified_times）
    :return: 待写入 txt 的完整文本；entries 为空返回空字符串
    """
    if not entries:
        return ""
    lines = ["%s|%s|%s|%s" % (e["mtime"], e["sha1"], e["md5"], e["rel_path"])
             for e in entries]
    return "\n".join(lines) + "\n"


def write_local_file(local_file, content):
    """把清单文本写入本地 txt（UTF-8、LF 换行）

    :param local_file: 本地文件绝对路径
    :param content: 待写入文本
    :return: 无；写入失败抛 OSError（由调用方决定如何处理）
    """
    with open(local_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def main(cfg_path=None, out_dir=None, commit_fn=None, commit_msg=None):
    """主流程：收集清单 → 写本地 txt → 上传到远端 BranchMigration 分支

    :param cfg_path: Upstream.json 路径；None = 与本脚本同目录（便于测试注入）
    :param out_dir: 本地临时 txt 输出目录；None = 系统临时目录
    :param commit_fn: 上传函数，签名同 commit_content_file(path_key, local_file,
                      branch=...)；None = GitHubCommitContent.commit_content_file
    :param commit_msg: 提交说明；None = 由 GitHubCommitContent 生成默认提交说明
    :return: 退出码：0 = 成功 / 清单为空正常跳过；1 = 任一环节失败
    """
    _ensure_console_utf8()

    # ---- 1) 解析分支配置（无默认值，失败即中止）----
    cfg, cfg_err = load_branches_from_json(cfg_path)
    if cfg is None:
        print("[FAIL] %s" % cfg_err)
        return 1
    target_branch = _require_str_field(cfg, JSON_KEY_BRANCH_MIGRATION)
    current_branch = _require_str_field(cfg, JSON_KEY_CURRENT_BRANCH)
    if not target_branch or not current_branch:
        missing = [k for k, v in ((JSON_KEY_BRANCH_MIGRATION, target_branch),
                                  (JSON_KEY_CURRENT_BRANCH, current_branch))
                   if not v]
        print("[FAIL] %s 缺少字段 %s（不得为空，避免误传）"
              % (cfg_path or os.path.join(_SCRIPT_DIR, UPSTREAM_JSON),
                 "、".join(missing)))
        return 1
    print("[INFO] 上传目标分支 = %s | 远端路径分支目录 = %s"
          % (target_branch, current_branch))

    # ---- 2) 收集文件清单（相对路径 + 内容 SHA1/MD5）----
    entries = collect_with_digests()
    if not entries:
        print("[INFO] 无可上传文件，正常跳过（未生成清单、未上传）")
        return 0

    # ---- 2b) 取每个文件的 git 最后提交时间（稳定值，四元组唯一性依赖它）----
    root = _repo_root()
    if root is None:
        print("[FAIL] 未能在仓库中找到 .git 入口，无法定位仓库根目录")
        return 1
    try:
        mtime_map, fallback = git_last_modified_times(
            [e["rel_path"] for e in entries], root)
    except RuntimeError as e:
        print("[FAIL] %s" % e)
        return 1
    for entry in entries:
        entry["mtime"] = mtime_map.get(entry["rel_path"], "")
    print("[INFO] 收集到 %d 个数据文件（时间戳取 git 最后提交时间；"
          "其中 %d 个未提交、已回退为文件 mtime）" % (len(entries), fallback))

    # ---- 3) 组装清单文本、MD5、文件名与远端路径 ----
    content = compose_manifest_content(entries)
    print("[INFO] 清单内容 %d 行 / %d 字节，前 3 行示例："
          % (len(entries), len(content.encode("utf-8"))))
    for line in content.splitlines()[:3]:
        print("[INFO]   %s" % line)
    digest = hashlib.md5(content.encode("utf-8")).hexdigest().upper()
    timestamp = build_timestamp()
    file_name = make_file_name(timestamp, digest)
    path_key = "%s/%s/%s" % (REMOTE_BASE_DIR, current_branch, file_name)
    print("[INFO] 远端落点 = %s（分支 %s）" % (path_key, target_branch))

    # ---- 4) 清单写入本地临时 txt ----
    out = out_dir or tempfile.gettempdir()
    local_file = os.path.join(out, file_name)
    try:
        write_local_file(local_file, content)
    except OSError as e:
        print("[FAIL] 写入本地临时文件失败: %s" % e)
        return 1
    print("[INFO] 本地临时清单 = %s" % local_file)

    # ---- 5) 上传（完成后无论成败都清理本地临时文件）----
    try:
        upload = commit_fn or commit_content_file
        result = upload(path_key, local_file, branch=target_branch,
                        commit_msg=commit_msg)
    finally:
        try:
            os.remove(local_file)
        except OSError:
            pass

    if isinstance(result, dict) and result.get("success"):
        status = result.get("http_status")
        print("[PASS] 上传成功，http_status=%s（201=新建 / 200=更新）" % status)
        return 0
    if isinstance(result, dict):
        message = result.get("message")
        status = result.get("http_status")
        status_prefix = "HTTP %s: " % status if status is not None else ""
    else:
        message, status_prefix = result, ""
    print("[FAIL] 上传失败: %s%s" % (status_prefix, message))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
