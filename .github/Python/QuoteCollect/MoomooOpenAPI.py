# -*- coding: utf-8 -*-
"""moomoo OpenAPI client library.

Public contract of this module (everything else is internal):

    createMoomooOpenAPIClient()   factory; credentials come from environment:
                                  MOOMOO_OPENAPI_AK + MOOMOO_OPENAPI_SK
                                  (or MOOMOO_OPENAPI_SK_FILE)
    MoomooOpenAPIClient           client facade: getHistoryKline / fetchHistoryKline /
                                  fetchHistoryKlineFullDay / getServerTime /
                                  fetchServerDrift / getTradingDays / fetchTradingDays /
                                  postStockBasicInfo / fetchStockBasicInfo
    MoomooOpenAPIException        raised for credentials, network, parse and business errors
    KlineBar                      one history-kline record (returned by the client)
    convertKlineItems()           raw kline_list -> list[KlineBar]
    KTYPE_MIN / KTYPE_DAY / EXTENDED_TIME_ALL    avoid hard-coded literals

Everything else -- pure-stdlib crypto (Ed25519 / RSA-SHA256 / DER), signature assembly,
HTTP transport, endpoint constants, error tables and internal helpers -- is implementation
detail. Names starting with an underscore are internal; they are also deliberately left out
of __all__ so that `from MoomooOpenAPI import *` exposes the contract only.

Credentials are read from the environment and never hard-coded here.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    # factory + client facade
    "createMoomooOpenAPIClient",
    "MoomooOpenAPIClient",
    # error type callers are expected to catch
    "MoomooOpenAPIException",
    # data handed back to callers
    "KlineBar",
    "convertKlineItems",
    # constants that spare callers from hard-coded literals
    "KTYPE_MIN",
    "KTYPE_DAY",
    "KTYPE_WEEK",
    "EXTENDED_TIME_RTH",
    "EXTENDED_TIME_ETH",
    "EXTENDED_TIME_ALL",
]


# ===== BEGIN INLINED FROM ExportArchiveMvsvInline.py =====
# 来源：原型脚本 ExportArchiveMvsvInline.py（**已退役**，不再随仓库提供）。
# 本区段是其必需子集的**逐字搬运**：常量 + 客户端库（Ed25519 / RSA-SHA256 签名、传输层、
# 历史 K 线接口）；仅挖去原型的 `main()` 与 `if __name__` 入口（由本文件的入口替代）。
# 内联来源模块清单见本文件开头的模块 docstring。
#
# ⚠️ 本区段已**不再包含 MVSV 生成逻辑**：格式定义、数据模型（KlineMinBar / KLineDayBar）、
#   文件名生成与写文件已提取到同目录公共模块 `MvsvWriter.py`（见本文件上方 import）。
#   本区段保留的是采集侧的取数与回调逻辑（历史 K 线三接口、传输层、模型转换）。
#
# ⚠️ 维护方式：来源文件已退役，**不再有"重新生成"的途径**。
#   故：A) 本区段**可以**直接编辑，改动即最终生效；B) 请勿期待与任何外部来源保持同步 ——
#   本内联段是这些实现的**唯一副本**（这同时意味着不再有双副本漂移问题）。
# -*- coding: utf-8 -*-
"""归档调度驱动的历史 K 线导出示例（MVSV 输出，**完全自包含单文件版**）。

【自包含范围】本文件**不含任何外部依赖**：除 Python 标准库外不需要任何文件或包——
不仅两个数据处理模型，连本仓库的 ``MetaIncubator.APIHub.SecurityQuote.MoomooOpenAPI``
客户端库（含 Ed25519 / RSA-SHA256 签名与纯标准库密码学实现）都已内联进来。
因此可以直接拷贝本文件到任意目录、云函数或容器中运行，无需 ``src/`` 目录，也无需安装
任何依赖。

【内联来源】（按依赖顺序原样搬运，未改写逻辑）

| 来源模块 | 内容 |
| --- | --- |
| ``Vendor/Futu/PureCrypto.py`` | Ed25519 / RSA-SHA256 与最小 DER 编解码 |
| ``Vendor/Futu/MoomooOpenAPISignature.py`` | 传统 API Key 签名（原文构造、请求头装配） |
| ``MoomooOpenAPI/Const.py`` | BaseURL、端点、ktype、时段边界等常量 |
| ``MoomooOpenAPI/Exception.py`` | ``MoomooOpenAPIException`` |
| ``MoomooOpenAPI/Model.py`` | ``KlineBar`` / ``TradingDay`` / ``StockBasicInfo`` |
| ``MoomooOpenAPI/Validator.py`` | 日期参数校验 |
| ``MoomooOpenAPI/Convert.py`` | 响应转换、时段归类、去重与统计 |
| ``MoomooOpenAPI/HTTPTransport.py`` | 请求发送、签名装配、错误归类与重试 |
| ``MoomooOpenAPI/QuoteHistoryKline.py`` | ``getHistoryKline`` / ``fetchHistoryKline`` / ``fetchHistoryKlineFullDay`` |
| ``MoomooOpenAPI/QuoteServerTime.py`` | ``getServerTime`` / ``fetchServerDrift`` |
| ``MoomooOpenAPI/QuoteStockBasicInfo.py`` | ``postStockBasicInfo`` / ``fetchStockBasicInfo`` |
| ``MoomooOpenAPI/QuoteTradingDays.py`` | ``getTradingDays`` / ``fetchTradingDays`` |
| ``MoomooOpenAPI/ClientFacade.py`` | ``MoomooOpenAPIClient`` / ``createMoomooOpenAPIClient`` |
| ``KlineMinBar.py`` / ``KLineDayBar.py`` | 两个数据处理模型（MVSV 数据列标准） |

【维护提示】上表列出的是这些实现的**原始出处模块**，仅供溯源——那些源文件与承载它们的
原型脚本均已退役，本内联段是**当前唯一副本**。直接在本文件内修改即可生效，
不存在需要同步的"另一份"。

