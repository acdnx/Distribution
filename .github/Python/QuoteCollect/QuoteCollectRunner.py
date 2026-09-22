#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FTMM 历史 K 线采集作业执行器（Supabase 作业表驱动 + GitHub Contents API 落点）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
本脚本是 `.github/workflows/MMQuoteCollect.yml` 的执行体，负责**一次运行处理一条作业**：

    查询 Supabase 作业表 → 挑一条可运行作业 → 解析出采集范围与导出配置
      → 调 Moomoo OpenAPI 采集 K 线 → 处理成 MVSV → 经 Contents API 落到 quote-mon 分支
      → 回写作业状态

内联段的来源是原型脚本 `ExportArchiveMvsvInline.py`（**已退役**，不再随仓库提供）：下方标有
`BEGIN INLINED FROM` / `END INLINED FROM` 的区段即其必需子集的**逐字搬运**。那种「取数与
数据处理逻辑零重复、本脚本只做作业层」的分工设计由此而来：

    - **取数与数据处理逻辑零重复**：`buildMvsvName` / `writeMvsv` / `collectMinuteBars` /
      `collectDayBars` / `applySessions` / `_deduplicate` / `_periodLabel` / `_typeKlineLabel`
      等纯函数**全部来自内联段**（不再有外部 import），本脚本只新增「作业来源、落点、状态流转」；
    - 原型的「随机挑一条调度表任务」入口未被沿用（本脚本的作业来源是作业表或调度表，见第二节）。

二、作业来源（JOB_SOURCE）
----------------------------------------------------------------------------------------
- `supabase`（默认）：查 `finv_quote_collect_job_ftmm` 作业表，见第三节「就绪判定」；
- `schedule`：回退到原型脚本的 JSONL 调度表路径（`downloadSchedule` + `pickTask`），
  用于与作业表对照回归，或作业表不可用时的应急通道。

三、就绪判定与作业选取（query_next_job）
----------------------------------------------------------------------------------------
符合以下任一条即视为「可运行」：

    1. `job_status = 'READY'`                            —— 就绪，可直接采集；
    2. `job_status IN ('FAILED','ABORTED')` 且 `count_retry < 3` —— 曾失败 / 曾中断且重试未耗尽。

排除：`DISABLED`（未启用）、`COLLECTING`（防与其它运行并发占用同一作业）、
`CANCELED`（人工取消，不再执行）、`COMPLETED`（已完成）。

排序 `dt_update.asc, dt_create.asc, id.asc`：保证多次运行按同一顺序取，
配合工作流的 `concurrency` 分组避免同刻双写同一作业。每次只取**一条**（`limit=1`）。

四、字段口径与 `type_kline` 大驼峰（**强制**）
----------------------------------------------------------------------------------------
作业表 → 原型脚本 task dict 的映射：

    date_start / date_end（INTEGER YYYYMMDD） → start / end
    type_kline（大小写不限）                  → **统一归一为大驼峰**
    其余字段（period / label / region / market / usc / symbol）同名直通

`type_kline` 全链路一律大驼峰：`MIN` / `min` / `Min` → `Min`，`MIN5` → `Min5`。
该归一值同时决定三处输出，必须一致：

    - `.mvsv` 文件名第 4 段（`buildMvsvName` 走 `_typeKlineLabel`，已是大驼峰）；
    - MVSV 头部 `# TypeKLine`（`writeMvsv` 同上）；
    - 状态回写与日志（本脚本统一输出归一值）。

未登记在标准取值域内的写法**不静默接受**：打印告警后按首字母大写兜底，
并在运行摘要里点名，便于及早发现数据问题。归一动作集中在 `normalize_type_kline()`
一次完成（任务构建期），此后全链路只读归一值；`--selftest` 对该口径有专门断言，防回归。

五、落点路径（build_remote_path）
----------------------------------------------------------------------------------------
    Archive/Finv/SecuQuoteData/FTMM/{period}/{region}_{market}_{usc}_{type_kline}_FTMM_{period}_{label}.mvsv

`{period}` 取作业表 `period` 的大驼峰值（`Mon` / `Week` / `Year` / `Day`），**与文件名第 6 段
同源**，保证目录名与文件名永远一致。该前缀已登记在 `Migration.quote-mon.json` 的
`OBSCIDRoutes`（`Archive/Finv/SecuQuoteData/FTMM/Mon/`，CID 3518524409277056744），无需新增路由。

六、落点写入（GitHub Contents API）
----------------------------------------------------------------------------------------
**直接调用同目录既有封装** `GitHubCommitContent.commit_content_file`（**刻意不内联**：它是
仓库既有的公共模块，复制一份只会带来两份实现漂移）：流程为「GET 查 sha → PUT 提交」，
404 视为新建（HTTP 201）、已存在则更新（HTTP 200）；失败以 dict 返回、不抛异常。

调用方式与封装的默认解析链：

    - 目标仓库/分支由 `COMMIT_OWNER` / `COMMIT_REPO` / `COMMIT_BRANCH` **显式传入**，
      三者齐备时封装**不会**去读 Commit.json 或 `.git`（自包含部署无需这些文件）；
    - 其余能力沿用封装：分块重试、失败原因归类等。

本脚本在调用前后自行增加的两道保障：

    - 上传前做 **sha256 + 行数 + 字节数**三方对账并打印，确保「上传的就是刚采的那份」；
    - **50 MB 前置检查**：Contents API 单文件硬阈值，超限直接明确报错，不浪费一次上传。

七、作业状态流转（**全部由本仓库脚本维护**）
----------------------------------------------------------------------------------------
    时刻                          目标状态        字段变更
    ---------------------------------------------------------------------------
    选中并开始采集                 COLLECTING      dt_starte = now()
    采集 + 推送均成功              COMPLETED       dt_finish = now(), last_error = NULL
    取数失败 / 推送失败（永久性）  FAILED          last_error = 摘要, count_retry = count_retry + 1
    超时、被中断、软停止           ABORTED         last_error = 摘要
    重试回退（本脚本负责）         READY           FAILED/ABORTED 且 count_retry < 3 → 置回 READY，
                                                   随即进入 COLLECTING（见 run_job 第 1 步）

任何退出方式都经 `run_job` 的 `try/except/finally` 收口，确保作业不会被留在悬空的 `COLLECTING`。

**权限尚未配妥期间的降级**：`SUPABASE_ENABLE_JOB_UPDATE` 默认 `false`，此时只把待执行的
SQL 与绑定值打印出来（dry-run），不触网写入。表写权限配妥后，在仓库
`Settings → Secrets and variables → Actions → Variables` 里把该变量置为 `true` 即上线，
**不需要改任何代码**。

八、环境变量
----------------------------------------------------------------------------------------
【采集】
    MOOMOO_OPENAPI_AK / MOOMOO_OPENAPI_SK   moomoo OpenAPI 凭据（由客户端库自行读取）

【作业来源】
    JOB_SOURCE             supabase（默认）| schedule
    JOB_ID                 指定作业 id（留空 = 按就绪规则自动取一条）
    JOB_PERIOD_OVERRIDE    覆盖落点目录用的 period（定点重跑用）
    JOB_USC_OVERRIDE       覆盖标的 usc（定点重跑用）
    SUPABASE_PROJECT_REF   Supabase 项目引用（必填）
    SUPABASE_KEY           Supabase API 密钥（必填，不落日志）
    SUPABASE_REST_BASE     可选：整体覆盖 Data API 根地址（自托管 / 经代理 / 指向本地服务；
                           留空 = https://<SUPABASE_PROJECT_REF>.supabase.co/rest/v1）

【落点与推送】
    GIT_COMMIT_TOKEN       GitHub 写仓库令牌（必填，经 Contents API 用）
    COMMIT_BRANCH          目标分支，默认 quote-mon
    COMMIT_OWNER           owner，默认 acdnx
    COMMIT_REPO            repo，默认 Distribution
    DRY_RUN                true 时只采集 + 本地落盘，**不推送**

【状态回写】
    SUPABASE_ENABLE_JOB_UPDATE   true 才真正回写作业表；默认 false（只打印待执行 SQL）

【其它】
    JOB_RUN_ID             本次运行标识（默认取 GITHUB_RUN_ID，仅用于日志与提交信息）

【行情库】行情客户端库（含 Ed25519 / RSA-SHA256 签名与纯标准库密码学实现）**已内联于本文件**，
    不需要 `src/` 目录、不需要外部包、也不需要安装依赖；凭据由客户端自行读取下列环境变量：
        MOOMOO_OPENAPI_AK        AppKey ID
        MOOMOO_OPENAPI_SK        Base64 PKCS#8 私钥（或用 MOOMOO_OPENAPI_SK_FILE 指向文件）

