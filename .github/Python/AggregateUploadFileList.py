#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AggregateUploadFileList —— 把 Branch/{branch}/ 下的 UploadFileList_*.jsonl 增量聚合进 UploadFileList.jsonl
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
承接采集端（DMDCBWD11CollectFile）产出的分片清单：它在 Branch/{branch}/ 下不断写入
形如 UploadFileList_yyyyMMdd_HHmmssSSS_{MD5}.jsonl 的分片；本工具把同目录下的所有分片
**增量聚合**到一个 UploadFileList.jsonl（即“待上传队列”），并删除已并入的分片。

    1. 扫描 Branch/ 下的一级子目录（每个子目录即一个 {branch}，如 quote、quote-gold），
       逐个独立处理；
    2. 每个目录内：取现有 UploadFileList.jsonl 的行在前，分片按**文件名升序**依次追加
       （文件名内嵌 yyyyMMdd_HHmmssSSS，故等价于时间顺序）；
    3. 按行去重：同一行只保留首次出现；忽略空行与纯空白行；行尾统一 LF、以换行结尾；
    4. 写入同目录的 UploadFileList.jsonl，并删除本次并入的全部分片；
    5. 目录内无分片时整步跳过（幂等，不产生任何改动）。

二、清单行格式
----------------------------------------------------------------------------------------
现行格式为 JSONL：一行一个 JSON 对象，键序固定 dt → sha1 → md5 → rel_path
（格式细节见 ManifestJsonl.py）：

    {"dt":"20260912124805000","sha1":"8A38…","md5":"07BE…","rel_path":"Data/Demo.json"}

本工具**不解析行内容**，只按整行去重——行内容稳定（dt 取 git 提交时间，且序列化是
确定性的：同内容必得同字节）时，整行去重等价于按路径去重；采集端重复产出的同一文件
同一版本会被自然折叠。

将来清单新增字段时，本环节同样无需改动：整行原样搬运，不认得的键自然跟着走。

三、运行方式
----------------------------------------------------------------------------------------
    python3 .github/Python/AggregateUploadFileList.py

【环境要求】
    - Python 3.8+，仅标准库；无网络请求、无 git 命令；
    - 需在仓库工作树根目录执行（或由工作流在检出后执行）。

退出码：
    0 = 正常结束（含“无任何目录需要聚合”这一幂等情形）；
    1 = 致命失败（Branch/ 存在但读写失败等）。
"""

import glob
import os
import sys

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

BASE_DIR = 'Branch'                     # {branch} 的父目录
TARGET_NAME = 'UploadFileList.jsonl'    # 聚合目标（注意：不带下划线）
PART_PREFIX = 'UploadFileList_'         # 分片前缀，其后必为下划线 → 天然不匹配目标文件名


def _ensure_console_utf8():
    """将 stdout/stderr 重配置为 UTF-8（errors=replace）

    防止 Windows 遗留控制台 / 非 UTF-8 locale 下输出中文时抛 UnicodeEncodeError
    导致看似“没有任何输出”的静默崩溃。重配置失败时静默跳过。

    :return: 无
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def read_lines(path):
    """按行读取文本文件，返回去掉行尾换行与首尾空白的行列表

    :param path: 文件路径
    :return: list[str]（保留空行，交由调用方区分空行 / 重复行）
    """
    with open(path, "r", encoding="utf-8") as fh:
        return [raw.rstrip("\r\n").strip() for raw in fh]


def merge_dir(dir_path):
    """聚合单个 {branch} 目录

    :param dir_path: 形如 Branch/quote 的目录路径
    :return: (是否改动, 并入分片数, 结果行数, 被删除文件路径列表)
    """
    target = os.path.join(dir_path, TARGET_NAME)
    parts = sorted(glob.glob(os.path.join(dir_path, PART_PREFIX + "*.jsonl")))
    if not parts:
        print("[SKIP] %s：目录下无 %s*.jsonl 分片，跳过（不产生提交）"
              % (dir_path, PART_PREFIX))
        return False, 0, 0, []

    print("[INFO] %s：发现 %d 个待并入分片，开始聚合" % (dir_path, len(parts)))
    if os.path.isfile(target):
        base_lines = [line for line in read_lines(target) if line]
        print("       现有目标 %s：存在，原有 %d 行" % (TARGET_NAME, len(base_lines)))
    else:
        base_lines = []
        print("       现有目标 %s：不存在，按空基线处理" % TARGET_NAME)

    seen = set(base_lines)
    lines = list(base_lines)
    print("       按下述顺序并入（文件名升序 = 时间顺序）：")
    for idx, src in enumerate(parts, 1):
        raw_lines = read_lines(src)
        added = dup = blank = 0
        for line in raw_lines:
            if not line:
                blank += 1
            elif line in seen:
                dup += 1
            else:
                seen.add(line)
                lines.append(line)
                added += 1
        print("         %d) %s" % (idx, os.path.basename(src)))
        print("            读入 %d 行 → 新增 %d 行 / 重复 %d 行 / 空行 %d 行"
              % (len(raw_lines), added, dup, blank))

    content = "".join(line + "\n" for line in lines)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print("       写入目标 %s：%d 行 / %d 字节（原有 %d 行，净增 %d 行）"
          % (target, len(lines), len(content.encode("utf-8")),
             len(base_lines), len(lines) - len(base_lines)))

    removed = []
    for src in parts:
        os.remove(src)
        removed.append(src)
    print("       删除已并入分片 %d 个：" % len(removed))
    for src in removed:
        print("         - %s" % src)
    return True, len(parts), len(lines), removed


def main():
    """主流程：逐 {branch} 目录聚合分片 → 重写 UploadFileList.jsonl → 删除分片

    :return: 退出码：0 = 正常结束；1 = 致命失败
    """
    _ensure_console_utf8()

    if not os.path.isdir(BASE_DIR):
        print("[INFO] 仓库内无 %s 目录，无需处理" % BASE_DIR)
        return 0

    dirs = sorted(d for d in glob.glob(os.path.join(BASE_DIR, '*'))
                  if os.path.isdir(d))
    print("[INFO] 扫描 %s/ 下目录 %d 个：%s"
          % (BASE_DIR, len(dirs),
             "、".join(os.path.basename(d) for d in dirs) or "（无）"))
    print("")

    merged_dirs = skipped_dirs = 0
    total_parts = total_lines = 0
    all_removed = []
    for dir_path in dirs:
        changed, part_count, line_count, removed = merge_dir(dir_path)
        if changed:
            merged_dirs += 1
            total_parts += part_count
            total_lines += line_count
            all_removed.extend(removed)
        else:
            skipped_dirs += 1
        print("")

    print("[INFO] 汇总：扫描目录 %d 个 | 已聚合 %d 个 | 已跳过 %d 个"
          % (len(dirs), merged_dirs, skipped_dirs))
    print("[INFO] 汇总：并入分片 %d 个 | 删除分片 %d 个 | 写入目标文件 %d 个"
          % (total_parts, len(all_removed), merged_dirs))
    if merged_dirs:
        print("[INFO] 汇总：聚合结果共 %d 行（各目录去重后的合计）" % total_lines)
        print("[INFO] 以下文件已被删除（内容已并入同目录的 %s）：" % TARGET_NAME)
        for src in all_removed:
            print("[INFO]   - %s" % src)
    else:
        print("[INFO] 本次无任何目录需要聚合（幂等跳过，不产生提交）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