【部署提示】默认输出目录由常量 ``OUTPUT_DIR`` 决定，取值
``<本文件所在目录>/../../../ZZFS/Finv/Quote``（与原仓库布局一致）；
作业运行时会被 ``LOCAL_OUTPUT_DIR``（系统临时目录）覆盖，故该默认值仅供独立运行本文件时参考；
把本文件拷贝到其他位置运行，也请按需修改该常量。
"""

import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


# ===========================================================================
# 内联：Vendor/Futu/PureCrypto.py（Ed25519 / RSA-SHA256 纯标准库实现）
# ===========================================================================

# -*- coding: utf-8 -*-




class PureCryptoError(Exception):
    """纯标准库密码学模块的异常。

    覆盖：私钥结构非法、密钥长度不足、消息过长等无法完成签名的情形。
    """


# ---------------------------------------------------------------------------
# Ed25519（RFC 8032 §5.1）
# ---------------------------------------------------------------------------

#: 域素数 2^255 - 19
_ED_P = 2 ** 255 - 19

#: 子群阶 2^252 + 27742317777372353535851937790883648493
_ED_Q = 2 ** 252 + 27742317777372353535851937790883648493

#: 曲线常数 d = -121665 / 121666
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P

#: 模 p 的平方根因子 2^((p-1)/4)
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _edRecoverX(y: int) -> int:
    """由 y 坐标恢复 Ed25519 曲线上的 x 坐标（取偶数值分支）。

    Args:
        y: y 坐标（模 p）。

    Returns:
        x 坐标（模 p）。
    """
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if x % 2 != 0:
        x = _ED_P - x
    return x


#: 基点 B（y = 4/5，x 取偶数值分支）
_ED_BY = (4 * pow(5, _ED_P - 2, _ED_P)) % _ED_P
_ED_B = (_edRecoverX(_ED_BY), _ED_BY)


def _edAdd(pointA: Tuple[int, int], pointB: Tuple[int, int]) -> Tuple[int, int]:
    """Edwards 曲线点加（仿射坐标）。

    Args:
        pointA: 点 A 的 ``(x, y)``。
        pointB: 点 B 的 ``(x, y)``。

    Returns:
        相加后的 ``(x, y)``。
    """
    x1, y1 = pointA
    x2, y2 = pointB
    k = (_ED_D * x1 * x2 * y1 * y2) % _ED_P
    x3 = (x1 * y2 + x2 * y1) * pow(1 + k, _ED_P - 2, _ED_P) % _ED_P
    y3 = (y1 * y2 + x1 * x2) * pow(1 - k, _ED_P - 2, _ED_P) % _ED_P
    return x3, y3


def _edScalarMult(point: Tuple[int, int], scalar: int) -> Tuple[int, int]:
    """标量乘（迭代式 double-and-add，避免递归深度限制）。

    Args:
        point: 基点或任意曲线点 ``(x, y)``。
        scalar: 标量。

    Returns:
        ``scalar * point``。
    """
    result: Tuple[int, int] = (0, 1)          # 单位元
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _edAdd(result, addend)
        addend = _edAdd(addend, addend)
        scalar >>= 1
    return result


def _edEncodePoint(point: Tuple[int, int]) -> bytes:
    """把曲线点压缩为 32 字节（y 坐标 + x 的最低位放在最高位）。

    Args:
        point: 曲线点 ``(x, y)``。

    Returns:
        32 字节压缩表示。
    """
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _edSecretExpand(seed: bytes) -> Tuple[int, bytes]:
    """按 RFC 8032 展开 32 字节种子为标量与前缀。

    Args:
        seed: 32 字节私钥种子。

    Returns:
        ``(标量 a, 前缀 prefix)``——前者用于标量乘，后者参与 nonce 计算。
    """
    if len(seed) != 32:
        raise PureCryptoError(f"Ed25519 种子必须为 32 字节，实际 {len(seed)} 字节")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8                  # 清最低 3 位
    scalar |= 1 << 254                        # 置次高位
    return scalar, digest[32:]


def ed25519PublicKeyFromSeed(seed: bytes) -> bytes:
    """由 Ed25519 种子派生 32 字节公钥。

    Args:
        seed: 32 字节私钥种子（PKCS#8 中的 OCTET STRING 内容）。

    Returns:
        32 字节公钥。

    Raises:
        PureCryptoError: 种子长度不是 32 字节。
    """
    scalar, _ = _edSecretExpand(seed)
    return _edEncodePoint(_edScalarMult(_ED_B, scalar))


def ed25519SeedFromPkcs8(derBytes: bytes) -> bytes:
    """从 PKCS#8 DER 中提取 Ed25519 的 32 字节私钥种子。

    Ed25519 的 PKCS#8 结构固定：``SEQUENCE`` + ``version`` + ``AlgorithmIdentifier``
    （OID 1.3.101.112）+ ``OCTET STRING``（内层再套一层 ``OCTET STRING`` 装种子），
    故直接按 TLV 解析后取内层内容，不依赖任何前缀假设。

    Args:
        derBytes: PKCS#8 DER 字节。

    Returns:
        32 字节种子。

    Raises:
        PureCryptoError: 结构非法或种子长度不是 32 字节。
    """
    tag, pkcs8, _ = _derReadTlv(derBytes, 0)
    if tag != 0x30:
        raise PureCryptoError(f"PKCS#8 首元素应为 SEQUENCE，实际标签 0x{tag:02x}")
    _, _, offset = _derReadTlv(pkcs8, 0)                    # version
    _, _, offset = _derReadTlv(pkcs8, offset)               # privateKeyAlgorithm
    tag, inner, _ = _derReadTlv(pkcs8, offset)              # privateKey (OCTET STRING)
    if tag != 0x04:
        raise PureCryptoError(f"PKCS#8 的 privateKey 应为 OCTET STRING，实际标签 0x{tag:02x}")
    tag, seed, _ = _derReadTlv(inner, 0)                    # 内层 OCTET STRING
    if tag != 0x04:
        raise PureCryptoError(f"Ed25519 私钥应为 OCTET STRING，实际标签 0x{tag:02x}")
    if len(seed) != 32:
        raise PureCryptoError(f"Ed25519 种子必须为 32 字节，私钥中为 {len(seed)} 字节")
    return seed


def ed25519Sign(seed: bytes, message: bytes) -> bytes:
    """按 RFC 8032 对消息做 Ed25519 签名（确定性、无需随机数）。

    Args:
        seed: 32 字节私钥种子。
        message: 待签名消息。

    Returns:
        64 字节签名（``R || S``）。

    Raises:
        PureCryptoError: 种子长度不是 32 字节。
    """
    scalar, prefix = _edSecretExpand(seed)
    publicKey = _edEncodePoint(_edScalarMult(_ED_B, scalar))
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _ED_Q
    encodedR = _edEncodePoint(_edScalarMult(_ED_B, nonce))
    challenge = int.from_bytes(
        hashlib.sha512(encodedR + publicKey + message).digest(), "little"
    ) % _ED_Q
    s = (nonce + challenge * scalar) % _ED_Q
    return encodedR + s.to_bytes(32, "little")


# ---------------------------------------------------------------------------
# 最小 DER 编解码（只覆盖签名所需的结构）
# ---------------------------------------------------------------------------

def _derReadTlv(data: bytes, offset: int) -> Tuple[int, bytes, int]:
    """读取一个 DER 的 TLV 结构。

    Args:
        data: DER 字节。
        offset: 起始偏移。

    Returns:
        ``(tag, value, nextOffset)``。

    Raises:
        PureCryptoError: 长度越界或长度字段非法时抛出。
    """
    if offset + 2 > len(data):
        raise PureCryptoError(f"DER 结构在偏移 {offset} 处被截断（总长 {len(data)}）")
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + count > len(data):
            raise PureCryptoError(f"DER 长度字段非法：偏移 {offset - 2} 处声明 {count} 字节长度")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    if offset + length > len(data):
        raise PureCryptoError(
            f"DER 内容越界：偏移 {offset} 处声明 {length} 字节，剩余 {len(data) - offset} 字节"
        )
    return tag, data[offset:offset + length], offset + length


def _derEncodeLength(length: int) -> bytes:
    """编码 DER 长度字段（短形式与长形式）。

    Args:
        length: 内容长度。

    Returns:
        长度字段字节。
    """
    if length < 0x80:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _derEncodeTlv(tag: int, value: bytes) -> bytes:
    """编码一个 DER 的 TLV 结构。

    Args:
        tag: 标签字节。
        value: 内容字节。

    Returns:
        TLV 字节。
    """
    return bytes([tag]) + _derEncodeLength(len(value)) + value


def _derEncodeInteger(value: int) -> bytes:
    """把非负整数编码为 DER INTEGER（必要时补前导零）。

    Args:
        value: 非负整数。

    Returns:
        DER INTEGER 字节。
    """
    encoded = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if encoded[0] & 0x80:
        encoded = b"\x00" + encoded
    return _derEncodeTlv(0x02, encoded)


def _derDecodeInteger(value: bytes, label: str) -> int:
    """把 DER INTEGER 的内容解码为非负整数。

    Args:
        value: INTEGER 的内容字节。
        label: 字段名（用于错误消息定位）。

    Returns:
        整数值。

    Raises:
        PureCryptoError: 内容为空时抛出。
    """
    if not value:
        raise PureCryptoError(f"DER 中的 {label} 为空")
    return int.from_bytes(value, "big")


# ---------------------------------------------------------------------------
# RSA-SHA256（PKCS#1 v1.5）
# ---------------------------------------------------------------------------

#: SHA-256 的 DigestInfo DER 前缀（PKCS#1 v1.5 固定值）
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

#: rsaEncryption 的 AlgorithmIdentifier（OID 1.2.840.113549.1.1.1 + NULL）
_RSA_ALGORITHM_IDENTIFIER = bytes.fromhex("300d06092a864886f70d0101010500")


def _rsaParsePkcs8(derBytes: bytes) -> Tuple[int, int, int]:
    """从 PKCS#8 DER 中提取 RSA 私钥的 ``(n, e, d)``。

    结构：``PrivateKeyInfo`` → ``privateKey``（OCTET STRING 包裹的 ``RSAPrivateKey``）。

    Args:
        derBytes: PKCS#8 DER 字节。

    Returns:
        ``(模数 n, 公钥指数 e, 私钥指数 d)``。

    Raises:
        PureCryptoError: 结构非法或缺少必要字段时抛出。
    """
    tag, pkcs8, _ = _derReadTlv(derBytes, 0)
    if tag != 0x30:
        raise PureCryptoError(f"PKCS#8 首元素应为 SEQUENCE，实际标签 0x{tag:02x}")
    _, _, offset = _derReadTlv(pkcs8, 0)                    # version
    _, _, offset = _derReadTlv(pkcs8, offset)               # privateKeyAlgorithm
    tag, inner, _ = _derReadTlv(pkcs8, offset)              # privateKey (OCTET STRING)
    if tag != 0x04:
        raise PureCryptoError(f"PKCS#8 的 privateKey 应为 OCTET STRING，实际标签 0x{tag:02x}")

    tag, rsaKey, _ = _derReadTlv(inner, 0)
    if tag != 0x30:
        raise PureCryptoError(f"RSAPrivateKey 应为 SEQUENCE，实际标签 0x{tag:02x}")
    _, _, cursor = _derReadTlv(rsaKey, 0)                   # version
    _, modulusBytes, cursor = _derReadTlv(rsaKey, cursor)   # n
    _, exponentBytes, cursor = _derReadTlv(rsaKey, cursor)  # e
    _, privateBytes, cursor = _derReadTlv(rsaKey, cursor)   # d
    return (
        _derDecodeInteger(modulusBytes, "RSA 模数 n"),
        _derDecodeInteger(exponentBytes, "RSA 公钥指数 e"),
        _derDecodeInteger(privateBytes, "RSA 私钥指数 d"),
    )


def rsaSignPkcs1v15Sha256(derBytes: bytes, message: bytes) -> bytes:
    """按 PKCS#1 v1.5（EMSA-PKCS1-v1_5）做 RSA-SHA256 签名。

    Args:
        derBytes: PKCS#8 DER 私钥字节。
        message: 待签名消息。

    Returns:
        签名字节（长度等于密钥模长）。

    Raises:
        PureCryptoError: 私钥结构非法，或密钥长度不足以容纳 DigestInfo 时抛出。
    """
    modulus, _, privateExponent = _rsaParsePkcs8(derBytes)
    keyLength = (modulus.bit_length() + 7) // 8
    digestInfo = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    paddingLength = keyLength - len(digestInfo) - 3
    if paddingLength < 8:
        raise PureCryptoError(
            f"RSA 密钥过短：模长 {keyLength} 字节，无法容纳 SHA-256 DigestInfo"
            f"（至少需要 {len(digestInfo) + 11} 字节）"
        )
    encoded = b"\x00\x01" + b"\xff" * paddingLength + b"\x00" + digestInfo
    signature = pow(int.from_bytes(encoded, "big"), privateExponent, modulus)
    return signature.to_bytes(keyLength, "big")


def rsaPublicKeySpki(derBytes: bytes) -> bytes:
    """由 PKCS#8 私钥派生 RSA 公钥的 SPKI DER。

    Args:
        derBytes: PKCS#8 DER 私钥字节。

    Returns:
        SubjectPublicKeyInfo 的 DER 字节。

    Raises:
        PureCryptoError: 私钥结构非法时抛出。
    """
    modulus, publicExponent, _ = _rsaParsePkcs8(derBytes)
    rsaPublicKey = _derEncodeTlv(
        0x30, _derEncodeInteger(modulus) + _derEncodeInteger(publicExponent)
    )
    return _derEncodeTlv(
        0x30, _RSA_ALGORITHM_IDENTIFIER + _derEncodeTlv(0x03, b"\x00" + rsaPublicKey)
    )


# ===========================================================================
# 内联：Vendor/Futu/MoomooOpenAPISignature.py（传统 API Key 签名）
# ===========================================================================

# -*- coding: utf-8 -*-



# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 支持的签名算法标识
ALGORITHM_ED25519 = "Ed25519"
ALGORITHM_RSA_SHA256 = "RSA-SHA256"

#: 支持的签名算法集合
SUPPORTED_ALGORITHMS = (ALGORITHM_ED25519, ALGORITHM_RSA_SHA256)

#: Ed25519 PKCS#8 DER 前缀：
#: SEQUENCE(46) + version(0) + AlgorithmIdentifier(OID 1.3.101.112) + OCTET STRING(32 字节种子)
ED25519_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

#: Ed25519 SPKI DER 前缀（公钥派生用）：
#: SEQUENCE(42) + AlgorithmIdentifier(OID 1.3.101.112) + BIT STRING(32 字节公钥)
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")

#: Ed25519 算法 OID（1.3.101.112）的 DER 编码
ED25519_OID_DER = bytes.fromhex("06032b6570")

#: RSA 算法 OID（1.2.840.113549.1.1.1）的 DER 编码
RSA_OID_DER = bytes.fromhex("06092a864886f70d010101")

#: X-Nonce 允许字符集（官方：仅字母、数字、下划线、连字符）
NONCE_ALPHABET = string.ascii_letters + string.digits + "_-"

#: 默认 X-Nonce 长度
DEFAULT_NONCE_LENGTH = 32

#: 官方服务端时间戳偏移阈值（毫秒）
TIMESTAMP_DRIFT_LIMIT_MS = 5000

#: 签名原文的段数（时间戳 / 方法 / 路径 / 查询串 / 请求体摘要）
SIGNATURE_SOURCE_SEGMENTS = 5


class FutuOpenApiSignatureError(Exception):
    """moomoo OpenAPI 签名异常。

    覆盖：私钥格式非法、算法无法识别、缺少 AppKey ID、签名失败。

    Attributes:
        algorithm: 涉及的签名算法（未知时为 None）。
    """

    def __init__(self, message: str, algorithm: Optional[str] = None) -> None:
        """初始化异常。

        Args:
            message: 中文错误描述（含可定位信息）。
            algorithm: 签名算法标识（可选）。

        Returns:
            无。
        """
        super().__init__(message)
        self.algorithm = algorithm


class FutuOpenApiSignature:
    """moomoo OpenAPI「传统 API Key」签名生成器。

    支持 Ed25519 与 RSA-SHA256 两种算法，私钥可用 Base64 编码的 PKCS#8 DER
    或 PEM 文本；算法由私钥内容自动识别，无需手工指定。

    Attributes:
        appKeyId: AppKey ID（请求头 ``X-Api-Key``）。
        algorithm: 识别出的签名算法（``Ed25519`` 或 ``RSA-SHA256``）。
        privateKeyDer: PKCS#8 DER 私钥字节。
    """

    def __init__(
        self,
        privateKeyText: str,
        appKeyId: str = "",
        algorithm: Optional[str] = None,
    ) -> None:
        """初始化签名器。

        Args:
            privateKeyText: 私钥原文（Base64 PKCS#8 DER 或 PEM）。
            appKeyId: AppKey ID（请求头 X-Api-Key）；也可后续用 setAppKeyId 设置。
            algorithm: 可选，强制指定算法；为 None 时由私钥自动识别。

        Returns:
            无。

        Raises:
            FutuOpenApiSignatureError: 私钥为空、格式非法或算法无法识别时抛出。
        """
        self.appKeyId = appKeyId
        self.privateKeyDer = self.normalizePrivateKey(privateKeyText)
        detected = algorithm or self.detectAlgorithm(self.privateKeyDer)
        if detected not in SUPPORTED_ALGORITHMS:
            raise FutuOpenApiSignatureError(
                f"不支持的签名算法：{detected}；仅支持 {list(SUPPORTED_ALGORITHMS)}",
                algorithm=detected,
            )
        self.algorithm = detected

    # ------------------------------------------------------------------
    # 私钥解析与算法识别（静态工具）
    # ------------------------------------------------------------------

    @staticmethod
    def normalizePrivateKey(raw: str) -> bytes:
        """把私钥文本规范化为 PKCS#8 DER 字节。

        支持两种输入：

        - **Base64 编码的 PKCS#8 DER**（形如 ``MC4CAQAwBQYDK2VwBCIEI...``，
          即环境变量 ``MOOMOO_OPENAPI_SK`` 的存储形态）
        - **PEM 文本**（``-----BEGIN PRIVATE KEY-----`` 包裹）

        Args:
            raw: 私钥原文。

        Returns:
            PKCS#8 DER 字节。

        Raises:
            FutuOpenApiSignatureError: 输入为空或无法解析时抛出。
        """
        text = (raw or "").strip()
        if not text:
            raise FutuOpenApiSignatureError(
                "私钥为空：请通过参数或环境变量 MOOMOO_OPENAPI_SK 传入 PKCS#8 私钥"
            )
        if text.startswith("-----BEGIN"):
            stripped = re.sub(r"-----(BEGIN|END)[^-]+-----", "", text)
            try:
                return base64.b64decode("".join(stripped.split()), validate=True)
            except Exception as exc:  # noqa: BLE001 - 统一归一为签名异常
                raise FutuOpenApiSignatureError(f"PEM 私钥解析失败：{exc}") from exc
        compact = "".join(text.split())
        try:
            return base64.b64decode(compact, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise FutuOpenApiSignatureError(
                "私钥既不是合法 PEM，也不是合法 Base64 字符串；"
                f"原文长度 {len(text)}，前 12 字符 {text[:12]!r}"
            ) from exc

    @staticmethod
    def detectAlgorithm(derBytes: bytes) -> str:
        """依据 PKCS#8 DER 内容识别签名算法。

        Args:
            derBytes: PKCS#8 DER 字节。

        Returns:
            ``Ed25519`` 或 ``RSA-SHA256``。

        Raises:
            FutuOpenApiSignatureError: 无法识别的算法标识时抛出。
        """
        if derBytes.startswith(ED25519_PKCS8_PREFIX) or ED25519_OID_DER in derBytes[:32]:
            return ALGORITHM_ED25519
        if RSA_OID_DER in derBytes[:32]:
            return ALGORITHM_RSA_SHA256
        raise FutuOpenApiSignatureError(
            "无法识别的私钥算法：仅支持 Ed25519（OID 1.3.101.112）"
            "与 RSA（OID 1.2.840.113549.1.1.1）"
        )

    def setAppKeyId(self, appKeyId: str) -> "FutuOpenApiSignature":
        """设置 AppKey ID（请求头 X-Api-Key）。

        Args:
            appKeyId: AppKey ID。

        Returns:
            自身（便于链式调用）。
        """
        self.appKeyId = appKeyId
        return self

    # ------------------------------------------------------------------
    # 签名原文与随机串
    # ------------------------------------------------------------------

    @staticmethod
    def buildSignatureSource(
        timestampMs: int,
        httpMethod: str,
        requestPath: str,
        queryString: str = "",
        body: Optional[bytes] = None,
    ) -> str:
        """按官方规则构造签名原文（5 段，以 ``\\n`` 连接）。

        Args:
            timestampMs: 毫秒时间戳，须与请求头 ``X-Timestamp`` 一致。
            httpMethod: HTTP 方法，大小写不敏感（内部转大写）。
            requestPath: 路径部分，须以 ``/`` 开头，
                如 ``/api/v1.0/quote/US.FUTU/history-kline``。
            queryString: 原始查询串（不含开头 ``?``）；无参数传空字符串。
            body: 请求体原始字节；无请求体传 None（摘要为空串）。

        Returns:
            签名原文（含 4 个 ``\\n``）。

        Raises:
            FutuOpenApiSignatureError: 时间戳非法、方法为空或路径不以 / 开头时抛出。
        """
        if not isinstance(timestampMs, int) or isinstance(timestampMs, bool) or timestampMs <= 0:
            raise FutuOpenApiSignatureError(
                f"timestampMs 必须是正整数毫秒时间戳，当前为 {timestampMs!r}"
            )
        method = (httpMethod or "").strip().upper()
        if not method:
            raise FutuOpenApiSignatureError("httpMethod 不能为空")
        if not requestPath or not requestPath.startswith("/"):
            raise FutuOpenApiSignatureError(
                f"requestPath 必须是 / 开头的路径，当前为 {requestPath!r}"
            )
        query = queryString or ""
        if query.startswith("?"):
            raise FutuOpenApiSignatureError("queryString 不应包含开头的 '?'")
        bodyPart = hashlib.sha256(body).hexdigest() if body else ""
        return f"{timestampMs}\n{method}\n{requestPath}\n{query}\n{bodyPart}"

    @staticmethod
    def generateNonce(length: int = DEFAULT_NONCE_LENGTH) -> str:
        """生成符合官方字符集要求的 ``X-Nonce`` 随机串。

        Args:
            length: 长度，须在 1-64 之间。

        Returns:
            随机字符串。

        Raises:
            FutuOpenApiSignatureError: 长度越界时抛出。
        """
        if not 1 <= length <= 64:
            raise FutuOpenApiSignatureError(f"X-Nonce 长度须在 1-64 之间，当前为 {length}")
        return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))

    @staticmethod
    def bodyDigest(body: Optional[bytes]) -> str:
        """计算请求体摘要（SHA256 小写十六进制；无请求体返回空串）。

        Args:
            body: 请求体原始字节。

        Returns:
            64 位小写十六进制摘要或空串。

        Raises:
            无。
        """
        if not body:
            return ""
        return hashlib.sha256(body).hexdigest()

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------

    def signSource(self, source: str) -> str:
        """对签名原文签名并 Base64 编码。

        Args:
            source: ``buildSignatureSource`` 产出的原文。

        Returns:
            Base64 编码的签名串（Authorization 请求头的值）。

        Raises:
            FutuOpenApiSignatureError: 依赖缺失或签名失败时抛出。
        """
        data = source.encode("utf-8")
        if self.algorithm == ALGORITHM_ED25519:
            signature = self._signEd25519(data)
        else:
            signature = self._signRsa(data)
        return base64.b64encode(signature).decode("ascii")

    def signRequest(
        self,
        httpMethod: str,
        requestPath: str,
        queryString: str = "",
        body: Optional[bytes] = None,
        timestampMs: Optional[int] = None,
    ) -> Tuple[str, str, int]:
        """按请求要素一次性完成签名。

        Args:
            httpMethod: HTTP 方法。
            requestPath: 路径部分。
            queryString: 原始查询串（不含 ``?``）。
            body: 请求体原始字节。
            timestampMs: 毫秒时间戳；为 None 时取当前时间。

        Returns:
            ``(signatureBase64, signatureSource, timestampMs)``；
            返回原文便于排查验签失败原因。

        Raises:
            FutuOpenApiSignatureError: 参数非法或签名失败时抛出。
        """
        ts = int(timestampMs if timestampMs is not None else time.time() * 1000)
        source = self.buildSignatureSource(ts, httpMethod, requestPath, queryString, body)
        return self.signSource(source), source, ts

    def buildAuthHeaders(
        self,
        httpMethod: str,
        requestPath: str,
        queryString: str = "",
        body: Optional[bytes] = None,
        timestampMs: Optional[int] = None,
        nonce: Optional[str] = None,
    ) -> Tuple[Dict[str, str], str]:
        """构造 API Key 认证所需的全部请求头。

        产出四个官方要求的请求头：

        - ``X-Api-Key``：AppKey ID
        - ``Authorization``：Base64 签名（**不加** ``Bearer `` 前缀）
        - ``X-Timestamp``：毫秒时间戳（与签名原文一致）
        - ``X-Nonce``：随机串

        Args:
            httpMethod: HTTP 方法。
            requestPath: 路径部分。
            queryString: 原始查询串（不含 ``?``）。
            body: 请求体原始字节。
            timestampMs: 毫秒时间戳；为 None 时取当前时间。
            nonce: 随机串；为 None 时自动生成。

        Returns:
            ``(headers, signatureSource)``。

        Raises:
            FutuOpenApiSignatureError: 缺少 AppKey ID、参数非法或签名失败时抛出。
        """
        if not self.appKeyId:
            raise FutuOpenApiSignatureError(
                "缺少 AppKey ID（请求头 X-Api-Key）。请在 "
                "https://open.moomoo.com/dashboard 「用户中心」查看 AppKey ID，"
                "并通过构造参数 appKeyId 或环境变量 MOOMOO_OPENAPI_AK 传入。"
            )
        signature, source, ts = self.signRequest(
            httpMethod, requestPath, queryString, body, timestampMs
        )
        headers = {
            "X-Api-Key": self.appKeyId,
            "Authorization": signature,
            "X-Timestamp": str(ts),
            "X-Nonce": nonce or self.generateNonce(),
        }
        return headers, source

    # ------------------------------------------------------------------
    # 公钥派生（用于与 dashboard 比对）
    # ------------------------------------------------------------------

    def derivePublicKeyBase64(self) -> str:
        """派生公钥的 SPKI DER 的 Base64 编码。

        dashboard 创建 AppKey 时需上传公钥，此方法产出的字符串可直接逐字符比对，
        用于快速定位「验签失败是否因为公钥不匹配」。两种算法都由纯标准库实现：
        Ed25519 用固定 SPKI 前缀拼接 32 字节公钥，RSA 现场编码 SPKI。

        Returns:
            Base64 编码的 SPKI 公钥。

        Raises:
            FutuOpenApiSignatureError: 私钥结构非法或派生失败时抛出。
        """
        try:
            if self.algorithm == ALGORITHM_ED25519:
                seed = ed25519SeedFromPkcs8(self.privateKeyDer)
                der = ED25519_SPKI_PREFIX + ed25519PublicKeyFromSeed(seed)
            else:
                der = rsaPublicKeySpki(self.privateKeyDer)
        except PureCryptoError as exc:
            raise FutuOpenApiSignatureError(
                f"公钥派生失败：{exc}", algorithm=self.algorithm
            ) from exc
        return base64.b64encode(der).decode("ascii")

    # ------------------------------------------------------------------
    # 调试
    # ------------------------------------------------------------------

    def generateVerbose(
        self,
        httpMethod: str,
        requestPath: str,
        queryString: str = "",
        body: Optional[bytes] = None,
        timestampMs: Optional[int] = None,
        nonce: Optional[str] = None,
        expectedSignature: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成签名并返回全部中间结果，便于排查验签失败。

        Args:
            httpMethod: HTTP 方法。
            requestPath: 路径部分。
            queryString: 原始查询串。
            body: 请求体原始字节。
            timestampMs: 毫秒时间戳；为 None 时取当前时间。
            nonce: 随机串；为 None 时自动生成。
            expectedSignature: 可选，期望签名（用于比对并标出是否一致）。

        Returns:
            字典，含 ``algorithm`` / ``signatureSource`` / ``signature`` /
            ``headers`` / ``bodySha256`` / ``matchesExpected`` 等字段。

        Raises:
            FutuOpenApiSignatureError: 参数非法或签名失败时抛出。
        """
        headers, source = self.buildAuthHeaders(
            httpMethod, requestPath, queryString, body, timestampMs, nonce
        )
        detail: Dict[str, Any] = {
            "algorithm": self.algorithm,
            "appKeyId": self.appKeyId,
            "signatureSource": source,
            "signature": headers["Authorization"],
            "headers": headers,
            "bodySha256": self.bodyDigest(body),
            "sourceSegments": source.split("\n"),
        }
        if expectedSignature is not None:
            detail["expectedSignature"] = expectedSignature
            detail["matchesExpected"] = headers["Authorization"] == expectedSignature
        return detail

    # ------------------------------------------------------------------
    # 内部：具体算法实现
    # ------------------------------------------------------------------

    def _signEd25519(self, data: bytes) -> bytes:
        """Ed25519 直接签名（纯标准库实现，见 ``PureCrypto``）。

        Args:
            data: 待签名字节。

        Returns:
            64 字节签名。

        Raises:
            FutuOpenApiSignatureError: 私钥结构非法或签名失败时抛出。
        """
        try:
            seed = ed25519SeedFromPkcs8(self.privateKeyDer)
            return ed25519Sign(seed, data)
        except PureCryptoError as exc:
            raise FutuOpenApiSignatureError(
                f"Ed25519 签名失败：{exc}", algorithm=ALGORITHM_ED25519
            ) from exc

    def _signRsa(self, data: bytes) -> bytes:
        """RSA-SHA256（PKCS#1 v1.5）签名（纯标准库实现，见 ``PureCrypto``）。

        Args:
            data: 待签名字节。

        Returns:
            签名字节（长度等于密钥模长）。

        Raises:
            FutuOpenApiSignatureError: 私钥结构非法或签名失败时抛出。
        """
        try:
            return rsaSignPkcs1v15Sha256(self.privateKeyDer, data)
        except PureCryptoError as exc:
            raise FutuOpenApiSignatureError(
                f"RSA-SHA256 签名失败：{exc}", algorithm=ALGORITHM_RSA_SHA256
            ) from exc


