#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHubCommitContent —— 单文件 GitHub Contents API 提交库（纯标准库，无第三方依赖）
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
在“不 clone 仓库”的前提下，通过 GitHub Contents REST API 将一段文本内容直接提交
（新建或更新）到 GitHub 仓库的指定文件路径。典型用途：自动化脚本 / CI / Agent 向远端
仓库回写一份文件，例如接口文档、执行结果上报、数据快照同步。

形态说明：
    - 本文件是纯函数库，没有命令行入口；通过“同目录下的其他 Python 脚本” import 调用；
    - import 时不发起任何网络请求、无全局副作用，可放心在脚本头部导入；
    - 公开函数：commit_content（提交文本）、commit_content_file（提交本地文件）、
      load_owner_repo_from_git_config（解析仓库身份）；
    - 结果一律以 dict 返回，调用方判断 success 即可分流；失败不抛异常；
    - 调用过程中的诊断信息走 stderr，不会污染调用方 print 的结果流；
    - 纯标准库实现（urllib / json / base64 / configparser 等），可直接拷到任何
      Python 3.8+ 环境使用。

二、快速上手：同目录脚本调用示例
----------------------------------------------------------------------------------------
以下示例均假设调用脚本与 GitHubCommitContent.py 位于同一目录，因此可直接 import
（运行前请先通过外层环境注入令牌：PowerShell 执行
$env:GIT_COMMIT_TOKEN = "ghp_你的Token"；严禁把令牌写进任何源码）。

【示例 1：提交一段文本（最常用；新建或更新二合一）】

    from GitHubCommitContent import commit_content

    # owner/repo 自动从本仓库 .git 配置解析；分支默认 "latest"；
    # 提交说明默认 "UpdatedAt@<当前本地时间>"，均可按需传参覆盖
    result = commit_content("docs/发布说明.md", "# v2.3.0 发布说明\n\n本次更新内容……")

    if result["success"]:
        # http_status：201 = 新建文件；200 = 更新已有文件
        print("提交成功, http_status =", result["http_status"])
    else:
        print("提交失败:", result["message"])   # message 已含失败原因，可据此告警/重试

【示例 2：把本地文件内容提交到仓库（如上传构建产物）】

    from GitHubCommitContent import commit_content_file

    result = commit_content_file(
        "scripts/report_gen.py",        # 仓库内目标路径（可与本地文件名不同）
        "./build/report_gen.py",        # 本地文件（按 UTF-8 原样读取）
        commit_msg="同步最新构建产物",
    )
    if not result["success"]:
        print("上传失败:", result["message"])

【示例 3：显式指定目标仓库、分支与提交说明】

    from GitHubCommitContent import commit_content

    # 显式 owner/repo 会覆盖 .git 解析结果，适用于把内容写到“其它仓库”的场景
    result = commit_content(
        "data/snapshot.json",
        '{"time": "2026-09-09T00:00:00", "type": "daily"}',
        owner="ACANX", repo="Distribution",
        branch="latest", commit_msg="每日数据快照",
    )

【示例 4：批量提交多个文件（循环调用）】

    from GitHubCommitContent import commit_content

    files = {
        "docs/a.md": "# A 文档",
        "docs/b.md": "# B 文档",
        "docs/c.md": "# C 文档",
    }
    for path, body in files.items():
        # 每个文件独立执行“查 sha + PUT”，互不影响
        result = commit_content(path, body, commit_msg="批量更新文档")
        if not result["success"]:
            print("失败:", path, "->", result["message"])

【示例 5：先解析本仓库 owner/repo（排查身份来源 / 组合其它用途）】

    from GitHubCommitContent import load_owner_repo_from_git_config

    owner, repo = load_owner_repo_from_git_config()   # 解析失败返回 (None, None)
    if owner and repo:
        print("将提交到:", owner, "/", repo)
    else:
        print("未能在本仓库 .git 配置中解析出 github.com 的 owner/repo，需显式传参")

【示例 6：更新远端数据文件（含失败退出与状态区分）】

    from GitHubCommitContent import commit_content

    payload = '{"updatedAt": "2026-09-09 00:00:00", "count": 128}'
    result = commit_content("status/last_run.json", payload)
    if not result["success"]:
        raise SystemExit("上报失败: " + result["message"])
    if result["http_status"] == 201:          # 首次运行 → 新建成功
        print("已创建 status/last_run.json")
    else:                                      # 200 → 更新成功
        print("已更新 status/last_run.json")

