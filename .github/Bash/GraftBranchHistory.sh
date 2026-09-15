#!/usr/bin/env bash
#
# GraftBranchHistory.sh —— 「移花接木」：把分支的历史压缩成单个无父提交
#
# ── 原理 ────────────────────────────────────────────────────────────────────
# Git 的提交对象就是一个四元组 (tree, parents, message, author)。
# 本脚本读取源分支当前的 tree SHA，用它新建一个「无父提交」，于是：
#   · 文件内容、文件权限位、符号链接、子模块(gitlink)条目 —— 一字未动、逐字节相同；
#   · parents 链被整个丢弃，分支只剩 1 个提交；
#   · 全程只走 GitHub Git Data API，不克隆仓库、不传输任何文件内容。
#
# tree SHA 是整棵文件树的 Merkle 根哈希。新提交的 tree SHA 与源分支相等，
# 即是「内容完全一致」的密码学证明 —— 无需逐文件比对。
#
# ── 用法 ────────────────────────────────────────────────────────────────────
# 参数说明、使用流程、回滚方式：见同目录 GraftBranchHistory.md
#
set -euo pipefail

# ------------------------------------------------------------------ 默认值
REPO="${GITHUB_REPOSITORY:-}"
MODE="verify"
SOURCE=""
TEMP=""
MESSAGE=""
AUTHOR_NAME=""
AUTHOR_EMAIL=""
EXPECTED_SHA=""
ON_DRIFT="abort"
KEEP_BACKUP="true"
RESTORE_SHA=""
DRY_RUN="false"
ALLOW_PROTECTED="false"
REPLACE_TEMP="false"

BACKUP_PREFIX="graft-backup"
SUMMARY_FILE="${GITHUB_STEP_SUMMARY:-/dev/null}"

# ------------------------------------------------------------------ 输出
# 日志一律走 stderr —— stdout 留给「被 $(...) 捕获的数据」（SHA 等），
# 否则日志会污染 create_root_commit / ref_sha 的返回值。emit 是唯一的 stdout 出口。
info()   { printf '==> %s\n' "$*" >&2; }
warn()   { printf '警告: %s\n' "$*" >&2; }
die()    { printf '错误: %s\n' "$*" >&2; exit 1; }
emit()   { printf '%s\n' "$*"; printf '%s\n' "$*" >>"$SUMMARY_FILE"; }
is_dry() { [ "$DRY_RUN" = "true" ]; }

usage() {
  cat <<'EOF'
GraftBranchHistory.sh —— 把分支历史压缩成单个无父提交（内容逐字节不变）

用法:
  GraftBranchHistory.sh --source <分支> [选项]

必填:
  --source <分支>              源分支（要压缩历史的分支）

选项:
  --mode <模式>                verify(默认) | create | swap | restore
  --repo <owner/name>          目标仓库，默认取 $GITHUB_REPOSITORY
  --temp <分支>                临时分支名，默认 new-<源分支>
  --message <文本>             新提交说明，默认自动生成（内含原 tip SHA 便于溯源）
  --author-name <名字>         新提交作者名，默认沿用源分支 tip 的作者
  --author-email <邮箱>        新提交作者邮箱，默认沿用源分支 tip 的作者
  --expected-source-sha <SHA>  swap 时的乐观并发校验值（create 阶段输出）
  --on-drift <策略>            abort(默认) | retree  —— 源分支被外部推送时的处理
  --keep-backup <true|false>   切换前是否建备份分支，默认 true
  --restore-sha <SHA>          restore 时回滚到的 SHA，默认找最近的备份分支
  --replace-temp               临时分支已存在时，删除后重建
  --allow-protected            遇到分支保护规则时仅告警不中止（需确认执行者有 bypass 权限）
  --dry-run                    只打印计划，不实际写入
  -h, --help                   显示本帮助

模式说明:
  verify   只读预演：打印现状、计划与风险，不写入任何内容
  create   用源分支的 tree 建「无父提交」，创建临时分支并自校验
  swap     校验临时分支 → 建备份 → 强制移动源分支到该提交 → 删除临时分支
  restore  把源分支强制移回备份 SHA（回滚）
EOF
}