def createFutuOpenApiSignature(
    privateKeyText: str,
    appKeyId: str = "",
    algorithm: Optional[str] = None,
) -> FutuOpenApiSignature:
    """工厂函数：创建 FutuOpenApiSignature 签名器实例。

    Args:
        privateKeyText: 私钥原文（Base64 PKCS#8 DER 或 PEM）。
        appKeyId: AppKey ID（请求头 X-Api-Key）。
        algorithm: 可选，强制指定算法；为 None 时自动识别。

    Returns:
        FutuOpenApiSignature 实例。

    Raises:
        FutuOpenApiSignatureError: 私钥非法或算法不支持时抛出。
    """
    return FutuOpenApiSignature(
        privateKeyText=privateKeyText, appKeyId=appKeyId, algorithm=algorithm
    )


# ===========================================================================
# 内联：MoomooOpenAPI/Const.py（常量）
# ===========================================================================

# -*- coding: utf-8 -*-


# ---------------------------------------------------------------------------
# 接入点与请求默认值
# ---------------------------------------------------------------------------

#: API Host（官方文档：快速开始 → API Host）
API_BASE_URL = "https://webapi.moomoo.com"

#: API Host 别名（与 FTMoomooComWeb.BASE_URL 同形，便于调用方直接引用；
#: 注意两者取值不同：本子包为 webapi.moomoo.com，ComWeb 为 www.moomoo.com）
BASE_URL = API_BASE_URL

#: 历史 K 线接口路径模板（``{symbol}`` 为标的代码，如 ``US.FUTU``）
PATH_HISTORY_KLINE = "/api/v1.0/quote/{symbol}/history-kline"

#: 交易日历接口路径
PATH_TRADING_DAYS = "/api/v1.0/quote/trading-days"

#: 标的静态档案接口路径
PATH_STOCK_BASICINFO = "/api/v1.0/quote/stock-basicinfo"

#: 服务端时间接口路径
PATH_SERVER_TIME = "/api/v1.0/server-time"

