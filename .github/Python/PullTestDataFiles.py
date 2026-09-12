#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PullTestDataFiles —— 跨分支按原路径搬运 Data 目录下的测试数据

用途
    测试时常常需要「某个仓库某分支的 Data 测试数据，出现在另一个仓库 / 分支上」，手工
    下载再上传既慢又容易漏文件、改路径。本脚本把【源仓库 : 源分支】Data 目录下匹配的
    文件，按**原路径**复制到【目标仓库 : 目标分支】的相同路径，作为测试数据直接使用。

    源与目标**可以是不同的仓库**：目标仓库身份取自本仓的 Commit.json（登记的是本仓
    自己），源仓库身份由 --source-owner / --source-repo 指定；两者相同时即退化为
    「同一仓库内跨分支搬运」。源仓库按只读访问，令牌对它无需写权限。

    由于 git 的 blob sha 只由内容决定，跨仓库比对依然成立：源仓库某文件的 blob sha 与
    目标仓库同路径文件的 blob sha 相同，即说明两边内容逐字节一致，可直接跳过。

    默认搬运 *.json、*.mvsv 与 *.log 三种后缀（按文件名匹配，不限目录深度）；要搬运
    其它后缀用 --pattern 覆盖，多个模式用英文逗号分隔。

为什么不 clone
    仓库体积大，即便 --depth 1 也会拉下完整目录树；而测试数据通常只有十几个文件，
    为此 clone 一次不划算。故本脚本全程走 GitHub REST API，**不开工作树、不做检出**：

        1) Git Trees API（GET /git/trees/{branch}?recursive=1）
           一次取回两侧的完整文件清单，含每个文件的 blob sha 与字节数；
        2) 比对 blob sha —— sha 相同即内容逐字节相同，**直接跳过**（不下载、不提交）；
        3) 仅对「目标分支缺失、或内容不同」的文件走 Git Blobs API 取回原始字节；
        4) 走 Git Data API 打包成**一个提交**写回目标分支（见下）。

    于是「已经同步过」的一轮只花 2 次清单请求；只有真正变化的文件才会被传输。

写入方式：整批一个提交
    Git Data API 四步（全部围绕本次要写的文件，不碰目标分支上的其它内容）：

        POST /git/blobs   逐文件上传原始字节 → 得到 blob sha
        POST /git/trees   以**目标分支当前的树**为 base_tree，挂上这批新 blob
        POST /git/commits 以目标分支当前 head 为 parent，生成新提交
        PATCH /git/refs/heads/{branch}   移动分支指针（force=false，只允许快进）

    好处是一次同步只留一个提交；代价是**失败即整批不落地** —— 任一文件出错就中止，
    目标分支保持原样（详见下方「失败语义」）。

字节精确
    刻意**不**复用 GitHubCommitContent.commit_content —— 那条路按文本处理（读临时文件
    时走 UTF-8 文本模式），Python 的通用换行会把 CRLF 静默翻译成 LF。当前数据全是 LF，
    看不出差别，但那是「碰巧正确」。本脚本按原始字节下载、按原始字节上传，并以
    「POST /git/blobs 返回的 sha == 源 blob sha」作为逐字节一致的证明。

    这道校验放在**建提交之前**：一旦某文件的内容在服务端被改动（例如将来加了
    .gitattributes 的 clean 过滤），立刻整批中止，绝不把走样的数据写进目标分支。

失败语义（整批不落地）
    - 任一文件下载失败 / 内容校验失败 / 超过单文件上限 → 中止，**不创建提交**；
    - 创建 blob 后 sha 与源不符 → 中止，**不创建提交**；
    - 分支指针移动被拒（目标分支在运行期间被别处改动，非快进）→ 目标分支原样未动；
    - 提交成功后再复核一次：重新取目标分支清单，逐路径比对 sha，全等才判成功。