# ------------------------------------------------------------------ 参数解析
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)                REPO="${2:?--repo 缺少取值}"; shift 2 ;;
    --mode)                MODE="${2:?--mode 缺少取值}"; shift 2 ;;
    --source)              SOURCE="${2:?--source 缺少取值}"; shift 2 ;;
    --temp)                TEMP="${2:?--temp 缺少取值}"; shift 2 ;;
    --message)             MESSAGE="${2:?--message 缺少取值}"; shift 2 ;;
    --author-name)         AUTHOR_NAME="${2:?--author-name 缺少取值}"; shift 2 ;;
    --author-email)        AUTHOR_EMAIL="${2:?--author-email 缺少取值}"; shift 2 ;;
    --expected-source-sha) EXPECTED_SHA="${2:?--expected-source-sha 缺少取值}"; shift 2 ;;
    --on-drift)            ON_DRIFT="${2:?--on-drift 缺少取值}"; shift 2 ;;
    --keep-backup)         KEEP_BACKUP="${2:?--keep-backup 缺少取值}"; shift 2 ;;
    --restore-sha)         RESTORE_SHA="${2:?--restore-sha 缺少取值}"; shift 2 ;;
    --allow-protected)     ALLOW_PROTECTED="true"; shift ;;
    --replace-temp)        REPLACE_TEMP="true"; shift ;;
    --dry-run)             DRY_RUN="true"; shift ;;
    -h|--help)             usage; exit 0 ;;
    *)                     die "未知参数: $1（用 --help 查看用法）" ;;
  esac
done

# ------------------------------------------------------------------ 前置检查
command -v gh >/dev/null 2>&1 || die "未找到 gh CLI。"
if [ -z "${GH_TOKEN:-}" ]; then
  # Actions 里 GH_TOKEN 一定存在；本地执行时允许退回 gh 自己的登录态
  gh auth status >/dev/null 2>&1 || die "无可用凭据：请设置 GH_TOKEN，或先执行 gh auth login。"
fi
[ -n "$REPO" ] || die "未指定仓库：用 --repo owner/name，或设置 GITHUB_REPOSITORY。"
[ -n "$SOURCE" ] || die "未指定源分支：--source <分支>。"

case "$MODE" in
  verify|create|swap|restore) ;;
  *) die "未知模式 '$MODE'，可选：verify | create | swap | restore" ;;
esac
case "$ON_DRIFT" in abort|retree) ;; *) die "--on-drift 只能是 abort 或 retree" ;; esac

[ -z "$TEMP" ] && TEMP="new-$SOURCE"
[ "$TEMP" = "$SOURCE" ] && die "临时分支不能与源分支同名。"

# 探测仓库可达性，尽早暴露鉴权/拼写问题
gh api "repos/$REPO" --jq '.full_name' >/dev/null 2>&1 \
  || die "无法访问仓库 '$REPO'，请检查仓库名与 GH_TOKEN 权限。"

# ------------------------------------------------------------------ API 封装
#
# 注意：gh api 在 HTTP 错误时【不会】套用 --jq，而是把原始错误 JSON 打到 stdout。
# 因此这里一律按「退出码」判断成败，失败时丢弃 stdout，绝不靠判空来识别不存在。
api_get() {   # $1=endpoint  $2=jq 过滤表达式
  local out
  out=$(gh api "$1" --jq "$2" 2>/dev/null) || return 1
  printf '%s' "$out"
}

ref_sha()        { api_get "repos/$REPO/git/ref/heads/$1" '.object.sha // empty'; }
ref_exists()     { ref_sha "$1" >/dev/null 2>&1; }
commit_tree()    { api_get "repos/$REPO/git/commits/$1" '.tree.sha // empty'; }
parents_count()  { api_get "repos/$REPO/git/commits/$1" '(.parents // []) | length'; }
author_of()      { api_get "repos/$REPO/git/commits/$1" '.author.name // empty'; }
email_of()       { api_get "repos/$REPO/git/commits/$1" '.author.email // empty'; }
date_of()        { api_get "repos/$REPO/git/commits/$1" '.author.date // empty'; }
default_branch() { api_get "repos/$REPO" '.default_branch // empty'; }

