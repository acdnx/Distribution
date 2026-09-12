#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ArchiveUploadFileList —— 排空「待上传队列」：把已确认上传成功的行移出并归档
========================================================================================

一、工具定位与能力
----------------------------------------------------------------------------------------
采集端每天全量扫描后产出的清单会聚合进 Branch/{branch}/UploadFileList.jsonl（待上传队列）；
而上传成功后，源文件会被 OBS 回调（Cloudflare Worker）从原分支删除。若不排空，队列里会
长期堆积“本地文件已不存在”的死条目，上传端每轮都要逐个遍历它们，越积越慢。

本工具定期把【已确认上传成功】的行移出队列，并归档留痕：

    1. 读 **Delete 分支** 上的回调元数据（事实来源）：
       Archive/Branch/{branch}/{cid}/UploadSuccessCallback_{yyyyMMdd}_{HHmmssSSS}_{MD5}.json
       其中 rel_path 为文件相对路径、etag 为对象内容的 MD5；
    2. 逐个 Branch/{branch}/UploadFileList.jsonl：把确认已上传的行拆出；
    3. 拆出的行**原样**归档到 Archive/{branch}/ArchiveUploadFileList_yyyyMMddHHmmss.jsonl
       （同一目录段的归档，时间戳为东八区；原样保留，故行内未知字段一并留痕）；
    4. 队列重写为剩余行；无确认项时整步跳过（幂等，不产生提交）。

【为什么本脚本放在 Migration 分支】
    归档工作流（ArchiveUploadFileList.yml）为满足 schedule 必须留在默认分支，但它运行时
    检出的是 Migration，并在该检出里执行本脚本 —— 故脚本必须随 Migration 一起存在，
    与它操作的 Branch/ 队列同处一个分支（否则工作流会找不到脚本而失败）。

二、匹配规则（三条全满足才算“已确认上传”）
----------------------------------------------------------------------------------------
    1. 回调记录的 rel_path 与队列行的 rel_path 字段相同；
    2. 回调记录的 etag 与队列行的 md5 字段相同（忽略大小写）；
       队列行 md5 为空（三要素皆空的条目）时，退化为只按路径匹配；
    3. **回调时间 >= 该行的 dt 字段**。

    第 3 条的必要性：OBS 对象 key 含日期（{前缀}/{yyyyMMdd}/{cid}/{文件名}），“已上传”
    是按天的；而 Delete 分支的回调记录长期累积。若不做时间约束，某文件上传后被删、
    日后又被重新加入（内容相同 → MD5 相同）时，会被几天前的旧记录误判为已上传，
    从而永远不会上传到当天的新 key（数据丢失）。
    回调文件名自带东八区时间戳（yyyyMMdd + HHmmssSSS，共 17 位），与队列行的 dt
    同格式，可直接按字符串比较。

三、运行方式
----------------------------------------------------------------------------------------
    # 需先取得 Delete 分支的远端跟踪引用（工作流中由 git fetch 完成）
    git fetch --depth=1 origin +refs/heads/Delete:refs/remotes/origin/Delete
    python3 .github/Python/ArchiveUploadFileList.py

【环境要求】
    - Python 3.8+，仅标准库；
    - **需要 git 命令**：用于列取并读取 origin/Delete 上的回调记录（见上）；
    - 需在仓库工作树根目录（Migration 分支的检出）执行；
    - 同目录需存在：ManifestJsonl.py（清单 JSONL 格式的权威实现）。

退出码：
    0 = 正常结束（含“无可归档行”这一幂等情形）；
    1 = 致命失败（git 调用失败、回调记录读取失败等）。
    单行无法解析为 JSONL 条目**不属致命失败**：逐行告警后保留在队列中 ——
    宁可留着下次再判，也不把看不懂的行误当“已上传”删掉。
