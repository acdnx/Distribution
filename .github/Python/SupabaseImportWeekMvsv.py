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

三、查询与分页
----------------------------------------------------------------------------------------
Supabase Data API（PostgREST）默认一次最多回 1000 行，而 7×24 品种一周有 10080 分钟，
故必须分页。采用 **keyset（游标）分页**：按 ts 升序，每页取 limit 行，下一页把
`ts=gte.<周起点>` 换成 `ts=gt.<上一页最后一行的 ts>`，直到某页不足 limit 行为止。

选 keyset 而非 offset 的理由：主键就是 (usc, ts)，同一 usc 下 ts 不会重复 ⇒ 游标严格单调，
**不漏行也不重行**；且正好走索引 idx_finv_quote_secu_secu_ts (usc, ts)。

四、输出文件规范（.mvsv）
----------------------------------------------------------------------------------------
21 行中英双段元信息头 + 1 空行 + 数据行（末行**无结尾换行**）：

    # 标题 / # 数据供应商 / # 字段 / # 字段名称 / # 字段类型 / # 计数 / # 采集时间 /
    # 证券代码 / # 地区 / # 市场 / # 备注                        （中文段 11 行）
    # Title / # DataProvider / # Field / # FieldName / # FieldType / # Count /
    # FetchTime / # SecuCode / # Region / # Market               （英文段 10 行）

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
    Data/Finv/SecuQuoteWeek/FT/{region}_{market}/{Code}/{region}_{market}_{Code}_MIN_{yyyyWW}.mvsv

例（Code=IAU / region=US / market=ARCA / yyyyWW=202625）：
    Data/Finv/SecuQuoteWeek/FT/US_ARCA/IAU/US_ARCA_IAU_MIN_202625.mvsv

与既有 Day 规范 Data/Finv/SecuQuote/FT/{Freq}/{Region}_{Market}/{Code}/… **七层同构**，
唯一差异是周期与日期段（_Min_FT_yyyyMMdd ↔ _MIN_yyyyWW）。

Region / Market **不在表里**，由同目录 SecuMetaMapping.jsonl 按 Code 查得。

六、环境变量（凭据一律经环境注入，严禁写进源码或日志）
----------------------------------------------------------------------------------------
    SUPABASE_PROJECT_REF   Supabase 项目引用（必填）
    SUPABASE_KEY           Supabase API 密钥（service-role；必填，不落日志）
    GIT_COMMIT_TOKEN       GitHub 令牌（提交 .mvsv 用；必填）
    SUPABASE_PAGE_SIZE     单页行数（默认 1000）
    SECU_CODE              证券代码，多个以逗号分隔（如 IAU 或 IAU,GLD）；
                           **留空则取同目录 SecuMetaMapping.jsonl 中的全部 Code**
    WEEK                   目标周 yyyyWW（可省；省则取该证券「最早一条记录」所在的周）
    CURR_BRANCH / GITHUB_REF_NAME   目标分支（默认 quote）
    SECU_META_MAPPING      映射表路径（默认同目录 SecuMetaMapping.jsonl）

命令行参数优先于同名环境变量：argv[1]=SECU_CODE，argv[2]=WEEK，argv[3]=CURR_BRANCH。

每个证券**独立处理**：各自一次查询、各自一份文件、各自一次提交；互不影响，单个失败不
阻断后续证券。

七、usc 探测 = 取最早记录（一查两用）
----------------------------------------------------------------------------------------
库里的 usc 若写成「裸码」（IAU）而不是「全码」，查询会返回 0 行 —— 而这与「该周真的没有
数据」（如国庆长假）产出的**空文件外观完全一致，无法区分**。故在正式取数前先查该证券的
最早一条记录：

    GET /rest/v1/<表>?select=ts&usc=eq.<码>&order=ts.asc&limit=1

这一条查询**同时办两件事**：
    1. 探测 —— 0 行 ⇒ 判定 usc 取值可疑，**该证券直接失败退出**，不产出任何文件；
    2. 定周 —— 有行 ⇒ 取其 ts 所在的那一周作为「未显式指定 WEEK」时的目标周。

查询语句会打进日志，故 usc 的真实取值形态从首次运行的日志即可读出。

八、退出码
----------------------------------------------------------------------------------------
    0 = 全部证券处理完毕（含「该周无数据」→ 产出仅含文件头的空文件；含「自动定周但历史
        尚未攒够 14 天」→ 跳过，两者均属预期状态）
    1 = 致命错误（凭据缺失 / 分支未定 / 映射表缺失）或全部证券失败
    2 = 部分证券失败（其余成功或跳过）