# 分支上生效的规则（rulesets + 旧版保护）。读取失败返回空串并置标志，由调用方告警。
RULES_UNREADABLE=0
effective_rules() {
  local out
  if out=$(gh api "repos/$REPO/rules/branches/$1" --jq '.[].type' 2>/dev/null); then
    printf '%s' "$out"
  else
    RULES_UNREADABLE=1
  fi
}

# ------------------------------------------------------------ 写操作（受 dry-run 控制）
create_ref() {   # $1=refs/heads/xxx  $2=sha
  if is_dry; then info "[DRY-RUN] 建引用 $1 -> $2"; return 0; fi
  gh api -X POST "repos/$REPO/git/refs" -f ref="$1" -f sha="$2" --jq '.ref' >/dev/null
}

move_ref() {     # $1=分支名  $2=sha   （强制移动，非快进）
  if is_dry; then info "[DRY-RUN] 强制移动 $1 -> $2"; return 0; fi
  gh api -X PATCH "repos/$REPO/git/refs/heads/$1" -f sha="$2" -F force=true --jq '.object.sha' >/dev/null
}

delete_ref() {   # $1=分支名
  if is_dry; then info "[DRY-RUN] 删除分支 $1"; return 0; fi
  gh api -X DELETE "repos/$REPO/git/refs/heads/$1"
}

# 创建「无父提交」：只给 tree，parents 传空数组。
# gh 的 -f 会自动完成 JSON 转义，key[subkey] 表示嵌套对象，key[] 表示空数组。
create_root_commit() {   # $1=tree  $2=message  $3=author_name  $4=author_email
  if is_dry; then
    info "[DRY-RUN] 将基于 tree $1 创建无父提交（不写入）"
    printf '%s' "0000000000000000000000000000000000000000"
    return 0
  fi
  local out
  if out=$(gh api -X POST "repos/$REPO/git/commits" \
             -f message="$2" -f tree="$1" \
             -f "author[name]=$3"    -f "author[email]=$4" \
             -f "committer[name]=$3" -f "committer[email]=$4" \
             -f "parents[]" --jq '.sha' 2>&1); then
    printf '%s' "$out"; return 0
  fi
  # 回退：省略 parents（GitHub 对「省略即无父」的处理在版本间有过差异）
  warn "以 parents:[] 建提交失败（$out），回退为省略 parents 重试。"
  gh api -X POST "repos/$REPO/git/commits" \
    -f message="$2" -f tree="$1" \
    -f "author[name]=$3"    -f "author[email]=$4" \
    -f "committer[name]=$3" -f "committer[email]=$4" \
    --jq '.sha'
}

# ------------------------------------------------------------------ 安全检查
assert_safe_target() {   # $1=分支名  $2=动作描述
  local b="$1" act="$2" def rules
  def="$(default_branch || true)"
  [ -n "$def" ] || die "无法读取仓库默认分支，为安全起见中止。"
  if [ "$b" = "$def" ]; then
    die "分支 '$b' 是仓库默认分支。默认分支不可被改写或删除，拒绝$act。"
  fi
  rules="$(effective_rules "$b")"
  if [ "$RULES_UNREADABLE" = "1" ]; then
    warn "无法读取分支 '$b' 的规则，未能预先确认是否受保护。若确实受保护，写入会被 GitHub 拒绝。"
  fi
  if [ -n "$rules" ]; then
    local hit=""
    printf '%s\n' "$rules" | grep -qx 'deletion'         && hit="deletion"
    printf '%s\n' "$rules" | grep -qx 'non_fast_forward' && hit="${hit:+$hit,}non_fast_forward"
    if [ -n "$hit" ]; then
      if [ "$ALLOW_PROTECTED" = "true" ]; then
        warn "分支 '$b' 受规则保护（$hit）。已按 --allow-protected 继续，若执行者不在 bypass 名单，后续写入会被拒绝。"
      else
        die "分支 '$b' 受规则保护（$hit），无法$act。需先调整 ruleset/保护规则，或由具备 bypass 权限的执行者加 --allow-protected。"
      fi
    fi
  fi
}