边界（重要）
    - **只新增 / 覆盖匹配到的文件，从不删除**目标分支上的任何文件 —— 源分支没有的文件
      原样保留，因此本脚本不会造成数据丢失；
    - 源分支与目标分支同名时直接失败退出（无内容可同步，且极易被误判为「同步成功」）；
    - 文件清单被 GitHub 截断（truncated=true）时失败退出，绝不按残缺清单做同步。

用法
    # 跨仓库：从 ACANX/Distribution 的 quote 分支，拉到本仓的 datatest 分支
    python3 .github/Python/PullTestDataFiles.py \
        --source-owner ACANX --source-repo Distribution --source-branch quote \
        --target-branch datatest --dry-run

    # 同仓库跨分支：省略 --source-owner / --source-repo 时，默认与目标仓库相同
    python3 .github/Python/PullTestDataFiles.py \
        --source-branch datatest --target-branch quote

环境变量
    GIT_COMMIT_TOKEN —— 需**目标仓库**写权限；缺失即失败退出（不静默降级为只读）。
                        源仓库按只读访问，令牌对它无写权限要求（源为公开仓库时，
                        即使令牌未覆盖它也能读）。

退出码
    0 = 成功（含「无可同步文件」这类正常结束）；1 = 存在失败项或参数/环境不满足。
"""

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import GitHubCommitContent

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: GitHub REST API 的仓库根（与 GitHubCommitContent 保持一致）
DEFAULT_API_BASE = "https://api.github.com/repos"

#: 令牌环境变量名（与 GitHubCommitContent 共用同一个）
ENV_TOKEN = "GIT_COMMIT_TOKEN"

#: 默认从哪个目录下取数据
DEFAULT_PATH_PREFIX = "Data"

#: 默认匹配哪些文件（按**文件名**匹配，不含目录段，故任意深度都覆盖）
#: 多个模式用英文逗号分隔
DEFAULT_PATTERN = "*.json,*.mvsv,*.log"

#: 单次 HTTP 请求超时秒数
DEFAULT_TIMEOUT = 30

#: 报错信息里截取的响应急长度
EXCERPT_LIMIT = 160

#: 单文件上限（字节）；超过则中止整批，不去发注定失败的请求
MAX_FILE_BYTES = 100 * 1024 * 1024

#: 提交到 git 的普通文件模式（非可执行、非符号链接）
BLOB_MODE = "100644"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _log(msg):
    """输出主流程日志（stdout，逐行 flush，便于 Actions 里实时看到进度）"""
    print(msg, flush=True)


def _excerpt(raw, limit=EXCERPT_LIMIT):
    """把响应体压成一行短摘要，用于拼错误信息

    :param raw: 原始响应字节；可为 None
    :param limit: 截断长度
    :return: 单行字符串（超长加省略号）
    """
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace").strip().replace("\n", " ").replace("\r", "")
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _api_headers(token, with_body=False):
    """组装 GitHub API 请求头

    :param token: 访问令牌
    :param with_body: 是否携带请求体（需要声明 Content-Type）
    :return: 请求头 dict
    """
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PullTestDataFiles",
    }
    if with_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    return headers


def _request(method, url, headers, body=None, timeout=DEFAULT_TIMEOUT):
    """发一次 HTTP 请求，任何异常都收敛成返回值（本函数不抛异常）

    HTTP 层的错误响应（4xx/5xx）不算「请求失败」：状态码与响应体照常返回，交给调用方
    按状态码判定；只有网络层错误（连不上、超时等）才返回 err。

    :param method: HTTP 方法
    :param url: 完整 URL
    :param headers: 请求头
    :param body: 请求体字节；None 表示无体
    :param timeout: 超时秒数
    :return: (status, raw_bytes, err) —— err 非 None 时 status/raw 无意义
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read(), None
    except urllib.error.HTTPError as e:
        # 先于 URLError 捕获：HTTPError 是它的子类
        try:
            raw = e.read()
        except OSError:
            raw = b""
        return e.code, raw, None
    except (urllib.error.URLError, OSError) as e:
        return None, None, "%s: %s" % (type(e).__name__, e)