【环境要求】
    - Python 3.8+，仅标准库，不依赖 requests 等任何第三方包；
    - 可直连 api.github.com（443）。

三、输入来源与解析规则
----------------------------------------------------------------------------------------
| 输入        | 来源优先级                                              | 说明 |
|-------------|---------------------------------------------------------|------|
| token       | 1) 参数 token 显式传入                                   | 见【安全红线】 |
|             | 2) 环境变量 GIT_COMMIT_TOKEN（约定专用变量名）        |      |
| owner / repo| 1) 参数 owner/repo 显式传入（局部覆盖，可只传其一）      | 见【.git 解析】 |
|             | 2) 本脚本所在仓库的 .git/config（remote origin 的 url）   |      |
| branch      | 1) 参数 branch 显式传入         2) 默认 "Migration"      |      |
| commit_msg  | 1) 参数 commit_msg 显式传入                              | 默认 |
|             | 2) 默认 "UpdatedAt@<本地当前时间ISO>"                    | "UpdatedAt@now" |
| api_base    | 参数 api_base 显式传入，默认 https://api.github.com/repos | REST 仓库集合根 |

【Commit.json 登记规则】（load_commit_config 实现）
    1. Commit.json 与本脚本同目录，登记目标仓库身份与分支默认值（PascalCase 键）：
           { "Owner": "ACANX", "Repo": "Dist",
             "BranchMigration": "Migration", "BranchDelete": "Delete" }
       - Owner / Repo：提交 / 删除 / 遍历的目标仓库身份；
       - BranchMigration：迁移文件（上传清单/成功清单）驻留分支
         （commit_content 的默认分支，默认 "Migration"）；
       - BranchDelete：存放“已上传待删除文件清单”的分支（删除类 API 使用）。
    2. 解析优先级：
       - owner / repo：参数显式传入 → Commit.json 的 Owner/Repo → 本仓库 .git 解析；
       - branch：参数显式传入 → Commit.json 的 BranchMigration → 默认 "Migration"；
       - Commit.json 缺失时静默回退；非法（非 JSON / 顶层非对象）仅 stderr 告警回退，
         不影响模块被拷到无配置目录的正常使用。

【.git 解析规则】（load_owner_repo_from_git_config 实现）
    1. 从本脚本文件所在目录开始逐级向上查找 ".git" 入口：
       - ".git" 是目录  → 即仓库元数据目录；
       - ".git" 是文件  → 读取首行 "gitdir: <相对或绝对路径>" 定位真实元数据目录
         （支持 git worktree / submodule 场景）。
    2. 读取 <gitdir>/config 中的 [remote "origin"] 的 url；无 origin 时退而取
       sections 中第一个 [remote "..."]。
    3. 从 url 解析 owner/repo，支持以下常见形式（host 必须是 github.com）：
       - https://github.com/ACANX/Distribution.git
       - git@github.com:ACANX/Distribution.git   （scp-like）
       - ssh://git@github.com/ACANX/Distribution.git
    4. 解析失败（非 github.com 仓库 / 无 remote / 不在 git 仓库内）→ 返回 (None, None)，
       commit_content 会以 success=False 返回原因，提示显式传 owner/repo。
    说明：url 可能带 ".git" 后缀与结尾 "/"，自动去除；按仓库目录查找而非当前工作目录，
    因此无论调用方 cwd 在哪、是否在仓库内，结果都一致（跟随本文件所在仓库）。

四、行为语义
----------------------------------------------------------------------------------------
    A. 查 sha（GET /repos/{owner}/{repo}/contents/{path}?ref={branch}）：
       - 200 → 取返回 JSON 的 "sha"，后续 PUT 携带 sha 实现“更新旧文件”；
       - 404 → 文件不存在，按“新建文件”处理（不携带 sha）；
       - 其它状态 / 网络错误 → stderr 告警后继续，交给 PUT 的结果做最终判定。
    B. 提交（PUT /repos/{owner}/{repo}/contents/{path}，body JSON）：
       {
         "message": commit_msg,
         "content": <文件内容的 UTF-8 Base64>,
         "branch":  branch,
         "author":    { "name": "github-bot", "email": "github-bot@users.noreply.github.com" },
         "committer": { "name": "github-bot", "email": "github-bot@users.noreply.github.com" },
         "sha": <查到的 sha，仅更新场景>
       }
       - 请求头：User-Agent / Accept: application/vnd.github.v3+json /
         Content-Type: application/json / Authorization: Bearer <token>；
       - 成功判定：HTTP 2xx 且 响应 JSON 顶层无 "status" 与 "message"；
       - 新建返回 HTTP 201、更新返回 200（可通过 result["http_status"] 区分）。
    C. 返回结构（success/message/path/http_status 四字段）：
       { "success": bool, "message": str|None, "path": path_key, "http_status": int|None }
       - success=True  时 message 为 None；
       - success=False 时 message 为失败原因（令牌缺失 / .git 解析失败 / 网络错误 /
         GitHub 返回的错误 message 原文等），http_status 为 GitHub 状态码或 None。

