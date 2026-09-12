#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBSClient —— 将仓库数据文件上传到华为云 OBS 存储桶（纯标准库实现）
========================================================================================

一、定位
----------------------------------------------------------------------------------------
供 UpstreamUpload 等流程调用：把仓库文件逐个真实上传到华为云 OBS 对象存储。

    桶      ：gza（华南-广州）
    目录结构：obs://gza/{OBSRootPrefix}/{yyyyMMdd}/{CID}/{文件名}
    说明    ：
      - OBSRootPrefix = key 前缀（如 GitHub/EventGridOBSStorage/G0128M），由
        调用方从环境变量 HWC_OBS_ROOT_PREFIX 读取（原 Upstream.json 的
        OBSRootPrefix，已移出配置文件）、经 root_prefix 参数传入，本模块不内置；
      - CID 段：未指定 cid 时用本模块默认常量 DEFAULT_OBS_CID（3501806882199176893）；
        调用方可按 Upstream.json 的前缀路由规则为每个文件解析出各自 CID（经 cid 参数
        传入），从而让不同目录下的文件落到桶内不同 CID 目录；
      - {yyyyMMdd} 段 = 上传【东八区(UTC+8)运行当天】的日期（yyyyMMdd，整批统一），
        与文件内容无关；即便程序部署地域不在东八区，也按东八区日期归日（east8_today）；
      - 对象文件名 = 上传文件的【文件名】（清单行 basename）；
      - 调用方显式传入 key 时【优先用调用方指定的 key】，未传（None）才按上述规则自动构造。

二、鉴权与实现方式
----------------------------------------------------------------------------------------
- AK/SK 与区域/桶从环境变量读取：
      HWC_OBS_AK         —— 必填
      HWC_OBS_SK         —— 必填
      HWC_OBS_REGION_ID  —— 可选，区域 ID（默认 cn-south-1 华南-广州）；
                           无需配置冗长的端点变量，端点由区域 ID 推导：
                           obs.{region_id}.myhuaweicloud.com
      HWC_OBS_BUCKET     —— 可选，存储桶名（默认 gza）
  upload_file 在【真正发请求前】先经 get_init_params() 校验上述环境变量是否配置，
  缺失时直接返回失败 dict，不发起任何网络请求。
- 纯 Python 标准库（urllib + hmac + base64）实现 OBS 原生 REST 签名：
      Authorization: OBS {AK}:{Signature}
      Signature = Base64( HMAC-SHA1( SK, StringToSign ) )
      StringToSign = HTTP-Verb + "\\n" + Content-MD5 + "\\n" + Content-Type
                     + "\\n" + Date + "\\n" + CanonicalizedResource
      CanonicalizedResource = "/{bucket}/{objectKey}"
  （对象名取原始解码形态；当前数据文件名均为 URL 安全字符，与请求路径一致。）
- 对象级请求使用【虚拟主机域名】virtual-host 风格（桶名作为子域名）：
      https://{bucket}.{endpoint}/{objectKey}
  例如 https://gza.obs.cn-south-1.myhuaweicloud.com/GitHub/...
  若把桶名放进路径访问 obs.cn-south-1.myhuaweicloud.com，OBS 将返回 403
  VirtualHostDomainRequired；签名里的 CanonicalizedResource 仍含桶（/gza/...）。
- 服务端原生回调（可选、配置驱动）：
      调用方把回调端点（env HWC_OBS_CALLBACK_URL，原 Upstream.json 的 CallbackUrl，
      已移出配置文件）经 upload_file 的 callback_url 参数传入后，PUT 请求会附带
      x-obs-callback 头（值 = Base64 编码的 JSON，含
      callbackUrl / callbackBody / callbackBodyType），对象落盘后由 OBS 主动回调该端点。
      callbackBody 为 JSON（callbackBodyType=application/json），真桶实测结论：
        - 仓库侧上下文（owner / repo / branch / rel_path / domain / file_url）与对象
          key 以字面量写入 JSON，可【原样到达】回调端点（已实测）；
        - OBS 系统变量 $(bucket) / $(etag) / $(size) 会展开为真实值并入 JSON（已实测）；
          $(object) 实测展开为 null（OBS 不识别），故对象 key 改走字面量 key= 字段；
        - 实测确认：application/json 时 OBS 把渲染后的 JSON 放进【请求体】(POST body)、
          回调 URL 不带参数；若用 application/x-www-form-urlencoded，OBS 则把键值对
          拼到回调 URL 查询串且请求体为空（两种均已实测，本实现默认 application/json）。
      一旦携带 x-obs-* 头，签名 CanonicalizedHeaders 段不再为空（见上）。