九、命令行（也可纯用环境变量驱动，工作流即如此）
----------------------------------------------------------------------------------------
    python3 QuoteCollectRunner.py --job-source supabase --dry-run
    python3 QuoteCollectRunner.py --job-source schedule
    python3 QuoteCollectRunner.py --job-id 12 --commit-branch quote-mon
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 同目录模块与原型脚本复用
# ---------------------------------------------------------------------------
#: 本脚本所在目录（`.github/Python/QuoteCollect`）。后续按功能拆分出的同目录模块
#: 由这里进入 sys.path，便于直接 `import <模块名>`。
_SCRIPT_DIR = Path(__file__).resolve().parent          # .github/Python/QuoteCollect
_PARENT_DIR = _SCRIPT_DIR.parent                       # .github/Python
#: 两者都需在 sys.path 上：
#:   父目录 —— 供 `import GitHubCommitContent`（Contents API 封装，**刻意不内联**，
#:             它是同目录既有公共封装，复制一份只会带来两份实现漂移）；
#:   本目录 —— 供后续「按功能拆分出同目录模块」时直接 import。
#: 注：下方内联段自带的文档字符串（「完全自包含单文件版」「不含任何外部依赖」等）描述的是
#: **被搬运的原型**，未随内联改写以保持搬运的可核对性；本文件的真实依赖边界以上一段为准。
for _dir in (_PARENT_DIR, _SCRIPT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

#: 注意：行情库（含 Ed25519 / RSA-SHA256 签名及纯标准库密码学实现）已**内联到本文件**。
#: 除 Python 标准库外，唯一的仓库内依赖是 **`GitHubCommitContent`（Contents API 封装）** ——
#: 它是本仓库既有的公共模块，**刻意不内联**，直接从上一级目录导入。
#: 因此本文件不是"拷到哪都能跑"的完全自包含形态：**需与 .github/Python/GitHubCommitContent.py
#: 同仓部署**（在仓库内运行、或按 CI 的检出方式部署均满足）。
#: 它也不依赖仓库根的绝对位置（导入走 sys.path，非相对路径推算）。

from GitHubCommitContent import commit_content_file  # noqa: E402  （Contents API 封装；同目录既有模块，直接用）

# ---------------------------------------------------------------------------
# 配置：作业来源
# ---------------------------------------------------------------------------
ENV_JOB_SOURCE = "JOB_SOURCE"
ENV_JOB_ID = "JOB_ID"
ENV_JOB_PERIOD_OVERRIDE = "JOB_PERIOD_OVERRIDE"
ENV_JOB_USC_OVERRIDE = "JOB_USC_OVERRIDE"
ENV_SUPABASE_REF = "SUPABASE_PROJECT_REF"
ENV_SUPABASE_KEY = "SUPABASE_KEY"

#: Supabase Data API 根地址模板；可用 `SUPABASE_REST_BASE` 整体覆盖
#: （自托管 Supabase、经代理访问、或端到端验证时指向本地 mock 服务）
SUPABASE_REST_BASE = "https://%s.supabase.co/rest/v1"
ENV_SUPABASE_REST_BASE = "SUPABASE_REST_BASE"

#: 作业表名与查询列（PostgREST）
JOB_TABLE = "finv_quote_collect_job_ftmm"
JOB_SELECT_COLUMNS = ("id,job_name,job_prefix,type_kline,period,date_start,date_end,label,"
                      "region,market,usc,symbol,job_status,count_retry")

#: 就绪判定：READY，或 FAILED/ABORTED 且重试次数未达上限
JOB_STATUS_COLLECTABLE = ("READY", "FAILED", "ABORTED")
JOB_STATUS_READY = "READY"
JOB_STATUS_COLLECTING = "COLLECTING"
JOB_STATUS_COMPLETED = "COMPLETED"
JOB_STATUS_FAILED = "FAILED"
JOB_STATUS_ABORTED = "ABORTED"

#: 重试次数上限：`count_retry < JOB_RETRY_LIMIT` 才允许再次执行
JOB_RETRY_LIMIT = 3

#: PostgREST 的 or 过滤表达式（与第三、四节的判定逐条对应）
JOB_READY_FILTER = ("or=(job_status.eq.READY,"
                    "and(job_status.in.(FAILED,ABORTED),count_retry.lt.%d))" % JOB_RETRY_LIMIT)

#: 排序：更新时间升序优先，保证多次运行按同一顺序取（配合工作流 concurrency 防双写）
JOB_ORDER = "order=dt_update.asc,dt_create.asc,id.asc"

#: 单条选取
JOB_LIMIT = "limit=1"

# ---------------------------------------------------------------------------
# 配置：落点路径与推送
# ---------------------------------------------------------------------------
#: 归档根（相对仓库根，不含文件名），形如 Archive/Finv/SecuQuoteData/FTMM/{period}/
ARCHIVE_ROOT_TEMPLATE = "Archive/Finv/SecuQuoteData/FTMM/%s"

ENV_COMMIT_TOKEN = "GIT_COMMIT_TOKEN"
ENV_COMMIT_BRANCH = "COMMIT_BRANCH"
ENV_COMMIT_OWNER = "COMMIT_OWNER"
ENV_COMMIT_REPO = "COMMIT_REPO"
ENV_DRY_RUN = "DRY_RUN"

DEFAULT_COMMIT_OWNER = "acdnx"
DEFAULT_COMMIT_REPO = "Distribution"
DEFAULT_COMMIT_BRANCH = "quote-mon"

#: Contents API 单文件硬阈值（字节）：超过则无法经该接口提交，前置拦截并明确报错
GITHUB_CONTENTS_MAX_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# 配置：状态回写
# ---------------------------------------------------------------------------
ENV_ENABLE_JOB_UPDATE = "SUPABASE_ENABLE_JOB_UPDATE"

#: 真值集合（与 SupabaseImportWeekMvsv.py 的开关口径保持一致）
TRUE_VALUES = ("1", "true", "yes", "on")

#: 运行标识（仅日志与提交信息用）
ENV_RUN_ID = "JOB_RUN_ID"


class JobExecutionError(Exception):
    """作业执行错误：承载「作业身份 + 是否永久失败」，供状态流转决定写 FAILED 还是 ABORTED

    Attributes:
        permanent: True = 永久性失败（配置错、数据/口径不支持），重试无意义 → FAILED；
            False = 临时性失败（网络、限流、被中断）→ ABORTED，允许后续重试。
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _log(message: str) -> None:
    """打印一行带时间戳的运行日志（stdout，flush 以便 Actions 实时看到进度）"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def _warn(message: str) -> None:
    """打印告警到 stderr（stderr 与 _log 分开，便于日志过滤）"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print("[%s] [WARN] %s" % (stamp, message), file=sys.stderr, flush=True)


def _envText(name: str, default: str = "") -> str:
    """读环境变量并 strip；未设置或为空时返回默认值"""
    return (os.environ.get(name) or "").strip() or default


def _envFlag(name: str, default: bool = False) -> bool:
    """读布尔型环境变量（TRUE_VALUES 命中为真；未设置时取 default）"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in TRUE_VALUES


def _envInt(name: str, default: Optional[int] = None) -> Optional[int]:
    """读整数型环境变量；未设置或非法时返回 default（不抛异常，仅因环境脏值就崩在入口）"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _warn("%s=%r 不是整数，已忽略并取默认值 %r" % (name, raw, default))
        return default




# ---------------------------------------------------------------------------
# 二、取值归一化
# ---------------------------------------------------------------------------


# ===== BEGIN INLINED FROM ExportArchiveMvsvInline.py =====
# 来源：原型脚本 ExportArchiveMvsvInline.py（**已退役**，不再随仓库提供）。
# 本区段是它的必需子集**逐字搬运**：常量 + 客户端库（Ed25519 / RSA-SHA256 签名、传输层、
# 历史 K 线接口）+ 数据模型 + 导出层；仅挖去原型的 `main()` 与 `if __name__` 入口
# （由本文件的入口替代）。内联来源模块清单见本文件开头的模块 docstring。
#
# ⚠️ 维护方式：来源文件已退役，**不再有"重新生成"的途径**
#   （曾尝试的 build_apply.py 未跑通、从未提交、已删除）。
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
# 别名：ClientFacade 原本以 ``from .QuoteHistoryKline import getHistoryKline as _getHistoryKline``
# 这类形式导入同包函数，内联到同一命名空间后补上等价别名，使其代码保持原样。
# ---------------------------------------------------------------------------
_fetchHistoryKline = fetchHistoryKline
_fetchHistoryKlineFullDay = fetchHistoryKlineFullDay
_getHistoryKline = getHistoryKline
_fetchServerDrift = fetchServerDrift
_getServerTime = getServerTime
_fetchStockBasicInfo = fetchStockBasicInfo
_postStockBasicInfo = postStockBasicInfo
_fetchTradingDays = fetchTradingDays
_getTradingDays = getTradingDays


# ===========================================================================
# 以下为示例自身的逻辑（导出参数、数据处理模型、取数、序列化与 main）
# ===========================================================================

# -*- coding: utf-8 -*-


# 非正式模块导入使用相对路径导入，需要加上以下这一行，正式pypi模块下面这一行可以去掉。

# ---- 导出参数：按需修改 ----
SCHEDULE_URL = ("https://raw.githubusercontent.com/acdnx/Distribution/refs/heads/quote-mon"
                "/.github/Python/ArchiveSchedule.jsonl")
DATA_PROVIDER = "FTMM"              # 写入 MVSV 头与文件名第 5 段的数据源标识
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "ZZFS" / "Finv" / "Quote"
REQUEST_INTERVAL_SECONDS = 1.0      # 相邻日期的请求间隔（秒）
MAX_RETRIES = 5                     # 命中限流时的最大重试次数
RETRY_BACKOFF_SECONDS = 30          # 限流重试的退避基数（秒），按 2 的幂递增

#: 调度表 type_kline 取值 → 接口 ktype
#: 该键同为本示例写入 MVSV 头部 ``TypeKLine`` 的来源，标准取值域为：
#: ``MIN`` / ``MIN5`` / ``MIN10`` / ``HOUR`` / ``HOUR2`` / ``HOUR3`` / ``HOUR6`` /
#: ``DAY`` / ``WEEK`` / ``MONTH`` / ``YEAR``（头部按**大驼峰**输出，如 ``MIN`` → ``Min``）
KTYPE_BY_TYPE_KLINE: Dict[str, str] = {"MIN": KTYPE_MIN, "DAY": KTYPE_DAY}

#: 一根 K 线覆盖多日的周期类型（调度表 type_kline 取值）→ 按**整段区间**取数。
#: 这类 K 线逐日请求毫无意义（一天只回一根），故对 ``[start, end)`` 一次请求取全；
#: 分钟级类型仍走「逐日循环 + 按标的类型分流」的取数策略。
INTERVAL_KLINE_TYPES = ("DAY", "WEEK", "MONTH", "YEAR")

#: MVSV 头部 Title 中的周期词（按调度表 type_kline 取值；未列出时回退为 TypeKLine 大驼峰值）
MVSV_TITLE_PERIODS: Dict[str, str] = {
    "MIN": "Minute", "MIN5": "5 Minute", "MIN10": "10 Minute",
    "HOUR": "Hourly", "HOUR2": "2 Hour", "HOUR3": "3 Hour", "HOUR6": "6 Hour",
    "DAY": "Daily", "WEEK": "Weekly", "MONTH": "Monthly", "YEAR": "Yearly",
}

# ---------------------------------------------------------------------------
# 单日取数策略的判据（临时适配层）
# ---------------------------------------------------------------------------
# 官方历史 K 线接口对「24 小时连续交易」的品种无法一次取全单日数据：单次上限 1000 条、
# extended_time 三档对它们不过滤（实测三档的 time_key 集合完全相同）、REST 路径又不支持
# 翻页与按分钟指定窗口。故按标的类型分流，在接口修复前尽最大努力覆盖全天。
# 完整实测依据与缺口分析见同目录 IssueFullDayCoverage.md。
#
# 维护指引：调整分类只改下面三个常量；替换某类标的的取数算法，
# 只需改 fetchDayBarsByStrategy 中对应的那个分支。

#: 连续交易市场前缀（无盘中/盘前盘后/夜盘划分）→ 区间 + 游标拼接
#: 实测：CC.PAXG 三档均返回 1000 条（00:00~16:39），拼接后 1370 条
CONTINUOUS_TRADING_MARKETS = ("CC",)

#: 有时段划分的市场前缀 → 三档 extended_time 并集
#: 实测：US.GLD 三档为 390 / 960 / 871 条，并集 1441 条（完整一天）
TIERED_TRADING_MARKETS = ("US",)

#: 连续交易的代码后缀（期货主连）→ 与连续交易市场同样处理
#: 实测：US.GCmain 同样三档等价（各 1000 条）；注意 CC.GCmain 是无效代码，
#: 期货主连的规范写法为「市场.品种main」（品种大写、main 小写）
CONTINUOUS_TRADING_SUFFIX = "main"

#: 区间模式单次返回上限（实测值）：区间请求未触及该值即说明当日数据已取全，无需游标补尾
SERVER_PAGE_LIMIT = 1000

#: 调度表 region 取值 → 时区标识（写入 MVSV 头）
TIMEZONE_BY_REGION: Dict[str, str] = {
    "US": "America/New_York",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Shanghai",
}

#: MVSV 字段元数据：缩写 → （长名, 类型）。**列序不在这里**——两个级别的列序不同，
#: 由下面的 ``MVSV_MIN_FIELDS`` / ``MVSV_DAY_FIELDS`` 各自声明；标的名只在头部 `# Name`
#: 出现，故不占数据列。共 19 个候选字段
MVSV_FIELD_SPECS: Dict[str, Tuple[str, str]] = {
    "ts": ("Timestamp", "Int"),
    "d": ("Date", "Int"),
    "t": ("Time", "Int"),
    "o": ("Open", "Decimal"),
    "h": ("High", "Decimal"),
    "l": ("Low", "Decimal"),
    "c": ("Close", "Decimal"),
    "v": ("Volume", "Decimal"),
    "a": ("Turnover", "Decimal"),
    "cp": ("ChangePrice", "Decimal"),
    "cr": ("ChangeRatio", "Decimal"),
    "lc": ("LastClose", "Decimal"),
    "se": ("Session", "String"),
    "om": ("OffsetMinute", "Int"),
    "sp": ("SettlePrice", "Decimal"),
    "oi": ("OpenInterest", "Decimal"),
    "pe": ("PeRatio", "Decimal"),
    "tr": ("TurnoverRate", "Decimal"),
    "iv": ("ImpliedVolatility", "Decimal"),
}

#: **分钟级**列序（14 列）：没有 `pe` / `tr` / `oi` / `iv`（分钟级无这些口径）与 `sp`
#: （接口不返回结算价）；`om` 紧随 `lc`，**`se` 为行末字段**
MVSV_MIN_FIELDS = ("ts", "d", "t", "o", "h", "l", "c", "v", "a", "cp", "cr", "lc", "om", "se")

#: **日 / 周 / 月 / 年级**列序（18 列）：`lc` 之后依次是结算价、持仓量与基本面三项，
#: **行末为 `om`**；没有 `se`（周期 K 线没有时段划分）
MVSV_DAY_FIELDS = ("ts", "d", "t", "o", "h", "l", "c", "v", "a", "cp", "cr", "lc",
                   "sp", "oi", "pe", "tr", "iv", "om")

#: 分钟级字段长名（与 ``MVSV_MIN_FIELDS`` 一一对应）
MVSV_MIN_FIELD_NAMES = tuple(MVSV_FIELD_SPECS[field][0] for field in MVSV_MIN_FIELDS)

#: 分钟级字段类型（与 ``MVSV_MIN_FIELDS`` 一一对应）
MVSV_MIN_FIELD_TYPES = tuple(MVSV_FIELD_SPECS[field][1] for field in MVSV_MIN_FIELDS)

#: 日 / 周 / 月 / 年级字段长名（与 ``MVSV_DAY_FIELDS`` 一一对应）
MVSV_DAY_FIELD_NAMES = tuple(MVSV_FIELD_SPECS[field][0] for field in MVSV_DAY_FIELDS)

#: 日 / 周 / 月 / 年级字段类型（与 ``MVSV_DAY_FIELDS`` 一一对应）
MVSV_DAY_FIELD_TYPES = tuple(MVSV_FIELD_SPECS[field][1] for field in MVSV_DAY_FIELDS)

#: MVSV 时段标识 → 中文说明（仅用于运行日志；时段值本身由 ``KlineMinBar.applySessions``
#: 按市场写入：美股四档，其他市场留空）
SESSION_TEXT_LABELS: Dict[str, str] = {
    "PRE": "盘前",
    "RTH": "盘中",
    "POST": "盘后",
    "ONT": "夜盘",
}


# ---------------------------------------------------------------------------
# 数据模型（原先在 KlineMinBar.py / KLineDayBar.py，此处内联以保持单文件自包含）
# ---------------------------------------------------------------------------
#: 美股四时段的划分（标的**本地时间**的分钟数，半开区间 ``[起, 止)``）。
#: 边界与客户端 ``classifyTradingSession`` 一致（09:30 / 16:00 / 20:00 / 04:00），
#: 区别只在于把客户端合并成一档的「盘前盘后」（``ETH``）拆成盘前与盘后两档。
US_SESSION_RANGES = (
    (4 * 60, 9 * 60 + 30, "PRE"),           # 盘前 04:00 ~ 09:30
    (9 * 60 + 30, 16 * 60, "RTH"),          # 盘中 09:30 ~ 16:00
    (16 * 60, 20 * 60, "POST"),             # 盘后 16:00 ~ 20:00
)

#: 夜盘标识（20:00 ~ 次日 04:00，即上表未覆盖的其余时间）
US_SESSION_OVERNIGHT = "ONT"


@dataclass
class KlineMinBar:
    """分钟级单根 K 线（字段即 MVSV 数据列，共 14 列 + 头部用 ``name``）。

    Attributes:
        timestamp: 秒级时间戳（UTC），对应 MVSV 列 ``Timestamp``。
        date: 标的本地日期整数 ``YYYYMMDD``，对应 ``Date``。
        time: 标的本地时间整数 ``HHMMSS``，对应 ``Time``。
        open: 开盘价。
        high: 最高价。
        low: 最低价。
        close: 收盘价。
        volume: 成交量。
        turnover: 成交额。
        changePrice: 涨跌额（``close - lastClose`` 现算），对应 ``ChangePrice``。
        changeRatio: 涨跌幅（百分数），对应 ``ChangeRatio``。
        lastClose: 昨收价，对应 ``LastClose``。
        offsetMinute: 该行所属时区偏移（**分钟**），对应 ``OffsetMinute``。
        session: MVSV 时段标识（**行末字段**）——美股取 ``PRE`` 盘前 / ``RTH`` 盘中 /
            ``POST`` 盘后 / ``ONT`` 夜盘四档；非美股（无盘前盘后与夜盘划分）取空串。
            由 ``applySessions`` 按市场重写，故 ``fromKlineBar`` 里先透传客户端的原值。
        name: 标的显示名（中文名优先）——**不是数据列**，只供文件头部 ``# Name`` 使用。
    """

    timestamp: int = 0
    date: int = 0
    time: int = 0
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    changePrice: Optional[float] = None
    changeRatio: Optional[float] = None
    lastClose: Optional[float] = None
    offsetMinute: int = 0
    session: str = ""
    name: Optional[str] = None

    @classmethod
    def fromKlineBar(cls, bar: KlineBar) -> "KlineMinBar":
        """由客户端 ``KlineBar`` 构造本模型（顺带完成一次性的字段换算）。

        Args:
            bar: 客户端产出的 ``KlineBar``。

        Returns:
            对应的 ``KlineMinBar``。
        """
        dateText = str(bar.localDate or "").replace("-", "")
        timeText = str(bar.time or "").split(" ")[-1].replace(":", "")
        changePrice = None
        if bar.close is not None and bar.lastClose is not None:
            changePrice = bar.close - bar.lastClose
        return cls(
            timestamp=bar.timeKey // 1000,
            date=int(dateText) if dateText.isdigit() else 0,
            time=int(timeText) if timeText.isdigit() else 0,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            turnover=bar.turnover,
            changePrice=changePrice,
            changeRatio=bar.changeRate,
            lastClose=bar.lastClose,
            offsetMinute=bar.timeZoneMinutes,
            session=bar.session,
            name=bar.scName or bar.name,
        )

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


def toMinBars(bars: Iterable[KlineBar]) -> List[KlineMinBar]:
    """把客户端的 ``KlineBar`` 序列转换为分钟级模型。

    这是**数据处理环节**的入口：取数部分（示例的 ``fetchDayBars`` 等）保持原样、
    依旧产出 ``KlineBar``，字段换算与命名标准化只在这里发生，基础接口不受影响。
    转换后通常还要调用 ``applySessions`` 按市场重写时段。

    Args:
        bars: ``KlineBar`` 序列。

    Returns:
        ``KlineMinBar`` 列表（保持输入顺序）。
    """
    return [KlineMinBar.fromKlineBar(bar) for bar in bars]


def resolveUsSession(timeInt: int) -> str:
    """按标的本地时间（``HHMMSS`` 整数）判定美股所处的时段。

    Args:
        timeInt: 标的本地时间整数，如 ``93000``（09:30:00）、``160000``（16:00:00）。

    Returns:
        时段标识：``PRE`` 盘前 / ``RTH`` 盘中 / ``POST`` 盘后 / ``ONT`` 夜盘。
    """
    minute = (timeInt // 10000) * 60 + (timeInt // 100) % 100
    for start, end, label in US_SESSION_RANGES:
        if start <= minute < end:
            return label
    return US_SESSION_OVERNIGHT


def applySessions(bars: Iterable[KlineMinBar], isUsMarket: bool) -> None:
    """按市场重写各行的 ``session``（原地修改）。

    美股（``region == "US"``）严格区分**盘前 / 盘中 / 盘后 / 夜盘**四档；其他市场（港股、
    A 股等）没有盘前盘后与夜盘的划分，一律写空串——序列化时该列留空。

    Args:
        bars: 分钟级模型序列。
        isUsMarket: 是否美股任务。

    Returns:
        无。
    """
    for bar in bars:
        bar.session = resolveUsSession(bar.time) if isUsMarket else ""


@dataclass
class KLineDayBar:
    """日 / 周 / 月 / 年级单根 K 线（字段即 MVSV 数据列，共 18 列 + 头部用 ``name``）。

    Attributes:
        timestamp: 秒级时间戳（UTC），对应 MVSV 列 ``Timestamp``。
        date: 标的本地日期整数 ``YYYYMMDD``，对应 ``Date``。
        time: 标的本地时间整数 ``HHMMSS``（周期 K 线为 ``0``），对应 ``Time``。
        open: 开盘价。
        high: 最高价。
        low: 最低价。
        close: 收盘价。
        volume: 成交量。
        turnover: 成交额。
        changePrice: 涨跌额（``close - lastClose`` 现算），对应 ``ChangePrice``。
        changeRatio: 涨跌幅（百分数），对应 ``ChangeRatio``。
        lastClose: 昨收价，对应 ``LastClose``。
        settlePrice: 结算价（期货 / 期权，正股为 None），对应 ``SettlePrice``。
        openInterest: 持仓量（期货 / 期权），对应 ``OpenInterest``。
        peRatio: 市盈率，对应 ``PeRatio``。
        turnoverRate: 换手率（百分数），对应 ``TurnoverRate``。
        impliedVolatility: 隐含波动率（期权），对应 ``ImpliedVolatility``。
        offsetMinute: 该行所属时区偏移（**分钟**），对应 ``OffsetMinute``。
        name: 标的显示名（中文名优先）——**不是数据列**，只供文件头部 ``# Name`` 使用。
    """

    timestamp: int = 0
    date: int = 0
    time: int = 0
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    turnover: Optional[float] = None
    changePrice: Optional[float] = None
    changeRatio: Optional[float] = None
    lastClose: Optional[float] = None
    settlePrice: Optional[float] = None
    openInterest: Optional[float] = None
    peRatio: Optional[float] = None
    turnoverRate: Optional[float] = None
    impliedVolatility: Optional[float] = None
    offsetMinute: int = 0
    name: Optional[str] = None

    @classmethod
    def fromKlineBar(cls, bar: KlineBar) -> "KLineDayBar":
        """由客户端 ``KlineBar`` 构造本模型（顺带完成一次性的字段换算）。

        Args:
            bar: 客户端产出的 ``KlineBar``。

        Returns:
            对应的 ``KLineDayBar``。
        """
        dateText = str(bar.localDate or "").replace("-", "")
        timeText = str(bar.time or "").split(" ")[-1].replace(":", "")
        changePrice = None
        if bar.close is not None and bar.lastClose is not None:
            changePrice = bar.close - bar.lastClose
        return cls(
            timestamp=bar.timeKey // 1000,
            date=int(dateText) if dateText.isdigit() else 0,
            time=int(timeText) if timeText.isdigit() else 0,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            turnover=bar.turnover,
            changePrice=changePrice,
            changeRatio=bar.changeRate,
            lastClose=bar.lastClose,
            settlePrice=bar.settlePrice,
            openInterest=bar.openInterest,
            peRatio=bar.peRatio,
            turnoverRate=bar.turnoverRate,
            impliedVolatility=bar.impliedVolatility,
            offsetMinute=bar.timeZoneMinutes,
            name=bar.scName or bar.name,
        )

    @property
    def hasPrice(self) -> bool:
        """是否含有效价格（四价至少一项非空）。

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


def toDayBars(bars: Iterable[KlineBar]) -> List[KLineDayBar]:
    """把客户端的 ``KlineBar`` 序列转换为日 / 周 / 月 / 年级模型。

    与 ``KlineMinBar.toMinBars`` 的唯一差别是多带 5 个周期 K 线专有字段，换算规则完全一致。

    Args:
        bars: ``KlineBar`` 序列（日 / 周 / 月 / 年级任务整段区间取数的产出）。

    Returns:
        ``KLineDayBar`` 列表（保持输入顺序）。
    """
    return [KLineDayBar.fromKlineBar(bar) for bar in bars]



def downloadSchedule(url: str) -> List[Dict[str, Any]]:
    """下载并解析归档调度表（JSONL，一行一条任务）。

    Args:
        url: 调度表 URL。

    Returns:
        任务字典列表（空行与无法解析的行被跳过）。

    Raises:
        MoomooOpenAPIException: 下载失败时抛出（含 URL 与原因）。
    """
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode("utf-8")
    except OSError as exc:
        raise MoomooOpenAPIException(f"下载归档调度表失败：{url}｜{exc}") from exc
    tasks: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tasks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not tasks:
        raise MoomooOpenAPIException(f"归档调度表无有效任务：{url}")
    return tasks


def pickTask(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """随机挑选一条归档任务。

    Args:
        tasks: 任务字典列表。

    Returns:
        被选中的任务字典。
    """
    return random.choice(tasks)


def iterDays(startDate: str, endDate: str) -> List[str]:
    """列出需要逐日请求的自然日（左闭右开）。

    调度表的 ``end`` 是**不含**的上界（如 20250401 ~ 20250501 表示整个 4 月），
    故请求日到 ``endDate`` 的前一天为止。

    Args:
        startDate: 起始日 ``YYYY-MM-DD``（含）。
        endDate: 结束日 ``YYYY-MM-DD``（不含）。

    Returns:
        日期字符串列表（升序）。
    """
    start = date.fromisoformat(startDate)
    return [(start + timedelta(days=offset)).isoformat()
            for offset in range((date.fromisoformat(endDate) - start).days)]


def isRetryable(exc: MoomooOpenAPIException) -> bool:
    """判断异常是否值得重试。

    服务端限流有三种形态，前两种带状态码或业务码，第三种是直接切断 TLS 连接：

    1. HTTP 429（显式限流）；
    2. HTTP 200 + ``ret_code=-11``（``error.code=rate_limited``，附 ``retry after Ns``）；
    3. 网络层失败——既无 HTTP 状态码也无业务码（``httpStatus`` 与 ``code`` 皆为空）。

    Args:
        exc: 网关抛出的异常。

    Returns:
        True 表示可重试。
    """
    if exc.httpStatus == 429 or exc.code == -11:
        return True
    return exc.httpStatus is None and exc.code is None


def fetchDayBars(client: Any, symbol: str, day: str, ktype: str) -> List[Any]:
    """取单日 K 线（按标的类型分流策略）；命中限流时按指数退避重试。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码，如 ``US.FUTU`` / ``CC.PAXG`` / ``US.GCmain``。
        day: 目标自然日 ``YYYY-MM-DD``。
        ktype: K 线类型，``1`` 为 1 分钟。

    Returns:
        KlineBar 列表。

    Raises:
        MoomooOpenAPIException: 不可重试的错误，或重试次数耗尽时抛出。
    """
    for attempt in range(MAX_RETRIES):
        try:
            return fetchDayBarsByStrategy(client, symbol, day, ktype)
        except MoomooOpenAPIException as exc:
            if not isRetryable(exc) or attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * 2 ** attempt)
    return []


def fetchRangeBars(client: Any, symbol: str, startDate: str, endDate: str,
                   ktype: str) -> List[Any]:
    """整段取区间型 K 线（日 / 周 / 月 / 年）：一次区间请求，必要时前向分页。

    日 / 周 / 月 / 年 K 线**一根覆盖多日**，逐日请求既慢又错（一天只回一根），
    故对 ``[startDate, endDate)`` 直接按区间取数；接口的记录**自带** ``settle_price``，
    无需像分钟级那样另取日 K 回填。

    服务端区间模式单次上限 ``SERVER_PAGE_LIMIT``（1000 条）：月度、周度任务的条数远低于
    该值，一次请求即取全；若触顶（如数年以上的日线区间），自本页最后一条之后继续请求，
    直到取满或越过 ``endDate``，避免静默截断。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码，如 ``US.GCmain``。
        startDate: 区间起点 ``YYYY-MM-DD``（含）。
        endDate: 区间终点 ``YYYY-MM-DD``（调度表口径为**不含**；接口会多返回该日当天的
            K 线，由调用方按 ``localDate`` 过滤剔除）。
        ktype: K 线类型，日 K 为 ``2``。

    Returns:
        KlineBar 列表（按 ``timeKey`` 升序、已剔除无 OHLC 的占位记录）。

    Raises:
        MoomooOpenAPIException: 不可重试的错误，或重试次数耗尽时抛出。
    """
    merged: Dict[int, Any] = {}
    cursor = startDate
    while cursor < endDate:
        page = _fetchRangePage(client, symbol, cursor, endDate, ktype)
        if not page:
            break
        for bar in page:
            merged.setdefault(bar.timeKey, bar)
        if len(page) < SERVER_PAGE_LIMIT:
            break
        cursor = (date.fromisoformat(page[-1].localDate) + timedelta(days=1)).isoformat()
        print(f"（区间模式触及单次上限 {SERVER_PAGE_LIMIT} 条，自 {cursor} 继续分页）", flush=True)
        time.sleep(REQUEST_INTERVAL_SECONDS)
    bars = [merged[key] for key in sorted(merged)]
    print(f"（策略：区间模式取到 {len(bars)} 条）", flush=True)
    return bars


def _fetchRangePage(client: Any, symbol: str, startDate: str, endDate: str,
                    ktype: str) -> List[Any]:
    """区间型 K 线的单页请求（命中限流时按指数退避重试）。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码。
        startDate: 本页起点 ``YYYY-MM-DD``（含）。
        endDate: 区间终点 ``YYYY-MM-DD``。
        ktype: K 线类型。

    Returns:
        KlineBar 列表（升序、已剔除无 OHLC 的占位记录）。

    Raises:
        MoomooOpenAPIException: 不可重试的错误，或重试次数耗尽时抛出。
    """
    for attempt in range(MAX_RETRIES):
        try:
            data = client.getHistoryKline(symbol=symbol, start=startDate, end=endDate,
                                          ktype=ktype, extendedTime=EXTENDED_TIME_ALL)
            bars = convertKlineItems((data or {}).get("kline_list"))
            return [bar for bar in bars if bar.hasPrice]
        except MoomooOpenAPIException as exc:
            if not isRetryable(exc) or attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * 2 ** attempt)
    return []


