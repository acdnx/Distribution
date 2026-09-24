#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SupabaseRestClient —— Supabase Data API（PostgREST）最小客户端
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把「经 PostgREST 访问 Supabase 数据库」的通用能力（凭据解析、URL 拼装、认证头、
请求发送、错误归一化、过滤条件构造）集中到一个文件维护，供同目录各脚本
（当前 FinvQuoteCollectPollFtmm.py，后续可扩展）import 复用，避免每个脚本各写一份
HTTP 细节、改认证或改超时口径时多处漂移。

形态说明：
    - 纯函数库，没有命令行入口；import 时不触网、无全局副作用（环境变量在调用时读）；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，模块私有实现
      （下划线前缀）保持 snake_case；
    - HTTP 收发复用同目录 HttpUtil 的 sendRequest（通用传输层，纯函数，无副作用）；
      不再依赖 GitHubCommitContent 的私有实现——那是只读共享库且职责是文件递交；
    - 仅标准库实现；每个请求同时携带 apikey 与 Authorization: Bearer 两个头
      （Supabase 要求二者并存）；
    - 失败抛 SupabaseRestError（message 已做项目引用遮蔽），调用方决定降级策略。

二、凭据解析
----------------------------------------------------------------------------------------
| 输入         | 优先级                                                          |
|--------------|-----------------------------------------------------------------|
| project_ref  | 1) 构造函数显式传入   2) 环境变量 SUPABASE_PROJECT_REF            |
| api_key      | 1) 构造函数显式传入   2) 环境变量 SUPABASE_KEY                    |
fromEnv() 即上述环境变量路径；两者任一缺失抛 ValueError（不打印取值）。

三、环境变量
----------------------------------------------------------------------------------------
    SUPABASE_PROJECT_REF   Supabase 项目引用（Dashboard 地址中 .supabase.co 前一段）
    SUPABASE_KEY           Supabase API 密钥（service-role；不落日志）

四、使用示例
----------------------------------------------------------------------------------------
    from SupabaseRestClient import SupabaseRestClient, SupabaseRestError, eqFilter

    try:
        client = SupabaseRestClient.fromEnv()
    except ValueError as e:
        print("缺少凭据:", e)

    # 读：从视图取（视图可直出元数据列与状态列；主键 usc）
    rows = json.loads(client.query(
        "finv_quote_collect_state_poll_futu_view",
        "select=usc,region,market,timezone,symbol,dt_last_check&order=usc.asc"))
    # 写：一律落表（视图只读）
    client.patch("finv_quote_collect_state_poll_futu",
                 eqFilter("usc", "159937"),
                 {"count_fail": 0})

五、注意事项
----------------------------------------------------------------------------------------
    - PostgREST 默认单页最多返回 1000 行，行数超过需调用方自行分页；
    - **视图（读）与表（写）是两条通道**：视图列由视图定义决定，查询里列到不存在的列
      会直接 400，故调用方的 select 列清单必须与视图定义严格对齐（例如视图不暴露
      flag_enable 时不要拼该过滤条件）；
    - 响应中的 timestamptz 为 **UTC 偏移的 ISO 字符串**，解析请用 DateTimeUtil.parseDt；
    - 日志中的项目引用一律遮蔽为 ***，密钥不落任何输出。