build_message() {   # $1=分支  $2=tip  $3=tree
  cat <<EOF
chore: squash history of $1 into a single commit

原分支 tip : $2
原 tree    : $3
压缩时间   : $(date -u +%Y-%m-%dT%H:%M:%SZ)
压缩方式   : Git Data API 换树（内容逐字节不变，parents 置空）
运行记录   : ${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-$REPO}/actions/runs/${GITHUB_RUN_ID:-local}
EOF
}

find_latest_backup() {   # $1=源分支
  local out
  out=$(gh api "repos/$REPO/git/matching-refs/heads/$BACKUP_PREFIX/$1-" --jq '.[].ref' 2>/dev/null) || return 0
  printf '%s' "$out" | sed 's#^refs/heads/##' | sort | tail -n1
}

# ================================================================== 主流程
info "仓库=$REPO  模式=$MODE  源分支=$SOURCE  临时分支=$TEMP  dry-run=$DRY_RUN"

case "$MODE" in

# ---------------------------------------------------------------- verify
verify)
  SRC_SHA="$(ref_sha "$SOURCE" || true)"
  [ -n "$SRC_SHA" ] || die "源分支 '$SOURCE' 不存在或无法读取。"
  TREE_SHA="$(commit_tree "$SRC_SHA" || true)"
  [ -n "$TREE_SHA" ] || die "无法读取 $SRC_SHA 的 tree。"
  NP="$(parents_count "$SRC_SHA" || true)"
  DEF="$(default_branch || true)"
  RULES="$(effective_rules "$SOURCE")"
  # 规则是多行输出，压成一行，否则会把 markdown 表格撑破
  RULES_DISPLAY="$(printf '%s' "$RULES" | tr '\n' ',' | sed 's/,$//')"

  emit "## 预演（未写入任何内容）"
  emit ""
  emit "| 项目 | 值 |"
  emit "| --- | --- |"
  emit "| 仓库 | \`$REPO\` |"
  emit "| 默认分支 | \`$DEF\` |"
  emit "| 源分支 | \`$SOURCE\` |"
  emit "| 源分支 tip | \`$SRC_SHA\` |"
  emit "| 源分支 tree | \`$TREE_SHA\` |"
  emit "| tip 的父提交数 | $NP |"
  emit "| tip 作者 | $(author_of "$SRC_SHA") <$(email_of "$SRC_SHA")> @ $(date_of "$SRC_SHA") |"
  emit "| 临时分支 | \`$TEMP\`（$(ref_exists "$TEMP" && echo 已存在 || echo 不存在)）|"
  emit "| 生效规则 | ${RULES_DISPLAY:-（无）} |"
  emit ""

  if [ "$SOURCE" = "$DEF" ]; then
    emit "⛔ **源分支是默认分支，方案不可行。** 默认分支不可删除/改写，需先把默认分支切到别处。"
  elif [ -n "$RULES" ]; then
    emit "⚠️ **源分支受规则保护**（$RULES_DISPLAY），删除/强推会被拒绝，除非执行者具备 bypass 权限。"
  else
    emit "✅ 源分支未受保护，可以执行 create → swap。"
  fi
  if [ "$NP" = "0" ]; then
    emit "ℹ️ 源分支 tip 本身已是无父提交（只有 1 个提交），无需压缩。"
  fi
  emit ""
  emit "**后续计划**：\`create\` 建临时分支 → 人工核对 → \`swap\` 切换（源分支强制移动到新提交，旧提交存入备份分支）。"
  if [ -n "$RULES" ]; then warn "源分支受规则保护：$RULES_DISPLAY"; fi
  ;;

