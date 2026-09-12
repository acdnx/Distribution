#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UpstreamFileList —— 扫描仓库收集上游数据文件相对路径清单（纯标准库，无第三方依赖）
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
在不 clone / 不依赖 git 命令的前提下，扫描“本脚本所在的 Git 仓库”工作树，收集所有以
指定扩展名结尾的文件，返回它们【相对于仓库根目录】的相对路径清单（List[str]）。
典型用途：为后续的批量上传 / GitHub Contents API 提交 / 元数据统计提供一份稳定的
文件清单（manifest）。

形态说明：
    - 本文件是纯函数库，没有破坏性动作；直接执行仅打印清单，可放心 import / 试运行；
    - import 时不发起任何网络请求、无全局副作用，可放心在脚本头部导入；
    - 公开函数：collect（返回相对路径清单）、_repo_root（定位仓库根）；
    - 纯标准库实现（os / sys），可直接拷到任何 Python 3.8+ 环境使用。

二、快速上手：同目录脚本调用示例
----------------------------------------------------------------------------------------
以下示例均假设调用脚本与 UpstreamFileList.py 位于同一目录，因此可直接 import：

【示例 1：收集全部数据文件相对路径（最常见）】

    import UpstreamFileList

    paths = UpstreamFileList.collect()  # list[str]，元素为相对仓库根目录的路径（“/”分隔）
    for p in paths:
        print(p)

【示例 2：直接 import 模块函数（等价写法）】

    from UpstreamFileList import collect

    paths = collect()
    print("共 %d 个文件" % len(paths))

【示例 3：指定额外扩展名 / 指定其它仓库根】

    paths = UpstreamFileList.collect(extensions=(".json", ".txt"))  # 覆盖默认后缀清单
    paths = UpstreamFileList.collect(repo_root="D:/other/repo")     # 显式指定扫描根目录

【示例 4：连同每个文件的内容哈希一起收集（供清单写成 JSONL 用）】

    entries = UpstreamFileList.collect_with_digests()
    # list[dict]，每项 {"rel_path": ..., "sha1": ..., "md5": ...}，哈希为大写 hex
    for e in entries:
        print("%s|%s|%s" % (e["sha1"], e["md5"], e["rel_path"]))

三、采集语义
----------------------------------------------------------------------------------------
    - 默认后缀清单见模块常量 DATA_EXTENSIONS = (".json", ".jsonl", ".mvsv")，
      匹配时忽略大小写（xxx.JSON 也命中）；
    - 路径以【仓库根目录】为基准返回相对路径，统一用 “/” 分隔（与 Git/GitHub 路径
      约定一致），返回前默认按字典序排序，保证清单稳定、可 diff；
    - 默认排除隐藏目录（以 "." 开头，如 .github / .git / .idea 等）下的文件，避免把
      CI 配置、仓库元数据等非数据文件收进清单；.git 与 __pycache__ 无论何种取值都
      始终跳过；可通过参数 exclude_hidden=False 关闭隐藏目录排除，或用参数
      skip_dirs 追加要跳过的目录名；
    - “仓库”指本脚本所在仓库（以脚本目录为起点逐级向上找 .git 入口）：
        - .git 是目录 → 其所在目录即仓库根；
        - .git 是文件（git worktree / submodule）→ 该 .git 文件所在目录即仓库根
          （与 GitHubCommitContent.load_owner_repo_from_git_config 的定位思路一致，
           跟随本文件所在仓库，与调用方 cwd 无关）；
    - 扫描的是磁盘工作树现状：包含未跟踪/未提交的新文件；如需仅统计 git 已跟踪文件，
      请改用 git ls-files 方案，本工具不做区分。

【环境要求】
    - Python 3.8+，仅标准库，无需 git 命令、无网络请求。