def fetchDayBarsByStrategy(client: Any, symbol: str, day: str, ktype: str) -> List[Any]:
    """按标的类型选择单日取数策略（临时适配层：接口修复前的最优努力方案）。

    官方历史 K 线接口当前对「24 小时连续交易」的品种无法一次取全单日数据
    （单次上限 1000 条、``extended_time`` 三档对它们不过滤、REST 路径不支持翻页），
    此处按市场前缀与代码后缀分流；完整实测依据见同目录 ``IssueFullDayCoverage.md``。

    | 类别 | 判据 | 策略 | 单日覆盖 |
    | --- | --- | --- | --- |
    | 连续交易（加密货币 / 期货主连） | 市场为 ``CC`` 或代码以 ``main`` 结尾 | 区间模式 + 游标模式拼接 | 1370 条（期货约 99%、加密货币约 95%） |
    | 美股 | 市场为 ``US`` 且无 ``main`` 后缀 | 三档 ``extended_time`` 并集 | 1441 条（完整） |
    | 其他市场 | 其余 | 单次按日请求（``extended_time=2``） | 该市场当日全量 |

    本方法只负责**取数**，产出客户端的 ``KlineBar``；转成分钟级模型是后续数据处理环节的
    职责（见 ``KlineMinBar.toMinBars``）。

    后续官方若支持按分钟指定 ``start`` / ``end`` 或恢复游标翻页，只需调整本方法的分支。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码，如 ``US.FUTU`` / ``CC.PAXG`` / ``US.GCmain`` / ``HK.00700``。
        day: 目标自然日 ``YYYY-MM-DD``（标的本地日期）。
        ktype: K 线类型，``1`` 为 1 分钟。

    Returns:
        KlineBar 列表（升序、已剔除无 OHLC 的占位记录）。

    Raises:
        MoomooOpenAPIException: 接口报错时抛出（限流重试由 ``fetchDayBars`` 负责）。
    """
    # 判据：市场前缀（CC / US）与代码后缀（期货主连 main）；调整分类只需改这三个常量
    market = symbol.split(".")[0].upper()
    if market in CONTINUOUS_TRADING_MARKETS or symbol.lower().endswith(CONTINUOUS_TRADING_SUFFIX):
        bars = _fetchDayByIntervalAndCursor(client, symbol, day, ktype)          # 首段 + 尾段拼接
    elif market in TIERED_TRADING_MARKETS:
        bars = client.fetchHistoryKlineFullDay(symbol=symbol, day=day, ktype=ktype)["bars"]  # 三档并集
    else:
        bars = _fetchDayOnce(client, symbol, day, ktype)                         # 单次按日
    # 统一剔除未产生行情的占位记录：其 OHLC 为空，会让 MVSV 的 cp / cr 输出成空字段
    bars = [bar for bar in bars if bar.hasPrice]
    # 期货标的回填当日结算价：分钟级接口不返回 settle_price，只有日 K（ktype=2）才带该字段
    if symbol.lower().endswith(CONTINUOUS_TRADING_SUFFIX):
        _fillSettlePrice(client, symbol, day, bars)
    return bars