#: 默认请求超时（秒）
DEFAULT_TIMEOUT = 25

#: 标准浏览器 User-Agent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

#: GET 请求默认头
DEFAULT_GET_HEADERS: Dict[str, str] = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

#: POST 请求默认头
DEFAULT_POST_HEADERS: Dict[str, str] = {
    "content-type": "application/json;charset=UTF-8",
    "accept": "application/json, text/plain, */*",
}

# ---------------------------------------------------------------------------
# 凭据环境变量名
# ---------------------------------------------------------------------------

#: AppKey ID 环境变量名
ENV_APP_AK = "MOOMOO_OPENAPI_AK"

#: 私钥原文环境变量名（Base64 PKCS#8 DER 或 PEM）
ENV_APP_SK = "MOOMOO_OPENAPI_SK"

#: 私钥文件路径环境变量名
ENV_APP_SK_FILE = "MOOMOO_OPENAPI_SK_FILE"

# ---------------------------------------------------------------------------
# K 线类型（ktype）—— 依据 2026-09-21 真机实测的中位跨度推定
# ---------------------------------------------------------------------------

KTYPE_MIN = "1"
KTYPE_DAY = "2"
KTYPE_WEEK = "3"
KTYPE_MIN5 = "6"
KTYPE_MIN15 = "7"
KTYPE_MIN30 = "8"

#: ktype 取值 → 中文说明（实测跨度）
KTYPE_LABELS: Dict[str, str] = {
    KTYPE_MIN: "1 分钟（实测跨度 60000 ms）",
    KTYPE_DAY: "日 K（实测跨度 86400000 ms）",
    KTYPE_WEEK: "周 K",
    KTYPE_MIN5: "5 分钟（实测跨度 300000 ms）",
    KTYPE_MIN15: "15 分钟（实测跨度 900000 ms）",
    KTYPE_MIN30: "30 分钟（实测跨度 1800000 ms）",
}

# ---------------------------------------------------------------------------
# 复权类型（autype）
# ---------------------------------------------------------------------------

AUTYPE_NONE = "0"
AUTYPE_FORWARD = "1"
AUTYPE_BACKWARD = "2"

#: autype 取值 → 中文说明
AUTYPE_LABELS: Dict[str, str] = {
    AUTYPE_NONE: "不复权",
    AUTYPE_FORWARD: "前复权",
    AUTYPE_BACKWARD: "后复权",
}

# ---------------------------------------------------------------------------
# 时段开关（extended_time）—— 实测为**包含式**分档
# ---------------------------------------------------------------------------

EXTENDED_TIME_RTH = "0"
EXTENDED_TIME_ETH = "1"
EXTENDED_TIME_ALL = "2"

#: extended_time 取值 → 中文说明（含实测返回条数，样本 US.FUTU 单日）
EXTENDED_TIME_LABELS: Dict[str, str] = {
    EXTENDED_TIME_RTH: "仅盘中（实测 390 条/日，09:31-16:00）",
    EXTENDED_TIME_ETH: "盘中 + 盘前盘后（实测 960 条/日，04:01-20:00）",
    EXTENDED_TIME_ALL: "盘中 + 盘前盘后 + 夜盘（实测 871 条/日，00:00-24:00）",
}

#: 三档时段开关（供单日全时段并集请求按序使用）
EXTENDED_TIME_TIERS = (EXTENDED_TIME_RTH, EXTENDED_TIME_ETH, EXTENDED_TIME_ALL)

# ---------------------------------------------------------------------------
# 服务端限制
# ---------------------------------------------------------------------------

#: 接口单次返回硬上限（实测：不传 num 时为 1000 条）
SERVER_PAGE_LIMIT = 1000

#: 显式传 num 时的上限（实测 > 370 返回 -3）
MAX_NUM_PER_REQUEST = 370

#: 服务端时间戳偏移告警阈值（毫秒，官方默认 5 秒）
TIMESTAMP_DRIFT_LIMIT_MS = 5000

#: 标的静态档案单次最多可查询的代码数
MAX_BASICINFO_CODES = 400

# ---------------------------------------------------------------------------
# 交易时段（按标的**本地时间**划分，用于 K 线记录的 session 归类）
# ---------------------------------------------------------------------------

#: 盘中时段（美股常规交易时段，本地时间）
SESSION_RTH = "RTH"

#: 盘前盘后时段（美股延长时段，本地时间）
SESSION_ETH = "ETH"

#: 夜盘时段（美股夜盘，本地时间）
SESSION_OVERNIGHT = "OVERNIGHT"

#: 时段标识 → 中文说明
SESSION_LABELS: Dict[str, str] = {
    SESSION_RTH: "盘中",
    SESSION_ETH: "盘前盘后",
    SESSION_OVERNIGHT: "夜盘",
}

#: 盘中开始时刻（含），本地时间 09:30
RTH_OPEN_MINUTE = 9 * 60 + 30

#: 盘中结束时刻（不含），本地时间 16:00
RTH_CLOSE_MINUTE = 16 * 60

#: 夜盘开始时刻（含），本地时间 20:00
OVERNIGHT_OPEN_MINUTE = 20 * 60

#: 夜盘结束时刻（不含），本地时间次日 04:00
OVERNIGHT_CLOSE_MINUTE = 4 * 60

#: 无时区信息时的兜底偏移（分钟）：美东夏令时 UTC-4
FALLBACK_TZ_MINUTES = -240

# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------

#: 错误标识 → 处理建议（用于异常消息增强）
ERROR_HINTS: Dict[str, str] = {
    "invalid_parameter": "参数不合法：请检查日期格式（YYYY-MM-DD）、ktype/extended_time 枚举取值",
    "invalid_symbol": "标的代码不存在：请确认代码格式（如 US.FUTU）与市场前缀",
    "unsupported": "市场前缀不在网关支持范围",
    "internal_error": "网关内部错误：可稍后重试；持续失败请联系 moomoo 支持",
}

#: 鉴权类业务错误码（触发鉴权排查建议）
AUTH_ERROR_CODES = frozenset({-12001, -12002, -12003, -12004, -12005, -12006})

#: 鉴权失败排查建议
AUTH_ERROR_HINT = (
    "鉴权失败：请核对 1) AppKey ID 是否正确；2) dashboard 上传的公钥是否与"
    "本地私钥匹配（可用 derivePublicKeyBase64() 比对）；"
    "3) 本地时间与服务端偏移是否超过 5 秒（可用 fetchServerDrift() 检查）。"
)

#: HTTP 429 处理建议
RATE_LIMIT_HINT = "HTTP 429：触发限流，请指数退避后重试（可参考 Retry-After 响应头）"


# ===========================================================================
# 内联：MoomooOpenAPI/Exception.py（异常）
# ===========================================================================

# -*- coding: utf-8 -*-



class MoomooOpenAPIException(Exception):
    """moomoo OpenAPI 调用异常。

    Attributes:
        code: 业务错误码（响应 JSON 的 ret_code；解析失败时为 None）。
        errorCode: 错误标识（响应 error.code，如 invalid_parameter）。
        httpStatus: HTTP 状态码（网络层失败时为 None）。
        url: 请求 URL。
        hint: 针对错误码的处理建议（无对应建议时为 None）。
    """

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        errorCode: Optional[str] = None,
        httpStatus: Optional[int] = None,
        url: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> None:
        """初始化异常。

        Args:
            message: 中文错误描述。
            code: 业务错误码 ret_code（可选）。
            errorCode: 错误标识 error.code（可选）。
            httpStatus: HTTP 状态码（可选）。
            url: 请求 URL（可选）。
            hint: 处理建议（可选）。

        Returns:
            无。
        """
        super().__init__(message)
        self.code = code
        self.errorCode = errorCode
        self.httpStatus = httpStatus
        self.url = url
        self.hint = hint


# ===========================================================================
# 内联：MoomooOpenAPI/Model.py（KlineBar 等模型）
# ===========================================================================

# -*- coding: utf-8 -*-




@dataclass
class KlineBar:
    """标准化后的单根 K 线（由 ``convertKlineItems`` 从接口原始条目转换而来）。

    【为什么需要这层转换】接口原始条目有若干实测坑，直接使用易出错，转换时
    一次性消化：

    - ``time_zone`` 实测单位是**小时**（如 ``-4``），官方文档称「分钟」；
    - ``date`` 是**所属交易日**，夜盘归属次日，与该行本地日期可能不同；
    - 未产生行情的时间点会返回缺 OHLC 的占位记录。

    Attributes:
        timeKey: 原始毫秒时间戳（``time_key``）。
        time: 标的本地时间字符串 ``YYYY-MM-DD HH:MM:SS``。
        localDate: 标的本地日期 ``YYYY-MM-DD``（按月/按日归档请用此字段）。
        tradeDate: 交易日 ``YYYY-MM-DD``（由接口 ``date`` 归一化，夜盘归属次日）。
        tradeDateInt: 交易日整数 ``YYYYMMDD``（接口原值）。
        session: 由本地时间推导的时段：``RTH`` / ``ETH`` / ``OVERNIGHT``。
        timeZoneMinutes: 归一化后的时区偏移（**分钟**，如 ``-240``）。
        open: 开盘价。
        high: 最高价。
        low: 最低价。
        close: 收盘价。
        volume: 成交量（股）。
        turnover: 成交额。
        changeRate: 涨跌幅（百分数，相对昨收）。
        lastClose: 昨收价。
        peRatio: 市盈率（分钟级通常为 0，日 K 及以上有效）。
        turnoverRate: 换手率（百分数；分钟级通常为 0）。
        name: 标的英文名。
        scName: 标的简体中文名。
        tcName: 标的繁体中文名。
        openInterest: 持仓量（期货/期权）。
        settlePrice: 结算价（期货/期权）。
        impliedVolatility: 隐含波动率（期权）。
    """

    timeKey: int = 0
    time: str = ""
    localDate: str = ""
    tradeDate: str = ""
    tradeDateInt: int = 0
    session: str = ""
    timeZoneMinutes: int = FALLBACK_TZ_MINUTES
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    changeRate: Optional[float] = None
    lastClose: Optional[float] = None
    peRatio: Optional[float] = None
    turnoverRate: Optional[float] = None
    name: Optional[str] = None
    scName: Optional[str] = None
    tcName: Optional[str] = None
    openInterest: Optional[float] = None
    settlePrice: Optional[float] = None
    impliedVolatility: Optional[float] = None

    @property
    def hasPrice(self) -> bool:
        """是否含有效价格（四价至少一项非空）。

        未产生行情的时间点会返回缺 OHLC 的占位记录，可用本属性过滤。

        Returns:
            True 表示含有效价格数据。
        """
        return any(value is not None for value in (self.open, self.high, self.low, self.close))

    def toDict(self) -> Dict[str, Any]:
        """转为字典（剔除值为 None 的字段，便于直接落盘为 JSONL）。

        Returns:
            字段名与属性同名的字典。
        """
        return {key: value for key, value in vars(self).items() if value is not None}


@dataclass
class TradingDay:
    """标准化后的单个交易日（由 ``convertTradingDays`` 转换而来）。

    Attributes:
        date: 交易日 ``YYYY-MM-DD``。
        dateType: 交易日类型：``WHOLE``（全天）/ ``MORNING``（半日市）。
        tradeSecond: 当日交易总秒数。
        isHalfDay: 是否半日市（由 ``dateType`` 判定）。
        year: 年份。
        month: 月份。
    """

    date: str = ""
    dateType: str = ""
    tradeSecond: int = 0
    isHalfDay: bool = False
    year: int = 0
    month: int = 0

    def toDict(self) -> Dict[str, Any]:
        """转为字典（便于直接落盘 JSONL）。

        Returns:
            字段名与属性同名的字典。
        """
        return dict(vars(self))


@dataclass
class StockBasicInfo:
    """标准化后的标的静态档案（由 ``convertBasicInfos`` 转换而来）。

    Attributes:
        code: 标的代码，如 ``US.FUTU``。
        name: 英文名。
        scName: 简体中文名。
        tcName: 繁体中文名。
        stockType: 标的类型：``STOCK`` / ``ETF`` / ``IDX`` 等。
        exchange: 交易交易所，如 ``US`` / ``SEHK`` / ``SSE`` / ``SZSE``。
        lotSize: 每手股数。
        stockId: 内部数值标识（**保留为字符串**，避免跨语言精度损失）。
        stockIdInt: 内部数值标识（整数形态；Python 任意精度，无损失）。
        listingDateMs: 上市时间毫秒时间戳；无上市日时为 0。
        listingDate: 上市日期 ``YYYY-MM-DD``；无上市日时为空串。
        suspension: 是否停牌。
        state: 证券生命周期状态，如 ``NORMAL``。
        contractSize: 合约股数；正股为 None，ETF/指数为 0。
        mainContract: 是否主连合约（期货）。
        stockChildType: 窝轮子类型；非窝轮品类实测为 ``"N/A"``。
        stockOwner: 正股代码；非衍生品时接口不返回该字段（此处为 None）。
    """

    code: str = ""
    name: Optional[str] = None
    scName: Optional[str] = None
    tcName: Optional[str] = None
    stockType: Optional[str] = None
    exchange: Optional[str] = None
    lotSize: Optional[int] = None
    stockId: Optional[str] = None
    stockIdInt: Optional[int] = None
    listingDateMs: int = 0
    listingDate: Optional[str] = None
    suspension: Optional[bool] = None
    state: Optional[str] = None
    contractSize: Optional[int] = None
    mainContract: Optional[bool] = None
    stockChildType: Optional[str] = None
    stockOwner: Optional[str] = None

    def toDict(self) -> Dict[str, Any]:
        """转为字典（剔除值为 None 的字段，便于直接落盘 JSONL）。

        Returns:
            字段名与属性同名的字典。
        """
        return {key: value for key, value in vars(self).items() if value is not None}