【环境要求】Python 3.8+，仅标准库；可直连 *.supabase.co。
"""

import json
import os
import urllib.parse

from HttpUtil import sendRequest

# ============ 常量 ============
# 凭据环境变量名（约定专用）
ENV_PROJECT_REF = "SUPABASE_PROJECT_REF"
ENV_API_KEY = "SUPABASE_KEY"
# 单次请求默认超时（秒）
DEFAULT_TIMEOUT = 30
# PostgREST 更新时回写表示（便于调用方确认命中行数）
PREFER_REPRESENTATION = "return=representation"


class SupabaseRestError(Exception):
    """PostgREST 非 2xx / 网络错误（message 已做项目引用遮蔽）"""


def eqFilter(column, value):
    """构造 PostgREST 等值过滤片段：{column}=eq.{urlencoded(value)}

    :param column: 列名
    :param value: 取值（会被 URL 编码，支持非 ASCII 与特殊字符）
    :return: 可直接拼进查询串的片段，如 usc=eq.HSI
    """
    return "%s=eq.%s" % (column, urllib.parse.quote(str(value), safe=""))


class SupabaseRestClient:
    """Supabase Data API（PostgREST）最小客户端：query / patch"""

    def __init__(self, project_ref, api_key, timeout=DEFAULT_TIMEOUT):
        """构造客户端

        :param project_ref: Supabase 项目引用（只填 ref 本身，不带协议或域名）
        :param api_key: Supabase API 密钥
        :param timeout: 单次请求超时秒数，默认 30
        :raises ValueError: 凭据缺失或格式非法
        """
        ref = (project_ref or "").strip()
        if not ref:
            raise ValueError("Supabase 项目引用（%s）不能为空" % ENV_PROJECT_REF)
        if "/" in ref or ":" in ref:
            raise ValueError("项目引用只填 ref 本身，不要带协议或域名")
        if not (api_key or "").strip():
            raise ValueError("Supabase API 密钥（%s）不能为空" % ENV_API_KEY)
        self.rest_url = "https://%s.supabase.co/rest/v1" % ref
        self.api_key = api_key.strip()
        self.timeout = timeout

    @classmethod
    def fromEnv(cls, timeout=DEFAULT_TIMEOUT):
        """从环境变量构造客户端（SUPABASE_PROJECT_REF / SUPABASE_KEY）

        :raises ValueError: 任一凭据缺失
        """
        return cls(os.environ.get(ENV_PROJECT_REF, ""),
                   os.environ.get(ENV_API_KEY, ""),
                   timeout=timeout)

    def _redact(self, text):
        """把文本中的项目引用（嵌在主机名里）替换为 ***，供日志输出"""
        return text.replace(self.rest_url.split("//")[1].split(".")[0], "***")

    def _headers(self, prefer=None, with_body=False):
        """构造认证头；prefer 为 PostgREST 的 Prefer 取值（可 None）"""
        headers = {
            "apikey": self.api_key,
            "Authorization": "Bearer %s" % self.api_key,
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _send(self, method, url, body_json=None, prefer=None, operation="请求"):
        """发送请求并校验 2xx（URL 遮蔽后进日志）

        :raises SupabaseRestError: 网络错误或响应非 2xx
        """
        body_bytes = json.dumps(body_json, ensure_ascii=False).encode("utf-8") \
            if body_json is not None else None
        headers = self._headers(prefer=prefer, with_body=body_bytes is not None)
        status, text, err = sendRequest(method, url, headers, body_bytes, self.timeout)
        if err:
            raise SupabaseRestError("%s网络错误: %s" % (operation, err))
        if status is None or not (200 <= status < 300):
            raise SupabaseRestError("%s失败，HTTP %s，URL %s，响应: %s"
                                    % (operation, status, self._redact(url), (text or "")[:300]))
        return text

    def query(self, table, query_string=None):
        """查询行（GET），返回 JSON 数组原文；无命中行为 []

        :param table: 表名
        :param query_string: PostgREST 查询串（不含 ?），如 select=a,b&id=eq.1
        :return: 响应原文（调用方 json.loads）
        :raises SupabaseRestError: 网络错误或响应非 2xx
        """
        url = "%s/%s" % (self.rest_url, urllib.parse.quote(table, safe=""))
        if query_string:
            url = url + "?" + query_string
        return self._send("GET", url, operation="查询")

    def patch(self, table, filters, row):
        """按过滤条件更新行（PATCH），返回更新后的行数组原文 JSON

        :param table: 表名
        :param filters: 过滤条件片段（可用 eqFilter 构造），如 usc=eq.HSI
        :param row: 待更新的列 dict（只出现于 dict 的列会被更新）
        :return: 响应原文（[] 表示未命中任何行）
        :raises SupabaseRestError: 网络错误或响应非 2xx
        """
        url = "%s/%s?%s" % (self.rest_url, urllib.parse.quote(table, safe=""), filters)
        return self._send("PATCH", url, body_json=row,
                          prefer=PREFER_REPRESENTATION, operation="状态回写")


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("SupabaseRestClient 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from SupabaseRestClient import SupabaseRestClient, SupabaseRestError, eqFilter")
