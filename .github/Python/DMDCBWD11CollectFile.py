#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMDCBWD11CollectFile —— 编排：收集文件清单 → 写本地 txt → 上传远端 BranchMigration 分支
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
在不 clone 仓库的前提下，把“仓库内数据文件的清单（manifest）”上传到 GitHub
仓库的指定分支，让上游侧拿到一份“本仓库当前有哪些数据文件”的快照。整个流程三步：

    1. 调用同目录 UpstreamFileList.collect_with_digests() 获取可上传文件清单，
       每个文件附带【内容】的 SHA1 / MD5（大写 hex）；再取该文件在 git 中的
       最后提交时间（见下）；
    2. 将清单“一行一个 JSON 对象”写入本地临时 jsonl 文件（UTF-8、LF 换行）：

           {"dt":"20260912124805000","sha1":"8A38…","md5":"07BE…","rel_path":"Data/Demo.json"}

       - dt：该路径在 git 中最后一次被提交的时间，格式 yyyyMMddHHmmssSSS、
         东八区（GMT+8）；稳定值——文件内容不变则同一提交下该字段不变，故
         (dt, sha1, md5, rel_path) 可用于「同一文件同一版本」的唯一标识与增量去重。
         （切勿改用文件系统 mtime：每次 clone/checkout 都会刷新，会让清单天天变。）
       - sha1 / md5：文件【内容】的哈希，大写 hex；
       - rel_path：相对仓库根的路径，分隔符统一为正斜杠。

       四键恒存在（取值未知写空串），键序固定；格式的权威实现在同目录
       ManifestJsonl.py —— 该模块对未知键原样透传，故**将来新增字段只需改本脚本**，
       中间的解析/聚合环节无需跟着改。

    3. 调用同目录 GitHubCommitContent.commit_content_file 上传该 jsonl 到本仓库的
       {BranchMigration} 分支（取自 Commit.json，见下）；远端落点 path_key 固定模式：

           Branch/{BranchCurrent}/UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.jsonl

       - BranchCurrent：当前 git 检出的分支名（远端路径 Branch/... 中的“当前分支”目录段）；
       - yyyyMMdd_HHmmssSSS：本地生成时间（毫秒 3 位）；
       - MD5：清单 jsonl 文件内容的 MD5（UTF-8 编码、大写 hex），同内容再跑会生成相同 MD5。

配置来源（都是【仓库级 / 运行期】事实，不再往分支配置里抄副本）：

    Commit.json —— {BranchMigration} 取自它。该文件登记的是仓库身份与各流程共用的
    回传分支，与当前分支无关，权威副本只有 dev 分支上的一份，运行时经
    GitHubCommitContent.load_commit_config 取回（取不到才回退本地同目录副本）：

        { "Owner": "...", "Repo": "...", "BranchMigration": "Migration", ... }

    Migration.{BranchCurrent}.json —— 分支配置文件，文件名中的 {BranchCurrent} 随当前
    git 分支动态解析（见 resolve_config_path），在 quote 分支上即读 Migration.quote.json：

        { "UploadFileListPath": "Branch/quote/UploadFileList.jsonl", ... }

    该文件不再登记 BranchCurrent / BranchMigration / TargetBranch：BranchCurrent 与
    当前分支必然相同（写进文件反而可能与实际检出不一致），BranchMigration 与
    Commit.json 重复，TargetBranch 无任何代码读取。残留 BranchCurrent 时忽略并告警。

分支配置文件缺失 / 非法 / 缺少 UploadFileListPath，当前分支判定不出（游离 HEAD）或不在
Git 仓库内时一律失败退出——不落到任何默认分支，避免误传到意外分支（与
GitHubCommitContentDemo 的“无默认分支”约定一致）。

【环境要求】
    - Python 3.8+，仅标准库；
    - 真实上传依赖环境变量 GIT_COMMIT_TOKEN（由 GitHubCommitContent 读取）；
    - 同目录需存在：UpstreamFileList.py（提供 collect）、GitHubCommitContent.py
      （提供 commit_content_file）、ManifestJsonl.py（清单格式实现）、
      Commit.json（远端权威副本 + 本地回退副本）、Migration.{当前分支}.json（分支配置）；
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

import GitHubCommitContent  # noqa: E402
import ManifestJsonl        # noqa: E402
from UpstreamFileList import collect_with_digests, _repo_root  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

# 分支配置文件（与本文件同目录）命名约定：Migration.{BranchCurrent}.json
# 文件名中的 {BranchCurrent} **随当前 git 分支动态解析**（见 resolve_config_path）——
# 同一份脚本原样放到 quote / quote-gold / … 都能加载该分支自己的配置，各分支因此
# 无需各自维护一份 DMDCBWD11CollectFile.py 副本；两份配置在合并时也不会冲突。
CONFIG_NAME_TEMPLATE = "Migration.%s.json"

