#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SupabaseImportWeekMvsv —— 从 Supabase PG 导入「某证券某整周」分钟行情为 .mvsv
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
站在本仓库（acdnx/Distribution）的角度，本工具做的是**数据导入**：把 Supabase PG 表
public.finv_quote_secu_kline_min 里「某个证券 + 某个整周」的分钟级行情读出来，转成符合
本仓 .mvsv 规范的文本，再经 GitHub Contents API 提交到本仓库的 quote 分支。

数据流向与 ACANX/Distribution 的 SupabaseSyncMvsv 相反（那条是 .mvsv → 入库）。

二、周的定义与编号
----------------------------------------------------------------------------------------
周的分界点在「UTC 时间的周六与周日之间的 0 点」，即：

    周 = [ 周日 00:00:00 UTC , 次周日 00:00:00 UTC )     半开区间

比 ISO 周（周一~周日）整体**提前一天**。编号仍按 ISO 周号取，取该周「周一」的 ISO 年周
（该周恰好含一个周一，且该周一与其周六同属一个 ISO 周）：

    ISO 周 Mon 2026-06-15 ~ Sun 2026-06-21  (2026-W25)
      ↓ 整体 −1 天
    本仓周 Sun 2026-06-14 ~ Sat 2026-06-20  →  yyyyWW = 202625

跨年处**必须用周一（或等价的周六）查，不能用周日起点查**：
    周 2026-12-27(日) ~ 2027-01-02(六) → 2026-W53 → 202653
    （拿周日起点 2026-12-27 查会得到 2026-W52，整体偏移一周）

**周的选取**：
    - 显式指定（`WEEK` / argv[2]）时用指定值；
    - 未指定时，取该证券**在库中最早一条记录（min(ts)）所在的那一周** —— 即从历史起点开始，
      每一次运行导出该证券的「第一周」。

取周约束：**周结束（次周日 00:00 UTC）距今必须满 14 天**（避开仍在被订正/补录的数据）。
显式指定的周若违反该约束 → **报错退出**（属调用方写错）；自动解析出的周若违反 → 该证券
**跳过并记日志**（属「历史还没攒够两周」的正常状态，不是错误）。

三、准入校验、查询与分页
----------------------------------------------------------------------------------------
【准入校验】只有**在 `finv_quote_secu` 中登记过**的证券才允许导出（ACANX 2026-09-15）：

    `finv_quote_secu` 是证券元数据登记表（主键 `usc`；`region` / `market` / `dt_create` /
    `dt_update` 非空，`timezone` 可空），其 `usc` 与 `finv_quote_secu_kline_min.usc` 同值。

    **该表是本工具的唯一名单**（ACANX 2026-09-15：不再维护第二处）：运行开头一次性取回全表
    （按主键翻页，见第十二节），既作准入名单，也供落点路径的 `region` / `market`
    （见第五节）与文件头的 `# Timezone`（见第四节）。

    - 取不到该 usc（**未登记**）  → 该证券**根本不在名单里**，不会被尝试；若源库存有数据，
      由第十二节的盘点逐只点名；
    - 已登记但 `timezone` 为空     → 跳过（文件头必须有 Timezone 值）；
    - 登记表**全量查询本身失败**   → 名单无从确定，**整轮硬失败退出**（不再逐证券重试）。

    前两种都**不产出文件** —— 宁可少导，也不能产出缺 Timezone 的半成品流向下游。

    > 历史：名单原先由「`SecuMetaMapping.jsonl` 定尝试范围 + 本表定放行」两份人工清单共同
    > 决定，两处不同步会让上游在采的证券**静默缺席**（连一条「跳过」都没有）。现已收敛为
    > 本表一处，`SecuMetaMapping.jsonl` 退役（保留在仓库内仅作历史对照，代码不再读取）。

【分页】Supabase Data API（PostgREST）默认一次最多回 1000 行，而 7×24 品种一周有 10080
分钟，故必须分页。采用 **keyset（游标）分页**：按 ts 升序，每页取 limit 行，下一页把
`ts=gte.<周起点>` 换成 `ts=gt.<上一页最后一行的 ts>`，直到某页不足 limit 行为止。

选 keyset 而非 offset 的理由：主键就是 (usc, ts)，同一 usc 下 ts 不会重复 ⇒ 游标严格单调，
**不漏行也不重行**；且正好走索引 idx_finv_quote_secu_secu_ts (usc, ts)。

四、输出文件规范（.mvsv）
----------------------------------------------------------------------------------------
23 行中英双段元信息头 + 1 空行 + 数据行（末行**无结尾换行**）：

    # 标题 / # 数据供应商 / # 字段 / # 字段名称 / # 字段类型 / # 计数 / # 采集时间 /
    # 证券代码 / # 地区 / # 市场 / # Timezone / # 备注            （中文段 12 行）
    # Title / # DataProvider / # Field / # FieldName / # FieldType / # Count /
    # FetchTime / # SecuCode / # Region / # Market / # Timezone  （英文段 11 行）

`# Timezone` 取自在 `finv_quote_secu` 中登记的该证券的 `timezone` 列（IANA 名，如
`Asia/Shanghai`），**两段写法相同**（ACANX 2026-09-15：键名 `# Timezone :`，插在 Market
字段之后）。取不到就不导出 —— 见第三节的准入校验。

数据行列序（与头部 # 字段 一致）：

    Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|ChangePrice|ChangePercent

注意 mvsv 的列序是 Open|Close|Low|High，与表定义 open|high|low|close **不一致**，不可照抄表序。

数值格式（对本仓既有 Day 文件实测 11 份 / 7952 行推出，2026-09-15）：
    - 所有数值列一律**去尾随零、不限定小数位**；
      这是「不丢精度」要求下的唯一安全做法 —— 限定小数位必然引入舍入。
      （既有文件里 ChangePercent 恒为 2 位，但那是上游的巧合，不是可依赖的规则）
    - 去尾零**不是**四舍五入，也不是截断：有效位一位不动，只是末尾的 0 不写；
      尾零去光后小数点也一起去掉（4539.000000 → 4539，不是 "4539."）。
    - Decimal.normalize() 会输出科学计数法（1E+1），故一律走 format(d, 'f')。
    - POST 解析时须用 json.loads(..., parse_float=Decimal)，否则 numeric 会先落到
      double 上，高精度小数在解析阶段就已丢失。
    - 空值一律写**空串**（连续两个 "|"）。

五、落点路径
----------------------------------------------------------------------------------------
    Data/Finv/SecuQuoteWeek/FT/{region}_{market}/{Code}/{region}_{market}_{Code}_MIN_FT_{yyyyWW}.mvsv

例（Code=IAU / region=US / market=ARCA / yyyyWW=202625）：
    Data/Finv/SecuQuoteWeek/FT/US_ARCA/IAU/US_ARCA_IAU_MIN_FT_202625.mvsv

与既有 Day 规范 Data/Finv/SecuQuote/FT/{Freq}/{Region}_{Market}/{Code}/… **七层同构**，
唯一差异是频率段与日期段（_Min_FT_yyyyMMdd ↔ _MIN_FT_yyyyWW）：_FT_ 段的位置、大小写
与既有 Day 文件完全一致，只有频率标记（Min/MIN）和周/日粒度不同。

Region / Market 取自登记表 `finv_quote_secu` 的 `region` / `market` 两列（见第三节）。

六、同名冲突规避
----------------------------------------------------------------------------------------
导出前先**探测目标分支上该文件是否已存在**；已存在则打印日志，并改用 `_N` 后缀规避：

    …/US_ARCA_IAU_MIN_FT_202625.mvsv      常规（不存在时）
    …/US_ARCA_IAU_MIN_FT_202625_1.mvsv    同名冲突时，N 从 1 起取第一个未占用的

**为什么是「另存」而不是「覆盖」**：commit_content 本身幂等（同路径＝覆盖更新），但覆盖会
让「上一份导出」无声消失。加后缀后每份导出各自留痕，下游可按 `_N` 分辨先后。

探测走 Contents API 的**目录列举**（一次请求拿到该证券目录下全部文件名），
而非对 base / `_1` / `_2` … 逐个探测 —— 后者在冲突多时要发 N 次请求。

「已存在」的判定基准是**目标分支当前的 HEAD**。注意 `Data/**` 会被每日的
DMDCBWD31MigrationFile 采集后删除，故文件被取走后就不再算冲突，会回到常规命名。

`_1.._99` 全部占用时**报错退出**（不产出文件），避免无声覆盖或无限增长。

七、导出后删源库（数据迁移）
----------------------------------------------------------------------------------------
在**确认 .mvsv 已提交成功**之后，可选地把源库中该证券该整周的数据删掉，实现
「按证券、按周」的数据迁移。开关：

    SUPABASE_ENABLE_DELETE   "true"/"1"/"yes"/"on" 开启；**默认关**（安全模式：只报数不删）

取值写错（如 "ture"）一律倒向「关」，不会因拼错而意外删库。开关名与语义对齐
姊妹仓库 ACANX/Distribution 的 SupabaseSyncMvsv.py。

删除区间与取数区间**共用同一个 build_week_filter**，杜绝「导出的没删、删的没导出」。

三道闸门（任一不过即**不删**，并计入「部分成功」）：

    1. 提交必须已成功 —— 提交失败时根本走不到这一步；
    2. **删除前复核**：重查该区间现状，其 ts 集合必须与本次导出的完全一致。
       多一行（导出后又有新数据写入）或少一行（导出后被别处删过）都拒绝删除 ——
       **宁可少删，不可错删**；
    3. **删除后复核**：重查该区间须为空，否则报「未删净」。

`SupabaseRestClient.delete()` 另有一道保险丝：过滤串里必须同时出现 `usc=eq.` 与 `ts` 的
上下界，缺一即拒绝发请求（PostgREST 允许无过滤 DELETE，那会清空整表）。