def _parse_json(raw):
    """尽力把响应体解析成 JSON 对象；解析不了返回 None（不抛异常）"""
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _post(api_base, path, payload, token, timeout, method="POST"):
    """发一个带 JSON 体的写请求

    :param api_base: API 仓库根
    :param path: 仓库根之后的路径（如 "git/blobs"）
    :param payload: 请求体 dict
    :param token: 访问令牌
    :param timeout: 超时秒数
    :param method: HTTP 方法（默认 POST，更新引用时用 PATCH）
    :return: (status, data, err) —— data 为解析出的 JSON 对象或 None
    """
    url = "%s/%s" % (api_base.rstrip("/"), path)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw, err = _request(method, url, _api_headers(token, with_body=True),
                                body, timeout)
    if err:
        return None, None, "请求失败：%s" % err
    return status, _parse_json(raw), None


# ---------------------------------------------------------------------------
# 读取：清单与内容
# ---------------------------------------------------------------------------

def _branch_error(status, raw, branch):
    """把清单请求的失败响应翻译成人话"""
    if status == 404:
        return "分支 %s 不存在（或令牌无权访问该仓库）" % branch
    if status == 401:
        return "令牌无效或已过期（HTTP 401）"
    if status == 403:
        return "令牌权限不足或被限流（HTTP 403）：%s" % _excerpt(raw)
    return "HTTP %s：%s" % (status, _excerpt(raw))


def fetch_tree(api_base, owner, repo, branch, token, timeout):
    """取某分支的完整文件清单（Git Trees API，递归）

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param branch: 分支名
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: (entries, err) —— entries 为 {路径: (blob_sha, 字节数)}；err 非 None 表示失败
    """
    url = "%s/%s/%s/git/trees/%s?recursive=1" % (
        api_base, owner, repo, urllib.parse.quote(branch, safe=""))
    status, raw, err = _request("GET", url, _api_headers(token), None, timeout)
    if err:
        return None, "请求失败：%s" % err
    if status != 200:
        return None, _branch_error(status, raw, branch)

    data = _parse_json(raw)
    if not data or not isinstance(data.get("tree"), list):
        return None, "响应不是预期的文件树结构：%s" % _excerpt(raw)
    if data.get("truncated"):
        # 截断的清单会让「目标分支缺失该文件」的判断失真，宁可不做
        return None, ("文件清单被 GitHub 截断（truncated=true），"
                      "据此同步会漏文件；请缩小目录范围后再试")

    entries = {}
    for item in data["tree"]:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = item.get("path") or ""
        if not path:
            continue
        entries[path] = (item.get("sha") or "", int(item.get("size") or 0))
    return entries, None


def fetch_branch_head(api_base, owner, repo, branch, token, timeout):
    """取目标分支当前的 head 提交 sha 与根树 sha（Git Branches API）

    打包提交时两个都要：head 提交 sha 作为新提交的 parent，根树 sha 作为新树的 base_tree
    （base_tree 保证目标分支上未被本批覆盖的文件原样保留）。

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param branch: 分支名
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: ({"commit_sha": str, "tree_sha": str}, err)
    """
    url = "%s/%s/%s/branches/%s" % (api_base, owner, repo,
                                    urllib.parse.quote(branch, safe=""))
    status, raw, err = _request("GET", url, _api_headers(token), None, timeout)
    if err:
        return None, "请求失败：%s" % err
    if status != 200:
        return None, _branch_error(status, raw, branch)

    data = _parse_json(raw)
    commit = ((data or {}).get("commit") or {})
    commit_sha = commit.get("sha") or ""
    tree_sha = ((commit.get("commit") or {}).get("tree") or {}).get("sha") or ""
    if not (commit_sha and tree_sha):
        return None, "响应里没有 head 提交 / 根树 sha：%s" % _excerpt(raw)
    return {"commit_sha": commit_sha, "tree_sha": tree_sha}, None