三、约定接口
----------------------------------------------------------------------------------------
    def upload_file(local_file, rel_path, key=None, *,
                    owner=None, repo=None, branch=None,
                    sha1=None, md5=None, root_prefix=None, cid=None,
                    callback_url=None, **callback_extra)
    :param local_file : 本地待上传文件的【绝对路径】（上传源）
    :param rel_path   : 该文件相对【仓库根目录】的路径（仅当 key 未指定时，取其
                        basename 作为对象名参与 key 自动构造；回调上下文用）
    :param key        : 对象 key；None = 按规则自动构造，非 None = 原样使用
    :param owner/repo/branch : 来源仓库身份，回调/登记上下文（不影响上传目标）
    :param sha1/md5   : 文件哈希（回调上下文，暂不在请求中使用）
    :param root_prefix: OBSRootPrefix（如 GitHub/EventGridOBSStorage/G0128M），
                        由调用方从 env HWC_OBS_ROOT_PREFIX 注入；key 为 None 时用于自动构造
    :param cid        : CID 目录段；None = 默认 DEFAULT_OBS_CID。调用方可按
                        Upstream.json 前缀路由规则逐文件解析 CID，实现分目录保存
    :param callback_url: 服务端原生回调端点；None/空 = 不上传 x-obs-callback 头
                        （默认不回调）；配置后由 OBS 对象落盘成功时主动回调
    :param callback_extra : 其余扩展参数，透传保留
    :return: dict { "success": bool, "message": str, "key": str,
                    "http_status": int|None }
        - success=True ：已上传成功（HTTP 2xx）；
        - success=False：失败，message 为原因；http_status 为响应状态码或 None。