八、环境变量（凭据一律经环境注入，严禁写进源码或日志）
----------------------------------------------------------------------------------------
    SUPABASE_PROJECT_REF   Supabase 项目引用（必填）
    SUPABASE_KEY           Supabase API 密钥（service-role；必填，不落日志）
    GIT_COMMIT_TOKEN       GitHub 令牌（提交 .mvsv 用；必填）
    SUPABASE_PAGE_SIZE     单页行数（默认 1000）
    SUPABASE_ENABLE_DELETE 删除源库开关（默认关；见第七节）
    SECU_CODE              证券代码，多个以逗号分隔（如 IAU 或 IAU,GLD）；
                           **留空则取 `finv_quote_secu` 登记表中的全部 usc**
    WEEK                   目标周 yyyyWW（可省；省则取该证券「最早一条记录」所在的周）
    CURR_BRANCH / GITHUB_REF_NAME   目标分支（默认 quote）

命令行参数优先于同名环境变量：argv[1]=SECU_CODE，argv[2]=WEEK，argv[3]=CURR_BRANCH。

每个证券**独立处理**：各自一次查询、各自一份文件、各自一次提交；互不影响，单个失败不
阻断后续证券。

九、usc 探测 = 取最早记录（一查两用）
----------------------------------------------------------------------------------------
库里的 usc 若写成「裸码」（IAU）而不是「全码」，查询会返回 0 行 —— 而这与「该周真的没有
数据」（如国庆长假）产出的**空文件外观完全一致，无法区分**。故在正式取数前先查该证券的
最早一条记录：

    GET /rest/v1/<表>?select=ts&usc=eq.<码>&order=ts.asc&limit=1

这一条查询**同时办两件事**：
    1. 探测 —— 0 行 ⇒ 判定 usc 取值可疑，**该证券直接失败退出**，不产出任何文件；
    2. 定周 —— 有行 ⇒ 取其 ts 所在的那一周作为「未显式指定 WEEK」时的目标周。

查询语句会打进日志，故 usc 的真实取值形态从首次运行的日志即可读出。

十、日志
----------------------------------------------------------------------------------------
每一行日志都带 `[yyMMdd.HHmmss.SSS]` 时间前缀（毫秒 3 位），便于排查与分析各环节耗时：

    [260915.083000.123] [INFO] 证券 IAU：Region=US Market=ARCA

**逐行 flush 不可省**：CI 里 stdout 是管道、非 TTY，Python 默认块缓冲，日志会攒到进程结束才
一次性写出，导致 GitHub 侧的接收时间戳全部挤在同一秒 —— 逐行 flush 后，行内时间前缀与接收
时间才对得上。本格式与同目录 DMDCBWD31MigrationFile.py 的 `_now_tag` / `_log` 一致。

**stderr 同样逐行加前缀**：依赖模块 GitHubCommitContent 会向 stderr 写告警（如
「提交目标解析: 仓库 = …」），Actions 把 stdout/stderr 合流展示，不加前缀的行会混在日志里。
故 main() 启动时把 sys.stderr 包一层（_TimestampedStream），只作用于本进程，**不改动该模块
本身**（它同时被本目录其他脚本复用）。

十一、退出码
----------------------------------------------------------------------------------------
    0 = 全部证券处理完毕。以下几种都算「预期状态」，计入跳过、不影响退出码：
        「该周无数据」→ 产出仅含文件头的空文件（不算跳过）；
        「自动定周但历史尚未攒够 14 天」→ 跳过；
        「未在 finv_quote_secu 登记 / 其 timezone 为空」→ 跳过（补全元数据后重跑即可）。
    1 = 致命错误（凭据缺失 / 分支未定 / 登记表取不回）或全部证券失败
    2 = 部分证券失败，或有证券「已导出但源库未删净」（partial）

十二、存量盘点：源库里到底有哪些证券（2026-09-15）
----------------------------------------------------------------------------------------
本工具的导出名单是**人工维护**的（`finv_quote_secu` 的登记行），而上游的采集名单在库里 ——
两者之间**没有任何同步机制**。上游新采的证券不会自动出现在本工具的名单里，只会每周无声地
缺席：既不在名单里，日志连一条「跳过」都不会有（根本没被尝试）。

> 收敛之前名单分两处（`SecuMetaMapping.jsonl` 定尝试范围 + 本表定放行），两处不同步时
> 症状与此相同且更难查 —— 这正是收敛成一处（第三节）的直接动因。

故每次运行**开头**盘点一次源库，把两份名单做差后打进日志：

    ① 源库 `finv_quote_secu_kline_min`  上游**实际在采**什么 —— 扫描得到，是实测清单
    ② 登记表 `finv_quote_secu`          唯一名单：既定准入（见第三节），也供 Region/Market
                                        与 timezone（见第五节、第四节）

逐只打印 ① 的每个证券及其登记状态，再就三类问题单独点名：

    ！源库有数据、但 finv_quote_secu 未登记 —— **当前要补的就是这批**（附各自最早记录时刻）
    ！已登记但 timezone 为空                —— 补一列即可放行
    ！已登记但源库无数据                     —— 要么上游没采，要么 Code 形态不符，值得看一眼

**为什么要逐跳而不是一条 SQL**：PostgREST 没有 `DISTINCT` / `GROUP BY`，一条查询取不回去重
清单。故用 keyset 逐跳 —— `order=usc.asc,ts.asc` + `limit=1` + `usc=gt.<上一个>`，每次都走
主键索引 (usc, ts) 取「下一个更大的 usc」及其最早一条记录，跳 N 次得 N 个证券：

    GET /rest/v1/finv_quote_secu_kline_min?select=usc,ts&order=usc.asc,ts.asc&limit=1
    GET /rest/v1/finv_quote_secu_kline_min?select=usc,ts&order=usc.asc,ts.asc&limit=1&usc=gt.<上一个>

代价是 **N+1 次请求**（每次只回 1 行；N 是证券数，不是行数）。证券数上到几百以后，可在库侧
建 `DISTINCT` 视图或 RPC 收成一次请求 —— 那属于库侧改动，不是本脚本能单方面决定的。

源库清单**扫不动不阻断导出**：只记一条告警（结论只覆盖已扫到的部分），主流程照常；
`INVENTORY_MAX_SECU` 兜住上游证券数暴增。登记表则不同 —— 它是名单本身，取不到即整轮退出
（见第三节）。

十三、每日批次：公平顺序、周屏障与配额（2026-09-15）
----------------------------------------------------------------------------------------
目标（ACANX 2026-09-15 定）：**每天都有货交付**，而不是每周集中爆一次、其余六天闲置。

先说清一件事：稳态下这只能靠**有意保留一个已合格但未导出的队列**实现。所有证券的周都在
同一瞬间跨过 14 天线（第二节），所以「每天跑全量」并不会让数据更早到达，只是把爆发原样
推迟到那一天。要每天有货，就得让队列活过整周。

三条规则共同构成一个**无状态**的日调度（不引入任何新表、新文件、新状态字段）：

    ① 周屏障 —— 前一周全部导完，才碰下一周
       对每只证券算「最早一个尚未产出的周」next(c)，取 W_now = min next(c)，本次只导
       W_now。有证券没导完 W_now ⇒ 它的 next 还停在 W_now ⇒ min 不动 ⇒ 下次继续 W_now，
       **不会跳周**；某证券该周无数据不影响 min（min 取小，且空周本身要产出空文件）。

    ② 公平顺序 —— weekly_order()：首位按周序号轮转（每 N 周每只证券**恰好**当一次第一，
       是硬保证而非概率收敛），其余按 sha256("<年>-<周>|<usc>") 排序。
       **必须是确定性的**：用 random.random() 的话，手动重跑或任务重试会重新洗牌 ——
       同一天两次运行导出两批不同证券，配额翻倍、跨天边界错乱。哈希排序保证同一周
       永远同一个顺序。

    ③ 配额 —— DAILY_EXPORT_QUOTA（行数，见第八节）。按 ② 的顺序逐只导出，累计**实际
       数据行数**达到配额即停，剩下的留到下次（它们的 next 仍是 W_now，屏障自动接上）。
       证券是**原子单位**：不切半只，故最后一只可能小幅超出。空周文件计 0 行，不占配额。

**进度以目标仓库为准，不以源库是否删除为准。** 判据是「该证券该周的文件是否已在目标分支
上」—— 导出的语义本就是「在目标仓库产生文件」，源库删不删只是防膨胀的实现细节。故本节
**不依赖第七节的删除开关**：ACANX 2026-09-15 定「暂不删源库，等稳定后再开启」，而源库不删
时 `week_of_earliest_record()` 永远返回历史第一周，进度就只能由目标仓库的已产出集合推进
（`list_exported_weeks()`）。副产品是**幂等** —— 同一周不会被重复导出（ACANX：「证券 A 在
周一已导出，周二~六没必要再重复导一遍」），重跑也安全。

**配额的下界**：必须 ≥「每周新增行数 ÷ 7」。低于它则队列每天还不上，滞后无上限累积
（W_now 会越来越落后于当前可导出周）。反过来要「每天有货」也不宜 ≥ 每周总量，否则一天
就清空队列，剩下的六天依旧无事可做。

显式指定 `WEEK` 时走**手动路径**：全部证券同一周、**不受配额限制**（运维动作而非日常调度）；
此时同名文件按第六节加 `_N` 后缀，允许重复导出，用于数据订正。