# ---------------------------------------------------------------- create
create)
  assert_safe_target "$SOURCE" "改写历史"
  SRC_SHA="$(ref_sha "$SOURCE" || true)"
  [ -n "$SRC_SHA" ] || die "源分支 '$SOURCE' 不存在或无法读取。"
  TREE_SHA="$(commit_tree "$SRC_SHA" || true)"
  [ -n "$TREE_SHA" ] || die "无法读取 $SRC_SHA 的 tree。"

  if ref_exists "$TEMP"; then
    if [ "$REPLACE_TEMP" = "true" ]; then
      warn "临时分支 '$TEMP' 已存在，按 --replace-temp 删除重建。"
      delete_ref "$TEMP"
    else
      die "临时分支 '$TEMP' 已存在。请先删除它，或加 --replace-temp。"
    fi
  fi

  [ -n "$MESSAGE" ]        || MESSAGE="$(build_message "$SOURCE" "$SRC_SHA" "$TREE_SHA")"
  [ -n "$AUTHOR_NAME" ]    || AUTHOR_NAME="$(author_of "$SRC_SHA" || true)"
  [ -n "$AUTHOR_EMAIL" ]   || AUTHOR_EMAIL="$(email_of "$SRC_SHA" || true)"
  [ -n "$AUTHOR_NAME" ]    || AUTHOR_NAME="${GITHUB_ACTOR:-unknown}"
  [ -n "$AUTHOR_EMAIL" ]   || AUTHOR_EMAIL="${GITHUB_ACTOR:-unknown}@users.noreply.github.com"

  info "基于 tree $TREE_SHA 创建无父提交……"
  NEW_SHA="$(create_root_commit "$TREE_SHA" "$MESSAGE" "$AUTHOR_NAME" "$AUTHOR_EMAIL" || true)"
  [ -n "$NEW_SHA" ] || die "创建无父提交失败。"
  info "新提交: $NEW_SHA"

  create_ref "refs/heads/$TEMP" "$NEW_SHA"

  if is_dry; then
    emit "## DRY-RUN：create 计划"
    emit ""
    emit "将基于 tree \`$TREE_SHA\` 创建无父提交，并建立分支 \`$TEMP\`。未实际写入。"
    exit 0
  fi

  # ---- 自校验：这是整个流程的安全网，任何一条不满足就中止 ----
  CHK_SHA="$(ref_sha "$TEMP" || true)"
  [ "$CHK_SHA" = "$NEW_SHA" ] || die "临时分支指向 $CHK_SHA，期望 $NEW_SHA。"
  CHK_NP="$(parents_count "$NEW_SHA" || true)"
  [ "$CHK_NP" = "0" ] || die "新提交仍有 $CHK_NP 个父提交（期望 0），中止。"
  CHK_TREE="$(commit_tree "$NEW_SHA" || true)"
  [ "$CHK_TREE" = "$TREE_SHA" ] || die "tree 不一致：期望 $TREE_SHA，实际 $CHK_TREE，中止。"

  emit "## create 完成（源分支未改动）"
  emit ""
  emit "| 项目 | 值 |"
  emit "| --- | --- |"
  emit "| 源分支 | \`$SOURCE\` @ \`$SRC_SHA\` |"
  emit "| 新提交（无父） | \`$NEW_SHA\` |"
  emit "| tree | \`$TREE_SHA\`（与源分支**相同**）|"
  emit "| 父提交数 | $CHK_NP |"
  emit "| 临时分支 | \`$TEMP\` |"
  emit ""
  emit "✅ 自校验通过：新提交的 tree SHA 与源分支一致，内容逐字节相同。"
  emit ""
  emit "**下一步**：人工核对 \`$TEMP\` 的文件列表无误后，用以下参数执行 \`swap\`："
  emit ""
  emit '```'
  emit "--mode swap --source $SOURCE --temp $TEMP --expected-source-sha $SRC_SHA"
  emit '```'
  ;;

