# 新建独立分支操作指南

> 本文件随 **`branch-template`** 模板分支发布。新建一个「独立分支」时，从模板分支拉出
> 新分支，按本指南替换占位符、改三个文件、再往 `dev` 补两份工作流即可。

---

## 一、这个模板分支是什么

`branch-template` 从 **`main`** 拉出，带上了一整套「独立分支」所需的内容：

| 内容 | 说明 |
| --- | --- |
| `.github/Python/*.py`（7 个） | 流水线脚本，**各分支逐字节一致**，新建分支时原样带过去、不要改 |
| `.github/Python/Commit.json` | 仓库身份登记（owner/repo/各分支名），同样一致 |
| `.github/Python/Migration.{BRANCH}.json` | **每分支不同** —— 本次要改的三个文件之一 |
| `.github/workflows/DMDCBWD11CollectFile{BRANCH_PASCAL}.yml` | **每分支不同** —— 采集工作流 |
| `.github/workflows/DMDCBWD31MigrationFile{BRANCH_PASCAL}.yml` | **每分支不同** —— 上传工作流 |
| `Data/`（7 个小样例） | `.json` / `.mvsv` / `.log` 三种格式的测试数据，用于跑通流程 |
| `.gitignore` | 与 `dev` 一致，**外加一条例外** `!Data/**/*.log` —— 见「六、注意事项」第 7 条 |
| `LICENSE` / `README.md` | 与其它分支一致 |

> ⚠️ **模板分支本身不会被调度、也不该被调度。** 两份工作流里的 cron 是占位符
> `{CRON_COLLECT}` / `{CRON_MIGRATE}`，不是合法 cron，GitHub 会判定这两个文件无效
> （在该分支的 Actions 页面能看到报错）—— **这是故意的**：占位符没替换完，就不该跑起来。

---

## 二、一次创建要做的事（清单）

1. 从 `branch-template` 建新分支，名字即新分支名（下称 `<new>`，如 `market-news`）
2. 重命名 3 个文件（下面给了命令）
3. 替换 4 类占位符（下面给了命令）
4. 到 `dev` 上补两份同名工作流（**逐字节一致**）
5. 按「五、校验清单」逐项核对
6. 手动触发一次采集，确认分片落到 `Branch/<new>/`

### 占位符对照

| 占位符 | 含义 | 例（`<new>` = `market-news`） |
| --- | --- | --- |
| `{BRANCH}` | 分支名**原样**（用于 `ref`、`default`、配置路径、注释） | `market-news` |
| `{BRANCH_PASCAL}` | 分支名的**帕斯卡形式**（用于文件名与 `name:`） | `MarketNews` |
| `{CRON_COLLECT}` | 本分支**采集**的时段（见「四、时段分配」） | `45 18 * * *` |
| `{CRON_MIGRATE}` | 本分支**上传**的时段，须比采集晚 2 小时 | `45 20 * * *` |

### 命令

```bash
NEW=market-news                                  # ← 改成新分支名
COLLECT_CRON='45 18 * * *'                       # ← 从「四、时段分配」里挑一个没被占的
MIGRATE_CRON='45 20 * * *'                       # ← 比上面晚 2 小时
PASCAL=$(printf '%s' "$NEW" | sed -E 's/(^|-)([a-z])/\U\2/g')   # market-news → MarketNews

# 1) 重命名三个文件
git mv ".github/Python/Migration.{BRANCH}.json" ".github/Python/Migration.$NEW.json"
git mv ".github/workflows/DMDCBWD11CollectFile{BRANCH_PASCAL}.yml" \
       ".github/workflows/DMDCBWD11CollectFile$PASCAL.yml"
git mv ".github/workflows/DMDCBWD31MigrationFile{BRANCH_PASCAL}.yml" \
       ".github/workflows/DMDCBWD31MigrationFile$PASCAL.yml"

# 2) 替换占位符
#    ⚠️ 顺序不能反：{BRANCH_PASCAL} 里含 {BRANCH}，先替长的，否则会替成 "<new>_PASCAL}"
FILES=".github/Python/Migration.$NEW.json
.github/workflows/DMDCBWD11CollectFile$PASCAL.yml
.github/workflows/DMDCBWD31MigrationFile$PASCAL.yml"
sed -i "s/{BRANCH_PASCAL}/$PASCAL/g" $FILES
sed -i "s/{CRON_COLLECT}/$COLLECT_CRON/g; s/{CRON_MIGRATE}/$MIGRATE_CRON/g" $FILES
sed -i "s/{BRANCH}/$NEW/g" $FILES

# 3) 确认没有漏网的占位符（应输出 ✓）
#    必须排除本指南自身 —— 它通篇在讲这些占位符，不排除的话这条检查永远不通过
grep -rn '{BRANCH\|{CRON' . --exclude-dir=.git --exclude=BRANCH_TEMPLATE.md \
  && echo "✗ 上面这些文件里还有未替换的占位符" \
  || echo "✓ 占位符已全部替换"
```