【环境要求】Python 3.8+，仅标准库；可直连 api.github.com 与 *.supabase.co。
"""

import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse
from decimal import Decimal

# 同目录纯函数库：复用其 HTTP 请求 / 认证头 / 目标仓库与分支解析（纯函数，无副作用）
from GitHubCommitContent import (
    DEFAULT_API_BASE,
    _auth_headers,
    _ensure_console_utf8,
    _request,
    _resolve_target,
    commit_content,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# Supabase / PostgREST 源表（与 Java 版 FinvQuoteSecuKlineMin 一致）
TABLE = "finv_quote_secu_kline_min"

# 证券元数据登记表（导出前的准入校验 + 文件头 Timezone 的取值来源，见 docstring 第三节）
SECU_TABLE = "finv_quote_secu"

# SECU_TABLE 中与 finv_quote_secu_kline_min.usc 对应的列（ACANX 2026-09-15 给出建表 DDL 核实）
SECU_TABLE_CODE_COLUMN = "usc"

# 存量盘点（见 docstring 第十二节）一次取回登记表的列。按 ACANX 2026-09-15 给出的建表 DDL，
# region / market 与 timezone **同在** finv_quote_secu 一张表里。
SECU_TABLE_COLUMNS = "usc,region,market,timezone"

# 盘点源库时逐跳遍历 usc 的上限（每跳一次请求）。防上游证券数暴增时拖垮一次运行。
INVENTORY_MAX_SECU = 500

# 查询列（**按表列名**，与 mvsv 的列序无关）。prev_close / paocd 不取 —— mvsv 字段列表里没有。
SELECT_COLUMNS = ("ts,date,time,open,close,low,high,volume,turnover,"
                  "change_price,change_ratio")

# mvsv 数据列顺序（**与表定义列序不同**：mvsv 是 Open|Close|Low|High）
MVSV_FIELDS = ("Ts|Date|Time|Open|Close|Low|High|Volume|Turnover|"
               "ChangePrice|ChangePercent")
MVSV_FIELD_NAMES = ("时间戳(UTC)|日期|时间|开盘价|收盘价|最低价|最高价|成交量|"
                    "成交额|涨跌值|涨跌幅(%)")
MVSV_FIELD_TYPES = ("int|int|int|Decimal|Decimal|Decimal|Decimal|Decimal|"
                    "Decimal|Decimal|str")

# 供应商标识（本需求固定 FT）
PROVIDER = "FT"

# 落点目录模板（见模块 docstring 第五节）：<Region>_<Market>/<Code>
# 文件名模板拼接其上，故二者的 `%` 参数按同一顺序连续消费：
#     (region, market, code, region, market, code, iso_year, iso_week)
TARGET_DIR_TEMPLATE = "Data/Finv/SecuQuoteWeek/FT/%s_%s/%s"

# 落点路径模板（见模块 docstring 第五节）
TARGET_PATH_TEMPLATE = TARGET_DIR_TEMPLATE + "/%s_%s_%s_MIN_FT_%04d%02d.mvsv"

# 同名冲突规避：目标文件已存在时改用 _N 后缀（见 docstring 第六节）
TARGET_PATH_CONFLICT_TEMPLATE = \
    TARGET_DIR_TEMPLATE + "/%s_%s_%s_MIN_FT_%04d%02d_%d.mvsv"
CONFLICT_SUFFIX_MAX = 99

# 文件名反解用的正则**模板**：<Region>_<Market>_<Code>_MIN_FT_<yyyy><WW>[_N].mvsv
# 周号**零填充两位**（%04d%02d），故 `202605` 不会误配 `202650`（见 docstring 第十三节）
EXPORT_NAME_RE_TEMPLATE = r"^%s_%s_%s_MIN_FT_(\d{4})(\d{2})(?:_\d+)?\.mvsv$"

# 周结束距今至少需要的天数（见 docstring 第二节）
WEEK_END_LAG_DAYS = 14

# 默认配置
DEFAULT_BRANCH = "quote"
DEFAULT_PAGE_SIZE = 1000

# 删除源库数据的安全开关（见 docstring 第七节）。**默认关**：关时只打印「待删除」清单，
# 一个字节都不动库。开关名与语义对齐姊妹仓库 ACANX/Distribution 的 SupabaseSyncMvsv.py。
ENV_ENABLE_DELETE = "SUPABASE_ENABLE_DELETE"

# 每日导出配额（单位：**数据行数**，见 docstring 第十三节）。
# 取值来源刻意**不放在 workflow_dispatch 界面里**：由仓库变量 DAILY_EXPORT_QUOTA
# （Settings → Secrets and variables → Actions → Variables）注入，改它不必改本文件、
# 不必发 PR、也不必重新触发工作流；未设置时回落到下面的默认值。
ENV_DAILY_QUOTA = "DAILY_EXPORT_QUOTA"

# 配额缺省值：约「每周新增行数 ÷ 7」，使队列正好一周清空一轮（当前每周约 6.9 万行）。
# 低于「每周新增 ÷ 7」会让队列每天还不上、滞后无上限累积 —— 见 docstring 第十三节。
DEFAULT_DAILY_QUOTA = 10000

# PostgREST 请求超时（秒）
HTTP_TIMEOUT = 60


class ImportError_(Exception):
    """本工具的业务错误（与内建 ImportError 区分开，避免误捕获）"""


# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------

def env_bool(name, default=False):
    """读取布尔型环境变量

    只有 "true"/"1"/"yes"/"on"（忽略大小写与首尾空白）算开，其余一律算关 ——
    即**取值写错时倒向「关」**，不会因为拼错而意外打开删除开关。
    与姊妹仓库 SupabaseSyncMvsv.py 的同名函数口径一致。

    :param name: 环境变量名
    :param default: 变量未设置（或为空串）时的返回值
    :return: 布尔值
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# 日志：每行带 [yyMMdd.HHmmss.SSS] 前缀，逐行 flush
# ---------------------------------------------------------------------------