# ===========================================================================
# 内联：MoomooOpenAPI/Validator.py（参数校验）
# ===========================================================================

# -*- coding: utf-8 -*-




def validateDate(fieldName: str, value: Optional[str]) -> None:
    """校验日期参数为 ``YYYY-MM-DD``（接口要求最长 10 字符）。

    【实测约束】``start`` / ``end`` 传毫秒时间戳或含时分秒会返回
    ``-3 parameter 'x' exceeds maximum length 10``，因此必须在本地先行拦截。

    Args:
        fieldName: 字段名（用于错误消息）。
        value: 日期字符串；None 表示不校验（该字段可不传）。

    Returns:
        无。

    Raises:
        MoomooOpenAPIException: 格式非法时抛出。
    """
    if value is None:
        return
    text = str(value).strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise MoomooOpenAPIException(
            f"{fieldName} 必须为 YYYY-MM-DD 格式且长度 10，当前为 {value!r}；"
            "接口对超长日期返回 -3 exceeds maximum length 10"
        )
    if not all(part.isdigit() for part in text.split("-")):
        raise MoomooOpenAPIException(
            f"{fieldName} 必须为 YYYY-MM-DD 格式（纯数字），当前为 {value!r}"
        )


# ===========================================================================
# 内联：MoomooOpenAPI/Convert.py（响应转换与时段归类）
# ===========================================================================

# -*- coding: utf-8 -*-




def normalizeTimeZoneMinutes(raw: Any) -> int:
    """把接口 ``time_zone`` 归一化为「分钟」偏移。

    【实测修正】官方文档称 ``time_zone`` 为「时区偏移（分钟）」，
    但真机实测返回的是 **``-4``（即 -4 小时）**，与美东夏令时 UTC-4 一致。
    为避免文档与实现不一致导致时间换算错误，此处按量级自适应：

    - ``|value| <= 24`` 视为**小时**，乘以 60；
    - 否则视为**分钟**，原样返回。

    两种口径取值范围不重叠（真实时区偏移最大 ±14 小时 = ±840 分钟），故判定安全。

    Args:
        raw: 接口返回的 ``time_zone`` 原值（可为 int / str / None）。

    Returns:
        分钟偏移；无法解析时回退为美东夏令时 ``-240``。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return FALLBACK_TZ_MINUTES
    if abs(value) <= 24:
        return value * 60
    return value


def classifyTradingSession(timeKeyMs: int, timeZoneMinutes: int) -> str:
    """按标的**本地时间**判定交易时段。

    划分口径（与 moomoo 时段定义一致）：

    - ``OVERNIGHT`` 夜盘：20:00 - 次日 04:00
    - ``RTH`` 盘中：09:30 - 16:00
    - ``ETH`` 盘前盘后：其余（04:00 - 09:30 与 16:00 - 20:00）

    Args:
        timeKeyMs: 毫秒时间戳。
        timeZoneMinutes: 时区偏移（分钟，须已由 ``normalizeTimeZoneMinutes`` 归一化）。

    Returns:
        时段标识：``RTH`` / ``ETH`` / ``OVERNIGHT``。
    """
    local = dt.datetime.fromtimestamp(
        timeKeyMs / 1000.0, tz=dt.timezone(dt.timedelta(minutes=timeZoneMinutes))
    )
    minute = local.hour * 60 + local.minute
    if minute >= OVERNIGHT_OPEN_MINUTE or minute < OVERNIGHT_CLOSE_MINUTE:
        return SESSION_OVERNIGHT
    if RTH_OPEN_MINUTE <= minute < RTH_CLOSE_MINUTE:
        return SESSION_RTH
    return SESSION_ETH


def convertKlineItems(items: Optional[Iterable[Dict[str, Any]]]) -> List[KlineBar]:
    """把接口 ``kline_list`` 原始条目转换为标准化的 ``KlineBar`` 列表。

    转换内容：

    - 时间戳 → 标的本地时间字符串与本地日期（``time`` / ``localDate``）；
    - ``time_zone`` 按量级归一化为**分钟**（``timeZoneMinutes``）；
    - ``date`` 归一化为交易日字符串（``tradeDate``），**夜盘归属次日**；
    - 按本地时间推导时段（``session``）。

    Args:
        items: 接口返回的 ``kline_list``（list[dict]）；None 或空返回空列表。

    Returns:
        ``KlineBar`` 列表（保持输入顺序，不去重、不排序）。

    Raises:
        无（无法解析时间戳的条目会被跳过，不产生脏数据）。
    """
    if not items:
        return []
    bars: List[KlineBar] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            timeKey = int(item.get("time_key"))
        except (TypeError, ValueError):
            continue
        tzMinutes = normalizeTimeZoneMinutes(item.get("time_zone"))
        local = dt.datetime.fromtimestamp(
            timeKey / 1000.0, tz=dt.timezone(dt.timedelta(minutes=tzMinutes))
        )
        tradeDateInt = _toOptionalInt(item.get("date")) or 0
        tradeDate = (
            _formatDateInt(tradeDateInt) if tradeDateInt else local.strftime("%Y-%m-%d")
        )
        bars.append(
            KlineBar(
                timeKey=timeKey,
                time=local.strftime("%Y-%m-%d %H:%M:%S"),
                localDate=local.strftime("%Y-%m-%d"),
                tradeDate=tradeDate or "",
                tradeDateInt=tradeDateInt,
                session=classifyTradingSession(timeKey, tzMinutes),
                timeZoneMinutes=tzMinutes,
                open=_toOptionalFloat(item.get("open")),
                high=_toOptionalFloat(item.get("high")),
                low=_toOptionalFloat(item.get("low")),
                close=_toOptionalFloat(item.get("close")),
                volume=_toOptionalFloat(item.get("volume")),
                turnover=_toOptionalFloat(item.get("turnover")),
                changeRate=_toOptionalFloat(item.get("change_rate")),
                lastClose=_toOptionalFloat(item.get("last_close")),
                peRatio=_toOptionalFloat(item.get("pe_ratio")),
                turnoverRate=_toOptionalFloat(item.get("turnover_rate")),
                name=item.get("name") or None,
                scName=item.get("sc_name") or None,
                tcName=item.get("tc_name") or None,
                openInterest=_toOptionalFloat(item.get("open_interest")),
                settlePrice=_toOptionalFloat(item.get("settle_price")),
                impliedVolatility=_toOptionalFloat(item.get("implied_volatility")),
            )
        )
    return bars


def filterBarsWithPrice(bars: Iterable[KlineBar]) -> List[KlineBar]:
    """过滤掉无价格的占位记录。

    Args:
        bars: ``KlineBar`` 序列。

    Returns:
        仅含有效价格的 ``KlineBar`` 列表。
    """
    return [bar for bar in bars if bar.hasPrice]


def deduplicateBars(bars: Iterable[KlineBar]) -> List[KlineBar]:
    """按 ``timeKey`` 去重（先到先得）并升序排序。

    Args:
        bars: ``KlineBar`` 序列。

    Returns:
        去重且按时间升序的 ``KlineBar`` 列表。
    """
    seen: Dict[int, None] = {}
    unique: List[KlineBar] = []
    for bar in bars:
        if bar.timeKey in seen:
            continue
        seen[bar.timeKey] = None
        unique.append(bar)
    return sorted(unique, key=lambda item: item.timeKey)


def summarizeBarSessions(bars: Iterable[KlineBar]) -> Dict[str, int]:
    """统计各时段的 K 线条数。

    Args:
        bars: ``KlineBar`` 序列。

    Returns:
        形如 ``{"RTH": 390, "ETH": 570, "OVERNIGHT": 480}`` 的字典。
    """
    summary: Dict[str, int] = {}
    for bar in bars:
        summary[bar.session] = summary.get(bar.session, 0) + 1
    return summary


def convertTradingDays(items: Optional[Iterable[Dict[str, Any]]]) -> List[TradingDay]:
    """把交易日历响应 ``trading_days`` 转换为标准化 ``TradingDay`` 列表。

    转换内容：按 ``trade_date_type`` 判定半日市，并拆出年/月便于按月统计。

    Args:
        items: 接口返回的 ``trading_days``（list[dict]）；None 或空返回空列表。

    Returns:
        ``TradingDay`` 列表（保持输入顺序）。

    Raises:
        无（缺少 ``time`` 的条目会被跳过）。
    """
    if not items:
        return []
    result: List[TradingDay] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dateText = str(item.get("time") or "").strip()
        if not dateText:
            continue
        dateType = str(item.get("trade_date_type") or "").strip()
        parts = dateText.split("-")
        result.append(
            TradingDay(
                date=dateText,
                dateType=dateType,
                tradeSecond=_toOptionalInt(item.get("trade_second")) or 0,
                # 实测：全天 = WHOLE，半日市 = MORNING
                isHalfDay=dateType.upper() == "MORNING",
                year=_toOptionalInt(parts[0]) or 0 if len(parts) == 3 else 0,
                month=_toOptionalInt(parts[1]) or 0 if len(parts) == 3 else 0,
            )
        )
    return result


def convertBasicInfos(items: Optional[Iterable[Dict[str, Any]]]) -> List[StockBasicInfo]:
    """把标的档案响应 ``basic_list`` 转换为标准化 ``StockBasicInfo`` 列表。

    转换内容：``stock_id`` 同时保留字符串与整数两种形态（避免跨语言精度损失）、
    ``listing_date`` 归一化为日期字符串、``stock_child_type`` 的 ``"N/A"`` 保留原值。

    Args:
        items: 接口返回的 ``basic_list``（list[dict]）；None 或空返回空列表。

    Returns:
        ``StockBasicInfo`` 列表（保持输入顺序）。

    Raises:
        无（缺少 ``code`` 的条目会被跳过）。
    """
    if not items:
        return []
    result: List[StockBasicInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        stockIdInt = _toOptionalInt(item.get("stock_id"))
        listingMs = _toOptionalInt(item.get("listing_date")) or 0
        listingDate: Optional[str] = None
        if listingMs:
            listingDate = dt.datetime.fromtimestamp(
                listingMs / 1000.0, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d")
        result.append(
            StockBasicInfo(
                code=code,
                name=item.get("name") or None,
                scName=item.get("sc_name") or None,
                tcName=item.get("tc_name") or None,
                stockType=item.get("stock_type") or None,
                exchange=item.get("exchange") or None,
                lotSize=_toOptionalInt(item.get("lot_size")),
                stockId=str(stockIdInt) if stockIdInt is not None else None,
                stockIdInt=stockIdInt,
                listingDateMs=listingMs,
                listingDate=listingDate,
                suspension=(
                    item.get("suspension") if isinstance(item.get("suspension"), bool) else None
                ),
                state=item.get("state") or None,
                contractSize=_toOptionalInt(item.get("contract_size")),
                mainContract=(
                    item.get("main_contract")
                    if isinstance(item.get("main_contract"), bool)
                    else None
                ),
                stockChildType=item.get("stock_child_type") or None,
                stockOwner=item.get("stock_owner") or None,
            )
        )
    return result


def groupByStockType(infos: Iterable[StockBasicInfo]) -> Dict[str, List[StockBasicInfo]]:
    """按标的类型分组（便于批量处理不同品类）。

    Args:
        infos: ``StockBasicInfo`` 序列。

    Returns:
        形如 ``{"STOCK": [...], "ETF": [...]}`` 的字典。
    """
    grouped: Dict[str, List[StockBasicInfo]] = {}
    for info in infos:
        grouped.setdefault(info.stockType or "UNKNOWN", []).append(info)
    return grouped


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _toOptionalFloat(value: Any) -> Optional[float]:
    """尽力转换为 ``float``；失败或 NaN 返回 None。

    Args:
        value: 原始值。

    Returns:
        浮点数或 None。
    """
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _toOptionalInt(value: Any) -> Optional[int]:
    """尽力转换为 ``int``；失败返回 None（浮点会截断为整数）。

    Args:
        value: 原始值。

    Returns:
        整数或 None。
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _formatDateInt(value: Any) -> Optional[str]:
    """把 ``YYYYMMDD`` 整数归一化为 ``YYYY-MM-DD`` 字符串。

    Args:
        value: 形如 ``20260803`` 的日期整数或字符串。

    Returns:
        归一化日期字符串；非法或为 0 时返回 None。
    """
    number = _toOptionalInt(value)
    if not number:
        return None
    return f"{number // 10000:04d}-{(number // 100) % 100:02d}-{number % 100:02d}"


