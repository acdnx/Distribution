# GraftBranchHistory —— 分支历史「移花接木」

把一个分支的全部历史压缩成 **单个提交**，而文件内容 **逐字节不变**。
全程不克隆仓库、不传输任何文件内容，只调用 GitHub Git Data API。

| 文件 | 作用 |
| --- | --- |
| `.github/Bash/GraftBranchHistory.sh` | 全部逻辑（bash + `gh`，**不需要 jq**） |
| `.github/workflows/GraftBranchHistory.yml` | 手动触发入口，转发参数 |
| `.github/Bash/GraftBranchHistory.md` | 本文档 |

---

## 一、原理：为什么不用搬文件

Git 的提交对象就是一个四元组 `(tree, parents, message, author)`。
想要的效果是 **保留 tree、丢掉 parents**。

`tree` 的 SHA 是整棵文件树的 Merkle 根哈希 —— 它已经递归地把「全部文件名 + 内容 +
权限位」摘要进去了。所以只要新建一个 **无父提交**、`tree` 字段直接填原分支的 tree SHA：

- 文件内容、可执行位、符号链接、子模块(gitlink)条目 —— **一字未动，逐字节相同**；
- `parents` 为空，分支历史被整个丢弃，只剩 1 个提交。

> `新提交.tree == 原分支.tree` 就是一个 **密码学证明**（SHA 抗碰撞），
> 不需要再逐文件比对 —— 相等即「内容完全一致」。

对比另外两条路：

| 方案 | 做法 | 问题 |
| --- | --- | --- |
| 逐文件走 Contents API | `GET` 每个文件 → `PUT` 到新分支 | 每个文件产生 1 个提交（与目标相反）；撞 API 限流；有单文件体积上限；**建不出符号链接/子模块条目**；要证明内容一致还得再全量比对一遍 |
| 本地克隆 + 压缩 | `clone` → `rebase`/`orphan` → `push` | 结果正确，但要下载整个仓库（大仓库容易失败） |
| **本方案：换树** | 复用 `tree` SHA 建无父提交 | 只传几 KB JSON，秒级完成 |

注：若坚持用 git 命令本地做，等价写法是 `git commit-tree <原tree>`（不给 `-p`）——
**不要 checkout 任何文件**；克隆用 `--filter=blob:none --no-checkout` 就够。

---

## 二、安装

把两个文件提交到仓库：

```
.github/Bash/GraftBranchHistory.sh
.github/workflows/GraftBranchHistory.yml
```

```bash
chmod +x .github/Bash/GraftBranchHistory.sh   # 工作流里也会 chmod，本地执行需要
```

> ⚠️ **工作流文件必须存在于「默认分支」上**，才会出现在 Actions 的手动触发列表里。

---

## 三、前置条件与硬性限制

| 项目 | 要求 |
| --- | --- |
| 权限 | `permissions: contents: write`（已写在工作流里）。触发者需对仓库有 write 权限 |
| 凭据 | 默认用仓库自带的 `GITHUB_TOKEN`。若源分支受保护需要 bypass，或需要让新分支的创建去**触发其它工作流**，则新建 Secret `GRAFT_PAT`（PAT，`repo` 权限），工作流会自动优先使用 |
| **默认分支** | **不可改写、不可删除**。源分支若是默认分支，本方案走不通 —— 必须先把默认分支切到别处 |
| **受保护分支** | 带 `deletion` / `non_fast_forward` 规则（ruleset 或旧版保护）的分支，删除与强推都会被拒 —— 除非执行者在 bypass 名单里。`verify` 模式会先把生效规则打出来 |
| 依赖 | 只需 `gh`（Runner 自带）与 `base64` —— **不需要 jq**，也不需要任何第三方 action |

---

## 四、使用流程

四步：`verify`（预演） → `create`（建临时分支） → 人工核对 → `swap`（切换）。
`verify` 是默认模式，**不动任何东西**，建议永远先跑它。

### 0. 预演

```
Actions → GraftBranchHistory → Run workflow
  mode          : verify
  source_branch : latest
```

输出会列出：源分支 tip、tree SHA、父提交数、生效规则，以及「这个分支到底能不能动手」的结论。

### 1. 建临时分支

```
  mode          : create
  source_branch : latest
```

脚本会用源分支的 tree 造一个无父提交，建出 `new-latest`，然后**自校验**：
新引用指向正确、父提交数为 0、tree SHA 与源分支相同 —— 任一条不满足立即中止。
**此时源分支完全没被动过。**

运行摘要里会打印下一步要用的参数（含 `--expected-source-sha`）。

### 2. 人工核对

到 GitHub 上打开 `new-latest`，确认文件列表 / 内容无误。

### 3. 切换