五、安全红线（AGENT 必须遵守）
----------------------------------------------------------------------------------------
    - 令牌只允许经环境变量 GIT_COMMIT_TOKEN（或函数参数）注入；
    - 严禁把真实令牌写入本文件、任何源码、commit message、日志或回显输出；
    - 运行/调试时禁止把 "$env:GIT_COMMIT_TOKEN" 的内容打印到 stdout/stderr；
    - 本工具不读取、不落盘任何令牌缓存；令牌仅存在于进程内存中。

六、注意事项
----------------------------------------------------------------------------------------
    - path_key 为仓库内相对路径，可含子目录（如 "docs/guide.md"），传原始未编码形式即可，
      内部按 "/" 保留、其余字符 URL 编码；
    - 文件内容按 UTF-8 编码后再 Base64；content 参数可为多行字符串；
    - branch 不存在时 GitHub 会返回 422 错误（message 中说明 ref 不存在）；
    - 提交相同 sha 的文件会以 commit_msg 生成新 commit；提交未变更内容也会生成空 commit
      （GitHub 不拒绝）；删除文件不属本工具范围；
    - 大量循环提交时注意 GitHub API 限流（默认 5000 次/小时），建议批量场景自行控制频率；
    - Windows 控制台可能为 GBK 编码：调用脚本 print 中文结果时如乱码，请设置环境变量
      PYTHONIOENCODING=utf-8。
