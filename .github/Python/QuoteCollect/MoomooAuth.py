# -*- coding: utf-8 -*-
"""moomoo OpenAPI 认证：请求签名与纯标准库密码学实现。

从 `MoomooQuoteClient.py` 提取，是本目录内**唯一**与请求签名相关的模块。
若某处只需要 moomoo 的请求签名（而不做行情取数），可直接复用本模块，
无需引入行情客户端。

对外用途：
    createFutuOpenApiSignature(...)   构造签名器（凭据由参数或环境变量提供）
    FutuOpenApiSignature              签名器：原文构造与请求头装配
    FutuOpenApiSignatureError         签名相关错误

内部实现（Ed25519 / RSA-SHA256 / DER 编解码及其常量）不属对外契约，
以 `_` 前缀标记；依赖仅为 Python 标准库。
"""

import base64
import hashlib
import json
import os
import re
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

class PureCryptoError(Exception):
    """纯标准库密码学模块的异常。

    覆盖：私钥结构非法、密钥长度不足、消息过长等无法完成签名的情形。
    """


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


#: SHA-256 的 DigestInfo DER 前缀（PKCS#1 v1.5 固定值）
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


#: 支持的签名算法标识
ALGORITHM_ED25519 = "Ed25519"


ALGORITHM_RSA_SHA256 = "RSA-SHA256"


#: 支持的签名算法集合
SUPPORTED_ALGORITHMS = (ALGORITHM_ED25519, ALGORITHM_RSA_SHA256)


#: Ed25519 算法 OID（1.3.101.112）的 DER 编码
ED25519_OID_DER = bytes.fromhex("06032b6570")


#: Ed25519 PKCS#8 DER 前缀：
#: SEQUENCE(46) + version(0) + AlgorithmIdentifier(OID 1.3.101.112) + OCTET STRING(32 字节种子)
ED25519_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


#: Ed25519 SPKI DER 前缀（公钥派生用）：
#: SEQUENCE(42) + AlgorithmIdentifier(OID 1.3.101.112) + BIT STRING(32 字节公钥)
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


#: RSA 算法 OID（1.2.840.113549.1.1.1）的 DER 编码
RSA_OID_DER = bytes.fromhex("06092a864886f70d010101")


#: X-Nonce 允许字符集（官方：仅字母、数字、下划线、连字符）
NONCE_ALPHABET = string.ascii_letters + string.digits + "_-"


#: 默认 X-Nonce 长度
DEFAULT_NONCE_LENGTH = 32


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

