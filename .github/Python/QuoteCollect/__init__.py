# -*- coding: utf-8 -*-
"""QuoteCollect —— 行情采集作业包。

当前 `QuoteCollectRunner.py` 为**行情库内联**形态（便于单文件部署）：行情客户端库
（含 Ed25519 / RSA-SHA256 签名与纯标准库密码学实现）已内联其中；对 Contents API 则
**直接调用上一级目录既有的 `GitHubCommitContent` 封装**（刻意不内联，避免两份实现漂移）。
后续将按功能拆分为多个模块（如作业查询、采集、MVSV 序列化、落点推送、状态流转），
故在此预置包标记，使拆分后的模块既可直接执行入口脚本，也可被 `import QuoteCollect.*` 引用。
"""