"""

import base64
import configparser
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

# 令牌环境变量名（约定专用）
ENV_TOKEN = "GIT_COMMIT_TOKEN"

# GitHub Contents API 仓库集合根（含 /repos）
DEFAULT_API_BASE = "https://api.github.com/repos"

# 分支默认值（迁移文件驻留 / 上传目标远端分支）
DEFAULT_BRANCH = "Migration"

# Commit.json 配置文件与登记字段（与本脚本同目录，登记目标仓库身份 / 分支默认值）
#   Owner：目标仓库属主；Repo：目标仓库名；
#   BranchMigration：迁移文件（上传清单 / 成功清单）驻留分支（commit 默认分支，
#                    默认 "Migration"；原命名 BranchUpstream 已废弃）；
#   BranchDelete：存放“已上传待删除文件清单”的分支（删除类 API 使用）。
COMMIT_CONFIG_FILE = "Commit.json"
CFG_KEY_OWNER = "Owner"
CFG_KEY_REPO = "Repo"
CFG_KEY_BRANCH_MIGRATION = "BranchMigration"
CFG_KEY_BRANCH_DELETE = "BranchDelete"

# 提交人（固定使用通用 bot 身份，避免以个人账号产生提交）
BOT_NAME = "github-bot"
BOT_EMAIL = "github-bot@users.noreply.github.com"

USER_AGENT = "GitHubCommitContent/1.0 (pure-stdlib python)"
ACCEPT_HEADER = "application/vnd.github.v3+json"
CONTENT_TYPE_JSON = "application/json"

# 成功判定口径：PUT 响应 JSON 顶层同时缺失 "status" 与 "message" 才视为成功
# （GitHub 的错误应答带 "message"，部分异常应答还带 "status"）


def _script_dir():
    """返回本脚本所在目录（绝对路径）"""
    return os.path.dirname(os.path.abspath(__file__))


def _find_git_dir(start_dir):
    """从 start_dir 向上逐级查找 .git 入口，返回真实 git 元数据目录或 None

    :param start_dir: 开始查找的目录（绝对路径）
    :return: git 元数据目录绝对路径；找不到返回 None（支持 .git 目录 / .git 文件两种形态）
    """
    d = os.path.abspath(start_dir)
    while True:
        entry = os.path.join(d, ".git")
        if os.path.isdir(entry):
            return entry
        if os.path.isfile(entry):
            target = _read_gitdir_file(entry, d)
            return target if target and os.path.isdir(target) else None
        parent = os.path.dirname(d)
        if parent == d:  # 已到文件系统根
            return None
        d = parent


def _read_gitdir_file(git_file, base_dir):
    """解析 .git 文件（worktree/submodule 形态）中的 gitdir: 指向

    :param git_file: .git 文件路径
    :param base_dir: .git 文件所在目录（相对路径的基准）
    :return: 真实 git 元数据目录绝对路径；解析失败返回 None
    """
    try:
        with open(git_file, "r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
    except OSError:
        return None
    line = line.strip()
    if not line.lower().startswith("gitdir:"):
        return None
    value = line.split(":", 1)[1].strip()
    if not value:
        return None
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(base_dir, value))


def _read_remote_url(git_dir):
    """读取 <git_dir>/config 中 origin（缺省取第一个）remote 的 url

    :param git_dir: git 元数据目录
    :return: remote url 字符串；无 remote / 读取失败返回 None
    """
    cfg = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        read_ok = cfg.read(os.path.join(git_dir, "config"), encoding="utf-8")
    except (configparser.Error, OSError):
        return None
    if not read_ok:
        return None
    remotes = [s for s in cfg.sections() if s.strip().lower().startswith("remote ")]
    if not remotes:
        return None
    # 优先 origin；无 origin 时取 sections 序中第一个 remote，保证结果确定
    chosen = None
    for s in remotes:
        if s.strip().lower() == 'remote "origin"':
            chosen = s
            break
    if chosen is None:
        chosen = remotes[0]
    url = cfg.get(chosen, "url", fallback=None)
    return (url or "").strip() or None


def _parse_github_owner_repo(url):
    """从 github.com 的 remote url 解析 (owner, repo)

    支持形式：
        https://github.com/ACANX/Distribution.git
        git@github.com:ACANX/Distribution.git
        ssh://git@github.com/ACANX/Distribution.git

    :param url: remote url 字符串
    :return: (owner, repo) 元组；非 github.com 或解析失败返回 (None, None)
    """
    if not url:
        return None, None
    after = None
    # URL 形态（含 https/ssh/git 及携带 userinfo 的写法）：取 github.com/ 之后
    if "github.com/" in url:
        after = url.split("github.com/", 1)[1]
    # scp-like 形态（git@github.com:owner/repo.git）：取 github.com: 之后
    elif "github.com:" in url:
        after = url.split("github.com:", 1)[1]
    else:
        return None, None
    if not after:
        return None, None
    parts = after.strip("/").split("/")
    if len(parts) < 2:
        return None, None
    owner = parts[0].strip()
    repo = parts[1].strip()
    if not owner or not repo:
        return None, None
    # 去除仓库名尾部的 ".git"（忽略大小写）与可能残留的空格
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return owner.strip(), repo.strip()


def load_owner_repo_from_git_config(start_dir=None):
    """从本脚本所在仓库的 .git 配置解析 (owner, repo)

    查找规则：以脚本文件目录（或 start_dir）为起点向上逐级找 ".git" 入口，
    读取 [remote "origin"] 的 url 并解析 github.com 上的 owner/repo；
    支持 .git 为文件（worktree/submodule）与 url 的 https / scp-like / ssh 形式。

    :param start_dir: 起始查找目录（绝对路径）；None = 本脚本所在目录
    :return: (owner, repo) 字符串元组；解析失败返回 (None, None)（不抛异常）
    """
    base = os.path.abspath(start_dir) if start_dir else _script_dir()
    git_dir = _find_git_dir(base)
    if git_dir is None:
        return None, None
    url = _read_remote_url(git_dir)
    return _parse_github_owner_repo(url)


def load_commit_config(cfg_path=None):
    """读取同目录 Commit.json 登记的目标仓库默认值

    Commit.json 与本脚本同目录，登记字段（PascalCase）：
        Owner / Repo：提交 / 删除 / 遍历的目标仓库身份；
        BranchMigration：迁移文件（上传清单 / 成功清单）驻留分支（commit 默认分支，
                         默认 "Migration"）；
        BranchDelete：存放“已上传待删除文件清单”的分支（删除类 API 使用）。

    返回 dict 统一小写键；Commit.json 缺失时静默返回 None（正常回退到 .git 解析 /
    内置默认值），缺失或非法时仅 stderr 告警后返回 None，不抛异常。

    :param cfg_path: Commit.json 路径；None = 与本脚本同目录的 Commit.json
    :return: dict（含 owner / repo / branch_migration / branch_delete，缺失字段为空串）；
             文件缺失 / 非法 / 顶层非对象时返回 None
    """
    path = cfg_path or os.path.join(_script_dir(), COMMIT_CONFIG_FILE)
    if not os.path.isfile(path):
        return None  # 未登记：静默回退（本模块被拷到无配置目录时同样可用）
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        _warn("读取 Commit.json 失败（回退 .git 解析/内置默认）: %s" % e)
        return None
    if not isinstance(data, dict):
        _warn("Commit.json 顶层应为 JSON 对象（回退 .git 解析/内置默认）")
        return None

    def grab(key):
        """取出键值并转非空字符串；缺失或为空返回空串"""
        value = data.get(key)
        if value is None:
            return ""
        return str(value).strip()

    return {
        "owner": grab(CFG_KEY_OWNER),
        "repo": grab(CFG_KEY_REPO),
        "branch_migration": grab(CFG_KEY_BRANCH_MIGRATION),
        "branch_delete": grab(CFG_KEY_BRANCH_DELETE),
    }


def _resolve_target(owner, repo, branch):
    """统一解析目标仓库身份与默认分支：参数 → Commit.json → .git 解析/内置默认

    供 commit_content / read_file_text 等各类 Contents API 操作复用，保证
    owner/repo/branch 的默认解析口径完全一致：
        - owner / repo：显式参数 → Commit.json 的 Owner/Repo → 本仓库 .git 解析；
        - branch：显式参数 → Commit.json 的 BranchMigration → DEFAULT_BRANCH（"Migration"）。

    :param owner: 调用方显式传入的属主；可为 None
    :param repo: 调用方显式传入的仓库名；可为 None
    :param branch: 调用方显式传入的分支；可为 None
    :return: (owner, repo, branch) 三元组；无法解析出 owner/repo 时返回
             (None, None, None)（身份缺失原因已 _warn 告警）
    """
    cfg = None
    if (not owner or not repo) or not branch:
        cfg = load_commit_config()

    # owner/repo：参数 → Commit.json 登记值 → 本仓库 .git 配置解析
    if not owner or not repo:
        if cfg:
            if not owner:
                owner = cfg.get("owner") or None
            if not repo:
                repo = cfg.get("repo") or None
    if not owner or not repo:
        auto_owner, auto_repo = load_owner_repo_from_git_config()
        if not owner:
            owner = auto_owner
        if not repo:
            repo = auto_repo
    if not owner or not repo:
        _warn("❌ 未能从 Commit.json 或本仓库 .git/config 解析出 github.com 的 "
              "owner/repo，请显式传入 owner/repo 参数")
        return None, None, None

    # branch：参数 → Commit.json 的 BranchMigration → DEFAULT_BRANCH（"Migration"）
    if not branch:
        if cfg:
            branch = cfg.get("branch_migration") or None
    if not branch:
        branch = DEFAULT_BRANCH
    return owner, repo, branch


def _request(method, url, headers, body_bytes=None, timeout=30):
    """发送 HTTP 请求并返回统一结构（仅标准库 urllib）

    :param method: HTTP 方法（GET/PUT/POST/DELETE 等）
    :param url: 完整请求 URL
    :param headers: 请求头 dict
    :param body_bytes: 请求体字节；None 表示无 body
    :param timeout: 超时秒数
    :return: (status, body_text, error)：
        - status: int（HTTP 4xx/5xx 也原样返回）或 None（网络层失败）；
        - body_text: 响应体按 UTF-8（errors=replace）解码后的字符串；
        - error: 网络层失败原因字符串；请求到达服务器（含 HTTP 错误码）时为 None。
    """
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, text, None
    except urllib.error.HTTPError as e:
        # HTTP 错误码是服务器的正常应答：GitHub 错误体含 "message"，交给调用方判定
        text = e.read().decode("utf-8", errors="replace")
        return e.code, text, None
    except urllib.error.URLError as e:
        return None, "", "网络错误: %s" % (e.reason if e.reason is not None else e)
    except TimeoutError:
        return None, "", "请求超时(>%ss)" % timeout
    except OSError as e:
        return None, "", "网络/IO错误: %s" % e


def _auth_headers(token, with_body=False):
    """构造 GitHub Contents API 请求头

    :param token: GitHub 访问令牌
    :param with_body: True 时为带 JSON body 的请求追加 Content-Type
    :return: 请求头 dict
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT_HEADER,
        "Authorization": "Bearer %s" % token,
    }
    if with_body:
        headers["Content-Type"] = CONTENT_TYPE_JSON
    return headers


