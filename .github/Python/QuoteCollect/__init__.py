# -*- coding: utf-8 -*-
"""QuoteCollect —— 行情采集作业包。

本目录已完成模块化拆分，共 7 个模块，依赖严格单向（无循环）：

    JobCore.py            作业公共基础件（异常 / 日志 / 环境变量 / 字段归一化）
    MoomooAuth.py         moomoo 认证（请求签名 + 纯标准库密码学）
    MvsvWriter.py         MVSV 生成（格式定义 / 数据模型 / 序列化）
    SupabaseJobRepo.py    Supabase 作业仓库（查询 / 映射 / 状态回写）
    ArchivePublisher.py   归档落点发布（路径 / 指纹 / Contents API 推送）
    MoomooQuoteClient.py  moomoo 客户端库（契约见其 __all__）
    QuoteCollectRunner.py **入口**：编排七个阶段、采集链路、CLI

自下而上：JobCore / MoomooAuth / MvsvWriter 仅依赖标准库，可被其他模块直接复用。

本包标记（__init__.py）的用途：
    1. 使入口脚本既能按路径直接执行（CI 即如此），也能以 `import QuoteCollect.*` 引用；
    2. 为将来新增本目录模块预留包结构。

**本文件刻意不导出任何符号** —— 入口与公共契约分散在各自模块中，集中 re-export
会额外增加一层需要维护的 API 面，收益不明确。取用请直接 import 对应模块。
"""