# ---------------------------------------------------------------- swap
swap)
  assert_safe_target "$SOURCE" "改写历史"
  SRC_SHA="$(ref_sha "$SOURCE" || true)"
  [ -n "$SRC_SHA" ] || die "源分支 '$SOURCE' 不存在或无法读取。"
  TEMP_SHA="$(ref_sha "$TEMP" || true)"
  [ -n "$TEMP_SHA" ] || die "临时分支 '$TEMP' 不存在，请先执行 create。"
  TEMP_TREE="$(commit_tree "$TEMP_SHA" || true)"
  [ -n "$TEMP_TREE" ] || die "无法读取临时分支 $TEMP_SHA 的 tree。"

  # ---- 乐观并发：源分支在 create 之后若被推送过，这里会发现 ----
  if [ -n "$EXPECTED_SHA" ] && [ "$SRC_SHA" != "$EXPECTED_SHA" ]; then
    if [ "$ON_DRIFT" = "abort" ]; then
      die "源分支已漂移：期望 $EXPECTED_SHA，实际 $SRC_SHA。请重新 create 核对，或用 --on-drift retree 以最新内容重建。"
    fi
    warn "源分支已漂移：$EXPECTED_SHA -> $SRC_SHA，按 --on-drift retree 以最新内容重建。"
  fi

  CUR_TREE="$(commit_tree "$SRC_SHA" || true)"
  [ -n "$CUR_TREE" ] || die "无法读取源分支 $SRC_SHA 的 tree。"
  if [ "$TEMP_TREE" != "$CUR_TREE" ]; then
    # 临时分支落后于源分支：说明 create 之后源分支的 tree 变了
    if [ "$ON_DRIFT" = "abort" ]; then
      die "临时分支的 tree ($TEMP_TREE) 与源分支当前 tree ($CUR_TREE) 不一致，说明源分支已被推送过。请重新 create，或用 --on-drift retree。"
    fi
    warn "临时分支 tree 已过期，按 retree 以源分支当前 tree 重建提交。"
    [ -n "$MESSAGE" ]      || MESSAGE="$(build_message "$SOURCE" "$SRC_SHA" "$CUR_TREE")"
    [ -n "$AUTHOR_NAME" ]  || AUTHOR_NAME="$(author_of "$SRC_SHA" || true)"
    [ -n "$AUTHOR_EMAIL" ] || AUTHOR_EMAIL="$(email_of "$SRC_SHA" || true)"
    TEMP_SHA="$(create_root_commit "$CUR_TREE" "$MESSAGE" "$AUTHOR_NAME" "$AUTHOR_EMAIL" || true)"
    [ -n "$TEMP_SHA" ] || die "重建提交失败，中止。"
    move_ref "$TEMP" "$TEMP_SHA"
    TEMP_TREE="$(commit_tree "$TEMP_SHA" || true)"
    [ "$TEMP_TREE" = "$CUR_TREE" ] || die "重建后 tree 仍不一致，中止。"
  else
    info "临时分支 tree 与源分支当前 tree 一致，直接复用已核对过的提交。"
  fi

  # 最终自校验
  [ "$(parents_count "$TEMP_SHA" || true)" = "0" ] || die "待切换的提交有父提交，中止。"
  [ "$TEMP_TREE" = "$CUR_TREE" ] || die "tree 校验失败，中止。"

  # ---- 备份旧 tip，保证可回滚 ----
  BACKUP_REF=""
  if [ "$KEEP_BACKUP" = "true" ]; then
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    BACKUP_REF="$BACKUP_PREFIX/$SOURCE-$STAMP"
    create_ref "refs/heads/$BACKUP_REF" "$SRC_SHA"
  else
    warn "未启用备份分支：源分支旧 tip $SRC_SHA 将不再被任何引用指向（事后仍可用该 SHA 恢复一段时间）。"
  fi

  # ---- 原子切换：强制移动源分支，不存在「分支消失」的空窗 ----
  move_ref "$SOURCE" "$TEMP_SHA"
  delete_ref "$TEMP"

  if is_dry; then
    emit "## DRY-RUN：swap 计划"
    emit ""
    emit "将把 \`$SOURCE\` 从 \`$SRC_SHA\` 强制移动到 \`$TEMP_SHA\`${BACKUP_REF:+，并建立备份分支 \`$BACKUP_REF\`}，然后删除 \`$TEMP\`。未实际写入。"
    exit 0
  fi

  FINAL_SHA="$(ref_sha "$SOURCE" || true)"
  [ "$FINAL_SHA" = "$TEMP_SHA" ] || die "切换后源分支指向 $FINAL_SHA，期望 $TEMP_SHA。"
  [ "$(commit_tree "$FINAL_SHA" || true)" = "$CUR_TREE" ] || die "切换后 tree 校验失败。"
  [ "$(parents_count "$FINAL_SHA" || true)" = "0" ] || die "切换后父提交数不为 0。"

  emit "## swap 完成"
  emit ""
  emit "| 项目 | 值 |"
  emit "| --- | --- |"
  emit "| 分支 | \`$SOURCE\` |"
  emit "| 切换前 tip | \`$SRC_SHA\` |"
  emit "| 切换后 tip | \`$FINAL_SHA\`（无父提交，1 个提交）|"
  emit "| tree | \`$CUR_TREE\`（与切换前**相同**）|"
  [ -n "$BACKUP_REF" ] && emit "| 备份分支 | \`$BACKUP_REF\` -> \`$SRC_SHA\` |"
  emit ""
  emit "✅ 内容校验通过：tree SHA 与切换前一致，工作区逐字节相同；历史已压缩为单个提交。"
  if [ -n "$BACKUP_REF" ]; then
    emit ""
    emit "**回滚**（如需）："
    emit ""
    emit '```'
    emit "--mode restore --source $SOURCE --restore-sha $SRC_SHA"
    emit '```'
  fi
  ;;