def _parse_json(text):
    """解析 JSON 字符串；空文本或解析失败返回 None

    :param text: JSON 字符串
    :return: 解析结果对象；失败返回 None
    """
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# 控制台 UTF-8 重配置只执行一次的标志
_console_configured = False


def _ensure_console_utf8():
    """将 stdout/stderr 重配置为 UTF-8（errors=replace）

    防止 Windows 遗留控制台 / 非 UTF-8 locale（如代码页 437、C locale）下
    输出中文时抛 UnicodeEncodeError 导致静默崩溃。仅首次调用生效；
    重配置失败（如流已被重定向接管）时静默跳过，不影响程序运行。

    :return: 无
    """
    global _console_configured
    if _console_configured:
        return
    _console_configured = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _warn(msg):
    """向 stderr 输出告警（不进 stdout 结果流，保证调用方 stdout 只含自己的输出）

    :param msg: 告警文本
    """
    _ensure_console_utf8()
    sys.stderr.write("[GitHubCommitContent] %s\n" % msg)


def _get_file_sha(api_base, owner, repo, path_key, branch, token, timeout):
    """GET contents 接口查询文件当前 sha

    200 → 返回 sha 字符串；404 → 返回 None（文件不存在，走新建）；其余状态/网络错误 →
    stderr 告警并返回 None（继续提交，最终成败以 PUT 结果为准）。

    :param api_base: GitHub API 仓库根（含 /repos）
    :param owner: 仓库属主
    :param repo: 仓库名
    :param path_key: 文件路径
    :param branch: 分支名
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: sha 字符串或 None
    """
    url = "%s/%s/%s/contents/%s?ref=%s" % (
        api_base, owner, repo,
        urllib.parse.quote(path_key, safe="/"),
        urllib.parse.quote(branch, safe=""),
    )
    status, text, err = _request("GET", url, _auth_headers(token), None, timeout)
    if err:
        _warn("查询文件 sha 失败: %s（继续按新建提交）" % err)
        return None
    if status == 404:
        return None  # 文件不存在 → 新建
    if status == 200:
        data = _parse_json(text)
        if isinstance(data, dict) and data.get("sha"):
            return data.get("sha")
        _warn("查询文件 sha 响应异常（可能指向目录），继续按新建提交")
        return None
    _warn("查询文件 sha 失败: HTTP %d（继续按新建提交，交由 PUT 判定）" % status)
    return None