def _now_tag():
    """当前时间戳，格式 yyMMdd.HHmmss.SSS（毫秒 3 位）

    例：260915.083000.123。用于给日志行加前缀，方便排查、分析链路执行耗时。

    :return: 时间戳字符串
    """
    now = datetime.datetime.now()
    return now.strftime("%y%m%d.%H%M%S") + ".%03d" % (now.microsecond // 1000)


def _log(message):
    """带 yyMMdd.HHmmss.SSS 时间前缀的日志输出（走 stdout）

    flush=True 不可省：CI 里 stdout 是管道、非 TTY，Python 默认块缓冲，日志会攒到
    进程结束才一次性写出，导致 GitHub 侧的接收时间戳全部挤在同一秒、与实际产出时刻
    相差几十秒。逐行 flush 后，日志行的接收时间与行内 [yyMMdd.HHmmss.SSS] 前缀才能对上。

    :param message: 日志内容（可含 [INFO] 等分级前缀）
    """
    print("[%s] %s" % (_now_tag(), message), flush=True)


class _TimestampedStream:
    """给写入流的每一行加 [yyMMdd.HHmmss.SSS] 前缀的薄包装

    仅用于 stderr：本脚本自己的输出走 _log()（stdout，已带前缀），但依赖模块
    GitHubCommitContent 会向 stderr 写告警，Actions 把两股流合流展示，那些行会不带
    前缀地混进来。这里在本进程内包一层即可，不改动该模块本身。

    未定义的行为一律透传给底层流（flush / encoding / isatty / reconfigure / fileno …），
    故对调用方仍是「一个 file-like 对象」。跨多次 write 的半行不会被重复加前缀。
    """

    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True

    def write(self, text):
        """按行加前缀后转发；返回值为底层流的写法（字符数），与 file-like 约定一致

        :param text: 待写入文本
        :return: 写入的字符数
        """
        if not text:
            return 0
        for part in text.splitlines(True):
            if self._at_line_start:
                self._stream.write("[%s] " % _now_tag())
            self._stream.write(part)
            self._at_line_start = part.endswith("\n")
        self._stream.flush()
        return len(text)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        # 仅当常规查找失败时才会走到这里；_stream 存于实例字典，不会递归
        return getattr(self._stream, name)


# ---------------------------------------------------------------------------
# 周：定义、编号、边界
# ---------------------------------------------------------------------------

def week_bounds_utc(iso_year, iso_week):
    """由 yyyyWW 反推该周的 UTC 起止（半开区间 [周日起点, 次周日终点)）

    编号按 ISO 周取，起止把该 ISO 周整体前移一天（见模块 docstring 第二节）。

    :param iso_year: ISO 年（如 2026）
    :param iso_week: ISO 周号（1..53）
    :return: (start, end) 两个带 UTC 时区的 datetime
    :raises ImportError_: 周号不存在（如某年没有 W53）时抛出
    """
    try:
        monday = datetime.datetime.fromisocalendar(iso_year, iso_week, 1)
    except ValueError as e:
        raise ImportError_("周号不存在：%04dWW%02d（%s）" % (iso_year, iso_week, e))
    monday = monday.replace(tzinfo=datetime.timezone.utc)
    start = monday - datetime.timedelta(days=1)     # 周日 00:00:00 UTC（含）
    end = start + datetime.timedelta(days=7)        # 次周日 00:00:00 UTC（不含）
    return start, end


def iso_label_of(dt_utc):
    """由任一瞬间反推它所属「本仓周」的 (ISO 年, ISO 周号)

    :param dt_utc: 带时区的 datetime（任意时区，内部先归一到 UTC）
    :return: (iso_year, iso_week)
    """
    d = dt_utc.astimezone(datetime.timezone.utc)
    # 距本周（周日为第一天）起点的天数：周日=0, 周一=1, … 周六=6
    offset = (d.weekday() + 1) % 7
    start = (d - datetime.timedelta(days=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    y, w, _ = (start + datetime.timedelta(days=1)).isocalendar()   # 用该周的周一查
    return y, w


def parse_week_arg(text):
    """解析周参数：接受 202625 / 2026-W25 / 2026W25 三种写法

    :param text: 周字符串
    :return: (iso_year, iso_week)
    :raises ImportError_: 格式非法或周号不存在时抛出
    """
    s = (text or "").strip().upper().replace("-", "")
    if not s:
        raise ImportError_("周参数为空")
    # 归一化后接受两种形态：6 位纯数字 "202625"，或带 W 的 "2026W25"
    if len(s) == 6 and s.isdigit():
        y, w = int(s[:4]), int(s[4:])
    elif len(s) == 7 and s[4] == "W" and s[:4].isdigit() and s[5:].isdigit():
        y, w = int(s[:4]), int(s[5:])
    else:
        raise ImportError_("周参数格式非法：%s（应为 202625 或 2026-W25）" % text)
    week_bounds_utc(y, w)          # 借边界计算做存在性校验（周号不存在会抛错）
    return y, w


def week_of_earliest_record(client, usc):
    """查该证券在库中最早一条记录（min(ts)），返回 (UTC 秒时间戳, 所属周)

    一条查询**同时办两件事**：既探测 usc 取值是否可用（0 行 ⇒ 取值可疑），
    又为「未显式指定周」的场景定出目标周 —— 即该证券历史起点所在的那一周。

    :param client: SupabaseRestClient
    :param usc: 证券代码
    :return: (earliest_ts, (iso_year, iso_week))；该 usc 无任何记录时返回 (None, None)
    """
    qs = "select=ts&usc=eq.%s&order=ts.asc&limit=1" % urllib.parse.quote(usc, safe="")
    rows = parse_rows(client.query(TABLE, qs, operation="查最早记录/探测 usc"))
    if not rows:
        return None, None
    earliest_ts = int(rows[0]["ts"])
    dt_utc = datetime.datetime.fromtimestamp(earliest_ts, tz=datetime.timezone.utc)
    return earliest_ts, iso_label_of(dt_utc)


# ---------------------------------------------------------------------------
# 格式化：一律「去尾随零、不限小数位」
# ---------------------------------------------------------------------------

def number_text(value):
    """把 PostgREST 返回的数值转成 mvsv 文本（去尾随零、不限小数位、绝不科学计数法）

    去尾零**不丢精度**：删的只是末尾无意义的 0（82.055000 → 82.055，Decimal 意义上相等），
    有效位一位不动。真正会丢精度的是「限定小数位」，本函数不做任何限定。

    :param value: None / int / Decimal / float / str
    :return: 文本；None 或空串返回 ""
    """
    if value is None:
        return ""
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return ""
        try:
            d = Decimal(v)
        except ArithmeticError:
            return v                      # 非数值原样透出，不吞掉信息
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))           # float 兜底（正常路径不会走到）
    s = format(d, "f")                    # 'f' 永不输出科学计数法
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-", "-0"):
        s = "0"
    return s


def int_padded(value, width):
    """整数按固定位宽补零输出（用于 Date 8 位 / Time 6 位）"""
    try:
        return "%0*d" % (width, int(value))
    except (TypeError, ValueError):
        return ""


def derive_date_time(ts):
    """由 UTC 秒时间戳推导 (yyyyMMdd, HHMMSS) 两个整数（Date/Time 缺失时用）"""
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
    return int(dt.strftime("%Y%m%d")), int(dt.strftime("%H%M%S"))


# ---------------------------------------------------------------------------
# Supabase Data API（PostgREST）最小客户端
# ---------------------------------------------------------------------------

class SupabaseRestClient:
    """Supabase Data API 最小客户端（仅标准库 urllib）

    每个请求同时携带 apikey 与 Authorization: Bearer 两个头（Supabase 要求二者并存）；
    密钥不落日志；URL 中的项目引用一律遮蔽后再输出。
    """

    def __init__(self, project_ref, api_key, timezone_name="Asia/Shanghai", timeout=HTTP_TIMEOUT):
        ref = (project_ref or "").strip()
        if not ref:
            raise ImportError_("Supabase 项目引用（SUPABASE_PROJECT_REF）不能为空")
        if "/" in ref or ":" in ref:
            raise ImportError_("项目引用只填 ref 本身，不要带协议或域名")
        if not (api_key or "").strip():
            raise ImportError_("Supabase API 密钥（SUPABASE_KEY）不能为空")
        self.ref = ref
        self.rest_url = "https://%s.supabase.co/rest/v1" % ref
        self.api_key = api_key.strip()
        self.timezone_name = (timezone_name or "").strip() or None
        self.timeout = timeout

    def _redact(self, text):
        """把文本中的项目引用替换为 ***，供日志输出"""
        return (text or "").replace(self.ref, "***")

    def _headers(self, prefer=None):
        headers = {
            "apikey": self.api_key,
            "Authorization": "Bearer %s" % self.api_key,
            "Accept": "application/json",
        }
        if self.timezone_name:
            headers["Prefer"] = ("timezone=" + self.timezone_name) if not prefer \
                else (prefer + ",timezone=" + self.timezone_name)
        elif prefer:
            headers["Prefer"] = prefer
        return headers

    def query(self, table, query_string, operation="查询"):
        """执行 GET 查询，返回响应文本（调用方用 parse_json 解析）

        :raises ImportError_: 网络错误或响应非 2xx 时抛出
        """
        url = "%s/%s" % (self.rest_url, urllib.parse.quote(table, safe=""))
        if query_string:
            url = url + "?" + query_string
        status, text, err = _request("GET", url, self._headers(), None, self.timeout)
        if err:
            raise ImportError_("%s网络错误：%s（URL %s）" % (operation, err, self._redact(url)))
        if status is None or not (200 <= status < 300):
            raise ImportError_("%s失败，HTTP %s，URL %s，响应：%s"
                               % (operation, status, self._redact(url), (text or "")[:300]))
        return text

    def delete(self, table, query_string, operation="删除"):
        """执行 DELETE，返回响应文本（Prefer: return=minimal，通常为空串）

        **调用方必须保证 query_string 带足过滤条件** —— PostgREST 允许无过滤的
        DELETE（会清空整表）。本方法内置保险丝：过滤串里必须同时出现 usc=eq. 与
        ts 的上下界，缺一即拒绝发送请求。

        :raises ImportError_: 过滤串不完整，或网络错误、响应非 2xx
        """
        for needle, label in (("usc=eq.", "证券过滤"), ("ts=gte.", "起始时间"),
                              ("ts=lt.", "结束时间")):
            if needle not in (query_string or ""):
                raise ImportError_("拒绝执行无%s的 DELETE（防误删整表）：%s"
                                   % (label, query_string))
        url = "%s/%s" % (self.rest_url, urllib.parse.quote(table, safe=""))
        url = url + "?" + query_string
        status, text, err = _request("DELETE", url, self._headers(prefer="return=minimal"),
                                     None, self.timeout)
        if err:
            raise ImportError_("%s网络错误：%s（URL %s）" % (operation, err, self._redact(url)))
        if status is None or not (200 <= status < 300):
            raise ImportError_("%s失败，HTTP %s，URL %s，响应：%s"
                               % (operation, status, self._redact(url), (text or "")[:300]))
        return text


def parse_rows(text):
    """解析 PostgREST 响应，**必须用 Decimal 承接小数**，否则高精度值在解析阶段就丢了

    :param text: 响应文本
    :return: 行数组（list[dict]）
    :raises ImportError_: 响应不是 JSON 数组时抛出
    """
    data = json.loads(text, parse_float=Decimal)
    if not isinstance(data, list):
        raise ImportError_("查询响应不是 JSON 数组")
    return data


def fetch_registered_secus(client):
    """一次取回 `finv_quote_secu` 的**全部**登记行（而非逐 Code 单查，见 docstring 第十二节）

    keyset 游标按 `usc`（主键）升序翻页；登记表只有几十行，实际恒为一页。

    :param client: SupabaseRestClient
    :return: (dict {usc: (region, market, timezone)}, error)
        - 成功       → ({...}, None)
        - 查询失败   → (已取到的部分, "失败原因")
    """
    rows = {}
    last = None
    while True:
        qs = ("select=%s&order=%s.asc&limit=%d"
              % (SECU_TABLE_COLUMNS, SECU_TABLE_CODE_COLUMN, DEFAULT_PAGE_SIZE))
        if last is not None:
            qs += "&%s=gt.%s" % (SECU_TABLE_CODE_COLUMN, urllib.parse.quote(last, safe=""))
        try:
            page = parse_rows(client.query(SECU_TABLE, qs, operation="取登记表全量"))
        except ImportError_ as e:
            return rows, str(e)
        for r in page:
            code = (r.get(SECU_TABLE_CODE_COLUMN) or "").strip()
            if code:
                rows[code] = ((r.get("region") or "").strip(),
                              (r.get("market") or "").strip(),
                              (r.get("timezone") or "").strip())
        page_last = (page[-1].get(SECU_TABLE_CODE_COLUMN) or "").strip() if page else ""
        if len(page) < DEFAULT_PAGE_SIZE or not page_last or page_last == last:
            return rows, None
        last = page_last


def scan_source_secus(client, cap=INVENTORY_MAX_SECU):
    """盘点源库里**实际存有数据**的全部 usc（keyset 逐跳，见 docstring 第十二节）

    PostgREST 没有 `DISTINCT` / `GROUP BY`，一条 SQL 取不回去重清单，故逐跳：
    `order=usc.asc,ts.asc` + `limit=1` + `usc=gt.<上一个>` —— 每次都走主键索引 (usc, ts)
    取「下一个更大的 usc」及其最早一条记录（ts 升序下的第一行），跳 N 次得 N 个证券。
    代价是 N+1 次请求（每次只回 1 行；N 是证券数，**不是行数**）。

    :param client: SupabaseRestClient
    :param cap: 最多遍历多少个（防上游证券数暴增拖垮运行）
    :return: ([(usc, first_ts)], truncated, error)
        - truncated=True 表示触到 cap 上限，清单不完整
    """
    found = []
    last = None
    while len(found) < cap:
        qs = ("select=%s,ts&order=%s.asc,ts.asc&limit=1"
              % (SECU_TABLE_CODE_COLUMN, SECU_TABLE_CODE_COLUMN))
        if last is not None:
            qs += "&%s=gt.%s" % (SECU_TABLE_CODE_COLUMN, urllib.parse.quote(last, safe=""))
        try:
            page = parse_rows(client.query(TABLE, qs, operation="盘点源库证券清单"))
        except ImportError_ as e:
            return found, False, str(e)
        if not page:
            return found, False, None
        code = (page[0].get(SECU_TABLE_CODE_COLUMN) or "").strip()
        if not code or code == last:      # 游标没前进：防上游数据异常时空转
            return found, False, None
        found.append((code, page[0].get("ts")))
        last = code
    return found, True, None


def audit_secu_inventory(client, registry):
    """运行开头盘点一次源库存量，把两份名单做差后打进日志（见 docstring 第十二节）

    两份名单各管一段：

        ① 源库 `finv_quote_secu_kline_min`  上游**实际在采**什么（扫描得到，是实测清单）
        ② 登记表 `finv_quote_secu`          唯一名单：既定准入（见第三节），
                                           也供落点路径的 Region/Market 与 timezone
                                           （见第五节）

    ① 有而 ② 没有的，就是「上游在采、这边无声漏掉」的证券 —— 本盘点的目的就是把它们
    **逐只点名**，而不是让它们每轮静默缺席（既不在名单里，日志连一条「跳过」都不会有）。

    盘点失败**不阻断导出**：只记告警，主流程照常。

    :param registry: 已取回的登记表 {usc: (region, market, timezone)}（由 main 传入，避免重复查询）
    """
    _log("")
    _log("===== 源库证券盘点（%s）=====" % TABLE)

    source, truncated, err = scan_source_secus(client)
    if err:
        _log("⚠️ 源库清单盘点中断（已扫到 %d 个），以下仅就扫到的部分做差：%s"
             % (len(source), err))
    if truncated:
        _log("⚠️ 已触遍历上限 %d 个，清单不完整 —— 调大 INVENTORY_MAX_SECU 可继续"
             % INVENTORY_MAX_SECU)

    def fmt_ts(value):
        try:
            return datetime.datetime.fromtimestamp(
                int(value), tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return "?"

    src_codes = set(code for code, _ in source)
    unregistered = [(c, ts) for c, ts in source if c not in registry]
    no_tz = [c for c, _ in source if c in registry and not registry[c][2]]
    ready = [c for c, _ in source if c in registry and registry[c][2]]
    no_data = [c for c in registry if c not in src_codes]

    _log("[INFO] 源库有数据 %d 个 / 登记表 %d 个；其中可导出 %d 个"
         % (len(source), len(registry), len(ready)))
    for code, ts in source:
        meta = registry.get(code)
        if meta is None:
            mark, detail = "[未登记]", "-"
        else:
            mark = "[已登记]"
            detail = "%s/%s %s" % (meta[0] or "?", meta[1] or "?",
                                   meta[2] or "(timezone 空)")
        _log("[INFO]   %s %s %s | 最早 %s UTC" % (code, mark, detail, fmt_ts(ts)))

    if unregistered:
        _log("⚠️ 源库有数据、但 %s 未登记 %d 个 —— 每轮都导不出，逐只点名如下："
             % (SECU_TABLE, len(unregistered)))
        for code, ts in unregistered:
            _log("      %s  最早记录 %s UTC" % (code, fmt_ts(ts)))
        _log("      ↑ 在 %s 补一行（usc / region / market / timezone）后，"
             "下次运行即会导出 —— 名单只有这一处，不必再改别的地方"
             % SECU_TABLE)
    else:
        _log("[INFO] 源库有数据的证券**全部已登记**，无遗漏")

    if no_tz:
        _log("⚠️ 已登记但 timezone 为空 %d 个（补上 timezone 即放行）：%s"
             % (len(no_tz), ", ".join(no_tz)))
    if no_data:
        _log("⚠️ 已登记但源库无数据 %d 个（仍会被尝试；因 usc 探测 0 行，将记为 failed）—— "
             "要么上游没采，要么 Code 形态不符：%s" % (len(no_data), ", ".join(no_data)))


def fetch_week_rows(client, usc, start_ts, end_ts, page_size):
    """按 ts 升序、keyset 游标分页取全 [start_ts, end_ts) 区间内的记录

    终止条件只看「本页是否满 page_size」——某页不足即已到末尾。

    :param client: SupabaseRestClient
    :param usc: 证券代码
    :param start_ts: 周起点（含），UTC 秒
    :param end_ts: 周终点（不含），UTC 秒
    :param page_size: 单页行数
    :return: 行数组（list[dict]，按 ts 升序）
    """
    rows = []
    cursor = None
    page = 0
    base = build_week_filter(usc, start_ts, end_ts)
    while True:
        # 翻页时把游标条件**追加**在后面：PostgREST 对同名列取 AND，
        # 而 ts > 游标 已经蕴含 ts >= 起点，故与首页条件并存无副作用。
        lower = "" if cursor is None else ("&ts=gt.%d" % cursor)
        qs = ("select=%s&%s%s&order=ts.asc&limit=%d"
              % (SELECT_COLUMNS, base, lower, page_size))
        data = parse_rows(client.query(TABLE, qs, operation="取数第 %d 页" % (page + 1)))
        page += 1
        rows.extend(data)
        _log("[INFO]   第 %d 页：%d 行（累计 %d）" % (page, len(data), len(rows)))
        if len(data) < page_size:
            break
        cursor = int(data[-1]["ts"])
    return rows


def build_week_filter(usc, start_ts, end_ts):
    """拼出「某证券 + 某整周」的 PostgREST 过滤串（取数与删除共用同一串）

    取数与删除**必须**用同一个区间，否则会出现「导出的没删、删的没导出」。
    故这里集中拼一次，两处都调它。

    :param usc: 证券代码
    :param start_ts: 周起点（含），UTC 秒
    :param end_ts: 周终点（不含），UTC 秒
    :return: 查询串（不含前导 "?"）
    """
    return ("usc=eq.%s&ts=gte.%d&ts=lt.%d"
            % (urllib.parse.quote(usc, safe=""), start_ts, end_ts))


def delete_week_rows(client, usc, start_ts, end_ts):
    """删除某证券某整周的全部记录（**不可逆**，只在删除开关开启时才会被调用）

    区间与 fetch_week_rows 完全一致（同一个 build_week_filter）。

    :param client: SupabaseRestClient
    :param usc: 证券代码
    :param start_ts: 周起点（含），UTC 秒
    :param end_ts: 周终点（不含），UTC 秒
    :raises ImportError_: 网络错误或响应非 2xx
    """
    qs = build_week_filter(usc, start_ts, end_ts)
    client.delete(TABLE, qs, operation="删除 %s 的 [%d, %d)" % (usc, start_ts, end_ts))


# ---------------------------------------------------------------------------
# .mvsv 生成
# ---------------------------------------------------------------------------

def row_text(row):
    """把一行库记录转成 mvsv 数据行（11 列，"|" 分隔）"""
    ts = row.get("ts")
    date_v = row.get("date")
    time_v = row.get("time")
    if (date_v is None or time_v is None) and ts is not None:
        d, t = derive_date_time(ts)
        if date_v is None:
            date_v = d
        if time_v is None:
            time_v = t
    cells = [
        number_text(ts),
        int_padded(date_v, 8),
        int_padded(time_v, 6),
        number_text(row.get("open")),
        number_text(row.get("close")),
        number_text(row.get("low")),
        number_text(row.get("high")),
        number_text(row.get("volume")),
        number_text(row.get("turnover")),
        number_text(row.get("change_price")),
        number_text(row.get("change_ratio")),
    ]
    return "|".join(cells)


def build_mvsv(rows, code, region, market, timezone, fetch_time_text):
    """生成完整的 .mvsv 文本（23 行头 + 空行 + 数据行，末行无结尾换行）

    :param rows: 行数组（可能为空 = 该周无数据，仍产出仅含文件头的文件）
    :param code: 证券代码（裸码，如 IAU）
    :param region: 地区（如 US）
    :param market: 市场（如 ARCA）
    :param timezone: 时区（如 Asia/Shanghai），取自 finv_quote_secu.timezone
    :param fetch_time_text: 采集时间文本（导出时刻，UTC+8）
    :return: 文件文本
    """
    count = len(rows)
    lines = [
        "# 标题 : %s 分钟级行情数据" % code,
        "# 数据供应商 : %s" % PROVIDER,
        '# 字段 : "%s"' % MVSV_FIELDS,
        '# 字段名称 : "%s"' % MVSV_FIELD_NAMES,
        '# 字段类型 : "%s"' % MVSV_FIELD_TYPES,
        "# 计数 : %d" % count,
        '# 采集时间 : "%s"' % fetch_time_text,
        "# 证券代码 : %s" % code,
        "# 地区 : %s" % region,
        "# 市场 : %s" % market,
        "# Timezone : %s" % timezone,
        "# 备注 :",
        "# Title : %s Minute Quote Data" % code,
        "# DataProvider : %s" % PROVIDER,
        '# Field : "%s"' % MVSV_FIELDS,
        '# FieldName : "%s"' % MVSV_FIELDS,
        '# FieldType : "%s"' % MVSV_FIELD_TYPES,
        "# Count : %d" % count,
        '# FetchTime : "%s"' % fetch_time_text,
        "# SecuCode : %s" % code,
        "# Region : %s" % region,
        "# Market : %s" % market,
        "# Timezone : %s" % timezone,
        "",
    ]
    lines.extend(row_text(r) for r in rows)
    return "\n".join(lines)


def build_target_path(region, market, code, iso_year, iso_week, suffix=None):
    """拼出落点路径（见模块 docstring 第五节）

    :param suffix: 同名冲突时的规避序号；None = 不加后缀（常规命名）
    """
    if suffix is None:
        return TARGET_PATH_TEMPLATE % (region, market, code, region, market, code,
                                       iso_year, iso_week)
    return TARGET_PATH_CONFLICT_TEMPLATE % (region, market, code, region, market, code,
                                            iso_year, iso_week, suffix)


def list_remote_dir(cfg, dir_path):
    """列出目标分支上某目录下的文件名集合（Contents API GET）

    一次请求拿到整个证券目录的清单，冲突检测与 `_N` 选号都在本地完成，
    不必对每个候选路径各发一次探测请求。

    :param cfg: 配置 dict（用 branch / token）
    :param dir_path: 仓库内目录路径（不带前导 /）
    :return: (names, error)：
        - names: 文件名集合（目录不存在 → 空集）；失败时为 None
        - error: 失败原因；成功时为 None
    """
    owner, repo, branch = _resolve_target(None, None, cfg["branch"])
    if not (owner and repo):
        return None, "未能解析出目标仓库的 owner/repo"
    url = "%s/%s/%s/contents/%s?ref=%s" % (
        DEFAULT_API_BASE, owner, repo,
        urllib.parse.quote(dir_path, safe="/"),
        urllib.parse.quote(branch, safe=""),
    )
    status, text, err = _request("GET", url, _auth_headers(cfg["token"]), None, HTTP_TIMEOUT)
    if err:
        return None, "网络错误：%s" % err
    if status == 404:
        return set(), None      # 目录还不存在 ⇒ 目录下必然没有任何文件
    if status != 200:
        return None, "HTTP %s，响应：%s" % (status, (text or "")[:200])
    try:
        data = json.loads(text)
    except ValueError:
        return None, "响应不是合法 JSON"
    if not isinstance(data, list):
        return None, "响应不是目录列表（可能路径指向了文件）"
    return {item.get("name") for item in data if isinstance(item, dict)}, None


def pick_target_path(cfg, region, market, code, iso_year, iso_week):
    """定落点：目标文件不存在则用常规名，已存在则加 `_N` 后缀规避（docstring 第六节）

    :return: (path_key, suffix, error)：
        - path_key: 落点路径；失败时为 None
        - suffix: None = 常规命名；整数 = 本次用的规避序号
        - error: 失败原因；成功时为 None
    """
    base = build_target_path(region, market, code, iso_year, iso_week)
    dir_path, base_name = base.rsplit("/", 1)
    names, err = list_remote_dir(cfg, dir_path)
    if err:
        return None, None, "列举目标目录失败（%s）：%s" % (dir_path, err)

    if base_name not in names:
        return base, None, None

    _log("[WARN] 目标文件已存在：%s" % base)
    _log("[WARN]   → 按同名冲突规避，改用 _N 后缀（N 从 1 起，取第一个未占用的）")
    stem = base_name[:-len(".mvsv")]
    for n in range(1, CONFLICT_SUFFIX_MAX + 1):
        cand_name = "%s_%d.mvsv" % (stem, n)
        if cand_name not in names:
            path_key = dir_path + "/" + cand_name
            _log("[WARN]   → 本次落点：%s" % path_key)
            return path_key, n, None
    return None, None, ("目标目录下 %s_1..%s_%d.mvsv 全部已占用，无从规避"
                        % (stem, stem, CONFLICT_SUFFIX_MAX))


# ---------------------------------------------------------------------------
# 每日批次：公平顺序 + 周屏障 + 配额（见 docstring 第十三节）
# ---------------------------------------------------------------------------

def export_name_re(region, market, code):
    """拼出该证券落点文件名的正则

    周号零填充两位，故 `..._202605.mvsv` 与 `..._202650.mvsv` 不会互相误配。

    :return: 已编译的正则；第 1 组 = ISO 年，第 2 组 = ISO 周号
    """
    return re.compile(EXPORT_NAME_RE_TEMPLATE
                      % (re.escape(region), re.escape(market), re.escape(code)))


def next_iso_week(iso_year, iso_week):
    """该周的下一个 ISO 周（跨年自动进位）

    :return: (iso_year, iso_week)
    """
    start, _ = week_bounds_utc(iso_year, iso_week)
    return iso_label_of(start + datetime.timedelta(days=7))


def prev_iso_week(iso_year, iso_week):
    """该周的上一个 ISO 周（跨年自动退位）

    :return: (iso_year, iso_week)
    """
    start, _ = week_bounds_utc(iso_year, iso_week)
    return iso_label_of(start - datetime.timedelta(days=1))


def max_exportable_week(now_utc=None):
    """当前允许导出的**最大**周（其结束时刻距今已满 WEEK_END_LAG_DAYS 天）

    合格条件是 `end <= now - 14d`。cutoff 落在某周内 ⇒ 该周的 end 仍在 cutoff 之后 ⇒
    不合格，故取它的上一周。

    :param now_utc: 基准时刻（默认当前 UTC；测试可注入）
    :return: (iso_year, iso_week)
    """
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=WEEK_END_LAG_DAYS)
    return prev_iso_week(*iso_label_of(cutoff))


def resolve_daily_quota(raw):
    """解析每日配额（单位：数据行数）

    未设置 → DEFAULT_DAILY_QUOTA；0 或负数 → **不限额**（导出该周全部待导证券）。

    :raises ImportError_: 取值不是整数时抛出
    """
    s = (raw or "").strip()
    if not s:
        return DEFAULT_DAILY_QUOTA
    try:
        return int(s)
    except ValueError:
        raise ImportError_("%s 不是整数：%s（应为正整数行数；0 = 不限额）"
                           % (ENV_DAILY_QUOTA, raw))


def weekly_order(codes, iso_year, iso_week):
    """本周的导出顺序：首位轮转 + 其余按周哈希洗牌（**确定性**，见 docstring 第十三节 ②）

    :param codes: 本周待导的证券（入参顺序无关，内部先归一，故结果可复现）
    :return: 排好序的新列表
    """
    pool = sorted(codes)
    if len(pool) <= 1:
        return pool
    # 单调周序号：取该周周一的 ordinal（连续整数，跨年不跳号、不重叠）
    idx = datetime.date.fromisocalendar(iso_year, iso_week, 1).toordinal()
    head = pool[idx % len(pool)]
    rest = sorted((c for c in pool if c != head),
                  key=lambda c: hashlib.sha256(
                      ("%04d-%02d|%s" % (iso_year, iso_week, c)).encode("utf-8")).digest())
    return [head] + rest


def list_exported_weeks(cfg, region, market, code):
    """该证券在目标分支上**已产出**的周集合（从目录清单的文件名反解）

    一次 Contents API 拿整个证券目录，本地正则反解周号。**进度以此为准**，不依赖源库是否
    删除（见 docstring 第十三节）。

    :return: (weeks, names, error)：
        - weeks: set of (iso_year, iso_week)；目录不存在 → 空集
        - names: 原始文件名集合（供落点环节复用，省一次请求）
        - error: 失败原因；成功时为 None
    """
    dir_path = TARGET_DIR_TEMPLATE % (region, market, code)
    names, err = list_remote_dir(cfg, dir_path)
    if err:
        return None, None, "列举目标目录失败（%s）：%s" % (dir_path, err)
    pat = export_name_re(region, market, code)
    weeks = set()
    for name in names:
        m = pat.match(name)
        if m:
            weeks.add((int(m.group(1)), int(m.group(2))))
    return weeks, names, None


def pending_week(earliest_label, exported_weeks, max_label):
    """该证券**最早一个尚未产出**的周（见 docstring 第十三节 ①）

    从源库最早记录所在周起逐周前进、跳过目标仓库已有的。中间若有空洞（某周漏导）会返回
    那个空洞，而不是直接跳到末尾。

    :param earliest_label: 源库最早记录所在的周 (iso_year, iso_week)
    :param exported_weeks: 目标仓库已产出的周集合
    :param max_label: 允许导出的最大周（含）
    :return: (iso_year, iso_week)；已追平（无待导出）时返回 None
    """
    label = earliest_label
    while label <= max_label:                 # 元组比较：年在前，等价于时间先后
        if label not in exported_weeks:
            return label
        label = next_iso_week(*label)
    return None


def plan_daily_batch(client, cfg, registry, codes):
    """定出本次批次的计划：导哪一周、按什么顺序、哪些证券待导（docstring 第十三节）

    每只证券各一次源库查询（复用 `week_of_earliest_record`，同时充当 usc 有效性探测）
    与一次目标仓库目录列举 —— N 只证券约 2N 次请求。

    :return: (plan, error)。plan 为 dict：
        - week:    本次要导的周 (iso_year, iso_week)；无待导时为 None
        - ordered: 该周的待导证券，已按 `weekly_order` 排好
        - awaiting: 已领先于本次周、需等下一周的证券数
        - blocked: {code: 原因}，未进入队列的证券及原因
    """
    max_label = max_exportable_week()
    _log("[INFO] 当前可导出的最大周（结束距今满 %d 天）：%04dWW%02d"
         % (WEEK_END_LAG_DAYS, max_label[0], max_label[1]))

    blocked, pendings = {}, {}
    _log("[INFO] 逐只推算「最早未产出的周」：")
    for code in codes:
        meta = registry.get(code)
        if meta is None:
            blocked[code] = "未在 %s 中登记" % SECU_TABLE
            continue
        region, market, timezone = meta
        if not timezone:
            blocked[code] = "已登记但 timezone 为空（文件头缺值，不予导出）"
            continue

        earliest_ts, earliest_label = week_of_earliest_record(client, code)
        if earliest_ts is None:
            blocked[code] = "源库无任何记录（usc 探测 0 行）"
            continue

        exported, _names, err = list_exported_weeks(cfg, region, market, code)
        if err:
            blocked[code] = err
            continue

        label = pending_week(earliest_label, exported, max_label)
        if label is None:
            blocked[code] = ("已追平：%04dWW%02d 及以前均已产出" % (max_label[0], max_label[1]))
            continue
        pendings[code] = label
        _log("[INFO]   %s → 待导 %04dWW%02d（目标仓库已产出 %d 周，源库最早 %04dWW%02d）"
             % (code, label[0], label[1], len(exported),
                earliest_label[0], earliest_label[1]))

    if not pendings:
        return {"week": None, "ordered": [], "awaiting": 0, "blocked": blocked}, None

    week = min(pendings.values())
    ready = [c for c in pendings if pendings[c] == week]
    ordered = weekly_order(ready, week[0], week[1])
    awaiting = len(pendings) - len(ready)

    _log("[INFO] 周屏障：本次目标周 = %04dWW%02d（待导 %d 只；另有 %d 只已领先，须等下一周）"
         % (week[0], week[1], len(ordered), awaiting))

    # 队列积压提示：屏障取 min，故任何一只证券的**历史缺口**都会把整条队列按在那一周。
    # 首次运行（或某只证券换了 Region/Market 落点、目标目录还是空的）必然如此，
    # 会按每日配额逐日补齐；但也有可能是配额小于「每周新增 ÷ 7」导致的还不上账。
    _, week_end = week_bounds_utc(*week)
    overdue = (datetime.datetime.now(datetime.timezone.utc) - week_end).days - WEEK_END_LAG_DAYS
    if overdue > 7:
        _log("[WARN] 队列积压：目标周结束于 %s，超出入库线已 %d 天"
             % (week_end.strftime("%Y-%m-%d"), overdue))
        _log("[WARN]   常见成因：① 首次运行 / 某只证券的目标目录还是空的（历史缺口要逐日补）；"
             "② 配额低于「每周新增行数 ÷ 7」，队列每天还不上账")
        _log("[WARN]   当前配额 %s —— 积压不再增长才对；若逐日变大，请调高 %s"
             % ("不限" if cfg["daily_quota"] <= 0 else "%d 行" % cfg["daily_quota"],
                ENV_DAILY_QUOTA))
    _log("[INFO] 公平顺序（首位轮转 + 其余按周哈希洗牌，确定性）：%s%s"
         % (", ".join(ordered[:12]), " …" if len(ordered) > 12 else ""))
    return {"week": week, "ordered": ordered, "awaiting": awaiting, "blocked": blocked}, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def resolve_config():
    """解析运行参数：命令行 > 环境变量 > 默认值

    :return: 配置 dict
    :raises ImportError_: 必填项缺失时抛出
    """
    argv = sys.argv[1:]
    codes_raw = (argv[0] if len(argv) > 0 else os.environ.get("SECU_CODE", "")).strip()
    week_raw = (argv[1] if len(argv) > 1 else os.environ.get("WEEK", "")).strip()
    branch = (argv[2] if len(argv) > 2 else
              os.environ.get("CURR_BRANCH", "") or os.environ.get("GITHUB_REF_NAME", "")).strip() \
        or DEFAULT_BRANCH

    # 留空 = 取登记表 finv_quote_secu 中的全部 usc（由 main 取回登记表后补齐）
    codes = [c.strip() for c in codes_raw.split(",") if c.strip()] or None

    page_size_raw = os.environ.get("SUPABASE_PAGE_SIZE", "").strip()
    try:
        page_size = int(page_size_raw) if page_size_raw else DEFAULT_PAGE_SIZE
    except ValueError:
        raise ImportError_("SUPABASE_PAGE_SIZE 不是整数：%s" % page_size_raw)
    if page_size <= 0:
        raise ImportError_("SUPABASE_PAGE_SIZE 必须为正整数：%d" % page_size)

    return {
        "codes": codes,
        "week_raw": week_raw,
        "branch": branch,
        "page_size": page_size,
        "daily_quota": resolve_daily_quota(os.environ.get(ENV_DAILY_QUOTA, "")),
        "enable_delete": env_bool(ENV_ENABLE_DELETE, False),
        "project_ref": os.environ.get("SUPABASE_PROJECT_REF", "").strip(),
        "api_key": os.environ.get("SUPABASE_KEY", "").strip(),
        "token": os.environ.get("GIT_COMMIT_TOKEN", "").strip(),
    }


def process_one(client, cfg, registry, code, target_week=None, stats=None):
    """处理单个证券：登记校验 → 探测=取最早记录 →（定周）→ 取数 → 生成 → 提交

    登记表 `finv_quote_secu` 是**唯一**名单：既是准入名单，也供落点路径的 Region/Market
    与文件头的 Timezone（docstring 第三节、第五节）。

    :param target_week: 批次计划指定的周 (iso_year, iso_week)；None = 按老规矩自行定周。
        显式 WEEK 参数的优先级高于它（docstring 第十三节）
    :param stats: 可选出参 dict；成功落库后写入 rows / week / path，供调用方累计配额
    :return: ("ok"|"skipped"|"failed"|"partial", 结果描述)
    """
    meta = registry.get(code)
    if meta is None:
        return "skipped", ("未在 %s 中登记（%s=eq.%s 查无此行）—— 按约定不予导出；"
                           "补全该证券的元数据后重跑即可"
                           % (SECU_TABLE, SECU_TABLE_CODE_COLUMN, code))
    region, market, timezone = meta
    if not timezone:
        return "skipped", ("已在 %s 中登记，但其 timezone 为空 —— 文件头需要 Timezone 值，"
                           "故不予导出；补全 timezone 后重跑即可" % SECU_TABLE)
    _log("[INFO] 登记校验通过：Region=%s Market=%s timezone=%s" % (region, market, timezone))

    # 一次查询两用：探测 usc 取值是否可用 + 取该证券最早记录用于定周
    earliest_ts, auto_label = week_of_earliest_record(client, code)
    if earliest_ts is None:
        return "failed", ("usc 探测 0 行（usc=eq.%s）—— 该取值在本表中无任何记录，"
                          "疑为取值形态不符，**不产出任何文件**" % code)
    _log("[INFO] usc 探测通过：最早记录 ts=%d（%s UTC）"
          % (earliest_ts, datetime.datetime.fromtimestamp(earliest_ts, tz=datetime.timezone.utc)
             .strftime("%Y-%m-%d %H:%M:%S")))

    if cfg["week_raw"]:
        try:
            iso_year, iso_week = parse_week_arg(cfg["week_raw"])
        except ImportError_ as e:
            return "failed", str(e)
        _log("[INFO] 目标周取自参数：%04dWW%02d（手动路径）" % (iso_year, iso_week))
        auto_mode = False
    elif target_week is not None:
        iso_year, iso_week = target_week
        _log("[INFO] 目标周取自批次计划（周屏障）：%04dWW%02d" % (iso_year, iso_week))
        auto_mode = False
    else:
        iso_year, iso_week = auto_label
        _log("[INFO] 未指定周 → 取该证券最早一条记录所在的周：%04dWW%02d" % (iso_year, iso_week))
        auto_mode = True

    # 「周结束距今满 14 天」：显式指定时违反 = 调用方写错（硬失败）；
    # 自动解析时违反 = 历史还没攒够两周（正常状态，跳过）
    start, end = week_bounds_utc(iso_year, iso_week)
    if end > datetime.datetime.now(datetime.timezone.utc) - \
            datetime.timedelta(days=WEEK_END_LAG_DAYS):
        msg = ("目标周 %04dWW%02d 的结束时刻 %s 距今不足 %d 天"
               % (iso_year, iso_week, end.strftime("%Y-%m-%d %H:%M:%S"), WEEK_END_LAG_DAYS))
        if auto_mode:
            return "skipped", msg + "（历史尚未攒够，跳过）"
        return "failed", msg + "，拒绝导出"

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    _log("[INFO] 目标周 %04dWW%02d：UTC [%s, %s)  ts [%d, %d)"
          % (iso_year, iso_week,
             start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"),
             start_ts, end_ts))

    # 落点：先探测目标分支上是否已有同名文件，有则改用 _N 后缀规避
    path_key, conflict_suffix, err = pick_target_path(
        cfg, region, market, code, iso_year, iso_week)
    if path_key is None:
        return "failed", err
    _log("[INFO] 落点路径：%s%s"
          % (path_key, "（同名冲突规避 _%d）" % conflict_suffix if conflict_suffix else ""))

    rows = fetch_week_rows(client, code, start_ts, end_ts, cfg["page_size"])
    if not rows:
        _log("[INFO] 该周在库中无记录（如长假）—— 按约定产出仅含文件头的空文件")
    else:
        _log("[INFO] 该周共 %d 行，ts 首末 = %s .. %s"
              % (len(rows), rows[0].get("ts"), rows[-1].get("ts")))

    now_local = datetime.datetime.now().astimezone()
    text = build_mvsv(rows, code, region, market, timezone,
                      now_local.strftime("%Y-%m-%d %H:%M:%S"))
    _log("[INFO] .mvsv 生成完毕：%d 行头/数据，%d 字节（UTF-8）"
          % (text.count("\n") + 1, len(text.encode("utf-8"))))

    result = commit_content(
        path_key=path_key,
        content=text,
        branch=cfg["branch"],
        commit_msg="[SupabaseImportWeekMvsv] %s %04dWW%02d（%d 行）"
                   % (code, iso_year, iso_week, len(rows)),
        token=cfg["token"],
    )
    if not result.get("success"):
        return "failed", "提交失败：%s" % result.get("message")
    export_msg = "提交成功（HTTP %s，%s）" % (result.get("http_status"), path_key)
    if stats is not None:
        # 只有**确实落库**的行才计入配额：提交失败时不计，下次运行会重试同一只
        stats["rows"] = len(rows)
        stats["week"] = (iso_year, iso_week)
        stats["path"] = path_key

    # 导出已确认落库，才轮到「删源库」这一步（见 docstring 第七节）
    delete_msg = purge_source_rows(client, cfg, code, start_ts, end_ts, rows)
    if delete_msg is None:
        return "ok", export_msg
    return "partial", export_msg + "；但" + delete_msg


def purge_source_rows(client, cfg, code, start_ts, end_ts, exported_rows):
    """导出成功后的「删源库」环节

    :param exported_rows: 本次实际导出的行（用于与删除前的现状比对）
    :return: None = 无需删或已删净；字符串 = 未删/未删净的原因（计入 partial）
    """
    if not cfg["enable_delete"]:
        # 安全模式：只报数，不动库
        _log("[INFO] 删除开关未开启（%s≠true）—— 本次不删源库数据，仅提示"
              % ENV_ENABLE_DELETE)
        if exported_rows:
            _log("[INFO]   本轮已导出 %d 行，可删未删（开启开关后重跑即会删除）"
                  % len(exported_rows))
        return None

    if not exported_rows:
        _log("[INFO] 该周库中本就无记录，无从删起 —— 跳过删除环节")
        return None

    _log("[INFO] 删除开关已开启 → 准备删除源库中 %s 的 [%d, %d) 区间数据"
          % (code, start_ts, end_ts))
    _log("[INFO] 删除前复核：重查该区间现状，确认与本次导出的行完全一致")

    try:
        before = fetch_week_rows(client, code, start_ts, end_ts, cfg["page_size"])
        before_ts = {int(r["ts"]) for r in before}
        exported_ts = {int(r["ts"]) for r in exported_rows}
    except ImportError_ as e:
        return "删除前复核查询失败，**未执行删除**：%s" % e

    if before_ts != exported_ts:
        only_db = sorted(before_ts - exported_ts)
        only_file = sorted(exported_ts - before_ts)
        _log("[ERROR] 复核不通过：库中现值与本次导出的行不一致，**拒绝删除**")
        _log("[ERROR]   库中如今 %d 行 / 本次导出 %d 行" % (len(before_ts), len(exported_ts)))
        if only_db:
            _log("[ERROR]   仅在库中（导出后新写入？）%d 行，如 ts=%s"
                  % (len(only_db), only_db[:5]))
        if only_file:
            _log("[ERROR]   仅在文件中（导出后被删？）%d 行，如 ts=%s"
                  % (len(only_file), only_file[:5]))
        _log("[ERROR]   宁可少删不可错删：请人工确认后再决定是否开启删除开关")
        return ("删除前复核不通过（库中 %d 行 ≠ 导出 %d 行），已拒绝删除以免丢数据"
                % (len(before_ts), len(exported_ts)))

    try:
        delete_week_rows(client, code, start_ts, end_ts)
        _log("[INFO] DELETE 已发出，执行后复核该区间是否已空")
        after = fetch_week_rows(client, code, start_ts, end_ts, cfg["page_size"])
    except ImportError_ as e:
        return "删除请求失败：%s" % e

    if after:
        return ("删除后该区间仍有 %d 行残留（如 ts=%s），删除未删净"
                % (len(after), [r.get("ts") for r in after[:5]]))
    _log("[INFO] 复核通过：该区间已空（删除 %d 行）" % len(before_ts))
    return None


def main():
    """入口：解析配置 → 取登记表 →（定批次）→ 逐证券处理 → 汇总退出码"""
    _ensure_console_utf8()
    # 依赖模块写往 stderr 的告警也带上时间前缀（stdout 侧由 _log 负责）
    sys.stderr = _TimestampedStream(sys.stderr)

    try:
        cfg = resolve_config()
    except ImportError_ as e:
        _log("❌ %s" % e)
        return 1

    if not cfg["project_ref"] or not cfg["api_key"]:
        _log("❌ 缺少 Supabase 凭据（SUPABASE_PROJECT_REF / SUPABASE_KEY）")
        return 1
    if not cfg["token"]:
        _log("❌ 缺少 GIT_COMMIT_TOKEN")
        return 1

    try:
        client = SupabaseRestClient(cfg["project_ref"], cfg["api_key"])
    except ImportError_ as e:
        _log("❌ %s" % e)
        return 1

    # 名单只有一处：登记表 finv_quote_secu —— 既定准入，也供 Region/Market 与 Timezone
    # （docstring 第三节、第五节）。取不到就无法确定名单，故为硬失败。
    registry, err = fetch_registered_secus(client)
    if err:
        _log("❌ 取登记表 %s 全量失败（名单与 Region/Market/Timezone 均出自该表）：%s"
             % (SECU_TABLE, err))
        return 1

    # 未指定证券时取登记表中的全部 usc（按主键升序，结果可复现）
    codes = cfg["codes"] if cfg["codes"] else list(registry.keys())
    if cfg["codes"]:
        _log("[INFO] 待处理证券取自参数：%d 个" % len(codes))
    else:
        _log("[INFO] 未指定证券 → 取 %s 登记表中的全部证券：%d 个" % (SECU_TABLE, len(codes)))

    _log("[INFO] 目标分支 = %s" % cfg["branch"])
    _log("[INFO] 删除源库开关 %s = %s" % (ENV_ENABLE_DELETE,
                                      "开" if cfg["enable_delete"] else "关（安全模式，只导出不删除）"))
    _log("[INFO] 每日配额 %s = %s"
         % (ENV_DAILY_QUOTA,
            "不限额" if cfg["daily_quota"] <= 0 else "%d 行" % cfg["daily_quota"]))

    # 逐证券处理之前先盘一次源库存量：把「上游在采、这边名单里没有」的证券逐只点名
    audit_secu_inventory(client, registry)

    # 两条路径（docstring 第十三节）：
    #   手动 —— 显式指定 WEEK：运维动作，全部证券同一周，**不受每日配额限制**
    #   日常 —— 周屏障定出目标周，按公平顺序导到配额为止
    if cfg["week_raw"]:
        _log("[INFO] 目标周：%s（全部证券同一周；手动路径，**不受每日配额限制**）"
             % cfg["week_raw"])
        queue = [(code, None) for code in codes]
        quota = 0
    else:
        _log("[INFO] 目标周：未指定 → 走每日批次（周屏障 + 公平顺序 + 配额）")
        plan, err = plan_daily_batch(client, cfg, registry, codes)
        if err:
            _log("❌ 批次计划失败：%s" % err)
            return 1
        if plan["blocked"]:
            _log("")
            _log("[INFO] 未进入本次队列的 %d 只：" % len(plan["blocked"]))
            for code in sorted(plan["blocked"]):
                _log("[INFO]   %s：%s" % (code, plan["blocked"][code]))
        if plan["week"] is None:
            _log("")
            _log("[INFO] 全部证券均已追平可导出周 —— 本次无待导数据"
                  "（正常：新的一周尚未跨过 %d 天线）" % WEEK_END_LAG_DAYS)
            return 0
        queue = [(code, plan["week"]) for code in plan["ordered"]]
        quota = cfg["daily_quota"]

    ok, skipped, failed, partial = [], [], [], []
    exported_rows = 0
    for i, (code, week) in enumerate(queue, 1):
        if quota > 0 and exported_rows >= quota:
            _log("")
            _log("[INFO] 已达每日配额 %d 行（本批实际已导 %d 行）—— 就此收工；"
                  "剩余 %d 只留待下次运行（屏障会自动接上同一周 %04dWW%02d）"
                  % (quota, exported_rows, len(queue) - i + 1, week[0], week[1]))
            break
        _log("")
        _log("===== [%d/%d] 证券 %s 开始（本批已导 %d 行）====="
             % (i, len(queue), code, exported_rows))
        stats = {}
        try:
            status, message = process_one(client, cfg, registry, code,
                                          target_week=week, stats=stats)
        except ImportError_ as e:
            status, message = "failed", str(e)
        exported_rows += stats.get("rows", 0)
        _log("[INFO] 本只 %d 行，本批累计 %d 行" % (stats.get("rows", 0), exported_rows))
        if status == "ok":
            ok.append(code)
            _log("✅ %s：%s" % (code, message))
        elif status == "skipped":
            skipped.append(code)
            _log("⏭️  %s：%s" % (code, message))
        elif status == "partial":
            partial.append("%s：%s" % (code, message))
            _log("⚠️  %s：%s" % (code, message))
        else:
            failed.append(code)
            _log("❌ %s：%s" % (code, message))

    def fmt(lst):
        return ", ".join(lst) if lst else "（无）"

    _log("")
    _log("===== 汇总 =====")
    _log("本批实际导出 %d 行（配额 %s）"
         % (exported_rows, "不限" if quota <= 0 else "%d 行" % quota))
    _log("成功 %d 个：%s" % (len(ok), fmt(ok)))
    _log("跳过 %d 个：%s" % (len(skipped), fmt(skipped)))
    _log("失败 %d 个：%s" % (len(failed), fmt(failed)))
    _log("部分成功（已导出但源库未删净）%d 个：%s" % (len(partial), fmt(partial)))
    if partial:
        _log("   ↑ 文件已在分支上，源库数据仍在；排查后重跑即会重试删除环节")
    # 跳过不算失败：属「历史尚未攒够两周」的正常状态
    if partial:
        return 2
    if failed and (ok or skipped):
        return 2
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