"""

import datetime
import json
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# 同目录模块 import：显式把本脚本所在目录加入 sys.path（兼容任意 cwd 执行）
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import ManifestJsonl  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（默认值）
# ---------------------------------------------------------------------------

QUEUE_ROOT = 'Branch'                  # {branch} 的父目录（队列所在）
QUEUE_NAME = 'UploadFileList.jsonl'    # 待上传队列文件名（内容为 JSONL）
ARCHIVE_ROOT = 'Archive'               # 归档根目录（与 {branch} 同名子目录）
ARCHIVE_PREFIX = 'ArchiveUploadFileList'   # 归档文件名前缀

CB_BRANCH = 'origin/Delete'           # 回调记录所在分支（远端跟踪引用）
CB_DIR_PREFIX = 'Archive/Branch/'     # 回调记录在 Delete 分支上的路径前缀
# 回调文件名：UploadSuccessCallback_{yyyyMMdd}_{HHmmssSSS}_{MD5}.json
CB_NAME_RE = re.compile(
    r'^UploadSuccessCallback_(\d{8})_(\d{9})_([0-9A-Fa-f]{32})\.json$')

# 东八区（GMT+8）：归档文件名与时间比较统一按此时区
TZ_GMT8 = datetime.timezone(datetime.timedelta(hours=8))

# 告警里回显原始行时的截断长度，避免坏行过长刷屏
EXCERPT_LIMIT = 120


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


def run_git(*args):
    """执行 git 命令并返回 stdout

    :param args: git 子命令及参数
    :return: stdout 文本
    :raises RuntimeError: git 不可用或返回码非 0
    """
    try:
        proc = subprocess.run(['git'] + list(args), capture_output=True,
                              text=True, encoding='utf-8', errors='replace')
    except OSError as e:
        raise RuntimeError("无法执行 git（%s）；本流程需要 git 读取 %s 上的回调记录"
                           % (e, CB_BRANCH))
    if proc.returncode != 0:
        raise RuntimeError("git %s 失败：%s"
                           % (' '.join(args), (proc.stderr or '').strip()))
    return proc.stdout


def load_callbacks():
    """读取 Delete 分支上的回调元数据

    :return: dict —— {rel_path: [(回调时间戳, etag 小写), ...]}
    :raises RuntimeError: git 调用失败
    """
    paths = [p for p in run_git('ls-tree', '-r', '--name-only', CB_BRANCH,
                                '--', CB_DIR_PREFIX).splitlines() if p]
    index = {}
    skipped = 0
    for path in paths:
        matched = CB_NAME_RE.match(os.path.basename(path))
        if not matched:
            skipped += 1
            continue
        try:
            body = json.loads(run_git('show', '%s:%s' % (CB_BRANCH, path)))
        except (ValueError, RuntimeError):
            skipped += 1
            continue
        rel_path = str(body.get('rel_path') or '').strip()
        etag = str(body.get('etag') or '').strip().lower()
        if not rel_path:
            skipped += 1
            continue
        # 回调时间戳 = 文件名里的 yyyyMMdd + HHmmssSSS（东八区，共 17 位）
        cb_ts = matched.group(1) + matched.group(2)
        index.setdefault(rel_path, []).append((cb_ts, etag))
    print('[INFO] %s 上的回调记录：共 %d 个文件，解析出 %d 条有效记录，'
          '跳过 %d 个（文件名或内容不符合回调格式）'
          % (CB_BRANCH, len(paths), sum(len(v) for v in index.values()), skipped))
    return index


def parse_row(raw):
    """把队列中的一行解析为清单条目（格式约定见同目录 ManifestJsonl.py）

    队列行是“一行一个 JSON 对象”，本工具只取出匹配所需的三项：
    rel_path / md5 / dt；行内其余（含将来新增的）字段原样留在行里，
    归档时按原始行整体写出，故不因未识别而丢失。

    :param raw: 原始行文本
    :return: (entry, reason)：成功时 entry 为 dict、reason 为 None；
             失败时 entry 为 None、reason 为失败原因
    """
    entries, problems = ManifestJsonl.parse(raw)
    if entries:
        return entries[0], None
    if problems:
        return None, problems[0]['reason']
    return None, '空行'


def is_confirmed(rel_path, row_md5, row_ts, index):
    """判断某一行是否已被回调记录证实“上传成功”

    规则见模块文档“二、匹配规则”；任一条不满足即不算确认。

    :param rel_path: 行的相对路径
    :param row_md5: 行的 MD5（小写）；md5 为空的条目传 None
    :param row_ts: 行的 dt（最后修改时间戳）；dt 为空的条目传 None
    :param index: load_callbacks() 的结果
    :return: bool
    """
    for cb_ts, cb_md5 in index.get(rel_path, []):
        if row_md5 and cb_md5 and cb_md5 != row_md5:
            continue                     # MD5 不符：不是同一版本
        if row_ts and cb_ts < row_ts:
            continue                     # 旧回调不能证明“这一次”已上传
        return True
    return False


def archive_dir(branch, index, ts):
    """处理单个 {branch} 目录：拆出已确认行 → 归档 → 重写队列

    :param branch: {branch} 目录名（如 quote）
    :param index: load_callbacks() 的结果
    :param ts: 本次归档时间戳（东八区 yyyyMMddHHmmss），用于归档文件名
    :return: (归档行数, 保留行数)；无需改动时返回 (0, 0)
    """
    queue_path = os.path.join(QUEUE_ROOT, branch, QUEUE_NAME)
    if not os.path.isfile(queue_path):
        print('[SKIP] %s：无 %s' % (os.path.join(QUEUE_ROOT, branch), QUEUE_NAME))
        return 0, 0

    # 保留真实行号，便于告警直接指到文件里的第几行
    with open(queue_path, 'r', encoding='utf-8') as fh:
        rows = [(lineno, raw.rstrip('\r\n'))
                for lineno, raw in enumerate(fh, 1) if raw.strip()]
    if not rows:
        print('[SKIP] %s：队列为空' % queue_path)
        return 0, 0

    archived, kept, unparsable = [], [], 0
    for lineno, raw in rows:
        entry, reason = parse_row(raw)
        if entry is None:
            unparsable += 1
            print('[WARN] %s：第 %d 行不是合法 JSONL 条目（%s），'
                  '保留在队列中不归档：%s'
                  % (queue_path, lineno, reason, raw[:EXCERPT_LIMIT]))
            kept.append(raw)
            continue
        rel_path = entry.get('rel_path') or ''
        row_md5 = str(entry.get('md5') or '').strip().lower() or None
        row_ts = str(entry.get('dt') or '').strip() or None
        if rel_path and is_confirmed(rel_path, row_md5, row_ts, index):
            archived.append(raw)
        else:
            kept.append(raw)
    if unparsable:
        print('[WARN] %s：共 %d 行无法解析（已保留在队列中，下次再判）；'
              '若整份队列都解析不出，多半是它仍为旧的分隔符格式'
              % (queue_path, unparsable))

    print('[INFO] %s：队列 %d 行 → 确认已上传 %d 行（归档）/ 保留 %d 行'
          % (queue_path, len(rows), len(archived), len(kept)))
    if not archived:
        return 0, 0

    archive_dir_path = os.path.join(ARCHIVE_ROOT, branch)
    os.makedirs(archive_dir_path, exist_ok=True)
    archive_path = os.path.join(
        archive_dir_path, '%s_%s.jsonl' % (ARCHIVE_PREFIX, ts))
    with open(archive_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(archived) + '\n')
    print('       归档 → %s（%d 行）' % (archive_path, len(archived)))
    for raw in archived:
        print('         - %s' % raw)

    with open(queue_path, 'w', encoding='utf-8', newline='\n') as fh:
        if kept:
            fh.write('\n'.join(kept) + '\n')
    print('       队列已重写 → %s（保留 %d 行）' % (queue_path, len(kept)))
    return len(archived), len(kept)


def main():
    """主流程：读回调记录 → 逐 {branch} 拆行归档 → 重写队列

    :return: 退出码：0 = 正常结束；1 = 致命失败
    """
    _ensure_console_utf8()

    try:
        index = load_callbacks()
    except RuntimeError as e:
        print('[FAIL] %s' % e)
        return 1

    if not os.path.isdir(QUEUE_ROOT):
        print('[INFO] 仓库内无 %s 目录，无需处理' % QUEUE_ROOT)
        return 0

    branches = sorted(d for d in os.listdir(QUEUE_ROOT)
                      if os.path.isdir(os.path.join(QUEUE_ROOT, d)))
    print('[INFO] 扫描 %s/ 下目录 %d 个：%s'
          % (QUEUE_ROOT, len(branches), '、'.join(branches) or '（无）'))

    ts = datetime.datetime.now(TZ_GMT8).strftime('%Y%m%d%H%M%S')
    changed_dirs = total_archived = total_kept = 0
    for branch in branches:
        archived, kept = archive_dir(branch, index, ts)
        if archived:
            changed_dirs += 1
            total_archived += archived
            total_kept += kept

    print('[INFO] 汇总：有改动的目录 %d 个 | 归档行 %d | 保留行 %d'
          % (changed_dirs, total_archived, total_kept))
    if not changed_dirs:
        print('[INFO] 本次无可归档的行（幂等跳过，不产生提交）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