【环境要求】Python 3.8+，仅标准库；可直连 api.github.com 与 *.supabase.co。
"""

import datetime
import json
import os
import sys
import urllib.parse
from decimal import Decimal

# 同目录纯函数库：复用其 HTTP 请求 / 认证头 / 目标仓库与分支解析（纯函数，无副作用）
from GitHubCommitContent import (
    _ensure_console_utf8,
    _request,
    commit_content,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# Supabase / PostgREST 源表（与 Java 版 FinvQuoteSecuKlineMin 一致）
TABLE = "finv_quote_secu_kline_min"

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

# 落点路径模板（见模块 docstring 第五节）
TARGET_PATH_TEMPLATE = "Data/Finv/SecuQuoteWeek/FT/%s_%s/%s/%s_%s_%s_MIN_%04d%02d.mvsv"

# 周结束距今至少需要的天数（见 docstring 第二节）
WEEK_END_LAG_DAYS = 14

# 默认配置
DEFAULT_BRANCH = "quote"
DEFAULT_PAGE_SIZE = 1000

# 映射表（与脚本同目录）
SECU_META_MAPPING_FILE = "SecuMetaMapping.jsonl"

# PostgREST 请求超时（秒）
HTTP_TIMEOUT = 60


class ImportError_(Exception):
    """本工具的业务错误（与内建 ImportError 区分开，避免误捕获）"""


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
# 映射表
# ---------------------------------------------------------------------------

def load_secu_meta_mapping(path):
    """加载 SecuMetaMapping.jsonl（一行一个 {Code, Region, Market} JSON）

    :param path: 文件路径
    :return: dict {Code: (Region, Market)}
    :raises ImportError_: 文件缺失，或没有任何有效记录时抛出
    """
    if not os.path.isfile(path):
        raise ImportError_("证券元信息映射表不存在：%s" % path)
    mapping = {}
    bad = 0
    with open(path, "r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                print("⚠️ %s 第 %d 行不是合法 JSON，已跳过" % (path, line_no))
                bad += 1
                continue
            if not isinstance(data, dict):
                bad += 1
                continue
            code = str(data.get("Code", "")).strip()
            region = str(data.get("Region", "")).strip()
            market = str(data.get("Market", "")).strip()
            if not (code and region and market):
                print("⚠️ %s 第 %d 行缺 Code/Region/Market，已跳过" % (path, line_no))
                bad += 1
                continue
            mapping[code] = (region, market)
    if not mapping:
        raise ImportError_("证券元信息映射表无有效记录：%s" % path)
    print("[INFO] 映射表 %s：有效 %d 条，跳过 %d 条" % (path, len(mapping), bad))
    return mapping


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
    quoted_usc = urllib.parse.quote(usc, safe="")
    while True:
        lower = ("ts=gte.%d" % start_ts) if cursor is None else ("ts=gt.%d" % cursor)
        qs = ("select=%s&usc=eq.%s&%s&ts=lt.%d&order=ts.asc&limit=%d"
              % (SELECT_COLUMNS, quoted_usc, lower, end_ts, page_size))
        data = parse_rows(client.query(TABLE, qs, operation="取数第 %d 页" % (page + 1)))
        page += 1
        rows.extend(data)
        print("[INFO]   第 %d 页：%d 行（累计 %d）" % (page, len(data), len(rows)))
        if len(data) < page_size:
            break
        cursor = int(data[-1]["ts"])
    return rows


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


def build_mvsv(rows, code, region, market, fetch_time_text):
    """生成完整的 .mvsv 文本（21 行头 + 空行 + 数据行，末行无结尾换行）

    :param rows: 行数组（可能为空 = 该周无数据，仍产出仅含文件头的文件）
    :param code: 证券代码（裸码，如 IAU）
    :param region: 地区（如 US）
    :param market: 市场（如 ARCA）
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
        "",
    ]
    lines.extend(row_text(r) for r in rows)
    return "\n".join(lines)


def build_target_path(region, market, code, iso_year, iso_week):
    """拼出落点路径（见模块 docstring 第五节）"""
    return TARGET_PATH_TEMPLATE % (region, market, code, region, market, code,
                                   iso_year, iso_week)


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

    # 留空 = 用映射表里的全部 Code（由 main 载入映射表后补齐）
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
        "project_ref": os.environ.get("SUPABASE_PROJECT_REF", "").strip(),
        "api_key": os.environ.get("SUPABASE_KEY", "").strip(),
        "token": os.environ.get("GIT_COMMIT_TOKEN", "").strip(),
    }