> **为什么 `default:` 的值在模板里带引号**：YAML 里 `default: {BRANCH}` 不加引号会被当成
> **flow mapping**（一个键为 `BRANCH` 的映射），而不是字符串。模板里已写成
> `default: '{BRANCH}'`，替换后请保持引号。

---

## 三、`dev` 侧要补什么

`schedule` **只在仓库默认分支上已存在的工作流文件生效**（当前默认分支是 `dev`）。
所以分支上的这两份工作流的 `schedule` 是**死的** —— 真正驱动定时的是 `dev` 上的同名副本。

新建分支时，把替换完成的两份工作流**原样**再提交到 `dev`：

```bash
# 在 dev 的工作副本里
cp <分支>/.github/workflows/DMDCBWD11CollectFile$PASCAL.yml .github/workflows/
cp <分支>/.github/workflows/DMDCBWD31MigrationFile$PASCAL.yml .github/workflows/
git add .github/workflows/DMDCBWD11CollectFile$PASCAL.yml \
        .github/workflows/DMDCBWD31MigrationFile$PASCAL.yml
```

**两边不要求逐字节一致，但下面这几项必须一致**（它们决定工作流「跑不跑、跟谁排队」）：

| 必须一致 | 不一致的后果 |
| --- | --- |
| `on.schedule[].cron` | 从分支 `workflow_dispatch` 手动触发时用的就是它 |
| `concurrency.group` | 写错就不是同一个组 —— 会**与同系列其它分支并发跑**，串行化白做 |
| `workflow_dispatch.inputs.branch.default`、`jobs.*.steps[0].with.ref` | 都必须是本分支名 |

```bash
# 关键配置逐项比对，别只靠肉眼
for f in DMDCBWD11CollectFile$PASCAL DMDCBWD31MigrationFile$PASCAL; do
  diff <(grep -E 'cron:|group:|cancel-in-progress:|default:|ref:' .github/workflows/$f.yml) \
       <(grep -E 'cron:|group:|cancel-in-progress:|default:|ref:' <分支>/.github/workflows/$f.yml) \
    && echo "$f 关键配置一致 ✓"
done
```

> ⚠️ **别把分支副本当权威。** `schedule` 只读默认分支，分支副本**不参与定时**，长期没人
> 碰就会悄悄滞后 —— `quote` / `quote-gold` 的分支副本就曾整整落后一代（11 系列 cron 还是
> `0 20`、完全没有 `concurrency` 块）。**要判断某分支的现行配置，看 `dev`。**

> 本仓 `cnxnc` 对 `acdnx/Distribution` **只有读权限**，任何改动都走 **Fork PR**，
> 从上游拉特性分支、推到 Fork、向上游开 PR（不要动 Fork 上的同名镜像分支）。

---

## 四、时段分配

三条轴：**11 系列（采集）先跑 → 31 系列（上传）晚 2 小时**；同系列各分支**共用
concurrency 组串行执行**（`DMDCBWD11-collect` / `DMDCBWD31-migration`），且各分支
cron **相互错开**，避免同一时刻一起起跑。

| 分支 | 采集（11） | 上传（31） |
| --- | --- | --- |
| `quote` | `0 18 * * *`（北京 02:00） | `0 20 * * *`（北京 04:00） |
| `quote-gold` | `15 18 * * *`（北京 02:15） | `15 20 * * *`（北京 04:15） |
| `news` | `45 18 * * *`（北京 02:45） | `45 20 * * *`（北京 04:45） |
| **下一个可用** | **`30 18 * * *`（北京 02:30）** | **`30 20 * * *`（北京 04:30）** |

新分支取「下一个可用」那一档；取走后把该行改成新分支名，再把「下一个可用」换成再下一档
—— **本文件在模板分支上，新分支里也有一份，记得同步更新**。