def commit_content(path_key, content, branch=None, commit_msg=None,
                   owner=None, repo=None, token=None,
                   api_base=DEFAULT_API_BASE, timeout=30):
    """向 GitHub 仓库提交/更新指定路径的文件内容（Contents API，无第三方依赖）

    行为要点：先 GET 查 sha（404 视为新建）再 PUT；默认分支按 Commit.json 的
    BranchMigration（缺省 "Migration"）、默认提交信息 "UpdatedAt@<当前本地时间>"、
    author/committer 固定为通用 bot 身份 github-bot；失败不抛异常，一律以返回 dict 表示。

    :param path_key: 仓库内文件路径（可含目录，如 "docs/x.md"）；传原始未编码形式
    :param content: 待提交的文本内容（UTF-8 编码后 Base64 上传，可为多行）
    :param branch: 目标分支；None 时按 Commit.json BranchMigration → 默认 "Migration" 解析
    :param commit_msg: 提交说明；None 时默认 "UpdatedAt@<当前本地时间ISO>"
    :param owner: 仓库属主；None 时按 Commit.json Owner → 本仓库 .git/config 解析
                  （参数显式传入可覆盖，可只传其一）
    :param repo: 仓库名；None 时按 Commit.json Repo → 本仓库 .git/config 解析
                 （参数显式传入可覆盖，可只传其一）
    :param token: 访问令牌；None 时读取环境变量 GIT_COMMIT_TOKEN（二者都缺则失败）
    :param api_base: GitHub API 仓库根，默认 https://api.github.com/repos
    :param timeout: 单次 HTTP 请求超时秒数，默认 30
    :return: 固定结构 dict：
             { "success": bool, "message": str|None, "path": str, "http_status": int|None }
             - success=False 时 message 为失败原因（含 GitHub 返回的错误 message 原文）；
             - 新建成功 http_status=201，更新成功=200；
             - 函数内部不抛网络/HTTP 异常，一切失败以 dict 形式返回。
    """
    def fail(msg):
        """构造失败结果并输出诊断信息到 stderr

        :param msg: 失败原因
        :return: 失败结果 dict
        """
        _warn(msg)
        return {"success": False, "message": msg, "path": path_key, "http_status": None}

    # 1) Token：参数优先，其次环境变量 GIT_COMMIT_TOKEN
    tok = (token or "").strip()
    if not tok:
        tok = os.environ.get(ENV_TOKEN, "").strip()
    if not tok:
        return fail("❌ 请设置环境变量 %s 或者在参数 token 中传入令牌" % ENV_TOKEN)

    # 1.5) 统一解析目标仓库身份与分支：参数 → Commit.json → .git 解析/内置默认
    owner, repo, target_branch = _resolve_target(owner, repo, branch)
    if not (owner and repo):
        return fail("❌ 未能从 Commit.json 或本仓库 .git/config 解析出 github.com 的 "
                    "owner/repo，请显式传入 owner/repo 参数")
    now_iso = datetime.datetime.now().isoformat()
    target_msg = commit_msg or ("UpdatedAt@" + now_iso)

    # 4) 组装 contents URL（path_key 仅保留 "/"，其余字符 URL 编码）
    api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
    contents_url = "%s/%s/%s/contents/%s" % (
        api_base, owner, repo, urllib.parse.quote(path_key, safe="/"))

    # 5) 查 sha：区分“更新旧文件(带 sha)”与“新建文件(不带 sha)”
    sha = _get_file_sha(api_base, owner, repo, path_key, target_branch, tok, timeout)

    # 6) PUT 提交：body 与 Contents API 更新语义一致（仅更新场景携带 sha）
    body_json = {
        "message": target_msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": target_branch,
        "author": {"name": BOT_NAME, "email": BOT_EMAIL},
        "committer": {"name": BOT_NAME, "email": BOT_EMAIL},
    }
    if sha:
        body_json["sha"] = sha
    status, text, err = _request(
        "PUT", contents_url,
        _auth_headers(tok, with_body=True),
        json.dumps(body_json, ensure_ascii=False).encode("utf-8"),
        timeout,
    )
    if err:
        return fail("提交失败: %s" % err)

    parsed = _parse_json(text)
    # 7) 成功判定：HTTP 2xx 且响应顶层无 "status" 与 "message"
    if status is not None and 200 <= status < 300:
        if parsed is None or not isinstance(parsed, dict) \
                or (not parsed.get("status") and not parsed.get("message")):
            return {"success": True, "message": None,
                    "path": path_key, "http_status": status}
        # 2xx 但响应带 message/status（GitHub 的异常应答形态）→ 按失败处理
        reason = parsed.get("message") or ("HTTP %s" % status)
    else:
        # 非 2xx：优先取 GitHub 错误体中的 message 原文
        if isinstance(parsed, dict) and parsed.get("message"):
            reason = parsed.get("message")
        else:
            snippet = (text or "").strip().replace("\n", " ")[:300]
            reason = "HTTP %s: %s" % (status, snippet if snippet else "空响应体")
    return {"success": False, "message": reason, "path": path_key, "http_status": status}