# ===========================================================================
# 内联：MoomooOpenAPI/HTTPTransport.py（请求与签名装配）
# ===========================================================================

# -*- coding: utf-8 -*-




#: 自定义传输层签名：``(path, queryString, headers, bodyBytes) -> (httpStatus, payload)``
TransportType = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Any]]


class MoomooOpenApiTransport:
    """moomoo OpenAPI 请求客户端。

    凭据解析优先级（高 → 低）：

    1. 直接传入 ``signature``（已构造好的签名器，最高优先级）；
    2. 参数 ``appKeyId`` + ``privateKeyText`` / ``privateKeyFile``；
    3. 环境变量 ``MOOMOO_OPENAPI_AK`` + ``MOOMOO_OPENAPI_SK_FILE`` / ``MOOMOO_OPENAPI_SK``。

    Attributes:
        appKeyId: AppKey ID（请求头 ``X-Api-Key``）。
        timeout: 默认请求超时（秒）。
        lastSignatureSource: 最近一次成功签名所用的原文（调试用）。
    """

    def __init__(
        self,
        appKeyId: Optional[str] = None,
        privateKeyText: Optional[str] = None,
        privateKeyFile: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        signature: Optional[FutuOpenApiSignature] = None,
        customTransport: Optional[TransportType] = None,
    ) -> None:
        """初始化客户端。

        Args:
            appKeyId: AppKey ID；为 None 时读环境变量 ``MOOMOO_OPENAPI_AK``。
            privateKeyText: 私钥原文（Base64 PKCS#8 DER 或 PEM）。
            privateKeyFile: 私钥文件路径（仅当 privateKeyText 为空时使用）。
            timeout: 默认请求超时（秒），默认 25。
            signature: 可选，注入已构造的签名器（便于测试与共享实例）。
            customTransport: 可选，注入自定义传输层（离线测试或自定义代理）。

        Returns:
            无。

        Raises:
            MoomooOpenAPIException: 未提供任何私钥、或私钥无法解析时抛出。
        """
        self.timeout = timeout
        self.lastSignatureSource: Optional[str] = None
        self._customTransport = customTransport

        if signature is not None:
            self._signature = signature
            self.appKeyId = appKeyId or signature.appKeyId
            if self.appKeyId and not signature.appKeyId:
                signature.setAppKeyId(self.appKeyId)
            return

        resolvedKeyId = appKeyId if appKeyId is not None else os.environ.get(ENV_APP_AK, "")
        keyText = privateKeyText or ""
        if not keyText:
            filePath = privateKeyFile or os.environ.get(ENV_APP_SK_FILE, "")
            if filePath:
                try:
                    with open(filePath, "r", encoding="utf-8") as handle:
                        keyText = handle.read()
                except OSError as exc:
                    raise MoomooOpenAPIException(
                        f"读取私钥文件失败：{filePath}（{exc}）"
                    ) from exc
        if not keyText:
            keyText = os.environ.get(ENV_APP_SK, "")
        if not keyText:
            raise MoomooOpenAPIException(
                "未找到私钥：请通过构造参数 privateKeyText / privateKeyFile 传入，"
                f"或设置环境变量 {ENV_APP_SK_FILE} / {ENV_APP_SK}"
                "（PKCS#8 Base64 或 PEM）"
            )
        try:
            self._signature = FutuOpenApiSignature(keyText, resolvedKeyId or "")
        except FutuOpenApiSignatureError as exc:
            raise MoomooOpenAPIException(f"私钥解析失败：{exc}") from exc
        self.appKeyId = resolvedKeyId or ""

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def signature(self) -> FutuOpenApiSignature:
        """当前签名器实例（只读）。"""
        return self._signature

    @property
    def algorithm(self) -> str:
        """当前签名算法标识（``Ed25519`` 或 ``RSA-SHA256``）。"""
        return self._signature.algorithm

    def derivePublicKeyBase64(self) -> str:
        """派生公钥（SPKI DER 的 Base64），用于与 dashboard 上传的公钥比对。

        Returns:
            Base64 编码公钥。

        Raises:
            MoomooOpenAPIException: 私钥结构非法导致派生失败时抛出。
        """
        try:
            return self._signature.derivePublicKeyBase64()
        except FutuOpenApiSignatureError as exc:
            raise MoomooOpenAPIException(f"公钥派生失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------

    @staticmethod
    def _encodeQuery(params: Optional[Dict[str, Any]]) -> str:
        """把参数字典编码为查询串（签名与实际请求共用同一字符串）。

        注意：签名原文中的 ``query_string`` 必须与最终请求的查询串**逐字节一致**，
        因此本方法产出的字符串同时用于签名与 URL 拼接。

        Args:
            params: 查询参数字典（值会被字符串化；None 值的键会被跳过）。

        Returns:
            查询串（不含开头 ``?``）；无参数时返回空串。
        """
        if not params:
            return ""
        cleaned: List[Tuple[str, str]] = [
            (key, str(value)) for key, value in params.items() if value is not None
        ]
        return urllib.parse.urlencode(cleaned)

    def requestApi(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        timestampMs: Optional[int] = None,
        nonce: Optional[str] = None,
        timeout: Optional[int] = None,
        plainPayload: bool = False,
    ) -> Any:
        """通用底层方法：签名 → HTTP 请求 → 响应解析（供各接口方法复用）。

        Args:
            path: 接口路径（以 "/" 开头，如 "/api/v1.0/quote/trading-days"）。
            method: HTTP 方法，GET 或 POST。
            params: GET 查询参数字典（值会被字符串化）。
            body: POST 请求体（dict，序列化为紧凑 JSON，类型保真）。
            timestampMs: 可选，指定毫秒时间戳（默认当前时间）。
            nonce: 可选，指定 ``X-Nonce``（默认随机生成）。
            timeout: 可选，覆盖实例级超时（秒）。
            plainPayload: 响应是否为**裸数据**（无 ``ret_code`` / ``data`` 包裹）。
                实测 ``/api/v1.0/server-time`` 直接返回 ``{"server_time_ms": "..."}``，
                该接口需置 True；其余接口保持默认 False。

        Returns:
            响应 JSON 的 ``data`` 字段（业务数据；ret_code = 0 时返回）；
            ``plainPayload=True`` 时返回整个响应体。

        Raises:
            MoomooOpenAPIException: 缺少 AppKey ID、HTTP 非 200、响应解析失败、
                业务 ret_code != 0、网络失败时抛出（含接口路径与业务码）。
        """
        timeout = timeout or self.timeout
        upperMethod = method.upper()
        queryString = self._encodeQuery(params) if upperMethod == "GET" else ""
        url = BASE_URL + path + (f"?{queryString}" if queryString else "")

        bodyBytes: Optional[bytes] = None
        if upperMethod == "POST":
            if body is None:
                raise MoomooOpenAPIException(
                    f"POST 接口 {path} 必须提供请求体 body 参数。", url=url
                )
            # 紧凑 JSON：签名基于原始字节，不得二次格式化
            bodyBytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        elif upperMethod != "GET":
            raise MoomooOpenAPIException(f"不支持的 HTTP 方法：{method}", url=url)

        # 1. 签名（签名原文可在 lastSignatureSource 中回看）
        try:
            authHeaders, source = self._signature.buildAuthHeaders(
                httpMethod=method,
                requestPath=path,
                queryString=queryString,
                body=bodyBytes,
                timestampMs=timestampMs,
                nonce=nonce,
            )
        except FutuOpenApiSignatureError as exc:
            raise MoomooOpenAPIException(
                f"接口 {path} 签名失败：{exc}", url=url
            ) from exc
        self.lastSignatureSource = source

        # 2. 构造请求头
        mergedHeaders = dict(
            DEFAULT_POST_HEADERS if upperMethod == "POST" else DEFAULT_GET_HEADERS
        )
        mergedHeaders.update(authHeaders)
        mergedHeaders["user-agent"] = DEFAULT_USER_AGENT

        # 3. 发送请求（transport 非空时走注入的传输层，便于离线测试与自定义代理）
        if self._customTransport is not None:
            try:
                httpStatus, payload = self._customTransport(path, queryString, mergedHeaders, bodyBytes)
            except MoomooOpenAPIException:
                raise
            except Exception as exc:  # noqa: BLE001 - 传输层异常统一包装
                raise MoomooOpenAPIException(
                    f"接口 {path} 传输层失败：{exc}（url={url}）", url=url
                ) from exc
            return self._unwrapPayload(path, payload, httpStatus, url, plainPayload)

        request = urllib.request.Request(
            url, data=bodyBytes, headers=mergedHeaders, method=upperMethod
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                httpStatus = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001 - 读取错误体失败不影响主流程
                detail = ""
            raise MoomooOpenAPIException(
                f"接口 {path} HTTP 错误：状态码 {exc.code}；响应体 {detail}（url={url}）",
                httpStatus=exc.code,
                url=url,
                hint=RATE_LIMIT_HINT if exc.code == 429 else None,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MoomooOpenAPIException(
                f"接口 {path} 网络请求失败：{exc}（url={url}）", url=url
            ) from exc

        # 4. 解析响应
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise MoomooOpenAPIException(
                f"接口 {path} 响应解析失败（HTTP {httpStatus}）：{raw[:200]}",
                httpStatus=httpStatus,
                url=url,
            ) from exc
        return self._unwrapPayload(path, payload, httpStatus, url, plainPayload)

    @staticmethod
    def _unwrapPayload(
        path: str, payload: Any, httpStatus: int, url: str, plainPayload: bool = False
    ) -> Any:
        """校验响应结构并返回 ``data`` 字段。

        Args:
            path: 接口路径（用于错误消息）。
            payload: 已解析的响应对象。
            httpStatus: HTTP 状态码。
            url: 请求 URL。
            plainPayload: 是否允许**裸数据**响应（无 ``ret_code`` 包裹）。

        Returns:
            响应 ``data`` 字段；``plainPayload=True`` 且响应无 ``ret_code`` 时
            返回整个响应体。

        Raises:
            MoomooOpenAPIException: 响应结构异常或业务 ``ret_code != 0`` 时抛出。
        """
        if not isinstance(payload, dict):
            raise MoomooOpenAPIException(
                f"接口 {path} 响应结构异常（期望 JSON 对象，实际 {type(payload).__name__}）",
                httpStatus=httpStatus,
                url=url,
            )

        retCode = payload.get("ret_code")
        if retCode is None and plainPayload:
            # 实测 /api/v1.0/server-time 直接返回裸数据，无 ret_code / data 包裹
            return payload
        if retCode != 0:
            error = payload.get("error") or {}
            errorCode = error.get("code")
            hint = ERROR_HINTS.get(errorCode or "")
            if retCode in AUTH_ERROR_CODES:
                hint = AUTH_ERROR_HINT
            raise MoomooOpenAPIException(
                f"接口 {path} 调用失败：ret_code={retCode}, ret_msg={payload.get('ret_msg')}, "
                f"error.code={errorCode}, error.message={error.get('message')}（url={url}）",
                code=retCode,
                errorCode=errorCode,
                httpStatus=httpStatus,
                url=url,
                hint=hint,
            )

        return payload.get("data")


# ===========================================================================
# 内联：MoomooOpenAPI/QuoteHistoryKline.py（历史 K 线三接口）
# ===========================================================================

# -*- coding: utf-8 -*-





def getHistoryKline(
    client: "MoomooOpenAPIClient",
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    ktype: str = KTYPE_MIN,
    autype: str = AUTYPE_FORWARD,
    extendedTime: str = EXTENDED_TIME_ALL,
    num: Optional[int] = None,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """获取指定标的的历史 K 线（GET /api/v1.0/quote/{symbol}/history-kline）。

    接口直通方法：返回接口原始响应 ``data``，不做任何字段换算。

    Args:
        client: 已构造的请求客户端。
        symbol: 标的代码，moomoo 格式，如 ``US.FUTU`` / ``HK.00700``。
        start: 起始日期（含），``YYYY-MM-DD``；为 None 时不传（服务端按 num 前推）。
        end: 结束日期（**不含**，半开区间 ``[start, end)``），``YYYY-MM-DD``；**必填**。
        ktype: K 线类型，默认 ``KTYPE_MIN``（1 分钟）。
        autype: 复权类型，默认 ``AUTYPE_FORWARD``（前复权）。
        extendedTime: 时段开关，默认 ``EXTENDED_TIME_ALL``。
        num: 数量；为 None 时不传该参数（区间模式上限 1000 条、游标模式默认 370 条）。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        响应 ``data`` 字典：``{"kline_list": [...], "next_time": ...,
        "volume_precision": ...}``。

    Raises:
        MoomooOpenAPIException: ``end`` 缺失、参数非法或接口报错时抛出。
    """
    if not end:
        raise MoomooOpenAPIException(
            "历史 K 线接口的 end 参数必填（格式 YYYY-MM-DD）；"
            "若只想取最近若干条，请显式传入 end",
            url=PATH_HISTORY_KLINE.format(symbol=symbol),
        )
    validateDate("start", start)
    validateDate("end", end)
    if num is not None and not 1 <= int(num) <= MAX_NUM_PER_REQUEST:
        raise MoomooOpenAPIException(
            f"num 须在 1-{MAX_NUM_PER_REQUEST} 之间（>{MAX_NUM_PER_REQUEST} 服务端返回 -3），"
            f"当前为 {num}"
        )
    return client.requestApi(
        PATH_HISTORY_KLINE.format(symbol=symbol),
        method="GET",
        params={
            "start": start,
            "end": end,
            "ktype": ktype,
            "autype": autype,
            "extended_time": extendedTime,
            "num": num,
        },
        timeout=timeout,
    )


def fetchHistoryKline(
    client: "MoomooOpenAPIClient",
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    ktype: str = KTYPE_MIN,
    autype: str = AUTYPE_FORWARD,
    extendedTime: str = EXTENDED_TIME_ALL,
    num: Optional[int] = None,
    dropEmpty: bool = True,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """获取历史 K 线并转换为 ``KlineBar`` 列表（扩展封装：``getHistoryKline`` + 转换）。

    换算细节（时区归一化、交易日归属、时段判定）全部由
    ``Convert.convertKlineItems`` 完成。

    Args:
        client: 已构造的请求客户端。
        symbol: 标的代码，如 ``US.FUTU``。
        start: 起始日期（含），``YYYY-MM-DD``；None 时不传。
        end: 结束日期（**不含**，半开区间 ``[start, end)``），``YYYY-MM-DD``；**必填**。
        ktype: K 线类型，默认 1 分钟。
        autype: 复权类型，默认前复权。
        extendedTime: 时段开关，默认 ``EXTENDED_TIME_ALL``。
        num: 数量；None 时不传（区间模式上限 1000 条、游标模式默认 370 条）。
        dropEmpty: 是否剔除无 OHLC 的占位记录（默认 True）。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        字典：``symbol`` / ``ktype`` / ``bars``（``KlineBar`` 列表，升序未去重）/
        ``sessions``（时段条数统计）/ ``timeZoneMinutes`` / ``raw``（接口原始 data）。

    Raises:
        MoomooOpenAPIException: 参数非法或接口报错时抛出。
    """
    data = getHistoryKline(
        client,
        symbol=symbol,
        start=start,
        end=end,
        ktype=ktype,
        autype=autype,
        extendedTime=extendedTime,
        num=num,
        timeout=timeout,
    )
    bars = convertKlineItems((data or {}).get("kline_list"))
    if dropEmpty:
        bars = filterBarsWithPrice(bars)
    return {
        "symbol": symbol,
        "ktype": ktype,
        "bars": bars,
        "sessions": summarizeBarSessions(bars),
        "timeZoneMinutes": bars[0].timeZoneMinutes if bars else FALLBACK_TZ_MINUTES,
        "raw": data,
    }


def _mergeFullDayItems(
    client: "MoomooOpenAPIClient",
    symbol: str,
    day: str,
    ktype: str,
    autype: str,
    timeout: Optional[int],
) -> Dict[str, Any]:
    """按 0/1/2 三档 ``extended_time`` 各请求一次并按 ``time_key`` 合并去重。

    内部辅助函数，用于绕开「单次 1000 条上限 + 不支持翻页 + 各档漏时段」三重限制。

    Args:
        client: 已构造的请求客户端。
        symbol: 标的代码。
        day: 目标自然日 ``YYYY-MM-DD``（标的市场时区）。
        ktype: K 线类型。
        autype: 复权类型。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        字典：``{"kline_list": [按 time_key 升序去重的条目], "day": day,
        "requestCount": n, "tierSizes": {...}}``。

    Raises:
        MoomooOpenAPIException: 日期非法或任一档请求失败时抛出。
    """
    validateDate("day", day)
    try:
        endDate = (dt.datetime.strptime(day, "%Y-%m-%d").date() + dt.timedelta(days=1)).isoformat()
    except ValueError as exc:
        raise MoomooOpenAPIException(f"day 不是合法日期：{day!r}") from exc

    merged: Dict[int, Dict[str, Any]] = {}
    tierSizes: Dict[str, int] = {}
    for tier in EXTENDED_TIME_TIERS:
        data = getHistoryKline(
            client,
            symbol=symbol,
            start=day,
            end=endDate,
            ktype=ktype,
            autype=autype,
            extendedTime=tier,
            timeout=timeout,
        )
        items = (data or {}).get("kline_list") or []
        tierSizes[tier] = len(items)
        for item in items:
            key = item.get("time_key") if isinstance(item, dict) else None
            if isinstance(key, int) and key not in merged:
                merged[key] = item
    return {
        "kline_list": [merged[key] for key in sorted(merged)],
        "day": day,
        "requestCount": len(EXTENDED_TIME_TIERS),
        "tierSizes": tierSizes,
    }


def fetchHistoryKlineFullDay(
    client: "MoomooOpenAPIClient",
    symbol: str,
    day: str,
    ktype: str = KTYPE_MIN,
    autype: str = AUTYPE_FORWARD,
    dropEmpty: bool = True,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """获取某一自然日**全时段** K 线并转换为 ``KlineBar`` 列表（扩展封装）。

    内部按 0/1/2 三档 ``extended_time`` 各请求一次，合并去重后转换，
    返回已按 ``timeKey`` 去重升序的 ``KlineBar`` 列表。

    Args:
        client: 已构造的请求客户端。
        symbol: 标的代码。
        day: 目标自然日 ``YYYY-MM-DD``（标的市场时区）。
        ktype: K 线类型，默认 1 分钟。
        autype: 复权类型，默认前复权。
        dropEmpty: 是否剔除无 OHLC 的占位记录（默认 True）。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        字典：``symbol`` / ``day`` / ``ktype`` / ``bars`` / ``sessions`` /
        ``requestCount`` / ``tierSizes`` / ``timeZoneMinutes`` /
        ``raw``（三档并集后的原始条目与各档条数）。

    Raises:
        MoomooOpenAPIException: 日期非法或任一档请求失败时抛出。
    """
    data = _mergeFullDayItems(
        client, symbol=symbol, day=day, ktype=ktype, autype=autype, timeout=timeout
    )
    bars = deduplicateBars(convertKlineItems((data or {}).get("kline_list")))
    if dropEmpty:
        bars = filterBarsWithPrice(bars)
    return {
        "symbol": symbol,
        "day": day,
        "ktype": ktype,
        "bars": bars,
        "sessions": summarizeBarSessions(bars),
        "requestCount": (data or {}).get("requestCount", 0),
        "tierSizes": (data or {}).get("tierSizes", {}),
        "timeZoneMinutes": bars[0].timeZoneMinutes if bars else FALLBACK_TZ_MINUTES,
        "raw": data,
    }


# ===========================================================================
# 内联：MoomooOpenAPI/QuoteServerTime.py（服务端时间）
# ===========================================================================

# -*- coding: utf-8 -*-



#: 响应中可能承载服务端时间戳的字段名（按优先级探测）
TIMESTAMP_FIELDS = ("server_time_ms", "timestamp_ms", "timestamp", "server_time")



def getServerTime(client: "MoomooOpenAPIClient", timeout: Optional[int] = None) -> int:
    """获取服务端毫秒时间戳（GET /api/v1.0/server-time）。

    用于校验本地时钟偏移：官方对 AppKey 签名的时间戳偏移阈值为 5 秒，
    超出会返回 ``-12006``。

    Args:
        client: 已构造的请求客户端。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        服务端毫秒时间戳（响应实测形如 ``{"server_time_ms": "1789986366383"}``，
        值为字符串，此处已转为 int）。

    Raises:
        MoomooOpenAPIException: 接口报错或响应缺少时间戳字段时抛出。

    Note:
        实测本接口**直接返回裸数据**（无 ``ret_code`` / ``data`` 包裹），
        因此请求时置 ``plainPayload=True``；若服务端将来改为标准包裹，
        本函数同样可以正确解析。
    """
    data = client.requestApi(
        PATH_SERVER_TIME, method="GET", timeout=timeout, plainPayload=True
    )
    value: Any = None
    if isinstance(data, dict):
        for key in TIMESTAMP_FIELDS:
            if key in data:
                value = data[key]
                break
    elif isinstance(data, int):
        value = data
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int):
        return value
    raise MoomooOpenAPIException(
        f"server-time 响应缺少可识别的时间戳字段，实际响应 data={data!r}",
        url=BASE_URL + PATH_SERVER_TIME,
    )


def fetchServerDrift(
    client: "MoomooOpenAPIClient", timeout: Optional[int] = None
) -> Tuple[int, int, int]:
    """比对本地与服务端时间偏移（扩展封装：本机时间 + ``getServerTime``）。

    签名时间戳校验的快速自检。

    Args:
        client: 已构造的请求客户端。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        ``(本地毫秒时间戳, 服务端毫秒时间戳, 偏移毫秒)``，
        偏移 = 本地 - 服务端；绝对值超过 5000 时会触发签名失败。

    Raises:
        MoomooOpenAPIException: 服务端时间接口调用失败时抛出。
    """
    localMs = int(time.time() * 1000)
    serverMs = getServerTime(client, timeout=timeout)
    return localMs, serverMs, localMs - serverMs


# ===========================================================================
# 内联：MoomooOpenAPI/QuoteStockBasicInfo.py（标的静态信息）
# ===========================================================================

# -*- coding: utf-8 -*-





def postStockBasicInfo(
    client: "MoomooOpenAPIClient",
    codeList: List[str],
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """批量获取标的静态档案（POST /api/v1.0/quote/stock-basicinfo）。

    接口直通方法：返回接口原始响应 ``data``，不做任何字段换算。

    Args:
        client: 已构造的请求客户端。
        codeList: 标的代码列表，如 ``["US.FUTU", "HK.00700"]``，数量 1-400。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        响应 ``data`` 字典：``{"basic_list": [{"code", "name", "sc_name",
        "lot_size", "stock_type", "exchange", ...}, ...]}``。

    Raises:
        MoomooOpenAPIException: 代码列表为空、超长或接口报错时抛出。
    """
    if not codeList:
        raise MoomooOpenAPIException("codeList 不能为空（单次需 1-400 个标的代码）")
    if len(codeList) > MAX_BASICINFO_CODES:
        raise MoomooOpenAPIException(
            f"codeList 单次最多 {MAX_BASICINFO_CODES} 个，当前为 {len(codeList)} 个"
        )
    return client.requestApi(
        PATH_STOCK_BASICINFO,
        method="POST",
        body={"code_list": [str(code) for code in codeList]},
        timeout=timeout,
    )


def fetchStockBasicInfo(
    client: "MoomooOpenAPIClient",
    codeList: List[str],
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """批量获取标的静态档案并转换为 ``StockBasicInfo`` 列表
    （扩展封装：``postStockBasicInfo`` + 转换）。

    ``stock_id`` 精度保护、``listing_date`` 归一化由
    ``Convert.convertBasicInfos`` 完成；分品类归组由 ``groupByStockType`` 完成。

    Args:
        client: 已构造的请求客户端。
        codeList: 标的代码列表，数量 1-400。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        字典：``infos``（``StockBasicInfo`` 列表）/ ``byStockType``（按品类分组）/
        ``requested``（请求代码数）/ ``returned``（返回条数）/
        ``missing``（未解析出的代码列表）/ ``raw``。

    Raises:
        MoomooOpenAPIException: 参数非法或接口报错时抛出。
    """
    data = postStockBasicInfo(client, codeList, timeout=timeout)
    infos = convertBasicInfos((data or {}).get("basic_list"))
    returned = {info.code for info in infos}
    return {
        "infos": infos,
        "byStockType": groupByStockType(infos),
        "requested": len(codeList),
        "returned": len(infos),
        "missing": [code for code in codeList if code not in returned],
        "raw": data,
    }


# ===========================================================================
# 内联：MoomooOpenAPI/QuoteTradingDays.py（交易日历）
# ===========================================================================

# -*- coding: utf-8 -*-





def getTradingDays(
    client: "MoomooOpenAPIClient",
    market: str,
    start: str,
    end: str,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """获取指定市场的交易日历（GET /api/v1.0/quote/trading-days）。

    接口直通方法：返回接口原始响应 ``data``，不做任何字段换算。

    Args:
        client: 已构造的请求客户端。
        market: 市场前缀，支持 ``HK`` / ``US`` / ``SH`` / ``SZ`` / ``BJ`` /
            ``SG`` / ``JP`` / ``KR`` / ``CA`` / ``AU`` / ``JP_FUTURE`` / ``SG_FUTURE``。
        start: 起始日期（含），``YYYY-MM-DD``。
        end: 结束日期（含），``YYYY-MM-DD``，须 >= start。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        响应 ``data`` 字典：``{"trading_days": [{"time", "trade_date_type",
        "trade_second"}, ...]}``。

    Raises:
        MoomooOpenAPIException: 参数非法或接口报错时抛出。
    """
    validateDate("start", start)
    validateDate("end", end)
    return client.requestApi(
        PATH_TRADING_DAYS,
        method="GET",
        params={"market": market, "start": start, "end": end},
        timeout=timeout,
    )


def fetchTradingDays(
    client: "MoomooOpenAPIClient",
    market: str,
    start: str,
    end: str,
    halfDaysOnly: bool = False,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """获取交易日历并转换为 ``TradingDay`` 列表（扩展封装：``getTradingDays`` + 转换）。

    半日市判定与年/月拆解全部由 ``Convert.convertTradingDays`` 完成。

    Args:
        client: 已构造的请求客户端。
        market: 市场前缀（``US`` / ``HK`` / ``SH`` / ``SZ`` 等）。
        start: 起始日期（含），``YYYY-MM-DD``。
        end: 结束日期（含），``YYYY-MM-DD``。
        halfDaysOnly: 是否只返回半日市（默认 False 返回全部交易日）。
        timeout: 可选，覆盖实例级超时（秒）。

    Returns:
        字典：``market`` / ``days``（``TradingDay`` 列表）/ ``halfDays``
        （半日市列表）/ ``count`` / ``halfDayCount`` / ``raw``。

    Raises:
        MoomooOpenAPIException: 参数非法或接口报错时抛出。
    """
    data = getTradingDays(client, market=market, start=start, end=end, timeout=timeout)
    days = convertTradingDays((data or {}).get("trading_days"))
    halfDays = [day for day in days if day.isHalfDay]
    return {
        "market": market,
        "days": halfDays if halfDaysOnly else days,
        "halfDays": halfDays,
        "count": len(days),
        "halfDayCount": len(halfDays),
        "raw": data,
    }


# ===========================================================================
# 内联：MoomooOpenAPI/ClientFacade.py（客户端门面与工厂）
# ===========================================================================

# -*- coding: utf-8 -*-





class MoomooOpenAPIClient:
    """moomoo OpenAPI（webapi.moomoo.com）行情网关。

    封装官方 REST 接口：历史 K 线、交易日历、标的静态档案、服务端时间，
    并暴露通用底层请求方法 ``requestApi``。

    凭据解析优先级（高 → 低）：

    1. 直接传入 ``signature``（已构造好的签名器，最高优先级）；
    2. 参数 ``appKeyId`` + ``privateKeyText`` / ``privateKeyFile``；
    3. 环境变量 ``MOOMOO_OPENAPI_AK`` + ``MOOMOO_OPENAPI_SK_FILE`` / ``MOOMOO_OPENAPI_SK``。

    Attributes:
        providerName: 提供者标识，固定 "MoomooOpenAPIClient"。
    """

    #: 提供者标识
    providerName = "MoomooOpenAPIClient"

    def __init__(
        self,
        appKeyId: Optional[str] = None,
        privateKeyText: Optional[str] = None,
        privateKeyFile: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        signature: Optional[FutuOpenApiSignature] = None,
        customTransport: Optional[TransportType] = None,
    ) -> None:
        """初始化客户端。

        Args:
            appKeyId: AppKey ID（请求头 X-Api-Key）。
            privateKeyText: 私钥原文（Base64 PKCS#8 DER 或 PEM）。
            privateKeyFile: 私钥文件路径。
            timeout: 默认请求超时（秒），默认 25。
            signature: 可选，注入已构造的签名器。
            customTransport: 可选，注入自定义传输层（离线测试或自定义代理）。

        Returns:
            无。

        Raises:
            MoomooOpenAPIException: 凭据缺失或非法时抛出。
        """
        self._transport = MoomooOpenApiTransport(
            appKeyId=appKeyId,
            privateKeyText=privateKeyText,
            privateKeyFile=privateKeyFile,
            timeout=timeout,
            signature=signature,
            customTransport=customTransport,
        )

    # ------------------------------------------------------------------
    # 传输层属性转发
    # ------------------------------------------------------------------

    @property
    def transport(self) -> MoomooOpenApiTransport:
        """底层传输层实例（只读）。"""
        return self._transport

    @property
    def appKeyId(self) -> str:
        """当前 AppKey ID。"""
        return self._transport.appKeyId

    @property
    def timeout(self) -> int:
        """默认请求超时（秒）。"""
        return self._transport.timeout

    @property
    def signature(self) -> FutuOpenApiSignature:
        """当前签名器实例（只读）。"""
        return self._transport.signature

    @property
    def algorithm(self) -> str:
        """当前签名算法标识（``Ed25519`` 或 ``RSA-SHA256``）。"""
        return self._transport.algorithm

    @property
    def lastSignatureSource(self) -> Optional[str]:
        """最近一次成功签名所用的原文（调试用）。"""
        return self._transport.lastSignatureSource

    def derivePublicKeyBase64(self) -> str:
        """派生公钥（SPKI DER 的 Base64），用于与 dashboard 上传的公钥比对。

        Returns:
            Base64 编码公钥。

        Raises:
            MoomooOpenAPIException: 私钥结构非法导致派生失败时抛出。
        """
        return self._transport.derivePublicKeyBase64()

    # ------------------------------------------------------------------
    # 通用底层请求
    # ------------------------------------------------------------------

    def requestApi(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        timestampMs: Optional[int] = None,
        nonce: Optional[str] = None,
        timeout: Optional[int] = None,
        plainPayload: bool = False,
    ) -> Any:
        """通用底层方法：签名 → HTTP 请求 → 响应解析。

        Args:
            path: 接口路径（以 "/" 开头）。
            method: HTTP 方法，GET 或 POST。
            params: GET 查询参数字典（值会被字符串化）。
            body: POST 请求体（dict，序列化为紧凑 JSON）。
            timestampMs: 可选，指定毫秒时间戳（默认当前时间）。
            nonce: 可选，指定 X-Nonce（默认随机生成）。
            timeout: 可选，覆盖实例级超时（秒）。
            plainPayload: 响应是否为裸数据（无 ret_code / data 包裹）。

        Returns:
            响应 JSON 的 ``data`` 字段。

        Raises:
            MoomooOpenAPIException: 签名、网络、解析或业务错误时抛出。
        """
        return self._transport.requestApi(
            path,
            method=method,
            params=params,
            body=body,
            timestampMs=timestampMs,
            nonce=nonce,
            timeout=timeout,
            plainPayload=plainPayload,
        )

    # ------------------------------------------------------------------
    # 历史 K 线：GET /api/v1.0/quote/{symbol}/history-kline
    # ------------------------------------------------------------------

    def getHistoryKline(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        ktype: str = KTYPE_MIN,
        autype: str = AUTYPE_FORWARD,
        extendedTime: str = EXTENDED_TIME_ALL,
        num: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取历史 K 线原始响应（详见 ``QuoteHistoryKline.getHistoryKline``）。"""
        return _getHistoryKline(
            self._transport,
            symbol=symbol,
            start=start,
            end=end,
            ktype=ktype,
            autype=autype,
            extendedTime=extendedTime,
            num=num,
            timeout=timeout,
        )

    def fetchHistoryKline(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        ktype: str = KTYPE_MIN,
        autype: str = AUTYPE_FORWARD,
        extendedTime: str = EXTENDED_TIME_ALL,
        num: Optional[int] = None,
        dropEmpty: bool = True,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取历史 K 线并转换为 ``KlineBar``（详见 ``QuoteHistoryKline.fetchHistoryKline``）。"""
        return _fetchHistoryKline(
            self._transport,
            symbol=symbol,
            start=start,
            end=end,
            ktype=ktype,
            autype=autype,
            extendedTime=extendedTime,
            num=num,
            dropEmpty=dropEmpty,
            timeout=timeout,
        )

    def fetchHistoryKlineFullDay(
        self,
        symbol: str,
        day: str,
        ktype: str = KTYPE_MIN,
        autype: str = AUTYPE_FORWARD,
        dropEmpty: bool = True,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取单日全时段 K 线并转换为 ``KlineBar``
        （详见 ``QuoteHistoryKline.fetchHistoryKlineFullDay``）。"""
        return _fetchHistoryKlineFullDay(
            self._transport,
            symbol=symbol,
            day=day,
            ktype=ktype,
            autype=autype,
            dropEmpty=dropEmpty,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # 交易日历：GET /api/v1.0/quote/trading-days
    # ------------------------------------------------------------------

    def getTradingDays(
        self,
        market: str,
        start: str,
        end: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取交易日历原始响应（详见 ``QuoteTradingDays.getTradingDays``）。"""
        return _getTradingDays(
            self._transport, market=market, start=start, end=end, timeout=timeout
        )

    def fetchTradingDays(
        self,
        market: str,
        start: str,
        end: str,
        halfDaysOnly: bool = False,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取交易日历并转换为 ``TradingDay``（详见 ``QuoteTradingDays.fetchTradingDays``）。"""
        return _fetchTradingDays(
            self._transport,
            market=market,
            start=start,
            end=end,
            halfDaysOnly=halfDaysOnly,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # 标的静态档案：POST /api/v1.0/quote/stock-basicinfo
    # ------------------------------------------------------------------

    def postStockBasicInfo(
        self,
        codeList: List[str],
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """批量获取标的静态档案原始响应（详见 ``QuoteStockBasicInfo.postStockBasicInfo``）。"""
        return _postStockBasicInfo(self._transport, codeList, timeout=timeout)

    def fetchStockBasicInfo(
        self,
        codeList: List[str],
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """批量获取标的静态档案并转换为 ``StockBasicInfo``
        （详见 ``QuoteStockBasicInfo.fetchStockBasicInfo``）。"""
        return _fetchStockBasicInfo(self._transport, codeList, timeout=timeout)

    # ------------------------------------------------------------------
    # 服务端时间：GET /api/v1.0/server-time
    # ------------------------------------------------------------------

    def getServerTime(self, timeout: Optional[int] = None) -> int:
        """获取服务端毫秒时间戳（详见 ``QuoteServerTime.getServerTime``）。"""
        return _getServerTime(self._transport, timeout=timeout)

    def fetchServerDrift(self, timeout: Optional[int] = None) -> Tuple[int, int, int]:
        """比对本地与服务端时间偏移（详见 ``QuoteServerTime.fetchServerDrift``）。"""
        return _fetchServerDrift(self._transport, timeout=timeout)


def createMoomooOpenAPIClient(
    appKeyId: Optional[str] = None,
    privateKeyText: Optional[str] = None,
    privateKeyFile: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    signature: Optional[FutuOpenApiSignature] = None,
    customTransport: Optional[TransportType] = None,
) -> MoomooOpenAPIClient:
    """工厂函数：创建 MoomooOpenAPIClient 客户端实例。

    凭据可在调用时传入，也可预先设置环境变量
    ``MOOMOO_OPENAPI_AK`` + ``MOOMOO_OPENAPI_SK``（或 ``MOOMOO_OPENAPI_SK_FILE``）。

    Args:
        appKeyId: AppKey ID（请求头 X-Api-Key）。
        privateKeyText: 私钥原文（Base64 PKCS#8 DER 或 PEM）。
        privateKeyFile: 私钥文件路径。
        timeout: 默认请求超时（秒），默认 25。
        signature: 可选，注入已构造的签名器。
        customTransport: 可选，注入自定义传输层（离线测试或自定义代理）。

    Returns:
        MoomooOpenAPIClient 实例。

    Raises:
        MoomooOpenAPIException: 凭据缺失或非法时抛出。
    """
    return MoomooOpenAPIClient(
        appKeyId=appKeyId,
        privateKeyText=privateKeyText,
        privateKeyFile=privateKeyFile,
        timeout=timeout,
        signature=signature,
        customTransport=customTransport,
    )


# ---------------------------------------------------------------------------
# 别名映射（原内联段的收尾处理）
# ---------------------------------------------------------------------------
# ClientFacade 原本以 ``from .QuoteHistoryKline import getHistoryKline as _getHistoryKline``
# 这类形式导入同包函数；本库合成单文件后不再有子模块，故在此补上等价别名，
# 使门面代码无需改写即可工作。**属内部实现，不应被外部依赖。**
_fetchHistoryKline = fetchHistoryKline
_fetchHistoryKlineFullDay = fetchHistoryKlineFullDay
_getHistoryKline = getHistoryKline
_fetchServerDrift = fetchServerDrift
_getServerTime = getServerTime
_fetchStockBasicInfo = fetchStockBasicInfo
_postStockBasicInfo = postStockBasicInfo
_fetchTradingDays = fetchTradingDays
_getTradingDays = getTradingDays