# 历史字段：BranchCurrent / BranchMigration / TargetBranch 均已废弃 —— BranchCurrent
# 改由当前 git 分支动态取得，BranchMigration 取自 Commit.json（取值与它完全重复），
# TargetBranch 无任何代码读取。文件中若仍残留 BranchCurrent，忽略并告警。
JSON_KEY_BRANCH_CURRENT_LEGACY = "BranchCurrent"

# 分支配置中“待上传清单文件的仓库内路径”键。本脚本不消费它（消费方是
# DMDCBWD31MigrationFile），只借它的目录段核对远端落点，防止配置文件与当前分支错配。
JSON_KEY_UPLOAD_FILE_LIST_PATH = "UploadFileListPath"

# 远端目录基名：Branch/{current_branch}/
REMOTE_BASE_DIR = "Branch"

# 清单文件名前缀：UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.jsonl
# （内容为 JSONL；扩展名随格式而定，与 ManifestJsonl 的格式约定配套）
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


def current_branch(start_dir=None):
    """取当前工作树所在的分支名（即 BranchCurrent）

    这是“当前分支”的唯一事实来源 —— 配置文件路径与远端路径目录段都由它推出，
    不再写进配置文件，否则又多出一份可能与实际检出的分支不一致的副本。
    （与 DMDCBWD31MigrationFile.current_branch 为同一份实现的镜像，两脚本各自
    独立、不互相 import。）

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


def load_branch_config(cfg_path=None, start_dir=None):
    """读取分支配置文件（Migration.{当前分支}.json）的 JSON 对象

    文件名按当前 git 分支动态解析；文件缺失 / 非合法 JSON / 顶层不是对象均判定失败，
    不提供任何默认值。必需字段由调用方校验（见 main）。

    :param cfg_path: 配置文件路径；None = 按约定解析
    :param start_dir: 判定当前分支的起始目录；None = 本脚本所在目录
    :return: (config, path, branch, err)：成功时 config 为完整 JSON dict、err 为 None；
             失败时 config 为 None、err 为失败原因字符串
    """
    path, branch, err = resolve_config_path(cfg_path, start_dir)
    if path is None:
        return None, None, None, "解析分支配置文件路径失败: %s" % err
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except OSError as e:
        return None, path, branch, "读取 %s 失败: %s（分支 %s 的配置文件按约定名为 %s）" % (
            path, e, branch, CONFIG_NAME_TEMPLATE % branch)
    except ValueError as e:
        return None, path, branch, "%s 不是合法 JSON: %s" % (path, e)
    if not isinstance(cfg, dict):
        return None, path, branch, "%s 顶层应为 JSON 对象" % path
    return cfg, path, branch, None


def make_file_name(timestamp, digest):
    """按 UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.jsonl 模式拼接清单文件名

    :param timestamp: 时间戳串，形如 "20260909_143000123"（由 build_timestamp 生成）
    :param digest: 清单内容的 MD5（小写 hex）
    :return: 文件名，如 "UploadFileList_20260909_143000123_abc....jsonl"
    """
    return "%s_%s_%s.jsonl" % (FILE_NAME_PREFIX, timestamp, digest)


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

    结果写入清单的 dt 字段（见 compose_manifest_content）。

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
    """把文件清单拼成完整清单文本（JSONL，UTF-8、LF、末行带换行）

    每行一个 JSON 对象，键序固定 dt → sha1 → md5 → rel_path：

        {"dt":"20260912124805000","sha1":"8A38…","md5":"07BE…","rel_path":"Data/Demo.json"}

    格式细节（含四键恒存在、未知键透传、确定性序列化）见同目录 ManifestJsonl.py。

    :param entries: dict 列表，每项需含 rel_path / sha1 / md5 / dt 四个键
                    （前三个来自 UpstreamFileList.collect_with_digests，
                      dt 来自 git_last_modified_times）
    :return: 待写入 jsonl 的完整文本；entries 为空返回空字符串
    """
    return ManifestJsonl.compose(entries)


def write_local_file(local_file, content):
    """把清单文本写入本地 jsonl（UTF-8、LF 换行）

    :param local_file: 本地文件绝对路径
    :param content: 待写入文本
    :return: 无；写入失败抛 OSError（由调用方决定如何处理）
    """
    with open(local_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def main(cfg_path=None, out_dir=None, commit_fn=None, commit_msg=None):
    """主流程：收集清单 → 写本地 jsonl → 上传到远端 BranchMigration 分支

    :param cfg_path: 分支配置 Migration.{当前分支}.json 的路径；None = 按当前分支解析
    :param out_dir: 本地临时 jsonl 输出目录；None = 系统临时目录
    :param commit_fn: 上传函数，签名同 commit_content_file(path_key, local_file,
                      branch=...)；None = GitHubCommitContent.commit_content_file
    :param commit_msg: 提交说明；None = 由 GitHubCommitContent 生成默认提交说明
    :return: 退出码：0 = 成功 / 清单为空正常跳过；1 = 任一环节失败
    """
    _ensure_console_utf8()

    # ---- 1) 解析分支配置 + 解析上传目标分支（无默认值，失败即中止）----
    cfg, cfg_file, current_branch_name, cfg_err = load_branch_config(cfg_path)
    if cfg is None:
        print("[FAIL] %s" % cfg_err)
        return 1

    # 历史字段 BranchCurrent：已改为按当前 git 分支动态取得，残留则忽略并告警
    if JSON_KEY_BRANCH_CURRENT_LEGACY in cfg:
        print("[WARN] %s 中的 %s 已废弃并被忽略：BranchCurrent 现由当前 git 分支"
              "动态取得（本次 = %s）"
              % (cfg_file, JSON_KEY_BRANCH_CURRENT_LEGACY, current_branch_name))

    list_path = cfg.get(JSON_KEY_UPLOAD_FILE_LIST_PATH)
    list_path = str(list_path).strip() if list_path is not None else ""
    if not list_path:
        print("[FAIL] %s 缺少字段 %s（不得为空）" % (cfg_file, JSON_KEY_UPLOAD_FILE_LIST_PATH))
        return 1

    # 上传目标分支（BranchMigration）取自 Commit.json：它与仓库身份一样是【仓库级】
    # 事实，登记在本分支配置里纯属重复；权威副本在 dev 分支，运行时取回
    commit_cfg = GitHubCommitContent.load_commit_config()
    target_branch = (commit_cfg or {}).get("branch_migration") or ""
    # owner / repo 仅供日志末尾拼可点链接用（上传本身的身份解析由 commit_content_file 自己做）
    owner = (commit_cfg or {}).get("owner") or ""
    repo = (commit_cfg or {}).get("repo") or ""
    if not target_branch:
        print("[FAIL] Commit.json 缺少字段 BranchMigration（不得为空，避免误传）")
        return 1

    remote_dir = "%s/%s" % (REMOTE_BASE_DIR, current_branch_name)
    print("[INFO] 当前分支（BranchCurrent）= %s（取自当前 git 检出）" % current_branch_name)
    print("[INFO] 上传目标分支（BranchMigration）= %s（取自 Commit.json）" % target_branch)
    print("[INFO] 分支配置 = %s" % cfg_file)
    # 配置里的 UploadFileListPath 决定 DMDCBWD31 从哪儿取清单，其目录段必须与本次
    # 落点同目录，否则是配置文件与当前分支错配（如合并后残留另一分支的配置）
    if os.path.dirname(list_path) != remote_dir:
        print("[WARN] 分支配置的 %s = %s，其目录段与本次落点目录 %s/ 不一致；"
              "请核对该文件是否属于当前分支"
              % (JSON_KEY_UPLOAD_FILE_LIST_PATH, list_path, remote_dir))

    # ---- 2) 收集文件清单（相对路径 + 内容 SHA1/MD5）----
    entries = collect_with_digests()
    if not entries:
        print("[INFO] 无可上传文件，正常跳过（未生成清单、未上传）")
        return 0

    # ---- 2b) 取每个文件的 git 最后提交时间（稳定值，清单条目唯一性依赖它）----
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
        entry["dt"] = mtime_map.get(entry["rel_path"], "")
    print("[INFO] 收集到 %d 个数据文件（dt 取 git 最后提交时间；"
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
    path_key = "%s/%s" % (remote_dir, file_name)
    print("[INFO] 远端落点 = %s（分支 %s）" % (path_key, target_branch))

    # ---- 4) 清单写入本地临时 jsonl ----
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
        upload = commit_fn or GitHubCommitContent.commit_content_file
        result = upload(path_key, local_file, branch=target_branch,
                        commit_msg=commit_msg)
    finally:
        try:
            os.remove(local_file)
        except OSError:
            pass

    if isinstance(result, dict) and result.get("success"):
        status = result.get("http_status")
        # 末尾附可点链接，便于从运行日志直接跳到上传结果；
        # 身份取不到时不打半截链接（宁可没有，也不给一个点不开的 URL）
        link = ("  https://github.com/%s/%s/blob/%s/%s"
                % (owner, repo, target_branch, path_key)) if (owner and repo) else ""
        print("[PASS] 上传成功，http_status=%s（201=新建 / 200=更新）%s" % (status, link))
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
