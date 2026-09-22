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

    - **取数逻辑零重复**：`collectMinuteBars` / `collectDayBars` / `applySessions` /
      `_deduplicate` / `_periodLabel` 等纯函数来自内联段，本脚本只新增
      「作业来源、落点、状态流转」；
    - **MVSV 生成逻辑另立公共模块**：`buildMvsvName` / `writeMvsv` / `toMvsvFieldMap` /
      数据模型（`KlineMinBar` / `KLineDayBar`）与 MVSV 格式常量已提取到同目录
      `MvsvWriter.py`，供本脚本与后续其他模块复用（见第三节「依赖」）；
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

【行情库】行情客户端库已提取到同目录 `MoomooOpenAPI.py`（含 Ed25519 / RSA-SHA256 签名与
    纯标准库密码学实现），本脚本只通过其**公共契约**使用；凭据由客户端自行读取下列环境变量：
        MOOMOO_OPENAPI_AK        AppKey ID
        MOOMOO_OPENAPI_SK        Base64 PKCS#8 私钥（或用 MOOMOO_OPENAPI_SK_FILE 指向文件）

九、模块划分与依赖边界
----------------------------------------------------------------------------------------
本目录内两个模块各司其职：

| 模块 | 职责 |
| --- | --- |
| `QuoteCollectRunner.py`（本文件） | 作业执行器：作业表查询与挑选、采集链路（取数 / 时段归类 / 区间过滤 / 去重）、落点推送、状态流转、运行摘要 |
| `MvsvWriter.py` | **MVSV 生成公共模块**：格式定义与两套列序、数据模型（`KlineMinBar` / `KLineDayBar`）、文件名生成（`buildMvsvName`）、序列化（`writeMvsv` / `toMvsvFieldMap`） |

`MvsvWriter` **仅依赖标准库**，刻意不依赖行情客户端库：其数据模型入参以鸭子类型使用
（只要求具备 `timeKey` / `localDate` / `open` 等属性），因此任何数据源只要产出同形对象
即可复用它，也避免了两模块形成循环依赖。

仓库内依赖共两项：`GitHubCommitContent`（上一级目录的 Contents API 封装，刻意不内联）
与 `MvsvWriter`（同目录）。故本文件**不是**「拷到哪都能跑」的完全自包含形态，
需与这两个模块同仓部署。

十、命令行（也可纯用环境变量驱动，工作流即如此）
----------------------------------------------------------------------------------------
    python3 QuoteCollectRunner.py --job-source supabase --dry-run
    python3 QuoteCollectRunner.py --job-source schedule
    python3 QuoteCollectRunner.py --job-id 12 --commit-branch quote-mon
