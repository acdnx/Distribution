#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ManifestJsonl —— 清单文件（UploadFileList / UploadSuccessList）的 JSONL 格式实现
========================================================================================

一、格式约定
----------------------------------------------------------------------------------------
清单**一行一个 JSON 对象**（JSONL / NDJSON），UTF-8、LF 换行、末行带换行：

    {"dt":"20260912124805000","sha1":"8A38700E...","md5":"07BE15F0...","rel_path":"Data/Demo.json"}

四个键，顺序固定：

    dt        该文件在 git 中最后一次被提交的时间，yyyyMMddHHmmssSSS、东八区（GMT+8）。
              稳定值——文件内容不变则同一提交下该字段不变，故 (rel_path, dt, sha1, md5)
              可用于「同一文件同一版本」的唯一标识与增量去重。
              （切勿改用文件系统 mtime：每次 clone/checkout 都会刷新，会让清单天天变。）
    sha1      文件【内容】的 SHA1，大写 hex
    md5       文件【内容】的 MD5，大写 hex
    rel_path  相对【仓库根目录】的路径，分隔符统一为正斜杠

四个键**恒存在**；取值未知时写空串 ""（不写 null，也不省略键）——
读端因此永远可以用 obj["rel_path"] 取值，其余三键取值前用 .get() 即可。

二、为什么是 JSONL 而不是分隔符文本
----------------------------------------------------------------------------------------
早期格式为 `{dt}|{sha1}|{md5}|{rel_path}` 四元组文本。它的痛点在于：**每加一个字段，
所有解析端都要改切分逻辑并兼容旧行**（路径含 | 时还要靠「路径放最后一位」的技巧兜底）。
JSONL 下新增字段只是多一个键。这正是本次改格式的目的。

三、扩展性：新增字段无需改动中间环节
----------------------------------------------------------------------------------------
本模块对**未知键一律原样透传**——compose 输出时按「已知键（FIELD_ORDER 顺序）+
未知键（按键名排序）」写回，parse 读进来时也保留。于是新增一个字段只需动**产出它的
那一个脚本**：

    DMDCBWD11（产出）加一个键  →  DMDCBWD31（读队列、回写成功清单）原样带过去
                              →  Aggregate（整行聚合）原样带过去

中间的脚本**一行都不用改**。若改成「只保留已知的四个键」，链路里每个会回写清单的
环节都得跟着改一次，就退化回分隔符文本时代的老问题了。

（约定：未知键的取值应为 JSON 标量。传嵌套对象也能工作，但嵌套层的键序由读入时的
文件内容决定，程序内构造时务必保持键序一致，否则会破坏下面的确定性。）

四、确定性：整行去重的前提
----------------------------------------------------------------------------------------
AggregateUploadFileList 按【整行】去重（不解析内容）。要让「同一文件同一版本 → 同一行」
成立，序列化必须**逐字节确定**：本模块对同一组键值总是按同一顺序输出（已知键固定序、
未知键排序），并固定用 separators=(",", ":") 紧凑输出、ensure_ascii=False。
改动本模块的序列化方式时务必维持这一点，否则聚合端会开始堆积重复行。

五、读端的容错口径
----------------------------------------------------------------------------------------
parse() 对空行、纯空白行**静默跳过**（不产生 problem）；对**非空但解析不出**的行记入
problems 返回给调用方，由调用方打印告警。不在这里直接 print，是为了让本模块可被
非控制台场景（测试、批处理）复用。

调用方须注意：**清单非空却一行都没解析出来，应判定为失败而非「无内容可处理」**——
那通常意味着文件还是旧格式、或已被写坏。静默按空清单处理会让整条链路无声停摆。

【环境要求】
    - Python 3.8+，仅标准库；无网络、无文件 IO（纯字符串进出）。