def blob_sha_of(raw):
    """按 git 对象规则计算 blob 的 sha1

    git 的 blob sha1 = sha1("blob <字节数>\\0" + 内容)，这就是 API 里各处出现的 sha。

    :param raw: 文件原始字节
    :return: 40 位十六进制 sha1
    """
    header = ("blob %d\0" % len(raw)).encode("ascii")
    return hashlib.sha1(header + raw).hexdigest()


def fetch_blob(api_base, owner, repo, blob_sha, token, timeout):
    """按 blob sha 取回文件的原始字节（Git Blobs API）

    用 Blobs API 而非 Contents API：后者对 1 MB 以上的文件不再内联返回内容，需额外
    指定 raw 媒体类型；Blobs API 直接给 base64，且上限 100 MB，一条路走通。

    取回后按 git 规则重算 sha1 自证：与请求的 sha 不符说明内容在传输中损坏，宁可失败
    也不能把坏字节写进目标分支。

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param blob_sha: 文件的 blob sha（来自文件清单）
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: (raw_bytes, err)
    """
    url = "%s/%s/%s/git/blobs/%s" % (api_base, owner, repo, blob_sha)
    status, raw, err = _request("GET", url, _api_headers(token), None, timeout)
    if err:
        return None, "请求失败：%s" % err
    if status != 200:
        return None, "HTTP %s：%s" % (status, _excerpt(raw))

    data = _parse_json(raw)
    if not data or data.get("encoding") != "base64" or not data.get("content"):
        return None, "响应不是预期的 blob 结构：%s" % _excerpt(raw)
    try:
        content = base64.b64decode(data["content"])
    except (ValueError, TypeError) as e:
        return None, "base64 解码失败：%s" % e

    actual = blob_sha_of(content)
    if actual != blob_sha:
        return None, ("内容校验失败：取回字节的 sha=%s，与清单登记的 sha=%s 不一致"
                      % (actual[:8], blob_sha[:8]))
    return content, None


# ---------------------------------------------------------------------------
# 写入：整批一个提交（Git Data API）
# ---------------------------------------------------------------------------

def create_blob(api_base, owner, repo, content, token, timeout):
    """上传一份原始字节，得到它的 blob sha

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param content: 文件原始字节
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: (blob_sha, err)
    """
    payload = {"content": base64.b64encode(content).decode("ascii"),
               "encoding": "base64"}
    status, data, err = _post("%s/%s/%s" % (api_base, owner, repo), "git/blobs",
                              payload, token, timeout)
    if err:
        return None, err
    if status not in (200, 201):
        return None, "HTTP %s：%s" % (status, (data or {}).get("message") or "")
    sha = (data or {}).get("sha") or ""
    if not sha:
        return None, "响应里没有 blob sha"
    return sha, None


def create_tree(api_base, owner, repo, base_tree_sha, entries, token, timeout):
    """在指定的 base_tree 之上挂一批新 blob，得到新树 sha

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param base_tree_sha: 目标分支当前根树（未覆盖到的文件由它原样保留）
    :param entries: [{"path": 仓库内路径, "sha": blob sha}]
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: (tree_sha, err)
    """
    payload = {
        "base_tree": base_tree_sha,
        "tree": [{"path": e["path"], "mode": BLOB_MODE,
                  "type": "blob", "sha": e["sha"]} for e in entries],
    }
    status, data, err = _post("%s/%s/%s" % (api_base, owner, repo), "git/trees",
                              payload, token, timeout)
    if err:
        return None, err
    if status not in (200, 201):
        return None, "HTTP %s：%s" % (status, (data or {}).get("message") or "")
    sha = (data or {}).get("sha") or ""
    if not sha:
        return None, "响应里没有 tree sha"
    return sha, None