四、注意事项
----------------------------------------------------------------------------------------
    - 返回的是字符串 List，元素为相对路径（形如 "UpStream/Archive/20260825/xx.json"），
      如需绝对路径可用 os.path.join(repo_root, rel) 还原（Windows 下 “/” 同样可 open）；
    - 仓库中找不到 .git 入口（不在任何 Git 仓库内）时返回空列表，不抛异常；
    - 默认已排除隐藏目录（.github 等），故仓库自身的 *.json 配置文件
      （如 .github/Python/Migration.{当前分支}.json）不会进清单；若要采集隐藏目录下的文件，
      请以 exclude_hidden=False 调用（.git 仍会被跳过）。
"""

import hashlib
import os
import sys

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

# 需要采集的数据文件扩展名（小写；匹配时忽略大小写）。
# 说明：默认按需求收集 .json / .jsonl / .mvsv 三类；可通过 collect 参数覆盖。
DATA_EXTENSIONS = (".json", ".jsonl", ".mvsv")

# 遍历时始终跳过的目录名（无论是否开启隐藏目录排除）
# 注意：.git 属隐藏目录，本会被"隐藏目录一律跳过"覆盖；此处保留为显式兜底。
DEFAULT_SKIP_DIRS = (".git", "__pycache__")

# 计算文件内容哈希（file_digests / collect_with_digests）时的分块大小：1 MiB。
# 分块读取使内存占用与文件大小无关，可安全处理大文件。
DIGEST_CHUNK_SIZE = 1 << 20


def _script_dir():
    """返回本脚本所在目录（绝对路径）"""
    return os.path.dirname(os.path.abspath(__file__))


def _repo_root(start_dir=None):
    """定位 Git 仓库工作树根目录（以 start_dir 为起点逐级向上找 .git 入口）

    :param start_dir: 起始查找目录（绝对路径）；None = 本脚本所在目录
    :return: 仓库工作树根目录绝对路径；找不到 .git 入口返回 None（不抛异常）
    """
    d = os.path.abspath(start_dir) if start_dir else _script_dir()
    while True:
        entry = os.path.join(d, ".git")
        # .git 目录或 .git 文件（worktree/submodule 形态）所在目录即工作树根
        if os.path.isdir(entry) or os.path.isfile(entry):
            return d
        parent = os.path.dirname(d)
        if parent == d:  # 已到文件系统根
            return None
        d = parent


def _normalize_extensions(extensions):
    """把扩展名清单规整为小写、带前导点、无尾随点的元组

    :param extensions: 扩展名可迭代对象（可为 None）；每项可带或不带前导点，忽略大小写
    :return: 规整后的小写扩展名元组，如 ('.json', '.jsonl', '.mvsv')
    """
    if not extensions:
        extensions = DATA_EXTENSIONS
    cleaned = []
    for ext in extensions:
        s = str(ext).strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        s = s.rstrip(".")  # 容忍 ".mvsv." 之类的多余尾随点
        if s != "." and s not in cleaned:
            cleaned.append(s)
    return tuple(cleaned)


def collect(repo_root=None, extensions=None, skip_dirs=None, exclude_hidden=True):
    """收集仓库下所有以指定扩展名结尾文件的仓库相对路径清单

    以 repo_root（缺省时自动定位本脚本所在仓库的根目录）为根递归扫描，
    返回所有扩展名命中 DATA_EXTENSIONS（或 extensions）的文件相对路径；
    结果默认按字典序排序，路径统一用 “/” 分隔。

    默认排除隐藏目录（以 "." 开头，如 .github / .git / .idea 等）下的文件，
    避免把 CI 配置、仓库元数据等非数据文件收进“可上传清单”；可用
    exclude_hidden=False 关闭该行为（此时 .git 仍会被跳过）。

    :param repo_root: 仓库根目录（绝对路径）；None = 自动定位本脚本所在仓库根
    :param extensions: 需匹配的扩展名可迭代对象；None = DATA_EXTENSIONS
                       （每项可带或不带前导点，忽略大小写与多余尾随点）
    :param skip_dirs: 额外跳过的目录名集合；None = DEFAULT_SKIP_DIRS
                      （.git / __pycache__ 无论何种取值都始终跳过）
    :param exclude_hidden: True = 跳过一切以 "." 开头（隐藏）的目录；
                           False = 允许扫描隐藏目录（.git 仍始终跳过）
    :return: 相对路径字符串 List（list[str]）；仓库根未找到或无可匹配文件时返回空列表
    """
    # 1) 定位扫描根：参数优先，缺省从本脚本向上定位所在仓库根
    root = os.path.abspath(repo_root) if repo_root else _repo_root()
    if root is None or not os.path.isdir(root):
        return []

    # 2) 规整扩展名清单（统一小写、带前导点），用于忽略大小写匹配
    exts = _normalize_extensions(extensions)

    # 3) 跳过目录集合（含用户的显式清单）；.git / __pycache__ 始终跳过
    skip = set(skip_dirs) if skip_dirs is not None else set(DEFAULT_SKIP_DIRS)
    skip.update(DEFAULT_SKIP_DIRS)

    # 4) 递归收集：剪枝跳过（隐藏目录 + 具名跳过目录）+ 按文件名后缀（忽略大小写）命中
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if (not exclude_hidden or not d.startswith("."))
                       and d not in skip]
        for name in filenames:
            if name.lower().endswith(exts):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                result.append(rel.replace(os.sep, "/"))

    # 5) 字典序排序，保证清单稳定可 diff
    result.sort()
    return result


def file_digests(abs_path, chunk_size=DIGEST_CHUNK_SIZE):
    """计算单个文件【内容】的 SHA1 与 MD5（均为大写 hex）

    分块读取，避免大文件一次性载入内存；空文件同样返回其哈希（不会返回 None）。

    :param abs_path: 文件绝对路径
    :param chunk_size: 每次读取的字节数；缺省 DIGEST_CHUNK_SIZE（1 MiB）
    :return: (sha1_hex_upper, md5_hex_upper)
    :raises OSError: 文件不可读时抛出（由调用方决定如何处理）
    """
    sha1 = hashlib.sha1()
    md5 = hashlib.md5()
    with open(abs_path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            sha1.update(chunk)
            md5.update(chunk)
    return sha1.hexdigest().upper(), md5.hexdigest().upper()


def collect_with_digests(repo_root=None, extensions=None, skip_dirs=None,
                         exclude_hidden=True):
    """在 collect() 清单的基础上，为每个文件附带【内容】哈希（SHA1 / MD5，大写 hex）

    扫描范围、排序与排除规则完全复用 collect()，区别仅在于额外读取每个文件内容
    计算哈希，故耗时与文件总大小正相关。

    :param repo_root: 同 collect()
    :param extensions: 同 collect()
    :param skip_dirs: 同 collect()
    :param exclude_hidden: 同 collect()
    :return: list[dict]，每项 {"rel_path": 相对路径, "sha1": 大写 hex, "md5": 大写 hex}；
             顺序与 collect() 一致（按路径字典序）；仓库根未找到或无匹配文件时为空列表
    """
    root = os.path.abspath(repo_root) if repo_root else _repo_root()
    entries = []
    for rel_path in collect(root, extensions, skip_dirs, exclude_hidden):
        sha1_hex, md5_hex = file_digests(os.path.join(root, rel_path))
        entries.append({"rel_path": rel_path, "sha1": sha1_hex, "md5": md5_hex})
    return entries


if __name__ == "__main__":
    # 直接执行仅打印清单与总数（纯只读，便于试运行验证）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    file_paths = collect()
    print("== UpstreamFileList.collect()：共 %d 个数据文件（仓库根: %s）=="
          % (len(file_paths), _repo_root()))
    for rel_path in file_paths:
        print(rel_path)
