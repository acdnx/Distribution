#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HttpUtil —— 通用 HTTP 收发工具库（纯标准库 urllib）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把「发送 HTTP 请求并归一化结果」的通用能力集中到一个文件维护，供同目录各模块
（当前 SupabaseRestClient.py、FtmmQuoteV2WebRestClient.py，后续可扩展）import 复用，
避免每个模块各写一份 urllib 细节、改超时或改错误口径时多处漂移。

为什么要从 GitHubCommitContent.py 里抽出来独立成文件：
    - 此前 HTTP 收发直接依赖 GitHubCommitContent 的私有函数 _request，属于
      「跨模块依赖他人私有实现」：该模块职责是 GitHub 文件递交，与 HTTP 传输无关，
      且是只读共享库（未经允许不得修改），对方一旦调整就会连锁受影响；
    - 抽成公共文件后，各模块只依赖本层契约，GitHubCommitContent 回归单一职责，
      依赖方向也从「业务模块 → GitHub 递交模块」改为「业务模块 → 通用传输层」。

形态说明：
    - 纯函数库，没有命令行入口；import 无副作用、不触网；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**，模块私有实现
      （下划线前缀）保持 snake_case；
    - 仅标准库；不重试、不打日志、不含任何业务语义（不认得 Supabase / 行情 / GitHub），
      成败判定与降级策略全部交给调用方。

二、返回值契约
----------------------------------------------------------------------------------------
sendRequest 统一返回三元组 (status, body_text, error)：

    | 情形                        | status      | body_text | error      |
    |-----------------------------|-------------|-----------|------------|
    | 2xx / 3xx 服务器正常应答    | int 状态码  | 响应体    | None       |
    | 4xx / 5xx 服务器错误应答    | int 状态码  | 错误体    | None       |
    | 网络层失败（超时/DNS/重置） | None        | ""        | 原因字符串 |

即：**HTTP 错误码不算异常**，原样返回交给调用方按业务判定；
只有「请求根本没到达服务器」才算 error（status 为 None）。

三、使用示例
----------------------------------------------------------------------------------------
    from HttpUtil import sendRequest, parseJson

    status, text, err = sendRequest("GET", "https://api.example.com/x",
                                    headers={"Accept": "application/json"}, timeout=30)
    if err:
        print("网络错误:", err)              # 未到达服务器
    elif not (200 <= status < 300):
        print("HTTP", status, text)          # 服务器明确拒绝
    else:
        data = parseJson(text)              # 解析失败返回 None

四、注意事项
----------------------------------------------------------------------------------------
    - 响应体按 UTF-8（errors=replace）解码，永远不会因编码问题抛异常；
    - 超时按单次请求计，不重试；需要重试的调用方自行在外层循环控制；
    - 所有异常在本层收敛为返回值，不会向外抛出（调用方无需 try/except 包网络错误）。

【环境要求】Python 3.8+，仅标准库。
"""

import json
import urllib.error
import urllib.request

# ============ 常量 ============
# 单次请求默认超时（秒）
DEFAULT_TIMEOUT = 30


def sendRequest(method, url, headers=None, body_bytes=None, timeout=DEFAULT_TIMEOUT):
    """发送一次 HTTP 请求并归一化结果（异常已在本层收敛，不会抛出）

    :param method: HTTP 方法（GET / POST / PUT / PATCH / DELETE 等）
    :param url: 完整请求 URL（查询串由调用方自行拼装）
    :param headers: 请求头 dict；None 视为空头
    :param body_bytes: 请求体字节；None 表示无 body
    :param timeout: 超时秒数，默认 DEFAULT_TIMEOUT
    :return: (status, body_text, error) 三元组，语义见模块文档「二、返回值契约」：
        - status: int（含 HTTP 4xx/5xx 原样返回）或 None（网络层失败）；
        - body_text: 响应体按 UTF-8（errors=replace）解码后的字符串；
        - error: 网络层失败原因字符串；请求到达服务器（含 HTTP 错误码）时为 None。
    """
    req = urllib.request.Request(url, data=body_bytes,
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, text, None
    except urllib.error.HTTPError as e:
        # HTTP 错误码是服务器的正常应答（错误体常含 "message"），交给调用方判定
        text = e.read().decode("utf-8", errors="replace")
        return e.code, text, None
    except urllib.error.URLError as e:
        return None, "", "网络错误: %s" % (e.reason if e.reason is not None else e)
    except TimeoutError:
        return None, "", "请求超时(>%ss)" % timeout
    except OSError as e:
        return None, "", "网络/IO错误: %s" % e


def parseJson(text):
    """解析 JSON 文本；空文本或解析失败返回 None（不抛异常）

    :param text: 待解析的文本（通常来自 sendRequest 的 body_text）
    :return: 解析结果对象（dict / list 等）；失败返回 None
    """
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("HttpUtil 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from HttpUtil import sendRequest, parseJson")
    print("    status, text, err = sendRequest('GET', 'https://api.example.com/x')")