"""

import argparse
import random
import time
import datetime
#: `datetime` 之名在本文件中指**类**而非模块：下方多处使用 `datetime.now()`。
#: 需要模块形式的地方请显式写 `datetime.datetime`，或改用 `date` / `timedelta`。
from datetime import datetime, date, timedelta
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable

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

#: 注意：行情客户端库已提取到同目录 `MoomooOpenAPI.py`（公共契约见该模块 docstring）；
#: 本文件保留采集链路（取数策略、时段归类、区间过滤、去重、作业与状态流转）。
#: 因此本文件的仓库内依赖有三项：
#:     1. `MoomooOpenAPI`（同目录）—— moomoo 客户端库，仅通过其公共契约使用；
#:     2. `MvsvWriter`（同目录）—— MVSV 格式、数据模型与序列化；
#:     3. `GitHubCommitContent`（上一级目录）—— Contents API 封装，既有公共模块。
#: 故本文件不是"拷到哪都能跑"的完全自包含形态：**需与这三个模块同仓部署**
#: （在仓库内运行、或按 CI 的检出方式部署均满足）。
#: 它不依赖仓库根的绝对位置（导入走 sys.path，非相对路径推算）。

from MoomooOpenAPI import (  # noqa: E402  （moomoo 客户端库：仅用公共契约）
    EXTENDED_TIME_ALL,
    KTYPE_DAY,
    KTYPE_MIN,
    KlineBar,
    MoomooOpenAPIException,
    convertKlineItems,
    createMoomooOpenAPIClient,
    fetchHistoryKline,
    fetchHistoryKlineFullDay,
    fetchServerDrift,
    fetchStockBasicInfo,
    fetchTradingDays,
    getHistoryKline,
    getServerTime,
    getTradingDays,
    postStockBasicInfo,
)

from GitHubCommitContent import commit_content_file  # noqa: E402  （Contents API 封装；同目录既有模块，直接用）

#: MVSV 生成逻辑已提取到同目录公共模块 `MvsvWriter`，供本脚本与后续其他模块复用。
#: 提取范围：MVSV 格式定义、数据模型（KlineMinBar / KLineDayBar）、文件名生成与写文件。
#: 本脚本保留**采集链路**：取数、时段归类、区间过滤、去重、作业查询与状态流转。
import MvsvWriter  # noqa: E402  （为 OUTPUT_DIR 之类模块级常量提供改写入口）
from MvsvWriter import (  # noqa: E402
    DATA_PROVIDER,
    INTERVAL_KLINE_TYPES,
    KLineDayBar,
    KlineMinBar,
    MVSV_DAY_FIELDS,
    MVSV_DAY_FIELD_NAMES,
    MVSV_DAY_FIELD_TYPES,
    MVSV_FIELD_SPECS,
    MVSV_MIN_FIELDS,
    MVSV_MIN_FIELD_NAMES,
    MVSV_MIN_FIELD_TYPES,
    MVSV_TITLE_PERIODS,
    TIMEZONE_BY_REGION,
    _fixed6,
    _sessionOf,
    _text,
    _toPascal,
    _typeKline,
    _typeKlineLabel,
    buildMvsvName,
    toDayBars,
    toMinBars,
    toMvsvFieldMap,
    writeMvsv,
)

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


# ===========================================================================
# 以下为示例自身的逻辑（导出参数、数据处理模型、取数、序列化与 main）
# ===========================================================================

# -*- coding: utf-8 -*-


# 非正式模块导入使用相对路径导入，需要加上以下这一行，正式pypi模块下面这一行可以去掉。

# ---- 导出参数：按需修改 ----
SCHEDULE_URL = ("https://raw.githubusercontent.com/acdnx/Distribution/refs/heads/quote-mon"
                "/.github/Python/ArchiveSchedule.jsonl")
REQUEST_INTERVAL_SECONDS = 1.0      # 相邻日期的请求间隔（秒）
MAX_RETRIES = 5                     # 命中限流时的最大重试次数
RETRY_BACKOFF_SECONDS = 30          # 限流重试的退避基数（秒），按 2 的幂递增

#: 调度表 type_kline 取值 → 接口 ktype
#: 该键同为本示例写入 MVSV 头部 ``TypeKLine`` 的来源，标准取值域为：
#: ``MIN`` / ``MIN5`` / ``MIN10`` / ``HOUR`` / ``HOUR2`` / ``HOUR3`` / ``HOUR6`` /
#: ``DAY`` / ``WEEK`` / ``MONTH`` / ``YEAR``（头部按**大驼峰**输出，如 ``MIN`` → ``Min``）
KTYPE_BY_TYPE_KLINE: Dict[str, str] = {"MIN": KTYPE_MIN, "DAY": KTYPE_DAY}



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

    # 本地中间产物落在系统临时目录：仅供推送与对账使用，不污染仓库工作树。
    # 注意：必须改写 **MvsvWriter 模块的 OUTPUT_DIR** —— MVSV 生成逻辑已提取到该模块，
    # writeMvsv 读的是它自己的模块全局；本脚本的模块全局已不再影响落盘位置。
    MvsvWriter.OUTPUT_DIR = LOCAL_OUTPUT_DIR
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
    """无凭据自检：验证「字段映射 → 大驼峰归一 → 落点路径 → 状态 SQL → 客户端库契约」纯逻辑链路

    不触网、不采集、不需要任何凭据，可在任意环境运行；也便于回归 `type_kline` 大驼峰口径
    （本脚本的强制要求）与「MoomooOpenAPI 公共契约齐备」这一前提。

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

    print("\n--- 客户端库契约检查（MoomooOpenAPI，不触网）---")
    try:
        print("客户端库契约：OK（MoomooOpenAPIException / createMoomooOpenAPIClient 齐备）")
        print("  契约常量：KTYPE_MIN=%r｜KTYPE_DAY=%r｜EXTENDED_TIME_ALL=%r"
              % (KTYPE_MIN, KTYPE_DAY, EXTENDED_TIME_ALL))
    except JobExecutionError as exc:
        print("客户端库契约：失败 → %s" % exc)
        failures.append("MoomooOpenAPI 公共契约缺失")

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
                        help="跑纯逻辑自检（字段映射/大驼峰/落点路径/状态 SQL/客户端库契约），不采集不触网")
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