def create_commit(api_base, owner, repo, message, tree_sha, parent_sha, token, timeout):
    """基于新树生成一个提交

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param message: 提交说明
    :param tree_sha: create_tree 得到的树 sha
    :param parent_sha: 目标分支当前 head 提交 sha
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: (commit_sha, err)
    """
    payload = {"message": message, "tree": tree_sha, "parents": [parent_sha]}
    status, data, err = _post("%s/%s/%s" % (api_base, owner, repo), "git/commits",
                              payload, token, timeout)
    if err:
        return None, err
    if status not in (200, 201):
        return None, "HTTP %s：%s" % (status, (data or {}).get("message") or "")
    sha = (data or {}).get("sha") or ""
    if not sha:
        return None, "响应里没有 commit sha"
    return sha, None


def update_ref(api_base, owner, repo, branch, commit_sha, token, timeout):
    """把分支指针移到新提交（只允许快进）

    force=false 让 GitHub 拒绝非快进更新：若目标分支在本次运行期间被别处改动，这里会
    失败而**不会**覆盖掉别人的提交，目标分支保持原样。

    :param api_base: API 仓库根
    :param owner: 仓库属主
    :param repo: 仓库名
    :param branch: 分支名
    :param commit_sha: 新提交 sha
    :param token: 访问令牌
    :param timeout: 超时秒数
    :return: (status, err)
    """
    payload = {"sha": commit_sha, "force": False}
    path = "git/refs/heads/%s" % urllib.parse.quote(branch, safe="")
    status, data, err = _post("%s/%s/%s" % (api_base, owner, repo), path,
                              payload, token, timeout, method="PATCH")
    if err:
        return None, err
    if status != 200:
        message = (data or {}).get("message") or ""
        if status in (409, 422):
            return status, ("目标分支在本次运行期间被别处改动（非快进更新被拒）：%s"
                            % (message or "ref update rejected"))
        return status, "HTTP %s：%s" % (status, message or "")
    return status, None


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------

def parse_patterns(spec):
    """把逗号分隔的模式串拆成模式列表

    允许一次匹配多种后缀（如 "*.json,*.mvsv"），便于测试数据混有多种扩展名时一并搬运。

    :param spec: 模式串，逗号分隔
    :return: 去空、去重后的模式列表（保持出现顺序）
    """
    patterns = []
    for part in (spec or "").split(","):
        p = part.strip()
        if p and p not in patterns:
            patterns.append(p)
    return patterns


def select_source_files(entries, path_prefix, patterns):
    """从文件清单里挑出「位于 path_prefix 目录下、且文件名匹配任一 pattern」的文件

    :param entries: fetch_tree 返回的 {路径: (sha, 字节数)}
    :param path_prefix: 目录前缀（如 "Data"），只取它**内部**的文件
    :param patterns: 文件名通配列表（如 ["*.json", "*.mvsv"]）；只比对最后一段，不含路径
    :return: (matched, ignored) —— matched 为按路径排序的 list[dict]；
             ignored 为命中目录但文件名不匹配的路径列表
    """
    head = path_prefix.strip("/")
    matched, ignored = [], []
    for path in sorted(entries):
        if not path.startswith(head + "/"):
            continue
        name = path.rsplit("/", 1)[-1]
        if not any(fnmatch.fnmatchcase(name, p) for p in patterns):
            ignored.append(path)
            continue
        sha, size = entries[path]
        matched.append({"path": path, "sha": sha, "size": size})
    return matched, ignored