def commit_content_file(path_key, local_file, branch=None, commit_msg=None,
                        owner=None, repo=None, token=None,
                        api_base=DEFAULT_API_BASE, timeout=30):
    """以本地文件内容提交到仓库（便捷包装：读 UTF-8 文件后调 commit_content）

    :param path_key: 仓库内目标路径（远程落点，可与本地文件名不同）
    :param local_file: 本地文件路径（内容按 UTF-8 原样读取，不追加/去除换行）
    :param branch: 目标分支；None 时按 Commit.json BranchMigration → 默认 "Migration" 解析
    :param commit_msg: 提交说明；None = "UpdatedAt@<当前本地时间>"
    :param owner: 仓库属主；None 时按 Commit.json Owner → 本仓库 .git/config 解析
    :param repo: 仓库名；None 时按 Commit.json Repo → 本仓库 .git/config 解析
    :param token: 访问令牌；None = 环境变量 GIT_COMMIT_TOKEN
    :param api_base: GitHub API 仓库根；默认 https://api.github.com/repos
    :param timeout: 单次 HTTP 请求超时秒数，默认 30
    :return: 与 commit_content 相同的结果 dict；本地文件读取失败时 success=False
    """
    try:
        with open(local_file, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return {"success": False, "message": "读取本地文件失败: %s" % e,
                "path": path_key, "http_status": None}
    return commit_content(path_key, content, branch=branch, commit_msg=commit_msg,
                          owner=owner, repo=repo, token=token,
                          api_base=api_base, timeout=timeout)


def read_file_text(path_key, branch=None, owner=None, repo=None,
                   token=None, api_base=DEFAULT_API_BASE, timeout=30):
    """从目标仓库的指定分支读取文件并返回 UTF-8 文本（Contents API GET）

    用于“下载”远端清单 / 配置文件等文本文件；owner/repo/branch 的默认解析链与
    commit_content 完全一致（参数 → Commit.json → .git 解析 / 内置默认分支）。

    :param path_key: 仓库内文件路径（可含目录，传原始未编码形式）
    :param branch: 目标分支；None 时按 Commit.json BranchMigration → 默认 "Migration" 解析
    :param owner: 仓库属主；None 时按 Commit.json Owner → 本仓库 .git/config 解析
    :param repo: 仓库名；None 时按 Commit.json Repo → 本仓库 .git/config 解析
    :param token: 访问令牌；None 时读取环境变量 GIT_COMMIT_TOKEN
    :param api_base: GitHub API 仓库根；默认 https://api.github.com/repos
    :param timeout: 单次 HTTP 请求超时秒数，默认 30
    :return: dict { "success": bool, "message": str|None, "path": path_key,
                    "branch": str|None, "http_status": int|None,
                    "text": str|None }
             - success=True 时 text 为文件内容（UTF-8、errors=replace 解码）；
             - 404 / 响应异常 / 网络错误均 success=False，text 为 None；不抛异常
    """
    def fail(msg):
        _warn(msg)
        return {"success": False, "message": msg, "path": path_key,
                "branch": None, "http_status": None, "text": None}

    # 1) Token：参数优先，其次环境变量 GIT_COMMIT_TOKEN
    tok = (token or "").strip()
    if not tok:
        tok = os.environ.get(ENV_TOKEN, "").strip()
    if not tok:
        return fail("❌ 请设置环境变量 %s 或者在参数 token 中传入令牌" % ENV_TOKEN)

    # 2) 统一解析目标仓库身份与分支
    resolved_owner, resolved_repo, resolved_branch = _resolve_target(owner, repo, branch)
    if not (resolved_owner and resolved_repo):
        return fail("❌ 未能从 Commit.json 或本仓库 .git/config 解析出 github.com 的 "
                    "owner/repo，请显式传入 owner/repo 参数")

    # 3) GET contents/<path>?ref=<branch>
    url = "%s/%s/%s/contents/%s?ref=%s" % (
        (api_base or DEFAULT_API_BASE).rstrip("/"), resolved_owner, resolved_repo,
        urllib.parse.quote(path_key, safe="/"),
        urllib.parse.quote(resolved_branch, safe=""))
    status, text, err = _request("GET", url, _auth_headers(tok), None, timeout)
    if err:
        return fail("读取远端文件失败: %s" % err)
    if status == 404:
        return fail("远端文件不存在（HTTP 404）: %s" % path_key)
    if status != 200:
        snippet = (text or "").strip().replace("\n", " ")[:300]
        return fail("读取远端文件失败: HTTP %s: %s" % (status, snippet or "空响应体"))

    # 4) 解析响应：文件内容以 base64 存放在顶层 "content" 字段
    data = _parse_json(text)
    if not isinstance(data, dict) or not data.get("content"):
        return fail("读取远端文件响应异常（缺少顶层 content 字段，可能指向目录）: %s"
                    % path_key)
    try:
        raw = base64.b64decode(data["content"])
    except (TypeError, ValueError) as e:
        return fail("远端文件 content 非合法 Base64: %s" % e)
    body = raw.decode("utf-8", errors="replace")
    return {"success": True, "message": None, "path": path_key,
            "branch": resolved_branch, "http_status": 200, "text": body}


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    _ensure_console_utf8()
    print("GitHubCommitContent 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from GitHubCommitContent import commit_content, commit_content_file")
    print("仓库身份解析：from GitHubCommitContent import load_owner_repo_from_git_config")
    print("Commit.json 登记值：from GitHubCommitContent import load_commit_config")
    print("冒烟/调用示例脚本：python3 GitHubCommitContentDemo.py")
