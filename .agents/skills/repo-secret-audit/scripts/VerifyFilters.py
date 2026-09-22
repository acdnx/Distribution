#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 过滤规则的自检：**真实密钥零误拦**（唯一硬性要求）+ 伪命中降权效果。

为什么需要这个自检：
    高熵扫描器有一个比其他工具更危险的失败模式 —— **假阴性**。
    为提高信噪比而加过滤规则时，极易顺手把真实密钥也滤掉，而"扫了但没看见"
    远比"扫出一堆噪声"严重。本脚本用随机生成的**真实密钥样本**把过滤链钉住。

设计决定（实测得出）：
    过滤链只保留**不会产生假阴性**的判据 —— 长度、噪声形态、含数字、熵。
    至于"像不像标识符"（全大写常量名、camelCase 版式、版本号数字块），
    一律**只降权排序、不做过滤**：实测这类版式启发式在二值过滤下会持续误拦随机密钥
    （camelCase 阈值调紧后自检 5 次里出现 1 次、误拦率 25%）。

    因此本脚本的**退出码只由假阴性决定**；伪命中（噪声）只报告、不判失败 ——
    否则会诱使后续维护者继续收紧规则，把假阴性重新引回来。

用法：
    python3 VerifyFilters.py       # 无假阴性 → 0；出现假阴性 → 1
"""

import base64
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import EntropyScan as ES  # noqa: E402

MIN_LENGTH = 24


def realSamples():
    """生成形态各异的**真实密钥**样本（随机生成，避免样本本身成为凭据）"""
    return {
        "base64 随机": base64.b64encode(secrets.token_bytes(32)).decode(),
        "base64url 随机": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
        "hex 随机(64)": secrets.token_hex(32),
        "大小写混合+数字": "".join(secrets.choice(string.ascii_letters + string.digits)
                                   for _ in range(32)),
        "Supabase 风格": "sbp_" + secrets.token_hex(20),
        "GitHub PAT 风格": "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits)
                                            for _ in range(36)),
        "下划线分段式": "sk_live_" + secrets.token_hex(12) + "_" + secrets.token_hex(4),
        "JWT 段": base64.urlsafe_b64encode(secrets.token_bytes(40)).decode().rstrip("="),
    }


#: 伪命中样本：来自真实仓库扫描中出现的噪声（用于观察降权效果）
FAKE_SAMPLES = (
    "ed25519PublicKeyFromSeed", "ALGORITHM_ED25519", "ALGORITHM_RSA_SHA256",
    "DEFAULT_API_BASE", "include_system_vars", "privateKeyText",
    "US_COMEX_GCMain_Day_FTMM_Mon_20260921", "UploadFileList_20260909_143000123_abc",
    "classifyTradingSession", "ExportArchiveMvsvInline",
    "JobCollectExportMvsvThenArchive", "DMDCBWD31MigrationFileQuote",
)

#: 期望被降权的伪命中（identifierScore ≥ 该阈值即视为"已排到报告后部"）
DEMOTE_THRESHOLD = 0.6


def survives(token, minLength):
    """复刻 EntropyScan 的**过滤链**（不含降权项）：返回是否会被作为候选保留"""
    if len(token) < minLength:
        return False
    if ES.looksLikeNoise(token):
        return False
    if not ES.hasDigit(token):
        return False
    return ES.entropy(token) >= 3.0


def main():
    print("=== 真实密钥样本（硬性要求：全部放行）===")
    missed = []
    for name, token in realSamples().items():
        ok = survives(token, MIN_LENGTH)
        if not ok:
            missed.append(name)
        print("  %-18s 长度%-3d 熵%.2f → %s"
              % (name, len(token), ES.entropy(token), "✅放行" if ok else "❌被误拦"))

    print("\n=== 伪命中样本（观察降权，不判失败）===")
    noisy = []
    for token in FAKE_SAMPLES:
        kept = survives(token, MIN_LENGTH)
        score = ES.identifierScore(token)
        if not kept:
            print("  %-42s → ✅过滤链拦截" % token[:42])
        elif score >= DEMOTE_THRESHOLD:
            print("  %-42s → ✅保留但降权（标识符分 %.2f）" % (token[:42], score))
        else:
            noisy.append(token)
            print("  %-42s → ⚠️保留且未降权（标识符分 %.2f）" % (token[:42], score))

    print("\n=== 结论 ===")
    print("  真实密钥误拦（假阴性，唯一失败条件）: %d / %d"
          % (len(missed), len(realSamples())))
    print("  未降权的噪声（仅提示，可人工略过）  : %d / %d" % (len(noisy), len(FAKE_SAMPLES)))
    if missed:
        print("\n  ❌ 假阴性：%s —— 过滤链过严，真实密钥会被漏掉！" % "、".join(missed))
        print("     修复方向：放宽对应判据；或确认该形态由 L1 层（Scan.ps1）捕获。")
        return 1
    if noisy:
        print("\n  ℹ️ 以下噪声未被降权：%s" % "、".join(noisy))
        print("     **不要**为此收紧过滤链（会引入假阴性）；如确需处理，请改为增加降权特征。")
    print("\n  ✅ 无假阴性 —— 过滤链满足安全底线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