"""

import base64
import datetime
import email.utils
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 常量与配置（默认值）
# ---------------------------------------------------------------------------

# AK/SK 环境变量名（初始化前必须配置，缺失则拒绝上传）
ENV_OBS_AK = "HWC_OBS_AK"
ENV_OBS_SK = "HWC_OBS_SK"
# 区域 ID / 桶名环境变量名（均可选，缺省用下面的默认值）：
#   HWC_OBS_REGION_ID —— 端点由它推导，无需配冗长端点变量
#   HWC_OBS_BUCKET    —— 存储桶名
ENV_OBS_REGION_ID = "HWC_OBS_REGION_ID"
ENV_OBS_BUCKET = "HWC_OBS_BUCKET"

# OBS 目标：华南-广州（cn-south-1）桶 gza（均可被上述环境变量覆盖）。
# 注意：对象级请求使用 virtual-host 域名 {bucket}.{endpoint}（桶作子域名），
# 桶名不放路径；否则 OBS 返回 403 VirtualHostDomainRequired。
DEFAULT_OBS_REGION_ID = "cn-south-1"
OBS_ENDPOINT_TMPL = "obs.%s.myhuaweicloud.com"  # 端点 = obs.{region_id}.myhuaweicloud.com
DEFAULT_OBS_BUCKET = "gza"

# 对象 key 目录骨架：…/{OBSRootPrefix}/{yyyyMMdd}/{CID}/{文件名}
# OBSRootPrefix（如 GitHub/EventGridOBSStorage/G0128M）由调用方从 env
# HWC_OBS_ROOT_PREFIX 读取（原 Upstream.json 的 OBSRootPrefix，已移出配置文件）、
# 经 root_prefix 参数传入（本模块不内置默认前缀）。
DEFAULT_OBS_CID = "3501806882199176893"
DATE_KEY_FORMAT = "%Y%m%d"

# 单次上传 HTTP 超时（秒）
UPLOAD_TIMEOUT = 120

# ---------------------------------------------------------------------------
# 服务端原生上传回调（可选、配置驱动）
#   callback_url 由调用方从 env HWC_OBS_CALLBACK_URL 读取（原 Upstream.json 的
#   CallbackUrl，已移出配置文件）、经 upload_file 的 callback_url 参数传入
#   （本模块不内置默认端点）。配置了才在 PUT 请求携带
#   x-obs-callback 头，由 OBS 在对象落盘成功后主动回调 callback_url；
#   未配置则不携带该头，行为与未开回调时一致。
# ---------------------------------------------------------------------------
OBS_HEADER_CALLBACK = "x-obs-callback"
# 回调请求体类型。真桶实测：
#   application/json            -> OBS 把渲染后的 JSON 放进 POST 请求体（推荐，本默认）
#   application/x-www-form-urlencoded -> OBS 把键值对拼到回调 URL 查询串且请求体为空
CALLBACK_BODY_TYPE = "application/json"
# 来源仓库托管域：回调参数 domain= 字段的默认值（来源主机名，如 github.com）
DEFAULT_CALLBACK_DOMAIN = "github.com"

# ---------------------------------------------------------------------------
# 一键开关：入参打印（调试用）
#   PRINT_UPLOAD_ARGS = True  → 每次调用 upload_file 都打印入参；
#   PRINT_UPLOAD_ARGS = False → 一键关闭。默认 False（正式上传不刷屏）。
# ---------------------------------------------------------------------------
PRINT_UPLOAD_ARGS = False


def _print_upload_args(local_file, rel_path, key=None,
                       owner=None, repo=None, branch=None,
                       sha1=None, md5=None, root_prefix=None, cid=None,
                       callback_url=None, callback_extra=None):
    """打印 upload_file 的入参（单独成方法，便于一键开启/关闭调试输出）

    :param local_file: 本地待上传文件的绝对路径
    :param rel_path: 该文件相对仓库根目录的路径（清单行原值）
    :param key: 对象 key
    :param owner: 来源仓库属主（回调上下文）
    :param repo: 来源仓库名（回调上下文）
    :param branch: 来源分支（回调上下文）
    :param sha1: 文件 SHA-1（预留）
    :param md5: 文件 MD5（大写 hex）
    :param root_prefix: OBSRootPrefix（key 自动构造用）
    :param cid: CID 目录段（key 自动构造用）
    :param callback_url: 服务端原生回调端点（None/空 = 不带 x-obs-callback 头）
    :param callback_extra: 其余回调扩展参数 dict；None 视为空 dict
    :return: 无
    """
    extra = callback_extra or {}
    print("[OBSClient] upload_file 入参：")
    print("    local_file  = %r" % (local_file,))
    print("    rel_path    = %r" % (rel_path,))
    print("    key         = %r" % (key,))
    print("    owner       = %r" % (owner,))
    print("    repo        = %r" % (repo,))
    print("    branch      = %r" % (branch,))
    print("    sha1        = %r" % (sha1,))
    print("    md5         = %r" % (md5,))
    print("    root_prefix = %r" % (root_prefix,))
    print("    cid         = %r" % (cid,))
    print("    callback_url= %r" % (callback_url,))
    if extra:
        for name in sorted(extra):
            print("    extra.%s    = %r" % (name, extra[name]))
    else:
        print("    callback_extra = (空)")


# ---------------------------------------------------------------------------
# 初始化参数
# ---------------------------------------------------------------------------

def get_init_params(env=None):
    """校验环境变量并组装 OBS 初始化参数（发起请求前必须成功）

    :param env: 环境变量映射（默认 os.environ，便于测试注入）
    :return: (params, err)：
        - 成功：params = {"ak", "sk", "endpoint", "bucket"}，err = None；
          其中 endpoint = obs.{region_id}.myhuaweicloud.com，region_id/桶 可经
          HWC_OBS_REGION_ID / HWC_OBS_BUCKET 覆盖（缺省 cn-south-1 / gza）；
        - 失败：params = None，err = 缺失的环境变量提示（不读取/不暴露 AK/SK 值）
    """
    env = os.environ if env is None else env

    def grab(name):
        val = env.get(name)
        return val.strip() if val else ""

    ak, sk = grab(ENV_OBS_AK), grab(ENV_OBS_SK)
    missing = [name for name, val in ((ENV_OBS_AK, ak), (ENV_OBS_SK, sk)) if not val]
    if missing:
        return None, "OBS 初始化参数缺失：环境变量 %s 未配置（拒绝上传）" % "、".join(missing)
    region_id = grab(ENV_OBS_REGION_ID) or DEFAULT_OBS_REGION_ID
    bucket = grab(ENV_OBS_BUCKET) or DEFAULT_OBS_BUCKET
    endpoint = OBS_ENDPOINT_TMPL % region_id
    return {"ak": ak, "sk": sk, "endpoint": endpoint,
            "bucket": bucket}, None


# ---------------------------------------------------------------------------
# 东八区时间（对象 key 的 yyyyMMdd 日期取东八区，与程序部署地域无关）
# ---------------------------------------------------------------------------

TZ_EAST8 = datetime.timezone(datetime.timedelta(hours=8))  # 东八区 UTC+8


def east8_now():
    """当前东八区时间（tz-aware datetime，与程序部署地域无关）

    程序部署地域可能不是东八区；统一取 UTC+8 时间，保证跨地域“当天”口径一致。

    :return: datetime（tzinfo = 东八区）
    """
    return datetime.datetime.now(TZ_EAST8)


def east8_today():
    """当前东八区日期（date）

    OBS 对象 key 的 {yyyyMMdd} 段默认取此日期：即使程序部署在非东八区，
    也按东八区（北京时间）当天归日，避免跨时区把“运行当天”切错。

    :return: date
    """
    return east8_now().date()


# ---------------------------------------------------------------------------
# 对象 key
# ---------------------------------------------------------------------------

def build_default_key(rel_path, when=None, root_prefix=None, cid=None):
    """按规则构造默认对象 key

    规则：{OBSRootPrefix}/{运行当天yyyyMMdd}/{CID}/{文件名}
    OBSRootPrefix 不内置默认值，由调用方从 env HWC_OBS_ROOT_PREFIX 读取后传入。

    :param rel_path: 该文件相对仓库根目录的路径（取 basename 作为对象文件名）
    :param when: 日期（默认东八区当天 east8_today()）；yyyyMMdd 段即取自此日期。
                 传 date/datetime 便于测试注入
    :param root_prefix: OBSRootPrefix 前缀（如 GitHub/EventGridOBSStorage/G0128M）
    :param cid: CID 目录段；None = 默认 DEFAULT_OBS_CID（调用方可按路由规则传入）
    :return: 对象 key（不含前导斜杠）
    :raises ValueError: rel_path 无有效文件名，或 root_prefix 为空时
    """
    rel = (rel_path or "").replace("\\", "/").strip("/")
    name = rel.split("/")[-1]
    if not name:
        raise ValueError("build_default_key: 无法从相对路径 %r 取到文件名" % (rel_path,))
    prefix = (root_prefix or "").strip().strip("/")
    if not prefix:
        raise ValueError("build_default_key: 缺少 root_prefix（应由调用方从 "
                         "env HWC_OBS_ROOT_PREFIX 读取传入）")
    dt = when or east8_today()
    return "%s/%s/%s/%s" % (prefix, dt.strftime(DATE_KEY_FORMAT),
                            (cid or DEFAULT_OBS_CID), name)


# ---------------------------------------------------------------------------
# 签名与请求
# ---------------------------------------------------------------------------

_CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv",
}


def _guess_content_type(local_file):
    """根据扩展名推断 Content-Type

    :param local_file: 本地文件路径
    :return: Content-Type 字符串；无法推断时用 application/octet-stream
    """
    ext = os.path.splitext(local_file)[1].lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(local_file)
    return guessed or "application/octet-stream"


def _gmt_now():
    """当前 UTC 时间，RFC1123 格式（如 Tue, 09 Sep 2026 08:00:00 GMT）"""
    return email.utils.formatdate(time.time(), usegmt=True)


def _canonicalized_headers(headers):
    """按 OBS 签名规则构造 CanonicalizedHeaders 段（仅收集 x-obs- 前缀头）

    规则：头名转小写并按字典序排序，逐行拼接 "名称:值\n"（值去除首尾空白）；
    请求未携带任何 x-obs- 头时返回空串（此时签名的 Date 与 CanonicalizedResource
    直接相邻）。目前会命中的头只有 x-obs-callback（服务端原生回调）。

    :param headers: 请求头 dict（原始大小写均可）
    :return: CanonicalizedHeaders 字符串；无 x-obs- 头时为空串
    """
    items = []
    for name, value in (headers or {}).items():
        lower = str(name).lower()
        if lower.startswith("x-obs-"):
            items.append((lower, str(value).strip()))
    if not items:
        return ""
    items.sort(key=lambda pair: pair[0])
    return "".join("%s:%s\n" % (name, value) for name, value in items)


def _canonical_resource(bucket, object_key):
    """计算 CanonicalizedResource：/bucket/objectKey

    对象名取原始（解码）形态；当前数据文件名均为 URL 安全字符，与请求路径一致。

    :param bucket: 桶名
    :param object_key: 对象 key
    :return: 形如 "/gza/GitHub/..." 的资源串
    """
    return "/%s/%s" % (bucket, object_key)


def _compute_signature(sk, method, canonicalized_resource,
                       content_md5, content_type, date_header,
                       canonicalized_headers=""):
    """计算 OBS 原生签名（HMAC-SHA1）

    StringToSign = VERB\\n Content-MD5\\n Content-Type\\n Date\\n
                   CanonicalizedHeaders + CanonicalizedResource
    其中 CanonicalizedHeaders 为请求中所有 x-obs-* 头（名称小写、按字典序、
    逐行 "名称:值\\n"）的拼接结果；未携带 x-obs-* 头时为空串，Date 与
    CanonicalizedResource 直接相邻，等价经典四段签名。

    :param sk: Secret Access Key
    :param method: HTTP 方法（大写，如 PUT）
    :param canonicalized_resource: _canonical_resource() 的结果
    :param content_md5: Content-MD5 值（未携带传空串）
    :param content_type: Content-Type 值（与请求头一致）
    :param date_header: Date 头值（与请求头一致，RFC1123）
    :param canonicalized_headers: _canonicalized_headers() 的结果（可为空串）
    :return: Base64 编码的签名串
    """
    string_to_sign = "%s\n%s\n%s\n%s\n%s%s" % (
        method, content_md5 or "", content_type or "",
        date_header, canonicalized_headers or "", canonicalized_resource)
    digest = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


# OBS 落盘成功后由 OBS 展开的“自带回调参数”（系统变量占位）。
# 真桶实测：$(bucket)/$(etag)/$(size) 均有效；$(object) 实测展开为 null
# （OBS 不识别该变量名），故对象 key 由调用方以字面量经 object_key 传入
# （回调字段名 key=），不列入系统变量。
_CALLBACK_SYSTEM_VARS = (("bucket", "$(bucket)"),
                         ("etag", "$(etag)"), ("size", "$(size)"))


def build_callback_body(owner=None, repo=None, branch=None, rel_path=None,
                        object_key=None, domain=None, include_system_vars=True,
                        cid=None):
    """构造服务端原生回调的请求体（JSON 文本，callbackBodyType=application/json）

    回调体为 JSON 对象：仓库侧上下文（owner / repo / branch / rel_path / cid /
    domain / file_url）与对象 key 由客户端【按字面量】写入，真桶实测可原样到达回调
    端点；OBS 侧参数（bucket / etag / size）以系统变量占位 $(...)，由 OBS 在对象落盘
    成功后展开为真实值并入 JSON 字符串值（真桶实测 $(bucket)/$(etag)/$(size) 有效；
    $(object) 实测不识别，故对象 key 走 object_key 字面量）。

    :param owner: 来源仓库属主
    :param repo: 来源仓库名
    :param branch: 来源分支
    :param rel_path: 该文件相对仓库根目录的路径（原值，含目录层级）
    :param object_key: 本次实际使用的对象 key（客户端已知，字面量写入 key 字段）
    :param domain: 来源仓库托管域；None = DEFAULT_CALLBACK_DOMAIN（github.com）。
                   回调 domain 字段取此值，file_url 的 https://{domain} 前缀同源
    :param include_system_vars: 是否追加 OBS 系统变量字段（默认 True）
    :param cid: 业务分组目录段 CID；非空时以字面量写入 cid 字段，供回调处理端
                （Cloudflare Worker）把回调数据归档到 Archive/Branch/{branch}/{cid}/ 路径
    :return: JSON 文本（作 x-obs-callback 的 callbackBody）
    """
    host = domain or DEFAULT_CALLBACK_DOMAIN
    obj = {}
    for name, value in (("owner", owner), ("repo", repo), ("branch", branch),
                        ("rel_path", rel_path)):
        if value:
            obj[name] = value
    cid = str(cid or "").strip()
    if cid:
        obj["cid"] = cid
    obj["domain"] = host
    if object_key:
        obj["key"] = object_key
    if owner and repo and branch and rel_path:
        obj["file_url"] = "https://%s/%s/%s/blob/%s/%s" % (
            host, owner, repo, branch, str(rel_path).replace("\\", "/"))
    if include_system_vars:
        obj["bucket"] = "$(bucket)"
        obj["etag"] = "$(etag)"
        obj["size"] = "$(size)"
    return json.dumps(obj, ensure_ascii=False)


def build_callback_header(callback_url, callback_body, body_type=None):
    """构造 x-obs-callback 请求头值

    OBS 要求回调参数以 Base64 编码的 JSON 字符串形式随请求携带（整体集中在
    本函数构造，便于真桶实测后一次性调整编码方式——若 OBS 拒绝本编码，上传将
    报错且日志可见，届时改为 json.dumps 明文即可）。

    :param callback_url: 回调端点 URL（如 https://.../API/V1/GitHub/CallBack）
    :param callback_body: build_callback_body() 构造的请求体（JSON 文本模板）
    :param body_type: callbackBodyType；None = CALLBACK_BODY_TYPE（默认
                      application/json，实测参数进请求体）
    :return: x-obs-callback 头的值
    """
    body_type = body_type or CALLBACK_BODY_TYPE
    payload = {"callbackUrl": callback_url,
               "callbackBody": callback_body,
               "callbackBodyType": body_type}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _http_put(url, body, headers, timeout=UPLOAD_TIMEOUT):
    """执行一次 HTTP PUT（真实网络调用）

    :param url: 完整请求 URL
    :param body: 请求体 bytes
    :param headers: 请求头 dict
    :param timeout: 超时秒数
    :return: (status, resp_headers, resp_body)
    :raises urllib.error.HTTPError: 服务端返回非 2xx 时抛出（由 upload_file 捕获）
    :raises urllib.error.URLError / OSError: 网络层异常
    """
    req = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), dict(resp.headers), \
            resp.read().decode("utf-8", "replace")


def upload_file(local_file, rel_path, key=None,
                owner=None, repo=None, branch=None,
                sha1=None, md5=None, root_prefix=None, cid=None,
                callback_url=None, **callback_extra):
    """将本地文件上传到 OBS（真实实现，纯标准库）

    key 处理：
        - 调用方显式传入 key（非 None）→ 原样作为对象 key；
        - key 为 None → 按 build_default_key 规则自动构造
          （{OBSRootPrefix}/{运行当天yyyyMMdd}/{CID}/{文件名}，
          OBSRootPrefix 由 root_prefix 提供（来自 env HWC_OBS_ROOT_PREFIX），CID 由 cid 提供
          （None = 默认，调用方可按前缀路由规则逐文件解析各自 CID））。

    上传前先 get_init_params() 校验 HWC_OBS_AK / HWC_OBS_SK 是否已配置，
    缺失则直接返回失败 dict，不发起网络请求。

    :param local_file: 本地待上传文件的绝对路径
    :param rel_path: 该文件相对仓库根目录的路径（key 自动构造时取其文件名）
    :param key: 对象 key；None = 自动构造
    :param owner: 来源仓库属主（回调上下文）
    :param repo: 来源仓库名（回调上下文）
    :param branch: 来源分支（回调上下文）
    :param sha1: 文件 SHA-1（预留）
    :param md5: 文件 MD5（大写 hex，回调上下文）
    :param root_prefix: OBSRootPrefix（如 GitHub/EventGridOBSStorage/G0128M），
                        由调用方从 env HWC_OBS_ROOT_PREFIX 注入；key 为 None 时用于自动构造
    :param cid: CID 目录段；None = 默认 DEFAULT_OBS_CID。调用方可按
                Upstream.json 前缀路由规则为每个文件解析各自 CID（分目录保存）；
                配置回调时，cid 还会作为字面量字段写入回调请求体，供回调处理端
                （Cloudflare Worker）归档到 Archive/Branch/{branch}/{cid}/ 路径
    :param callback_url: 服务端原生回调端点；None/空 = 不带 x-obs-callback 头
                         （默认不回调）；配置后对象落盘成功由 OBS 主动回调，
                         回调体含仓库上下文（字面量）+ OBS 系统变量（落盘展开）
    :param callback_extra: 其余扩展参数，透传保留
    :return: {"success": bool, "message": str, "key": str,
              "http_status": int|None}
    """
    if PRINT_UPLOAD_ARGS:
        _print_upload_args(local_file, rel_path, key=key, owner=owner,
                           repo=repo, branch=branch, sha1=sha1, md5=md5,
                           root_prefix=root_prefix, cid=cid,
                           callback_url=callback_url,
                           callback_extra=callback_extra)

    # 1) 校验 AK/SK 等初始化参数（未配置则直接失败，不发请求）
    params, err = get_init_params()
    if params is None:
        return {"success": False, "message": err,
                "key": key, "http_status": None}

    # 2) 确定对象 key：显式指定 > 自动构造（root_prefix 由调用方从 env HWC_OBS_ROOT_PREFIX
    #    注入；cid 由调用方按 Upstream.json 的 OBSCIDRoutes 前缀路由规则解析传入）
    try:
        object_key = key or build_default_key(rel_path, root_prefix=root_prefix,
                                              cid=cid)
    except ValueError as e:
        return {"success": False, "message": str(e),
                "key": key, "http_status": None}

    # 3) 读取本地文件内容
    try:
        with open(local_file, "rb") as f:
            body = f.read()
    except OSError as e:
        return {"success": False,
                "message": "读取本地文件失败 %s: %s" % (local_file, e),
                "key": object_key, "http_status": None}

    # 4) 构造签名请求（可选携带 x-obs-callback 服务端原生回调头）
    content_type = _guess_content_type(local_file)
    date_header = _gmt_now()
    resource = _canonical_resource(params["bucket"], object_key)
    headers = {"Date": date_header, "Content-Type": content_type}
    callback_url = (callback_url or "").strip()
    if callback_url:
        # 回调参数字符串：仓库上下文 + cid + 对象 key 按字面量固化，
        # OBS 系统变量占位（bucket/etag/size，落盘后由 OBS 展开）
        body_tpl = build_callback_body(owner=owner, repo=repo, branch=branch,
                                       rel_path=rel_path, object_key=object_key,
                                       cid=cid)
        headers[OBS_HEADER_CALLBACK] = build_callback_header(callback_url, body_tpl)
    canonical_headers = _canonicalized_headers(headers)
    signature = _compute_signature(
        params["sk"], "PUT", resource,
        content_md5="", content_type=content_type, date_header=date_header,
        canonicalized_headers=canonical_headers)
    quoted = urllib.parse.quote(object_key, safe="/")
    # virtual-host 域名：桶名作子域名，路径不含桶
    url = "https://%s.%s/%s" % (params["bucket"], params["endpoint"], quoted)
    headers["Authorization"] = "OBS %s:%s" % (params["ak"], signature)

    # 5) 发送 PUT（2xx = 成功，其余/网络异常 = 失败）
    try:
        status, _, resp_body = _http_put(url, body, headers)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace") if e.fp else ""
        return {"success": False,
                "message": "OBS 上传失败 HTTP %s: %s" % (e.code, detail.strip()[:200]),
                "key": object_key, "http_status": e.code}
    except (urllib.error.URLError, OSError) as e:
        return {"success": False,
                "message": "OBS 上传网络异常: %s" % (e,),
                "key": object_key, "http_status": None}

    if 200 <= status < 300:
        message = "已上传 obs://%s/%s（HTTP %s）" % (params["bucket"],
                                                    object_key, status)
        if callback_url:
            message += "；已配置服务端原生回调至 %s" % callback_url
        return {"success": True, "message": message,
                "key": object_key, "http_status": status}
    return {"success": False,
            "message": "OBS 上传失败 HTTP %s: %s" % (status, resp_body.strip()[:200]),
            "key": object_key, "http_status": status}


if __name__ == "__main__":
    # 直接执行：仅做配置自检 + 打印 key 构造示例，不做任何真实上传
    print("OBSClient —— 配置自检（不发起任何 OBS 请求）")
    cfg, err = get_init_params()
    if cfg is None:
        print("[FAIL] %s" % err)
        raise SystemExit(1)
    print("[PASS] 桶 = %s | 上传域名 = %s.%s | AK/SK 已从环境变量读取"
          % (cfg["bucket"], cfg["bucket"], cfg["endpoint"]))
    # OBSRootPrefix 从环境变量读取（与 DMDCBWD31MigrationFile 的 load_obs_runtime_env 一致）
    prefix = os.environ.get("HWC_OBS_ROOT_PREFIX", "").strip()
    if not prefix:
        print("（未设置环境变量 HWC_OBS_ROOT_PREFIX，跳过示例 key 演示）")
    else:
        print("[PASS] HWC_OBS_ROOT_PREFIX = %s（取自环境变量）" % prefix)
        for sample in ("UpStream/Archive/20260825/CN_AQuoteList.json",
                       "UpStream/a.jsonl"):
            print("  示例 key（%s） -> %s"
                  % (sample, build_default_key(sample, root_prefix=prefix)))