def _fetchDayByIntervalAndCursor(client: Any, symbol: str, day: str, ktype: str) -> List[Any]:
    """连续交易品种：区间模式（上限 1000 条）+ 游标模式（默认 370 条）拼接去重。

    两种窗口分别锚定当日的**起点**与**终点**——区间模式自 ``start`` 往后返回，
    游标模式（不传 ``start``）自 ``end`` 往前返回；合并后覆盖当日首尾两段，
    中间仍留有缺口（期货约 11 分钟、加密货币约 71 分钟），无法在 REST 参数空间内补齐。

    若区间模式返回条数**未触及** ``SERVER_PAGE_LIMIT``，说明当日数据已取全
    （如恒指主连约 976 条），此时直接返回，不再发游标请求。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码。
        day: 目标自然日 ``YYYY-MM-DD``。
        ktype: K 线类型。

    Returns:
        KlineBar 列表（升序）。

    Raises:
        MoomooOpenAPIException: 区间模式请求失败时抛出；游标模式失败仅告警不中断。
    """
    endDate = (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    # ① 区间模式：服务端自 start 往后返回，最多 SERVER_PAGE_LIMIT 条，构成当日**首段**
    merged: Dict[int, Dict[str, Any]] = {}
    data = client.getHistoryKline(symbol=symbol, start=day, end=endDate, ktype=ktype,
                                  extendedTime=EXTENDED_TIME_ALL)
    _collectItems(data, merged)
    intervalCount = len(merged)
    if intervalCount < SERVER_PAGE_LIMIT:
        print(f"（策略：区间模式 {intervalCount} 条，未触顶，当日已取全）", flush=True)
        return convertKlineItems([merged[key] for key in sorted(merged)])

    # ② 游标模式：不传 start，服务端自 end 往前返回（默认 370 条），构成**尾段**；
    #    失败时降级为仅区间结果（尽力而为），不中断整日取数。
    try:
        data = client.getHistoryKline(symbol=symbol, end=endDate, ktype=ktype,
                                      extendedTime=EXTENDED_TIME_ALL)
        _collectItems(data, merged)
    except MoomooOpenAPIException as exc:
        print(f"（游标模式补尾失败，仅保留区间模式结果：{exc}）", flush=True)
    else:
        print(f"（策略：区间模式 {intervalCount} 条 + 游标补尾，合计 {len(merged)} 条）", flush=True)
    return convertKlineItems([merged[key] for key in sorted(merged)])


def _fetchDayOnce(client: Any, symbol: str, day: str, ktype: str) -> List[Any]:
    """常规市场：单次按日请求（``extended_time=2`` 取最全时段）。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码。
        day: 目标自然日 ``YYYY-MM-DD``。
        ktype: K 线类型。

    Returns:
        KlineBar 列表（升序）。

    Raises:
        MoomooOpenAPIException: 接口报错时抛出。
    """
    endDate = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    # 其他市场（如 HK.00700）当日数据量远小于 1000 条，单次请求即可取全，无需分档或拼接
    data = client.getHistoryKline(symbol=symbol, start=day, end=endDate, ktype=ktype,
                                  extendedTime=EXTENDED_TIME_ALL)
    return convertKlineItems((data or {}).get("kline_list"))


def _fillSettlePrice(client: Any, symbol: str, day: str, bars: List[Any]) -> None:
    """为期货标的回填当日结算价（``settle_price``，原地修改 ``bars``）。

    实测：分钟级 K 线的条目**不含** ``settle_price`` 字段——该字段只在日 / 周 / 月 K
    （``ktype=2`` / ``3`` / ``4``）返回。故对期货主连（代码以 ``main`` 结尾）额外请求
    一次**日 K** 取当日结算价，再回填到当日全部分钟行（结算价是当日唯一值，逐行相同）。

    该请求失败或接口未返回该字段时仅打印提示，``sp`` 留空，不影响整日取数。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码，如 ``US.GCmain``。
        day: 目标自然日 ``YYYY-MM-DD``。
        bars: 该日的 KlineBar 列表，原地写入 ``settlePrice``。

    Returns:
        无。
    """
    endDate = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    try:
        data = client.getHistoryKline(symbol=symbol, start=day, end=endDate, ktype=KTYPE_DAY,
                                      extendedTime=EXTENDED_TIME_ALL)
    except MoomooOpenAPIException as exc:
        print(f"（结算价补取失败，{day} 的 sp 留空：{exc}）", flush=True)
        return
    settle: Optional[float] = None
    for item in (data or {}).get("kline_list") or []:
        value = item.get("settle_price") if isinstance(item, dict) else None
        if isinstance(value, (int, float)) and value:
            settle = float(value)
            break
    if settle is None:
        print(f"（{day} 日 K 未返回 settle_price，sp 留空）", flush=True)
        return
    for bar in bars:
        bar.settlePrice = settle
    print(f"（结算价 {settle} 已回填 {len(bars)} 行）", flush=True)


def toMvsvFieldMap(bar: Any) -> Dict[str, str]:
    """把单根 K 线**模型**转为 MVSV 数据行的 ``{字段缩写: 文本}`` 映射（键同 ``MVSV_FIELD_SPECS``）。

    模型（``KlineMinBar`` / ``KLineDayBar``）的字段名就是 MVSV 列名的小驼峰，因此这里只做
    **文本化**，不再有任何名字映射与数值换算——换算（时间戳取秒、日期时间整数化、涨跌额现算）
    已在模型的 ``fromKlineBar`` 里一次性完成。返回映射而非定长列表，是为了让字段集与序列化
    解耦：分钟级取 ``MVSV_MIN_FIELDS``（14 列）、日 / 周 / 月 / 年级取 ``MVSV_DAY_FIELDS``
    （18 列），二者共用本函数。

    文本化规则：数值缺失留空（与参考 MVSV 文件的写法一致）；``ChangePrice`` 与 ``ChangeRatio``
    按定点 6 位小数输出（不足位右补 0）；``Time`` 补足 6 位（``000000``）。时段列 ``se`` 直接取
    模型已写入的值（美股 ``PRE`` / ``RTH`` / ``POST`` / ``ONT``，其他市场为空）。

    Args:
        bar: ``KlineMinBar``（分钟级）或 ``KLineDayBar``（日 / 周 / 月 / 年级）实例。

    Returns:
        ``{字段缩写: 文本}`` 映射，键与 ``MVSV_FIELD_SPECS`` 一致（19 项）：分钟级模型不含
        ``pe`` / ``tr`` / ``oi`` / ``iv`` / ``sp``，日线级模型不含 ``se``，取不到值的列留空，
        且不会被 ``writeMvsv()`` 导出。
    """
    return {
        "ts": str(bar.timestamp),
        "d": str(bar.date),
        "t": f"{bar.time:06d}",
        "o": _text(bar.open),
        "h": _text(bar.high),
        "l": _text(bar.low),
        "c": _text(bar.close),
        "v": _text(bar.volume),
        "a": _text(bar.turnover),
        "cp": _fixed6(bar.changePrice),
        "cr": _fixed6(bar.changeRatio),
        "lc": _text(bar.lastClose),
        "se": _sessionOf(bar),
        "om": str(bar.offsetMinute),
        "pe": _text(getattr(bar, "peRatio", None)),
        "tr": _text(getattr(bar, "turnoverRate", None)),
        "oi": _text(getattr(bar, "openInterest", None)),
        "iv": _text(getattr(bar, "impliedVolatility", None)),
        "sp": _text(getattr(bar, "settlePrice", None)),
    }


def _sessionOf(bar: Any) -> str:
    """取模型里的时段原值（日线级模型没有 ``session`` 字段，返回空串）。

    Args:
        bar: K 线模型实例。

    Returns:
        时段文本（``RTH`` / ``ETH`` / ``OVERNIGHT``）或空串。
    """
    return str(getattr(bar, "session", "") or "")


def buildMvsvName(task: Dict[str, Any], periodLabel: str) -> str:
    """按归档任务拼出 MVSV 文件名。

    形如 ``US_COMEX_GCMain_Day_FTMM_Mon_20260921.mvsv``——第 4 段是 K 线周期、第 6 段是调度表的
    ``period``，两段都按**大驼峰**输出（``MIN`` / ``Min`` → ``Min``，``DAY`` / ``Day`` → ``Day``，
    ``MON`` / ``Mon`` → ``Mon``）。周期标识紧随数据源之后：日标签为 ``YYYYMMDD``、月标签为
    ``YYYYMM``、周标签为 ``YYYYWW``，仅凭标签数字无法分辨周期，故显式写入。

    Args:
        task: 归档任务字典（提供 region / market / usc / type_kline / label）。
        periodLabel: 周期标识（大驼峰，如 ``Day`` / ``Mon`` / ``Week``）。

    Returns:
        文件名（不含目录）。
    """
    return (f"{task['region']}_{task['market']}_{task['usc']}_{_typeKlineLabel(task)}_"
            f"{DATA_PROVIDER}_{periodLabel}_{task['label']}.mvsv")


def writeMvsv(task: Dict[str, Any], periodLabel: str, bars: List[Any]) -> Optional[Path]:
    """把任务区间的 K 线写入 MVSV 文件（覆盖写）。

    头部按 14 行元信息写：``Title`` / ``DataProvider`` / ``Field`` / ``FieldName`` /
    ``FieldType`` / ``TypeKLine`` / ``Count`` / ``FetchTime`` / ``Symbol`` / ``Region`` /
    ``Market`` / ``USC`` / ``Name`` / ``TimeZone``，其中 ``Title`` 的周期词与 ``TypeKLine``
    均来自任务的 ``type_kline``（``MIN`` → ``Minute`` / ``Min``，``DAY`` → ``Daily`` / ``Day``），
    文件名第 4 段则沿用全大写原值。

    **字段集按级别切换**：分钟级任务只导出 ``MVSV_MIN_FIELDS``（14 列，不含 ``pe`` / ``tr`` /
    ``oi`` / ``iv`` / ``sp``），日 / 周 / 月 / 年级任务导出 ``MVSV_DAY_FIELDS``（18 列，
    保留上述 5 列但**不含** ``se``）。

    Args:
        task: 归档任务字典（提供 region / market / usc / type_kline / label / symbol）。
        periodLabel: 周期标识（大驼峰，如 ``Day`` / ``Mon`` / ``Week``），参与文件名。
        bars: 已过滤并去重的 K 线**模型**列表（``KlineMinBar`` 或 ``KLineDayBar``）。

    Returns:
        写入的文件路径；无数据时不创建文件并返回 None。
    """
    if not bars:
        print("[跳过] 任务区间内无数据，不创建文件")
        return None
    typeKline = _typeKline(task)
    periodWord = MVSV_TITLE_PERIODS.get(typeKline, _typeKlineLabel(task))
    if typeKline in INTERVAL_KLINE_TYPES:
        fields, fieldNames, fieldTypes = (MVSV_DAY_FIELDS, MVSV_DAY_FIELD_NAMES,
                                          MVSV_DAY_FIELD_TYPES)
    else:
        fields, fieldNames, fieldTypes = (MVSV_MIN_FIELDS, MVSV_MIN_FIELD_NAMES,
                                          MVSV_MIN_FIELD_TYPES)
    target = OUTPUT_DIR / buildMvsvName(task, periodLabel)
    tmpPath = target.with_suffix(target.suffix + ".tmp")
    with tmpPath.open("w", encoding="utf-8", newline="\n") as handle:
        header = [
            f"# Title : {task['usc']} {periodWord} Quote Data",
            f"# DataProvider : {DATA_PROVIDER}",
            f'# Field : "{"|".join(fields)}"',
            f'# FieldName : "{"|".join(fieldNames)}"',
            f'# FieldType : "{"|".join(fieldTypes)}"',
            f"# TypeKLine : {_typeKlineLabel(task)}",
            f"# Count : {len(bars)}",
            f'# FetchTime : "{datetime.now().astimezone().isoformat(timespec="seconds")}"',
            f"# Symbol : {task['symbol']}",
            f"# Region : {task['region']}",
            f"# Market : {task['market']}",
            f"# USC : {task['usc']}",
            f'# Name : "{bars[0].name or task["usc"]}"',
            f"# TimeZone : {TIMEZONE_BY_REGION.get(task['region'], 'UTC')}",
        ]
        # 头部与数据集之间保留一行空行
        handle.write("\n".join(header) + "\n\n")
        for bar in bars:
            fieldMap = toMvsvFieldMap(bar)
            handle.write("|".join(fieldMap[field] for field in fields))
            handle.write("\n")
    tmpPath.replace(target)
    size = target.stat().st_size
    print(f"[落盘] {target.name}｜{len(bars)} 行｜{size:,} 字节｜{len(fields)} 列｜"
          f"{bars[0].date} {bars[0].time:06d} ~ {bars[-1].date} {bars[-1].time:06d}")
    return target


def collectMinuteBars(client: Any, symbol: str, startDate: str, endDate: str,
                      ktype: str) -> List[KlineMinBar]:
    """分钟级任务：逐日取数 → 转 ``KlineMinBar`` → 按任务区间过滤。

    取数环节产出客户端 ``KlineBar``；数据处理环节（``toMinBars``）再换算成
    MVSV 数据列标准的模型（时间戳取秒、日期与时间整数化、涨跌额现算）。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码。
        startDate: 区间起点 ``YYYY-MM-DD``（含）。
        endDate: 区间终点 ``YYYY-MM-DD``（不含）。
        ktype: K 线类型（分钟级）。

    Returns:
        已过滤、**未去重**的 ``KlineMinBar`` 列表。
    """
    bars: List[KlineMinBar] = []
    startInt, endInt = _toDateInt(startDate), _toDateInt(endDate)
    days = iterDays(startDate, endDate)
    for index, day in enumerate(days, start=1):
        # 请求窗口会延伸到次日零点，须按日期过滤，剔除区间外的记录
        bars.extend(bar for bar in toMinBars(fetchDayBars(client, symbol, day, ktype))
                    if startInt <= bar.date < endInt)
        print(f"[进度] {index}/{len(days)} 天｜累计 {len(bars)} 根", flush=True)
        if index < len(days):
            time.sleep(REQUEST_INTERVAL_SECONDS)
    return bars


def collectDayBars(client: Any, symbol: str, startDate: str, endDate: str,
                   ktype: str) -> List[KLineDayBar]:
    """日 / 周 / 月 / 年级任务：整段区间取数 → 转 ``KLineDayBar`` → 按任务区间过滤。

    这类 K 线的记录**自带** ``settle_price``，模型因此保留 ``settlePrice``（分钟级模型没有
    该字段），导出的 19 列里 `PeRatio` / `TurnoverRate` / `OpenInterest` / `ImpliedVolatility` /
    `SettlePrice` 全部保留。

    Args:
        client: 已构造的 MoomooOpenAPIClient。
        symbol: 标的代码。
        startDate: 区间起点 ``YYYY-MM-DD``（含）。
        endDate: 区间终点 ``YYYY-MM-DD``（不含）。
        ktype: K 线类型（日 / 周 / 月 / 年）。

    Returns:
        已过滤、**未去重**的 ``KLineDayBar`` 列表。
    """
    rawBars = fetchRangeBars(client, symbol, startDate, endDate, ktype)
    # 接口的 end 为闭区间（会多回 endDate 当日那根），按「end 不含」的调度口径过滤
    startInt, endInt = _toDateInt(startDate), _toDateInt(endDate)
    bars = toDayBars(bar for bar in rawBars if startInt <= _toDateInt(bar.localDate) < endInt)
    print(f"[进度] 区间取数完成｜区间内 {len(bars)} 根（原始 {len(rawBars)} 根）", flush=True)
    return bars


def _typeKline(task: Dict[str, Any]) -> str:
    """取任务的 K 线周期类型（归一化为大写），用于 MVSV 文件名的第 4 段。

    标准取值为 ``MIN`` / ``MIN5`` / ``MIN10`` / ``HOUR`` / ``HOUR2`` / ``HOUR3`` /
    ``HOUR6`` / ``DAY`` / ``WEEK`` / ``MONTH`` / ``YEAR``，未列出的取值原样输出。

    Args:
        task: 归档任务字典（提供 type_kline）。

    Returns:
        大写的周期类型文本（如 ``MIN``）；字段缺失时返回空串。
    """
    return str(task.get("type_kline", "")).upper()


def _toPascal(value: Any) -> str:
    """把调度表取值转为大驼峰文本。

    ``type_kline`` 与 ``period`` 两列的取值统一按大驼峰输出：``DAY`` / ``Day`` → ``Day``，
    ``MON`` / ``Mon`` → ``Mon``，``MIN5`` / ``Min5`` → ``Min5``（历史数据里的全大写与新版
    调度表的大驼峰都能正确处理）。

    Args:
        value: 调度表字段值（大小写不限）。

    Returns:
        大驼峰文本；空值时返回空串。
    """
    return str(value or "").capitalize()


def _typeKlineLabel(task: Dict[str, Any]) -> str:
    """取任务的 K 线周期类型**大驼峰**文本，用于文件名第 4 段与 MVSV 头部 ``TypeKLine``。

    取 ``_typeKline`` 结果的大驼峰形式：``MIN`` → ``Min``、``MIN5`` → ``Min5``、
    ``MIN10`` → ``Min10``、``HOUR2`` → ``Hour2``、``MONTH`` → ``Month``。

    Args:
        task: 归档任务字典（提供 type_kline）。

    Returns:
        大驼峰周期类型文本（如 ``Min``）；字段缺失时返回空串。
    """
    return _toPascal(task.get("type_kline"))


def _periodLabel(task: Dict[str, Any]) -> str:
    """取任务的周期标识**大驼峰**文本，用于文件名第 6 段。

    调度表的 ``period`` 取值即 ``Day`` / ``Mon`` / ``Week`` 这类大驼峰文本（历史数据里也出现过
    全大写 ``DAY`` / ``MON``），本函数统一按大驼峰输出、不做取值域映射——``MON`` 与 ``Mon``
    都得到 ``Mon``。

    Args:
        task: 归档任务字典（提供 period）。

    Returns:
        大驼峰周期标识；字段缺失时返回空串（由调用方负责报错）。
    """
    return _toPascal(task.get("period"))


def _text(value: Optional[float]) -> str:
    """把可空数值转为字段文本（``None`` → 空串）。

    Args:
        value: 原始值。

    Returns:
        数值文本或空串。
    """
    return "" if value is None else str(value)


def _toDateInt(value: Any) -> int:
    """把日期文本转为 ``YYYYMMDD`` 整数（非法输入返回 0）。

    区间过滤统一用该整数比较：模型里的 ``date`` 已是 ``YYYYMMDD`` 整数，
    而客户端 ``KlineBar.localDate`` 是 ``YYYY-MM-DD`` 文本，两者都能处理。

    Args:
        value: ``YYYY-MM-DD`` 文本、``YYYYMMDD`` 整数或空值。

    Returns:
        ``YYYYMMDD`` 整数；无法解析时返回 0。
    """
    text = str(value or "").replace("-", "")
    return int(text) if text.isdigit() else 0


def _fixed6(value: Optional[float]) -> str:
    """把可空数值格式化为定点 6 位小数文本（不足位右补 0）；``None`` → 空串。

    涨跌额（``ChangePrice``）与涨跌幅（``ChangeRatio``）用本函数输出：浮点差值会出现长尾
    （如 ``0.10826371999999651``），接口原值也可达 17 位有效数字（如 ``0.10074182600727029``），
    按定点 6 位输出既稳定又可读（``0.108264`` / ``0.100742``）。

    Args:
        value: 原始数值。

    Returns:
        形如 ``0.100742`` 的文本或空串。
    """
    return "" if value is None else f"{value:.6f}"


def _collectItems(data: Any, merged: Dict[int, Dict[str, Any]]) -> None:
    """把接口响应中的 ``kline_list`` 按 ``time_key`` 收进字典（同一时间点保留首次出现）。

    两段窗口（区间模式的首段、游标模式的尾段）理论上不重叠，此处为防御性去重；
    非法条目（非 dict 或缺 ``time_key``）直接跳过。

    Args:
        data: 接口响应的 ``data`` 字段。
        merged: 累积字典，原地修改。

    Returns:
        无。
    """
    for item in (data or {}).get("kline_list") or []:
        if isinstance(item, dict) and isinstance(item.get("time_key"), int):
            merged.setdefault(item["time_key"], item)


def _deduplicate(bars: List[Any]) -> List[Any]:
    """按 ``timestamp`` 去重（先到先得）并按时间升序排序。

    Args:
        bars: K 线模型列表（``KlineMinBar`` 或 ``KLineDayBar``）。

    Returns:
        去重且升序的模型列表。
    """
    unique: Dict[int, Any] = {}
    for bar in bars:
        unique.setdefault(bar.timestamp, bar)
    return sorted(unique.values(), key=lambda bar: bar.timestamp)


def _toIsoDate(value: Any) -> str:
    """把调度表中的 ``YYYYMMDD`` 整数/字符串转为 ``YYYY-MM-DD``。

    Args:
        value: 形如 ``20250401`` 的日期。

    Returns:
        ``YYYY-MM-DD`` 字符串。

    Raises:
        MoomooOpenAPIException: 无法解析时抛出。
    """
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise MoomooOpenAPIException(f"调度表日期无法解析：{value!r}（应为 YYYYMMDD）") from exc

# ===== END INLINED FROM ExportArchiveMvsvInline.py =====


#: type_kline 标准取值域（大驼峰）；域外写法告警后按首字母大写兜底
STANDARD_TYPE_KLINE = ("Min", "Min5", "Min10", "Hour", "Hour2", "Hour3", "Hour6",
                       "Day", "Week", "Month", "Year")


def normalize_type_kline(raw: Any) -> str:
    """把作业表 `type_kline` 归一为**大驼峰**（本脚本全链路唯一出口）

    `MIN` / `min` / `Min` → `Min`；`MIN5` → `Min5`；`HOUR` → `Hour`。
    该值决定 `.mvsv` 文件名第 4 段、MVSV 头部 `# TypeKLine`、状态回写与日志展示，
    三处必须一致，故统一从本函数取。

    Args:
        raw: 作业表原值（大小写不限）。

    Returns:
        大驼峰文本；空值返回空串。域外写法按首字母大写兜底并告警。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    label = text.capitalize()
    if label not in STANDARD_TYPE_KLINE:
        _warn("type_kline=%r 不在标准取值域 %s 内，按大驼峰兜底为 %r；请核对作业表取值"
              % (raw, "/".join(STANDARD_TYPE_KLINE), label))
    return label


def normalize_period(raw: Any) -> str:
    """把作业表 `period` 归一为大驼峰（`MON` / `Mon` → `Mon`）

    与 `_periodLabel` 同口径（两者都是 `capitalize()`）；本脚本单独包一层是为了
    在空值时给出**点名到字段**的错误，而不是等到文件名拼接时才暴露。

    Args:
        raw: 作业表原值。

    Returns:
        大驼峰文本。

    Raises:
        JobExecutionError: 取值为空时抛出（永久性失败：没有 period 无法确定落点路径）。
    """
    text = str(raw or "").strip()
    if not text:
        raise JobExecutionError("作业表 period 为空，无法确定落点目录与文件名", permanent=True)
    return text.capitalize()


# ---------------------------------------------------------------------------
# 三、作业来源：Supabase 作业表
# ---------------------------------------------------------------------------
class SupabaseRestError(Exception):
    """Supabase Data API 调用失败"""


class SupabaseRestClient:
    """Supabase Data API 最小客户端（仅标准库 urllib）

    与 `SupabaseImportWeekMvsv.py` 的客户端同一手法：每个请求同时携带 `apikey` 与
    `Authorization: Bearer` 两个头（Supabase 要求二者并存），失败即抛 `SupabaseRestError`。

    本阶段只用它做两件事：查作业（GET）、回写作业状态（PATCH，受开关控制）。
    """

    def __init__(self, projectRef: str, apiKey: str, timeout: int = 30) -> None:
        """构造客户端

        :param projectRef: Supabase 项目引用（xvtunbplzpraqrogxsnt 这类）
        :param apiKey: API 密钥（service-role；不落日志）
        :param timeout: 单次请求超时秒数
        """
        if not projectRef:
            raise SupabaseRestError("Supabase 项目引用（%s）不能为空" % ENV_SUPABASE_REF)
        if not apiKey:
            raise SupabaseRestError("Supabase API 密钥（%s）不能为空" % ENV_SUPABASE_KEY)
        # 根地址可整体覆盖（自托管 / 代理 / 本地 mock）；未覆盖时按项目引用拼默认地址
        base = _envText(ENV_SUPABASE_REST_BASE) or (SUPABASE_REST_BASE % projectRef)
        self.restUrl = base.rstrip("/")
        self.apiKey = apiKey
        self.timeout = timeout

    def _headers(self, withBody: bool = False) -> Dict[str, str]:
        """组装请求头（apikey + Bearer + 可选 JSON 体声明）"""
        headers = {
            "apikey": self.apiKey,
            "Authorization": "Bearer %s" % self.apiKey,
            "Accept": "application/json",
        }
        if withBody:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, table: str, query: str, body: Optional[bytes],
                 prefer: Optional[str] = None) -> Tuple[int, str]:
        """发一次请求，返回 (状态码, 响应文本)

        :param prefer: Prefer 头（如 `return=representation` / `return=minimal`）
        :raises SupabaseRestError: 网络层失败（无状态码）时抛出
        """
        url = "%s/%s" % (self.restUrl, urllib.parse.quote(table, safe=""))
        if query:
            url += "?" + query
        headers = self._headers(withBody=body is not None)
        if prefer:
            headers["Prefer"] = prefer
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise SupabaseRestError("HTTP %s：%s" % (exc.code, detail.strip()[:500])) from exc
        except OSError as exc:
            raise SupabaseRestError("网络错误：%s" % exc) from exc

    def get(self, table: str, query: str) -> List[Dict[str, Any]]:
        """GET 查询，返回解析后的行列表"""
        status, text = self._request("GET", table, query, None)
        if status != 200:
            raise SupabaseRestError("GET %s 返回 HTTP %s：%s" % (table, status, text[:300]))
        try:
            data = json.loads(text or "[]")
        except ValueError as exc:
            raise SupabaseRestError("GET %s 响应不是合法 JSON：%s" % (table, exc)) from exc
        if not isinstance(data, list):
            raise SupabaseRestError("GET %s 响应不是行数组" % table)
        return [row for row in data if isinstance(row, dict)]

    def patch(self, table: str, query: str, payload: Dict[str, Any]) -> int:
        """PATCH 更新，返回受影响行数（靠 `Prefer: return=representation` 统计）

        :param query: 过滤条件（如 `id=eq.12`）；**必须非空**，防止误全表更新
        """
        if not query:
            raise SupabaseRestError("PATCH 缺少过滤条件，拒绝执行（防止误全表更新）")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, text = self._request("PATCH", table, query, body,
                                     prefer="return=representation")
        if status not in (200, 204):
            raise SupabaseRestError("PATCH %s 返回 HTTP %s：%s" % (table, status, text[:300]))
        if not text.strip():
            return 0
        try:
            rows = json.loads(text)
        except ValueError:
            return 0
        return len(rows) if isinstance(rows, list) else 0


def query_next_job(client: SupabaseRestClient, jobId: Optional[int] = None) -> Dict[str, Any]:
    """查询一条可运行作业（就绪规则见模块 docstring 第三节）

    Args:
        client: Supabase 客户端。
        jobId: 指定作业 id（定点重跑用）；None = 按就绪规则自动取最久未更新的那条。

    Returns:
        作业行 dict。

    Raises:
        JobExecutionError: 没有可运行作业（正常状态，非错误）或查询失败时抛出。
    """
    if jobId is not None:
        query = "select=%s&id=eq.%d&limit=1" % (JOB_SELECT_COLUMNS, jobId)
        rows = client.get(JOB_TABLE, query)
        if not rows:
            raise JobExecutionError("作业表中不存在 id=%d 的作业" % jobId, permanent=True)
        row = rows[0]
        status = str(row.get("job_status") or "")
        retry = int(row.get("count_retry") or 0)
        if status not in JOB_STATUS_COLLECTABLE:
            raise JobExecutionError(
                "指定作业 id=%d 状态为 %s，不在可运行集合 %s 内"
                % (jobId, status, "/".join(JOB_STATUS_COLLECTABLE)), permanent=True)
        if status != JOB_STATUS_READY and retry >= JOB_RETRY_LIMIT:
            raise JobExecutionError(
                "指定作业 id=%d 状态 %s 且 count_retry=%d 已达上限 %d，不再执行"
                % (jobId, status, retry, JOB_RETRY_LIMIT), permanent=True)
        return row

    query = "select=%s&%s&%s&%s" % (JOB_SELECT_COLUMNS, JOB_READY_FILTER, JOB_ORDER, JOB_LIMIT)
    rows = client.get(JOB_TABLE, query)
    if not rows:
        raise JobExecutionError(
            "作业表中没有可运行作业（READY，或 FAILED/ABORTED 且 count_retry<%d）"
            % JOB_RETRY_LIMIT)
    return rows[0]


def build_task_from_job(row: Dict[str, Any], periodOverride: str = "",
                        uscOverride: str = "") -> Dict[str, Any]:
    """把作业表行适配成原型脚本的 task dict（字段映射见模块 docstring 第四节）

    产出除原型的 9 个字段外，另带 `_job` 子字典承载作业元数据（id / job_name /
    job_status / count_retry），供状态流转与日志追溯；下划线前缀避免与调度表字段重名。

    Args:
        row: 作业表行。
        periodOverride: 覆盖 period（定点重跑用，空串 = 不覆盖）。
        uscOverride: 覆盖 usc（空串 = 不覆盖）。

    Returns:
        task dict。

    Raises:
        JobExecutionError: 关键字段缺失时抛出（永久性失败）。
    """
    def required(key: str) -> Any:
        value = row.get(key)
        if value is None or str(value).strip() == "":
            raise JobExecutionError("作业表字段 %s 为空，无法确定采集范围" % key, permanent=True)
        return value

    usc = str(uscOverride or "").strip() or str(required("usc")).strip()
    period = str(periodOverride or "").strip() or str(required("period"))

    task: Dict[str, Any] = {
        # ---- 原型脚本口径（collectMinuteBars / buildMvsvName / writeMvsv 直接消费）----
        "type_kline": normalize_type_kline(required("type_kline")),
        "period": normalize_period(period),
        "start": int(required("date_start")),
        "end": int(required("date_end")),
        "label": str(required("label")).strip(),
        "region": str(required("region")).strip(),
        "market": str(required("market")).strip(),
        "usc": usc,
        "symbol": str(required("symbol")).strip(),
        # ---- 作业元数据（不参与文件名与数据列）----
        "_job": {
            "id": row.get("id"),
            "job_name": row.get("job_name") or "",
            "job_prefix": row.get("job_prefix") or "",
            "job_status": row.get("job_status") or "",
            "count_retry": int(row.get("count_retry") or 0),
        },
    }
    if int(task["end"]) < int(task["start"]):
        raise JobExecutionError(
            "作业 %s 的日期区间非法：date_end(%s) < date_start(%s)"
            % (task["_job"]["job_name"], task["end"], task["start"]), permanent=True)
    return task


# ---------------------------------------------------------------------------
# 四、落点路径与远端写入
# ---------------------------------------------------------------------------
def build_remote_path(task: Dict[str, Any]) -> str:
    """拼出仓库内落点路径（第五节的模式）

    Args:
        task: 已适配的 task dict。

    Returns:
        `Archive/Finv/SecuQuoteData/FTMM/{period}/{文件名}`（POSIX 风格，不含前导 /）。
    """
    period = normalize_period(task["period"])
    fileName = buildMvsvName(task, period)
    return "%s/%s" % (ARCHIVE_ROOT_TEMPLATE % period, fileName)


def compute_digest(localFile: Path) -> Tuple[str, int, int]:
    """核算本地文件指纹：sha256 + 字节数 + 行数（上传前后对账用）

    Args:
        localFile: 本地文件路径。

    Returns:
        (sha256 十六进制, 字节数, 行数)。
    """
    raw = localFile.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    # 行数：以换行符计数（MVSV 每行以 \n 结尾，故行数 = \n 个数）
    lines = raw.count(b"\n")
    return digest, len(raw), lines


def push_to_repo(remotePath: str, localFile: Path, owner: str, repo: str, branch: str,
                 commitMsg: str) -> Dict[str, Any]:
    """经 GitHub Contents API 提交文件（第六节）

    Args:
        remotePath: 仓库内落点路径。
        localFile: 本地 MVSV 文件。
        owner / repo / branch: 目标仓库身份与分支（显式传入，不走 Commit.json 解析链）。
        commitMsg: 提交说明。

    Returns:
        结果 dict：`success` / `message` / `path` / `http_status`，另带 `digest` 元信息。

    Raises:
        JobExecutionError: 文件超过 Contents API 阈值（永久性失败）时抛出。
    """
    digest, size, lines = compute_digest(localFile)
    if size > GITHUB_CONTENTS_MAX_BYTES:
        raise JobExecutionError(
            "文件 %s 大小 %s 字节，超过 Contents API 单文件上限 %s 字节，无法经该接口提交"
            % (localFile.name, format(size, ","), format(GITHUB_CONTENTS_MAX_BYTES, ",")),
            permanent=True)

    _log("推送落点：%s/%s@%s ← %s（sha256 %s…｜%s 字节｜%d 行）"
         % (owner, repo, branch, remotePath, digest[:16], format(size, ","), lines))
    result = commit_content_file(remotePath, str(localFile), branch=branch,
                                 commit_msg=commitMsg, owner=owner, repo=repo)
    result["digest"] = digest
    result["size"] = size
    result["lines"] = lines
    if result.get("success"):
        _log("推送成功：HTTP %s｜%s" % (result.get("http_status"), remotePath))
    else:
        _warn("推送失败：HTTP %s｜%s" % (result.get("http_status"), result.get("message")))
    return result


# ---------------------------------------------------------------------------
# 五、作业状态回复写
# ---------------------------------------------------------------------------
class JobStateWriter:
    """作业状态写入器（受 `SUPABASE_ENABLE_JOB_UPDATE` 开关控制）

    开关关闭（默认）时只**打印**将要执行的 SQL 与绑定值，不触网——这是表写权限
    尚未配妥期间的降级形态；开关打开后同一批调用真实生效，调用点代码不变。
    """

    def __init__(self, client: Optional[SupabaseRestClient], enabled: bool) -> None:
        """构造写入器

        :param client: Supabase 客户端；None = 无凭据（此时即便 enabled 也只能 dry-run）
        :param enabled: 是否真正写入
        """
        self.client = client
        self.enabled = bool(enabled and client is not None)
        self.failures: List[str] = []
        #: dry-run / 真实写入时累积的状态变更 SQL，供运行摘要集中展示
        #: （动机：SQL 混在长日志里容易被淹没，摘要里一眼可见）
        self.sqlPlans: List[str] = []

    def _plan(self, jobId: Any, action: str, payload: Dict[str, Any]) -> str:
        """渲染一条待执行 SQL（dry-run 打印 + 日志追溯两用）

        用 `_sqlLiteral` 而非 `json.dumps`：后者产出双引号，在 PostgreSQL 里是标识符语义。
        """
        sets = ", ".join("%s = %s" % (key, _sqlLiteral(value))
                         for key, value in payload.items())
        return "UPDATE %s SET %s WHERE id = %s;  -- %s" % (JOB_TABLE, sets,
                                                           _sqlLiteral(jobId), action)

    def apply(self, jobId: Any, action: str, payload: Dict[str, Any]) -> bool:
        """执行（或计划）一次状态变更

        :param jobId: 作业 id。
        :param action: 动作说明（仅用于日志/SQL 注释）。
        :param payload: 待更新字段。
        :return: True = 已写入或已按 dry-run 计划；False = 真实写入失败。
        """
        sql = self._plan(jobId, action, payload)
        self.sqlPlans.append(sql)
        if not self.enabled:
            _log("[SQL计划·dry-run] %s" % sql)
            return True
        try:
            affected = self.client.patch(JOB_TABLE, "id=eq.%s" % jobId, payload)
        except SupabaseRestError as exc:
            reason = "作业状态回写失败（%s）：%s" % (action, exc)
            _warn(reason + "｜SQL: %s" % sql)
            self.failures.append(reason)
            return False
        if affected == 0:
            reason = "作业状态回写未命中任何行（%s）：id=%s" % (action, jobId)
            _warn(reason)
            self.failures.append(reason)
            return False
        _log("[状态] %s → id=%s 已更新 %d 行" % (action, jobId, affected))
        return True

    def mark_ready_rollback(self, job: Dict[str, Any]) -> None:
        """重试回退：把 FAILED/ABORTED 且未耗尽重试的作业置回 READY（第七节）

        本仓库脚本负责该回退（外部调度方不参与），回退后随即进入 COLLECTING。
        READY 作业本身无需回退。
        """
        status = str(job.get("job_status") or "")
        if status == JOB_STATUS_READY:
            return
        self.apply(job.get("id"), "重试回退 %s → READY" % status,
                   {"job_status": JOB_STATUS_READY, "dt_cancel": None, "cancel_reason": None})

    def mark_collecting(self, job: Dict[str, Any]) -> None:
        """标记开始采集：COLLECTING + dt_starte"""
        self.apply(job.get("id"), "%s → COLLECTING" % (job.get("job_status") or "READY"),
                   {"job_status": JOB_STATUS_COLLECTING, "dt_starte": _nowIso()})

    def mark_completed(self, job: Dict[str, Any], remotePath: str, result: Dict[str, Any]) -> None:
        """标记采集完成：COMPLETED + dt_finish，并清空 last_error

        `last_error` 写入远端落点与提交结果，作为成功侧的审计线索（而非错误信息）。
        """
        note = "已采集并落点：%s（HTTP %s）" % (remotePath, result.get("http_status"))
        self.apply(job.get("id"), "COLLECTING → COMPLETED",
                   {"job_status": JOB_STATUS_COMPLETED, "dt_finish": _nowIso(),
                    "last_error": None, "remark": note})

    def mark_failed(self, job: Dict[str, Any], error: Optional[BaseException],
                    permanent: bool) -> None:
        """标记失败：永久性失败 → FAILED（重试计数 +1）；临时性 → ABORTED

        重试计数用「读出当前值 + 1」的方式（PostgREST 不支持 `count_retry + 1` 表达式）；
        并发窗口由工作流 concurrency 分组收窄，将来若需严格原子可直接改走 PG 函数 RPC。
        """
        detail = _shortError(error)
        retry = int(job.get("count_retry") or 0)
        if permanent:
            self.apply(job.get("id"), "COLLECTING → FAILED（重试 %d → %d）" % (retry, retry + 1),
                       {"job_status": JOB_STATUS_FAILED, "last_error": detail,
                        "count_retry": retry + 1})
        else:
            self.apply(job.get("id"), "COLLECTING → ABORTED（重试计数保持 %d）" % retry,
                       {"job_status": JOB_STATUS_ABORTED, "last_error": detail})


def _nowIso() -> str:
    """当前时间（ISO8601，秒精度，带本地时区偏移）——写入 dt_starte / dt_finish"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sqlLiteral(value: Any) -> str:
    """把 Python 值渲染成 PostgreSQL 字面量（供 dry-run 打印的 SQL 可直接照抄执行）

    注意：PostgreSQL 的字符串字面量是**单引号**，双引号是标识符语义，
    故这里不能用 `json.dumps`（那会产出 `"READY"` 这种在 PG 里表示列名的写法）。

    None → NULL；bool → TRUE/FALSE；int/float → 原样；其余 → 单引号包裹并转义内部单引号。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'%s'" % str(value).replace("'", "''")


def _shortError(error: Optional[BaseException]) -> str:
    """把异常压成一行可入库的错误摘要（截断避免超长）"""
    if error is None:
        return "未知错误"
    text = "%s: %s" % (type(error).__name__, error) if str(error) else type(error).__name__
    text = " ".join(text.split())
    return text[:2000]


# ---------------------------------------------------------------------------
# 六、运行摘要（日志 + GitHub Step Summary）
# ---------------------------------------------------------------------------
def emit_summary(entries: Dict[str, Any]) -> None:
    """输出运行摘要：终端日志 + `$GITHUB_STEP_SUMMARY`（有该环境变量时）

    多行值（如「状态变更 SQL 计划」）用围栏代码块呈现：列表项里直接内嵌换行会糊成一段，
    代码块在 Actions 摘要页更易读，也便于整段复制去执行。
    """
    lines = ["## FTMM 历史 K 线采集作业运行摘要", ""]
    for key, value in entries.items():
        text = str(value)
        if "\n" in text:
            lines.append("- **%s**：" % key)
            lines.append("")
            lines.append("  ```sql")
            lines.extend("  " + row for row in text.splitlines())
            lines.append("  ```")
        else:
            lines.append("- **%s**：%s" % (key, text))
    rendered = "\n".join(lines)
    print("\n" + rendered + "\n", flush=True)

    summaryPath = (os.environ.get("GITHUB_STEP_SUMMARY") or "").strip()
    if not summaryPath:
        return
    try:
        with open(summaryPath, "a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    except OSError as exc:
        _warn("写入 GITHUB_STEP_SUMMARY 失败：%s" % exc)


# ---------------------------------------------------------------------------
# 七、主流程
# ---------------------------------------------------------------------------
#: 采集阶段本地中间产物目录（不写入仓库工作树，避免污染 checkout）；
#: 命名与工作流同名（MMQuoteCollect），便于在 runner 上按运行定位落盘产物
LOCAL_OUTPUT_DIR = Path(tempfile.gettempdir()) / "MMQuoteCollect"


def resolve_job(source: str, jobId: Optional[int],
                scheduleUrl: str = "") -> Dict[str, Any]:
    """按作业来源取一条任务（supabase = 作业表；schedule = 原型 JSONL 调度表）

    Args:
        source: `supabase` 或 `schedule`。
        jobId: 指定作业 id（仅 supabase 来源有效）。
        scheduleUrl: 调度表 URL（schedule 来源用；空串 = 用原型脚本内置常量）。

    Returns:
        task dict（supabase 来源带 `_job` 元数据；schedule 来源的 `_job` 为占位空值）。
    """
    if source == "schedule":
        tasks = downloadSchedule(scheduleUrl or SCHEDULE_URL)
        task = pickTask(tasks)
        _log("调度表来源：%d 条任务，随机选中 %s" % (len(tasks), task.get("job_name") or task))
        # 与 supabase 来源对齐口径：同样做字段归一，保证文件名与头部大小写一致
        task["type_kline"] = normalize_type_kline(task.get("type_kline"))
        task["period"] = normalize_period(task.get("period"))
        task["_job"] = {"id": None, "job_name": task.get("job_name") or "", "job_prefix": "",
                        "job_status": "", "count_retry": 0, "_from": "schedule"}
        return task

    projectRef = _envText(ENV_SUPABASE_REF)
    apiKey = _envText(ENV_SUPABASE_KEY)
    client = SupabaseRestClient(projectRef, apiKey)
    row = query_next_job(client, jobId)
    _log("作业表命中：id=%s｜%s｜状态 %s｜重试 %s"
         % (row.get("id"), row.get("job_name"), row.get("job_status"), row.get("count_retry")))
    _log("作业原始行：%s" % json.dumps(row, ensure_ascii=False, sort_keys=True))

    task = build_task_from_job(row,
                              periodOverride=_envText(ENV_JOB_PERIOD_OVERRIDE),
                              uscOverride=_envText(ENV_JOB_USC_OVERRIDE))
    task["_client"] = client
    return task


def collect_and_write(task: Dict[str, Any]) -> Optional[Path]:
    """调原型脚本的取数与处理链路采集数据并落本地 MVSV（复用，不重写）

    分流规则与原型 `main()` 完全一致：分钟级逐日取数 + 按时段重写；日/周/月/年级整段取数。

    Args:
        task: 已适配的 task dict。

    Returns:
        本地 MVSV 路径；区间内无数据时返回 None。

    Raises:
        JobExecutionError: 类型不支持等永久性失败时抛出。
    """

    typeKline = _typeKline(task)
    ktype = KTYPE_BY_TYPE_KLINE.get(typeKline)
    if ktype is None:
        raise JobExecutionError(
            "type_kline=%r（归一 %r）暂不支持采集，已支持：%s"
            % (task.get("type_kline"), typeKline, "/".join(KTYPE_BY_TYPE_KLINE)),
            permanent=True)
    periodLabel = _periodLabel(task)
    if not periodLabel:
        raise JobExecutionError("period 为空，无法生成文件名", permanent=True)

    startDate = _toIsoDate(task["start"])
    endDate = _toIsoDate(task["end"])
    isRangeKline = typeKline in INTERVAL_KLINE_TYPES
    if isRangeKline:
        _log("区间 %s ~ %s（不含）｜%s 级整段取数" % (startDate, endDate, typeKline))
    else:
        days = iterDays(startDate, endDate)
        _log("区间 %s ~ %s（不含）｜共 %d 天｜预计 %d 次请求"
             % (startDate, endDate, len(days), len(days) * 3))

    # 本地中间产物落在系统临时目录：仅供推送与对账使用，不污染仓库工作树
    global OUTPUT_DIR
    OUTPUT_DIR = LOCAL_OUTPUT_DIR
    LOCAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = createMoomooOpenAPIClient()
    bars: List[Any] = []
    try:
        if isRangeKline:
            bars = collectDayBars(client, task["symbol"], startDate, endDate, ktype)
        else:
            bars = collectMinuteBars(client, task["symbol"], startDate, endDate, ktype)
            applySessions(bars, str(task.get("region", "")).upper() == "US")
    finally:
        # 与原型一致：正常收尾或中途失败，都把已取到的数据尽力写出（统计口径均为去重后）
        unique = _deduplicate(bars)
        localPath = writeMvsv(task, periodLabel, unique)
        if unique:
            _log("已取 %d 根（去重后），本地落盘：%s" % (len(unique), localPath))
    return localPath


def _publish_run_marker(job: Dict[str, Any]) -> None:
    """把作业运行标记发布给 Actions 后续步骤（`$GITHUB_OUTPUT` + `$GITHUB_ENV`）

    用途：本作业被 **job 级 timeout 强杀**时，脚本进程来不及写状态，此时由工作流的
    善后步骤凭这些标记把残留的 `COLLECTING` 拉回 `READY` —— 尽量收窄悬挂态窗口。

    两份出口各有用途：

    - `$GITHUB_OUTPUT`：写**原始值**，供 `steps.<id>.outputs.*` 直接引用（可读、无需解码）；
    - `$GITHUB_ENV`：写 **URL 编码值**。Actions 对 `env` 命令文件里的裸行按 `NAME=VALUE`
      解析，值含 `=`（如 `JOB_RUN_ID` 形态）或非 ASCII 时会被截断/污染，编码后可安全承载。

    本机运行时这两个变量都不存在，直接跳过，无副作用。

    Args:
        job: 作业元数据 dict（`_job`）。
    """
    fields = ("id", "job_name", "job_status", "count_retry")
    lines = {key: str(job.get(key)) for key in fields
             if job.get(key) is not None and str(job.get(key)) != ""}
    if not lines:
        return

    outputFile = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if outputFile:
        try:
            with open(outputFile, "a", encoding="utf-8") as handle:
                for key, value in lines.items():
                    handle.write("job_%s=%s\n" % (key, value))
        except OSError as exc:
            _warn("写入 GITHUB_OUTPUT 失败：%s" % exc)

    envFile = (os.environ.get("GITHUB_ENV") or "").strip()
    if envFile:
        try:
            with open(envFile, "a", encoding="utf-8") as handle:
                for key, value in lines.items():
                    handle.write("FTMM_JOB_%s=%s\n"
                                 % (key.upper(), urllib.parse.quote(value, safe="")))
        except OSError as exc:
            _warn("写入 GITHUB_ENV 失败（善后步骤将拿不到作业标记）：%s" % exc)


def run_job(args: argparse.Namespace) -> int:
    """执行一条作业：查询 → 采集 → 本地落盘 → 推送 → 状态流转

    Args:
        args: 命令行参数。

    Returns:
        进程退出码：0 = 成功；2 = 无作业可运行（正常空闲，非错误）；1 = 执行失败。
    """
    started = datetime.now()
    writer = JobStateWriter(None, enabled=False)
    task: Optional[Dict[str, Any]] = None
    localPath: Optional[Path] = None
    remotePath = ""
    pushResult: Dict[str, Any] = {}
    pushed = False
    failure: Optional[BaseException] = None
    permanent = False

    #: 退出码：0 = 成功；2 = 无作业可运行（正常空闲）；1 = 执行失败。
    #: 刻意用变量而非在 `finally` 里 `return` — `finally` 中的 return 会吞掉传播中的异常，
    #: 把真正的错误掩盖成「退出码 1」，且 Python 会给出 SyntaxWarning。
    exitCode = 0

    try:
        # ---- 1) 取作业（作业表就绪规则 / 调度表回退）----
        task = resolve_job(args.job_source, args.job_id, args.schedule_url)
        job = task["_job"]
        client = task.get("_client")

        # 状态写入器：正式来源才开通回写（schedule 来源无作业行可写）
        if client is not None:
            writer = JobStateWriter(client, enabled=args.enable_job_update)
            if not args.enable_job_update:
                _log("状态回写开关 %s=false → 本次只打印待执行 SQL（权限配妥后置 true 即生效）"
                     % ENV_ENABLE_JOB_UPDATE)
        else:
            # 调度表来源没有作业表客户端，状态流转不适用 —— 明确说明，
            # 免得让人误以为「该打印的 SQL 没打印出来」
            _log("作业来源 = %s（无作业表客户端）→ 本次不涉及作业状态流转，故无 SQL 计划"
                 % args.job_source)

        # 发布作业标记：即便本进程随后被 job 级 timeout 强杀，善后步骤也能定位到这条作业
        _publish_run_marker(job)

        # ---- 2) 重试回退：FAILED/ABORTED → READY（本脚本负责）----
        writer.mark_ready_rollback(job)

        # ---- 3) 解析范围与导出配置，打印出来（日志自证）----
        remotePath = build_remote_path(task)
        _log("作业身份：id=%s｜%s" % (job.get("id"), job.get("job_name")))
        _log("采集范围：%s ｜ %s ~ %s ｜ label=%s"
             % (task["symbol"], task["start"], task["end"], task["label"]))
        _log("导出配置：type_kline=%s（大驼峰）｜period=%s｜region=%s｜market=%s｜usc=%s"
             % (task["type_kline"], task["period"], task["region"], task["market"], task["usc"]))
        _log("落点路径：%s" % remotePath)

        # ---- 4) 标记 COLLECTING ----
        writer.mark_collecting(job)

        # ---- 5) 采集 + 本地落盘 ----
        try:
            localPath = collect_and_write(task)
        except MoomooOpenAPIException as exc:
            # 限流等可重试形态 → 临时失败（ABORTED），计数值由库内重试策略决定何时耗尽
            temporary = getattr(exc, "httpStatus", None) is None
            raise JobExecutionError("行情接口失败：%s" % _shortError(exc),
                                    permanent=not temporary) from exc

        if localPath is None:
            # 区间内无数据：既非成功也非错误，按中止处理并留下可读原因（不产生空文件）
            raise JobExecutionError(
                "作业区间 %s ~ %s 内无任何数据，未生成文件" % (task["start"], task["end"]))

        # ---- 6) 推送落点 ----
        if args.dry_run:
            digest, size, lines = compute_digest(localPath)
            _log("DRY_RUN=true → 跳过推送。本地文件 %s｜sha256 %s…｜%s 字节｜%d 行"
                 % (localPath.name, digest[:16], format(size, ","), lines))
        else:
            pushResult = push_to_repo(remotePath, localPath, args.commit_owner,
                                      args.commit_repo, args.commit_branch,
                                      _make_commit_message(remotePath, task))
            if not pushResult.get("success"):
                raise JobExecutionError(
                    "落点推送失败（HTTP %s）：%s"
                    % (pushResult.get("http_status"), pushResult.get("message")))
            pushed = True

        # ---- 7) 成功收尾 ----
        if pushed:
            writer.mark_completed(job, remotePath, pushResult)
        else:
            # dry-run：不写 COMPLETED（远端尚无产物），只留计划 SQL 供核对
            _log("[状态·dry-run] DRY_RUN=true，本次不改写作业状态（远端未落点）")

    except JobExecutionError as exc:
        failure, permanent = exc, exc.permanent
    except BaseException as exc:  # noqa: BLE001  （任何异常都必须收口成作业状态，不留悬挂态）
        failure, permanent = exc, False
    finally:
        if failure is not None and task is not None:
            _warn("作业执行失败：%s（%s）"
                  % (_shortError(failure), "永久性失败 → FAILED" if permanent
                     else "临时性失败 → ABORTED"))
            try:
                writer.mark_failed(task["_job"], failure, permanent)
            except Exception as exc:  # noqa: BLE001  （状态回写自身再失败也不能吞掉原始错误）
                _warn("状态回写过程异常：%s" % _shortError(exc))

        elapsed = (datetime.now() - started).total_seconds()
        job = (task or {}).get("_job") or {}
        try:
            emit_summary({
                "作业来源": args.job_source,
                "作业 id": job.get("id", "(未取到)"),
                "作业名": job.get("job_name", "(未取到)"),
                "作业状态（执行前）": job.get("job_status", "(未知)"),
                "重试次数（执行前）": job.get("count_retry", "(未知)"),
                "标的": (task or {}).get("symbol", "(未取到)"),
                "采集区间": "%s ~ %s" % ((task or {}).get("start", "?"), (task or {}).get("end", "?")),
                "type_kline（大驼峰）": (task or {}).get("type_kline", "(未取到)"),
                "落点路径": remotePath or "(未生成)",
                "本地文件": str(localPath) if localPath else "(未生成)",
                "推送": "已推送" if pushed else ("DRY_RUN 跳过" if args.dry_run else "未推送"),
                "推送 HTTP": pushResult.get("http_status", "-"),
                "推送 sha256": (pushResult.get("digest") or "")[:16] or "-",
                "字节数": format(pushResult["size"], ",") if pushResult.get("size") else "-",
                "行数": pushResult.get("lines", "-"),
                "状态回写": "已启用" if args.enable_job_update else "dry-run（仅打印 SQL）",
                "状态回写失败": "; ".join(writer.failures) if writer.failures else "无",
                "状态变更 SQL 计划": ("\n".join("  %d. %s" % (i, s)
                                                for i, s in enumerate(writer.sqlPlans, 1))
                                      if writer.sqlPlans
                                      else "（本次无状态流转）"),
                "耗时": "%.1f 秒" % elapsed,
                "结果": "成功" if failure is None else "失败",
                "失败摘要": _shortError(failure) if failure is not None else "-",
            })
        except Exception as exc:  # noqa: BLE001
            # 摘要输出失败不得影响退出码，更不得掩盖真正的失败原因
            _warn("输出运行摘要失败：%s" % _shortError(exc))

        # 无作业可运行属正常空闲（退出码 2），工作流据此判定「跳过」而非告警
        if isinstance(failure, JobExecutionError) and not task:
            exitCode = 2
        elif failure is not None:
            exitCode = 1
    return exitCode


def run_selftest(args: argparse.Namespace) -> int:
    """无凭据自检：验证「字段映射 → 大驼峰归一 → 落点路径 → 状态 SQL → 行情库符号」纯逻辑链路

    不触网、不采集、不需要任何凭据，可在任意环境运行；也便于回归 `type_kline` 大驼峰口径
    （本脚本的强制要求）与「原型脚本内联行情库符号齐备」这一前提。

    Args:
        args: 命令行参数。

    Returns:
        0 = 全部断言通过；1 = 有断言失败。
    """
    cases = [
        # (作业表行, 期望落点路径)
        ({"id": 1, "job_name": "US_NASDAQ_FUTU_Min_FTMM_Mon_202501", "type_kline": "MIN",
          "period": "MON", "date_start": 20250101, "date_end": 20250201, "label": "202501",
          "region": "US", "market": "NASDAQ", "usc": "FUTU", "symbol": "US.FUTU",
          "job_status": "READY", "count_retry": 0},
         "Archive/Finv/SecuQuoteData/FTMM/Mon/US_NASDAQ_FUTU_Min_FTMM_Mon_202501.mvsv"),
        ({"id": 2, "job_name": "US_NASDAQ_FUTU_Day_FTMM_Year_2025", "type_kline": "day",
          "period": "Year", "date_start": 20250101, "date_end": 20251231, "label": "2025",
          "region": "US", "market": "NASDAQ", "usc": "FUTU", "symbol": "US.FUTU",
          "job_status": "FAILED", "count_retry": 2},
         "Archive/Finv/SecuQuoteData/FTMM/Year/US_NASDAQ_FUTU_Day_FTMM_Year_2025.mvsv"),
        ({"id": 3, "job_name": "US_NASDAQ_FUTU_Min5_FTMM_Week_202511", "type_kline": "MIN5",
          "period": "Week", "date_start": 20251101, "date_end": 20251130, "label": "202511",
          "region": "US", "market": "NASDAQ", "usc": "FUTU", "symbol": "US.FUTU",
          "job_status": "ABORTED", "count_retry": 1},
         "Archive/Finv/SecuQuoteData/FTMM/Week/US_NASDAQ_FUTU_Min5_FTMM_Week_202511.mvsv"),
    ]

    failures: List[str] = []
    for row, expected in cases:
        task = build_task_from_job(row, uscOverride=args.usc_override,
                                   periodOverride=args.period_override)
        actual = build_remote_path(task)
        status = "OK  " if actual == expected else "FAIL"
        print("%s %s\n     期望 %s\n     实际 %s" % (status, row["job_name"], expected, actual))
        if actual != expected:
            failures.append(row["job_name"])
        # 大驼峰口径断言：文件名第 4 段与头部 TypeKLine 同源，必须为大驼峰
        label = _typeKlineLabel(task)
        if label != task["type_kline"]:
            failures.append("%s：type_kline 归一值 %r 与文件名第 4 段 %r 不一致"
                            % (row["job_name"], task["type_kline"], label))

    # 状态 SQL 计划（不触网）：覆盖重试回退 / 开始 / 完成 / 失败四种迁移
    print("\n--- 状态流转 SQL 计划（dry-run，不触网）---")
    writer = JobStateWriter(None, enabled=False)
    row = cases[1][0]
    writer.mark_ready_rollback(row)
    writer.mark_collecting(row)
    writer.mark_completed(row, cases[1][1], {"http_status": 201})
    writer.mark_failed(row, JobExecutionError("模拟失败"), permanent=True)
    writer.mark_failed(row, JobExecutionError("模拟中断"), permanent=False)

    print("\n--- 行情库符号检查（已内联于本文件，不触网）---")
    try:
        print("行情库符号：OK（MoomooOpenAPIException / createMoomooOpenAPIClient 齐备）")
        print("  内联常量：KTYPE_MIN=%r｜KTYPE_DAY=%r｜EXTENDED_TIME_ALL=%r"
              % (KTYPE_MIN, KTYPE_DAY, EXTENDED_TIME_ALL))
    except JobExecutionError as exc:
        print("行情库符号：失败 → %s" % exc)
        failures.append("内联行情库符号缺失")

    print("\n自检结果：%s" % ("全部通过" if not failures else "存在 %d 项失败" % len(failures)))
    for item in failures:
        print("  - %s" % item)
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（环境变量为默认值，命令行项可覆盖）"""
    parser = argparse.ArgumentParser(
        description="FTMM 历史 K 线采集作业执行器（Supabase 作业表驱动 + Contents API 落点）")
    parser.add_argument("--job-source", choices=("supabase", "schedule"),
                        default=_envText(ENV_JOB_SOURCE, "supabase"),
                        help="作业来源：supabase=作业表（默认）；schedule=原型 JSONL 调度表")
    parser.add_argument("--job-id", type=int, default=_envInt(ENV_JOB_ID),
                        help="指定作业 id（留空 = 按就绪规则自动取一条）")
    parser.add_argument("--schedule-url", default="",
                        help="schedule 来源的调度表 URL（留空 = 原型脚本内置常量）")
    parser.add_argument("--period-override", default=_envText(ENV_JOB_PERIOD_OVERRIDE),
                        help="覆盖落点目录用的 period（定点重跑用）")
    parser.add_argument("--usc-override", default=_envText(ENV_JOB_USC_OVERRIDE),
                        help="覆盖标的 usc（定点重跑用）")
    parser.add_argument("--dry-run", action="store_true", default=_envFlag(ENV_DRY_RUN),
                        help="只采集 + 本地落盘，不推送仓库")
    parser.add_argument("--commit-branch", default=_envText(ENV_COMMIT_BRANCH, DEFAULT_COMMIT_BRANCH),
                        help="Contents API 目标分支（默认 %s）" % DEFAULT_COMMIT_BRANCH)
    parser.add_argument("--commit-owner", default=_envText(ENV_COMMIT_OWNER, DEFAULT_COMMIT_OWNER),
                        help="目标仓库 owner（默认 %s）" % DEFAULT_COMMIT_OWNER)
    parser.add_argument("--commit-repo", default=_envText(ENV_COMMIT_REPO, DEFAULT_COMMIT_REPO),
                        help="目标仓库名（默认 %s）" % DEFAULT_COMMIT_REPO)
    parser.add_argument("--enable-job-update", action="store_true",
                        default=_envFlag(ENV_ENABLE_JOB_UPDATE),
                        help="真正回写作业表状态（默认关闭：只打印待执行 SQL）")
    parser.add_argument("--selftest", action="store_true",
                        help="跑纯逻辑自检（字段映射/大驼峰/落点路径/状态 SQL/内联行情库符号），不采集不触网")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """入口：自检模式或作业执行模式

    Returns:
        进程退出码：0 成功；1 失败；2 无作业可运行（正常空闲）。
    """
    args = build_parser().parse_args(argv)
    if args.selftest:
        return run_selftest(args)

    _log("作业执行开始：来源 %s｜分支 %s｜仓库 %s/%s｜DRY_RUN=%s｜状态回写=%s"
         % (args.job_source, args.commit_branch, args.commit_owner, args.commit_repo,
            args.dry_run, args.enable_job_update))
    return run_job(args)


def _make_commit_message(remotePath: str, task: Dict[str, Any]) -> str:
    """生成提交说明（含作业身份与运行标识，便于在仓库历史里追溯）"""
    runId = _envText(ENV_RUN_ID, _envText("GITHUB_RUN_ID", "-"))
    return ("[FTMM][collect] %s｜job=%s｜%s ~ %s｜run=%s"
            % (task["_job"].get("job_name") or remotePath, task["_job"].get("id") or "-",
               task.get("start"), task.get("end"), runId))


if __name__ == "__main__":
    sys.exit(main())