> `30 18` / `30 20` 这一档**暂时还被一个待删除的分支占着**（`datatest`，已决定删除）。
> 它真正删掉之前**先别占这一档** —— 会跟它撞进同一个 concurrency 组排队。

> GitHub 对同一 concurrency 组只保留「1 个运行中 + 1 个待运行」，若前一个跑得过久、
> 下一个又到点，待运行的那个会被新来的顶掉 —— 靠错峰规避，所以**别把时间挤在一起**。

---

## 五、校验清单

建完之后逐项过一遍：

- [ ] `Migration.<new>.json` 的 `UploadFileListPath` 目录段是 `Branch/<new>/`（**不是** `branch-template`）
- [ ] 两份工作流的 `default:` 与 `ref: ... || '...'` 都是 `<new>`
- [ ] 两份工作流的文件名与 `name:` 都是 `<Pascal>` 形式
- [ ] `grep -rn '{BRANCH\|{CRON' . --exclude-dir=.git --exclude=BRANCH_TEMPLATE.md` 无输出
- [ ] `dev` 副本与分支副本的**关键配置一致**（`cron` / `concurrency.group` / `default` /
      `ref`），用「三、」里那段 `for … diff …` 比对通过
- [ ] 手动触发一次 **11 系列**（`workflow_dispatch`，选新分支），日志里
      `[INFO] 当前分支（BranchCurrent）= <new>`、`远端落点 = Branch/<new>/UploadFileList_….jsonl`
- [ ] 手动触发一次 **31 系列**，确认能取到上一步的分片并上传

---

## 六、注意事项（踩过的坑）

1. **`{BRANCH_PASCAL}` 必须先于 `{BRANCH}` 替换** —— 前者含后者，顺序反了会替出
   `<new>_PASCAL}` 这种残渣。上面的命令已按正确顺序排好。
2. **`Data/` 里的 7 个样例是给你跑通用的**（三种格式 × 二层路径各一份）。跑通之后按需
   清理；也可以直接用 `PullTestDataFiles` 工作流从 `ACANX/Distribution` 拉真实数据：
   ```
   source_owner=ACANX  source_repo=Distribution  source_branch=quote
   target_branch=<new>  pattern=*.json,*.mvsv,*.log
   ```
3. **`.log` 已在采集范围内**：`UpstreamFileList.DATA_EXTENSIONS = (".json", ".jsonl", ".mvsv", ".log")`，
   `Data/` 下的 `.log` 会走 11 系列采集与 31 系列上传。**这个常量属全分支共识项，各分支
   必须一致，别只改一个分支。**
4. **`OBSCIDRoutes` 是可选字段**：模板里的配置**没有**它 —— 不配即所有文件走默认 CID。
   需要按路径前缀路由到不同 CID 时，照 `quote` 的写法加：
   ```json
   {
     "UploadFileListPath": "Branch/<new>/UploadFileList.jsonl",
     "OBSCIDRoutes": [
       { "FilePrefix": "Data/Test/", "CID": "<真实CID>" },
       { "FilePrefix": "Data/Demo/", "CID": "<真实CID>" }
     ]
   }
   ```
   ⚠️ **顺序即优先级**（先配置先命中）；CID 必须填真实值，**别把占位符留在里面** ——
   填错的后果是把数据传到别人的 CID 下。
5. **脚本（`.github/Python/*.py`）各分支必须逐字节一致**，只靠各自的配置文件区分分支。
   新建分支时**原样拷贝**，不要顺手改脚本；改了就会与其它分支漂移。
6. **删掉本文件？** 随你。新分支里留着它没有副作用，只是内容会随模板更新而变旧。
7. **⚠️ `.gitignore` 里那条 `!Data/**/*.log` 别删** —— 这是本仓一个**真实的坑**：
   第 3 行的 `*.log` 来自 Node 模板，会把 `Data/` 下的 `.log` **静默挡在提交之外**。
   走 GitHub API 提交（采集/上传/PullTestDataFiles 都是）不受 `.gitignore` 约束，所以
   流水线一直没暴露这个问题；但**任何本地 `git add` 都会漏掉它们**，且不报任何错。
   本模板因此加了一条例外。`quote` / `quote-gold` / `news` 都已带上它；**`dev` 还没有** ——
   不过 `dev` 上不存数据（`Data/` 只存在于各独立分支），影响有限。