def process_one(client, cfg, mapping, code, mapping_path):
    """处理单个证券：探测=取最早记录 →（定周）→ 取数 → 生成 → 提交

    :return: ("ok"|"skipped"|"failed", 结果描述)
    """
    meta = mapping.get(code)
    if meta is None:
        return "failed", "Code %s 在 %s 中无记录（拿不到 Region/Market）" % (code, mapping_path)
    region, market = meta

    # 一次查询两用：探测 usc 取值是否可用 + 取该证券最早记录用于定周
    earliest_ts, auto_label = week_of_earliest_record(client, code)
    if earliest_ts is None:
        return "failed", ("usc 探测 0 行（usc=eq.%s）—— 该取值在本表中无任何记录，"
                          "疑为取值形态不符，**不产出任何文件**" % code)
    print("[INFO] usc 探测通过：最早记录 ts=%d（%s UTC）"
          % (earliest_ts, datetime.datetime.fromtimestamp(earliest_ts, tz=datetime.timezone.utc)
             .strftime("%Y-%m-%d %H:%M:%S")))

    if cfg["week_raw"]:
        try:
            iso_year, iso_week = parse_week_arg(cfg["week_raw"])
        except ImportError_ as e:
            return "failed", str(e)
        print("[INFO] 目标周取自参数：%04dWW%02d" % (iso_year, iso_week))
        auto_mode = False
    else:
        iso_year, iso_week = auto_label
        print("[INFO] 未指定周 → 取该证券最早一条记录所在的周：%04dWW%02d" % (iso_year, iso_week))
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
    path_key = build_target_path(region, market, code, iso_year, iso_week)

    print("[INFO] 证券 %s：Region=%s Market=%s" % (code, region, market))
    print("[INFO] 目标周 %04dWW%02d：UTC [%s, %s)  ts [%d, %d)"
          % (iso_year, iso_week,
             start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"),
             start_ts, end_ts))
    print("[INFO] 落点路径：%s" % path_key)

    rows = fetch_week_rows(client, code, start_ts, end_ts, cfg["page_size"])
    if not rows:
        print("[INFO] 该周在库中无记录（如长假）—— 按约定产出仅含文件头的空文件")
    else:
        print("[INFO] 该周共 %d 行，ts 首末 = %s .. %s"
              % (len(rows), rows[0].get("ts"), rows[-1].get("ts")))

    now_local = datetime.datetime.now().astimezone()
    text = build_mvsv(rows, code, region, market,
                      now_local.strftime("%Y-%m-%d %H:%M:%S"))
    print("[INFO] .mvsv 生成完毕：%d 行头/数据，%d 字节（UTF-8）"
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
    return "ok", "提交成功（HTTP %s，%s）" % (result.get("http_status"), path_key)


def main():
    """入口：解析配置 → 载入映射表 → 逐证券独立处理 → 汇总退出码"""
    _ensure_console_utf8()

    try:
        cfg = resolve_config()
    except ImportError_ as e:
        print("❌ %s" % e)
        return 1

    if not cfg["project_ref"] or not cfg["api_key"]:
        print("❌ 缺少 Supabase 凭据（SUPABASE_PROJECT_REF / SUPABASE_KEY）")
        return 1
    if not cfg["token"]:
        print("❌ 缺少 GIT_COMMIT_TOKEN")
        return 1

    script_dir = os.path.dirname(os.path.abspath(__file__))
    mapping_path = os.environ.get("SECU_META_MAPPING", "").strip() \
        or os.path.join(script_dir, SECU_META_MAPPING_FILE)

    try:
        mapping = load_secu_meta_mapping(mapping_path)
    except ImportError_ as e:
        print("❌ %s" % e)
        return 1

    # 未指定证券时取映射表中的全部 Code（保持文件内顺序，结果可复现）
    codes = cfg["codes"] if cfg["codes"] else list(mapping.keys())
    if cfg["codes"]:
        print("[INFO] 待处理证券取自参数：%d 个" % len(codes))
    else:
        print("[INFO] 未指定证券 → 取映射表中的全部 Code：%d 个" % len(codes))

    try:
        client = SupabaseRestClient(cfg["project_ref"], cfg["api_key"])
    except ImportError_ as e:
        print("❌ %s" % e)
        return 1

    print("[INFO] 目标分支 = %s" % cfg["branch"])
    if cfg["week_raw"]:
        print("[INFO] 目标周：%s（全部证券同一周）" % cfg["week_raw"])
    else:
        print("[INFO] 目标周：未指定 → 逐个证券取其「最早一条记录」所在的周")

    ok, skipped, failed = [], [], []
    for i, code in enumerate(codes, 1):
        print("\n===== [%d/%d] 证券 %s 开始 =====" % (i, len(codes), code))
        try:
            status, message = process_one(client, cfg, mapping, code, mapping_path)
        except ImportError_ as e:
            status, message = "failed", str(e)
        if status == "ok":
            ok.append(code)
            print("✅ %s：%s" % (code, message))
        elif status == "skipped":
            skipped.append(code)
            print("⏭️  %s：%s" % (code, message))
        else:
            failed.append(code)
            print("❌ %s：%s" % (code, message))

    def fmt(lst):
        return ", ".join(lst) if lst else "（无）"

    print("\n===== 汇总 =====")
    print("成功 %d 个：%s" % (len(ok), fmt(ok)))
    print("跳过 %d 个：%s" % (len(skipped), fmt(skipped)))
    print("失败 %d 个：%s" % (len(failed), fmt(failed)))
    # 跳过不算失败：属「历史尚未攒够两周」的正常状态
    if failed and (ok or skipped):
        return 2
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