"""

import json

# ---------------------------------------------------------------------------
# 格式常量
# ---------------------------------------------------------------------------

KEY_DT = "dt"
KEY_SHA1 = "sha1"
KEY_MD5 = "md5"
KEY_REL_PATH = "rel_path"

# 序列化时的键序（= to_record 构造新 dict 的顺序）。
# 新增字段请**追加在末尾**：键在中间插入会让所有既有行不再匹配整行去重，
# 聚合端会把同一版本当成两条不同记录各收一次。
FIELD_ORDER = (KEY_DT, KEY_SHA1, KEY_MD5, KEY_REL_PATH)

# 告警里回显行内容时的截断长度
EXCERPT_LIMIT = 120


def _as_field(value):
    """把条目取值规整为清单字段：转字符串、去首尾空白、None → 空串

    :param value: 条目里的原始取值（任意类型）
    :return: 字符串（可能为空串）
    """
    if value is None:
        return ""
    return str(value).strip()


def normalize_rel_path(value):
    """把相对路径规整为清单口径：去首尾空白、分隔符统一为正斜杠

    :param value: 原始路径（任意类型，非字符串按 str() 处理）
    :return: 规整后的路径；None 或空白返回空串
    """
    if value is None:
        return ""
    return str(value).strip().replace("\\", "/")


def to_record(entry):
    """把条目 dict 规整成清单记录（已知键固定序在前、未知键按键名排序在后）

    已知四键恒存在、取值一律为字符串（None → 空串）；**未知键原样保留**，
    以便上游新增的字段穿过中间环节而不被抹掉（见模块文档第三节）。
    键序由本函数决定，故同一组键值必然产出同一顺序 → 序列化逐字节确定。

    :param entry: 条目 dict，键取自 KEY_* 常量；缺失的键按空串处理
    :return: 新 dict，键按 FIELD_ORDER + 未知键升序排列
    """
    record = {
        KEY_DT: _as_field(entry.get(KEY_DT)),
        KEY_SHA1: _as_field(entry.get(KEY_SHA1)),
        KEY_MD5: _as_field(entry.get(KEY_MD5)),
        KEY_REL_PATH: normalize_rel_path(entry.get(KEY_REL_PATH)),
    }
    for key in sorted(k for k in entry if k not in FIELD_ORDER):
        record[key] = entry[key]
    return record


def dumps(entry):
    """把条目序列化成一行 JSONL（不含换行符）

    :param entry: 条目 dict
    :return: 单行文本，如 {"dt":"2026...","sha1":"...","md5":"...","rel_path":"Data/a.json"}
    """
    return json.dumps(to_record(entry), ensure_ascii=False, separators=(",", ":"))


def compose(entries):
    """把条目列表拼成完整清单文本（一行一个，UTF-8、LF、末行带换行）

    :param entries: 条目 dict 列表（可为 None）
    :return: 清单全文；entries 为空时返回空字符串（调用方据此判定「无可写内容」）
    """
    if not entries:
        return ""
    return "\n".join(dumps(entry) for entry in entries) + "\n"


def entry_has_hash(entry):
    """判断条目是否带内容标识（dt / sha1 / md5 任一非空）

    三者全空的条目等价于「只有路径」——历史格式（纯路径行）经 parse() 解析后就是
    这个样子，用于决定回写时是否保留哈希三要素。

    :param entry: parse() 的条目 dict
    :return: bool
    """
    return bool(_as_field(entry.get(KEY_DT)) or _as_field(entry.get(KEY_SHA1))
                or _as_field(entry.get(KEY_MD5)))


def _excerpt(raw):
    """截断行内容用于告警回显

    :param raw: 原始行文本
    :return: 未超长则原样返回，超长则截断并加省略号
    """
    return raw if len(raw) <= EXCERPT_LIMIT else raw[:EXCERPT_LIMIT] + "…"


def parse(text):
    """解析清单全文为条目列表

    :param text: 清单全文（可含 BOM；None 按空处理）
    :return: (entries, problems)
             entries  = list[dict]，四键齐全（rel_path 恒为非空字符串，
                        其余三键无值时一律为 None），顺序与文件行序一致；
             problems = list[dict]，每项 {"line": 行号（从 1 计）、"reason": 原因、
                        "excerpt": 行内容摘要}；空行与纯空白行不计入
    """
    text = (text or "").lstrip("﻿")   # 去掉可能的 BOM
    entries, problems = [], []
    for lineno, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError as e:
            problems.append({"line": lineno,
                             "reason": "不是合法 JSON：%s" % e,
                             "excerpt": _excerpt(raw)})
            continue
        if not isinstance(obj, dict):
            problems.append({"line": lineno,
                             "reason": "顶层不是 JSON 对象（%s）" % type(obj).__name__,
                             "excerpt": _excerpt(raw)})
            continue
        rel_path = normalize_rel_path(obj.get(KEY_REL_PATH))
        if not rel_path:
            problems.append({"line": lineno,
                             "reason": "缺少 %s（不得为空）" % KEY_REL_PATH,
                             "excerpt": _excerpt(raw)})
            continue
        record = {
            KEY_REL_PATH: rel_path,
            KEY_DT: _as_field(obj.get(KEY_DT)) or None,
            KEY_SHA1: _as_field(obj.get(KEY_SHA1)) or None,
            KEY_MD5: _as_field(obj.get(KEY_MD5)) or None,
        }
        # 未知键原样带出，使上游新增字段能穿过本环节（见模块文档第三节）
        for key in obj:
            if key not in FIELD_ORDER:
                record[key] = obj[key]
        entries.append(record)
    return entries, problems