```
  mode                : swap
  source_branch       : latest
  temp_branch         : new-latest
  expected_source_sha : <create 阶段输出的 tip SHA>
  on_drift            : abort
  keep_backup         : true
```

`swap` 会：

1. 校验临时分支存在、其 tree 与源分支当前 tree 一致、待切换提交无父提交；
2. 把源分支旧 tip 存到备份分支 `graft-backup/<源分支>-<UTC时间戳>`（可关）；
3. **强制移动**源分支指针到新提交 —— 一次 `PATCH`，**不存在「分支消失」的空窗**；
4. 删除临时分支；
5. 复核：源分支指向新提交、tree 与切换前一致、父提交数为 0。

### 4. 回滚（如需要）

```
  mode          : restore
  source_branch : latest
  restore_sha   : <切换前的 tip SHA>     # 留空则自动找最近的备份分支
```

### 命令行执行

同一套逻辑也能本地跑（需 `GH_TOKEN`）：

```bash
export GH_TOKEN=<token>
./.github/Bash/GraftBranchHistory.sh --mode verify --repo ACANX/Distribution --source latest
```

---

## 五、参数表

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--source <分支>` | 必填 | 源分支 |
| `--mode` | `verify` | `verify` / `create` / `swap` / `restore` |
| `--repo` | `$GITHUB_REPOSITORY` | `owner/name` |
| `--temp` | `new-<源分支>` | 临时分支名 |
| `--message` | 自动生成 | 新提交说明；默认内含原 tip SHA、tree SHA、运行链接 |
| `--author-name` / `--author-email` | 沿用源分支 tip 的作者 | 不指定则保持原署名，避免提交归属变掉 |
| `--expected-source-sha` | 空 | `swap` 的乐观并发校验值 |
| `--on-drift` | `abort` | `abort`=发现漂移就中止（推荐）；`retree`=以源分支**当前**内容重建提交 |
| `--keep-backup` | `true` | 切换前是否建备份分支 |
| `--restore-sha` | 空 | `restore` 的回滚目标；空则找最近的备份分支 |
| `--replace-temp` | 关 | 临时分支已存在时，删除后重建 |
| `--allow-protected` | 关 | 遇保护规则仅告警不中止（**需确认执行者具备 bypass 权限**） |
| `--dry-run` | 关 | 只打印计划，不写入 |

---

## 六、独立复核

想自己确认「内容真的一模一样」，用任意一条：

**A. 对比 tree SHA（最直接）**

```bash
b=latest; r=ACANX/Distribution
sha=$(gh api repos/$r/git/ref/heads/$b --jq .object.sha)
gh api repos/$r/git/commits/$sha --jq '{tree:.tree.sha, parents:(.parents|length)}'
```

`tree` 与切换前相同、`parents` 为 `0`，即为完成。

**B. 网页上比对**：打开分支的文件列表，或 `compare/<旧tip>...<新tip>` —— 文件层面不会有任何差异。

**C. 本地**：`git ls-remote` 确认指针，或把两个 tree SHA 拿来 `git cat-file -p` 对比。

---

## 七、已知局限（重要，别误解）

1. **删除/改写引用 ≠ 磁盘体积变小。**
   只要旧提交还被 **任何分支、标签、开放的 PR** 引用，它就依然可达；即便完全不可达，
   GitHub 也不会立即 GC，也不会回缩仓库已记录的 size。
   → 本方案解决的是「**这个分支的历史看起来干净、只剩一个提交**」。
   **如果目标是让以后 clone 变小，这条路达不到** —— 那需要开新仓库。

2. **与其他分支失去共同祖先。**
   派生自源分支的其它分支，改写后将与它没有共同祖先，后续 merge 会变成 unrelated histories。

3. **开放的 PR 会受影响。**
   以源分支为 base 的 open PR，其 diff 会发生剧烈变化。

4. **漂移竞态。**
   `swap` 的校验与 `PATCH` 之间有几秒窗口，期间若有自动化推送，那次推送会被丢弃。
   → 用 `--expected-source-sha` 把窗口缩到最小；或挑数据管线的静默时段执行。

5. **`GITHUB_TOKEN` 创建引用不会触发其它工作流**（GitHub 的既定行为，防止递归触发）。
   需要触发就用 `GRAFT_PAT`。

6. **备份分支会留着。**
   `graft-backup/*` 会一直存在直到你手动删除；它也意味着旧提交仍然可达。
   确认无误后按需清理：

   ```bash
   gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/graft-backup/<源分支>-<时间戳>
   ```

7. **无父提交无法与任何历史 merge。** 这是「只剩一个提交」的必然代价。

---

## 八、一句话总结

> 不搬文件，只换树。`新提交.tree == 原分支.tree` 即证明内容未变，`parents` 置空即完成压缩。
