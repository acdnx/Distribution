# -*- coding: utf-8 -*-
"""Supabase 作业仓库：作业查询、表行到任务字典的映射、作业状态回写。

从 `QuoteCollectRunner.py` 提取，是本目录内**唯一**与 Supabase PostgREST 耦合的模块。
更换作业来源（外部调度系统、云函数推任务等）时只需替换本模块。

对外用途：
    SupabaseRestClient     PostgREST 最小客户端（仅标准库 urllib）
    query_next_job         按「就绪」规则挑一条可运行作业
    build_task_from_job    表行 -> 采集任务字典（字段口径与归一化在此完成）
    JobStateWriter         作业状态回写（受开关控制，关闭时只打印待执行 SQL）

作业状态流转全部由本仓库脚本维护：COLLECTING / COMPLETED / FAILED / ABORTED，
并负责 FAILED、ABORTED -> READY 的重试回退。
"""

from datetime import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from JobCore import (JobExecutionError, STANDARD_TYPE_KLINE, _envText, _log,
                      _shortError, _warn, normalize_period, normalize_type_kline)

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
    """把作业表行适配成采集任务的 task dict（字段映射见模块 docstring 第四节）

    产出除采集链路的 9 个字段外，另带 `_job` 子字典承载作业元数据（id / job_name /
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
        # ---- 采集侧口径（collectMinuteBars / buildMvsvName / writeMvsv 直接消费）----
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
