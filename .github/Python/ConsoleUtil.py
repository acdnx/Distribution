#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConsoleUtil —— 控制台输出工具库（纯标准库）
========================================================================================

一、工具定位
----------------------------------------------------------------------------------------
把「Windows 遗留控制台 / 非 UTF-8 locale 下安全输出中文」的通用能力集中到一个文件
维护，供同目录各脚本 import 复用。

为什么要从 GitHubCommitContent.py 里抽出来独立成文件：
    - 此前各脚本直接依赖 GitHubCommitContent 的私有函数 _ensure_console_utf8，
      与「HTTP 收发依赖其 _request」是同一类问题：跨模块依赖他人私有实现，
      而对方职责是 GitHub 文件递交且为只读共享库；
    - 抽成公共文件后，控制台编码这类运行环境适配归位到通用层，
      GitHubCommitContent 只暴露自己的公开递交能力（commit_content）。

形态说明：
    - 纯函数库，没有命令行入口；import 无副作用（重配置只在实际调用时发生）；
    - 命名约定：**对外公开方法一律小驼峰（lowerCamelCase）**；
    - 仅标准库；失败静默跳过，绝不影响程序主流程。

二、使用示例
----------------------------------------------------------------------------------------
    from ConsoleUtil import ensureConsoleUtf8

    ensureConsoleUtf8()      # 建议在 main() 首行调用一次，之后放心 print 中文
    print("中文输出不会因为 cp437 / C locale 而崩溃")

三、注意事项
----------------------------------------------------------------------------------------
    - 重配置为 UTF-8（errors=replace），仅首次调用生效（模块内标志位记忆）；
    - 流已被重定向接管或不支持 reconfigure 时静默跳过，不抛异常；
    - 只处理 stdout / stderr，不触碰 stdin。

【环境要求】Python 3.7+（依赖 IOBase.reconfigure），仅标准库。
"""

import sys

# 控制台重配置只执行一次的标志（模块级，import 时不触发任何动作）
_console_configured = False


def ensureConsoleUtf8():
    """将 stdout / stderr 重配置为 UTF-8（errors=replace），仅首次调用生效

    防止 Windows 遗留控制台 / 非 UTF-8 locale（如代码页 437、C locale）下
    输出中文时抛 UnicodeEncodeError 导致静默崩溃；重配置失败（如流已被重定向
    接管）时静默跳过，不影响程序运行。

    :return: 无
    """
    global _console_configured
    if _console_configured:
        return
    _console_configured = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    # 本文件是纯函数库，无命令行入口；直接执行仅输出使用引导（便于误执行时自解释）
    print("ConsoleUtil 是纯函数库，没有命令行入口，直接执行无任何动作。")
    print("请在【同目录】的其他 Python 脚本中 import 使用：")
    print("    from ConsoleUtil import ensureConsoleUtf8")
    print("    ensureConsoleUtf8()")
