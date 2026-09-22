#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高熵长字符串扫描（L2）—— 发现不匹配已知格式的自研系统密钥。

与 `Scan.ps1`（L1 已知格式）互补：L1 只能认"标准形状"的密钥；自研系统的 token
往往没有固定前缀，只能靠**长度 + 熵**识别。

用法：
    python3 EntropyScan.py <仓库根> [--min-length 24] [--max-preview 60]

安全设计（本脚本自身踩过坑，勿简化）：
    1. **输出一律截断**（默认 60 字符）。扫描工具绝不能完整回显疑似凭据——否则工具输出、
       日志、CI artifact 自身就成为新的泄露面。
    2. **必须过滤噪声**：分隔线、路径、URL、纯标识符、文件后缀形态。噪声一多，真命中会被
       淹掉——"报告太长没人看"等同于漏报。
    3. **跳过纯注释行**：注释里的说明文字（如 "``XXX_SK`` 的存储形态"）不是凭据。

注意：本层是**启发式**，命中 ≠ 泄密。逐条人工判断，重点关注赋值语句右侧与
`key=`/`token=`/`secret=` 之后的长串。

退出码：0 = 无候选；1 = 有候选（需人工判断）；2 = 参数错误。
"""

import argparse
import re
import sys
from pathlib import Path

#: 参与扫描的文本类后缀（含无后缀文件，如 .env）
TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".json", ".jsonl", ".example", ".txt",
                 ".ini", ".cfg", ".conf", ".sh", ".ps1", ".bat", ".cmd", ".toml", ""}

#: 跳过的目录
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

#: 候选：长度达标的连续 token（含 base64 / JWT 字符集）
CANDIDATE = re.compile(r"[A-Za-z0-9_\-+/=]{12,}")

#: 噪声过滤：明显不是凭据的形态
NOISE_PATTERNS = (
    # 短纯 hex（git 短 oid、CRC 等）；**长 hex 不放行不了** —— 见 isHexEncodedKey
    re.compile(r"^[0-9a-fA-F]{1,20}$"),
    re.compile(r"^[A-Za-z_]+$"),                  # 纯字母：标识符
    re.compile(r"^https?$|^www$", re.I),
    re.compile(r"^[-=*_#~]{4,}$"),                # 分隔线
    re.compile(r"^[A-Za-z0-9_\-]+/[A-Za-z0-9_\-/\.]+$"),  # 路径形态（含 /）
    re.compile(r"^[/:][A-Za-z0-9_\-/\.]+$"),      # 以 / 或 : 开头的路径/端点片段
    re.compile(r"^\d+$"),                         # 纯数字
    re.compile(r"^[A-Z][A-Z0-9_]{3,}$"),          # 常量名：ENV_APP_AK 这类
    re.compile(r"^(application|text|image|multipart)/", re.I),   # MIME
    re.compile(r"^[a-z]+(\.[a-z]+)+$"),           # 域名/包名
)

#: 长数字块：标识符里的版本号/年份（`ed25519`、`20260921`、`sha256`）。
#: 仅用于**排序降权**，不用于过滤 —— 见文件末「设计权衡」说明。
LONG_DIGIT_RUN = re.compile(r"\d{4,}")

#: 已知密钥前缀（仅用于**排序加权**：命中则更可疑）
KEY_PREFIX = re.compile(
    r"^(sk|pk|rk|ak|tk|tok|pat|api|key|sec|secret|bearer|oauth|gh[pousr]|sbp|"
    r"glpat|xox[baprs]|sk_live|sk_test|pk_live|pk_test)[_\-]",
    re.I)


def isHexEncodedKey(token: str) -> bool:
    """长纯 hex 是否可能**就是密钥**（hex 编码的密钥极常见，不能一律当噪声）

    判据取长度与熵而非"是否 hex"：

    - 长度 ≥ 32 且熵 ≥ 3.5 → 视为密钥（256 位 hex = 64 字符；128 位 = 32 字符）；
    - 更短的 hex（git 短 oid、CID、CRC）仍按噪声处理。

    这一条来自反向验证：最初把所有纯 hex 一律当噪声，导致 **hex 编码的真实密钥被误拦**
    （假阴性远比噪声危险 —— 假阴性意味着"扫了但没看见"）。
    """
    if not re.fullmatch(r"[0-9a-fA-F]+", token):
        return False
    return len(token) >= 32 and entropy(token) >= 3.5

#: 命中"像密钥"的关键词上下文（出现则提高可信度，用于排序）
KEYWORD_HINT = re.compile(r"key|token|secret|passwd|password|credential|apikey|"
                          r"private|auth|sign", re.I)

#: 赋值/关键字实参的等号（排除 == / != / <= / >= 这类比较运算）
ASSIGN_EQ = re.compile(r"(?<![=!<>])=(?!=)")


def isAssignmentTarget(line: str, start: int) -> bool:
    """判断该位置的 token 是否紧跟在赋值等号左侧（即它是**被赋值方**，不是密钥值）

    实测动机：`privateKeyText=privateKeyText`、`api_base=DEFAULT_API_BASE` 这类
    「形参名/变量名 = 同名实参」会被高熵扫描大面积命中，而它们显然不是凭据。
    """
    prefix = line[:start].rstrip()
    if not prefix.endswith("="):
        return False
    # `==` / `!=` / `<=` / `>=` 是比较运算，不是赋值
    return not prefix.endswith(("==", "!=", "<=", ">="))


def hasDigit(token: str) -> bool:
    """候选是否含数字

    真实的随机密钥/令牌几乎必然含数字；而函数名、参数名、标识符通常不含。
    这条判据把噪声降了一个数量级。
    """
    return any(ch.isdigit() for ch in token)


def identifierScore(token: str) -> float:
    """候选「像标识符」的程度（0~1）——**仅用于降权排序，绝不用于过滤**

    ⚠️ 这是本脚本最重要的一条设计决定，来自实测教训：
        任何"版式启发式"（连续大写、camelCase 小写块长度、下划线分段同构…）在做
        **二值过滤**时都会持续产生假阴性 —— 实测把 camelCase 阈值调松会放行标识符噪声，
        调紧则随机密钥的误拦率升到 25%（自检 5 次出现 1 次失败）。
        随机密钥里出现这些"标识符特征"的概率并不低，而假阴性（扫了但没看见）远比
        噪声危险。**故此类判据一律只降权、不过滤。**

    :return: 0 = 完全不像标识符；越大越像
    """
    # 候选提取会把 `algorithm=ALGORITHM_ED25519` 整体当一个 token（等号在字符集内），
    # 此处剥掉 `名字=` 前缀，否则会把标识符误算成 0 分而排到报告最前面。
    core = token.rsplit("=", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_]+", core):
        return 0.0                       # 含 +/./- 等 ⇒ 更像编码后的密钥
    core = core.strip("_")
    letters = [ch for ch in core if ch.isalpha()]
    if not letters:
        return 0.5
    if all(ch.isupper() for ch in letters):
        return 1.0                       # 全大写常量名：ALGORITHM_ED25519
    upperRatio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    score = 0.0
    if upperRatio > 0.65:
        score += 0.6                     # camelCase/PascalCase 的典型特征
    if re.search(r"[A-Z]{2,}[a-z]", core):
        score += 0.3                     # 大写块后接小写：PublicKey / MigrationFile
    if LONG_DIGIT_RUN.search(core):
        score += 0.2                     # 版本号 / 年份
    return min(score, 1.0)


def looksLikeNoise(token: str) -> bool:
    """判断候选是否为噪声（不是凭据）

    例外：长纯 hex 可能是 **hex 编码的密钥**，由 `isHexEncodedKey` 放行。
    """
    if isHexEncodedKey(token):
        return False
    return any(p.match(token) for p in NOISE_PATTERNS)


def entropy(token: str) -> float:
    """字符分布熵（越高越像随机密钥）"""
    if not token:
        return 0.0
    import math
    counts = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(token)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def iterTextFiles(root: Path):
    """遍历可扫描的文本文件"""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
        except OSError:
            continue
        yield path


def scan(root: Path, minLength: int):
    """扫描并返回 {相对路径: [(行号, 候选, 熵, 关键词命中, 标识符分)]}

    **过滤链刻意保持最小**（长度 → 噪声形态 → 含数字），只保留不会产生假阴性的判据：
    长度与含数字是"随机密钥几乎必然满足"的必要条件，噪声形态针对的是明确非凭据的形态。
    至于"像不像标识符"，一律只记录分数用于排序（见 `identifierScore` 的说明）。
    """
    found = {}
    for path in iterTextFiles(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*", "<!--")):
                continue                      # 纯注释行：说明文字不是凭据
            for match in CANDIDATE.finditer(line):
                token = match.group(0)
                if len(token) < minLength or looksLikeNoise(token):
                    continue
                if isAssignmentTarget(line, match.start()):
                    continue                  # 它是被赋值方（形参名/变量名），不是密钥值
                if not hasDigit(token):
                    continue                  # 真实密钥几乎必含数字；纯字母多为标识符
                # 熵过低（如 aaaaaaaa...）不作为候选
                if entropy(token) < 3.0:
                    continue
                found.setdefault(str(path.relative_to(root)), []).append(
                    (lineno, token, entropy(token), bool(KEYWORD_HINT.search(line)),
                     identifierScore(token)))
    return found


def main():
    parser = argparse.ArgumentParser(description="高熵长字符串扫描（L2）")
    parser.add_argument("root", nargs="?", default=".", help="仓库根目录")
    parser.add_argument("--min-length", type=int, default=24, help="候选最小长度（默认 24）")
    parser.add_argument("--max-preview", type=int, default=60, help="回显截断长度（默认 60）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print("❌ 目录不存在：%s" % root)
        return 2

    found = scan(root, args.min_length)
    if not found:
        print("✅ 未发现高熵长字符串候选（min-length=%d）" % args.min_length)
        return 0

    # 疑似度排序（降权而非过滤）：
    #   1) 命中**已知密钥前缀**（`sk_live_`、`ghp_`、`sbp_`…）→ 最可疑；
    #   2) 标识符分**低**者优先（随机串 ~0.2；camelCase/常量名 ≥0.6 → 排到后部）；
    #   3) 熵与长度作为次级依据。
    # ⚠️ 刻意**不用**「行内含 key/token 关键词」作首要依据：函数名里的 `key`
    #    （`ed25519PublicKeyFromSeed`）会把纯标识符顶到报告最前面，实测造成误导。
    rows = []
    for name, items in found.items():
        for lineno, token, ent, hinted, ident in items:
            prefixHit = bool(KEY_PREFIX.match(token.strip("_")))
            rows.append((prefixHit, ident, hinted, ent, len(token), name, lineno, token))
    rows.sort(key=lambda r: (r[0], -r[1], r[2], r[3], r[4]), reverse=True)

    suspicious = sum(1 for r in rows if r[0] or r[1] < 0.4)
    print("⚠️ 发现 %d 处候选（启发式，命中 ≠ 泄密；按疑似度降序，前置 %d 处较可疑）\n"
          % (len(rows), suspicious))
    print("  %-6s %-6s %-5s %s" % ("疑似", "标识符分", "熵", "位置与片段（截断）"))
    for prefixHit, ident, hinted, ent, length, name, lineno, token in rows:
        mark = "★高" if prefixHit else ("中" if ident < 0.4 else "低")
        preview = token if len(token) <= args.max_preview else token[:args.max_preview] + "…"
        print("  %-6s %-6.2f %-5.2f %s:%d" % (mark, ident, ent, name, lineno))
        print("         %s" % preview)
    print("\n人工判断要点：")
    print("  1) 该行是否为「赋值右侧」或 key=/token=/secret= 之后的长串？")
    print("  2) 是否为文档示例、测试夹具、算法常量（如 DER 前缀、魔术数）？")
    print("  3) 标识符分高（≥0.6）= 更像 camelCase 常量/标识符，可优先略过；")
    print("     但**不要**仅凭此判定安全 —— 本脚本刻意不按它过滤，以免漏掉真实密钥。")
    print("  4) 若确认为真实凭据：先轮换该凭据，再清理代码，并检查 git 历史（Scan.ps1 -History）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