def plan_sync(matched, target_entries):
    """按 blob sha 比对，算出哪些要传、哪些已一致

    内容相同的文件 sha 必然相同，据此可在**不下载任何内容**的前提下判等 —— 这也是
    本脚本「增量同步几乎零成本」的关键。

    :param matched: select_source_files 选出的源文件列表
    :param target_entries: 目标分支的文件清单
    :return: (to_copy, identical) —— to_copy 的元素额外带 target_sha（None = 新增）
    """
    to_copy, identical = [], []
    for item in matched:
        target = target_entries.get(item["path"])
        if target and target[0] == item["sha"]:
            identical.append(item)
            continue
        record = dict(item)
        record["target_sha"] = target[0] if target else None
        to_copy.append(record)
    return to_copy, identical


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_parser():
    """构造命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="PullTestDataFiles.py",
        description="把源分支 Data 目录下的测试数据按原路径同步到目标分支（走 API，不 clone）")
    parser.add_argument("--source-branch", required=True,
                        help="源分支（从它的 Data 目录拉取），如 quote")
    parser.add_argument("--target-branch", required=True,
                        help="目标分支（写到它的相同路径），如 datatest")
    parser.add_argument("--source-owner", default="",
                        help="源仓库属主；留空 = 与目标仓库相同（即同仓库跨分支）")
    parser.add_argument("--source-repo", default="",
                        help="源仓库名；留空 = 与目标仓库相同（即同仓库跨分支）")
    parser.add_argument("--path-prefix", default=DEFAULT_PATH_PREFIX,
                        help="源目录前缀，默认 %s" % DEFAULT_PATH_PREFIX)
    parser.add_argument("--pattern", default=DEFAULT_PATTERN,
                        help="文件名通配，多个用逗号分隔，默认 %s" % DEFAULT_PATTERN)
    parser.add_argument("--dry-run", action="store_true",
                        help="只输出同步计划，不写入任何文件")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE,
                        help="GitHub API 仓库根，默认 %s" % DEFAULT_API_BASE)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="单次 HTTP 请求超时秒数，默认 %d" % DEFAULT_TIMEOUT)
    return parser


def _collect_contents(api_base, src_owner, src_repo, tgt_owner, tgt_repo,
                      to_copy, token, timeout):
    """从源仓库逐个下载待同步文件，并在目标仓库建 blob，边下边以 sha 自证

    下载走源仓库、建 blob 走目标仓库 —— 跨仓库时这是两个不同的仓库。构建出的 blob sha
    与源文件的 blob sha 相同，即证明目标仓库收到的字节与源逐字节一致。

    :return: (blobs, err) —— blobs 为 [{"path","sha","blob_sha"}]；任一文件出错即返回 err
    """
    blobs = []
    for item in to_copy:
        path = item["path"]
        content, err = fetch_blob(api_base, src_owner, src_repo, item["sha"],
                                  token, timeout)
        if err:
            return None, "%s：从 %s/%s 下载失败 —— %s" % (path, src_owner, src_repo, err)

        blob_sha, err = create_blob(api_base, tgt_owner, tgt_repo, content, token, timeout)
        if err:
            return None, "%s：在 %s/%s 创建 blob 失败 —— %s" % (path, tgt_owner, tgt_repo, err)
        if blob_sha != item["sha"]:
            # 服务端算出的 sha 与源不符 = 内容被改动过（如 clean 过滤），
            # 此刻尚未建提交，整批中止即可，目标分支仍是原样
            return None, ("%s：内容校验失败 —— 服务端 blob sha=%s 与源 sha=%s 不一致"
                          "（内容被改动过）" % (path, blob_sha[:8], item["sha"][:8]))
        _log("[INFO]   %s 已就绪（%d 字节，sha=%s）" % (path, item["size"], blob_sha[:8]))
        blobs.append({"path": path, "sha": item["sha"], "blob_sha": blob_sha})
    return blobs, None


def main(argv=None):
    """入口：解析参数 → 取两侧清单 → 比对 → 整批下载并一次提交

    :param argv: 命令行参数列表；None = 取 sys.argv[1:]
    :return: 进程退出码（0 成功 / 1 失败）
    """
    args = build_parser().parse_args(argv)
    src_branch = (args.source_branch or "").strip()
    tgt_branch = (args.target_branch or "").strip()
    prefix = (args.path_prefix or "").strip().strip("/")

    # ---- 1) 参数与环境 ----
    if not src_branch or not tgt_branch:
        _log("[FAIL] 源分支与目标分支都不得为空")
        return 1
    if not prefix:
        _log("[FAIL] 目录前缀不得为空")
        return 1
    patterns = parse_patterns(args.pattern)
    if not patterns:
        _log("[FAIL] 文件匹配模式不得为空（--pattern，多个用逗号分隔）")
        return 1
    pattern_label = "、".join(patterns)

    token = (os.environ.get(ENV_TOKEN) or "").strip()
    if not token:
        _log("[FAIL] 请设置环境变量 %s（需目标仓库写权限）" % ENV_TOKEN)
        return 1

    # 目标仓库身份取自本仓 Commit.json（登记的是本仓自己）；
    # 源仓库未指定时默认与目标相同，即退化为「同一仓库内跨分支搬运」
    cfg = GitHubCommitContent.load_commit_config() or {}
    tgt_owner = (cfg.get("owner") or "").strip()
    tgt_repo = (cfg.get("repo") or "").strip()
    if not (tgt_owner and tgt_repo):
        _log("[FAIL] 未能从 Commit.json 解析出 owner/repo，无法定位目标仓库")
        return 1
    src_owner = (args.source_owner or "").strip() or tgt_owner
    src_repo = (args.source_repo or "").strip() or tgt_repo

    if (src_owner, src_repo, src_branch) == (tgt_owner, tgt_repo, tgt_branch):
        _log("[FAIL] 源与目标是同一仓库的同一分支（%s/%s:%s）—— 无内容可同步，"
             "疑似参数填错；已中止以免误判为同步成功"
             % (src_owner, src_repo, src_branch))
        return 1

    _log("[INFO] 源   = %s/%s : %s" % (src_owner, src_repo, src_branch))
    _log("[INFO] 目标 = %s/%s : %s" % (tgt_owner, tgt_repo, tgt_branch))
    _log("[INFO] 目录 = %s/ | 匹配 = %s | 模式 = %s"
         % (prefix, pattern_label,
            "dry-run（只列计划、不写入）" if args.dry_run else "实际同步"))

    # ---- 2) 两侧清单 ----
    source_tree, err = fetch_tree(args.api_base, src_owner, src_repo, src_branch,
                                  token, args.timeout)
    if err:
        _log("[FAIL] 读取源（%s/%s:%s）文件清单失败：%s"
             % (src_owner, src_repo, src_branch, err))
        return 1
    target_tree, err = fetch_tree(args.api_base, tgt_owner, tgt_repo, tgt_branch,
                                  token, args.timeout)
    if err:
        _log("[FAIL] 读取目标（%s/%s:%s）文件清单失败：%s"
             % (tgt_owner, tgt_repo, tgt_branch, err))
        return 1
    _log("[INFO] 文件清单：源 %s 个文件，目标 %s 个文件"
         % (len(source_tree), len(target_tree)))

    # ---- 3) 挑选与比对 ----
    matched, ignored = select_source_files(source_tree, prefix, patterns)
    if ignored:
        shown = "、".join(ignored[:5]) + ("…" if len(ignored) > 5 else "")
        _log("[INFO] %s/ 下有 %d 个文件不匹配 %s，已忽略：%s"
             % (prefix, len(ignored), pattern_label, shown))
    if not matched:
        _log("[INFO] %s/ 下没有匹配 %s 的文件，正常结束（未做任何改动）"
             % (prefix, pattern_label))
        return 0

    to_copy, identical = plan_sync(matched, target_tree)
    _log("[INFO] 匹配到 %d 个文件：需同步 %d 个，已一致跳过 %d 个"
         % (len(matched), len(to_copy), len(identical)))
    for item in to_copy:
        _log("[INFO]   %s %s（%d 字节）"
             % ("新增" if item["target_sha"] is None else "覆盖", item["path"], item["size"]))

    if args.dry_run:
        _log("[PASS] dry-run：同步计划如上，未写入任何文件")
        return 0
    if not to_copy:
        _log("[PASS] 目标已与源一致，无需提交（未做任何改动）")
        return 0

    # ---- 4) 超限文件先把关：整批不落地，故任一超限即中止 ----
    oversize = [i for i in to_copy if i["size"] > MAX_FILE_BYTES]
    if oversize:
        for item in oversize:
            _log("[FAIL] %s：%d 字节，超过单文件上限（%d 字节）"
                 % (item["path"], item["size"], MAX_FILE_BYTES))
        _log("[FAIL] 本批共 %d 个文件超限；整批一次提交失败即不落地，"
             "故中止本次同步（目标分支未改动）。可用 --pattern 排除这些文件后重试"
             % len(oversize))
        return 1

    # ---- 5) 从源仓库下载 + 在目标仓库建 blob（内容在此逐字节自证）----
    _log("[INFO] 从 %s/%s 下载并在 %s/%s 创建 blob：%d 个文件"
         % (src_owner, src_repo, tgt_owner, tgt_repo, len(to_copy)))
    blobs, err = _collect_contents(args.api_base, src_owner, src_repo,
                                   tgt_owner, tgt_repo, to_copy, token, args.timeout)
    if err:
        _log("[FAIL] %s" % err)
        _log("[FAIL] 已在建提交之前中止：目标分支未改动（blob 为游离对象，不会被引用）")
        return 1

    # ---- 6) 打包成一次提交 ----
    head, err = fetch_branch_head(args.api_base, tgt_owner, tgt_repo, tgt_branch,
                                  token, args.timeout)
    if err:
        _log("[FAIL] 读取目标分支 head 失败：%s" % err)
        return 1

    tree_sha, err = create_tree(args.api_base, tgt_owner, tgt_repo, head["tree_sha"],
                                blobs, token, args.timeout)
    if err:
        _log("[FAIL] 创建树失败：%s" % err)
        return 1

    commit_msg = "chore(testdata): 从 %s/%s:%s 同步测试数据到 %s（%d 个文件）" % (
        src_owner, src_repo, src_branch, tgt_branch, len(blobs))
    commit_sha, err = create_commit(args.api_base, tgt_owner, tgt_repo, commit_msg,
                                    tree_sha, head["commit_sha"], token, args.timeout)
    if err:
        _log("[FAIL] 创建提交失败：%s" % err)
        return 1

    _, err = update_ref(args.api_base, tgt_owner, tgt_repo, tgt_branch, commit_sha,
                        token, args.timeout)
    if err:
        _log("[FAIL] 移动 %s 分支指针失败：%s" % (tgt_branch, err))
        _log("[FAIL] 目标分支保持原样（提交对象已生成但未被引用）")
        return 1
    _log("[PASS] 已提交到 %s/%s:%s：https://github.com/%s/%s/commit/%s"
         % (tgt_owner, tgt_repo, tgt_branch, tgt_owner, tgt_repo, commit_sha))

    # ---- 7) 复核：重新取目标分支清单，逐路径比对 sha ----
    verify_tree, err = fetch_tree(args.api_base, tgt_owner, tgt_repo, tgt_branch,
                                  token, args.timeout)
    if err:
        _log("[WARN] 提交已成功，但复核时读取目标分支清单失败：%s" % err)
        return 0
    mismatched = [b["path"] for b in blobs
                  if verify_tree.get(b["path"], ("", 0))[0] != b["sha"]]
    if mismatched:
        _log("[FAIL] 复核不通过：%d 个路径的落盘 sha 与源不一致：%s"
             % (len(mismatched), "、".join(mismatched[:5])))
        return 1

    _log("[INFO] 完成：一次提交 %d 个文件 / 已一致跳过 %d 个 / 复核 %d 个路径的 sha 全部一致"
         % (len(blobs), len(identical), len(blobs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