# ---------------------------------------------------------------- restore
restore)
  assert_safe_target "$SOURCE" "改写历史"
  if [ -z "$RESTORE_SHA" ]; then
    LATEST_BACKUP="$(find_latest_backup "$SOURCE")"
    [ -n "$LATEST_BACKUP" ] || die "未找到 \`$BACKUP_PREFIX/$SOURCE-*\` 备份分支，请用 --restore-sha 显式指定。"
    RESTORE_SHA="$(ref_sha "$LATEST_BACKUP" || true)"
    info "使用最近的备份分支：$LATEST_BACKUP"
  fi
  [ -n "$RESTORE_SHA" ] || die "无法确定回滚目标 SHA。"

  CUR_SHA="$(ref_sha "$SOURCE" || true)"
  [ -n "$CUR_SHA" ] || die "源分支 '$SOURCE' 不存在或无法读取。"
  info "把 $SOURCE 从 $CUR_SHA 强制移回 $RESTORE_SHA"
  move_ref "$SOURCE" "$RESTORE_SHA"

  if is_dry; then
    emit "## DRY-RUN：restore 计划"
    emit ""
    emit "将把 \`$SOURCE\` 从 \`$CUR_SHA\` 移回 \`$RESTORE_SHA\`。未实际写入。"
    exit 0
  fi

  FINAL_SHA="$(ref_sha "$SOURCE" || true)"
  [ "$FINAL_SHA" = "$RESTORE_SHA" ] || die "回滚后指向 $FINAL_SHA，期望 $RESTORE_SHA。"

  emit "## restore 完成"
  emit ""
  emit "| 项目 | 值 |"
  emit "| --- | --- |"
  emit "| 分支 | \`$SOURCE\` |"
  emit "| 回滚前 | \`$CUR_SHA\` |"
  emit "| 回滚后 | \`$FINAL_SHA\` |"
  emit ""
  emit "✅ 已回到备份状态。确认无误后可手动删除备份分支。"
  ;;

esac
